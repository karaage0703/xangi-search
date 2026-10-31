[日本語](../design.md) | English

# Design Document

This document describes the architecture, search pipeline, API, and persistence model of xangi-search.

## Overview

xangi-search is a [xangi](https://github.com/karaage0703/xangi) extension that searches text files
inside a workspace. The search process itself does not use an LLM.

```text
User / Agent → xangi → same-origin proxy → xangi-search → Workspace
```

## Managed extension architecture

`xangi-extension.json` declares a schema v2 `managed-http` runtime and the `workspace.search`
capability. xangi starts the foreground `serve` process and manages:

- the workspace path
- an OS-assigned localhost port
- a runtime-only Bearer token
- start, stop, status, doctor, update, and rollback

The extension does not use a fixed port, PID file, or URL in its manifest. It exits when the parent
process closes stdin. Browsers and AI agents access it through xangi's same-origin proxy.

The managed process binds its HTTP listener and emits the existing `ready` event before loading the
embedding model and `SearchIndex` in the background. Here, `ready` means that the transport can
answer status requests; it does not mean that the search index is ready. `GET /health` reads only
in-memory lifecycle data and does not query SQLite.

- `initialization_phase`: one of `starting`, `initializing`, `loading_embedder`, `loading_index`,
  `ready`, `error`, or `stopping`
- `initialization_error`: initialization failure detail, or `null` after success
- `index_available`: whether an index is attached and can serve search requests
- `usable_snapshot`: whether a usable existing snapshot or successfully refreshed index is available
- `ready`: whether a usable snapshot is available to serve search requests

While no usable snapshot is available, `/health`, `/ui`, `/settings` (GET), and `/file` remain
available. Index-dependent search, FACT, settings mutation, and reindex endpoints return HTTP 503 with
`Retry-After: 2`, allowing callers to retry without killing the process. If shutdown races with
initialization, the completed index is closed instead of being attached.
The 503 response `phase` distinguishes model/index loading from `initial_reindex` and
`initial_reindex_failed` states.

A usable existing snapshot continues serving searches during the startup refresh. If that refresh
fails, health remains `ready: true` and reports `degraded: true` plus `last_reindex_error`. A fresh
database remains `ready: false` until its first successful reindex.

During shutdown, new reindex work is rejected and the initialization, reindex, and automatic update
threads are joined before SQLite is closed. This prevents workers from using a closed connection
when shutdown races with attachment or an active refresh.

## Search pipeline

SQLite is the source of truth for the index. The default Hybrid mode combines embedding-based
semantic search with SQLite FTS5 keyword search. The Hybrid base score uses the keyword score as a
floor so that an exact match omitted from the vector candidates cannot rank below a semantic-only
candidate. When vector dependencies are unavailable, Hybrid falls back to Keyword mode. An explicit Vector request without an available encoder returns an error rather than a successful empty result.

If the final fixed-width chunk is no longer than the overlap, its content is already present in the
previous chunk and no redundant chunk is created. Directory exclusions for dependencies, build
artifacts, and runtime state are declared once as glob patterns and shared by incremental indexing
and grep fallbacks. Root-only patterns remain separate instead of being hard-coded per search path.

A bounded grep fallback supplements searches with too few results. Directory weights, a minimum
score, and optional time decay are applied to the final ranking. The embedding matrix is cached in
memory instead of being read from SQLite for every query.

Search pins a SQLite read transaction and coordinates DB commits with vector-cache publication under the same lock. Scores and content come from one generation while background encoding leaves the existing snapshot searchable. Hybrid enables keyword retries when the query encoder is busy. Both grep implementations use literal matching, and the UI, search agent and Preflight consume supplemental grep hits.

Incremental indexing runs at startup and at the configured interval. Once the `SearchIndex` is
attached, an existing index becomes searchable while the startup refresh continues in the
background.

The default `intfloat/multilingual-e5-small` and its `base`/`large` siblings use
`query: ` for queries and `passage: ` for documents and FACTs. Instruct variants,
other models, and local paths retain their own prompt policy.

SQLite `embedding_metadata` stores the model name, prompts, maximum input length,
and encoding policy. When these change, or a legacy index has no metadata, stored
chunks and active FACTs are re-embedded in batches of 64 before serving queries.
Text, IDs, and access history are preserved. Vectors and metadata commit together
only after success; failure rolls back and fails initialization, allowing a retry
on the next startup. The first upgrade may therefore take longer than usual.
Keyword-only sessions preserve metadata; returning to vector mode also fills
missing embeddings. Replacing weights under the same model name is not detected.
Custom embedders can opt in with a stable string `embedding_signature` attribute.

## FACTs

FACTs are structured facts stored in SQLite. Each add, update, or deactivation writes a Markdown
snapshot. The default output path is `.xangi-search/facts.md`.

The snapshot must be a `.md` path under the workspace’s `.xangi-search/` directory.
Symlinks and existing files without the generated snapshot header are rejected. Exports use
a unique temporary file, so existing temporary files are not overwritten.

Failed writes roll back DB changes instead of leaving them for a later operation to commit. FACT snapshots are generated from pending changes before the DB and search cache are published. Settings are persisted only after a required snapshot export succeeds. This does not provide a single transaction across DB and files for power loss or forced process termination.

Snapshot format 2 stores each FACT as a single-line JSON record in Markdown, preserving headings, metadata-like text and newlines. Legacy snapshots remain readable; restoring format 2 requires a compatible version. Empty-DB recovery prefers the configured snapshot, and an existing empty snapshot does not fall back to older files. Editing without an encoder invalidates stale vectors; missing vectors are generated when the encoder returns or a FACT is reactivated.

`XANGI_SEARCH_READ_ONLY=true` is captured when the server is constructed. After authentication,
PUT/DELETE and POST requests other than `POST /agent` return 403. GET, agent search, startup
indexing and periodic indexing remain available. This is an API mutation restriction, not a
read-only filesystem mode; search access statistics still update.

## Persistence

Each workspace stores its index and settings under `.xangi-search/`. xangi instances that use
different workspaces have separate indexes, processes, and ports. Concurrent updates to the same
workspace from multiple instances are outside the supported operating model.

## HTTP API

When started as a managed extension, every endpoint requires the runtime Bearer token. Normal clients
use the xangi proxy or extension API instead of handling the child process port or token directly.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Version, capabilities, and index state |
| `GET` | `/ui` | Web UI for search, FACTs, and settings |
| `GET` | `/search` | File search with `q`, `mode`, `k`, `s`, `forgetting`, `r2ag`, `context_chunks`, and `context_results` |
| `GET` | `/file` | Read a small text file inside the workspace |
| `POST` | `/agent` | xangi agent backend contract |
| `GET` / `PUT` | `/settings` | Read or update settings |
| `POST` | `/reindex` | Start incremental indexing asynchronously |
| `GET` / `POST` | `/facts` | List or add FACTs |
| `GET` | `/facts/similar` | Find similar FACTs |
| `GET` / `PUT` / `DELETE` | `/facts/{id}` | Retrieve, update, or deactivate a FACT. Retrieval also returns inactive FACTs |

Regular service responses use `schema_version: 1`, while `/agent` uses the xangi contract field
`schemaVersion: 1`. `POST /reindex` returns HTTP 202 when accepted; clients use `GET /health` to
observe completion.

`context_chunks` is an integer from 0 to 2. The default, 0, preserves the existing response with only the top-ranked chunk from each file. Positive values keep the ranking unchanged and add neighboring chunks from the same file, in order, under `context.chunks` for the top `context_results` results (default and maximum 3). This optional mode lets agents combine discovery and evidence reading in one round trip without increasing normal search responses.

## Updates

Repository-managed builds declare `./scripts/prepare-update` through `update.prepare` in the
manifest. The preparation program normally runs `uv sync --frozen --extra vector`, or
`uv sync --frozen` for keyword-only setups. xangi owns source switching, startup verification,
doctor checks, and rollback after a failed update.

Workspace-specific `exclude_paths` are validated by the settings store and applied during index initialization and settings updates. Directory pruning, existing candidate filtering and grep fallback share the same relative-path boundary check. Initialization and settings updates immediately remove excluded chunks and vectors while retaining source files and FACTs.

The settings store treats only a missing file as an initial state. Read, JSON parsing, and validation failures propagate as SettingsLoadError, blocking index construction and fallback search. API updates persist settings only after successful validation.

Environment files (`.env`, `.env.*`, `*.env`, case-insensitive at any depth) are always excluded from indexing and both grep implementations. Path weights of zero are not exclusions: candidate selection happens before weighting. Use `exclude_paths` to keep logs from consuming candidates. Logical deletion does not guarantee forensic erasure from database pages, backups or previously copied logs.

### Initialization group

The manifest declares startup.exclusiveGroup=gpu with timeoutSeconds=120. Supporting hosts serialize model initialization for this group. Health startup.state is initializing while constructing the model/index, complete once constructed, or failed after an initialization exception. This is independent of initial reindexing and snapshot readiness. This change adds neither automatic retries nor CPU fallback. Older hosts ignore the added fields.

Document decay uses only mtime: `max(0.5, min(1, 1 - age_days/30))`. Search hits do not update access history. Standalone numbers, times, and alphanumeric identifiers use boundary-aware literal matching in keyword/hybrid mode; thousands separators are interchangeable. Path weights apply after candidate retrieval and cannot recover documents absent from the candidate set.
