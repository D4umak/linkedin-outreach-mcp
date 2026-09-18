"""Email first-touch uses the LinkedIn writer, then adds a subject.

The body is the same generate_message draft (v63 invitation + brief + voice)
that would have gone out as a connection note. Subject is that draft's first
sentence — not a second cold-email prompt.
"""

from __future__ import annotations

from ..textutil import first_name

import logging
from typing import Any

from .llm_router import call_llm

logger = logging.getLogger(__name__)

# Email constraints
SUBJECT_MAX_CHARS = 60
BODY_MAX_CHARS = 800


def merge_email_prospect(
    prospect: dict[str, Any] | None,
    profile: dict[str, Any] | None,
) -> dict[str, Any]:
    """Name/title/company from the campaign contact; email from either.

    Referral enroll often stores ``profile_json`` as ``{email: ...}`` only.
    Passing that blob to ``generate_email`` makes ``first_name`` fall back
    to ``there`` even when the contact row already has ``Sam Rivera``.
    """
    from ..services.prospect_email import extract_profile_email

    row = dict(profile or {})
    contact = prospect or {}
    name = (contact.get("name") or row.get("name") or "").strip()
    if name:
        row["name"] = name
    title = contact.get("title") or row.get("title") or ""
    if title:
        row["title"] = title
    company = contact.get("company") or row.get("company") or ""
    if company:
        row["company"] = company
    email = extract_profile_email(row, contact)
    if email:
        row["email"] = email
    return row


def _is_referral_email(analysis: dict[str, Any] | None) -> bool:
    if not analysis:
        return False
    if analysis.get("referral_email_handoff"):
        return True
    ctx = analysis.get("signal_context")
    return isinstance(ctx, dict) and ctx.get("signal_angle") == "warm_referral"


def _referral_analysis(analysis: dict[str, Any] | None) -> dict[str, Any]:
    """Guarantee ``signal_angle=warm_referral`` so the existing strategy loads."""
    out = dict(analysis or {})
    ctx = dict(out.get("signal_context") or {}) if isinstance(out.get("signal_context"), dict) else {}
    if not ctx.get("signal_angle"):
        ctx["signal_angle"] = "warm_referral"
    out["signal_context"] = ctx
    return out


def subject_from_linkedin_body(body: str, max_chars: int = SUBJECT_MAX_CHARS) -> str:
    """First sentence of the LinkedIn draft — not a new campaign pitch."""
    text = " ".join((body or "").strip().split())
    if not text:
        return ""
    end = len(text)
    for i, ch in enumerate(text):
        if ch in ".?!" and i + 1 < len(text) and text[i + 1].isspace():
            end = i + 1
            break
        if ch in ".?!" and i == len(text) - 1:
            end = i + 1
            break
    subject = text[:end].strip()
    if len(subject) > max_chars:
        cut = subject[: max_chars - 1].rsplit(" ", 1)[0]
        subject = (cut or subject[: max_chars - 1]) + "…"
    if subject and subject[0].islower():
        subject = subject[0].upper() + subject[1:]
    return subject


async def generate_email(
    prospect: dict[str, Any],
    sender_profile: dict[str, Any],
    voice_signature: dict[str, Any],
    campaign_context: dict[str, Any] | None = None,
    prospect_analysis: dict[str, Any] | None = None,
    intelligence_text: str = "",
    max_body_chars: int = BODY_MAX_CHARS,
    campaign_ctx: dict[str, Any] | None = None,
    brief: Any = None,
    body: str = "",
) -> dict[str, Any]:
    """Write email the same way LinkedIn first-touch is written.

    Body comes from ``generate_message`` (v63 invitation + brief + voice),
    or from a pre-written LinkedIn draft when ``body=`` is passed. Subject
    is the first sentence of that draft — not a separate cold-email pitch.
    """
    _ = intelligence_text  # kept so existing callers do not break
    campaign_context = campaign_context or {}
    analysis = (
        _referral_analysis(prospect_analysis)
        if _is_referral_email(prospect_analysis)
        else prospect_analysis
    )

    try:
        if not (body or "").strip():
            from .brief_builder import build_message_brief
            from .intent import resolve_intent
            from . import message_generator

            if brief is None:
                brief = build_message_brief(
                    intent=resolve_intent(campaign_context),
                    campaign_config=campaign_context,
                    campaign_ctx=campaign_ctx or campaign_context,
                    prospect=prospect,
                    analysis=analysis,
                )
            result = await message_generator.generate_message(
                prospect=prospect,
                sender_profile=sender_profile,
                voice_signature=voice_signature,
                campaign_context=campaign_context,
                prospect_analysis=analysis,
                campaign_ctx=campaign_ctx or campaign_context,
                brief=brief,
                max_chars=max_body_chars,
            )
            body = result.get("message") or ""
            reasoning = result.get("reasoning", "")
        else:
            reasoning = "linkedin_draft"

        subject = subject_from_linkedin_body(body)
        from .draft_guard import guard_draft
        body = await guard_draft(body, voice_signature, "email", max_body_chars)
        return {
            "subject": subject[:SUBJECT_MAX_CHARS],
            "body": body[:max_body_chars] if body else "",
            "reasoning": reasoning if isinstance(reasoning, dict) else {"source": reasoning},
        }
    except Exception as e:
        logger.error(f"Email generation failed: {e}")
        raise


async def generate_email_followup(
    prospect: dict[str, Any],
    sender_profile: dict[str, Any],
    voice_signature: dict[str, Any],
    previous_subject: str = "",
    previous_body: str = "",
    followup_count: int = 1,
) -> dict[str, Any]:
    """Generate a follow-up email (reply to previous thread).

    Returns:
        {"subject": str, "body": str}
    """
    prospect_first = first_name(prospect.get("name"), "there")
    sender_name = sender_profile.get("name") or ""
    # voice_signature has no "style" key — the analyzer emits tone,
    # sentence_length, signature_pattern, vocabulary_preferences and no_go.
    style = voice_signature.get("sentence_length", "")
    tone = voice_signature.get("tone", "conversational")

    prompt = f"""Generate a follow-up email for B2B outreach.

This is follow-up #{followup_count} to a cold email that got no reply.

SENDER: {sender_name}
PROSPECT: {prospect_first}
PREVIOUS SUBJECT: {previous_subject}
PREVIOUS EMAIL: {previous_body[:300]}

VOICE: {tone}{", " + style if style else ""}

RULES:
1. Subject: "Re: {previous_subject}" (keep the thread)
2. Body: 2-3 sentences max
3. Don't repeat the original pitch
4. Add new value (insight, stat, case study reference)
5. Lighter CTA than before
6. Follow-up #{followup_count}: {'gentle bump' if followup_count == 1 else 'final reach-out with graceful close' if followup_count >= 3 else 'add new angle'}
7. Do not cite a specific stat (e.g. "That 21x gap") or use sales-methodology jargon (call cadence, market context, more than rapport, repeatable habits).

Return JSON: {{"subject": "...", "body": "..."}}"""

    try:
        raw = await call_llm(prompt, max_tokens=300, json_mode=True)
        import json
        result = json.loads(raw)
        body = (result.get("body") or "")[:BODY_MAX_CHARS]
        from .draft_guard import guard_draft
        body = await guard_draft(body, voice_signature, "email", BODY_MAX_CHARS)
        return {
            "subject": result.get("subject", f"Re: {previous_subject}"),
            "body": body,
        }
    except Exception as e:
        logger.error(f"Email follow-up generation failed: {e}")
        raise
