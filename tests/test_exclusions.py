import threading

import pytest
from test_index import FakeEmbedder
from test_server import request

from xangi_search.index import SearchIndex
from xangi_search.server import SearchServer, grep_search, python_grep_search
from xangi_search.settings import SettingsStore, default_settings_path


def write_files(root):
    paths = [
        "output/copy.md",
        ".cache/vendor.h",
        "logs/tool-trajectory/run.jsonl",
        "logs/sessions/chat.md",
        "output-other/keep.md",
        "notes/original.md",
    ]
    for path in paths:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("cat search needle", encoding="utf-8")
    return paths


def test_exclusions_prune_existing_vectors_and_restore_without_deleting_files(tmp_path):
    paths = write_files(tmp_path)
    index = SearchIndex(tmp_path, embedder=FakeEmbedder())
    index.reindex()
    assert index.stats()["files"] == len(paths)
    index.configure_exclude_paths(
        ["output", ".cache", "logs/tool-trajectory", "notes/original.md"]
    )
    # Existing snapshots must stop returning excluded files even before reindex.
    assert all(r.file_path in paths[3:5] for r in index.search("cat", limit=30))
    assert index.reindex()["removed_files"] == 0
    for mode in ("keyword", "vector", "hybrid"):
        assert {r.file_path for r in index.search("cat", mode=mode, limit=30)} == set(
            paths[3:5]
        )
    assert index.stats()["vectors"] == 2
    assert all((tmp_path / p).is_file() for p in paths)
    index.configure_exclude_paths([])
    assert index.reindex()["changed_files"] == 4
    assert index.stats()["files"] == len(paths)
    index.close()


@pytest.mark.parametrize(
    "invalid",
    [
        "output",
        [".."],
        ["../notes"],
        ["/etc"],
        ["."],
        [""],
        ["out*"],
        ["x?"],
        ["[x]"],
        ["C:/x"],
        [4],
        ["x\ny"],
        ["x\x00y"],
    ],
)
def test_invalid_exclusions_do_not_replace_settings(tmp_path, invalid):
    store = SettingsStore(default_settings_path(tmp_path))
    store.update({"exclude_paths": ["output/"]})
    with pytest.raises(ValueError):
        store.update({"exclude_paths": invalid})
    assert SettingsStore(store.path).get().exclude_paths == ["output"]


def test_settings_api_and_restart_preserve_exclusions(tmp_path):
    paths = write_files(tmp_path)
    index = SearchIndex(tmp_path)
    index.reindex()
    server = SearchServer(("127.0.0.1", 0), index)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        status, data = request(
            base + "/settings",
            "PUT",
            {
                "exclude_paths": [
                    "./output/",
                    "output",
                    ".cache",
                    "logs/tool-trajectory",
                ],
                "auto_reindex": False,
            },
        )
        assert status == 200
        assert data["exclude_paths"] == ["output", ".cache", "logs/tool-trajectory"]
        assert index.stats()["files"] == 3
        status, data = request(base + "/search?q=needle&mode=keyword&k=8")
        assert status == 200
        assert {r["file_path"] for r in data["results"]} == set(paths[3:])
        assert all(r["file_path"] in paths[3:] for r in data["grep_results"])
    finally:
        server.shutdown()
        server.server_close()
        index.close()
    restarted = SearchIndex(tmp_path)
    assert restarted.exclude_paths == ("output", ".cache", "logs/tool-trajectory")
    assert restarted.reindex()["changed_files"] == 0
    restarted.close()


@pytest.mark.parametrize("search", [grep_search, python_grep_search])
def test_grep_exclusions_keep_sibling_and_existing_log_policy(tmp_path, search):
    paths = write_files(tmp_path)
    SettingsStore(default_settings_path(tmp_path)).update(
        {"exclude_paths": ["output", ".cache", "logs/tool-trajectory"]}
    )
    assert {r["file_path"] for r in search("needle", tmp_path)} == set(paths[4:])


@pytest.mark.parametrize("search", [grep_search, python_grep_search])
def test_environment_files_never_reach_grep(tmp_path, search):
    for name in [
        ".env",
        ".env.example",
        ".env.local",
        "prod.env",
        "nested/PROD.ENV",
        "nested/.ENV.LOCAL",
        "keep.md",
    ]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("needle dummy only")
    assert [r["file_path"] for r in search("needle", tmp_path)] == ["keep.md"]


def test_existing_database_is_purged_before_search_without_reindex(
    tmp_path, monkeypatch
):

    for name in [
        ".env",
        ".env.example",
        "prod.env",
        "nested/.ENV.LOCAL",
        "logs/run.jsonl",
        "keep.md",
    ]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("cat needle")
    index = SearchIndex(tmp_path, embedder=FakeEmbedder())
    # Seed a legacy snapshot, including names newer versions must reject.
    with monkeypatch.context() as patch:
        patch.setattr(
            index,
            "_iter_files",
            lambda: (
                p
                for p in tmp_path.rglob("*")
                if p.is_file() and ".xangi-search" not in p.parts
            ),
        )
        index.reindex()
    assert index.stats()["files"] == 6
    index.close()
    SettingsStore(default_settings_path(tmp_path)).update(
        {"exclude_paths": ["logs"], "auto_reindex": False}
    )
    reopened = SearchIndex(tmp_path, embedder=FakeEmbedder())
    assert reopened.stats()["files"] == 1
    assert reopened.stats()["vectors"] == 1
    assert (
        reopened.connection.execute(
            "SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH ?", ("needle",)
        ).fetchone()[0]
        == 1
    )
    for mode in ["keyword", "vector", "hybrid"]:
        assert [r.file_path for r in reopened.search("cat", mode=mode)] == ["keep.md"]
    assert reopened.reindex()["changed_files"] == 0
    reopened.close()


def test_excluded_logs_cannot_exhaust_vector_candidates(tmp_path):
    import numpy as np

    class RankedEmbedder:
        def __call__(self, texts, *, query=False):
            return np.array(
                [[1.0, 0.0] if query or "log" in t else [0.8, 0.6] for t in texts],
                dtype=np.float32,
            )

    for folder, count in [("logs", 60), ("notes", 10)]:
        (tmp_path / folder).mkdir()
        for n in range(count):
            (tmp_path / folder / f"{n}.md").write_text(folder)
    index = SearchIndex(tmp_path, embedder=RankedEmbedder())
    index.reindex()
    assert not index.search(
        "query", mode="vector", limit=5, min_score=0.01, path_weights={"logs/": 0}
    )
    index.configure_exclude_paths(["logs"])
    results = index.search("query", mode="vector", limit=5, min_score=0.01)
    assert len(results) == 5
    assert all(r.file_path.startswith("notes/") for r in results)
    index.close()


def test_failed_reindex_does_not_restore_purged_content(tmp_path):
    write_files(tmp_path)
    index = SearchIndex(tmp_path, embedder=FakeEmbedder())
    index.reindex()
    index.configure_exclude_paths(["output"])
    (tmp_path / "notes/new.md").write_text("cat")

    def failing_embedder(*args, **kwargs):
        raise RuntimeError("embedding unavailable")

    index.embedder = failing_embedder
    with pytest.raises(RuntimeError, match="embedding unavailable"):
        index.reindex()
    assert (
        index.connection.execute(
            "SELECT COUNT(*) FROM chunks WHERE file_path = 'output/copy.md'"
        ).fetchone()[0]
        == 0
    )
    assert all(
        r.file_path != "output/copy.md" for r in index.search("cat", mode="keyword")
    )
    index.close()


@pytest.mark.parametrize("search", [grep_search, python_grep_search])
def test_invalid_settings_block_index_and_fallback_search(tmp_path, search):
    from xangi_search.settings import SettingsLoadError

    write_files(tmp_path)
    path = default_settings_path(tmp_path)
    path.parent.mkdir()
    path.write_text('{"exclude_paths": ["logs"], "limit": 0}')
    with pytest.raises(SettingsLoadError):
        SearchIndex(tmp_path)
    assert not list(path.parent.glob("*.sqlite3"))
    with pytest.raises(SettingsLoadError):
        search("needle", tmp_path)
