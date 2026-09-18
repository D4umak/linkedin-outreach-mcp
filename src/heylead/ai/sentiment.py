"""Reply sentiment classifier.

Classifies LinkedIn reply messages into:
- positive: "Let's talk!", "Sounds interesting"
- negative: "Not interested", "Stop messaging me"
- question: "How much?", "What do you do?"
- neutral: "Thanks", "OK"
- out_of_office: "I'm currently out of office"
- opt_out: "Unsubscribe", "Remove me", "Don't contact me"
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..constants import LLM_TIER_FAST
from .llm import LLMClient

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Rule-based fast path (no LLM needed)
# ──────────────────────────────────────────────

OPT_OUT_KEYWORDS = [
    "not interested",
    "stop messaging",
    "stop contacting",
    "don't contact me",
    "do not contact",
    "unsubscribe",
    "remove me",
    "take me off",
    "leave me alone",
    "don't message me",
    "no thanks",
    "no thank you",
    "please stop",
    "opt out",
    "opt-out",
]

OOO_KEYWORDS = [
    "out of office",
    "out of the office",
    "on vacation",
    "on leave",
    "on holiday",
    "currently away",
    "will be back",
    "i'll be back",
    "limited access to email",
    "auto-reply",
    "automatic reply",
]

POSITIVE_SIGNALS = [
    "let's talk",
    "let's chat",
    "sounds interesting",
    "sounds great",
    "I'd love to",
    "i'd love to",
    "tell me more",
    "i'm interested",
    "am interested",
    "happy to chat",
    "let's set up",
    "let's schedule",
    "free next week",
    "book a time",
    "send me a link",
    "send your calendar",
    "calendly",
    "cal.com",
    # Meeting-intent signals — prospect wants to schedule
    "grab a slot",
    "grab a time",
    "pick a time",
    "schedule a call",
    "set up a call",
    "set up a meeting",
    "hop on a call",
    "jump on a call",
    "quick call",
    "when are you free",
    "when works for you",
    "when's good for you",
    "what time works",
    "what's your availability",
    "your availability",
    "check my calendar",
    "my calendar",
    "how's your calendar",
    "i'll grab a slot",
    "down to chat",
    "down for a call",
    "open to a call",
    "open to chat",
    "time for a chat",
    "time for a call",
    "lock in a time",
    "find a time",
]

NEGATIVE_SIGNALS = [
    "not doing",
    "not looking",
    "not the right time",
    "not a good time",
    "not a fit",
    "not a good fit",
    "not relevant",
    "not for us",
    "not for me",
    "not right now",
    "not at this time",
    "maybe later",
    "bad timing",
    "wrong time",
    "we don't need",
    "we already have",
    "already using",
    "we're all set",
    "we are all set",
    "we're good",
    "we are good",
    "pass on this",
    "i'll pass",
    "i will pass",
    "gonna pass",
    "going to pass",
    "can't help you",
    "can't help with",
    "unfortunately",
    "sorry but",
    "sorry, but",
    "sorry we",
    "appreciate it but",
    "thanks but",
    "thank you but",
]

# Regex patterns for negation + activity (e.g., "not doing X this year")
_NEGATIVE_REGEX = re.compile(
    r"\b(?:not|no longer|stopped|won't be|aren't|isn't|don't|doesn't)\b.{0,30}"
    r"\b(?:doing|offering|running|pursuing|looking|hiring|taking|accepting|available)\b",
    re.IGNORECASE,
)

# Calendar / booking URL patterns — detects when prospect shares their scheduling link
_CALENDAR_URL_REGEX = re.compile(
    # The booking hosts must match a whole host, not a substring: an unanchored
    # `cal\.com` also matches vertical.com / acmemedical.com.
    r"https?://(?:"
    r"(?:[^\s/?#]*\.)?(?:"
    r"calendly\.com|cal\.com|acuityscheduling\.com|"
    r"savvycal\.com|tidycal\.com|zcal\.co|youcanbook\.me|"
    r"meetingbird\.com|doodle\.com|koalendar\.com|chilipiper\.com"
    r")(?![^\s/?#])"
    r"|[^\s]*?(?:"
    r"hubspot\.com/meetings|"
    r"/book-a-call|/free-strategy-call|/schedule-a-call|/book-a-meeting|"
    r"/free-consultation|/schedule-a-meeting|/book-a-demo|/schedule-call|"
    r"/booking|/schedule\b|/meet/|/call/|/meeting/"
    r")"
    r")[^\s]*",
    re.IGNORECASE,
)

# Calendar intent keywords — catches "check my calendar" style replies without URLs
_CALENDAR_INTENT_REGEX = re.compile(
    r"\b(?:"
    r"(?:here(?:'s|s|\s+is)\s+my\s+(?:calendar|booking|scheduling)\s*(?:link|url|page)?)"
    r"|(?:check\s+my\s+calendar)"
    r"|(?:grab\s+a\s+(?:slot|time|spot))"
    r"|(?:book\s+a\s+(?:time|slot|call|meeting))"
    r"|(?:pick\s+a\s+(?:time|slot))"
    r"|(?:(?:my|the)\s+(?:calendar|booking)\s+(?:link|url|page))"
    r")\b",
    re.IGNORECASE,
)


def detect_calendar_url(text: str) -> str | None:
    """Detect a prospect's inbound calendar/booking URL in their message text.

    This detects INBOUND calendar links (a prospect sharing their scheduling page).
    Not to be confused with the user's OUTBOUND booking_link stored in campaign config.

    Returns the first calendar/booking URL found, or None.
    """
    match = _CALENDAR_URL_REGEX.search(text)
    return match.group(0) if match else None


def detect_calendar_intent(text: str) -> bool:
    """Detect if the prospect's message expresses calendar/booking intent without a URL.

    Catches phrases like "check my calendar", "grab a slot", "book a time".
    Used as a fallback when detect_calendar_url() finds no URL but the prospect
    clearly wants to schedule via a previously shared link.
    """
    return bool(_CALENDAR_INTENT_REGEX.search(text))


# ──────────────────────────────────────────────
# Meeting agreement detection — cheap pre-filter for LLM extraction
# ──────────────────────────────────────────────

_MEETING_AGREED_PATTERNS = re.compile(
    r"\b(?:"
    r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+(?:at|is\s+great|works|is\s+good|is\s+fine)"
    r"|(?:say|how\s+about|let'?s\s+do|confirmed?\s+for|see\s+you\s+(?:at|then|on))"
    r"|(?:\d{1,2}\s*(?:am|pm|a\.m\.|p\.m\.))"
    r"|(?:works?\s+for\s+me)"
    r"|(?:i'?ll?\s+send\s+(?:a\s+)?(?:calendar|invite|meeting))"
    r"|(?:sending\s+(?:a\s+)?(?:calendar|invite))"
    r"|(?:booked?\s+(?:it|you|for))"
    r"|(?:looking\s+forward\s+to\s+chatting)"
    r"|(?:i\s+look\s+forward\s+to\s+chatting)"
    r")\b",
    re.IGNORECASE,
)


def detect_meeting_agreement(text: str) -> bool:
    """Quick keyword check for meeting agreement signals.

    This is a cheap pre-filter before calling the LLM meeting extractor.
    Returns True if the text contains patterns suggesting a specific
    meeting time was proposed or confirmed.
    """
    return bool(_MEETING_AGREED_PATTERNS.search(text))


def classify_fast(reply_text: str) -> str | None:
    """Rule-based fast classification. Returns sentiment or None if uncertain."""
    text_lower = reply_text.lower().strip()

    # Check opt-out first (highest priority)
    for kw in OPT_OUT_KEYWORDS:
        if kw in text_lower:
            return "opt_out"

    # Check out-of-office
    for kw in OOO_KEYWORDS:
        if kw in text_lower:
            return "out_of_office"

    # Check negative signals before positive (declines with "sorry" shouldn't be positive)
    for signal in NEGATIVE_SIGNALS:
        if signal in text_lower:
            return "negative"
    if _NEGATIVE_REGEX.search(reply_text):
        return "negative"

    # Calendar/booking URL shared by prospect → positive (they want to meet)
    if detect_calendar_url(reply_text):
        return "positive"

    # Calendar intent without URL (e.g., "check my calendar", "grab a slot")
    if detect_calendar_intent(reply_text):
        return "positive"

    # Check positive signals (explicit buying interest)
    for signal in POSITIVE_SIGNALS:
        if signal in text_lower:
            return "positive"

    # Check if it's a question
    if text_lower.endswith("?") or text_lower.count("?") >= 1:
        return "question"

    # Very short neutral responses
    if len(text_lower) < 15 and text_lower in ("thanks", "thank you", "ok", "okay", "cool", "noted", "got it"):
        return "neutral"

    # Can't determine — need LLM
    return None


SENTIMENT_SYSTEM = """You classify LinkedIn reply messages. Output ONLY one word: positive, negative, question, neutral, engaged, out_of_office, or opt_out.

Definitions:
- positive: Explicitly interested in the SENDER'S product/service — wants to talk, meet, learn more about what the sender offers. ALSO positive: any message expressing intent to schedule a meeting/call (e.g., "I'll grab a slot", "Does Tuesday work?", "Let's find a time", "When are you free?", sharing a calendar link)
- engaged: Answering a question, sharing their own business context, or participating in conversation — but NOT expressing interest in the sender's product. They're talking WITH you, not asking FOR your thing
- negative: Politely or explicitly declining, not interested, bad timing, or shutting down the conversation. Includes soft declines like "sorry we are not doing X", "not the right time", "we already have a solution", "maybe later", "not looking right now"
- question: Asking for more info about the sender's product/service (pricing, features, details)
- neutral: Acknowledgment without clear intent (e.g., "thanks", "ok", "got it")
- out_of_office: Auto-reply or vacation notice
- opt_out: Asking to stop being contacted

CRITICAL RULES:
1. Most replies to discovery questions (e.g., "What's your biggest challenge?") are "engaged" NOT "positive". "Positive" means they want YOUR product. "Engaged" means they're having a conversation.
2. Any message containing "sorry" + a decline or "not doing/not looking/not right now" is "negative", NOT "positive" or "engaged". Polite rejections are still rejections.
3. When in doubt between "positive" and "engaged", choose "engaged". Only classify as "positive" when there is UNMISTAKABLE buying intent."""


def _format_prior_turns(prior_turns: list[dict[str, Any]] | None) -> str:
    """Render the turns that precede the message being classified.

    Two prior turns is enough to tell "not right now" answering *our* question
    apart from "not right now" answering their own earlier thought.
    """
    if not prior_turns:
        return ""
    from .prompt_loader import format_transcript

    return format_transcript(prior_turns[-2:], mark_latest=False)


async def classify_sentiment(
    reply_text: str,
    prior_turns: list[dict[str, Any]] | None = None,
    **log_ids: Any,
) -> str:
    """Classify the sentiment of a LinkedIn reply.

    Tries fast rule-based classification first.
    Falls back to LLM for ambiguous messages.

    Args:
        reply_text: The prospect message to classify.
        prior_turns: Optional preceding conversation turns ({role, text,
            timestamp}); the last two are shown to the LLM as context. The
            fast rule path ignores them — it only ever reads this message.

    Returns one of: positive, negative, question, neutral, engaged, out_of_office, opt_out
    Extra kwargs (outreach_id, message_id, …) are attached to reply_classified.
    """
    def _emit(sentiment: str, classifier: str) -> str:
        from ..ops_log import log_event, message_hash

        extra = {k: v for k, v in log_ids.items() if v}
        log_event(
            "reply_classified",
            sentiment=sentiment,
            classifier=classifier,
            text_hash=message_hash(reply_text),
            prior_turns=len(prior_turns or []),
            **extra,
        )
        return sentiment

    # Fast path (always runs, no LLM needed)
    fast_result = classify_fast(reply_text)
    if fast_result:
        return _emit(fast_result, "fast")

    # Route through backend if in backend mode and no local LLM key
    from ..config import has_local_llm_key, is_backend_mode
    if is_backend_mode() and not has_local_llm_key():
        from ..linkedin import get_linkedin_client
        backend_client = get_linkedin_client()
        try:
            return _emit(await backend_client.classify_sentiment(reply_text), "llm")
        except Exception as e:
            logger.warning(f"Backend sentiment classification failed: {e}, defaulting to neutral")
            return _emit("neutral", "llm")
        finally:
            await backend_client.close()

    # LLM path
    try:
        client = LLMClient()
        _prior = _format_prior_turns(prior_turns)
        _context = (
            f"Earlier in this thread (oldest first):\n{_prior}\n\n" if _prior else ""
        )
        prompt = (
            f'{_context}Classify this LinkedIn reply message:\n\n"{reply_text}"\n\n'
            "Output ONLY one word: positive, negative, question, neutral, "
            "engaged, out_of_office, or opt_out."
        )
        result = await client.generate(prompt, system=SENTIMENT_SYSTEM, temperature=0.1, max_tokens=20,
                                      tier=LLM_TIER_FAST)
        sentiment = result.strip().lower().replace('"', "").replace("'", "")
        if not sentiment:
            return _emit("neutral", "llm")

        # Ordered, not a set: a wordy answer can name several labels, and the
        # winner must be the same in every process. Opt-out first — reading an
        # opt-out as anything else keeps messaging someone who said stop.
        valid_sentiments = (
            "opt_out", "out_of_office", "negative", "question",
            "positive", "engaged", "neutral",
        )
        if sentiment in valid_sentiments:
            return _emit(sentiment, "llm")

        # Try to extract from a longer response
        for s in valid_sentiments:
            if s in sentiment:
                return _emit(s, "llm")

        logger.warning(f"LLM returned unexpected sentiment: {sentiment}, defaulting to neutral")
        return _emit("neutral", "llm")

    except Exception as e:
        logger.warning(f"Sentiment classification failed: {e}, defaulting to neutral")
        return _emit("neutral", "llm")
