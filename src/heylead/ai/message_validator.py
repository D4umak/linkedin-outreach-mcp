"""6-stage message validation pipeline.

Ensures every outreach message passes quality checks before sending.
From the spec: "Your 56 prompt versions and 5-stage validation pipeline
are not features — they are THE product."

Stages:
1. Length check (≤200 chars for regular LinkedIn)
2. Salesy language detection
3. AI-tell patterns detection
4. Voice consistency check
5. Generic phrase detection
6. Specificity check (catches messages that could be sent to anyone)
7. Consulting-speak (stat mirroring + methodology jargon)
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..textutil import contains_term

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Stage 2: Salesy language blocklist
# ──────────────────────────────────────────────

SALESY_PHRASES = [
    "leverage",
    "synergy",
    "exciting opportunity",
    "game-changer",
    "cutting-edge",
    "revolutionary",
    "transform your",
    "skyrocket",
    "unlock the power",
    "world-class",
    "best-in-class",
    "take your business to the next level",
    "scale your",
    "maximize your",
    "disruptive",
    "paradigm shift",
    "thought leader",
    "low-hanging fruit",
    "value proposition",
    "circle back",
    "move the needle",
    "boil the ocean",
    "deep dive",
    "hop on a call",
    "hop on a quick call",
    "let's connect",
    "would love to connect",
    "pick your brain",
    "touch base",
    "quick chat",
    "quick question",
    "no-brainer",
    # v63 additions — refined from 56 production iterations
    "innovative",
    "leaders",
    "landscape",
    "fast-paced",
    "optimize",
    "unlock",
    "robust",
    "streamline",
    "actionable insights",
    "state of the art",
    "frictionless",
    "next-gen",
    "empower",
    "enable",
    # 25 Aug 2026 — 160 sent invites reused "hands-on" from the writer
    # prompt's GOOD/CTA examples. LinkedIn-SDR filler, not a situation.
    "hands-on",
    "in the weeds",
    "hits differently",
]

# A post is not a 200-char cold note, and the list above is tuned for one.
# Half of it is invite filler ("quick chat") or ordinary English that the v63
# note rules banned for want of room ("leaders", "enable"). Blocking those in
# a 1200-char post rejected drafts that read perfectly well, and guard_draft
# turns a rejection it cannot fix into a silently dropped post.
POST_ALLOWED_SALESY = frozenset({
    # Invite-filler CTAs — they say nothing in a post either way.
    "let's connect",
    "would love to connect",
    "pick your brain",
    "touch base",
    "quick chat",
    "quick question",
    "hop on a call",
    "hop on a quick call",
    "circle back",
    # Ordinary English, banned in a note only because 200 chars cannot
    # afford a word that carries no information.
    "innovative",
    "leaders",
    "landscape",
    "fast-paced",
    "optimize",
    "unlock",
    "robust",
    "streamline",
    "empower",
    "enable",
    "hands-on",
    "in the weeds",
    "deep dive",
    "scale your",
    "maximize your",
})

# ──────────────────────────────────────────────
# Stage 3: AI-tell patterns
# ──────────────────────────────────────────────

AI_TELL_PATTERNS = [
    r"^I noticed (you|your|that you)",
    r"^I came across your",
    r"^I saw your (profile|post|article)",
    r"^I was impressed by",
    r"^Hope this message finds you",
    r"^I'm reaching out because",
    r"^I'd love to",
    r"^I stumbled upon",
    r"^Your work on .+ caught my (eye|attention)",
    r"^As a fellow",
    r"I believe (we|our|I) can",
    r"I'm confident that",
    r"I'm sure you're busy",
    r"Don't hesitate to",
    r"Feel free to",
    r"Looking forward to",
    r"Best regards",
    r"Warm regards",
    r"Kind regards",
    # v63 additions — hard guardrails from production
    r"\u2014",  # em dash — (v63 hard guardrail: "Do not use em dashes")
    r"we've helped similar",  # v63: "Do not use 'we've helped similar companies'"
    # Generic AI questions that provide no value
    r"How do you see .+ (changing|impacting|evolving|shaping)",
    r"Where do you see .+ (heading|going|evolving)",
    r"What's your (take|approach|perspective) on",
    r"How are you (using|leveraging|approaching) AI",
]

# The post prompt asks for a closing question, and an em dash is unremarkable
# prose once there is room for a second clause. Both are real tells in an
# invite, where the whole message is one sentence a stranger did not ask for.
POST_EXEMPT_AI_TELLS = frozenset({
    r"\u2014",
    r"How do you see .+ (changing|impacting|evolving|shaping)",
    r"Where do you see .+ (heading|going|evolving)",
    r"What's your (take|approach|perspective) on",
    r"How are you (using|leveraging|approaching) AI",
})

# v63 guardrail: "DO NOT USE THE WORD 'hope' IN YOUR FIRST 3 SENTENCES"
HOPE_IN_OPENING_PATTERN = re.compile(r"\bhope\b", re.IGNORECASE)

# v63 guardrail: role/title mentions
ROLE_TITLE_PATTERNS = [
    r"\b(ceo|cto|cfo|coo|cmo|vp|svp|evp|founder|co-founder|manager|director|head of|analyst|executive)\b",
]

# v63 guardrail: industry/domain mentions
INDUSTRY_PATTERNS = [
    r"\b(finance|fintech|tech|healthcare|sustainability|erp|saas|ai|blockchain)\b",
]

# ──────────────────────────────────────────────
# Stage 5: Generic phrases
# ──────────────────────────────────────────────

GENERIC_PHRASES = [
    "in your industry",
    "in your field",
    "in your space",
    "professionals like you",
    "leaders like you",
    "people like you",
    "your company",  # Too vague — should name the company
    "your role",
    "your position",
    "mutual connections",
    "shared interests",
]

# ──────────────────────────────────────────────
# Stage 6: Specificity check — catches messages that could be sent to anyone
# ──────────────────────────────────────────────

# Meta-commentary the model writes when it decides not to outreach.
# 22 Aug 2026: a HealthTech CEO received the Ukrainian skip rationale as a DM
# because this draft passed length/salesy/AI-tell and was then "fixed" into
# a sendable note. Detect the refusal itself — do not treat it as copy.
EVALUATOR_REFUSAL_PATTERNS = [
    r"не надсилав(?:ла|ли)? б",
    r"не варто надсилати",
    r"це не схоже на постачальника",
    r"\bi would(?:n't| not) send\b",
    r"\bwould(?:n't| not) send a (?:request|message|invite|invitation)\b",
    r"\b(?:do not|don't) send this\b",
    r"\bskip this (?:prospect|person|contact|profile)\b",
    r"\bdoes not match the icp\b",
    r"\bnot a fit for (?:this|our|the) (?:campaign|icp|brief)\b",
    r"\bthis does(?:n't| not) look like (?:an? )?(?:ais|pis|provider|fit|match)\b",
]


def is_evaluator_refusal(message: str) -> bool:
    """True when the model wrote a skip rationale instead of outreach."""
    text = (message or "").strip()
    if not text:
        return False
    return any(re.search(p, text, re.IGNORECASE) for p in EVALUATOR_REFUSAL_PATTERNS)


# 24 Aug 2026: a live invite cited "That 21x gap" plus sales-methodology
# jargon. The sender could not parse their own message. Prompts already
# forbid pointing at a specific and talking process; the regex layer did
# not, and Specificity even rewarded the flashy number.
STAT_MIRROR_PATTERN = re.compile(r"\bthat\s+\d+(?:\.\d+)?\s*x\b", re.IGNORECASE)

METHODOLOGY_JARGON_PHRASES = [
    "call cadence",
    "market context",
    "more than rapport",
    "repeatable habits",
]


# ──────────────────────────────────────────────
# Stage 4: the user's own no-go list
# ──────────────────────────────────────────────

# Quoted spans are the literal terms. The analyzer prompt asks for the field
# that way — "Won't use emojis, won't say 'synergy'" — so when a value has
# quotes at all, only they count.
#
# A straight or curly single quote is an apostrophe far more often than a
# delimiter, and "Won't ... 'synergy'" opens on the one inside "Won't" if you
# let it: the first span comes back as "t use emojis, won". So a single quote
# only delimits when a letter does not sit against it on the outside.
_QUOTE_PATTERNS = (
    re.compile(r"[\"\u201c\u201d]([^\"\u201c\u201d]{2,40})[\"\u201c\u201d]"),
    re.compile(r"(?<![A-Za-z])['\u2018\u2019]([^'\u2018\u2019]{2,40})['\u2018\u2019](?![A-Za-z])"),
)


# Fragments open with the instruction rather than the term.
_DIRECTIVE_PREFIX = re.compile(
    r"^(?:avoid|do not|don't|never|no|not|won't|will not|use|using|sound|be)\b\s*",
    re.IGNORECASE,
)


# "won't say synergy" — the object of say/use/write is the term, even alone.
_SAY_OBJECT = re.compile(
    r"^(?:avoid|do not|don't|never|no|won't|will not)\s+"
    r"(?:say|use|write|mention)\s+",
    re.IGNORECASE,
)


def no_go_terms(no_go: Any) -> list[str]:
    """The literal phrases a user would never write, from their no_go field.

    Stage 4 used to split this on commas. The analyzer writes prose, so that
    produced fragments like '" "leverage' — nothing that could ever match, and
    the one personal check in the pipeline did nothing (9 Sep 2026).

    The field stays prose because a dozen prompt templates interpolate it as
    voice_nogo. Only the reading of it changed.
    """
    if isinstance(no_go, dict):
        return no_go_terms(no_go.get("terms") or no_go.get("no_go") or [])
    if isinstance(no_go, (list, tuple, set)):
        return _dedupe(str(t).strip().lower() for t in no_go)

    text = str(no_go or "").strip()
    if not text:
        return []

    quoted = [
        m.group(1)
        for pattern in _QUOTE_PATTERNS
        for m in pattern.finditer(text)
    ]
    if quoted:
        return _dedupe(q.strip().strip(".,;:").lower() for q in quoted)

    terms: list[str] = []
    for fragment in re.split(r"[,;.]", text):
        raw = fragment.strip()
        after_say = _SAY_OBJECT.sub("", raw).strip().strip(".,;:").lower()
        if after_say != raw.strip().lower() and after_say:
            if 1 <= len(after_say.split()) <= 4:
                terms.append(after_say)
            continue
        cleaned = _DIRECTIVE_PREFIX.sub("", raw).strip().lower()
        # A bare word in prose is the tail of an instruction, not a term:
        # "Do not sound detached, academic" must not ban "academic".
        if 2 <= len(cleaned.split()) <= 4:
            terms.append(cleaned)
    return _dedupe(terms)


def _dedupe(terms: Any) -> list[str]:
    seen: list[str] = []
    for term in terms:
        if term and term not in seen:
            seen.append(term)
    return seen


# 12 Sep 2026: first visible DM opened "Following product developments at
# Scenario" — reads as a follow-up when the thread has no earlier bubble.
CONTINUATION_OPENER = re.compile(
    r"^(following|followed up)\b",
    re.IGNORECASE,
)


GENERIC_OPENER_PATTERNS = [
    r"^it takes courage",
    r"^building something from (scratch|zero|the ground)",
    r"what's your biggest (priority|challenge|focus)",
    r"what keeps you up at night",
    r"how's business going",
    r"how's everything going",
    r"what's on your plate",
    r"what are you working on",
    r"what drives you",
    r"what motivates you",
    r"curious what you think about",
    r"what's your take on the (market|industry|space)",
    r"how do you stay ahead",
    r"what's next for you",
]


class ValidationResult:
    """Result of the 5-stage message validation."""

    def __init__(self) -> None:
        self.is_valid = True
        self.issues: list[str] = []
        self.warnings: list[str] = []
        self.stage_results: dict[str, bool] = {}

    def fail(self, stage: str, reason: str) -> None:
        self.is_valid = False
        self.issues.append(f"[{stage}] {reason}")
        self.stage_results[stage] = False

    def warn(self, stage: str, reason: str) -> None:
        self.warnings.append(f"[{stage}] {reason}")

    def pass_stage(self, stage: str) -> None:
        self.stage_results[stage] = True


# A 1–3 word prior SDR message ("Thanks" / "Got it thanks") shares a token
# with almost any polite reply. Dividing overlap by min(len) then yields
# 100% and hard-blocks the send. Skip short sides; Jaccard over content
# words is what a real rewrite-vs-copy check needs.
_REPETITION_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "to", "of", "in", "on", "for", "is", "it",
    "you", "i", "we", "my", "your", "that", "this", "with", "at", "as",
    "be", "are", "was", "were", "will", "can", "just", "so", "if", "but",
    "got",
})
_REPETITION_MIN_CONTENT_WORDS = 5
_REPETITION_JACCARD = 0.6


def _content_words(text: str) -> set[str]:
    return {
        tok for tok in re.findall(r"[a-z0-9]+", text.lower())
        if tok not in _REPETITION_STOPWORDS
    }


def _is_repetition(new_text: str, prev_text: str) -> bool:
    new_words = _content_words(new_text)
    prev_words = _content_words(prev_text)
    if (
        len(new_words) < _REPETITION_MIN_CONTENT_WORDS
        or len(prev_words) < _REPETITION_MIN_CONTENT_WORDS
    ):
        return False
    union = new_words | prev_words
    if not union:
        return False
    return len(new_words & prev_words) / len(union) > _REPETITION_JACCARD


def _apply_v63_guardrails(result: ValidationResult, message: str, msg_lower: str) -> None:
    """The v63 note rules: no "hope" opener, no roles, no sector labels."""
    # Check "hope" in first 3 sentences (v63 hard rule)
    sentences = re.split(r'[.!?]+', message, maxsplit=3)
    opening_text = " ".join(sentences[:3])
    if HOPE_IN_OPENING_PATTERN.search(opening_text):
        result.warn("v63-Guardrails", 'Uses "hope" in opening sentences (v63 rule: avoid in first 3)')

    # Check role/title mentions (v63: no roles, titles, seniority, acronyms)
    for pattern in ROLE_TITLE_PATTERNS:
        if re.search(pattern, msg_lower):
            result.warn("v63-Guardrails", "Mentions job roles/titles (v63 rule: no role references)")
            break

    # Check industry/domain mentions (v63: no sector labels)
    for pattern in INDUSTRY_PATTERNS:
        if re.search(pattern, msg_lower):
            result.warn("v63-Guardrails", "Mentions industry/domain (v63 rule: no sector labels)")
            break


def validate_message(
    message: str,
    voice_signature: dict[str, Any] | None = None,
    max_chars: int = 200,
    message_type: str = "invitation",
) -> ValidationResult:
    """Run the 5-stage validation pipeline on a message.

    Args:
        message: The generated message text
        voice_signature: User's voice analysis (for stage 4)
        max_chars: Character limit (200 regular, 300 Sales Nav)
        message_type: What is being judged. "post" relaxes the rules that
            exist because an invite is 200 chars of unsolicited text — the
            v63 note guardrails, the invite-filler half of the salesy list,
            and the ban on a closing question the post prompt asks for.

    Returns:
        ValidationResult with pass/fail and details
    """
    result = ValidationResult()
    msg_lower = message.lower().strip()
    is_post = message_type == "post"

    if not is_post and CONTINUATION_OPENER.search((message or "").strip()):
        result.fail(
            "ContinuationOpener",
            "Opens like a follow-up ('Following…') — the first in-thread "
            "message has to be an intro",
        )

    # ── Stage 0: Evaluator refusal (skip rationale leaked as the draft) ──
    if is_evaluator_refusal(message):
        result.fail(
            "EvaluatorRefusal",
            "Draft is a skip rationale, not an outreach message",
        )
    else:
        result.pass_stage("EvaluatorRefusal")

    # ── Stage 1: Length check ──
    if len(message) > max_chars:
        result.fail("Length", f"Message is {len(message)} chars (max {max_chars})")
    elif len(message) < 20:
        result.fail("Length", f"Message is too short ({len(message)} chars) — needs personalization")
    else:
        result.pass_stage("Length")

    # ── Stage 1.5: Unfilled template tokens ──
    # Four real prospects received notes containing the literal "[Company]"
    # on 18 Aug 2026 — the prompt named it as a forbidden example and the
    # model echoed it. A bracketed token is never legitimate in a 200-char
    # invite; hard-fail so it can't ship.
    placeholder = re.search(r"\[[A-Za-z][^\]\n]{0,30}\]", message)
    if placeholder:
        result.fail("Placeholders", f"Unfilled template token: {placeholder.group(0)}")
    else:
        result.pass_stage("Placeholders")

    # ── Stage 1.6: Opaque consulting-speak ──
    # 24 Aug 2026: "That 21x gap suggests call cadence..." shipped. The
    # sender asked what it meant. Hard-fail so Fix/regenerate must rewrite.
    # Do not ban every "That …" opener or the bare word "rapport".
    if STAT_MIRROR_PATTERN.search(message):
        result.fail("StatMirroring", "Cites a specific stat from their content")
    else:
        result.pass_stage("StatMirroring")

    found_jargon = [
        phrase for phrase in METHODOLOGY_JARGON_PHRASES
        if contains_term(msg_lower, phrase)
    ]
    if found_jargon:
        result.fail(
            "MethodologyJargon",
            f"Uses sales-methodology jargon: {', '.join(found_jargon[:3])}",
        )
    else:
        result.pass_stage("MethodologyJargon")

    # ── Stage 2: Salesy language ──
    # "leaders" fired inside "leadership" and "enable" inside "enabled" while
    # this matched with `in` — the same substring family contains_term was
    # written for (9 Sep 2026).
    salesy_phrases = [
        phrase for phrase in SALESY_PHRASES
        if not (is_post and phrase in POST_ALLOWED_SALESY)
    ]
    found_salesy = [
        phrase for phrase in salesy_phrases if contains_term(msg_lower, phrase)
    ]
    if found_salesy:
        result.fail("Salesy", f"Contains salesy phrases: {', '.join(found_salesy[:3])}")
    else:
        result.pass_stage("Salesy")

    # ── Stage 3: AI-tell patterns ──
    found_ai_tells = []
    for pattern in AI_TELL_PATTERNS:
        if is_post and pattern in POST_EXEMPT_AI_TELLS:
            continue
        if re.search(pattern, message, re.IGNORECASE):
            found_ai_tells.append(pattern.replace(r"^", "").replace("\\", ""))
    if found_ai_tells:
        result.fail("AI-tells", f"Sounds AI-generated: matches {len(found_ai_tells)} known patterns")
    else:
        result.pass_stage("AI-tells")

    # ── Stage 3b: v63 guardrails — "hope" in opening, roles, industries ──
    # These are note rules: an invite that names a role or a sector is
    # describing a segment, not a person. A post is addressed to a room, so
    # "every founder I meet in fintech" is the subject, not a tell.
    if is_post:
        result.pass_stage("v63-Guardrails")
    else:
        _apply_v63_guardrails(result, message, msg_lower)

    # ── Stage 4: Voice consistency ──
    if voice_signature:
        formality = voice_signature.get("formality_level", 5)
        no_go = no_go_terms(voice_signature.get("no_go"))

        # Check formality mismatch
        has_exclamation = "!" in message
        has_emoji = bool(re.search(r"[\U0001f600-\U0001f650]", message))

        if formality >= 8 and (has_exclamation or has_emoji):
            result.warn("Voice", "Message is too casual for this user's formal tone")

        if formality <= 3 and not has_exclamation and len(message) > 150:
            result.warn("Voice", "Message is too formal for this user's casual tone")

        # Check no-go words
        if no_go:
            # Substring again: a no-go word of "ai" matched inside "said".
            found_nogo = [w for w in no_go if contains_term(msg_lower, w)]
            if found_nogo:
                result.fail("Voice", f"Uses no-go words: {', '.join(found_nogo)}")
            else:
                result.pass_stage("Voice")
        else:
            result.pass_stage("Voice")
    else:
        result.pass_stage("Voice")

    # ── Stage 5: Generic phrases ──
    # Every entry here is a stand-in for something the note should have named
    # about one prospect. A post has no prospect to name.
    if not is_post:
        found_generic = [
            phrase for phrase in GENERIC_PHRASES if contains_term(msg_lower, phrase)
        ]
        if found_generic:
            result.warn("Generic", f"Contains generic phrases: {', '.join(found_generic[:2])}")
    result.pass_stage("Generic")

    # ── Stage 6: Specificity check (outreach messages only, not replies/followups) ──
    if max_chars <= 200:  # Only for connection requests (200 char limit)
        found_generic_openers = []
        for pattern in GENERIC_OPENER_PATTERNS:
            if re.search(pattern, msg_lower):
                found_generic_openers.append(pattern.replace(r"^", "").replace("\\", ""))
        if found_generic_openers:
            result.fail(
                "Specificity",
                "Message uses a generic opener that could be sent to anyone — "
                "needs a concrete reference to the prospect's profile or situation"
            )
        else:
            result.pass_stage("Specificity")
    else:
        result.pass_stage("Specificity")

    return result


# ──────────────────────────────────────────────
# Follow-Up Specific Validation
# ──────────────────────────────────────────────

LAZY_FOLLOWUP_PHRASES = [
    "just following up",
    "following up on",
    "following up",
    "circling back",
    "touching base",
    "checking in",
    "wanted to follow up",
    "just wanted to check",
    "just checking in",
    "bumping this",
    "any thoughts on",
    "did you get a chance",
    "have you had a chance",
    "wanted to circle back",
]

# Two or more of these in one follow-up is a product spec, not a nudge.
FOLLOWUP_PITCH_TERMS = (
    "phase 0",
    "phase 1",
    "blueprint",
    "zero-trust",
    "zero trust",
    "orchestration",
    "core infrastructure",
    "architecture",
    "compliancy",
    "compliance gap",
    "model inefficiencies",
)


def validate_followup(
    message: str,
    voice_signature: dict[str, Any] | None = None,
    previous_messages: list[str] | None = None,
    max_chars: int = 500,
    followup_number: int | None = None,
) -> ValidationResult:
    """Validate a follow-up DM message.

    Extends the standard validation with:
    - Higher character limit (500 vs 200; 220 on the first follow-up)
    - Lazy follow-up phrase detection
    - Product-spec / pitch-stack detection
    - Repetition check against previous messages

    Args:
        message: The generated follow-up message text
        voice_signature: User's voice analysis (for voice consistency)
        previous_messages: List of previous message texts (for repetition check)
        max_chars: Character limit (default 500 for DMs)
        followup_number: 1 for the first DM after the invite note

    Returns:
        ValidationResult with pass/fail and details
    """
    if followup_number is not None and followup_number <= 1:
        from .followup_generator import FIRST_FOLLOWUP_MAX_CHARS
        max_chars = min(max_chars, FIRST_FOLLOWUP_MAX_CHARS)

    # Run base validation with the higher char limit
    result = validate_message(message, voice_signature, max_chars)

    msg_lower = message.lower().strip()

    # ── Extra Stage: Lazy follow-up phrases ──
    found_lazy = [phrase for phrase in LAZY_FOLLOWUP_PHRASES if phrase in msg_lower]
    if found_lazy:
        result.fail("LazyFollowUp", f"Uses lazy follow-up phrases: {', '.join(found_lazy[:2])}")
    else:
        result.pass_stage("LazyFollowUp")

    found_pitch = [term for term in FOLLOWUP_PITCH_TERMS if term in msg_lower]
    if len(found_pitch) >= 2:
        result.fail(
            "PitchStack",
            f"Reads as a product spec: {', '.join(found_pitch[:4])}",
        )
    else:
        result.pass_stage("PitchStack")

    # ── Extra Stage: Repetition check ──
    if previous_messages:
        for prev in previous_messages:
            if prev and _is_repetition(message, prev):
                result.fail(
                    "Repetition",
                    "Message is too similar to a previous message (high word overlap)",
                )
                break
        else:
            result.pass_stage("Repetition")
    else:
        result.pass_stage("Repetition")

    return result


# ──────────────────────────────────────────────
# Comment Specific Validation
# ──────────────────────────────────────────────

COMMENT_SALESY_PHRASES = [
    "we offer",
    "our product",
    "our service",
    "our solution",
    "our platform",
    "our tool",
    "check out",
    "have you tried",
    "you should try",
    "we help",
    "we specialize",
    "reach out to me",
    "feel free to dm",
    "send me a message",
    "book a call",
    "schedule a call",
    "let me know if",
    "happy to help",
    "love to chat",
]


def validate_comment(
    comment: str,
    voice_signature: dict[str, Any] | None = None,
    max_chars: int = 200,
) -> ValidationResult:
    """Validate a post comment.

    Extends the standard validation with:
    - Lower character limit (200 vs 500)
    - Comment-specific salesy detection (product mentions, CTAs)
    - No repetition check needed (comments are one-offs)

    Args:
        comment: The generated comment text
        voice_signature: User's voice analysis (for voice consistency)
        max_chars: Character limit (default 200)

    Returns:
        ValidationResult with pass/fail and details
    """
    # Run base validation with comment char limit
    result = validate_message(comment, voice_signature, max_chars)

    comment_lower = comment.lower().strip()

    # ── Extra Stage: Comment-specific salesy phrases ──
    found_comment_salesy = [
        phrase for phrase in COMMENT_SALESY_PHRASES if phrase in comment_lower
    ]
    if found_comment_salesy:
        result.fail(
            "CommentSalesy",
            f"Comment contains promotional language: {', '.join(found_comment_salesy[:2])}"
        )
    else:
        result.pass_stage("CommentSalesy")

    return result


# ──────────────────────────────────────────────
# Reply Specific Validation
# ──────────────────────────────────────────────

PUSHY_REPLY_PHRASES = [
    "i understand but",
    "i understand, but",
    "hear me out",
    "just give me",
    "five minutes of your time",
    "i think you're missing",
    "what if i told you",
    "but what about",
    "reconsider",
    "change your mind",
    "one more thing",
    "before you go",
    "at least consider",
    "maybe if you",
    "are you sure",
    "you might want to",
]


def validate_reply(
    message: str,
    voice_signature: dict[str, Any] | None = None,
    sentiment: str = "neutral",
    previous_messages: list[str] | None = None,
    max_chars: int = 500,
) -> ValidationResult:
    """Validate a reply message with sentiment-specific rules.

    Extends the standard validation with:
    - Lazy follow-up phrase detection (reused from follow-up validation)
    - Repetition check against previous messages
    - Pushy phrase detection for negative-sentiment replies
    - Conciseness warning for negative-sentiment replies

    Args:
        message: The generated reply message text
        voice_signature: User's voice analysis (for voice consistency)
        sentiment: The prospect's reply sentiment (positive, question, negative, neutral)
        previous_messages: List of previous SDR message texts (for repetition check)
        max_chars: Character limit (default 500 for DMs, 200 for negative)

    Returns:
        ValidationResult with pass/fail and details
    """
    # For negative sentiment, enforce shorter limit
    effective_max = min(max_chars, 200) if sentiment == "negative" else max_chars

    # Run base validation with the appropriate char limit
    result = validate_message(message, voice_signature, effective_max)

    msg_lower = message.lower().strip()

    # ── Extra Stage: Lazy follow-up phrases ──
    found_lazy = [phrase for phrase in LAZY_FOLLOWUP_PHRASES if phrase in msg_lower]
    if found_lazy:
        result.fail("LazyFollowUp", f"Uses lazy follow-up phrases: {', '.join(found_lazy[:2])}")
    else:
        result.pass_stage("LazyFollowUp")

    # ── Extra Stage: Repetition check ──
    if previous_messages:
        for prev in previous_messages:
            if prev and _is_repetition(message, prev):
                result.fail(
                    "Repetition",
                    "Reply is too similar to a previous message (high word overlap)",
                )
                break
        else:
            result.pass_stage("Repetition")
    else:
        result.pass_stage("Repetition")

    # ── Extra Stage: Pushy phrases (negative sentiment only) ──
    if sentiment == "negative":
        found_pushy = [phrase for phrase in PUSHY_REPLY_PHRASES if phrase in msg_lower]
        if found_pushy:
            result.fail(
                "PushyReply",
                f"Reply tries to overcome objections: {', '.join(found_pushy[:2])}"
            )
        else:
            result.pass_stage("PushyReply")

        # Warn if negative reply is too long (even if under max_chars)
        if len(message) > 150:
            result.warn("Conciseness", "Negative replies should be concise (1-2 sentences)")
    else:
        result.pass_stage("PushyReply")

    # ── Extra Stage: Email-style sign-offs ──
    # LinkedIn DMs should never end with "- Name" or "Best, Name" — it's a
    # classic bot tell. We still strip them in reply_pipeline.strip_signature
    # as a last-resort safety net, but failing here means the Fix stage gets
    # a chance to regenerate a clean message instead.
    # Last line only — a conversational "...thanks" / "suits you best"
    # is not an email signature. The old `$` + optional name matched any
    # sentence that happened to end on those words.
    _signoff_patterns = [
        r"[-\u2013\u2014]+\s*[A-Z][a-zA-Z'\-]{0,20}\s*\.?\s*$",
        r"(?:^|\n)\s*(?:best|cheers|thanks|regards|kind regards|warm regards|sincerely)"
        r"(?:\s*,\s*[A-Za-z][A-Za-z'\-]{0,20})?\s*[.!?]?\s*$",
    ]
    _stripped = message.strip()
    for _pat in _signoff_patterns:
        if re.search(_pat, _stripped, re.IGNORECASE):
            result.fail(
                "Signoff",
                "Reply ends with an email-style signature — LinkedIn DMs don't sign off"
            )
            break
    else:
        result.pass_stage("Signoff")

    return result
