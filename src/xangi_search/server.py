from __future__ import annotations

import hmac
import json
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, quote, urljoin, urlparse

from . import __version__
from .index import (
    DEFAULT_EXCLUDE_DIRECTORY_PATTERNS,
    DEFAULT_EXCLUDE_ROOT_DIRECTORY_PATTERNS,
    SearchIndex,
    is_excluded_directory,
)
from .settings import (
    SettingsStore,
    default_settings_path,
    validate_facts_snapshot_path,
)

GREP_EXTRA_EXCLUDE_DIRECTORY_PATTERNS = ("logs",)


class SearchServer(ThreadingHTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        index: SearchIndex | None,
        settings: SettingsStore | None = None,
        auth_token: str | None = None,
        *,
        workspace: Path | None = None,
    ):
        super().__init__(address, SearchHandler)
        if index is None and workspace is None:
            raise ValueError(
                "workspace is required until the search index is available"
            )
        resolved_workspace = (
            index.workspace if index is not None else workspace
        ).resolve()
        self.workspace = resolved_workspace
        self.index = index
        self.settings = settings or SettingsStore(
            default_settings_path(resolved_workspace),
            default_path_weights=initial_path_weights(resolved_workspace),
        )
        if index is not None:
            index.configure_facts_snapshot(
                self.settings.get().facts_snapshot_path, import_if_empty=True
            )
        self.auth_token = auth_token
        self.reindex_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.reindex_in_progress = False
        self.reindex_source: str | None = None
        self.reindex_started_at: float | None = None
        self.last_reindex_at: float | None = None
        self.last_reindex_duration_ms: float | None = None
        self.last_reindex_error: str | None = None
        self.reindex_count = 0
        self.initial_index_complete = False
        self.initialization_phase = "ready" if index is not None else "starting"
        self.initialization_error: str | None = None
        self.cached_stats: dict[str, int | float | str | None] = (
            index.stats()
            if index is not None
            else {
                "workspace": str(resolved_workspace),
                "files": 0,
                "chunks": 0,
                "vectors": 0,
                "last_indexed_at": None,
                "facts": 0,
            }
        )
        self.vector_enabled = index is not None and index.embedder is not None
        # A caller-supplied index preserves the standalone server contract: the
        # caller decides when it is usable. Managed initialization attaches a
        # fresh index later and derives snapshot usability from persisted data.
        self.usable_snapshot = index is not None
        self.stop_event = threading.Event()
        self.settings_changed = threading.Event()
        self.lifecycle_started = False
        self.initialization_thread: threading.Thread | None = None
        self.reindex_thread: threading.Thread | None = None
        self.auto_thread: threading.Thread | None = None

    def settings_payload(self) -> dict[str, object]:
        payload = self.settings.payload()
        directories = discover_workspace_directories(self.workspace)
        payload["workspace_directories"] = directories
        payload["path_weight_exists"] = {
            prefix: (self.workspace / prefix).is_dir()
            for prefix in payload["path_weights"]
        }
        return payload

    def available_index(self) -> SearchIndex | None:
        with self.state_lock:
            return self.index

    def require_index(self) -> SearchIndex:
        index = self.available_index()
        if index is None:
            raise RuntimeError("search index is not available")
        return index

    def set_initialization_phase(self, phase: str) -> None:
        with self.state_lock:
            if not self.stop_event.is_set():
                self.initialization_phase = phase

    def start_initialization(
        self, factory: Callable[[Callable[[str], None]], SearchIndex]
    ) -> None:
        """Construct and attach a managed index without blocking the HTTP listener."""

        def worker() -> None:
            created: SearchIndex | None = None
            try:
                self.set_initialization_phase("initializing")
                if self.stop_event.is_set():
                    return
                created = factory(self.set_initialization_phase)
                created.configure_facts_snapshot(
                    self.settings.get().facts_snapshot_path,
                    import_if_empty=True,
                )
                stats = created.stats()
                attached = False
                with self.state_lock:
                    if not self.stop_event.is_set():
                        self.index = created
                        self.vector_enabled = created.embedder is not None
                        self.cached_stats = stats
                        self.usable_snapshot = snapshot_is_usable(stats)
                        self.initialization_phase = "ready"
                        self.initialization_error = None
                        attached = True
                if not attached:
                    created.close()
                    return
                self.start_lifecycle()
            except Exception as cause:  # surfaced through constant-time /health
                if created is not None and self.available_index() is not created:
                    created.close()
                with self.state_lock:
                    if not self.stop_event.is_set():
                        self.initialization_phase = "error"
                        self.initialization_error = str(cause)

        thread = threading.Thread(
            target=worker, daemon=True, name="xangi-search-initialize"
        )
        with self.state_lock:
            if self.stop_event.is_set():
                return
            self.initialization_thread = thread
            thread.start()

    def refresh_cached_stats(self) -> None:
        index = self.available_index()
        if index is None:
            return
        stats = index.stats()
        with self.state_lock:
            if self.index is index:
                self.cached_stats = stats

    def close_index(self) -> None:
        with self.state_lock:
            index = self.index
            self.index = None
        if index is not None:
            index.close()

    def search_payload(
        self,
        query: str,
        *,
        mode: str | None = None,
        limit: int | None = None,
        min_score: float | None = None,
        forgetting: bool | None = None,
        context_chunks: int = 0,
        context_results: int = 3,
    ) -> dict[str, object]:
        defaults = self.settings.get()
        started = time.perf_counter()
        selected_mode = mode or defaults.mode
        selected_limit = max(1, min(30, limit or defaults.limit))
        selected_min_score = max(
            0.0, min(1.0, defaults.min_score if min_score is None else min_score)
        )
        selected_forgetting = defaults.forgetting if forgetting is None else forgetting
        if context_chunks < 0 or context_chunks > 2:
            raise ValueError("context_chunks must be between 0 and 2")
        if context_results < 1 or context_results > 4:
            raise ValueError("context_results must be between 1 and 4")
        index = self.require_index()
        with index.query_vector_context(query, enabled=selected_mode != "keyword") as (
            query_vector,
            degraded_reason,
        ):
            payload = index.search_payload(
                query,
                mode=selected_mode,
                limit=selected_limit,
                min_score=selected_min_score,
                path_weights=defaults.path_weights,
                default_path_weight=defaults.default_path_weight,
                query_vector=query_vector,
                allow_query_encoding=degraded_reason is None,
                forgetting=selected_forgetting,
                context_chunks=context_chunks,
                context_results=context_results,
            )
            rag_finished = time.perf_counter()
            fact_results = index.search_facts(query_vector, limit=3)
            facts_finished = time.perf_counter()
        rag_files = {result["file_path"] for result in payload["results"]}
        if payload["count"] >= selected_limit:
            grep_results: list[dict[str, str]] = []
            payload["grep_skipped_reason"] = "rag_results_sufficient"
        else:
            grep_results = [
                result
                for result in grep_search(query, self.workspace, max_results=5)
                if result["file_path"] not in rag_files
            ]
        finished = time.perf_counter()
        payload["rag_timings_ms"] = payload.pop("timings_ms")
        payload["timings_ms"] = {
            "rag": round((rag_finished - started) * 1000, 1),
            "facts": round((facts_finished - rag_finished) * 1000, 1),
            "grep": round((finished - facts_finished) * 1000, 1),
            "total": round((finished - started) * 1000, 1),
        }
        payload["elapsed_ms"] = payload["timings_ms"]["total"]
        payload["facts"] = fact_results
        payload["facts_count"] = len(fact_results)
        payload["grep_results"] = grep_results
        payload["grep_count"] = len(grep_results)
        payload["forgetting"] = selected_forgetting
        if degraded_reason:
            payload["degraded"] = True
            payload["degraded_reason"] = degraded_reason
        return payload

    def state_payload(self) -> dict[str, object]:
        with self.state_lock:
            return {
                "reindex_in_progress": self.reindex_in_progress,
                "reindex_source": self.reindex_source,
                "reindex_started_at": timestamp(self.reindex_started_at),
                "last_reindex": timestamp(self.last_reindex_at),
                "last_reindex_duration_ms": self.last_reindex_duration_ms,
                "last_reindex_error": self.last_reindex_error,
                "reindex_count": self.reindex_count,
                "initial_index_complete": self.initial_index_complete,
                "initialization_phase": self.initialization_phase,
                "initialization_error": self.initialization_error,
                "index_available": self.index is not None,
                "vector_enabled": self.vector_enabled,
                "usable_snapshot": self.usable_snapshot,
                "stats": dict(self.cached_stats),
            }

    def start_reindex(self, source: str) -> bool:
        if not self.reindex_lock.acquire(blocking=False):
            return False
        started_at = time.time()
        with self.state_lock:
            index = self.index
            if self.stop_event.is_set() or index is None:
                self.reindex_lock.release()
                return False
            self.reindex_in_progress = True
            self.reindex_source = source
            self.reindex_started_at = started_at
            self.last_reindex_error = None

        def worker() -> None:
            error: str | None = None
            stats: dict[str, int | float | str | None] | None = None
            try:
                index.reindex()
                stats = index.stats()
            except Exception as cause:  # surfaced through /health and doctor
                error = str(cause)
            finally:
                finished_at = time.time()
                with self.state_lock:
                    self.reindex_in_progress = False
                    self.reindex_source = None
                    self.reindex_started_at = None
                    self.last_reindex_error = error
                    if error is None:
                        self.last_reindex_at = finished_at
                        self.last_reindex_duration_ms = round(
                            (finished_at - started_at) * 1000, 1
                        )
                        self.reindex_count += 1
                        if source == "startup":
                            self.initial_index_complete = True
                        self.usable_snapshot = True
                        if stats is not None:
                            self.cached_stats = stats
                self.reindex_lock.release()

        thread = threading.Thread(
            target=worker, daemon=True, name=f"xangi-search-{source}-index"
        )
        with self.state_lock:
            if self.stop_event.is_set():
                self.reindex_in_progress = False
                self.reindex_source = None
                self.reindex_started_at = None
                self.reindex_lock.release()
                return False
            self.reindex_thread = thread
            thread.start()
        return True

    def start_lifecycle(self) -> bool:
        with self.state_lock:
            if self.stop_event.is_set() or self.lifecycle_started:
                return False
            self.lifecycle_started = True
        self.start_reindex("startup")

        def auto_reindex() -> None:
            while not self.stop_event.is_set():
                settings = self.settings.get()
                timeout = (
                    settings.reindex_interval_seconds if settings.auto_reindex else 60
                )
                changed = self.settings_changed.wait(timeout)
                self.settings_changed.clear()
                if self.stop_event.is_set() or changed:
                    continue
                if settings.auto_reindex:
                    self.start_reindex("auto")

        auto_thread = threading.Thread(
            target=auto_reindex, daemon=True, name="xangi-search-auto-index"
        )
        with self.state_lock:
            if self.stop_event.is_set():
                return False
            self.auto_thread = auto_thread
            auto_thread.start()
        return True

    def wait_for_workers(self) -> None:
        """Wait until workers that may access the index have stopped."""
        while True:
            with self.state_lock:
                threads = [
                    thread
                    for thread in (
                        self.initialization_thread,
                        self.reindex_thread,
                        self.auto_thread,
                    )
                    if thread is not None
                    and thread is not threading.current_thread()
                    and thread.is_alive()
                ]
            if not threads:
                return
            for thread in threads:
                thread.join()

    def request_stop(self) -> None:
        with self.state_lock:
            self.stop_event.set()
            self.settings_changed.set()
            self.initialization_phase = "stopping"

    def shutdown(self) -> None:
        self.request_stop()
        super().shutdown()

    def server_close(self) -> None:
        self.request_stop()
        super().server_close()


class SearchHandler(BaseHTTPRequestHandler):
    server: SearchServer

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        if content_type.startswith("text/html"):
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
                "form-action 'none'; frame-ancestors 'self'",
            )
        self.end_headers()
        self.wfile.write(body)

    def _json(
        self,
        status: int,
        payload: dict[str, object],
        headers: dict[str, str] | None = None,
    ) -> None:
        self._send(
            status,
            json.dumps(payload, ensure_ascii=False).encode(),
            "application/json; charset=utf-8",
            headers,
        )

    def _require_index(self) -> SearchIndex | None:
        state = self.server.state_payload()
        index = self.server.available_index()
        if index is not None and state["usable_snapshot"]:
            return index
        if state["index_available"]:
            if state["reindex_in_progress"]:
                phase = "initial_reindex"
                detail = "initial index refresh in progress"
            elif state["last_reindex_error"]:
                phase = "initial_reindex_failed"
                detail = f"initial index failed: {state['last_reindex_error']}"
            else:
                phase = "waiting_for_initial_reindex"
                detail = "waiting for the initial index refresh"
        else:
            phase = state["initialization_phase"]
            detail = state["initialization_error"] or "initialization in progress"
        self._json(
            503,
            {
                "error": "search index is unavailable",
                "retryable": not bool(
                    state["initialization_error"] or state["last_reindex_error"]
                ),
                "phase": phase,
                "detail": detail,
            },
            {"Retry-After": "2"},
        )
        return None

    def _authorized(self) -> bool:
        token = self.server.auth_token
        if token is None:
            return True
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {token}"
        return hmac.compare_digest(supplied, expected)

    def _require_authorization(self) -> bool:
        if self._authorized():
            return True
        self._json(401, {"error": "unauthorized"})
        return False

    def _json_body(self) -> dict[str, object]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("invalid Content-Length") from error
        if length > 64 * 1024:
            raise ValueError("request body is too large")
        try:
            value = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as error:
            raise ValueError("request body must be valid JSON") from error
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    def do_GET(self) -> None:
        if not self._require_authorization():
            return
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/ui"}:
            self._send(
                200,
                files("xangi_search").joinpath("ui.html").read_bytes(),
                "text/html; charset=utf-8",
            )
            return
        if parsed.path == "/health":
            settings = self.server.settings.payload()
            state = self.server.state_payload()
            stats = state.pop("stats")
            ready = bool(
                state["index_available"]
                and state["usable_snapshot"]
                and not state["initialization_error"]
            )
            if state["initialization_error"]:
                detail = f"initialization failed: {state['initialization_error']}"
            elif not state["index_available"]:
                detail = f"initialization phase: {state['initialization_phase']}"
            elif state["last_reindex_error"]:
                prefix = (
                    "ready; last refresh failed" if ready else "initial index failed"
                )
                detail = f"{prefix}: {state['last_reindex_error']}"
            elif not state["usable_snapshot"]:
                detail = "no usable index snapshot; initial index is still running"
            elif state["reindex_in_progress"]:
                detail = "ready; index refresh is running"
            else:
                detail = "ready"
            self._json(
                200,
                {
                    "status": "ok",
                    "service": "xangi-search",
                    "version": __version__,
                    "schema_version": 1,
                    "capabilities": [
                        "workspace.search",
                        "workspace.reindex",
                        "workspace.ui",
                        "workspace.facts",
                    ],
                    "auto_reindex": settings["auto_reindex"],
                    "reindex_interval_seconds": settings["reindex_interval_seconds"],
                    "ready": ready,
                    "degraded": bool(ready and state["last_reindex_error"]),
                    "detail": detail,
                    **state,
                    **stats,
                },
            )
            return
        if parsed.path == "/settings":
            self._json(200, self.server.settings_payload())
            return
        if parsed.path == "/facts":
            index = self._require_index()
            if index is None:
                return
            facts = index.list_facts()
            self._json(200, {"count": len(facts), "facts": facts})
            return
        fact_match = re.fullmatch(r"/facts/(\d+)", parsed.path)
        if fact_match:
            index = self._require_index()
            if index is None:
                return
            fact = index.get_fact(int(fact_match.group(1)))
            if fact is None:
                self._json(404, {"error": "fact not found"})
                return
            self._json(200, {"status": "ok", "result": fact})
            return
        if parsed.path == "/facts/similar":
            index = self._require_index()
            if index is None:
                return
            params = parse_qs(parsed.query)
            query = (params.get("q") or [""])[0].strip()
            if not query:
                self._json(400, {"error": "q is required"})
                return
            limit = max(1, min(30, int((params.get("k") or ["3"])[0])))
            started = time.perf_counter()
            results = index.find_similar_facts(query, limit)
            self._json(
                200,
                {
                    "query": query,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                    "count": len(results),
                    "results": results,
                },
            )
            return
        if parsed.path == "/search":
            if self._require_index() is None:
                return
            params = parse_qs(parsed.query)
            query = (params.get("q") or [""])[0].strip()
            if not query:
                self._json(400, {"error": "q is required"})
                return
            try:
                defaults = self.server.settings.get()
                payload = self.server.search_payload(
                    query,
                    mode=(params.get("mode") or [defaults.mode])[0],
                    limit=int((params.get("k") or [str(defaults.limit)])[0]),
                    min_score=float((params.get("s") or [str(defaults.min_score)])[0]),
                    forgetting=(
                        (params.get("forgetting") or [str(defaults.forgetting)])[
                            0
                        ].lower()
                        in {"1", "true", "yes", "on"}
                    ),
                    context_chunks=int((params.get("context_chunks") or ["0"])[0]),
                    context_results=int((params.get("context_results") or ["3"])[0]),
                )
            except (ValueError, TypeError) as error:
                self._json(400, {"error": str(error)})
                return
            r2ag = (params.get("r2ag") or [""])[0].lower() in {"1", "true", "yes", "on"}
            if r2ag and payload["results"]:
                payload["r2ag"] = format_r2ag(payload["results"])
            self._json(200, payload)
            return
        if parsed.path == "/file":
            requested = (parse_qs(parsed.query).get("path") or [""])[0]
            try:
                target = (self.server.workspace / requested).resolve()
                target.relative_to(self.server.workspace)
                if not target.is_file() or target.stat().st_size > 100 * 1024:
                    raise ValueError
                body = target.read_text(encoding="utf-8").encode()
            except (OSError, UnicodeDecodeError, ValueError):
                self._json(404, {"error": "file not found"})
                return
            self._send(200, body, "text/plain; charset=utf-8")
            return
        self._json(404, {"error": "not found"})

    def do_PUT(self) -> None:
        if not self._require_authorization():
            return
        parsed = urlparse(self.path)
        fact_match = re.fullmatch(r"/facts/(\d+)", parsed.path)
        if fact_match:
            index = self._require_index()
            if index is None:
                return
            try:
                result = index.update_fact(int(fact_match.group(1)), self._json_body())
            except ValueError as error:
                self._json(400, {"error": str(error)})
                return
            if result is None:
                self._json(404, {"error": "fact not found"})
                return
            self.server.refresh_cached_stats()
            self._json(200, {"status": "ok", "result": result})
            return
        if parsed.path != "/settings":
            self._json(404, {"error": "not found"})
            return
        try:
            body = self._json_body()
            index = self._require_index()
            if index is None:
                return
            if "facts_snapshot_path" in body:
                candidate = validate_facts_snapshot_path(body["facts_snapshot_path"])
                index.validate_facts_snapshot_location(candidate)
            settings = self.server.settings.update(body)
            index.configure_facts_snapshot(settings.facts_snapshot_path)
        except ValueError as error:
            self._json(400, {"error": str(error)})
            return
        except OSError as error:
            self._json(500, {"error": f"could not write facts snapshot: {error}"})
            return
        self.server.settings_changed.set()
        self._json(200, self.server.settings_payload())

    def do_POST(self) -> None:
        if not self._require_authorization():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/agent":
            if self._require_index() is None:
                return
            try:
                body = self._json_body()
                if body.get("schemaVersion") != 1:
                    raise ValueError("schemaVersion must be 1")
                prompt = body.get("userText") or body.get("prompt")
                if not isinstance(prompt, str):
                    raise TypeError("prompt must be a string")
                query = normalize_agent_query(prompt)
                if not query:
                    raise ValueError("search query is empty")
                platform = body.get("platform")
                workspace_url = body.get("workspaceUrl")
                if platform is not None and not isinstance(platform, str):
                    raise ValueError("platform must be a string")
                if workspace_url is not None and not isinstance(workspace_url, str):
                    raise ValueError("workspaceUrl must be a string")
                payload = self.server.search_payload(query)
                result = format_agent_results(
                    query,
                    payload,
                    platform=platform,
                    workspace_url=workspace_url,
                )
            except (ValueError, TypeError) as error:
                self._json(400, {"error": str(error)})
                return
            self._json(200, {"schemaVersion": 1, "result": result})
            return
        if parsed.path in {"/facts", "/extract"}:
            index = self._require_index()
            if index is None:
                return
            try:
                body = self._json_body()
                facts = body.get("facts")
                if not isinstance(facts, list) or not facts:
                    raise ValueError("facts must be a non-empty array")
                if not all(isinstance(fact, dict) for fact in facts):
                    raise ValueError("facts entries must be objects")
                results = index.add_facts(facts)
                self.server.refresh_cached_stats()
            except ValueError as error:
                self._json(400, {"error": str(error)})
                return
            self._json(
                200,
                {
                    "status": "ok",
                    "results": results,
                    "total_facts": self.server.state_payload()["stats"]["facts"],
                },
            )
            return
        if parsed.path != "/reindex":
            self._json(404, {"error": "not found"})
            return
        if self._require_index() is None:
            return
        if not self.server.start_reindex("manual"):
            self._json(409, {"error": "reindex already in progress"})
            return
        self._json(202, {"status": "accepted"})

    def do_DELETE(self) -> None:
        if not self._require_authorization():
            return
        match = re.fullmatch(r"/facts/(\d+)", urlparse(self.path).path)
        if not match:
            self._json(404, {"error": "not found"})
            return
        index = self._require_index()
        if index is None:
            return
        result = index.delete_fact(int(match.group(1)))
        self.server.refresh_cached_stats()
        if result is None:
            self._json(404, {"error": "fact not found"})
            return
        self._json(200, {"status": "ok", "result": result})


def discover_workspace_directories(workspace: Path) -> list[str]:
    try:
        children = workspace.iterdir()
    except OSError:
        return []
    return sorted(
        f"{child.name}/"
        for child in children
        if child.is_dir()
        and not child.is_symlink()
        and not child.name.startswith(".")
        and not is_excluded_directory(child.name.lower(), at_workspace_root=True)
    )


def initial_path_weights(workspace: Path) -> dict[str, float]:
    return {directory: 1.0 for directory in discover_workspace_directories(workspace)}


def timestamp(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def snapshot_is_usable(stats: dict[str, int | float | str | None]) -> bool:
    return bool(stats.get("files") or stats.get("facts"))


WEB_URL_PATTERN = re.compile(r'https?://[^\s<>"\'、。！？，；：「」『』【】〈〉《》]+')
TRAILING_URL_PUNCTUATION = re.compile(r"[.,;:!?、。！？，；：」』】〉》]+$")


def normalize_agent_query(value: str) -> str:
    original = re.sub(r"\s+", " ", re.sub(r"<@[!&]?\d+>", " ", value)).strip()
    normalized = re.sub(
        r"(?:に関する|についての|の)?(?:ファイル|資料|文書|メモ|コード)?を?"
        r"(?:検索|探)(?:して|してほしい|してください|して下さい)?[。.!！?？]*$",
        "",
        re.sub(r"^ワークスペース(?:内|の中)?(?:から|で|の)?\s*", "", original),
    )
    normalized = re.sub(
        r"(?:に関する|についての|の)?(?:ファイル|資料|文書|メモ|コード)?"
        r"(?:は|が)?(?:どこ|何処)(?:に)?(?:ある|ありますか)?[。.!！?？]*$",
        "",
        normalized,
    ).strip()
    return normalized or original


def extract_web_urls(content: str) -> list[str]:
    urls: list[str] = []
    for match in WEB_URL_PATTERN.findall(content):
        candidate = TRAILING_URL_PUNCTUATION.sub("", match)
        parsed = urlparse(candidate)
        if (
            parsed.scheme in {"http", "https"}
            and parsed.netloc
            and candidate not in urls
        ):
            urls.append(candidate)
    return urls[:3]


def _agent_excerpt(content: object) -> str:
    if not isinstance(content, str):
        return ""
    compact = re.sub(r"\[([^\]]+)\]\((?:[^()]|\([^()]*\))*\)", r"\1", content)
    compact = WEB_URL_PATTERN.sub("", compact).replace("<>", "")
    compact = re.sub(r"\s+", " ", compact).strip()
    return compact if len(compact) <= 160 else f"{compact[:157]}..."


def _workspace_file_link(
    file_path: str, *, platform: str | None, workspace_url: str | None
) -> str:
    label = file_path.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    relative = f"/workspace?path={quote(file_path, safe='')}"
    if platform and platform != "web":
        if not workspace_url:
            escaped_path = file_path.replace("`", "\\`")
            return f"`{escaped_path}`"
        parsed = urlparse(workspace_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("workspaceUrl must use HTTP(S)")
        return f"[{label}]({urljoin(workspace_url.rstrip('/') + '/', relative.lstrip('/'))})"
    return f"[{label}]({relative})"


def format_agent_results(
    query: str,
    payload: dict[str, object],
    *,
    platform: str | None = None,
    workspace_url: str | None = None,
) -> str:
    unique: dict[str, dict[str, object]] = {}
    for raw in payload.get("results", []):
        if not isinstance(raw, dict):
            continue
        file_path = raw.get("file_path")
        if isinstance(file_path, str) and file_path and file_path not in unique:
            unique[file_path] = raw
    if not unique:
        return f"「{query}」に一致するファイルは見つかりませんでした。"
    elapsed = payload.get("elapsed_ms")
    elapsed_text = f"、{round(elapsed)}ms" if isinstance(elapsed, (int, float)) else ""
    lines = [f"「{query}」の検索結果（{len(unique)}ファイル{elapsed_text}）", ""]
    entries = list(unique.items())
    for index, (file_path, result) in enumerate(entries):
        score = result.get("score")
        score_text = f" — 関連度 {score:.2f}" if isinstance(score, (int, float)) else ""
        content = result.get("content")
        preview = _agent_excerpt(content)
        web_urls = extract_web_urls(content) if isinstance(content, str) else []
        lines.append(
            f"- {_workspace_file_link(file_path, platform=platform, workspace_url=workspace_url)}"
            f"{score_text}{'  ' if preview or web_urls else ''}"
        )
        if preview:
            lines.append(f"  {preview}")
        if web_urls:
            lines.append(f"  Web: {' '.join(f'<{url}>' for url in web_urls)}")
        if index < len(entries) - 1:
            lines.append("")
    return "\n".join(lines)


def grep_search(
    query: str, workspace: Path, max_results: int = 10
) -> list[dict[str, str]]:
    command = [
        "rg",
        "-i",
        "-l",
        "--max-count",
        "1",
        "--glob",
        "!*.js",
        "--glob",
        "!*.min.js",
        "--glob",
        "!*.bundle.js",
        "--glob",
        "!*.pyc",
        "--glob",
        "!*.png",
        "--glob",
        "!*.jpg",
        "--glob",
        "!*.jpeg",
        "--glob",
        "!*.gif",
        "--glob",
        "!*.mp3",
        "--glob",
        "!*.mp4",
        "--glob",
        "!*.pdf",
        "--glob",
        "!*.zip",
        "--glob",
        "!*.lock",
    ]
    for pattern in (
        *DEFAULT_EXCLUDE_DIRECTORY_PATTERNS,
        *GREP_EXTRA_EXCLUDE_DIRECTORY_PATTERNS,
    ):
        command.extend(("--glob", f"!{pattern}/**"))
        command.extend(("--glob", f"!**/{pattern}/**"))
    for pattern in DEFAULT_EXCLUDE_ROOT_DIRECTORY_PATTERNS:
        command.extend(("--glob", f"!{pattern}/**"))
    command.extend(("--", query, str(workspace)))
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=5, check=False
        )
    except FileNotFoundError:
        return python_grep_search(query, workspace, max_results)
    except (subprocess.TimeoutExpired, OSError):
        return []
    files_found: list[str] = []
    for line in completed.stdout.splitlines():
        try:
            absolute = Path(line)
            if not absolute.is_absolute():
                absolute = workspace / absolute
            absolute = absolute.resolve()
            absolute.relative_to(workspace.resolve())
            relative = Path(os.path.relpath(absolute, workspace)).as_posix()
            if relative not in files_found:
                files_found.append(relative)
        except (TypeError, ValueError):
            continue
    results: list[dict[str, str]] = []
    for file_path in files_found[:max_results]:
        try:
            context = subprocess.run(
                [
                    "rg",
                    "-i",
                    "-n",
                    "-C",
                    "2",
                    "--max-count",
                    "3",
                    "--",
                    query,
                    str(workspace / file_path),
                ],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            ).stdout.strip()[:500]
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            context = ""
        results.append({"file_path": file_path, "context": context, "source": "grep"})
    return results


def python_grep_search(
    query: str, workspace: Path, max_results: int = 10
) -> list[dict[str, str]]:
    needle = query.casefold()
    if not needle:
        return []
    excluded_suffixes = {
        ".gif",
        ".jpeg",
        ".jpg",
        ".lock",
        ".mp3",
        ".mp4",
        ".pdf",
        ".png",
        ".pyc",
        ".zip",
    }
    results: list[dict[str, str]] = []
    for root, directories, filenames in os.walk(workspace, followlinks=False):
        at_workspace_root = Path(root) == workspace
        directories[:] = [
            name
            for name in directories
            if not is_excluded_directory(
                name,
                at_workspace_root=at_workspace_root,
                extra_patterns=GREP_EXTRA_EXCLUDE_DIRECTORY_PATTERNS,
            )
            and not (Path(root) / name).is_symlink()
        ]
        for name in filenames:
            path = Path(root) / name
            lower_name = name.lower()
            if (
                path.is_symlink()
                or path.suffix.lower() in excluded_suffixes
                or path.suffix.lower() == ".js"
                or lower_name.endswith((".min.js", ".bundle.js"))
            ):
                continue
            try:
                if path.stat().st_size > 1024 * 1024:
                    continue
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            matches = [
                index for index, line in enumerate(lines) if needle in line.casefold()
            ]
            if not matches:
                continue
            context: list[str] = []
            for line_index in matches[:3]:
                start = max(0, line_index - 2)
                end = min(len(lines), line_index + 3)
                context.extend(
                    f"{index + 1}:{lines[index]}" for index in range(start, end)
                )
            results.append(
                {
                    "file_path": path.relative_to(workspace).as_posix(),
                    "context": "\n".join(context)[:500],
                    "source": "grep",
                }
            )
            if len(results) >= max_results:
                return results
    return results


def format_r2ag(results: list[dict[str, object]]) -> str:
    text = (
        "以下の文書を参考に質問に答えてください。\n関連度が高いほど信頼できます。\n\n"
    )
    for index, result in enumerate(results, 1):
        score = float(result["score"])
        label = "高" if score >= 0.7 else "中" if score >= 0.5 else "低"
        text += (
            f"**文書{index}** [{result['file_path']}] [関連度: {score:.2f} ({label})]\n"
        )
        text += f"{str(result['content'])[:300]}...\n\n"
    return text


def serve(index: SearchIndex, host: str, port: int) -> None:
    server = SearchServer((host, port), index)
    server.start_lifecycle()
    try:
        server.serve_forever()
    finally:
        server.server_close()
        server.wait_for_workers()
        server.close_index()
