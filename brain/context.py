"""Assemble a token-budgeted, provenance-aware context pack from the shared brain.

This is the read API that makes one memory usable by many agents: instead of
dumping the whole store, an agent asks for a topic and a token budget and gets the
highest-value memory items, grouped by type and cited back to their notes/agents.
"""

from __future__ import annotations

from .util import estimate_tokens, jaccard, normalize_for_fingerprint, now_iso, truncate

TYPE_LABELS = {
    "decision": "决策",
    "task": "行动项",
    "summary": "摘要",
    "point": "要点",
    "question": "问题",
    "quote": "原话",
    "fact": "事实",
    "note": "记录",
}
TYPE_PRIORITY = ["decision", "task", "summary", "point", "question", "quote", "fact", "note"]


def _text_terms(text: str) -> set[str]:
    normalized = normalize_for_fingerprint(text)
    if len(normalized) < 2:
        return {normalized} if normalized else set()
    return {normalized[index : index + 2] for index in range(len(normalized) - 1)}


def _dedupe_items(items: list[dict]) -> list[dict]:
    kept: list[dict] = []
    kept_terms: list[set[str]] = []
    for item in items:
        terms = _text_terms(item.get("text", ""))
        if any(jaccard(terms, existing) >= 0.85 for existing in kept_terms):
            continue
        kept.append(item)
        kept_terms.append(terms)
    return kept


def _agent_list(item: dict) -> list[str]:
    agents: list[str] = []
    for contribution in item.get("contributors") or []:
        agent_id = contribution.get("agent_id")
        if agent_id and agent_id not in agents:
            agents.append(agent_id)
    return agents


def render_context_line(item: dict) -> str:
    label = TYPE_LABELS.get(item.get("type", ""), item.get("type", "记忆"))
    extras: list[str] = []
    meta = item.get("meta") or {}
    if item.get("type") == "task":
        if meta.get("owner"):
            extras.append(f"负责人：{meta['owner']}")
        if meta.get("due"):
            extras.append(f"截止：{meta['due']}")
        if item.get("status") == "done":
            extras.append("已完成")
    agents = _agent_list(item)
    if agents:
        shown = "、".join(agents[:3])
        if len(agents) > 3:
            shown += f" 等{len(agents)}个"
        extras.append(f"agent：{shown}")
    if item.get("note_title"):
        extras.append(f"《{truncate(item['note_title'], 16)}》")
    citation = "（" + "｜".join([label] + extras) + "）"
    bullet = "- [ ] " if item.get("type") == "task" and item.get("status") != "done" else "- "
    return f"{bullet}{item.get('text', '')} {citation}"


def _group_items(items: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for item in items:
        groups.setdefault(item.get("type", "fact"), []).append(item)
    for values in groups.values():
        values.sort(
            key=lambda item: (-float(item.get("score", 0.0)), item.get("updated_at", "")),
            reverse=False,
        )
    return groups


def build_context_pack(
    store,
    query: str = "",
    *,
    budget_tokens: int = 800,
    limit: int = 40,
    agent_id: str | None = None,
) -> dict:
    """Return a compact Markdown + structured context pack for one query."""
    query = (query or "").strip()
    budget_tokens = max(120, int(budget_tokens))
    limit = max(1, min(int(limit), 100))

    if query:
        candidates = store.search(query, limit=limit, agent_id=agent_id)
    elif agent_id:
        candidates = store.list_items(agent_id=agent_id, limit=limit)
    else:
        candidates = store.recent_items(limit=limit)

    candidates = _dedupe_items(candidates)
    groups = _group_items(candidates)
    ordered_types = [kind for kind in TYPE_PRIORITY if groups.get(kind)] + [
        kind for kind in groups if kind not in TYPE_PRIORITY
    ]

    header_lines = ["# 共享记忆上下文", ""]
    if query:
        header_lines.append(f"> 查询：`{query}`")
    else:
        header_lines.append("> 范围：最近更新的共享记忆")
    header_lines.append("")
    header = "\n".join(header_lines)

    selected: dict[str, list[dict]] = {kind: [] for kind in ordered_types}
    selected_ids: set[str] = set()
    used = estimate_tokens(header) + 70  # reserve for the meta line and section headings
    truncated = False
    skipped_for_budget = 0

    def try_add(item: dict) -> bool:
        nonlocal used
        if item.get("id") in selected_ids:
            return True
        if item.get("status") == "done":
            return False
        line = render_context_line(item)
        cost = estimate_tokens(line) + 1
        kind = item.get("type", "fact")
        if not selected.get(kind):
            cost += estimate_tokens(f"## {TYPE_LABELS.get(kind, kind)}") + 2
        if used + cost > budget_tokens:
            return False
        selected.setdefault(item.get("type", "fact"), []).append(item)
        selected_ids.add(item.get("id"))
        used += cost
        return True

    # Pass 1: guarantee one high-value item of each available type.
    for kind in ordered_types:
        if groups[kind]:
            try_add(groups[kind][0])
    # Pass 2: fill the remaining budget in priority order.
    for kind in ordered_types:
        for item in groups[kind][1:]:
            if item.get("id") in selected_ids:
                continue
            if not try_add(item):
                if item.get("status") != "done":
                    skipped_for_budget += 1
                    truncated = True

    # Degenerate case: one item larger than the whole budget.
    if not selected_ids and candidates:
        fallback = dict(candidates[0])
        fallback["text"] = truncate(str(fallback.get("text", "")), max(24, budget_tokens // 2))
        selected.setdefault(fallback.get("type", "fact"), []).append(fallback)
        selected_ids.add(fallback.get("id"))
        truncated = True

    def assemble() -> tuple[str, list[dict], list[dict]]:
        body: list[str] = []
        chosen: list[dict] = []
        referenced: list[dict] = []
        for kind in ordered_types:
            values = selected.get(kind) or []
            if not values:
                continue
            body.extend([f"## {TYPE_LABELS.get(kind, kind)}", ""])
            for item in values:
                body.append(render_context_line(item))
                chosen.append(item)
                if item.get("note_id") and item.get("note_title"):
                    if not any(note["id"] == item["note_id"] for note in referenced):
                        referenced.append({"id": item["note_id"], "title": item["note_title"]})
            body.append("")
        text = header
        if chosen:
            coverage = " · ".join(
                f"{TYPE_LABELS.get(kind, kind)} {len(selected.get(kind) or [])}"
                for kind in ordered_types
                if selected.get(kind)
            )
            text += (
                f"> 预算 {budget_tokens} tokens · 实际约 {estimate_tokens(header + chr(10).join(body))} tokens"
                f" · 命中 {len(chosen)} 条"
                + (f" · {coverage}" if coverage else "")
                + "\n\n"
            )
            text += "\n".join(body).rstrip() + "\n"
        else:
            text += "> 没有命中任何记忆。可以先用 `brain ingest` 或 `brain remember` 写入内容。\n"
        return text, chosen, referenced

    markdown, selected_items, notes = assemble()
    # Hard-trim the lowest-priority tail until the token budget actually holds.
    while selected_items and estimate_tokens(markdown) > budget_tokens:
        removed = selected_items[-1]
        for values in selected.values():
            if any(item.get("id") == removed.get("id") for item in values):
                values[:] = [item for item in values if item.get("id") != removed.get("id")]
                break
        selected_ids.discard(removed.get("id"))
        truncated = True
        markdown, selected_items, notes = assemble()

    used_tokens = estimate_tokens(markdown)
    if used_tokens > budget_tokens:
        truncated = True

    counts_by_type = {kind: len(values) for kind, values in selected.items() if values}
    return {
        "query": query,
        "budget_tokens": budget_tokens,
        "used_tokens": used_tokens,
        "truncated": truncated,
        "skipped_for_budget": skipped_for_budget,
        "item_count": len(selected_items),
        "counts_by_type": counts_by_type,
        "sections": {kind: values for kind, values in selected.items() if values},
        "markdown": markdown,
        "notes": notes,
        "generated_at": now_iso(),
    }
