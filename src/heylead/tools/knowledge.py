"""Tool: knowledge — curate the hosted retrieval corpus that grounds messages.

The backend keeps four kinds of source: `upload` (documents added here),
`website` (crawled pages from the account's own site), `campaign` (campaign
context and offerings) and `reply_exemplar` (replies that worked). Message
generation quotes them, so what is in here decides what the agent is allowed
to claim. This tool is the only surface that can see or change it.

Hosted only: the corpus lives in the workspace, not on this machine.
"""

from __future__ import annotations

from typing import Any

from ..config import is_backend_mode
from ..linkedin import UnipileError, get_linkedin_client


ACTIONS = ("list", "add", "remove", "refresh", "search")
SCOPES = ("all", "website", "campaigns", "exemplars")

NOT_HOSTED = (
    "The knowledge base is a hosted HeyLead feature — the corpus lives in your "
    "workspace, not on this machine. Sign in with setup_profile first."
)


def _parse_kinds(kinds: str) -> list[str]:
    """A comma-separated string from the model into a clean list."""
    return [k.strip() for k in (kinds or "").split(",") if k.strip()]


def _collapse(value: Any) -> str:
    """One line, whatever the backend stored.

    Titles come from crawled <title> tags and uploaded documents, and error
    strings from crawlers — both carry newlines that would break the one
    source per line shape of the list.
    """
    return " ".join(str(value or "").split())


def _source_line(src: dict[str, Any]) -> str:
    kind = src.get("source_type") or "unknown"
    title = _collapse(
        src.get("title") or src.get("source_uri") or src.get("id") or "(untitled)"
    )
    chunks = src.get("chunk_count")
    chunks = 0 if chunks is None else chunks
    status = src.get("embed_status") or "unknown"
    line = f"  {src.get('id')} | {kind} | {title} | {chunks} chunks | {status}"
    error = _collapse(src.get("error"))
    if error:
        line += f" | {error}"
    return line


def _render_list(data: dict[str, Any], kind: str) -> str:
    sources = data.get("sources") or []
    totals = data.get("totals") or {}
    scope = f" ({kind})" if kind else ""
    if not sources:
        head = f"No knowledge sources{scope} yet."
    else:
        head = f"Knowledge sources{scope}: {len(sources)}\n"
    lines = [head]
    lines.extend(_source_line(s) for s in sources)
    if totals:
        summary = ", ".join(f"{k}: {v}" for k, v in sorted(totals.items()))
        lines.append(f"\nTotals — {summary}")
    return "\n".join(lines)


def _render_evidence(evidence: list[dict[str, Any]], query: str) -> str:
    if not evidence:
        return f"No knowledge matched '{query}'."
    lines = [f"Evidence for '{query}' ({len(evidence)}):\n"]
    for n, chunk in enumerate(evidence, start=1):
        kind = chunk.get("source_type") or "unknown"
        title = chunk.get("source_title") or chunk.get("source_id") or "(untitled)"
        header = chunk.get("header_path") or ""
        try:
            score = f"{float(chunk.get('score') or 0.0):.2f}"
        except (TypeError, ValueError):
            score = "?"
        where = f"{title} > {header}" if header else title
        lines.append(f"  {n}. [{kind}] {where} (score {score})")
        # The backend delivers chunks as plain text — already stripped of
        # markup at ingest. Nothing here parses or sanitises; this only
        # reflows the whitespace so a chunk occupies one line.
        text = " ".join(str(chunk.get("text") or "").split())
        if len(text) > 400:
            text = text[:400].rstrip() + "…"
        lines.append(f"     {text}")
        uri = chunk.get("source_uri")
        if uri:
            lines.append(f"     {uri}")
    return "\n".join(lines)


def _render_refresh(data: dict[str, Any], scope: str) -> str:
    status = str(data.get("status") or "").strip()
    if status == "already_queued":
        return f"Knowledge refresh ({scope}) is already queued — nothing new started."
    if status == "queued":
        job = data.get("job_id")
        tail = f" (job {job})" if job else ""
        return f"Knowledge refresh ({scope}) queued{tail}."
    if status == "done":
        summary = data.get("summary")
        return f"Knowledge refresh ({scope}) done. {summary}".rstrip()
    return f"Knowledge refresh ({scope}): {status or data}"


async def run_knowledge(
    action: str = "list",
    title: str = "",
    text: str = "",
    source_uri: str = "",
    source_id: str = "",
    scope: str = "all",
    campaign_id: str = "",
    query: str = "",
    kinds: str = "",
    top_k: int = 6,
    sync: bool = False,
) -> str:
    action = (action or "list").lower().strip()
    # The backend clamps too, but a nonsense top_k should not cost a round
    # trip — and a 0 would quietly retrieve nothing.
    top_k = max(1, min(50, int(top_k) if top_k is not None else 6))

    if not is_backend_mode():
        return NOT_HOSTED

    if action not in ACTIONS:
        return f"Unknown action '{action}'. Use one of: {', '.join(ACTIONS)}."

    # Argument checks run before the client so an incomplete call costs nothing.
    if action == "add" and not text.strip():
        return "Provide text to upload. knowledge(action='add', title=..., text=...)."
    if action == "remove" and not source_id.strip():
        return "Provide source_id. Run knowledge(action='list') to find it."
    if action == "search" and not query.strip():
        return "Provide a query. knowledge(action='search', query='pricing')."
    if action == "refresh" and scope not in SCOPES:
        return f"Unknown scope '{scope}'. Use one of: {', '.join(SCOPES)}."

    client = get_linkedin_client()
    parsed_kinds = _parse_kinds(kinds)

    try:
        if action == "list":
            kind = parsed_kinds[0] if parsed_kinds else ""
            return _render_list(await client.knowledge_list(kind=kind), kind)

        if action == "add":
            src = await client.knowledge_add(
                title=title or "(untitled)", text=text, source_uri=source_uri,
            )
            return (
                f"Added {src.get('title')} ({src.get('source_type')}) as "
                f"{src.get('id')} — {src.get('chunk_count', 0)} chunks, "
                f"{src.get('embed_status')}."
            )

        if action == "remove":
            await client.knowledge_remove(source_id)
            return f"Removed knowledge source {source_id}."

        if action == "refresh":
            data = await client.knowledge_refresh(
                scope=scope, campaign_id=campaign_id, sync=sync,
            )
            return _render_refresh(data, scope)

        evidence = await client.knowledge_search(
            query=query,
            kinds=parsed_kinds or None,
            top_k=top_k,
            campaign_id=campaign_id,
        )
        return _render_evidence(evidence, query)
    except UnipileError as e:
        return f"Knowledge {action} failed: {e}"
