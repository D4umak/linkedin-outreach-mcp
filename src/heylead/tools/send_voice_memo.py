"""Tool: send_voice_memo — Generate and send a voice note on LinkedIn.

Core flow:
1. Find target outreach (connected prospect)
2. Generate message text via follow-up pipeline
3. Convert text to audio via Hume TTS
4. Send as LinkedIn voice message via Unipile
5. Update outreach status and save message with format="voice"
6. Clean up temp audio file

Falls back to text DM if voice generation fails.
"""

from __future__ import annotations

import logging
from typing import Any

from ..ai.followup_generator import generate_followup, FOLLOWUP_MAX_CHARS
from ..ai.voice_memo_generator import (
    cleanup_voice_memo,
    generate_voice_memo,
)
from ..config import is_voice_memo_enabled
from ..db.queries import (
    find_active_campaign,
    get_campaign,
    get_campaign_context,
    get_followup_candidates,
    get_messages_for_outreach,
    get_outreach_with_contact,
    get_setting,
    increment_usage,
    log_action,
    save_message,
    update_outreach,
)
from ..formatter import stars
from ..linkedin import (
    UnipileAuthError,
    get_account_id,
    get_linkedin_client,
)
from ..db.async_bridge import run_db

logger = logging.getLogger(__name__)

# Mirrors the WHERE clause of get_followup_candidates, which gates the
# auto-pick path. Kept here so the explicit-outreach_id path cannot drift
# away from it.
_VOICE_ELIGIBLE_STATUSES = frozenset({"connected", "messaged"})


async def run_send_voice_memo(
    campaign_id: str = "",
    outreach_id: str = "",
    text: str = "",
) -> str:
    """Generate and send a voice note to a prospect on LinkedIn.

    Args:
        campaign_id: Campaign to send from. Uses active campaign if empty.
        outreach_id: Specific outreach. Auto-picks next connected if empty.
        text: Custom text to convert to voice. Auto-generates if empty.

    Returns:
        Status message string.
    """
    logger.info("send_voice_memo: start (campaign_id=%s, outreach_id=%s, custom_text=%s)",
                campaign_id or "<auto>", outreach_id or "<auto>", bool(text))

    # ── Check voice capability ──
    if not is_voice_memo_enabled():
        logger.warning("send_voice_memo: voice not enabled (no Hume key and not backend mode)")
        return (
            "Voice memos are not enabled.\n\n"
            "In backend mode, voice generation is handled server-side.\n"
            "In direct mode, set your Hume API key:\n"
            "  Edit ~/.heylead/config.json → api_keys.hume"
        )

    # ── Resolve campaign ──
    if campaign_id:
        campaign = await run_db(get_campaign, campaign_id)
        if not campaign:
            logger.warning("send_voice_memo: campaign not found: %s", campaign_id)
            return f"Campaign not found: {campaign_id}"
    else:
        campaign, err = await run_db(find_active_campaign)
        if not campaign:
            logger.warning("send_voice_memo: no active campaign: %s", err)
            return err

    cid = campaign["id"]
    campaign_name = campaign.get("name", cid[:8])
    logger.info("send_voice_memo: resolved campaign=%s (%s)", cid[:8], campaign_name)

    # ── Resolve outreach ──
    if outreach_id:
        candidate = await run_db(get_outreach_with_contact, outreach_id)
        if not candidate:
            logger.warning("send_voice_memo: outreach not found: %s", outreach_id)
            return f"Outreach {outreach_id} not found."
        # The auto-pick path below is filtered by get_followup_candidates
        # (status IN connected/messaged). An explicit id must clear the same
        # bar, or an opted-out or closed prospect gets a voice DM.
        status = candidate.get("status") or ""
        if status not in _VOICE_ELIGIBLE_STATUSES:
            logger.warning(
                "send_voice_memo: refusing send to outreach %s with status %s",
                outreach_id[:8], status,
            )
            return (
                f"Outreach status is '{status}', not "
                f"{' or '.join(sorted(_VOICE_ELIGIBLE_STATUSES))}.\n"
                "Voice memos can only be sent to connected prospects."
            )
    else:
        candidates = await run_db(get_followup_candidates, cid)
        if not candidates:
            logger.info("send_voice_memo: no followup candidates for campaign %s", cid[:8])
            return (
                f"No connected prospects need a voice memo in **{campaign_name}**.\n"
                "All caught up!"
            )
        candidate = candidates[0]
        outreach_id = candidate["outreach_id"]
        logger.info("send_voice_memo: auto-picked outreach %s (1 of %d candidates)", outreach_id[:8], len(candidates))

    prospect_name = candidate.get("name") or candidate.get("contact_name") or "Unknown"
    prospect_provider_id = candidate.get("linkedin_id") or candidate.get("provider_id") or ""
    chat_id = candidate.get("chat_id") or ""
    followup_count = candidate.get("followup_count") or 0
    logger.info("send_voice_memo: prospect=%s, provider_id=%s, chat_id=%s, followup_count=%d",
                prospect_name, prospect_provider_id or "<none>", chat_id or "<none>", followup_count)

    # ── Get LinkedIn client ──
    account_id = await run_db(get_account_id)
    if not account_id:
        logger.warning("send_voice_memo: no LinkedIn account connected")
        return "No LinkedIn account connected. Run setup_profile() first."

    client = get_linkedin_client()

    # ── Generate message text ──
    if not text:
        voice_signature = await run_db(get_setting, "voice_signature") or {}
        campaign_context = await run_db(get_campaign_context, cid) or {}
        conversation = await run_db(get_messages_for_outreach, outreach_id)

        sender = await run_db(get_setting, "sender_profile") or {}
        prospect_data = {
            "name": prospect_name,
            "headline": candidate.get("headline", ""),
            "company": candidate.get("company", ""),
        }

        logger.info("send_voice_memo: generating followup text (voice_sig=%s, sender=%s, conv_len=%d)",
                     bool(voice_signature), bool(sender), len(conversation))
        result = await generate_followup(
            prospect=prospect_data,
            sender_profile=sender,
            voice_signature=voice_signature,
            campaign_context=campaign_context,
            conversation_history=conversation,
            followup_number=followup_count + 1,
        )
        text = result.get("message", "")
        reasoning = result.get("reasoning", {})
        logger.info("send_voice_memo: generated text (%d chars): %s", len(text), text[:80] if text else "<empty>")
    else:
        reasoning = {"custom": True}
        logger.info("send_voice_memo: using custom text (%d chars)", len(text))

    if not text:
        await client.close()
        logger.warning("send_voice_memo: text generation returned empty — aborting")
        return "Failed to generate message text for voice memo."

    # ── Load voice enhancement settings from campaign config ──
    campaign_config = campaign.get("config_json") or {}
    if isinstance(campaign_config, str):
        import json as _json
        try:
            campaign_config = _json.loads(campaign_config)
        except Exception:
            campaign_config = {}
    voice_humanize = campaign_config.get("voice_humanize", True)
    voice_noise_type = campaign_config.get("voice_noise_type", "auto")
    voice_noise_volume = campaign_config.get("voice_noise_volume", "subtle")
    voice_sig = await run_db(get_setting, "voice_signature") or {}

    # ── Generate voice audio ──
    logger.info("send_voice_memo: generating voice audio from text (%d chars)", len(text))
    voice_result = await generate_voice_memo(
        text,
        voice_signature=voice_sig,
        humanize=voice_humanize,
        noise_type=voice_noise_type,
        noise_volume=voice_noise_volume,
    )

    if not voice_result.get("success"):
        # Fall back to text DM
        error = voice_result.get("error", "unknown error")
        logger.warning("send_voice_memo: voice generation FAILED: %s — falling back to text", error)

        # Autopilot: send as text fallback
        logger.info("send_voice_memo: autopilot text fallback to %s (chat_id=%s, provider_id=%s)",
                     prospect_name, chat_id or "<none>", prospect_provider_id or "<none>")
        return await _send_text_fallback(
            client, account_id, outreach_id, candidate,
            prospect_name, prospect_provider_id, chat_id,
            text, followup_count, reasoning,
        )

    audio_path = voice_result["audio_path"]
    duration = voice_result.get("duration_seconds", 0)
    file_size = voice_result.get("file_size_bytes", 0)
    logger.info("send_voice_memo: voice audio generated — path=%s, duration=%.1fs, size=%d bytes",
                audio_path, duration, file_size)

    # ── Copilot: show for approval ──
    # ── Autopilot: send immediately ──
    create_new_chat = not chat_id and prospect_provider_id
    logger.info("send_voice_memo: sending voice — new_chat=%s, chat_id=%s, provider_id=%s",
                create_new_chat, chat_id or "<none>", prospect_provider_id or "<none>")
    try:
        if create_new_chat:
            logger.debug("send_voice_memo: calling send_new_voice_message(provider_id=%s)", prospect_provider_id)
            send_result = await client.send_new_voice_message(
                account_id=account_id,
                provider_id=prospect_provider_id,
                audio_path=audio_path,
                text="",  # Voice-only, no text duplication
            )
        else:
            if not chat_id:
                await client.close()
                cleanup_voice_memo(audio_path)
                logger.warning("send_voice_memo: no chat_id or provider_id for %s — cannot send", prospect_name)
                return f"No chat_id or provider_id for {prospect_name}. Cannot send voice memo."
            logger.debug("send_voice_memo: calling send_voice_message(chat_id=%s)", chat_id)
            send_result = await client.send_voice_message(
                account_id=account_id,
                chat_id=chat_id,
                audio_path=audio_path,
                text="",
            )
        logger.info("send_voice_memo: voice send result: %s", send_result)
    except UnipileAuthError:
        await client.close()
        cleanup_voice_memo(audio_path)
        logger.error("send_voice_memo: LinkedIn account disconnected (UnipileAuthError)")
        return (
            "LinkedIn account disconnected.\n\n"
            "Run setup_profile() again to reconnect."
        )
    except Exception as e:
        await client.close()
        cleanup_voice_memo(audio_path)
        logger.warning("send_voice_memo: voice send exception (%s: %s), falling back to text", type(e).__name__, e)
        # Fall back to text
        client = get_linkedin_client()
        return await _send_text_fallback(
            client, account_id, outreach_id, candidate,
            prospect_name, prospect_provider_id, chat_id,
            text, followup_count, reasoning,
        )
    finally:
        await client.close()

    # Clean up temp file
    cleanup_voice_memo(audio_path)

    if send_result.get("success"):
        new_count = followup_count + 1
        await run_db(update_outreach, outreach_id, status="messaged", followup_count=new_count)
        await run_db(save_message, outreach_id, role="sdr", text=text, format="voice")
        await run_db(increment_usage, "messages_sent")
        await run_db(log_action, "voice_memo_sent",
            outreach_id=outreach_id,
            result="success",
            details={
                "prospect": prospect_name,
                "followup_number": new_count,
                "duration_seconds": duration,
                "text_length": len(text),
                "reasoning": reasoning,
            },)
        return (
            f"✅ {stars()} Voice memo sent to **{prospect_name}**!\n\n"
            f"   Duration: {duration:.1f}s\n"
            f"   Follow-up #{new_count}\n\n"
            f"Text: {text[:100]}{'...' if len(text) > 100 else ''}"
        )
    else:
        error = send_result.get("error", "Unknown error")
        cleanup_voice_memo(audio_path)
        logger.warning("Voice send failed (%s), falling back to text", error)
        # Fall back to text
        client = get_linkedin_client()
        return await _send_text_fallback(
            client, account_id, outreach_id, candidate,
            prospect_name, prospect_provider_id, chat_id,
            text, followup_count, reasoning,
        )


async def _send_text_fallback(
    client: Any,
    account_id: str,
    outreach_id: str,
    candidate: dict,
    prospect_name: str,
    prospect_provider_id: str,
    chat_id: str,
    text: str,
    followup_count: int,
    reasoning: dict,
) -> str:
    """Send a text DM as fallback when voice generation/sending fails."""
    create_new_chat = not chat_id and prospect_provider_id
    logger.info("_send_text_fallback: sending to %s — new_chat=%s, chat_id=%s, provider_id=%s, text_len=%d",
                prospect_name, create_new_chat, chat_id or "<none>", prospect_provider_id or "<none>", len(text))
    try:
        if create_new_chat:
            logger.debug("_send_text_fallback: calling send_new_message(provider_id=%s)", prospect_provider_id)
            send_result = await client.send_new_message(
                account_id=account_id,
                provider_id=prospect_provider_id,
                text=text,
            )
        else:
            logger.debug("_send_text_fallback: calling send_message(chat_id=%s)", chat_id)
            send_result = await client.send_message(
                account_id=account_id,
                chat_id=chat_id,
                text=text,
            )
        logger.info("_send_text_fallback: result: %s", send_result)
    except Exception as e:
        await client.close()
        logger.error("_send_text_fallback: exception (%s: %s)", type(e).__name__, e)
        return f"Both voice and text send failed: {e}"
    finally:
        await client.close()

    if send_result.get("success"):
        new_count = followup_count + 1
        await run_db(update_outreach, outreach_id, status="messaged", followup_count=new_count)
        await run_db(save_message, outreach_id, role="sdr", text=text, format="text")
        await run_db(increment_usage, "messages_sent")
        await run_db(log_action, "followup_sent",
            outreach_id=outreach_id,
            result="success",
            details={
                "prospect": prospect_name,
                "followup_number": new_count,
                "message_length": len(text),
                "voice_fallback": True,
                "reasoning": reasoning,
            },)
        return (
            f"✅ Voice memo failed — sent as text DM instead to **{prospect_name}**.\n\n"
            f"   Follow-up #{new_count}\n\n"
            f"Message: {text[:100]}{'...' if len(text) > 100 else ''}"
        )
    else:
        logger.warning("_send_text_fallback: failed to send to %s: %s", prospect_name, send_result.get("error", "unknown"))
        return f"Failed to send to {prospect_name}: {send_result.get('error', 'unknown')}"
