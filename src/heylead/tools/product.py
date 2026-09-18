"""Tool: product — local-only product/code agent. Never sends LinkedIn."""

from __future__ import annotations

import logging

from ..db.async_bridge import run_db
from ..services.agent_commons import list_beats
from ..services.product_agent import (
    maybe_run_product_agent,
    product_agent_mode,
    resolve_product_repo,
)

logger = logging.getLogger(__name__)


async def run_product(action: str = "status", request: str = "") -> str:
    """Dispatch product(action='tick'|'status')."""
    verb = (action or "status").strip().lower()
    if verb == "status":
        return await _status()
    if verb != "tick":
        return "Unknown product action. Use action='tick' or action='status'."
    if not (request or "").strip():
        return "product(action='tick') needs a request describing the change."
    outcome = await maybe_run_product_agent(request.strip())
    parts = [outcome.summary or outcome.decision]
    if outcome.reason and outcome.reason not in parts[0]:
        parts.append(outcome.reason)
    return " — ".join(p for p in parts if p)


async def _status() -> str:
    repo = resolve_product_repo()
    mode = product_agent_mode(repo=repo)
    gate = f"repo: {repo}" if repo else "repo: (none — gate closed)"
    beats = await run_db(list_beats, campaign_id="")
    product = next((b for b in beats if str(b.get("agent") or "") == "product"), None)
    last = "no product beat"
    if product:
        last = (
            f"last: {product.get('decision') or '?'} — "
            f"{(product.get('reason') or '')[:160]}"
        )
    return f"product mode: {mode}\n{gate}\n{last}"
