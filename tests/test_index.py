import threading
import time
from pathlib import Path

import numpy as np
import pytest

from xangi_search.index import SearchIndex, chunk_text, is_excluded_directory


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
    assert chunk_text("abcdefghij", size=6, overlap=2) == ["abcdef", "efghij"]


def test_chunk_text_skips_redundant_short_tail():
    text = "a" * 896 + "tail"

    chunks = chunk_text(text)

    assert [len(chunk) for chunk in chunks] == [512, 452]
    assert chunks[-1].endswith("tail")


def test_directory_exclusions_are_pattern_driven():
    assert is_excluded_directory(".venv")
    assert is_excluded_directory(".venv_arxiv")
    assert is_excluded_directory("node_modules")
    assert is_excluded_directory("tmp", at_workspace_root=True)
    assert not is_excluded_directory("tmp")
    assert not is_excluded_directory("logs")


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


def test_hybrid_search_preserves_exact_keyword_match_outside_vector_candidates(
    tmp_path: Path, monkeypatch
):
    index = SearchIndex(tmp_path, tmp_path / "index.sqlite3")
    index.connection.executemany(
        "INSERT INTO chunks(id, file_path, chunk_index, content) VALUES (?, ?, 0, ?)",
        [(1, "exact.md", "denkitribe exact match")]
        + [
            (chunk_id, f"noise-{chunk_id}.md", "tiny noise")
            for chunk_id in range(2, 42)
        ],
    )
    index.connection.commit()
    monkeypatch.setattr(
        index,
        "_keyword_scores",
        lambda *_args, **_kwargs: {1: 1.0},
    )
    monkeypatch.setattr(
        index,
        "_vector_scores",
        lambda *_args, **_kwargs: (
            {1: 0.84} | {chunk_id: 0.88 for chunk_id in range(2, 42)}
        ),
    )

    results = index.search("denkitribe", mode="hybrid", limit=8)

    assert results[0].file_path == "exact.md"
    assert results[0].score == 1.0
    assert results[0].keyword_score == 1.0
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
    virtual_environment = tmp_path / ".venv_arxiv"
    virtual_environment.mkdir()
    (virtual_environment / "package.py").write_text(
        "third party package marker", encoding="utf-8"
    )
    index = SearchIndex(tmp_path, tmp_path / "index.sqlite3")
    index.reindex()
    assert [
        result.file_path for result in index.search("ignored", mode="keyword")
    ] == []
    assert index.search("third party package marker", mode="keyword") == []
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
    assert paths == {".dockerignore", "schema.graphql", "script.bash"}
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


def test_search_payload_can_add_bounded_context_without_changing_rank(
    tmp_path: Path, monkeypatch
):
    index = SearchIndex(tmp_path, tmp_path / "index.sqlite3")
    index.connection.executemany(
        "INSERT INTO chunks(file_path, chunk_index, content) VALUES (?, ?, ?)",
        [
            ("notes.md", 0, "before context"),
            ("notes.md", 1, "target matching chunk"),
            ("notes.md", 2, "after context"),
            ("other.md", 0, "target secondary file"),
        ],
    )
    index.connection.commit()

    baseline = index.search_payload("target", mode="keyword", limit=2)
    bundled = index.search_payload(
        "target", mode="keyword", limit=2, context_chunks=1, context_results=1
    )

    assert "context_chunks" not in baseline
    without_context = [
        {key: value for key, value in item.items() if key != "context"}
        for item in bundled["results"]
    ]
    assert without_context == baseline["results"]
    assert bundled["context_chunks"] == 1
    assert bundled["context_results"] == 1
    assert [
        chunk["chunk_index"] for chunk in bundled["results"][0]["context"]["chunks"]
    ] == [0, 1, 2]
    assert "context" not in bundled["results"][1]
    with pytest.raises(ValueError, match="context_chunks"):
        index.search_payload("target", mode="keyword", context_chunks=3)
    with pytest.raises(ValueError, match="context_results"):
        index.search_payload(
            "target", mode="keyword", context_chunks=1, context_results=5
        )
    monkeypatch.setattr(
        index,
        "context_windows",
        lambda *_args: {
            "notes.md": {
                "start_chunk_index": 1,
                "end_chunk_index": 1,
                "chunks": [{"chunk_index": 1, "content": "changed concurrently"}],
            }
        },
    )
    inconsistent = index.search_payload(
        "target", mode="keyword", context_chunks=1, context_results=1
    )
    assert "context" not in inconsistent["results"][0]
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
    assert "猫のウミについて" in (tmp_path / ".xangi-search" / "facts.md").read_text()

    updated = index.update_fact(fact_id, {"text": "猫のソラについて"})
    assert updated["text"] == "猫のソラについて"
    deleted = index.delete_fact(fact_id)
    assert deleted["is_active"] == 0
    assert index.stats()["facts"] == 0
    index.close()


def test_forgetting_uses_mtime_without_reinforcing_search_hits(
    tmp_path: Path, monkeypatch
):
    import os
    import time

    now = time.time()
    monkeypatch.setattr("xangi_search.index.time.time", lambda: now)
    for name, age in [
        ("current.md", 0),
        ("20200101.md", 0),
        ("old.md", 365),
        ("week.md", 7),
        ("future.md", -30),
    ]:
        path = tmp_path / name
        path.write_text("same retained phrase", encoding="utf-8")
        os.utime(path, (now - age * 86400, now - age * 86400))
    index = SearchIndex(tmp_path, tmp_path / "index.sqlite3")
    index.reindex()
    assert index._freshness("missing.md") == 1.0
    index.connection.execute(
        "UPDATE chunks SET access_count=99, last_accessed='2099-01-01' WHERE file_path='old.md'"
    )
    index.connection.commit()
    before = list(
        index.connection.execute("SELECT id, access_count, last_accessed FROM chunks")
    )
    off = index.search("retained", mode="keyword", forgetting=False, limit=8)
    assert all(r.score == r.base_score for r in off)
    first = index.search("retained", mode="keyword", forgetting=True, limit=8)
    rows = {r.file_path: r for r in first}
    for name in ["current.md", "20200101.md", "future.md"]:
        assert rows[name].score == rows[name].base_score
    assert abs(rows["week.md"].freshness - (1 - 7 / 30)) < 1e-6
    assert abs(rows["old.md"].score - rows["old.md"].base_score * 0.5) < 1e-6
    second = index.search("retained", mode="keyword", forgetting=True, limit=8)
    assert first == second
    assert before == list(
        index.connection.execute("SELECT id, access_count, last_accessed FROM chunks")
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
    snapshot = tmp_path / ".xangi-search" / "exports" / "facts.md"
    snapshot.parent.mkdir(parents=True)
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
    index.configure_facts_snapshot(
        ".xangi-search/exports/facts.md", import_if_empty=True
    )
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
    index = SearchIndex(tmp_path, tmp_path / "index.sqlite3", FakeEmbedder())
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


@pytest.mark.parametrize(
    "relative",
    [
        "notes/a.md",
        ".xangi-search/personal.md",
        ".xangi-search/link.md",
        ".xangi-search/sub/a.md",
    ],
)
def test_snapshot_refuses_user_files_and_symlinks(tmp_path, relative):
    index = SearchIndex(tmp_path)
    victim = tmp_path / "notes" / "a.md"
    victim.parent.mkdir()
    victim.write_text("private notes")
    (tmp_path / ".xangi-search" / "personal.md").write_text("private notes")
    (tmp_path / ".xangi-search" / "link.md").symlink_to(victim)
    (tmp_path / ".xangi-search" / "sub").symlink_to(
        victim.parent, target_is_directory=True
    )
    with pytest.raises(ValueError):
        index.configure_facts_snapshot(relative)
    assert victim.read_text() == "private notes"
    assert (tmp_path / ".xangi-search" / "personal.md").read_text() == "private notes"
    assert index.facts_snapshot_path == ".xangi-search/facts.md"
    index.close()


def test_snapshot_revalidates_path_before_fact_mutation(tmp_path):
    index = SearchIndex(tmp_path)
    fact_id = index.add_facts([{"text": "original"}])[0]["id"]
    target = tmp_path / ".xangi-search" / "facts.md"
    target.write_text("user replaced snapshot")
    for mutate in [
        lambda: index.add_facts([{"text": "new"}]),
        lambda: index.update_fact(fact_id, {"text": "changed"}),
        lambda: index.delete_fact(fact_id),
    ]:
        with pytest.raises(ValueError):
            mutate()
    assert target.read_text() == "user replaced snapshot"
    assert len(index.list_facts()) == 1
    assert index.get_fact(fact_id)["text"] == "original"
    assert index.get_fact(fact_id)["is_active"] == 1
    index.close()


@pytest.mark.parametrize("query", ["30,958", "30958"])
@pytest.mark.parametrize("mode", ["keyword", "hybrid"])
def test_numeric_search_matches_whole_number_with_optional_grouping(
    tmp_path, query, mode
):
    for name, content in {
        "grouped.md": "フォロワー30,958人です",
        "plain.md": "フォロワー30958人です",
        "noise.md": "30 と958、ID3095891982、数値130958",
    }.items():
        (tmp_path / name).write_text(content)
    index = SearchIndex(tmp_path, tmp_path / "index.sqlite3")
    index.reindex()
    rows = index.search(query, mode=mode, limit=8)
    assert {r.file_path for r in rows} == {"grouped.md", "plain.md"}
    index.close()


@pytest.mark.parametrize("query", ["00:48:48", "v0.2.3", "62a12595"])
def test_structured_search_does_not_split_identifiers(tmp_path, query):
    (tmp_path / "exact.md").write_text(f"対象 {query} です")
    (tmp_path / "noise.md").write_text(f"ID x{query}9 と 00 48 0.2.3")
    index = SearchIndex(tmp_path, tmp_path / "index.sqlite3")
    index.reindex()
    assert [r.file_path for r in index.search(query, mode="keyword")] == ["exact.md"]
    index.close()


def test_numeric_query_keeps_leading_zeroes():
    assert "1,234" not in SearchIndex._literal_query("001234")
    assert "001,234" in SearchIndex._literal_query("001234")
