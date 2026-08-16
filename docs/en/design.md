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
semantic search with SQLite FTS5 keyword search. When vector dependencies are unavailable, the
service falls back to Keyword mode.

A bounded grep fallback supplements searches with too few results. Directory weights, a minimum
score, and optional time decay are applied to the final ranking. The embedding matrix is cached in
memory instead of being read from SQLite for every query.

Incremental indexing runs at startup and at the configured interval. Once the `SearchIndex` is
attached, an existing index becomes searchable while the startup refresh continues in the
background.

## FACTs

FACTs are structured facts stored in SQLite. Each add, update, or deactivation writes a Markdown
snapshot. The default output path is `knowledge/rag_facts.md`.

The snapshot must be a workspace-relative `.md` path. Absolute paths, locations outside the
workspace, `.git/`, and `.xangi-search/` are rejected.

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
| `GET` | `/search` | File search with `q`, `mode`, `k`, `s`, `forgetting`, and `r2ag` |
| `GET` | `/file` | Read a small text file inside the workspace |
| `POST` | `/agent` | xangi agent backend contract |
| `GET` / `PUT` | `/settings` | Read or update settings |
| `POST` | `/reindex` | Start incremental indexing asynchronously |
| `GET` / `POST` | `/facts` | List or add FACTs |
| `GET` | `/facts/similar` | Find similar FACTs |
| `PUT` / `DELETE` | `/facts/{id}` | Update or deactivate a FACT |

Regular service responses use `schema_version: 1`, while `/agent` uses the xangi contract field
`schemaVersion: 1`. `POST /reindex` returns HTTP 202 when accepted; clients use `GET /health` to
observe completion.

## Updates

Repository-managed builds declare `./scripts/prepare-update` through `update.prepare` in the
manifest. The preparation program normally runs `uv sync --frozen --extra vector`, or
`uv sync --frozen` for keyword-only setups. xangi owns source switching, startup verification,
doctor checks, and rollback after a failed update.
