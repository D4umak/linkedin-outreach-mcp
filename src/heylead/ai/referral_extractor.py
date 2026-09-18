"""Detect a third-person email or named-person handoff in a LinkedIn reply."""

from __future__ import annotations

import re
from dataclasses import dataclass

_EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)

_INTENT_RE = re.compile(
    r"\b(?:"
    r"reach(?:ed)? out to"
    r"|connect you with"
    r"|contact is"
    r"|email them at"
    r"|email \S+ at"
    r"|get in touch with"
    r"|speak (?:to|with)"
    r"|talk to"
    r"|introduce you to"
    r"|pass you (?:to|along)"
    r"|please contact"
    r"|please email"
    r"|reach them at"
    r"|a (?:note|line|message) at"
    r"|drop .{0,50}?a (?:note|line|message) (?:to|at)"
    r")\b",
    re.I,
)

_OWN_EMAIL_RE = re.compile(
    r"\b(?:my email(?: address)? is|here is my email|drop me your email)\b",
    re.I,
)

_NAME_TOKEN_RE = re.compile(r"[A-Z][a-z]+(?:[-'][A-Z]?[a-z]+)?")

_NAME_STOP = frozenset({
    "one", "our", "the", "a", "an", "their", "your", "my",
    "commercial", "team", "chat", "someone", "somebody", "them",
    "him", "her", "you", "me", "us", "it", "this", "that", "then",
    "shortly", "contact", "person", "people", "colleague", "colleagues",
    "hi", "hello", "hey", "thanks", "please", "great",
})

_TLD_SUFFIXES = (
    ".com", ".io", ".ai", ".co", ".net", ".org", ".dev", ".app",
    ".uk", ".us", ".de", ".fr",
)


@dataclass(frozen=True)
class ReferralHandoff:
    name: str
    email: str
    company_guess: str
    quote: str


def detect_referral(
    text: str,
    *,
    sender_emails: set[str] | None = None,
    our_emails: set[str] | None = None,
    referrer_company: str = "",
) -> ReferralHandoff | None:
    """Return a third-party handoff, or None if this is not a referral."""
    if not text or not _INTENT_RE.search(text):
        return None
    if _OWN_EMAIL_RE.search(text) and not _handoff_intent_before_email(text):
        return None

    blocked = {e.strip().lower() for e in (sender_emails or set()) | (our_emails or set()) if e}
    candidates: list[str] = []
    for match in _EMAIL_RE.finditer(text):
        address = match.group(0).rstrip(".,;:)")
        if address.lower() not in blocked:
            candidates.append(address)
    if candidates:
        email = candidates[0]
        if not _email_near_handoff(text, email):
            return None
        name = _name_from_text(text, email) or _name_from_local_part(email.split("@", 1)[0])
        if not name:
            return None
        company = _company_from_domain(email.split("@", 1)[-1], referrer_company)
        return ReferralHandoff(
            name=name,
            email=email,
            company_guess=company,
            quote=text.strip()[:280],
        )

    name = _name_after_intent(text)
    if not name:
        return None
    return ReferralHandoff(
        name=name,
        email="",
        company_guess=(referrer_company or "").strip(),
        quote=text.strip()[:280],
    )


def detect_referral_from_thread(
    texts: list[str],
    *,
    sender_emails: set[str] | None = None,
    our_emails: set[str] | None = None,
    referrer_company: str = "",
) -> ReferralHandoff | None:
    """Prefer an earlier email handoff; fall back to a named nudge on the latest."""
    kwargs = {
        "sender_emails": sender_emails,
        "our_emails": our_emails,
        "referrer_company": referrer_company,
    }
    email_hit: ReferralHandoff | None = None
    name_hit: ReferralHandoff | None = None
    for text in texts:
        found = detect_referral(text, **kwargs)
        if not found:
            continue
        if found.email and email_hit is None:
            email_hit = found
        elif not found.email:
            name_hit = found
    return email_hit or name_hit


def _name_after_intent(text: str) -> str:
    """Take 1–3 capitalized tokens after the handoff phrase, skipping fillers."""
    intent = _INTENT_RE.search(text)
    if not intent:
        return ""
    after = text[intent.end(): intent.end() + 64]
    after = re.split(r"[.!?]", after, maxsplit=1)[0]
    tokens = _NAME_TOKEN_RE.findall(after)
    kept: list[str] = []
    for token in tokens[:3]:
        if token.lower() in _NAME_STOP:
            if kept:
                break
            continue
        kept.append(token)
    return " ".join(kept)


def _handoff_intent_before_email(text: str) -> bool:
    intent = _INTENT_RE.search(text)
    email = _EMAIL_RE.search(text)
    return bool(intent and email and intent.start() < email.start())


def _email_near_handoff(text: str, email: str) -> bool:
    idx = text.lower().find(email.lower())
    if idx < 0:
        return False
    window = text[max(0, idx - 80): idx + len(email)]
    return bool(_INTENT_RE.search(window))


def _name_from_text(text: str, email: str) -> str:
    local = email.split("@", 1)[0].split(".")[0]
    if len(local) >= 3:
        match = re.search(rf"\b({re.escape(local)}[a-z]*)\b", text, re.I)
        if match:
            first = match.group(1).capitalize()
            after = text[match.end(): match.end() + 24]
            last = re.match(r"\s+([A-Z][a-z]+)\b", after)
            if last:
                return f"{first} {last.group(1)}"
            return first
    return ""


def _name_from_local_part(local: str) -> str:
    parts = [p for p in re.split(r"[._+\-]+", local) if p.isalpha() and len(p) > 1]
    return " ".join(p.capitalize() for p in parts)


def _company_from_domain(domain: str, referrer_company: str) -> str:
    host = domain.strip().lower()
    for suffix in _TLD_SUFFIXES:
        if host.endswith(suffix):
            host = host[: -len(suffix)]
            break
    host = host.split(".")[-1]
    stem = host.replace("-", " ").strip()
    if not stem:
        return (referrer_company or "").strip()
    titled = stem[:1].upper() + stem[1:]
    company = (referrer_company or "").strip()
    if company and stem.lower() in company.lower():
        return company
    return titled
