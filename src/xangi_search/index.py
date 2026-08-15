from __future__ import annotations

import hashlib
import heapq
import math
import os
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

import numpy as np

DEFAULT_MODEL = "intfloat/multilingual-e5-small"
DEFAULT_INCLUDE_EXTENSIONS = {
    ".bash",
    ".c",
    ".cfg",
    ".cpp",
    ".css",
    ".csv",
    ".el",
    ".env",
    ".fish",
    ".gitattributes",
    ".gitignore",
    ".go",
    ".graphql",
    ".h",
    ".hpp",
    ".html",
    ".ini",
    ".java",
    ".jl",
    ".js",
    ".json",
    ".jsonl",
    ".jsx",
    ".kt",
    ".less",
    ".lua",
    ".m",
    ".md",
    ".mm",
    ".php",
    ".pl",
    ".pm",
    ".py",
    ".r",
    ".rb",
    ".rs",
    ".scala",
    ".scss",
    ".sh",
    ".sql",
    ".swift",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".vim",
    ".yaml",
    ".yml",
    ".zsh",
}
DEFAULT_INCLUDE_NAMES = {
    ".dockerignore",
    ".dockerfile",
    ".env",
    ".env.example",
    ".gitattributes",
    ".gitignore",
    "authors",
    "changelog",
    "contributing",
    "dockerfile",
    "license",
    "makefile",
    "readme",
}
DEFAULT_EXCLUDE_PARTS = {
    ".git",
    ".next",
    ".obsidian",
    ".openclaw",
    ".pio",
    ".venv",
    ".workspace_rag",
    ".xangi",
    ".xangi-search",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}
DEFAULT_EXCLUDE_ROOTS = {"tmp", "tools"}
DEFAULT_EXCLUDE_NAMES = {
    ".ds_store",
    "package-lock.json",
    "pnpm-lock.yaml",
    "thumbs.db",
    "yarn.lock",
}
BASE_HALF_LIFE = 30
STRENGTH_PER_ACCESS = 0.5
NO_DECAY_FILES = {"MEMORY.md", "AGENTS.md", "CLAUDE.md"}
DATE_PATTERN = re.compile(r"(\d{4})[-_]?(\d{2})[-_]?(\d{2})")


@dataclass(frozen=True)
class SearchResult:
    file_path: str
    chunk_index: int
    content: str
    score: float
    base_score: float = 0.0
    path_weight: float = 1.0
    freshness: float = 1.0
    vector_score: float = 0.0
    keyword_score: float = 0.0
    fts_score: float = 0.0
    decay: float | None = None
    access_count: int | None = None


def chunk_text(text: str, size: int = 512, overlap: int = 64) -> list[str]:
    if not text.strip():
        return []
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        value = text[start : start + size]
        if value.strip():
            chunks.append(value)
        start += size - overlap
    return chunks


def default_db_path(workspace: Path) -> Path:
    key = hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()[:12]
    return workspace / ".xangi-search" / f"index-{key}.sqlite3"


class SentenceTransformerEmbedder:
    def __init__(self, model_name: str = DEFAULT_MODEL):
        from sentence_transformers import SentenceTransformer

        self.model_name = model_name
        self.model = SentenceTransformer(model_name)

    def __call__(self, texts: Sequence[str], *, query: bool = False) -> np.ndarray:
        encoder = (
            self.model.encode_query
            if query and hasattr(self.model, "encode_query")
            else None
        )
        if encoder is None:
            encoder = (
                self.model.encode_document
                if hasattr(self.model, "encode_document")
                else self.model.encode
            )
        values = encoder(
            list(texts), normalize_embeddings=True, show_progress_bar=False
        )
        return np.asarray(values, dtype=np.float32)


class SearchIndex:
    def __init__(
        self,
        workspace: Path,
        db_path: Path | None = None,
        embedder: Callable[..., np.ndarray] | None = None,
    ):
        self.workspace = workspace.resolve()
        self.db_path = db_path or default_db_path(self.workspace)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.embedder = embedder
        self.connection = sqlite3.connect(self.db_path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._write_lock = threading.RLock()
        self._cache_lock = threading.RLock()
        self._embedding_ids: tuple[int, ...] = ()
        self._embedding_matrix = np.empty((0, 0), dtype=np.float32)
        self._fact_ids: tuple[int, ...] = ()
        self._fact_matrix = np.empty((0, 0), dtype=np.float32)
        self._init_db()
        self._import_legacy_facts_snapshot()
        self._ensure_fact_embeddings()
        self._reload_vector_cache()
        self._reload_fact_cache()

    def _read_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def close(self) -> None:
        self.connection.close()

    def _init_db(self) -> None:
        fts_columns = [
            row[1] for row in self.connection.execute("PRAGMA table_info(chunks_fts)")
        ]
        rebuild_fts = bool(fts_columns and fts_columns != ["file_path", "content"])
        if rebuild_fts:
            self.connection.executescript(
                """
                DROP TRIGGER IF EXISTS chunks_ai;
                DROP TRIGGER IF EXISTS chunks_ad;
                DROP TRIGGER IF EXISTS chunks_au;
                DROP TABLE chunks_fts;
                """
            )
        self.connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL,
                indexed_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY,
                file_path TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                content TEXT NOT NULL,
                embedding BLOB,
                UNIQUE(file_path, chunk_index)
            );
            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY,
                text TEXT NOT NULL,
                embedding BLOB,
                source_file TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                access_count INTEGER NOT NULL DEFAULT 0,
                last_accessed TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                fact_date TEXT
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                file_path,
                content,
                content='chunks',
                content_rowid='id',
                tokenize='trigram'
            );
            CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
              INSERT INTO chunks_fts(rowid, file_path, content) VALUES (new.id, new.file_path, new.content);
            END;
            CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
              INSERT INTO chunks_fts(chunks_fts, rowid, file_path, content) VALUES ('delete', old.id, old.file_path, old.content);
            END;
            CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
              INSERT INTO chunks_fts(chunks_fts, rowid, file_path, content) VALUES ('delete', old.id, old.file_path, old.content);
              INSERT INTO chunks_fts(rowid, file_path, content) VALUES (new.id, new.file_path, new.content);
            END;
            """
        )
        chunk_columns = {
            row[1]
            for row in self.connection.execute("PRAGMA table_info(chunks)").fetchall()
        }
        if "access_count" not in chunk_columns:
            self.connection.execute(
                "ALTER TABLE chunks ADD COLUMN access_count INTEGER NOT NULL DEFAULT 0"
            )
        if "last_accessed" not in chunk_columns:
            self.connection.execute("ALTER TABLE chunks ADD COLUMN last_accessed TEXT")
        if rebuild_fts:
            self.connection.execute(
                "INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')"
            )
        self.connection.commit()

    def _iter_files(self) -> Iterable[Path]:
        for root, directories, files in os.walk(self.workspace, followlinks=False):
            at_workspace_root = Path(root) == self.workspace
            directories[:] = [
                name
                for name in directories
                if name not in DEFAULT_EXCLUDE_PARTS
                and not (at_workspace_root and name in DEFAULT_EXCLUDE_ROOTS)
                and not (Path(root) / name).is_symlink()
            ]
            for name in files:
                path = Path(root) / name
                if path.is_symlink() or not path.is_file():
                    continue
                relative = path.relative_to(self.workspace)
                if any(part in DEFAULT_EXCLUDE_PARTS for part in relative.parts):
                    continue
                relative_posix = relative.as_posix()
                lower_name = path.name.lower()
                if relative.parts and relative.parts[0] in DEFAULT_EXCLUDE_ROOTS:
                    continue
                if relative_posix == "knowledge/rag_facts.md":
                    continue
                if lower_name in DEFAULT_EXCLUDE_NAMES or lower_name.endswith(".lock"):
                    continue
                if (
                    lower_name.endswith((".min.js", ".bundle.js"))
                    or path.suffix.lower() == ".js"
                ):
                    continue
                if (
                    path.suffix.lower() not in DEFAULT_INCLUDE_EXTENSIONS
                    and lower_name not in DEFAULT_INCLUDE_NAMES
                ):
                    continue
                if path.stat().st_size > 100 * 1024:
                    continue
                yield path

    def reindex(self) -> dict[str, int]:
        with self._write_lock:
            return self._reindex()

    def _reindex(self) -> dict[str, int]:
        current_paths: set[str] = set()
        changed: list[tuple[str, str, list[str]]] = []
        for path in self._iter_files():
            relative = path.relative_to(self.workspace).as_posix()
            current_paths.add(relative)
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            digest = hashlib.sha256(content.encode()).hexdigest()
            row = self.connection.execute(
                "SELECT sha256 FROM files WHERE path = ?", (relative,)
            ).fetchone()
            if row and row["sha256"] == digest:
                continue
            changed.append((relative, digest, chunk_text(content)))

        existing = {row[0] for row in self.connection.execute("SELECT path FROM files")}
        removed = existing - current_paths
        for path in removed:
            self.connection.execute("DELETE FROM chunks WHERE file_path = ?", (path,))
            self.connection.execute("DELETE FROM files WHERE path = ?", (path,))

        texts = [text for _, _, chunks in changed for text in chunks]
        embeddings: list[np.ndarray | None]
        if texts and self.embedder is not None:
            matrix = self.embedder(texts, query=False)
            embeddings = [np.asarray(row, dtype=np.float32) for row in matrix]
        else:
            embeddings = [None] * len(texts)

        cursor = 0
        for path, digest, chunks in changed:
            self.connection.execute("DELETE FROM chunks WHERE file_path = ?", (path,))
            for index, content in enumerate(chunks):
                embedding = embeddings[cursor]
                cursor += 1
                self.connection.execute(
                    "INSERT INTO chunks(file_path, chunk_index, content, embedding) VALUES (?, ?, ?, ?)",
                    (
                        path,
                        index,
                        content,
                        embedding.tobytes() if embedding is not None else None,
                    ),
                )
            self.connection.execute(
                "INSERT INTO files(path, sha256, indexed_at) VALUES (?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET sha256=excluded.sha256, indexed_at=excluded.indexed_at",
                (path, digest, time.time()),
            )
        self.connection.commit()
        if changed or removed:
            self._reload_vector_cache()
        return {
            "changed_files": len(changed),
            "removed_files": len(removed),
            "chunks": len(texts),
        }

    def _reload_vector_cache(self) -> None:
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT id, embedding FROM chunks WHERE embedding IS NOT NULL ORDER BY id"
            ).fetchall()
        ids = tuple(int(row["id"]) for row in rows)
        matrix = (
            np.vstack(
                [np.frombuffer(row["embedding"], dtype=np.float32) for row in rows]
            )
            if rows
            else np.empty((0, 0), dtype=np.float32)
        )
        with self._cache_lock:
            self._embedding_ids = ids
            self._embedding_matrix = matrix

    def _reload_fact_cache(self) -> None:
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT id, embedding FROM facts WHERE is_active = 1 AND embedding IS NOT NULL ORDER BY id"
            ).fetchall()
        ids = tuple(int(row["id"]) for row in rows)
        matrix = (
            np.vstack(
                [np.frombuffer(row["embedding"], dtype=np.float32) for row in rows]
            )
            if rows
            else np.empty((0, 0), dtype=np.float32)
        )
        with self._cache_lock:
            self._fact_ids = ids
            self._fact_matrix = matrix

    def _import_legacy_facts_snapshot(self) -> None:
        count = self.connection.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        snapshot = self.workspace / "knowledge" / "rag_facts.md"
        if count or not snapshot.is_file():
            return
        try:
            content = snapshot.read_text(encoding="utf-8")
        except OSError:
            return
        imported = 0
        now = datetime.now().astimezone().isoformat()
        for match in re.finditer(
            r"(?ms)^## fact #(\d+)\s*\n(.*?)(?=^## fact #\d+|\Z)", content
        ):
            fact_id = int(match.group(1))
            block = match.group(2).strip()
            metadata: dict[str, str] = {}
            body_lines: list[str] = []
            in_body = False
            for line in block.splitlines():
                if not in_body and line.startswith("- ") and ":" in line:
                    key, value = line[2:].split(":", 1)
                    metadata[key.strip()] = value.strip()
                    continue
                if not in_body and not line.strip():
                    continue
                in_body = True
                body_lines.append(line)
            text = "\n".join(body_lines).strip()
            if not text:
                continue
            self.connection.execute(
                "INSERT INTO facts(id, text, source_file, created_at, updated_at, fact_date) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    fact_id,
                    text,
                    metadata.get("source_file"),
                    metadata.get("created", now),
                    metadata.get("updated", now),
                    metadata.get("fact_date"),
                ),
            )
            imported += 1
        if imported:
            self.connection.commit()

    def _ensure_fact_embeddings(self) -> None:
        if self.embedder is None:
            return
        rows = self.connection.execute(
            "SELECT id, text FROM facts WHERE is_active = 1 AND embedding IS NULL ORDER BY id"
        ).fetchall()
        if not rows:
            return
        matrix = self.embedder([row["text"] for row in rows], query=False)
        self.connection.executemany(
            "UPDATE facts SET embedding = ? WHERE id = ?",
            [
                (np.asarray(vector, dtype=np.float32).tobytes(), int(row["id"]))
                for row, vector in zip(rows, matrix)
            ],
        )
        self.connection.commit()

    def stats(self) -> dict[str, int | float | str | None]:
        with self._read_connection() as connection:
            files = connection.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            chunks = connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            vectors = connection.execute(
                "SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL"
            ).fetchone()[0]
            last_indexed_at = connection.execute(
                "SELECT MAX(indexed_at) FROM files"
            ).fetchone()[0]
            facts = connection.execute(
                "SELECT COUNT(*) FROM facts WHERE is_active = 1"
            ).fetchone()[0]
        return {
            "workspace": str(self.workspace),
            "files": files,
            "chunks": chunks,
            "vectors": vectors,
            "last_indexed_at": last_indexed_at,
            "facts": facts,
        }

    def encode_query(self, query: str) -> np.ndarray | None:
        if self.embedder is None:
            return None
        return np.asarray(self.embedder([query], query=True)[0], dtype=np.float32)

    @staticmethod
    def _fts_query(query: str) -> str:
        tokens = re.findall(r"[\w.-]+", query, flags=re.UNICODE)
        return " OR ".join(
            f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens[:12]
        )

    def _keyword_scores(
        self, connection: sqlite3.Connection, query: str, limit: int
    ) -> dict[int, float]:
        expression = self._fts_query(query)
        if not expression:
            return {}
        rows = connection.execute(
            "SELECT rowid, bm25(chunks_fts, 2.0, 1.0) AS rank FROM chunks_fts "
            "WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?",
            (expression, max(limit * 50, 200)),
        ).fetchall()
        if not rows:
            return {}
        raw = {int(row["rowid"]): max(0.0, -float(row["rank"])) for row in rows}
        maximum = max(raw.values(), default=0.0)
        return {
            key: (value / maximum if maximum else 1.0) for key, value in raw.items()
        }

    def _vector_scores(
        self, query: str, query_vector: np.ndarray | None = None
    ) -> dict[int, float]:
        if self.embedder is None:
            return {}
        with self._cache_lock:
            ids = self._embedding_ids
            matrix = self._embedding_matrix
        if not ids:
            return {}
        if query_vector is None:
            query_vector = self.encode_query(query)
        if query_vector is None:
            return {}
        scores = matrix @ query_vector
        return {chunk_id: float(score) for chunk_id, score in zip(ids, scores)}

    @staticmethod
    def _path_weight(
        file_path: str, path_weights: Mapping[str, float], default_path_weight: float
    ) -> float:
        matches = (
            (prefix, weight)
            for prefix, weight in path_weights.items()
            if file_path.startswith(prefix)
        )
        return max(
            matches, key=lambda item: len(item[0]), default=("", default_path_weight)
        )[1]

    def _freshness(self, file_path: str) -> float:
        try:
            days_old = (
                time.time() - (self.workspace / file_path).stat().st_mtime
            ) / 86400
            return max(0.5, 1.0 - days_old / 365)
        except OSError:
            return 0.7

    @staticmethod
    def _memory_decay(
        file_path: str, access_count: int = 0, last_accessed: str | None = None
    ) -> float:
        path = Path(file_path)
        if path.name in NO_DECAY_FILES:
            return 1.0
        elapsed: int | None = None
        if last_accessed:
            try:
                elapsed = (
                    datetime.now().astimezone().date()
                    - date.fromisoformat(last_accessed[:10])
                ).days
            except (TypeError, ValueError):
                pass
        if elapsed is None:
            match = DATE_PATTERN.search(file_path)
            if match:
                try:
                    elapsed = (
                        datetime.now().astimezone().date()
                        - date(
                            int(match.group(1)),
                            int(match.group(2)),
                            int(match.group(3)),
                        )
                    ).days
                except ValueError:
                    pass
        if elapsed is None:
            return 0.5
        if elapsed < 0:
            return 1.0
        strength = BASE_HALF_LIFE * (1 + access_count * STRENGTH_PER_ACCESS)
        return math.exp(-math.log(2) * elapsed / strength)

    def search(
        self,
        query: str,
        *,
        mode: str = "hybrid",
        limit: int = 8,
        min_score: float = 0.0,
        path_weights: Mapping[str, float] | None = None,
        default_path_weight: float = 1.0,
        query_vector: np.ndarray | None = None,
        forgetting: bool = False,
        timings: dict[str, float] | None = None,
    ) -> list[SearchResult]:
        if mode not in {"hybrid", "vector", "keyword"}:
            raise ValueError("mode must be hybrid, vector, or keyword")
        weights = path_weights if path_weights is not None else {}
        with self._read_connection() as connection:
            started = time.perf_counter()
            keyword = (
                self._keyword_scores(connection, query, limit)
                if mode != "vector"
                else {}
            )
            keyword_finished = time.perf_counter()
            vector = (
                self._vector_scores(query, query_vector) if mode != "keyword" else {}
            )
            if vector:
                vector = dict(
                    heapq.nlargest(
                        max(limit * 4, 20), vector.items(), key=lambda item: item[1]
                    )
                )
            vector_finished = time.perf_counter()
            candidates = set(keyword) | set(vector)
            if candidates:
                placeholders = ",".join("?" for _ in candidates)
                rows = connection.execute(
                    f"SELECT id, file_path, chunk_index, content, access_count, last_accessed "
                    f"FROM chunks WHERE id IN ({placeholders})",
                    tuple(candidates),
                ).fetchall()
                rows_by_id = {int(row["id"]): row for row in rows}
            else:
                rows_by_id = {}
            rows_finished = time.perf_counter()
            scored: list[tuple[int, float, float, float, float, float, int]] = []
            for chunk_id in candidates:
                if mode == "keyword":
                    score = keyword.get(chunk_id, 0.0)
                elif mode == "vector":
                    score = vector.get(chunk_id, 0.0)
                elif not vector:
                    score = keyword.get(chunk_id, 0.0)
                elif not keyword:
                    score = vector.get(chunk_id, 0.0)
                else:
                    score = 0.7 * vector.get(chunk_id, 0.0) + 0.3 * keyword.get(
                        chunk_id, 0.0
                    )
                base_score = score
                row = rows_by_id.get(chunk_id)
                path_weight = (
                    self._path_weight(row["file_path"], weights, default_path_weight)
                    if row is not None
                    else default_path_weight
                )
                freshness = (
                    self._freshness(row["file_path"])
                    if forgetting and row is not None
                    else 1.0
                )
                access_count = int(row["access_count"] or 0) if row is not None else 0
                decay = (
                    self._memory_decay(
                        row["file_path"], access_count, row["last_accessed"]
                    )
                    if forgetting and row is not None
                    else 1.0
                )
                score *= path_weight * freshness * decay
                if score >= min_score:
                    scored.append(
                        (
                            chunk_id,
                            score,
                            base_score,
                            path_weight,
                            freshness,
                            decay,
                            access_count,
                        )
                    )
            scored.sort(key=lambda item: item[1], reverse=True)
            results: list[SearchResult] = []
            seen_files: set[str] = set()
            returned_ids: list[int] = []
            for (
                chunk_id,
                score,
                base_score,
                path_weight,
                freshness,
                decay,
                access_count,
            ) in scored:
                row = rows_by_id.get(chunk_id)
                if row is None or row["file_path"] in seen_files:
                    continue
                seen_files.add(row["file_path"])
                returned_ids.append(chunk_id)
                results.append(
                    SearchResult(
                        file_path=row["file_path"],
                        chunk_index=row["chunk_index"],
                        content=row["content"],
                        score=round(score, 6),
                        base_score=round(base_score, 6),
                        path_weight=round(path_weight, 6),
                        freshness=round(freshness, 6),
                        vector_score=round(vector.get(chunk_id, 0.0), 6),
                        keyword_score=round(keyword.get(chunk_id, 0.0), 6),
                        fts_score=round(keyword.get(chunk_id, 0.0), 6),
                        decay=round(decay, 6) if forgetting else None,
                        access_count=access_count if forgetting else None,
                    )
                )
                if len(results) >= limit:
                    break
            if forgetting and returned_ids:
                today = datetime.now().astimezone().date().isoformat()
                with self._write_lock:
                    self.connection.executemany(
                        "UPDATE chunks SET access_count = access_count + 1, last_accessed = ? WHERE id = ?",
                        [(today, chunk_id) for chunk_id in returned_ids],
                    )
                    self.connection.commit()
            if timings is not None:
                finished = time.perf_counter()
                timings.update(
                    {
                        "fts": (keyword_finished - started) * 1000,
                        "vector": (vector_finished - keyword_finished) * 1000,
                        "load_candidates": (rows_finished - vector_finished) * 1000,
                        "rank_and_build": (finished - rows_finished) * 1000,
                    }
                )
            return results

    def search_payload(self, query: str, **kwargs: object) -> dict[str, object]:
        started = time.perf_counter()
        timings: dict[str, float] = {}
        results = self.search(query, timings=timings, **kwargs)
        elapsed_ms = (time.perf_counter() - started) * 1000
        return {
            "schema_version": 1,
            "query": query,
            "mode": kwargs.get("mode", "hybrid"),
            "elapsed_ms": round(elapsed_ms, 1),
            "timings_ms": {key: round(value, 1) for key, value in timings.items()},
            "count": len(results),
            "results": [
                {
                    key: value
                    for key, value in asdict(result).items()
                    if value is not None
                }
                for result in results
            ],
        }

    def list_facts(self) -> list[dict[str, object]]:
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT id, text, source_file, created_at, updated_at, access_count, is_active, fact_date "
                "FROM facts ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def add_facts(self, facts: list[dict[str, object]]) -> list[dict[str, object]]:
        with self._write_lock:
            return self._add_facts(facts)

    def _add_facts(self, facts: list[dict[str, object]]) -> list[dict[str, object]]:
        valid = [
            fact
            for fact in facts
            if isinstance(fact.get("text"), str) and fact["text"].strip()
        ]
        if not valid:
            return []
        texts = [str(fact["text"]).strip() for fact in valid]
        embeddings = (
            self.embedder(texts, query=False)
            if self.embedder is not None
            else [None] * len(texts)
        )
        now = datetime.now().astimezone().isoformat()
        results: list[dict[str, object]] = []
        for fact, text, embedding in zip(valid, texts, embeddings):
            vector = (
                None
                if embedding is None
                else np.asarray(embedding, dtype=np.float32).tobytes()
            )
            cursor = self.connection.execute(
                "INSERT INTO facts(text, embedding, source_file, created_at, updated_at, fact_date) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    text,
                    vector,
                    fact.get("source_file"),
                    now,
                    now,
                    fact.get("fact_date"),
                ),
            )
            results.append({"id": cursor.lastrowid, "text": text})
        self.connection.commit()
        self._reload_fact_cache()
        self._export_facts_snapshot()
        return results

    def update_fact(
        self, fact_id: int, payload: dict[str, object]
    ) -> dict[str, object] | None:
        with self._write_lock:
            return self._update_fact(fact_id, payload)

    def _update_fact(
        self, fact_id: int, payload: dict[str, object]
    ) -> dict[str, object] | None:
        row = self.connection.execute(
            "SELECT * FROM facts WHERE id = ?", (fact_id,)
        ).fetchone()
        if row is None:
            return None
        text = payload.get("text", row["text"])
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a non-empty string")
        text = text.strip()
        embedding = row["embedding"]
        if text != row["text"] and self.embedder is not None:
            embedding = np.asarray(
                self.embedder([text], query=False)[0], dtype=np.float32
            ).tobytes()
        updated_at = datetime.now().astimezone().isoformat()
        self.connection.execute(
            "UPDATE facts SET text = ?, embedding = ?, source_file = ?, fact_date = ?, "
            "updated_at = ?, is_active = 1 WHERE id = ?",
            (
                text,
                embedding,
                payload.get("source_file", row["source_file"]),
                payload.get("fact_date", row["fact_date"]),
                updated_at,
                fact_id,
            ),
        )
        self.connection.commit()
        self._reload_fact_cache()
        self._export_facts_snapshot()
        return {"id": fact_id, "text": text, "updated_at": updated_at}

    def delete_fact(self, fact_id: int) -> dict[str, object] | None:
        with self._write_lock:
            return self._delete_fact(fact_id)

    def _delete_fact(self, fact_id: int) -> dict[str, object] | None:
        row = self.connection.execute(
            "SELECT text FROM facts WHERE id = ?", (fact_id,)
        ).fetchone()
        if row is None:
            return None
        updated_at = datetime.now().astimezone().isoformat()
        self.connection.execute(
            "UPDATE facts SET is_active = 0, updated_at = ? WHERE id = ?",
            (updated_at, fact_id),
        )
        self.connection.commit()
        self._reload_fact_cache()
        self._export_facts_snapshot()
        return {"id": fact_id, "text": row["text"], "is_active": 0}

    def search_facts(
        self, query_vector: np.ndarray | None, limit: int = 3, min_score: float = 0.5
    ) -> list[dict[str, object]]:
        if query_vector is None:
            return []
        with self._cache_lock:
            ids = self._fact_ids
            matrix = self._fact_matrix
        if not ids:
            return []
        scores = matrix @ query_vector
        ranked = sorted(zip(scores, ids), reverse=True)[:limit]
        selected = [
            (float(score), fact_id) for score, fact_id in ranked if score >= min_score
        ]
        if not selected:
            return []
        placeholders = ",".join("?" for _ in selected)
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT id, text, source_file, created_at, updated_at, access_count, fact_date "
                f"FROM facts WHERE id IN ({placeholders})",
                tuple(fact_id for _, fact_id in selected),
            ).fetchall()
        by_id = {int(row["id"]): row for row in rows}
        today = datetime.now().astimezone().date().isoformat()
        results: list[dict[str, object]] = []
        for score, fact_id in selected:
            row = by_id.get(fact_id)
            if row is None:
                continue
            results.append(
                {
                    "type": "fact",
                    "id": fact_id,
                    "text": row["text"],
                    "source_file": row["source_file"],
                    "score": round(score, 4),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "access_count": row["access_count"],
                    "fact_date": row["fact_date"],
                }
            )
            with self._write_lock:
                self.connection.execute(
                    "UPDATE facts SET access_count = access_count + 1, last_accessed = ? WHERE id = ?",
                    (today, fact_id),
                )
        with self._write_lock:
            self.connection.commit()
        return results

    def find_similar_facts(self, query: str, limit: int = 3) -> list[dict[str, object]]:
        return self.search_facts(self.encode_query(query), limit=limit, min_score=-1.0)

    def _export_facts_snapshot(self) -> None:
        facts = [fact for fact in self.list_facts() if fact["is_active"] == 1]
        lines = [
            "<!-- Generated by xangi-search. Do not edit directly. -->",
            "",
            "# xangi-search facts snapshot",
            "",
        ]
        for fact in facts:
            lines.extend(
                [
                    f"## fact #{fact['id']}",
                    "",
                    f"- created: {fact['created_at']}",
                    f"- updated: {fact['updated_at']}",
                ]
            )
            if fact["fact_date"]:
                lines.append(f"- fact_date: {fact['fact_date']}")
            if fact["source_file"]:
                lines.append(f"- source_file: {fact['source_file']}")
            lines.extend(["", str(fact["text"]), ""])
        target = self.workspace / "knowledge" / "rag_facts.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".md.tmp")
        temporary.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        temporary.replace(target)
