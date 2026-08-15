[English](XANGI_SETUP.en.md) | [日本語](XANGI_SETUP.md)

# xangi-search setup

This repository is a local xangi extension. Run setup from the repository root.

## Set up the extension

1. Confirm that `uv` and `xangi` are available. Do not install system packages or use `sudo` automatically.
2. Create the local environment with `uv sync --extra vector`. The standard setup combines embedding and keyword retrieval in hybrid mode. Use plain `uv sync` with `XANGI_SEARCH_NO_VECTOR=true` for keyword-only mode only when the user explicitly requests a lightweight setup or the environment cannot support the additional model dependencies.
3. Register this checkout with `xangi extension link ./xangi-extension.json`.
4. Start it with `xangi extension start xangi-search`. The xangi workspace is passed to the extension lifecycle automatically; do not replace it with the extension repository path.
5. Confirm that `xangi extension list` reports `xangi-search` as `autostart`.
6. Confirm service startup with `xangi extension status xangi-search`, then monitor the initial background index until `xangi extension doctor xangi-search` succeeds. A successful `start` does not mean the initial index has finished.
7. Open the standalone UI with `Open` on xangi's Extensions page and verify incremental search, persisted settings including weights for existing directories after reload, manual reindex status, and that `Back to xangi` returns to the same environment's home page. The service also exposes `/ui` directly, where the back link is hidden because the xangi URL cannot be inferred safely.

The search index, optional embeddings, and search/automatic-index settings are extension-owned state. Do not delete an existing index during setup. Incremental indexing runs at startup and every 30 minutes by default; do not add a duplicate xangi schedule or OS cron job. If a command fails, report the exact command and error before changing configuration.

## Propose the usage skill

After verifying the extension, inspect the workspace conventions and propose installing the bundled `skills/xs-xangi-search/SKILL.md`.

1. Check whether the workspace uses `skills/`, `.agents/skills/`, or `.claude/skills/`, and follow the existing convention. If none exists, propose `skills/xs-xangi-search/`.
2. If a skill with the same name exists, show the difference instead of overwriting it.
3. If the workspace has an `AGENTS.md`, read it and propose only the non-duplicated parts of this minimal policy:

```markdown
## xangi search

- Use the `xs-xangi-search` skill when an answer depends on past records or files in the workspace.
- After retrieving external information, search the workspace for the same topic when local context may matter.
- Do not conclude that no record exists from a single empty result; retry once with a shorter query or keyword mode.
```

Offer these choices:

- Recommended: install the skill and add the minimal `AGENTS.md` policy
- Install only the skill
- Keep the workspace unchanged and use only the extension

Do not modify workspace skills or `AGENTS.md` until the user selects an option. After changes, report the affected paths and the applied diff. Use the language of the current conversation for user-facing explanations.
