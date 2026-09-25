"""Multi-provider LLM client with fallback chain (BYOK)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

# Retry-the-same-provider policy for 429s. Gemini's free tier is limited per
# minute, so the defaults sit inside one quota window; a Retry-After header,
# when present, overrides them (capped, so a hostile header can't stall a
# scheduler job for minutes).
_RATE_LIMIT_RETRIES = 2
_RATE_LIMIT_BACKOFF = [10.0, 30.0]
_RATE_LIMIT_MAX_WAIT = 60.0


def _retry_after_seconds(resp: httpx.Response, default: float) -> float:
    """Delay before retrying a 429: Retry-After if sane, else the default."""
    raw = (resp.headers.get("Retry-After") or "").strip()
    if raw.isdigit():
        return min(float(raw), _RATE_LIMIT_MAX_WAIT)
    return default

from .. import config, constants
from .copywriter import provenance as copy_provenance

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(30.0, connect=10.0, read=120.0)

# Gemini finish reasons that mean the text we got back is not a usable answer.
# STOP is the only clean one; MAX_TOKENS means the output was cut mid-sentence,
# which for a JSON-producing prompt yields an unparseable fragment.
_GEMINI_BAD_FINISH = {
    "SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT",
    "SPII", "MAX_TOKENS", "MALFORMED_FUNCTION_CALL",
}


# Current models reason before answering, and those tokens are billed against
# the same output cap as the answer. Measured: gemini-3.6-flash spent 96
# thinking tokens replying "ok". A caller asking for 200 tokens of message
# would otherwise get an empty MAX_TOKENS response, so callers state the
# answer length they want and this is added on top.
_THINKING_HEADROOM_TOKENS = 2048


def _output_budget(max_tokens: int) -> int:
    """Caller's desired answer length plus room for the model to think."""
    return max(int(max_tokens), 1) + _THINKING_HEADROOM_TOKENS


class LLMError(Exception):
    """Raised when all LLM providers fail."""


def _loads_defensively(raw: str) -> dict[str, Any] | None:
    """Parse a JSON object, tolerating a markdown fence around it.

    Providers enforce the schema server-side, so this should be a plain
    json.loads. The fence strip stays because a wrapped response is cheap to
    recover from and costs a whole extra round trip otherwise.
    """
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        body = text.split("```", 2)
        if len(body) >= 2:
            text = body[1].removeprefix("json").strip()
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def loads_json_object(raw: str, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    """Parse a JSON object from LLM output.

    For callers whose expected shape cannot be written as a strict schema
    (Pydantic-validated reasoning, the nested brand plan). Prefer
    ``LLMClient.generate_json`` wherever the shape can be stated — the provider
    then enforces it and this becomes a formality.

    The old regex repair pass is gone: it patched tics specific to
    gemini-2.0-flash (stray characters before property names, trailing commas),
    which is a retired model.
    """
    data = _loads_defensively(raw)
    if data is not None:
        return data
    if fallback is not None:
        logger.warning("JSON parse failed, using fallback")
        return fallback
    raise ValueError(f"Failed to parse JSON from LLM output: {(raw or '')[:200]}")


def resolve_model(provider: str, tier: str = constants.LLM_TIER_QUALITY) -> str:
    """The model to use for a provider at a given tier.

    Single source of truth for model ids — anything that needs one (including
    the browser agent, which builds its own LangChain client) should call this
    rather than writing an id inline, or the two copies drift apart.

    A configured model that the provider has retired is ignored: a config file
    written months ago would otherwise keep calling a dead endpoint long after
    the defaults here were updated.
    """
    cfg = config.load_config()
    configured = (cfg.get(f"{provider}_model") or "").strip()
    if configured and configured not in constants.RETIRED_LLM_MODELS:
        return configured
    default = constants.DEFAULT_LLM_MODELS[provider][tier]
    if configured:
        logger.warning(
            "Configured %s model %r has been retired — using %s instead. "
            "Update ~/.heylead/config.json to silence this.",
            provider, configured, default,
        )
    return default


class LLMClient:
    """LLM client that tries providers in priority order.

    Usage:
        client = LLMClient()
        result = await client.generate("Analyze this profile...", system="...")

    Pass ``tier=constants.LLM_TIER_FAST`` for classification and extraction
    work; the default quality tier is for anything a prospect will read.
    """

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        cfg = config.load_config()
        self.api_keys: dict[str, str] = cfg.get("api_keys", {})
        self.priority: list[str] = cfg.get("llm_priority", constants.DEFAULT_LLM_PRIORITY)
        self._configured_models: dict[str, str] = {
            "gemini": cfg.get("gemini_model", ""),
            "claude": cfg.get("claude_model", ""),
            "openai": cfg.get("openai_model", ""),
        }
        self._transport = transport

    def _model_for(self, provider: str, tier: str) -> str:
        """Pick the model for a provider/tier, honouring config overrides."""
        configured = (self._configured_models.get(provider) or "").strip()
        if configured and configured not in constants.RETIRED_LLM_MODELS:
            return configured
        default = constants.DEFAULT_LLM_MODELS[provider][tier]
        if configured:
            logger.warning(
                "Configured %s model %r has been retired — using %s instead. "
                "Update ~/.heylead/config.json to silence this.",
                provider, configured, default,
            )
        return default

    def _available_providers(self) -> list[str]:
        """Return providers that have API keys configured."""
        return [p for p in self.priority if self.api_keys.get(p)]

    def _http(self) -> httpx.AsyncClient:
        if self._transport is not None:
            return httpx.AsyncClient(transport=self._transport)
        return httpx.AsyncClient()

    async def generate(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.7,
        max_tokens: int = 2000,
        tier: str = constants.LLM_TIER_QUALITY,
        schema: dict[str, Any] | None = None,
    ) -> str:
        """Generate text using the first available LLM provider.

        Tries each provider in priority order. Returns the generated text.
        Raises LLMError if all providers fail — including when a provider
        answers 200 with empty, blocked or truncated output, which is a failure
        the next provider may well be able to serve.
        """
        async def attempt(provider: str) -> str:
            text = await self._call_provider(
                provider, prompt, system, temperature, max_tokens, tier, schema
            )
            copy_provenance.note_model(self._model_for(provider, tier))
            return text

        return await self._try_providers(attempt, tier)

    async def _try_providers(self, attempt, tier: str):
        """Run `attempt` against each provider until one succeeds.

        Anything `attempt` raises — a transport error, a blocked response, or
        output that failed to parse — moves on to the next provider. That last
        case is the point: unusable output is a failure the next provider may
        well be able to serve, not a result to hand back.
        """
        providers = self._available_providers()
        if not providers:
            raise LLMError(
                "No LLM API keys configured.\n\n"
                "Add at least one API key to ~/.heylead/config.json:\n"
                '  "api_keys": {\n'
                '    "gemini": "YOUR_KEY_HERE"\n'
                "  }\n\n"
                "Get a free Gemini key at: https://aistudio.google.com/apikey"
            )

        errors = []
        for provider in providers:
            for attempt_no in range(_RATE_LIMIT_RETRIES + 1):
                try:
                    logger.debug("Trying LLM provider: %s (tier=%s)", provider, tier)
                    return await attempt(provider)
                except httpx.HTTPStatusError as e:
                    # 429 is the one failure where retrying the SAME provider is
                    # right: the quota window resets on its own, and with a
                    # single key configured "move on to the next provider" means
                    # "fail". Anything else falls through to the next provider.
                    if (
                        e.response is not None
                        and e.response.status_code == 429
                        and attempt_no < _RATE_LIMIT_RETRIES
                    ):
                        delay = _retry_after_seconds(
                            e.response, default=_RATE_LIMIT_BACKOFF[attempt_no]
                        )
                        logger.warning(
                            "LLM provider %s rate-limited (429); retrying in %ss",
                            provider, delay,
                        )
                        await asyncio.sleep(delay)
                        continue
                    message = self._redact(str(e))
                    logger.warning("LLM provider %s failed: %s", provider, message)
                    errors.append(f"{provider}: {message}")
                    break
                except Exception as e:
                    message = self._redact(str(e))
                    logger.warning("LLM provider %s failed: %s", provider, message)
                    errors.append(f"{provider}: {message}")
                    break

        raise LLMError(
            "All LLM providers failed:\n" + "\n".join(f"  - {e}" for e in errors)
        )

    async def generate_json(
        self,
        prompt: str,
        schema: dict[str, Any],
        system: str = "",
        temperature: float = 0.7,
        max_tokens: int = 2000,
        tier: str = constants.LLM_TIER_QUALITY,
    ) -> dict[str, Any]:
        """Generate a JSON object, enforced by the provider against `schema`.

        Every provider validates server-side, so the old strip-fences-and-hope
        repair pass is no longer the primary mechanism — but enforcement is not
        something to trust blindly, so the response is still parsed defensively
        and checked against the schema's required keys.

        A response that cannot be parsed raises rather than being handed back as
        prose. That prose used to become the outgoing message.
        """
        async def attempt(provider: str) -> dict[str, Any]:
            raw = await self._call_provider(
                provider, prompt, system, temperature, max_tokens, tier, schema
            )
            data = _loads_defensively(raw)
            if data is None:
                raise LLMError(f"Expected JSON matching the schema, got: {raw[:200]!r}")
            missing = [k for k in schema.get("required", []) if k not in data]
            if missing:
                raise LLMError(f"JSON response is missing required key(s): {missing}")
            copy_provenance.note_model(self._model_for(provider, tier))
            return data

        return await self._try_providers(attempt, tier)

    def _redact(self, text: str) -> str:
        """Strip any API key that leaked into an error message."""
        for key in self.api_keys.values():
            if key:
                text = text.replace(key, "***")
        return text

    async def _call_provider(
        self,
        provider: str,
        prompt: str,
        system: str,
        temperature: float,
        max_tokens: int,
        tier: str,
        schema: dict[str, Any] | None = None,
    ) -> str:
        model = self._model_for(provider, tier)
        if provider == "gemini":
            return await self._call_gemini(
                model, prompt, system, temperature, max_tokens, schema
            )
        elif provider == "claude":
            return await self._call_claude(model, prompt, system, max_tokens, schema)
        elif provider == "openai":
            return await self._call_openai(model, prompt, system, max_tokens, schema)
        else:
            raise ValueError(f"Unknown LLM provider: {provider}")

    # ──────────────────────────────────────
    # Gemini
    # ──────────────────────────────────────

    async def _call_gemini(
        self, model: str, prompt: str, system: str, temperature: float,
        max_tokens: int, schema: dict[str, Any] | None = None,
    ) -> str:
        # The key goes in a header, not the query string — a URL ends up in log
        # lines and in the text of any raised HTTP error.
        headers = {
            "x-goog-api-key": self.api_keys["gemini"],
            "content-type": "application/json",
        }
        url = f"{constants.GEMINI_API_URL}/{model}:generateContent"

        body: dict[str, Any] = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": _output_budget(max_tokens),
            },
        }
        if schema:
            body["generationConfig"]["responseFormat"] = {
                "text": {"mimeType": "application/json", "schema": schema}
            }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}

        async with self._http() as client:
            resp = await client.post(url, headers=headers, json=body, timeout=_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()

        candidates = data.get("candidates", [])
        if not candidates:
            blocked = data.get("promptFeedback", {}).get("blockReason", "")
            suffix = f" (blocked: {blocked})" if blocked else ""
            raise LLMError(f"Gemini returned no candidates{suffix}")

        finish = (candidates[0].get("finishReason") or "").upper()
        if finish in _GEMINI_BAD_FINISH:
            raise LLMError(f"Gemini stopped early (finishReason={finish})")

        parts = candidates[0].get("content", {}).get("parts", [])
        text = parts[0].get("text", "") if parts else ""
        if not text.strip():
            raise LLMError(
                f"Gemini returned empty text (finishReason={finish or 'unknown'})"
            )
        return text

    # ──────────────────────────────────────
    # Claude (Anthropic)
    # ──────────────────────────────────────

    async def _call_claude(
        self, model: str, prompt: str, system: str, max_tokens: int,
        schema: dict[str, Any] | None = None,
    ) -> str:
        headers = {
            "x-api-key": self.api_keys["claude"],
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        # No temperature/top_p/top_k: current Claude models reject a non-default
        # sampling parameter with a 400. Steer these calls from the prompt.
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": _output_budget(max_tokens),
            "messages": [{"role": "user", "content": prompt}],
        }
        if schema:
            body["output_config"] = {
                "format": {"type": "json_schema", "schema": schema}
            }
        if system:
            body["system"] = system

        async with self._http() as client:
            resp = await client.post(
                constants.CLAUDE_API_URL, headers=headers, json=body, timeout=_TIMEOUT
            )
            resp.raise_for_status()
            data = resp.json()

        stop_reason = data.get("stop_reason", "")
        if stop_reason == "refusal":
            raise LLMError("Claude declined the request (stop_reason=refusal)")
        if stop_reason == "max_tokens":
            raise LLMError("Claude output was truncated (stop_reason=max_tokens)")

        text = ""
        for block in data.get("content", []):
            if block.get("type", "text") == "text":
                text = block.get("text", "")
                break
        if not text.strip():
            raise LLMError(
                f"Claude returned empty text (stop_reason={stop_reason or 'unknown'})"
            )
        return text

    # ──────────────────────────────────────
    # OpenAI
    # ──────────────────────────────────────

    async def _call_openai(
        self, model: str, prompt: str, system: str, max_tokens: int,
        schema: dict[str, Any] | None = None,
    ) -> str:
        headers = {
            "Authorization": f"Bearer {self.api_keys['openai']}",
            "Content-Type": "application/json",
        }
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        # `max_tokens` is deprecated in favour of `max_completion_tokens`, and
        # current models reject a non-default temperature.
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_completion_tokens": _output_budget(max_tokens),
        }
        if schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": schema, "strict": True},
            }

        async with self._http() as client:
            resp = await client.post(
                constants.OPENAI_API_URL, headers=headers, json=body, timeout=_TIMEOUT
            )
            resp.raise_for_status()
            data = resp.json()

        choices = data.get("choices", [])
        if not choices:
            raise LLMError("OpenAI returned no choices")

        finish = choices[0].get("finish_reason", "")
        if finish == "length":
            raise LLMError("OpenAI output was truncated (finish_reason=length)")
        if finish == "content_filter":
            raise LLMError("OpenAI blocked the request (finish_reason=content_filter)")

        text = choices[0].get("message", {}).get("content") or ""
        if not text.strip():
            raise LLMError(
                f"OpenAI returned empty text (finish_reason={finish or 'unknown'})"
            )
        return text
