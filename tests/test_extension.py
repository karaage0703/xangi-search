import json
from pathlib import Path

from xangi_search import extension


def test_status_ignores_stale_pid(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XANGI_SEARCH_STATE_DIR", str(tmp_path))
    (tmp_path / "service.json").write_text(
        json.dumps({"pid": 999999999, "healthUrl": "http://127.0.0.1:1/health"}),
        encoding="utf-8",
    )
    assert extension.status() == {
        "schemaVersion": 1,
        "id": "xangi-search",
        "running": False,
        "healthy": False,
        "pid": None,
        "healthUrl": "http://127.0.0.1:1/health",
        "workspace": None,
        "workspaceMatches": True,
    }


def test_setup_contract_includes_localized_docs_and_usage_skill():
    root = Path(__file__).parents[1]
    manifest = json.loads((root / "xangi-extension.json").read_text(encoding="utf-8"))
    assert manifest["displayName"] == "xangi search"
    assert manifest["ui"] == {"capability": "workspace.search", "path": "/ui"}
    assert manifest["agentBackend"] == {
        "id": "workspace-search",
        "displayName": "xangi search",
        "capability": "workspace.search",
        "path": "/agent",
    }
    setup_path = root / manifest["setup"]["instructions"]
    skill_path = root / "skills" / "xs-xangi-search" / "SKILL.md"

    assert setup_path.name == "XANGI_SETUP.md"
    setup = setup_path.read_text(encoding="utf-8")
    assert "利用スキルを提案する" in setup
    assert "uv sync --extra vector" in setup
    assert "XANGI_SEARCH_NO_VECTOR=true" in setup
    assert (root / "XANGI_SETUP.en.md").is_file()
    assert skill_path.is_file()
    skill = skill_path.read_text(encoding="utf-8")
    assert skill.startswith("---\nname: xs-xangi-search\n")
    assert "AGENTS.md" in setup
