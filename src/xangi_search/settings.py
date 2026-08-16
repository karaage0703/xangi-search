from __future__ import annotations

import json
import math
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class SearchSettings:
    mode: str = "hybrid"
    limit: int = 8
    min_score: float = 0.0
    auto_reindex: bool = True
    reindex_interval_seconds: int = 1800
    path_weights: dict[str, float] = field(default_factory=dict)
    default_path_weight: float = 1.0
    forgetting: bool = False
    facts_snapshot_path: str = "knowledge/rag_facts.md"


class SettingsStore:
    def __init__(
        self,
        path: Path,
        default_path_weights: Mapping[str, float] | None = None,
    ):
        self.path = path
        self.default_path_weights = validate_path_weights(
            dict(default_path_weights) if default_path_weights is not None else {}
        )
        self._lock = threading.RLock()
        self._settings = self._load()

    def _defaults(self) -> SearchSettings:
        return SearchSettings(path_weights=dict(self.default_path_weights))

    def _load(self) -> SearchSettings:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._defaults()
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return self._defaults()
        if not isinstance(payload, dict):
            return self._defaults()
        try:
            return validate_settings(payload, self.default_path_weights)
        except ValueError:
            return self._defaults()

    def get(self) -> SearchSettings:
        with self._lock:
            return self._settings

    def update(self, payload: dict[str, object]) -> SearchSettings:
        with self._lock:
            current = asdict(self._settings)
            unknown = set(payload) - set(current) - {"schema_version"}
            if unknown:
                raise ValueError(f"unknown settings: {', '.join(sorted(unknown))}")
            current.update(
                {key: value for key, value in payload.items() if key in current}
            )
            settings = validate_settings(current)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            target = self.path
            temporary = target.with_suffix(f"{target.suffix}.tmp")
            temporary.write_text(
                json.dumps({"schema_version": 1, **asdict(settings)}, indent=2) + "\n",
                encoding="utf-8",
            )
            os.chmod(temporary, 0o600)
            temporary.replace(target)
            self._settings = settings
            return settings

    def payload(self) -> dict[str, object]:
        return {"schema_version": 1, **asdict(self.get())}


def validate_settings(
    payload: dict[str, object],
    default_path_weights: Mapping[str, float] | None = None,
) -> SearchSettings:
    mode = payload.get("mode", "hybrid")
    limit = payload.get("limit", 8)
    min_score = payload.get("min_score", 0.0)
    auto_reindex = payload.get("auto_reindex", True)
    interval = payload.get("reindex_interval_seconds", 1800)
    path_weights = validate_path_weights(
        payload.get(
            "path_weights",
            dict(default_path_weights) if default_path_weights is not None else {},
        )
    )
    default_path_weight = validate_weight(
        payload.get("default_path_weight", 1.0), "default_path_weight"
    )
    forgetting = payload.get("forgetting", False)
    facts_snapshot_path = validate_facts_snapshot_path(
        payload.get("facts_snapshot_path", "knowledge/rag_facts.md")
    )
    if mode not in {"hybrid", "vector", "keyword"}:
        raise ValueError("mode must be hybrid, vector, or keyword")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 30:
        raise ValueError("limit must be an integer between 1 and 30")
    if isinstance(min_score, bool) or not isinstance(min_score, (int, float)):
        raise ValueError("min_score must be a number between 0 and 1")
    if not 0.0 <= float(min_score) <= 1.0:
        raise ValueError("min_score must be a number between 0 and 1")
    if not isinstance(auto_reindex, bool):
        raise ValueError("auto_reindex must be a boolean")
    if not isinstance(forgetting, bool):
        raise ValueError("forgetting must be a boolean")
    if (
        isinstance(interval, bool)
        or not isinstance(interval, int)
        or not 60 <= interval <= 86400
    ):
        raise ValueError("reindex_interval_seconds must be between 60 and 86400")
    return SearchSettings(
        mode=str(mode),
        limit=limit,
        min_score=float(min_score),
        auto_reindex=auto_reindex,
        reindex_interval_seconds=interval,
        path_weights=path_weights,
        default_path_weight=default_path_weight,
        forgetting=forgetting,
        facts_snapshot_path=facts_snapshot_path,
    )


def validate_facts_snapshot_path(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(
            "facts_snapshot_path must be a workspace-relative Markdown path"
        )
    candidate = value.strip().replace("\\", "/")
    parts = [part for part in candidate.split("/") if part not in {"", "."}]
    if (
        not candidate
        or candidate.startswith("/")
        or not parts
        or any(part == ".." for part in parts)
        or ":" in parts[0]
    ):
        raise ValueError(
            "facts_snapshot_path must be a workspace-relative Markdown path"
        )
    normalized = "/".join(parts)
    if not normalized.lower().endswith(".md"):
        raise ValueError("facts_snapshot_path must end with .md")
    if parts[0].lower() in {".git", ".xangi-search"}:
        raise ValueError("facts_snapshot_path cannot use a protected directory")
    return normalized


def validate_weight(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number between 0 and 10")
    weight = float(value)
    if not math.isfinite(weight) or not 0.0 <= weight <= 10.0:
        raise ValueError(f"{name} must be a number between 0 and 10")
    return weight


def normalize_path_prefix(value: str) -> str:
    candidate = value.strip().replace("\\", "/")
    if not candidate or candidate.startswith("/"):
        raise ValueError("path weight prefixes must be relative workspace paths")
    parts = [part for part in candidate.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise ValueError("path weight prefixes must be relative workspace paths")
    return "/".join(parts) + "/"


def validate_path_weights(value: object) -> dict[str, float]:
    if not isinstance(value, dict):
        raise ValueError("path_weights must be an object")
    if len(value) > 100:
        raise ValueError("path_weights must contain at most 100 entries")
    normalized: dict[str, float] = {}
    for prefix, weight in value.items():
        if not isinstance(prefix, str):
            raise ValueError("path weight prefixes must be strings")
        path_prefix = normalize_path_prefix(prefix)
        if path_prefix in normalized:
            raise ValueError(f"duplicate path weight prefix: {path_prefix}")
        normalized[path_prefix] = validate_weight(
            weight, f"path_weights[{path_prefix}]"
        )
    return normalized


def default_settings_path(workspace: Path) -> Path:
    return workspace.resolve() / ".xangi-search" / "settings.json"
