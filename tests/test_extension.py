import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest


def test_managed_entrypoint_uses_ephemeral_port_and_parent_lifecycle(tmp_path: Path):
    token = "test-managed-extension-token"
    env = {**os.environ, "XANGI_EXTENSION_AUTH_TOKEN": token}
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "xangi_search.extension",
            "serve",
            "--workspace",
            str(tmp_path),
            "--no-vector",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert process.stdout is not None
        ready = json.loads(process.stdout.readline())
        assert ready["schemaVersion"] == 2
        assert ready["event"] == "ready"
        assert ready["workspace"] == str(tmp_path.resolve())
        assert ready["baseUrl"].startswith("http://127.0.0.1:")
        assert ready["baseUrl"] != "http://127.0.0.1:7891"

        with pytest.raises(HTTPError) as unauthorized:
            urlopen(f"{ready['baseUrl']}/health", timeout=2)
        assert unauthorized.value.code == 401

        request = Request(
            f"{ready['baseUrl']}/health",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urlopen(request, timeout=2) as response:
            health = json.load(response)
        assert health["service"] == "xangi-search"

        assert process.stdin is not None
        process.stdin.close()
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def test_managed_entrypoint_emits_ready_before_index_construction(tmp_path: Path):
    token = "test-managed-extension-token"
    env = {**os.environ, "XANGI_EXTENSION_AUTH_TOKEN": token}
    script = """
import sys
import time
from pathlib import Path
from xangi_search import extension

real_search_index = extension.SearchIndex

def slow_search_index(*args, **kwargs):
    time.sleep(2)
    return real_search_index(*args, **kwargs)

extension.SearchIndex = slow_search_index
extension.serve_managed(Path(sys.argv[1]), True)
"""
    started = time.monotonic()
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert process.stdout is not None
        ready = json.loads(process.stdout.readline())
        assert time.monotonic() - started < 1.5
        assert ready["event"] == "ready"

        assert process.stdin is not None
        process.stdin.close()
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def test_setup_contract_includes_managed_runtime_and_usage_skill():
    root = Path(__file__).parents[1]
    manifest = json.loads((root / "xangi-extension.json").read_text(encoding="utf-8"))
    assert manifest["schemaVersion"] == 2
    assert manifest["runtime"] == {"kind": "managed-http"}
    assert manifest["update"] == {
        "prepare": {
            "command": "./scripts/prepare-update",
            "args": [],
        }
    }
    assert manifest["displayName"] == "xangi-search"
    assert manifest["ui"] == {"capability": "workspace.search", "path": "/ui"}
    assert manifest["agentBackend"] == {
        "id": "workspace-search",
        "displayName": "xangi-search",
        "capability": "workspace.search",
        "path": "/agent",
    }
    assert manifest["capabilities"] == [
        {
            "id": "workspace.search",
            "protocol": "http",
            "healthPath": "/health",
        }
    ]
    setup_path = root / manifest["setup"]["instructions"]
    skill_path = root / "skills" / "xs-xangi-search" / "SKILL.md"
    readme = (root / "README.md").read_text(encoding="utf-8")
    readme_en = (root / "README.en.md").read_text(encoding="utf-8")

    assert setup_path.name == "XANGI_SETUP.md"
    assert "[English](README.en.md)" in readme
    assert "[日本語](README.md)" in readme_en
    assert "# xangi-search" in readme
    assert "# xangi-search" in readme_en
    setup = setup_path.read_text(encoding="utf-8")
    assert "利用スキルを提案する" in setup
    assert "uv sync --extra vector" in setup
    assert "XANGI_SEARCH_NO_VECTOR=true" in setup
    assert "fact利用を確認する" in setup
    assert "extension_request" in setup
    assert (root / "XANGI_SETUP.en.md").is_file()
    assert skill_path.is_file()
    skill = skill_path.read_text(encoding="utf-8")
    assert skill.startswith("---\nname: xs-xangi-search\n")
    assert "## factを管理する" in skill
    assert "--path /facts/similar" in skill
    assert "ADD/UPDATE/DELETE判断" in skill
    assert "http://127.0.0.1:7891" not in skill
    assert "AGENTS.md" in setup


def test_update_preparation_preserves_vector_mode(tmp_path: Path):
    root = Path(__file__).parents[1]
    prepare = root / "scripts" / "prepare-update"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text('#!/bin/sh\nprintf "%s" "$*" > "$XANGI_TEST_CAPTURE"\n')
    fake_uv.chmod(0o755)
    capture = tmp_path / "args.txt"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        "XANGI_TEST_CAPTURE": str(capture),
    }

    subprocess.run([prepare], cwd=root, env=env, check=True)
    assert capture.read_text() == "sync --frozen --extra vector"

    env["XANGI_SEARCH_NO_VECTOR"] = "true"
    subprocess.run([prepare], cwd=root, env=env, check=True)
    assert capture.read_text() == "sync --frozen"
