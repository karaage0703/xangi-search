from __future__ import annotations

PREFLIGHT_LIMIT = 4
PREFLIGHT_FETCH_LIMIT = 6
PREFLIGHT_CONTEXT_CHUNKS = 2
PREFLIGHT_CONTEXT_RESULTS = 4
PREFLIGHT_MAX_CHARS = 12_000


def preflight_query_parameters(query: str, mode: str = "hybrid") -> dict[str, object]:
    if mode not in {"keyword", "hybrid"}:
        raise ValueError(f"unsupported preflight mode: {mode}")
    return {
        "q": query,
        "mode": mode,
        "k": PREFLIGHT_FETCH_LIMIT,
        "s": 0.0,
        "context_chunks": PREFLIGHT_CONTEXT_CHUNKS,
        "context_results": PREFLIGHT_CONTEXT_RESULTS,
    }


def preflight_results(payload: dict[str, object]) -> list[dict[str, object]]:
    raw_results = payload.get("results", [])
    if not isinstance(raw_results, list):
        return []
    unique: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in raw_results:
        if not isinstance(item, dict) or not isinstance(item.get("file_path"), str):
            continue
        path = str(item["file_path"])
        canonical = (
            path.removeprefix(".agents/")
            if path.startswith(".agents/skills/") and path.endswith("/SKILL.md")
            else path
        )
        if canonical in seen:
            continue
        seen.add(canonical)
        unique.append(item)
    preferred = [
        *[item for item in unique if not str(item["file_path"]).endswith("/SKILL.md")],
        *[item for item in unique if str(item["file_path"]).endswith("/SKILL.md")],
    ]
    return preferred[:PREFLIGHT_LIMIT]


def preflight_candidate_paths(payload: dict[str, object]) -> list[str]:
    return [str(item["file_path"]) for item in preflight_results(payload)]


def _compact_content(item: dict[str, object], *, include_context: bool) -> str:
    chunks: object = [item]
    context = item.get("context")
    if include_context and isinstance(context, dict):
        chunks = context.get("chunks", [item])
    if not isinstance(chunks, list):
        chunks = [item]
    contents = [
        " ".join(str(chunk.get("content", "")).split())
        for chunk in chunks
        if isinstance(chunk, dict) and chunk.get("content")
    ]
    return " ".join(contents)


def format_preflight_pack(
    payload: dict[str, object], max_chars: int = PREFLIGHT_MAX_CHARS
) -> str:
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    results = preflight_results(payload)
    if not results:
        return "検索結果なし"[:max_chars]

    headers = [
        f"[{rank}] {item.get('file_path')} chunk={item.get('chunk_index')}"
        for rank, item in enumerate(results, 1)
    ]
    prefix = "根拠候補:\n" + "\n".join(headers) + "\n候補別抜粋:\n"
    if len(prefix) >= max_chars:
        return prefix[:max_chars]

    remaining = max_chars - len(prefix)
    blocks: list[str] = []
    for rank, item in enumerate(results, 1):
        label = f"[{rank}] "
        groups_left = len(results) - rank + 1
        allowance = max(0, remaining // groups_left)
        content = _compact_content(item, include_context=True)
        clipped = content[: max(0, allowance - len(label) - 1)]
        block = f"{label}{clipped}\n"
        blocks.append(block)
        remaining -= len(block)
    return (prefix + "".join(blocks))[:max_chars]
