[English](XANGI_SETUP.en.md) | [日本語](XANGI_SETUP.md)

# xangi-search setup

This repository is a local xangi extension. Run setup from the repository root.

## Set up the extension

1. Confirm that `uv` and `xangi` are available. Do not install system packages or use `sudo` automatically.
2. Create the local environment with `uv sync --extra vector`. The standard setup combines embedding and keyword retrieval in hybrid mode. Use plain `uv sync` with `XANGI_SEARCH_NO_VECTOR=true` for keyword-only mode only when the user explicitly requests a lightweight setup or the environment cannot support the additional model dependencies.
3. Register this checkout with `xangi extension link ./xangi-extension.json`.
4. Start it with `xangi extension start xangi-search`, targeting the xangi instance that owns this setup conversation. Never guess another instance. The parent runtime manager passes the xangi workspace; do not replace it with the extension repository path.
5. Confirm that `xangi extension list` reports `xangi-search` as `autostart`.
6. Confirm service startup with `xangi extension status xangi-search`, then monitor the initial background index until `xangi extension doctor xangi-search` succeeds. A successful `start` does not mean the initial index has finished.
7. Open the standalone UI with `Open` on xangi's Extensions page and verify incremental search, persisted settings including weights for existing directories after reload, manual reindex status, and that `Back to xangi` returns to the same environment's home page. The service also exposes `/ui` directly, where the back link is hidden because the xangi URL cannot be inferred safely.

The search index, optional embeddings, and search/automatic-index settings are extension-owned state under the workspace's `.xangi-search/`. The parent xangi keeps the child port and bearer token only at runtime. Multiple instances with different workspaces are isolated; do not configure multiple instances to update the same workspace concurrently. Do not delete an existing index during setup. Incremental indexing runs at startup and every 30 minutes by default; do not add a duplicate xangi schedule or OS cron job. If a command fails, report the exact command and error before changing configuration.

## Offer pre-search before the LLM runs

When xangi supports the `UserPromptSubmit` hook and `extension_request --query-json-stdin`, it can search once before sending the user input to the LLM and append an evidence pack capped at 12,000 characters. This uses xangi's generic command-hook mechanism; it does not add xangi-search-specific behavior to xangi core.

1. Confirm that `xangi tool help extension_request` shows `--query-json-stdin`. If it does not, do not configure the hook and report the required xangi version.
2. Confirm that `.venv/bin/xangi-search-preflight-hook` in this repository is executable.
3. Read the workspace's `hooks/hooks.json`. Preserve existing `Stop` and other `UserPromptSubmit` hooks, then show the following proposed addition to the user. Replace `<absolute-repository-path>` with the absolute path of this checkout.

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "id": "xangi-search-preflight",
        "exec": {
          "file": "<absolute-repository-path>/.venv/bin/xangi-search-preflight-hook",
          "args": []
        },
        "timeoutMs": 10000,
        "maxOutputChars": 14000
      }
    ]
  }
}
```

The hook receives only the current unexpanded user input as JSON on stdin and calls `/search` through the parent xangi's `extension_request`. It does not send conversation history, the system prompt, environment variables, or authentication tokens to xangi-search. Search failures, timeouts, and zero results add no context and allow the normal response to continue.

Before changing configuration, disclose that the command runs persistently on every turn, the exact executable, the data passed to it, the 10-second timeout, and the 14,000-character output limit. Offer these choices:

- Recommended: add the pre-search hook
- Use only the extension and skill; do not add the hook

Do not modify `hooks/hooks.json` until the user selects an option. If the same `id` already exists, do not add a duplicate. Show the diff and ask again if its executable or limits differ. When stopping or removing the extension, ask whether to disable or remove this hook too.

## Propose adding or updating the usage skill

After initial setup or an extension update, inspect the workspace conventions and propose adding or updating the bundled `skills/xs-xangi-search/SKILL.md` only when needed.

1. Check whether the workspace uses `skills/`, `.agents/skills/`, or `.claude/skills/`, and follow the existing convention. If none exists, propose `skills/xs-xangi-search/`.
2. If a skill with the same name exists, compare it with the bundled version. Propose an update only for material differences in APIs, operating procedures, or failure handling, and state the reason, target path, and change summary. Do not propose formatting-only changes or replace workspace guidance that is already equivalent or more complete.
3. If the workspace has an `AGENTS.md`, propose a minimal change only when an always-on rule is missing or stale. Preserve existing instructions and user-specific rules, and do not add duplicate guidance:

```markdown
## xangi-search

- Use the `xs-xangi-search` skill when an answer depends on past records or files in the workspace.
- After retrieving external information, search the workspace for the same topic when local context may matter.
- Do not conclude that no record exists from a single empty result; retry once with a shorter query or keyword mode.
- For durable facts worth remembering, search similar facts before deciding to ADD, UPDATE, or DELETE.
```

When an addition or update is warranted, offer these choices:

- Recommended: add or update the skill and apply the minimum necessary `AGENTS.md` change
- Add or update only the skill
- Keep the workspace unchanged and use only the extension

Approval of extension setup or update does not authorize workspace edits. Do not modify workspace skills or `AGENTS.md` until the user selects an option. After changes, report the affected paths, applied diff, and verification result. If no update is needed, state that no workspace change is recommended. Use the language of the current conversation for user-facing explanations.

## Verify fact usage

When the skill is installed, do not connect to a fixed child port. Verify the following flow through the parent xangi's `extension_request` command:

1. Search existing facts with `GET /facts/similar?q=...&k=3`.
2. Add a temporary fact with `POST /facts` and record the returned ID.
3. Update the same ID with `PUT /facts/{id}` and confirm the change with `GET /facts/{id}`.
4. Deactivate the same ID with `DELETE /facts/{id}` and confirm `is_active: 0` with `GET /facts/{id}`.
5. Restart the extension and confirm that normal facts persist. Deactivate the temporary fact after verification.

Facts are not substitutes for long logs or documents. Keep each entry to one durable fact, never store secrets, and include `source_file` and `fact_date` when available. Search for a similar fact before adding; update the existing ID when the same fact changes instead of creating a duplicate. Do not retrieve or record the child port or authentication token.
