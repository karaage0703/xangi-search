[日本語](README.md) | English

# xangi-search

A local extension that searches a [xangi](https://github.com/karaage0703/xangi) workspace.
It combines full-text and semantic search to quickly find past notes and records.

## Features

- Search related files in a workspace
- Save and search durable facts as FACTs
- Configure search and automatic indexing from the Web UI
- Let xangi manage startup, shutdown, and updates

The index and settings are stored under `.xangi-search/` in the workspace.
Search runs locally and does not use an LLM.

## Setup

Clone the repository, then run:

```bash
uv sync --extra vector
xangi extension link ./xangi-extension.json
xangi extension start xangi-search
xangi extension doctor xangi-search
```

The first index may take some time to build. After `doctor` succeeds, open `xangi-search` from
the xangi Extensions screen.

See [XANGI_SETUP.en.md](XANGI_SETUP.en.md) for the full setup flow and lightweight keyword-only mode.

## Usage

- `検索` (Search): Enter keywords or describe what you want to find.
- `FACT`: Add or edit facts that should remain available over time.
- `設定` (Settings): Change the search mode, result count, directory weights, and automatic indexing.

AI agents can also use the bundled
[`xs-xangi-search` skill](skills/xs-xangi-search/SKILL.md).

## Documentation

- [Usage guide](docs/en/usage.md) - UI, CLI, FACTs, settings, and troubleshooting
- [Design document](docs/en/design.md) - Architecture, search pipeline, API, and persistence
- [Setup guide](XANGI_SETUP.en.md) - Installation as a xangi extension

## Development

```bash
uv sync --extra vector
uv run pytest
```
