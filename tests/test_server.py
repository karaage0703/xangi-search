import json
import os
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import numpy as np
import pytest

from xangi_search.index import SearchIndex
from xangi_search.server import SearchServer


class FakeEmbedder:
    def __call__(self, texts, *, query=False):
        values = []
        for text in texts:
            vector = np.array(
                [float("猫" in text), float("検索" in text)], dtype=np.float32
            )
            norm = np.linalg.norm(vector)
            values.append(vector / norm if norm else vector)
        return np.vstack(values)


def request(url: str, method: str = "GET", payload=None):
    body = json.dumps(payload).encode() if payload is not None else None
    try:
        with urlopen(
            Request(
                url,
                data=body,
                method=method,
                headers={"Content-Type": "application/json"} if body else {},
            ),
            timeout=2,
        ) as response:
            if response.headers.get_content_type() != "application/json":
                return response.status, response.read().decode()
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


def test_http_contract(tmp_path: Path):
    (tmp_path / "hello.md").write_text("hello workspace search", encoding="utf-8")
    (tmp_path / "archive").mkdir()
    (tmp_path / "documents").mkdir()
    (tmp_path / "sources" / "project-a").mkdir(parents=True)
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "tmp").mkdir()
    index = SearchIndex(tmp_path, tmp_path / "index.sqlite3")
    index.reindex()
    server = SearchServer(("127.0.0.1", 0), index)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        status, health = request(f"{base}/health")
        assert status == 200
        assert health["service"] == "xangi-search"
        assert "workspace.search" in health["capabilities"]

        status, page = request(f"{base}/ui")
        assert status == 200
        assert "xangi-search" in page
        assert "ディレクトリごとの重み" in page
        assert 'id="factsTab"' in page
        assert 'name="facts_snapshot_path"' in page
        assert "'/facts'" in page
        assert 'id="xangiHomeLink"' in page
        assert "[hidden] { display: none !important; }" in page

        status, settings = request(f"{base}/settings")
        assert status == 200
        assert settings["path_weights"] == {
            "archive/": 1.0,
            "documents/": 1.0,
            "sources/": 1.0,
        }
        assert settings["workspace_directories"] == [
            "archive/",
            "documents/",
            "sources/",
        ]
        assert settings["path_weight_exists"] == {
            "archive/": True,
            "documents/": True,
            "sources/": True,
        }
        assert settings["facts_snapshot_path"] == "knowledge/rag_facts.md"

        status, payload = request(f"{base}/search?q=hello&mode=keyword&k=5")
        assert status == 200
        assert payload["schema_version"] == 1
        assert payload["results"][0]["file_path"] == "hello.md"
        assert set(payload["timings_ms"]) == {"rag", "facts", "grep", "total"}
        assert "rag_timings_ms" in payload

        status, r2ag = request(f"{base}/search?q=hello&mode=keyword&k=5&r2ag=on")
        assert status == 200
        assert "**文書1** [hello.md]" in r2ag["r2ag"]

        status, _ = request(f"{base}/search")
        assert status == 400
        status, agent = request(
            f"{base}/agent",
            "POST",
            {
                "schemaVersion": 1,
                "prompt": "ワークスペース内から hello を検索して",
                "platform": "discord",
                "workspaceUrl": "https://xangi.example.test",
            },
        )
        assert status == 200
        assert agent["schemaVersion"] == 1
        assert "hello.md" in agent["result"]
        assert "https://xangi.example.test/workspace?path=hello.md" in agent["result"]
        status, _ = request(
            f"{base}/agent", "POST", {"schemaVersion": 2, "prompt": "hello"}
        )
        assert status == 400
        status, accepted = request(f"{base}/reindex", "POST")
        assert status == 202
        assert accepted["status"] == "accepted"
    finally:
        server.shutdown()
        server.server_close()
        index.close()


def test_search_uses_portable_fallback_for_files_outside_the_index(
    tmp_path: Path, monkeypatch
):
    (tmp_path / "unindexed.custom").write_text(
        "needle only in fallback", encoding="utf-8"
    )
    index = SearchIndex(tmp_path, tmp_path / "index.sqlite3")
    index.reindex()
    server = SearchServer(("127.0.0.1", 0), index)

    def missing_rg(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr("xangi_search.server.subprocess.run", missing_rg)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, payload = request(
            f"http://127.0.0.1:{server.server_port}/search?q=needle&mode=keyword&k=5"
        )
        assert status == 200
        assert payload["count"] == 0
        assert payload["grep_count"] == 1
        assert payload["grep_results"][0]["file_path"] == "unindexed.custom"
    finally:
        server.shutdown()
        server.server_close()
        index.close()


def test_settings_are_persisted_and_become_search_defaults(tmp_path: Path):
    (tmp_path / "hello.md").write_text("hello settings", encoding="utf-8")
    index = SearchIndex(tmp_path, tmp_path / "index.sqlite3")
    index.reindex()
    server = SearchServer(("127.0.0.1", 0), index)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        status, settings = request(
            f"{base}/settings",
            "PUT",
            {
                "mode": "keyword",
                "limit": 3,
                "min_score": 0.2,
                "auto_reindex": False,
                "reindex_interval_seconds": 600,
                "path_weights": {"hello/": 2.0},
                "default_path_weight": 0.8,
                "forgetting": True,
                "facts_snapshot_path": "exports/facts/current.md",
            },
        )
        assert status == 200
        assert settings["mode"] == "keyword"
        assert settings["path_weights"] == {"hello/": 2.0}
        assert settings["path_weight_exists"] == {"hello/": False}
        assert settings["forgetting"] is True
        assert settings["facts_snapshot_path"] == "exports/facts/current.md"
        assert (tmp_path / "exports" / "facts" / "current.md").is_file()
        assert not (tmp_path / "knowledge").exists()
        settings_path = tmp_path / ".xangi-search" / "settings.json"
        assert settings_path.is_file()
        assert os.stat(settings_path).st_mode & 0o077 == 0

        status, payload = request(f"{base}/search?q=hello")
        assert status == 200
        assert payload["mode"] == "keyword"
        assert payload["results"][0]["path_weight"] == 0.8
        assert payload["forgetting"] is True
    finally:
        server.shutdown()
        server.server_close()
        index.close()


def test_settings_reject_fact_snapshot_symlink_outside_workspace(tmp_path: Path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / "outside-link").symlink_to(outside, target_is_directory=True)
    index = SearchIndex(tmp_path, tmp_path / "index.sqlite3")
    server = SearchServer(("127.0.0.1", 0), index)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        status, _ = request(
            f"{base}/settings",
            "PUT",
            {"facts_snapshot_path": "outside-link/facts.md"},
        )
        assert status == 400
        status, settings = request(f"{base}/settings")
        assert status == 200
        assert settings["facts_snapshot_path"] == "knowledge/rag_facts.md"
    finally:
        server.shutdown()
        server.server_close()
        index.close()


def test_startup_index_runs_in_background_and_updates_health(tmp_path: Path):
    (tmp_path / "hello.md").write_text("background indexing", encoding="utf-8")
    index = SearchIndex(tmp_path, tmp_path / "index.sqlite3")
    server = SearchServer(("127.0.0.1", 0), index)
    server.start_lifecycle()
    deadline = time.monotonic() + 2
    while (
        not server.state_payload()["initial_index_complete"]
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)
    try:
        state = server.state_payload()
        assert state["initial_index_complete"] is True
        assert state["last_reindex_error"] is None
        assert index.stats()["files"] == 1
    finally:
        server.server_close()
        index.close()


def test_managed_initialization_keeps_health_responsive_and_routes_retryable(
    tmp_path: Path,
):
    release = threading.Event()
    factory_started = threading.Event()
    server = SearchServer(("127.0.0.1", 0), None, workspace=tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def factory(set_phase):
        set_phase("loading_index")
        factory_started.set()
        release.wait(timeout=2)
        return SearchIndex(tmp_path, tmp_path / "index.sqlite3")

    server.start_initialization(factory)
    assert factory_started.wait(timeout=1)
    try:
        started = time.monotonic()
        status, health = request(f"{base}/health")
        assert time.monotonic() - started < 0.5
        assert status == 200
        assert health["ready"] is False
        assert health["index_available"] is False
        assert health["initialization_phase"] == "loading_index"
        assert health["files"] == 0

        with pytest.raises(HTTPError) as unavailable:
            urlopen(f"{base}/search?q=hello", timeout=2)
        assert unavailable.value.code == 503
        assert unavailable.value.headers["Retry-After"] == "2"
        payload = json.load(unavailable.value)
        assert payload == {
            "error": "search index is unavailable",
            "retryable": True,
            "phase": "loading_index",
            "detail": "initialization in progress",
        }

        release.set()
        deadline = time.monotonic() + 2
        while not server.state_payload()["initial_index_complete"]:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        status, health = request(f"{base}/health")
        assert status == 200
        assert health["ready"] is True
        assert health["index_available"] is True
        assert health["initialization_phase"] == "ready"
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        server.close_index()


def test_shutdown_prevents_late_managed_index_attach(tmp_path: Path):
    release = threading.Event()
    factory_started = threading.Event()
    closed = threading.Event()
    created: list[SearchIndex] = []
    server = SearchServer(("127.0.0.1", 0), None, workspace=tmp_path)

    def factory(set_phase):
        set_phase("loading_index")
        factory_started.set()
        release.wait(timeout=2)
        index = SearchIndex(tmp_path, tmp_path / "index.sqlite3")
        original_close = index.close

        def record_close():
            original_close()
            closed.set()

        index.close = record_close
        created.append(index)
        return index

    server.start_initialization(factory)
    assert factory_started.wait(timeout=1)
    server.server_close()
    release.set()
    deadline = time.monotonic() + 2
    while not created:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert closed.wait(timeout=2)
    assert server.available_index() is None
    assert server.state_payload()["initialization_phase"] == "stopping"


def test_health_uses_cached_stats_instead_of_querying_sqlite(tmp_path: Path):
    index = SearchIndex(tmp_path, tmp_path / "index.sqlite3")
    server = SearchServer(("127.0.0.1", 0), index)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def fail_if_called():
        raise AssertionError("health must not query SQLite stats")

    index.stats = fail_if_called
    try:
        status, health = request(f"http://127.0.0.1:{server.server_port}/health")
        assert status == 200
        assert health["files"] == 0
    finally:
        server.shutdown()
        server.server_close()
        index.close()


def test_managed_initialization_error_remains_observable(tmp_path: Path):
    server = SearchServer(("127.0.0.1", 0), None, workspace=tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def factory(set_phase):
        set_phase("loading_index")
        raise RuntimeError("broken index")

    server.start_initialization(factory)
    deadline = time.monotonic() + 2
    while server.state_payload()["initialization_phase"] != "error":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        status, health = request(f"{base}/health")
        assert status == 200
        assert health["ready"] is False
        assert health["initialization_error"] == "broken index"
        assert health["detail"] == "initialization failed: broken index"

        with pytest.raises(HTTPError) as unavailable:
            urlopen(f"{base}/search?q=hello", timeout=2)
        assert unavailable.value.code == 503
        assert unavailable.value.headers["Retry-After"] == "2"
        assert json.load(unavailable.value)["retryable"] is False
    finally:
        server.shutdown()
        server.server_close()


def test_existing_snapshot_stays_ready_during_and_after_failed_refresh(tmp_path: Path):
    (tmp_path / "existing.md").write_text("usable snapshot", encoding="utf-8")
    index = SearchIndex(tmp_path, tmp_path / "index.sqlite3")
    index.reindex()
    refresh_started = threading.Event()
    release_refresh = threading.Event()

    def failing_refresh():
        refresh_started.set()
        release_refresh.wait(timeout=2)
        raise RuntimeError("refresh failed")

    index.reindex = failing_refresh
    server = SearchServer(("127.0.0.1", 0), index)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    server.start_lifecycle()
    assert refresh_started.wait(timeout=1)
    try:
        status, health = request(f"{base}/health")
        assert status == 200
        assert health["ready"] is True
        assert health["usable_snapshot"] is True
        assert health["reindex_in_progress"] is True
        assert health["detail"] == "ready; index refresh is running"

        status, payload = request(f"{base}/search?q=usable&mode=keyword")
        assert status == 200
        assert payload["results"][0]["file_path"] == "existing.md"

        release_refresh.set()
        deadline = time.monotonic() + 2
        while server.state_payload()["reindex_in_progress"]:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        status, health = request(f"{base}/health")
        assert status == 200
        assert health["ready"] is True
        assert health["degraded"] is True
        assert health["detail"] == "ready; last refresh failed: refresh failed"
    finally:
        release_refresh.set()
        server.shutdown()
        server.server_close()
        server.wait_for_workers()
        server.close_index()


def test_fresh_attached_index_returns_503_until_first_reindex_succeeds(tmp_path: Path):
    reindex_started = threading.Event()
    release_reindex = threading.Event()
    server = SearchServer(("127.0.0.1", 0), None, workspace=tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def factory(set_phase):
        set_phase("loading_index")
        index = SearchIndex(tmp_path, tmp_path / "index.sqlite3")
        original_reindex = index.reindex

        def blocking_first_reindex():
            reindex_started.set()
            release_reindex.wait(timeout=2)
            original_reindex()

        index.reindex = blocking_first_reindex
        return index

    server.start_initialization(factory)
    assert reindex_started.wait(timeout=1)
    try:
        status, health = request(f"{base}/health")
        assert status == 200
        assert health["index_available"] is True
        assert health["usable_snapshot"] is False
        assert health["ready"] is False
        assert health["reindex_in_progress"] is True

        with pytest.raises(HTTPError) as unavailable:
            urlopen(f"{base}/search?q=hello", timeout=2)
        assert unavailable.value.code == 503
        assert unavailable.value.headers["Retry-After"] == "2"
        assert json.load(unavailable.value) == {
            "error": "search index is unavailable",
            "retryable": True,
            "phase": "initial_reindex",
            "detail": "initial index refresh in progress",
        }

        release_reindex.set()
        deadline = time.monotonic() + 2
        while not server.state_payload()["usable_snapshot"]:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        status, payload = request(f"{base}/search?q=hello&mode=keyword")
        assert status == 200
        assert payload["count"] == 0
    finally:
        release_reindex.set()
        server.shutdown()
        server.server_close()
        server.wait_for_workers()
        server.close_index()


def test_shutdown_between_attach_and_lifecycle_prevents_reindex(tmp_path: Path):
    server = SearchServer(("127.0.0.1", 0), None, workspace=tmp_path)
    lifecycle_entered = threading.Event()
    release_lifecycle = threading.Event()
    reindex_called = threading.Event()
    original_start_lifecycle = server.start_lifecycle

    def delayed_start_lifecycle():
        lifecycle_entered.set()
        release_lifecycle.wait(timeout=2)
        return original_start_lifecycle()

    server.start_lifecycle = delayed_start_lifecycle

    def factory(set_phase):
        set_phase("loading_index")
        index = SearchIndex(tmp_path, tmp_path / "index.sqlite3")

        def record_reindex():
            reindex_called.set()

        index.reindex = record_reindex
        return index

    server.start_initialization(factory)
    assert lifecycle_entered.wait(timeout=1)
    assert server.available_index() is not None
    server.server_close()
    release_lifecycle.set()
    server.wait_for_workers()
    try:
        assert reindex_called.is_set() is False
        assert server.state_payload()["reindex_in_progress"] is False
    finally:
        server.close_index()


def test_shutdown_waits_for_active_reindex_before_index_close(tmp_path: Path):
    index = SearchIndex(tmp_path, tmp_path / "index.sqlite3")
    reindex_started = threading.Event()
    release_reindex = threading.Event()
    workers_stopped = threading.Event()

    def blocking_reindex():
        reindex_started.set()
        release_reindex.wait(timeout=2)

    index.reindex = blocking_reindex
    server = SearchServer(("127.0.0.1", 0), index)
    assert server.start_lifecycle() is True
    assert reindex_started.wait(timeout=1)
    server.server_close()

    def wait_for_workers():
        server.wait_for_workers()
        workers_stopped.set()

    waiter = threading.Thread(target=wait_for_workers)
    waiter.start()
    assert workers_stopped.wait(timeout=0.05) is False
    release_reindex.set()
    assert workers_stopped.wait(timeout=2)
    waiter.join(timeout=1)
    server.close_index()
    assert server.available_index() is None


def test_facts_http_contract_and_search_integration(tmp_path: Path):
    (tmp_path / "cats.md").write_text("猫の記録", encoding="utf-8")
    index = SearchIndex(tmp_path, tmp_path / "index.sqlite3", FakeEmbedder())
    index.reindex()
    server = SearchServer(("127.0.0.1", 0), index)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        status, settings = request(
            f"{base}/settings",
            "PUT",
            {"facts_snapshot_path": "exports/facts.md"},
        )
        assert status == 200
        status, created = request(
            f"{base}/facts", "POST", {"facts": [{"text": "猫の名前はウミ"}]}
        )
        assert status == 200
        fact_id = created["results"][0]["id"]
        snapshot = tmp_path / "exports" / "facts.md"
        assert "猫の名前はウミ" in snapshot.read_text(encoding="utf-8")

        status, payload = request(f"{base}/search?q={quote('猫')}&mode=hybrid")
        assert status == 200
        assert payload["facts_count"] == 1
        assert payload["facts"][0]["id"] == fact_id

        status, similar = request(f"{base}/facts/similar?q={quote('猫')}")
        assert status == 200
        assert similar["results"][0]["id"] == fact_id

        status, updated = request(
            f"{base}/facts/{fact_id}", "PUT", {"text": "猫の名前はソラ"}
        )
        assert status == 200
        assert updated["result"]["text"] == "猫の名前はソラ"

        status, deleted = request(f"{base}/facts/{fact_id}", "DELETE")
        assert status == 200
        assert deleted["result"]["is_active"] == 0
        assert "猫の名前はソラ" not in snapshot.read_text(encoding="utf-8")
    finally:
        server.shutdown()
        server.server_close()
        index.close()


def test_busy_query_encoder_degrades_explicitly_to_keyword(tmp_path: Path):
    (tmp_path / "hello.md").write_text("hello searchable", encoding="utf-8")
    index = SearchIndex(
        tmp_path,
        tmp_path / "index.sqlite3",
        FakeEmbedder(),
        query_encode_wait_seconds=0.01,
    )
    index.reindex()
    assert index._vector_search_lock.acquire(blocking=False)
    try:
        server = SearchServer(("127.0.0.1", 0), index)
        payload = server.search_payload("hello", mode="hybrid")
        assert payload["degraded"] is True
        assert payload["degraded_reason"] == "query_encoder_busy"
        assert payload["results"][0]["file_path"] == "hello.md"
        server.server_close()
    finally:
        index._vector_search_lock.release()
        index.close()
