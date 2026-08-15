from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


def state_dir() -> Path:
    configured = os.environ.get("XANGI_SEARCH_STATE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return root / "xangi-search"


def state_file() -> Path:
    return state_dir() / "service.json"


def read_state() -> dict[str, object]:
    try:
        value = json.loads(state_file().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def health(url: str) -> dict[str, object] | None:
    try:
        with urlopen(url, timeout=2) as response:
            value = json.load(response)
            if response.status == 200 and value.get("service") == "xangi-search":
                return value if isinstance(value, dict) else None
            return None
    except (OSError, URLError, json.JSONDecodeError):
        return None


def status(expected_workspace: Path | None = None) -> dict[str, object]:
    state = read_state()
    pid = state.get("pid")
    url = str(state.get("healthUrl") or "http://127.0.0.1:7891/health")
    running = isinstance(pid, int) and process_alive(pid)
    health_payload = health(url) if running else None
    healthy = health_payload is not None
    workspace = state.get("workspace") if isinstance(state.get("workspace"), str) else None
    workspace_matches = (
        expected_workspace is None
        or workspace == str(expected_workspace.expanduser().resolve())
    )
    return {
        "schemaVersion": 1,
        "id": "xangi-search",
        "running": running,
        "healthy": healthy,
        "pid": pid if running else None,
        "healthUrl": url,
        "workspace": workspace,
        "workspaceMatches": workspace_matches,
        **({"health": health_payload} if health_payload is not None else {}),
    }


def start(workspace: Path, port: int, no_vector: bool) -> dict[str, object]:
    current = status()
    if current["running"]:
        return {**current, "changed": False}
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    log_path = directory / "service.log"
    command = [
        sys.executable,
        "-m",
        "xangi_search.cli",
        "--workspace",
        str(workspace.resolve()),
    ]
    if no_vector:
        command.append("--no-vector")
    command.extend(["serve", "--port", str(port)])
    with log_path.open("ab", buffering=0) as log:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    payload = {
        "pid": process.pid,
        "workspace": str(workspace.resolve()),
        "healthUrl": f"http://127.0.0.1:{port}/health",
        "logPath": str(log_path),
        "startedAt": time.time(),
    }
    target = state_file()
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(target)
    for _ in range(300):
        current = status()
        if current["healthy"]:
            return {**current, "changed": True}
        if not process_alive(process.pid):
            break
        time.sleep(0.1)
    if process_alive(process.pid):
        os.kill(process.pid, signal.SIGTERM)
        for _ in range(50):
            if not process_alive(process.pid):
                break
            time.sleep(0.1)
    state_file().unlink(missing_ok=True)
    raise RuntimeError(f"xangi-search failed to become healthy; inspect {log_path}")


def stop() -> dict[str, object]:
    current = read_state()
    pid = current.get("pid")
    if not isinstance(pid, int) or not process_alive(pid):
        state_file().unlink(missing_ok=True)
        return {**status(), "changed": False}
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        if not process_alive(pid):
            break
        time.sleep(0.1)
    if process_alive(pid):
        raise RuntimeError(f"xangi-search process {pid} did not stop")
    state_file().unlink(missing_ok=True)
    return {**status(), "changed": True}


def main() -> None:
    parser = argparse.ArgumentParser(prog="xangi-search-extension")
    parser.add_argument("action", choices=["start", "stop", "restart", "status", "doctor", "update"])
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--port", type=int, default=int(os.environ.get("XANGI_SEARCH_PORT", "7891")))
    parser.add_argument("--no-vector", action="store_true")
    args = parser.parse_args()

    if args.action in {"status", "doctor"}:
        result = status(args.workspace if args.action == "doctor" else None)
        health_payload = result.get("health")
        index_ready = isinstance(health_payload, dict) and health_payload.get(
            "initial_index_complete"
        ) is True
        index_error = (
            health_payload.get("last_reindex_error") if isinstance(health_payload, dict) else None
        )
        vector_ready = True
        if isinstance(health_payload, dict) and health_payload.get("vector_enabled") is True:
            vector_ready = health_payload.get("files", 0) == 0 or health_payload.get("vectors", 0) > 0
        result["ok"] = bool(
            result["healthy"]
            and result["workspaceMatches"]
            and index_ready
            and not index_error
            and vector_ready
        )
        if args.action == "doctor" and not result["ok"]:
            if not result["workspaceMatches"]:
                result["detail"] = "runtime workspace does not match xangi workspace"
            elif index_error:
                result["detail"] = f"last reindex failed: {index_error}"
            elif not index_ready:
                result["detail"] = "initial index is still running"
            elif not vector_ready:
                result["detail"] = "vector search is enabled but embeddings are missing"
            else:
                result["detail"] = "xangi-search is not healthy"
    elif args.action == "start":
        result = start(
            args.workspace,
            args.port,
            args.no_vector or os.environ.get("XANGI_SEARCH_NO_VECTOR") == "true",
        )
    elif args.action == "stop":
        result = stop()
    elif args.action == "restart":
        stop()
        result = start(
            args.workspace,
            args.port,
            args.no_vector or os.environ.get("XANGI_SEARCH_NO_VECTOR") == "true",
        )
    else:
        result = {
            "schemaVersion": 1,
            "id": "xangi-search",
            "ok": False,
            "unsupported": True,
            "detail": "checkout updates are managed by Git; packaged update support is not implemented",
        }
    print(json.dumps(result, ensure_ascii=False))
    if args.action == "doctor" and not result.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
