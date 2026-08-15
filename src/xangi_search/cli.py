from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .index import SearchIndex, SentenceTransformerEmbedder
from .evaluate import evaluate, load_queries
from .server import serve


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xangi-search")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--db", type=Path)
    parser.add_argument("--no-vector", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("index")
    search = subparsers.add_parser("search")
    search.add_argument("query")
    search.add_argument("--mode", choices=["hybrid", "vector", "keyword"], default="hybrid")
    search.add_argument("--limit", type=int, default=8)
    evaluation = subparsers.add_parser("evaluate")
    evaluation.add_argument("--queries", type=Path, required=True)
    evaluation.add_argument("--mode", choices=["hybrid", "vector", "keyword"], default="hybrid")
    evaluation.add_argument("--k", type=int, default=5)
    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=7891)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    embedder = load_embedder(args.no_vector)
    index = SearchIndex(args.workspace, args.db, embedder)
    if args.command == "index":
        print(json.dumps(index.reindex(), ensure_ascii=False))
        index.close()
        return
    if args.command == "search":
        print(
            json.dumps(
                index.search_payload(args.query, mode=args.mode, limit=args.limit),
                ensure_ascii=False,
                indent=2,
            )
        )
        index.close()
        return
    if args.command == "evaluate":
        print(
            json.dumps(
                evaluate(index, load_queries(args.queries), mode=args.mode, k=args.k),
                ensure_ascii=False,
                indent=2,
            )
        )
        index.close()
        return
    serve(index, args.host, args.port)


if __name__ == "__main__":
    main()
