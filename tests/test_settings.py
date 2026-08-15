import json
from pathlib import Path

import pytest

from xangi_search.settings import SettingsStore


def test_settings_store_reloads_saved_values(tmp_path: Path):
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    store.update({"mode": "keyword", "limit": 4, "reindex_interval_seconds": 900})

    reloaded = SettingsStore(path).payload()
    assert reloaded["mode"] == "keyword"
    assert reloaded["limit"] == 4
    assert reloaded["reindex_interval_seconds"] == 900
    assert reloaded["path_weights"] == {}
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
