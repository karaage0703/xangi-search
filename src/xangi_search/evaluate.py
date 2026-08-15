from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from pathlib import Path

from .index import SearchIndex


@dataclass(frozen=True)
class EvaluationQuery:
    query: str
    relevant: tuple[str, ...]


def load_queries(path: Path) -> list[EvaluationQuery]:
    queries: list[EvaluationQuery] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        query = value.get("query")
        relevant = value.get("relevant")
        if (
            not isinstance(query, str)
            or not query.strip()
            or not isinstance(relevant, list)
            or not relevant
            or not all(isinstance(item, str) and item for item in relevant)
        ):
            raise ValueError(f"invalid evaluation query at line {number}")
        queries.append(EvaluationQuery(query.strip(), tuple(relevant)))
    if not queries:
        raise ValueError("evaluation query file is empty")
    return queries


def percentile(values: list[float], percentage: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * percentage) - 1)
    return ordered[index]


def evaluate(index: SearchIndex, queries: list[EvaluationQuery], *, mode: str, k: int) -> dict[str, object]:
    recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    latencies: list[float] = []
    details: list[dict[str, object]] = []
    for item in queries:
        started = time.perf_counter()
        results = index.search(item.query, mode=mode, limit=k)
        elapsed_ms = (time.perf_counter() - started) * 1000
        latencies.append(elapsed_ms)
        returned = [result.file_path for result in results]
        relevant = set(item.relevant)
        hits = relevant.intersection(returned)
        recall = len(hits) / len(relevant)
        rank = next((position for position, path in enumerate(returned, 1) if path in relevant), 0)
        reciprocal_rank = 1 / rank if rank else 0.0
        recalls.append(recall)
        reciprocal_ranks.append(reciprocal_rank)
        details.append(
            {
                "query": item.query,
                "relevant": list(item.relevant),
                "returned": returned,
                "recall": round(recall, 4),
                "reciprocal_rank": round(reciprocal_rank, 4),
                "latency_ms": round(elapsed_ms, 3),
            }
        )
    return {
        "schema_version": 1,
        "mode": mode,
        "k": k,
        "queries": len(queries),
        f"recall@{k}": round(sum(recalls) / len(recalls), 4),
        f"mrr@{k}": round(sum(reciprocal_ranks) / len(reciprocal_ranks), 4),
        "p95_ms": round(percentile(latencies, 0.95), 3),
        "details": details,
    }
