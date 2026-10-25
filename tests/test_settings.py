import json
from pathlib import Path

import pytest

from xangi_search.settings import SettingsStore


def test_settings_store_reloads_saved_values(tmp_path: Path):
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    store.update(
        {
            "mode": "keyword",
            "limit": 4,
            "reindex_interval_seconds": 900,
            "facts_snapshot_path": ".xangi-search/system/facts.md",
        }
    )

    reloaded = SettingsStore(path).payload()
    assert reloaded["mode"] == "keyword"
    assert reloaded["limit"] == 4
    assert reloaded["reindex_interval_seconds"] == 900
    assert reloaded["path_weights"] == {}
    assert reloaded["facts_snapshot_path"] == ".xangi-search/system/facts.md"
    assert json.loads(path.read_text())["schema_version"] == 1


def test_settings_store_rejects_unknown_and_invalid_values(tmp_path: Path):
    store = SettingsStore(tmp_path / "settings.json")
    with pytest.raises(ValueError, match="unknown settings"):
        store.update({"secret": "no"})
    with pytest.raises(ValueError, match="between 1 and 30"):
        store.update({"limit": 31})


def test_settings_store_normalizes_and_persists_path_weights(tmp_path: Path):
    path = tmp_path / "settings.json"
    store = SettingsStore(path)

    updated = store.update(
        {
            "path_weights": {"notes": 2, "notes/private/": 3.5},
            "default_path_weight": 0.7,
            "forgetting": True,
        }
    )

    assert updated.path_weights == {"notes/": 2.0, "notes/private/": 3.5}
    assert SettingsStore(path).get().default_path_weight == 0.7
    assert SettingsStore(path).get().forgetting is True


def test_settings_store_uses_workspace_specific_initial_weights(tmp_path: Path):
    store = SettingsStore(
        tmp_path / "settings.json",
        default_path_weights={"notes/": 1, "repositories/": 1},
    )

    assert store.payload()["path_weights"] == {
        "notes/": 1.0,
        "repositories/": 1.0,
    }


@pytest.mark.parametrize(
    "path_weights",
    [
        ["notes/"],
        {"/absolute/": 1},
        {"../outside/": 1},
        {"notes/": -1},
        {"notes/": float("inf")},
    ],
)
def test_settings_store_rejects_invalid_path_weights(tmp_path: Path, path_weights):
    store = SettingsStore(tmp_path / "settings.json")
    with pytest.raises(ValueError):
        store.update({"path_weights": path_weights})


@pytest.mark.parametrize(
    "snapshot_path",
    [
        "",
        "/tmp/facts.md",
        "../facts.md",
        "notes/facts.txt",
        ".git/facts.md",
        "notes/facts.md",
        "C:/facts.md",
    ],
)
def test_settings_store_rejects_unsafe_facts_snapshot_paths(
    tmp_path: Path, snapshot_path: str
):
    store = SettingsStore(tmp_path / "settings.json")
    with pytest.raises(ValueError):
        store.update({"facts_snapshot_path": snapshot_path})


def test_settings_store_normalizes_facts_snapshot_path(tmp_path: Path):
    store = SettingsStore(tmp_path / "settings.json")
    updated = store.update({"facts_snapshot_path": ".xangi-search\\system\\facts.md"})
    assert updated.facts_snapshot_path == ".xangi-search/system/facts.md"


@pytest.mark.parametrize(
    "contents",
    [
        '{"exclude_paths": ["logs"], "auto_reindex": false, "limit": 0}',
        '{"exclude_paths": ["logs"],',
        '{"exclude_paths": "logs"}',
        '{"exclude_paths": ["logs"], "mode": []}',
        "[]",
    ],
)
def test_invalid_saved_settings_fail_closed(tmp_path, contents):
    from xangi_search.settings import SettingsLoadError

    path = tmp_path / "settings.json"
    path.write_text(contents)
    with pytest.raises(SettingsLoadError, match="settings.json"):
        SettingsStore(path)
    assert path.read_text() == contents


def test_unreadable_settings_fail_closed(tmp_path, monkeypatch):
    from xangi_search.settings import SettingsLoadError

    def denied(*args, **kwargs):
        raise PermissionError("access denied")

    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(SettingsLoadError, match="access denied"):
        SettingsStore(tmp_path / "settings.json")


def test_invalid_update_preserves_exclusions_and_saved_file(tmp_path):
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    store.update({"exclude_paths": ["logs"], "auto_reindex": False})
    before = path.read_bytes()
    with pytest.raises(ValueError):
        store.update({"limit": 0})
    assert path.read_bytes() == before
    assert store.get().exclude_paths == ["logs"]
    assert store.get().auto_reindex is False
