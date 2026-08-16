from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from .index import SearchIndex, SentenceTransformerEmbedder
from .server import SearchServer


def load_embedder(disabled: bool):
    if disabled:
        return None
    try:
        return SentenceTransformerEmbedder()
    except ImportError:
        print(
            "Vector dependencies are not installed; falling back to keyword search. "
            "Run `uv sync --extra vector` to enable embeddings.",
            file=sys.stderr,
        )
        return None


def serve_managed(workspace: Path, no_vector: bool) -> None:
    token = os.environ.get("XANGI_EXTENSION_AUTH_TOKEN")
    if not token:
        raise RuntimeError("XANGI_EXTENSION_AUTH_TOKEN is required")

    resolved_workspace = workspace.expanduser().resolve()
    server = SearchServer(
        ("127.0.0.1", 0), None, auth_token=token, workspace=resolved_workspace
    )

    shutdown_requested = threading.Event()

    def request_shutdown() -> None:
        if shutdown_requested.is_set():
            return
        server.request_stop()
        shutdown_requested.set()

    def watch_parent() -> None:
        try:
            sys.stdin.buffer.read()
        finally:
            request_shutdown()

    signal.signal(signal.SIGTERM, lambda *_: request_shutdown())
    signal.signal(signal.SIGINT, lambda *_: request_shutdown())
    threading.Thread(
        target=watch_parent,
        daemon=True,
        name="xangi-search-parent-watch",
    ).start()

    server_thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
        name="xangi-search-http",
    )
    server_thread.start()

    print(
        json.dumps(
            {
                "schemaVersion": 2,
                "event": "ready",
                "id": "xangi-search",
                "baseUrl": f"http://127.0.0.1:{server.server_port}",
                "workspace": str(resolved_workspace),
                "pid": os.getpid(),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    def build_index(set_phase: Callable[[str], None]) -> SearchIndex:
        set_phase("loading_embedder")
        embedder = load_embedder(no_vector)
        set_phase("loading_index")
        return SearchIndex(resolved_workspace, embedder=embedder)

    server.start_initialization(build_index)

    try:
        shutdown_requested.wait()
    finally:
        server.shutdown()
        server_thread.join(timeout=2)
        server.server_close()
        server.wait_for_workers()
        server.close_index()


def main() -> None:
    parser = argparse.ArgumentParser(prog="xangi-search-extension")
    parser.add_argument("action", choices=["serve", "update"])
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--no-vector", action="store_true")
    args = parser.parse_args()

    if args.action == "serve":
        serve_managed(
            args.workspace,
            args.no_vector or os.environ.get("XANGI_SEARCH_NO_VECTOR") == "true",
        )
        return

    print(
        json.dumps(
            {
                "schemaVersion": 2,
                "id": "xangi-search",
                "ok": False,
                "unsupported": True,
                "detail": "checkout updates are managed by Git; packaged update support is not implemented",
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
