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
