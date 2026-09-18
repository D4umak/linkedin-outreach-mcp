"""Hume AI TTS HTTP client.

Calls Hume's Text-to-Speech API (POST /v0/tts) to generate audio from text
using personalized voice parameters.

Hume's API uses natural language 'description' for voice personality and style
rather than numerical sliders. Our voice config's acting_instructions maps
directly to the description field.

API Reference: https://dev.hume.ai/reference/text-to-speech-tts/synthesize-json
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import httpx

from ..constants import HUME_TTS_API_URL

logger = logging.getLogger(__name__)


class HumeError(Exception):
    """Base error for Hume AI API calls."""


class HumeRateLimitError(HumeError):
    """Hume API rate limit reached."""


class HumeAuthError(HumeError):
    """Hume API authentication failed."""


class HumeClient:
    """HTTP client for Hume AI's TTS API."""

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, read=120.0),
        )

    async def generate_audio(
        self,
        text: str,
        voice_config: dict[str, Any],
    ) -> dict[str, Any]:
        """Generate audio from text using the voice config.

        Args:
            text: Message text to convert to speech.
            voice_config: Hume voice parameters from create_voice_config().
                Must contain: acting_instructions, speed, output_format.
                Sliders are stored for reference but not sent to API.

        Returns:
            Dict with:
                audio: Raw MP3/WAV bytes
                duration_seconds: Audio duration
                generation_id: Hume generation ID (for voice saving)

        Raises:
            HumeError: On API failure.
            HumeRateLimitError: On 429 rate limit.
            HumeAuthError: On 401 auth failure.
        """
        headers = {
            "X-Hume-Api-Key": self.api_key,
            "Content-Type": "application/json",
        }

        # Build utterance with acting instructions as description
        utterance: dict[str, Any] = {
            "text": text,
        }

        description = voice_config.get("acting_instructions", "")
        if description:
            utterance["description"] = description

        speed = voice_config.get("speed", 1.0)
        if speed != 1.0:
            utterance["speed"] = speed

        # Build request body
        output_format = voice_config.get("output_format", "mp3")
        payload: dict[str, Any] = {
            "utterances": [utterance],
            "format": {"type": output_format},
        }

        logger.debug(
            "Hume TTS request: %d chars, speed=%.2f, format=%s",
            len(text), speed, output_format,
        )

        resp = await self._client.post(
            HUME_TTS_API_URL,
            json=payload,
            headers=headers,
        )

        if resp.status_code == 429:
            raise HumeRateLimitError(
                "Hume AI rate limit reached. Try again later or upgrade your plan."
            )
        if resp.status_code == 401:
            raise HumeAuthError(
                "Hume AI authentication failed. Check your API key."
            )
        if resp.status_code >= 400:
            error_detail = resp.text[:200] if resp.text else f"HTTP {resp.status_code}"
            raise HumeError(f"Hume TTS failed: {error_detail}")

        data = resp.json()

        # Extract audio from response
        generations = data.get("generations", [])
        if not generations:
            raise HumeError("Hume TTS returned no generations")

        generation = generations[0]
        audio_b64 = generation.get("audio", "")
        if not audio_b64:
            raise HumeError("Hume TTS returned empty audio")

        audio_bytes = base64.b64decode(audio_b64)
        duration = generation.get("duration", 0.0)
        generation_id = generation.get("generation_id", "")

        logger.info(
            "Hume TTS generated: %.1fs audio, %d bytes, gen_id=%s",
            duration, len(audio_bytes), generation_id[:8],
        )

        return {
            "audio": audio_bytes,
            "duration_seconds": duration,
            "generation_id": generation_id,
        }

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def __aenter__(self) -> HumeClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()
