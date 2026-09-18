"""Voice memo generation orchestrator.

High-level service that generates voice memos from text:
1. Loads hume_voice_config from settings (or accepts override)
2. Routes to local Hume client or backend proxy
3. Writes MP3 to ~/.heylead/voice_memos/{uuid}.mp3
4. Returns metadata dict

Always has a text fallback — if audio generation fails for any reason,
callers should send the text message instead.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from ..config import get_hume_api_key, is_backend_mode, voice_memo_dir
from ..constants import VOICE_MEMO_MAX_TEXT_CHARS
from ..db.async_bridge import run_db
from ..db.queries import get_setting

logger = logging.getLogger(__name__)


async def generate_voice_memo(
    text: str,
    voice_config: dict[str, Any] | None = None,
    output_path: str | None = None,
    voice_signature: dict[str, Any] | None = None,
    humanize: bool = True,
    noise_type: str = "auto",
    noise_volume: str = "subtle",
) -> dict[str, Any]:
    """Generate a voice memo MP3 from text.

    Routes through backend if in backend mode and no local Hume key.
    Backend runs enhanced pipeline: humanization → multi-utterance → TTS → noise.

    Args:
        text: Message text to convert (truncated to VOICE_MEMO_MAX_TEXT_CHARS).
        voice_config: Hume voice config. Loads from settings if None.
        output_path: Custom output path. Auto-generates if None.
        voice_signature: Voice signature for text humanization.
        humanize: Whether to humanize text (add fillers, pauses).
        noise_type: Ambient noise type (office, cafe, street, quiet, none, auto).
        noise_volume: Noise volume (subtle, moderate, noticeable).

    Returns:
        {
            "success": bool,
            "audio_path": str,         # Path to generated MP3 file
            "duration_seconds": float,
            "text": str,               # The text that was spoken
            "file_size_bytes": int,
            "generation_id": str,      # Hume generation ID
            "error": str,              # Error message if failed
        }
    """
    result: dict[str, Any] = {
        "success": False,
        "audio_path": "",
        "duration_seconds": 0.0,
        "text": text,
        "file_size_bytes": 0,
        "generation_id": "",
        "error": "",
    }

    # Truncate text if too long
    if len(text) > VOICE_MEMO_MAX_TEXT_CHARS:
        text = text[:VOICE_MEMO_MAX_TEXT_CHARS]
        result["text"] = text
        logger.info("Text truncated to %d chars", VOICE_MEMO_MAX_TEXT_CHARS)

    if not text.strip():
        result["error"] = "Empty text — nothing to generate"
        return result

    # Load voice config from settings if not provided
    if voice_config is None:
        voice_config = await run_db(get_setting, "hume_voice_config", {})
        if not voice_config:
            result["error"] = (
                "No Hume voice config found. Run setup_profile first "
                "to generate your voice config."
            )
            return result

    # Determine output path
    if output_path is None:
        memo_dir = voice_memo_dir()
        memo_dir.mkdir(parents=True, exist_ok=True)
        file_id = uuid.uuid4().hex[:12]
        output_path = str(memo_dir / f"{file_id}.mp3")

    try:
        audio_bytes = await _generate_audio(
            text, voice_config,
            voice_signature=voice_signature,
            humanize=humanize,
            noise_type=noise_type,
            noise_volume=noise_volume,
        )
    except Exception as e:
        result["error"] = str(e)
        logger.error("Voice memo generation failed: %s", e)
        return result

    # Write audio to file
    try:
        with open(output_path, "wb") as f:
            f.write(audio_bytes["audio"])

        result["success"] = True
        result["audio_path"] = output_path
        result["duration_seconds"] = audio_bytes.get("duration_seconds", 0.0)
        result["file_size_bytes"] = len(audio_bytes["audio"])
        result["generation_id"] = audio_bytes.get("generation_id", "")

        logger.info(
            "Voice memo saved: %s (%.1fs, %d bytes)",
            output_path,
            result["duration_seconds"],
            result["file_size_bytes"],
        )

    except OSError as e:
        result["error"] = f"Failed to write audio file: {e}"
        logger.error("Voice memo file write failed: %s", e)

    return result


async def _generate_audio(
    text: str,
    voice_config: dict[str, Any],
    voice_signature: dict[str, Any] | None = None,
    humanize: bool = True,
    noise_type: str = "auto",
    noise_volume: str = "subtle",
) -> dict[str, Any]:
    """Route audio generation to local Hume client or backend proxy.

    Backend mode uses enhanced pipeline (humanization + multi-utterance + noise).
    Local mode uses basic single-utterance Hume call (enhanced pipeline is backend-only).

    Returns dict with 'audio' (bytes), 'duration_seconds', 'generation_id'.
    """
    hume_key = get_hume_api_key()

    if hume_key:
        return await _generate_local(text, voice_config, hume_key)
    elif is_backend_mode():
        return await _generate_via_backend(
            text, voice_config,
            voice_signature=voice_signature,
            humanize=humanize,
            noise_type=noise_type,
            noise_volume=noise_volume,
        )
    else:
        raise RuntimeError(
            "Voice memos require either a Hume API key or backend mode. "
            "Set your Hume key in config or use backend mode."
        )


async def _generate_local(
    text: str, voice_config: dict[str, Any], api_key: str
) -> dict[str, Any]:
    """Generate audio using local Hume API key."""
    from .hume_client import HumeClient

    async with HumeClient(api_key) as client:
        return await client.generate_audio(text, voice_config)


async def _generate_via_backend(
    text: str,
    voice_config: dict[str, Any],
    voice_signature: dict[str, Any] | None = None,
    humanize: bool = True,
    noise_type: str = "auto",
    noise_volume: str = "subtle",
) -> dict[str, Any]:
    """Generate audio via backend enhanced voice pipeline."""
    import base64

    from ..linkedin import get_linkedin_client

    client = get_linkedin_client()
    try:
        audio_b64, duration = await client.generate_voice_memo(
            text, voice_config,
            voice_signature=voice_signature,
            humanize=humanize,
            noise_type=noise_type,
            noise_volume=noise_volume,
        )
        audio_bytes = base64.b64decode(audio_b64 or "")
        # A 200 with no audio means TTS failed upstream. Returning it as
        # success writes a 0-byte mp3 that callers happily send as a voice
        # note; raising here puts them back on the text fallback.
        if not audio_bytes:
            raise RuntimeError("Backend voice pipeline returned empty audio")
        return {
            "audio": audio_bytes,
            "duration_seconds": duration,
            "generation_id": "",
        }
    finally:
        await client.close()


def cleanup_voice_memo(path: str) -> None:
    """Delete a voice memo file after it's been sent."""
    try:
        os.unlink(path)
        logger.debug("Cleaned up voice memo: %s", path)
    except OSError:
        pass


def cleanup_old_voice_memos(max_age_hours: int = 24) -> int:
    """Clean up voice memo files older than max_age_hours.

    Returns count of files deleted.
    """
    memo_dir = voice_memo_dir()
    if not memo_dir.exists():
        return 0

    cutoff = time.time() - (max_age_hours * 3600)
    deleted = 0

    for f in memo_dir.iterdir():
        if f.is_file() and f.suffix == ".mp3":
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    deleted += 1
            except OSError:
                pass

    if deleted:
        logger.info("Cleaned up %d old voice memos", deleted)

    return deleted
