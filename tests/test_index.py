import threading
import time
from pathlib import Path

import numpy as np

from xangi_search.index import SearchIndex, chunk_text


class FakeEmbedder:
    def __call__(self, texts, *, query=False):
        values = []
        for text in texts:
            lowered = text.lower()
            vector = np.array(
                [
                    float("cat" in lowered or "猫" in lowered),
                    float("search" in lowered or "検索" in lowered),
                ],
                dtype=np.float32,
            )
            norm = np.linalg.norm(vector)
            values.append(vector / norm if norm else vector)
        return np.vstack(values)


def test_chunk_text_uses_overlap():
    assert chunk_text("abcdefghij", size=6, overlap=2) == ["abcdef", "efghij", "ij"]


def test_reindex_and_hybrid_search(tmp_path: Path):
    (tmp_path / "cats.md").write_text("猫のウミとソラについての記録", encoding="utf-8")
    (tmp_path / "search.md").write_text(
        "workspace search architecture", encoding="utf-8"
    )
    index = SearchIndex(tmp_path, tmp_path / "index.sqlite3", FakeEmbedder())
    assert index.reindex()["changed_files"] == 2

    result = index.search_payload("猫", mode="hybrid", limit=5)
    assert result["schema_version"] == 1
    assert result["results"][0]["file_path"] == "cats.md"

    assert index.reindex()["changed_files"] == 0
    (tmp_path / "cats.md").unlink()
    assert index.reindex()["removed_files"] == 1
    index.close()


def test_keyword_search_returns_a_matching_file(tmp_path: Path):
    (tmp_path / "design.md").write_text(
        "xangi-search extension contract", encoding="utf-8"
    )
    index = SearchIndex(tmp_path, tmp_path / "index.sqlite3")
    index.reindex()
    results = index.search("xangi-search", mode="keyword", limit=5)
    assert results[0].file_path == "design.md"
    index.close()


def test_keyword_search_retries_long_japanese_query_with_bounded_anchors(
    tmp_path: Path,
):
    (tmp_path / "open-data.md").write_text(
        "愛知県ではGIS形式の公共施設情報をオープンデータとして公開しています。",
        encoding="utf-8",
    )
    index = SearchIndex(tmp_path, tmp_path / "index.sqlite3")
    index.reindex()

    query = "愛知県と県内市町村のGISや公共施設情報をまとめて探せるオープンデータ調査"
    assert index._fts_query(query) != index._fts_fallback_query(query)
    results = index.search(query, mode="keyword", limit=5)
    hybrid_without_vectors = index.search(query, mode="hybrid", limit=5)

    assert results[0].file_path == "open-data.md"
    assert hybrid_without_vectors[0].file_path == "open-data.md"
    assert hybrid_without_vectors[0].vector_score == 0
    assert len(index._fts_fallback_query(query).split(" OR ")) <= 12
    index.close()


def test_keyword_fallback_preserves_ascii_identifiers():
    expression = SearchIndex._fts_fallback_query(
        "ESP-SRを使ってARM64上で日本語音声認識を行う実装メモ"
    )

    assert '"ESP-SR"' in expression
    assert '"ARM64"' in expression
    assert len(expression.split(" OR ")) <= 12


def test_keyword_search_does_not_retry_when_primary_query_matches(
    tmp_path: Path, monkeypatch
):
    (tmp_path / "design.md").write_text(
        "xangi-search extension contract", encoding="utf-8"
    )
    index = SearchIndex(tmp_path, tmp_path / "index.sqlite3")
    index.reindex()

    def unexpected_fallback(_query: str) -> str:
        raise AssertionError(
            "fallback should only run after a zero-result primary query"
        )

    monkeypatch.setattr(index, "_fts_fallback_query", unexpected_fallback)

    assert index.search("xangi-search", mode="keyword")[0].file_path == "design.md"
    index.close()


def test_hybrid_skips_keyword_fallback_when_vectors_are_available(tmp_path: Path):
    (tmp_path / "search.md").write_text(
        "検索機能についての設計資料です。", encoding="utf-8"
    )
    index = SearchIndex(tmp_path, tmp_path / "index.sqlite3", FakeEmbedder())
    index.reindex()

    results = index.search(
        "検索機能を使って目的の資料を見つける方法", mode="hybrid", limit=5
    )

    assert results[0].file_path == "search.md"
    assert results[0].vector_score > 0
    assert results[0].keyword_score == 0
    index.close()


def test_hybrid_search_falls_back_to_keyword_scores_without_vectors(tmp_path: Path):
    (tmp_path / "design.md").write_text(
        "xangi-search extension contract", encoding="utf-8"
    )
    index = SearchIndex(tmp_path, tmp_path / "index.sqlite3")
    index.reindex()

    results = index.search("xangi-search", mode="hybrid", limit=5, min_score=0.3)

    assert results[0].file_path == "design.md"
    assert results[0].score == results[0].keyword_score
    assert results[0].vector_score == 0.0
    index.close()


def test_query_embeddings_are_cached_and_shared_by_concurrent_calls(tmp_path: Path):
    started = threading.Event()
    release = threading.Event()
    calls = 0

    class BlockingEmbedder(FakeEmbedder):
        def __call__(self, texts, *, query=False):
            nonlocal calls
            if query:
                calls += 1
                started.set()
                release.wait(timeout=1)
            return super().__call__(texts, query=query)

    index = SearchIndex(tmp_path, tmp_path / "index.sqlite3", BlockingEmbedder())
    results = []
    first = threading.Thread(target=lambda: results.append(index.encode_query("猫")))
    second = threading.Thread(target=lambda: results.append(index.encode_query("猫")))
    first.start()
    assert started.wait(timeout=1)
    second.start()
    time.sleep(0.01)
    release.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert calls == 1
    assert len(results) == 2
    assert np.array_equal(results[0], results[1])
    assert np.array_equal(index.encode_query("猫"), results[0])
    assert calls == 1
    index.close()


def test_query_cache_configuration_is_validated(tmp_path: Path):
    with np.testing.assert_raises_regex(ValueError, "query_cache_size"):
        SearchIndex(tmp_path, query_cache_size=0)
    with np.testing.assert_raises_regex(ValueError, "query_encode_wait_seconds"):
        SearchIndex(tmp_path, query_encode_wait_seconds=-1)


def test_reindex_prunes_excluded_directories(tmp_path: Path):
    (tmp_path / "kept.md").write_text("kept content", encoding="utf-8")
    excluded = tmp_path / "tmp"
    excluded.mkdir()
    (excluded / "ignored.md").write_text("ignored content", encoding="utf-8")
    index = SearchIndex(tmp_path, tmp_path / "index.sqlite3")
    index.reindex()
    assert [
        result.file_path for result in index.search("ignored", mode="keyword")
    ] == []
    assert index.search("kept", mode="keyword")[0].file_path == "kept.md"
    index.close()


def test_path_weights_are_configurable_and_longest_prefix_wins(tmp_path: Path):
    notes = tmp_path / "notes"
    private = notes / "private"
    private.mkdir(parents=True)
    (notes / "public.md").write_text("same searchable phrase", encoding="utf-8")
    (private / "secret.md").write_text("same searchable phrase", encoding="utf-8")
    index = SearchIndex(tmp_path, tmp_path / "index.sqlite3")
    index.reindex()

    results = index.search(
        "searchable",
        mode="keyword",
        path_weights={"notes/": 0.5, "notes/private/": 2.0},
    )

    assert results[0].file_path == "notes/private/secret.md"
    assert results[0].path_weight == 2.0
    assert results[1].path_weight == 0.5
    index.close()


def test_reindex_keeps_legacy_text_types_and_prunes_generated_files(tmp_path: Path):
    (tmp_path / "schema.graphql").write_text(
        "legacy searchable schema", encoding="utf-8"
    )
    (tmp_path / "script.bash").write_text("legacy searchable shell", encoding="utf-8")
    (tmp_path / ".env").write_text("LEGACY_SEARCHABLE_ENV=yes", encoding="utf-8")
    (tmp_path / ".dockerignore").write_text("legacy_searchable_cache", encoding="utf-8")
    (tmp_path / "package-lock.json").write_text(
        "legacy searchable lock", encoding="utf-8"
    )
    (tmp_path / "bundle.js").write_text("legacy searchable bundle", encoding="utf-8")
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    (knowledge / "rag_facts.md").write_text(
        "legacy searchable generated", encoding="utf-8"
    )
    index = SearchIndex(tmp_path, tmp_path / "index.sqlite3")
    index.reindex()

    paths = {
        result.file_path for result in index.search("legacy", mode="keyword", limit=10)
    }
    assert paths == {".dockerignore", ".env", "schema.graphql", "script.bash"}
    index.close()


def test_search_payload_includes_ranking_explanation_and_phase_timings(tmp_path: Path):
    (tmp_path / "notes.md").write_text("timing searchable", encoding="utf-8")
    index = SearchIndex(tmp_path, tmp_path / "index.sqlite3")
    index.reindex()

    payload = index.search_payload("timing", mode="keyword")

    assert set(payload["timings_ms"]) == {
        "fts",
        "vector",
        "load_candidates",
        "rank_and_build",
    }
    assert payload["results"][0]["base_score"] > 0
    assert payload["results"][0]["fts_score"] == payload["results"][0]["keyword_score"]
    index.close()


def test_facts_crud_similarity_and_snapshot(tmp_path: Path):
    index = SearchIndex(tmp_path, tmp_path / "index.sqlite3", FakeEmbedder())
    created = index.add_facts(
        [
            {
                "text": "猫のウミについて",
                "source_file": "notes/cats.md",
                "fact_date": "2026-08-15",
            }
        ]
    )
    fact_id = created[0]["id"]

    similar = index.find_similar_facts("猫", limit=3)
    assert similar[0]["id"] == fact_id
    assert index.stats()["facts"] == 1
    assert "猫のウミについて" in (tmp_path / "knowledge" / "rag_facts.md").read_text()

    updated = index.update_fact(fact_id, {"text": "猫のソラについて"})
    assert updated["text"] == "猫のソラについて"
    deleted = index.delete_fact(fact_id)
    assert deleted["is_active"] == 0
    assert index.stats()["facts"] == 0
    index.close()


def test_forgetting_decay_is_opt_in_and_reinforces_returned_chunks(tmp_path: Path):
    memory = tmp_path / "memory"
    memory.mkdir()
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    (tmp_path / "MEMORY.md").write_text("same retained phrase", encoding="utf-8")
    (memory / "20200101.md").write_text("same retained phrase", encoding="utf-8")
    (knowledge / "20200101.md").write_text("same retained phrase", encoding="utf-8")
    index = SearchIndex(tmp_path, tmp_path / "index.sqlite3")
    index.reindex()

    without_decay = index.search(
        "retained", mode="keyword", path_weights={}, forgetting=False, limit=5
    )
    assert all(result.score == result.base_score for result in without_decay)

    with_decay = index.search(
        "retained", mode="keyword", path_weights={}, forgetting=True, limit=5
    )
    assert with_decay[0].file_path == "MEMORY.md"
    dated = next(
        result for result in with_decay if result.file_path == "memory/20200101.md"
    )
    assert dated.decay < 1.0
    assert dated.access_count == 0
    generic_directory = next(
        result for result in with_decay if result.file_path == "knowledge/20200101.md"
    )
    assert generic_directory.decay < 1.0
    reinforced = index.search(
        "retained", mode="keyword", path_weights={}, forgetting=True, limit=5
    )
    assert (
        next(
            result for result in reinforced if result.file_path == "memory/20200101.md"
        ).access_count
        == 1
    )
    index.close()


def test_legacy_fact_snapshot_is_imported_with_existing_ids(tmp_path: Path):
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    (knowledge / "rag_facts.md").write_text(
        """# workspace-RAG facts snapshot (test)

## fact #42

- created: 2026-01-01T00:00:00
- updated: 2026-02-01T00:00:00
- fact_date: 2026-01-15
- source_file: notes/example.md

移行対象の事実
""",
        encoding="utf-8",
    )

    index = SearchIndex(tmp_path, tmp_path / "index.sqlite3", FakeEmbedder())

    fact = index.list_facts()[0]
    assert fact["id"] == 42
    assert fact["text"] == "移行対象の事実"
    assert fact["source_file"] == "notes/example.md"
    assert index.find_similar_facts("検索", limit=3)[0]["id"] == 42
    index.close()


def test_custom_fact_snapshot_is_imported_and_excluded_from_index(tmp_path: Path):
    snapshot = tmp_path / "exports" / "facts.md"
    snapshot.parent.mkdir()
    snapshot.write_text(
        """<!-- Generated by xangi-search. Do not edit directly. -->

# xangi-search facts snapshot

## fact #7

- created: 2026-08-16T00:00:00+09:00
- updated: 2026-08-16T00:00:00+09:00

custom snapshot only phrase
""",
        encoding="utf-8",
    )
    index = SearchIndex(tmp_path, tmp_path / "index.sqlite3")
    index.configure_facts_snapshot("exports/facts.md", import_if_empty=True)
    index.reindex()

    assert index.list_facts()[0]["id"] == 7
    assert index.search("custom snapshot only", mode="keyword") == []
    index.close()


def test_unchanged_reindex_keeps_the_existing_vector_cache(tmp_path: Path, monkeypatch):
    (tmp_path / "search.md").write_text("workspace search", encoding="utf-8")
    index = SearchIndex(tmp_path, tmp_path / "index.sqlite3", FakeEmbedder())
    index.reindex()
    reloads = 0
    original = index._reload_vector_cache

    def counted_reload():
        nonlocal reloads
        reloads += 1
        original()

    monkeypatch.setattr(index, "_reload_vector_cache", counted_reload)
    assert index.reindex()["changed_files"] == 0
    assert reloads == 0
    index.close()


def test_vector_search_limits_candidates_before_loading_rows(
    tmp_path: Path, monkeypatch
):
    index = SearchIndex(tmp_path, tmp_path / "index.sqlite3")
    index.connection.executemany(
        "INSERT INTO chunks(id, file_path, chunk_index, content) VALUES (?, ?, 0, ?)",
        [(chunk_id, f"file-{chunk_id}.md", "content") for chunk_id in range(1, 1201)],
    )
    index.connection.commit()
    monkeypatch.setattr(
        index,
        "_vector_scores",
        lambda query, query_vector=None: {
            chunk_id: chunk_id / 1200 for chunk_id in range(1, 1201)
        },
    )

    results = index.search("query", mode="vector", limit=5)

    assert len(results) == 5
    assert results[0].file_path == "file-1200.md"
    index.close()
