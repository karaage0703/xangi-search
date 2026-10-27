[日本語](../usage.md) | English

# Usage Guide

This guide covers installation, search, FACT management, and settings for xangi-search.

## Use as a xangi extension

Run these commands from the repository root:

```bash
uv sync --extra vector
xangi extension link ./xangi-extension.json
xangi extension start xangi-search
xangi extension doctor xangi-search
```

The process starts its HTTP listener first, then loads the embedding model and index in the
background. A successful `xangi extension start` means that HTTP startup is complete. Confirm
search readiness with `xangi extension doctor xangi-search` or `ready: true` from `GET /health`.
Then open `xangi-search` from the xangi Extensions screen. See the
[setup guide](../../XANGI_SETUP.en.md) for the full flow.

## Web UI

### Search

Enter keywords or a description in the Search (`検索`) tab to find related files. `Hybrid` is the
recommended mode for normal use.

- `Hybrid`: combines semantic and keyword search
- `Vector`: searches by semantic similarity
- `Keyword`: focuses on matching text

### FACT

The `FACT` tab lets you add, edit, and deactivate facts that should remain available over time.
FACTs are also used in normal file search. Keep each entry focused on one fact instead of storing
long logs or whole documents.
The API can retrieve one entry with `GET /facts/{id}`, including inactive FACTs marked with
`is_active: 0`.

### Settings

Open Settings (`設定`) to change these per-workspace values:

- search mode, result count, and minimum score
- automatic reindexing and its interval
- per-directory search weights
- optional time decay
- the Markdown snapshot path for FACTs

The index and settings are stored under `.xangi-search/` in the workspace. By default, changed
files are reindexed at startup and every 30 minutes.

## Use from an AI agent

The bundled [`xs-xangi-search` skill](../../skills/xs-xangi-search/SKILL.md) provides procedures
for search and FACT management. During initial setup and after an extension update, the agent
compares the bundled guidance with the workspace and proposes only material differences. It does
not add the skill or edit `AGENTS.md` until the user chooses to do so.

## Use the standalone CLI

xangi-search can also run directly from the command line:

```bash
# Build the index
uv run xangi-search --workspace /path/to/workspace index

# Search
uv run xangi-search --workspace /path/to/workspace search "query"

# Start the Web UI (default: http://127.0.0.1:7891/ui)
uv run xangi-search --workspace /path/to/workspace serve
```

For a lightweight setup without vector dependencies, use `uv sync` and add `--no-vector` to each
command. Search then runs in keyword mode.

## Troubleshooting

### The first index does not finish

Run `xangi extension doctor xangi-search` and inspect `initialization_phase` and
`initialization_error`. Initialization may take longer than 30 seconds on a slower CPU, a cold
cache, or a large index without stopping the process. Search requests return HTTP 503 with
`Retry-After: 2` while initialization is in progress. A successful `start` does not mean the
initial index is complete.

When an existing index is available, it remains `ready: true` and searchable during startup
refresh. If refresh fails, the existing index remains available and health reports `degraded: true`
with the failure in `last_reindex_error`.

### Semantic search is unavailable

Run `uv sync --extra vector`, then restart the extension. Without vector dependencies, the service
automatically falls back to keyword search.

### Search returns too few results

Try a shorter query, switch to `Keyword`, lower the minimum score, and check the directory weights.

### Reset settings

Settings and the index live under `.xangi-search/`. Removing it causes the data to be rebuilt, so
review any FACTs and settings you need before doing so.

### Exclude workspace paths

Defaults are used only when the settings file is absent. Invalid JSON, read errors, or invalid setting values stop startup and CLI indexing with an error identifying the file and cause. Processing never continues with custom exclusions silently cleared. Correct the file and retry. Invalid API updates are rejected without changing the current settings or saved file.

Use `exclude_paths` in `PUT /settings` to exclude literal workspace-relative files or directories.
The default is an empty array; built-in exclusions remain active. Globs, absolute paths and `..` are rejected.
A directory excludes its descendants, not sibling directories with a similar name.

```json
{"exclude_paths": ["output", ".cache", "logs/monitor-activity", "logs/tool-trajectory"]}
```

Saving exclusions or opening an existing index immediately removes excluded text, FTS entries and vectors, even with auto reindex disabled. Source files and FACTs are retained.
SQLite does not automatically shrink the physical database file.
To restore paths, remove them from the list and reindex. Settings persist in `.xangi-search/settings.json`
and also apply to CLI indexing. Edit this setting in the settings dialog (one relative path per line) or through the API.

Environment files (`.env`, `.env.*`, `*.env`, case-insensitive at any depth) are always excluded from indexing and both grep implementations. Path weights of zero are not exclusions: candidate selection happens before weighting. Use `exclude_paths` to keep logs from consuming candidates. Logical deletion does not guarantee forensic erasure from database pages, backups or previously copied logs.

### Startup ordering

Supporting xangi hosts serialize initialization with other members of the gpu group. For CPU-only operation, set `"startup": null` on this extension's record in the host extensions.json. For separate GPUs use distinct exclusiveGroup labels and configure actual model devices separately. Hosts without this feature keep the previous startup behavior.

### Read-only API mode

Set `XANGI_SEARCH_READ_ONLY=true` in the server process environment to reject settings changes,
FACT additions/edits/deletions, and manual reindex requests with HTTP 403. It is disabled by default.
GET requests, `POST /agent`, startup indexing, and periodic incremental indexing continue to work.
The UI displays settings and FACTs with mutation controls disabled. `GET /settings` and
`GET /health` expose `read_only`. The API cannot disable this mode: change the launch environment
and restart the process. This restricts HTTP mutations, not filesystem permissions or local CLI writes.

### Migrating FACT snapshots

Snapshots must be Markdown files under `.xangi-search/`; the default is `.xangi-search/facts.md`.
Existing files without the generated header and symlinks are never overwritten.
An old setting outside this directory is mapped to the new default at startup, preserving other
search settings. The settings file is updated on the next settings save; the snapshot is written
on the next FACT mutation. Existing SQLite FACTs are retained. If the database is empty, the old
default snapshot or old configured snapshot is imported. Old files are not modified or deleted.
Include `.xangi-search/facts.md` and the SQLite database in your backups.

## Checking retrieval quality

With decay enabled, fresh files receive a multiplier of 1; files at least 15 days old receive the floor of 0.5. Missing mtime is neutral. Search hits no longer reinforce documents. Standalone values such as `30,958`, `30958`, and `00:48:48` can be searched literally.

Before tuning, fix queries and expected source documents for current facts, historical decisions, books, and numbers. Compare actual API retrieval through final ranking; reranking a fixed candidate pool is only supplementary. If a source is absent, inspect indexing and missing field names. Summaries should retain metric names, units, and observation dates. Store raw evaluation JSON outside the indexed workspace to prevent search-result copies from becoming sources.

## Optional pre-search (Preflight)

Use the extension and skill to search on demand by default. Preflight optionally searches before each LLM turn and adds evidence candidates. It is neither recommended nor required; configure it in the workspace’s `hooks/hooks.json` only when requested. It adds latency and context on every turn and may provide little benefit for short acknowledgements. Updates do not automatically enable or disable existing hooks. See the [optional setup instructions](../../XANGI_SETUP.en.md#optional-pre-search-before-the-llm-runs-preflight).
