from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
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
    index = SearchIndex(resolved_workspace, embedder=load_embedder(no_vector))
    server = SearchServer(("127.0.0.1", 0), index, auth_token=token)
    server.start_lifecycle()

    shutdown_requested = threading.Event()

    def request_shutdown() -> None:
        if shutdown_requested.is_set():
            return
        shutdown_requested.set()
        threading.Thread(
            target=server.shutdown,
            daemon=True,
            name="xangi-search-shutdown",
        ).start()

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

    try:
        server.serve_forever()
    finally:
        server.server_close()
        index.close()


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
