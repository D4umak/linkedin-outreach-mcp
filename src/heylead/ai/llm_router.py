"""Single-prompt LLM entry point that respects the hosted/self-hosted split.

Backend-mode users have no local provider key, so the call has to go through
the backend's generic brand-generate endpoint; everyone else calls their own
provider directly. Callers that ask for json_mode get a string that json.loads
accepts — providers wrap JSON in prose or code fences often enough that
handing the raw text back makes every caller repeat the same repair.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def call_llm(
    prompt: str,
    system: str = "",
    temperature: float = 0.7,
    max_tokens: int = 2000,
    json_mode: bool = False,
    schema: dict[str, Any] | None = None,
    tier: str = "quality",
) -> str:
    """Run one prompt through whichever LLM path this install is configured for."""
    if json_mode and "JSON" not in prompt.upper():
        prompt = f"{prompt}\n\nReturn ONLY valid JSON, no prose and no code fences."

    from ..config import has_local_llm_key, is_backend_mode

    if is_backend_mode() and not has_local_llm_key():
        from ..linkedin import get_linkedin_client

        client = get_linkedin_client()
        try:
            raw = await client.brand_generate(
                prompt, system=system, temperature=temperature, max_tokens=max_tokens,
            )
        finally:
            await client.close()
    else:
        from .llm import LLMClient

        raw = await LLMClient().generate(
            prompt,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            schema=schema,
            tier=tier,
        )

    if json_mode:
        from .llm import loads_json_object

        return json.dumps(loads_json_object(raw or ""))
    return raw or ""
