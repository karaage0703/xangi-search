"""Failure and boundary cases for persistence and search contracts."""

import io
import json
import sqlite3
import subprocess
import sys
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from xangi_search.index import SearchIndex
from xangi_search.preflight import preflight_candidate_paths
from xangi_search.preflight_hook import run_hook
from xangi_search.server import (
    SearchHandler,
    SearchServer,
    format_agent_results,
    grep_search,
    python_grep_search,
)
from xangi_search.settings import SettingsStore, default_settings_path


class Encoder:
    embedding_signature = "test-v1"

    def __call__(self, texts, *, query=False):
        return np.array(
            [[1.0, 0.0] if "apple" in text else [0.0, 1.0] for text in texts],
            dtype=np.float32,
        )


@pytest.fixture
def index(tmp_path):
    value = SearchIndex(tmp_path, embedder=Encoder(), query_encode_wait_seconds=0)
    yield value
    value.close()


def server_for(index):
    server = object.__new__(SearchServer)
    server.workspace = index.workspace
    server.index = index
    server.settings = SettingsStore(default_settings_path(index.workspace))
    server.state_lock = threading.Lock()
    server.cached_stats = index.stats()
    server.read_only = False
    server.auth_token = None
    server.settings_changed = threading.Event()
    server.state_payload = lambda: {
        "stats": dict(server.cached_stats),
        "usable_snapshot": True,
    }
    return server


def request(server, method, path, body=None, length=None):
    handler = object.__new__(SearchHandler)
    handler.server, handler.path = server, path
    data = json.dumps(body or {}).encode()
    handler.headers = {"Content-Length": str(len(data) if length is None else length)}
    handler.rfile = io.BytesIO(data)
    responses = []
    handler._json = lambda *args: responses.append(args)
    getattr(handler, "do_" + method)()
    assert len(responses) == 1
    return responses[0]


def test_failed_fact_batch_does_not_leak_into_next_commit(index):
    with pytest.raises((sqlite3.ProgrammingError, ValueError)):
        index.add_facts([{"text": "first"}, {"text": "invalid", "source_file": {}}])
    assert not index.connection.in_transaction
    index.add_facts([{"text": "later"}])
    assert [fact["text"] for fact in index.list_facts()] == ["later"]


def test_failed_reindex_does_not_commit_deletions_later(index, tmp_path):
    path = tmp_path / "old.md"
    path.write_text("apple")
    index.reindex()
    path.unlink()
    (tmp_path / "new.md").write_text("banana")
    original = index.embedder

    def fail(*args, **kwargs):
        raise RuntimeError("encoding failed")

    index.embedder = fail
    with pytest.raises(RuntimeError):
        index.reindex()
    assert not index.connection.in_transaction
    index.embedder = original
    index.add_facts([{"text": "later"}])
    assert [r.file_path for r in index.search("apple", mode="keyword")] == ["old.md"]


def test_fact_edit_without_encoder_invalidates_old_vector(index):
    fact_id = index.add_facts([{"text": "apple"}])[0]["id"]
    index.embedder = None
    index.update_fact(fact_id, {"text": "banana"})
    assert index.connection.execute("SELECT embedding FROM facts").fetchone()[0] is None


def test_reactivate_fact_fills_missing_embedding(index):
    fact_id = index.add_facts([{"text": "apple"}])[0]["id"]
    index.delete_fact(fact_id)
    index.connection.execute("UPDATE facts SET embedding=NULL")
    index.connection.commit()
    index.update_fact(fact_id, {"text": "apple"})
    assert index.find_similar_facts("apple")[0]["score"] == 1


def test_restore_uses_configured_snapshot_before_old_default(tmp_path):
    index = SearchIndex(tmp_path)
    fact_id = index.add_facts([{"text": "obsolete"}])[0]["id"]
    custom = ".xangi-search/custom.md"
    index.configure_facts_snapshot(custom)
    SettingsStore(default_settings_path(tmp_path)).update(
        {"facts_snapshot_path": custom}
    )
    index.delete_fact(fact_id)
    index.add_facts([{"text": "current"}])
    db = index.db_path
    index.close()
    db.unlink()
    restored = SearchIndex(tmp_path)
    try:
        assert [f["text"] for f in restored.list_facts()] == ["current"]
        assert "current" in (tmp_path / custom).read_text()
    finally:
        restored.close()


@pytest.mark.parametrize(
    "text",
    [
        "- category: important\nkeep body",
        "body\n## fact #2\nother",
        "```\n## fact #1\n```",
        "a\u2028b\u0085c\n- record_json: forged",
    ],
)
def test_snapshot_round_trip_preserves_arbitrary_body(tmp_path, text):
    index = SearchIndex(tmp_path)
    index.add_facts(
        [{"text": text, "source_file": "file\nname.md"}, {"text": "second"}]
    )
    db = index.db_path
    index.close()
    db.unlink()
    restored = SearchIndex(tmp_path)
    try:
        fact = restored.get_fact(1)
        assert fact["text"] == text
        assert fact["source_file"] == "file\nname.md"
        assert restored.get_fact(2)["text"] == "second"
    finally:
        restored.close()


@pytest.mark.parametrize("operation", ["add", "update", "delete"])
def test_snapshot_failure_rolls_back_fact_and_cache(index, monkeypatch, operation):
    fact_id = index.add_facts([{"text": "apple"}])[0]["id"]
    before = index.list_facts()
    snapshot = index.workspace / index.facts_snapshot_path
    original = snapshot.read_bytes()

    def fail():
        raise OSError("disk full")

    monkeypatch.setattr(index, "_export_facts_snapshot", fail)
    with pytest.raises(OSError):
        if operation == "add":
            index.add_facts([{"text": "banana"}])
        elif operation == "update":
            index.update_fact(fact_id, {"text": "banana"})
        else:
            index.delete_fact(fact_id)
    assert not index.connection.in_transaction
    assert index.list_facts() == before
    assert snapshot.read_bytes() == original
    assert index.search_facts(np.array([1.0, 0.0]))[0]["text"] == "apple"


def test_failed_settings_export_leaves_persisted_and_effective_settings_unchanged(
    index,
):
    server = server_for(index)
    (index.workspace / ".xangi-search/blocked").write_text("not a directory")
    before = server.settings.payload()
    status, _ = request(
        server,
        "PUT",
        "/settings",
        {
            "facts_snapshot_path": ".xangi-search/blocked/facts.md",
            "exclude_paths": ["private.md"],
        },
    )
    assert status == 500
    assert server.settings.payload() == before
    assert index.facts_snapshot_path == before["facts_snapshot_path"]
    assert list(index.exclude_paths) == before["exclude_paths"]


def test_keyword_scores_and_content_use_one_read_snapshot(index, tmp_path, monkeypatch):
    path = tmp_path / "old.md"
    path.write_text("apple uniqueneedle")
    index.reindex()
    original = index._keyword_scores

    def interleave(*args, **kwargs):
        scores = original(*args, **kwargs)
        path.unlink()
        (tmp_path / "new.md").write_text("banana unrelated replacement")
        index.reindex()
        return scores

    monkeypatch.setattr(index, "_keyword_scores", interleave)
    results = index.search("uniqueneedle", mode="keyword")
    assert [(r.file_path, r.content) for r in results] == [
        ("old.md", "apple uniqueneedle")
    ]


def test_busy_hybrid_uses_keyword_retry(index, tmp_path):
    (tmp_path / "open-data.md").write_text(
        "愛知県ではGIS形式の公共施設情報をオープンデータとして公開しています。"
    )
    index.reindex()
    query = "愛知県と県内市町村のGISや公共施設情報をまとめて探せるオープンデータ調査"
    server = server_for(index)
    with index._vector_search_lock:
        payload = server.search_payload(query, mode="hybrid")
    assert payload["count"] == 1
    assert payload["degraded_reason"] == "query_encoder_busy"


def test_grep_hits_reach_agent_and_preflight(index, tmp_path):
    (tmp_path / "evidence.custom").write_text("uniqueevidence")
    payload = server_for(index).search_payload("uniqueevidence", mode="keyword")
    assert "evidence.custom" in format_agent_results("uniqueevidence", payload)
    assert preflight_candidate_paths(payload) == ["evidence.custom"]
    output = run_hook(
        {"prompt": "uniqueevidence"},
        runner=lambda *a, **k: SimpleNamespace(stdout=json.dumps(payload)),
    )
    assert "uniqueevidence" in output["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize("query", ["v0.2.3", "[bracket"])
def test_grep_is_literal_with_and_without_rg(tmp_path, query):
    (tmp_path / "literal.custom").write_text("v0.2.3 [bracket")
    (tmp_path / "noise.custom").write_text("v0x2x3")
    assert [r["file_path"] for r in grep_search(query, tmp_path)] == ["literal.custom"]
    assert [r["file_path"] for r in python_grep_search(query, tmp_path)] == [
        "literal.custom"
    ]


def test_literal_values_next_to_punctuation(index, tmp_path):
    for name, text in {
        "period.md": "Total is 30,958.",
        "colon.md": "Total:30958 people",
        "comma.md": "30,958, according to data",
        "noise.md": "3095891982",
        "decimal.md": "30958.25",
        "time.md": "12:30:59",
    }.items():
        (tmp_path / name).write_text(text)
    index.reindex()
    assert {r.file_path for r in index.search("30958", mode="keyword")} == {
        "period.md",
        "colon.md",
        "comma.md",
    }
    assert index.search("12:30", mode="keyword") == []


def test_literal_filter_runs_before_candidate_limit(index, tmp_path):
    for n in range(220):
        (tmp_path / f"noise{n}.md").write_text("1309580 " * 20)
    (tmp_path / "target.md").write_text("値30958人です")
    index.reindex()
    assert [r.file_path for r in index.search("30958", mode="keyword", limit=1)] == [
        "target.md"
    ]


def test_vector_mode_without_encoder_is_explicit_error(tmp_path):
    index = SearchIndex(tmp_path)
    try:
        with pytest.raises(ValueError, match="vector"):
            index.search("apple", mode="vector")
        assert (
            request(server_for(index), "GET", "/search?q=apple&mode=vector")[0] == 400
        )
    finally:
        index.close()


def test_hook_limits_serialized_json_including_escaping():
    payload = {
        "results": [{"file_path": f"{n}.md", "content": '\\"' * 4000} for n in range(4)]
    }
    output = run_hook(
        {"prompt": "query"},
        runner=lambda *a, **k: SimpleNamespace(stdout=json.dumps(payload)),
    )
    assert len(json.dumps(output, ensure_ascii=False, separators=(",", ":"))) <= 14000
    context = output["hookSpecificOutput"]["additionalContext"]
    assert context.endswith("</xangi_search_preflight>")


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/facts/similar?q=apple&k=oops"),
        *[
            (method, "/facts/9223372036854775808")
            for method in ["GET", "PUT", "DELETE"]
        ],
    ],
)
def test_invalid_fact_numbers_return_json_400(index, method, path):
    assert request(server_for(index), method, path)[0] == 400


def test_negative_content_length_is_rejected_before_read(index):
    assert (
        request(
            server_for(index), "POST", "/facts", {"facts": [{"text": "x" * 71680}]}, -1
        )[0]
        == 400
    )
    assert index.list_facts() == []


@pytest.mark.parametrize(
    "tail",
    [
        ["search", "apple", "--limit", "0"],
        ["search", "apple", "--limit", "-1"],
        ["evaluate", "--queries", "missing.jsonl", "--k", "0"],
    ],
)
def test_cli_rejects_nonpositive_counts_before_loading_model(tail):
    result = subprocess.run(
        [sys.executable, "-m", "xangi_search.cli", "--no-vector", *tail],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "positive" in result.stderr


def test_empty_current_snapshot_does_not_resurrect_old_facts(tmp_path):
    index = SearchIndex(tmp_path)
    fact_id = index.add_facts([{"text": "obsolete"}])[0]["id"]
    custom = ".xangi-search/current.md"
    index.configure_facts_snapshot(custom)
    SettingsStore(default_settings_path(tmp_path)).update(
        {"facts_snapshot_path": custom}
    )
    index.delete_fact(fact_id)
    db = index.db_path
    index.close()
    db.unlink()
    restored = SearchIndex(tmp_path)
    try:
        restored.configure_facts_snapshot(
            custom, import_if_empty=True, legacy_path=".xangi-search/facts.md"
        )
        assert restored.list_facts() == []
    finally:
        restored.close()


def test_settings_write_failure_keeps_old_snapshot_path(index):
    server = server_for(index)
    original = index.facts_snapshot_path
    server.settings.path.mkdir()
    status, _ = request(
        server,
        "PUT",
        "/settings",
        {
            "facts_snapshot_path": ".xangi-search/new.md",
        },
    )
    assert status == 500
    assert index.facts_snapshot_path == original
    assert server.settings.get().facts_snapshot_path == original


def test_fact_commit_failure_restores_snapshot_and_cache(index):
    fact_id = index.add_facts([{"text": "apple"}])[0]["id"]
    target = index.workspace / index.facts_snapshot_path
    before = target.read_bytes()
    connection = index.connection

    class FailingCommit:
        def __getattr__(self, name):
            return getattr(connection, name)

        def commit(self):
            raise sqlite3.OperationalError("commit failed")

    index.connection = FailingCommit()
    try:
        with pytest.raises(sqlite3.OperationalError):
            index.update_fact(fact_id, {"text": "banana"})
    finally:
        index.connection = connection
    assert target.read_bytes() == before
    assert index.get_fact(fact_id)["text"] == "apple"
    assert index.search_facts(np.array([1.0, 0.0]))[0]["text"] == "apple"


def test_vector_search_sees_consistent_generation_during_reindex(
    index, tmp_path, monkeypatch
):
    old = tmp_path / "old.md"
    old.write_text("apple")
    index.reindex()
    scored, pending, release = threading.Event(), threading.Event(), threading.Event()
    original_scores = index._vector_scores
    original_pending = index._pending_vector_cache
    observed, errors = [], []

    def pause_scores(*args, **kwargs):
        scores = original_scores(*args, **kwargs)
        scored.set()
        assert release.wait(5)
        return scores

    def signal_pending(*args, **kwargs):
        value = original_pending(*args, **kwargs)
        pending.set()
        return value

    def read():
        try:
            observed.extend(index.search("apple", mode="vector", min_score=0.5))
        except BaseException as error:
            errors.append(error)

    def write():
        try:
            index.reindex()
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(index, "_vector_scores", pause_scores)
    monkeypatch.setattr(index, "_pending_vector_cache", signal_pending)
    reader = threading.Thread(target=read)
    writer = threading.Thread(target=write)
    reader.start()
    try:
        assert scored.wait(5)
        old.unlink()
        (tmp_path / "new.md").write_text("banana")
        writer.start()
        assert pending.wait(5)
    finally:
        release.set()
        reader.join(5)
        if writer.ident is not None:
            writer.join(5)
    assert not reader.is_alive() and not writer.is_alive()
    assert not errors
    assert [(r.file_path, r.content) for r in observed] == [("old.md", "apple")]
    assert index.search("apple", mode="vector", min_score=0.5) == []


@pytest.mark.parametrize(
    "text,matched",
    [
        ("version:v0.2.3.", True),
        ("v0.2.3, according to release notes", True),
        ("prefix.v0.2.3", False),
        ("v0.2.3.alpha", False),
        ("v0.2.3.4", False),
    ],
)
def test_identifier_punctuation_does_not_split_longer_identifiers(text, matched):
    assert SearchIndex._matches_literal(text, ("v0.2.3",)) is matched
