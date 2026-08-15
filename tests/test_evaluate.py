import json
from pathlib import Path

from xangi_search.evaluate import evaluate, load_queries
from xangi_search.index import SearchIndex


def test_evaluation_metrics(tmp_path: Path):
    (tmp_path / "alpha.md").write_text("alpha extension registry", encoding="utf-8")
    (tmp_path / "beta.md").write_text("beta workspace search", encoding="utf-8")
    queries = tmp_path / "queries.jsonl"
    queries.write_text(
        "\n".join(
            [
                json.dumps({"query": "alpha registry", "relevant": ["alpha.md"]}),
                json.dumps({"query": "workspace search", "relevant": ["beta.md"]}),
            ]
        ),
        encoding="utf-8",
    )
    index = SearchIndex(tmp_path, tmp_path / "index.sqlite3")
    index.reindex()
    result = evaluate(index, load_queries(queries), mode="keyword", k=5)
    assert result["recall@5"] == 1.0
    assert result["mrr@5"] == 1.0
    assert result["p95_ms"] >= 0
    index.close()
