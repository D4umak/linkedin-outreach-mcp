"""LinkedIn's link wrappers, unwrapped in a person's profile text (heylead-api#2723; twin of heylead-api app/services/linkedin_text.py).

LinkedIn delivers a link inside profile text (a headline's "Co-Founder
CapVox.AI") as ``https://www.linkedin.com/redir/suspicious-page?url=CapVox%2eAI
&urlhash=…``. Stored as delivered, it read as the person's title in their
drawer, and a headline reaches six message generators.

``unwrap_linkedin_redirects`` replaces each wrapper with the value it points
at, decoded once: the label in prose (``CapVox.AI``), the target in a website
field (``keep_url=True``). A value that does not look like a host or a URL
(whitespace, control characters, ``<>"``, over 253 characters) is text a
prospect controls; the wrapper is dropped and nothing of it reaches a prompt.
It is idempotent: text with no wrapper comes back as it went in.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

# A wrapper and its query, up to the next whitespace. ``redir/`` covers
# ``suspicious-page`` and ``redirect`` alike.
_WRAPPER = re.compile(r"https?://(?:[a-z]+\.)?linkedin\.com/redir/[^\s?]+\?\S*", re.IGNORECASE)
_TRAILING = ".,;:)]}!?"
_UNSAFE = re.compile(r"[\s<>\"'`\x00-\x1f\x7f]")
_MAX_TARGET = 253


def _target(wrapper: str) -> str:
    query = urlsplit(wrapper).query
    values = parse_qs(query, keep_blank_values=False).get("url") or []
    if not values:
        return ""
    # parse_qs has decoded once already; a double-encoded value (%252e)
    # stays %2e and is shown as such rather than decoded again.
    value = values[0].strip()
    if not value or len(value) > _MAX_TARGET or _UNSAFE.search(value):
        return ""
    return value


def _label(target: str) -> str:
    """The target as prose shows it: its host and path, without the scheme."""
    parts = urlsplit(target if "://" in target else f"//{target}")
    host = parts.netloc or target
    path = parts.path.rstrip("/") if parts.netloc else ""
    return f"{host}{path}"


def unwrap_linkedin_redirects(text: str, *, keep_url: bool = False) -> str:
    """``text`` with every LinkedIn redirect wrapper replaced by its target."""
    if not text or "linkedin.com/redir/" not in text.lower():
        return text

    def swap(match: re.Match[str]) -> str:
        raw = match.group(0)
        tail = ""
        while raw and raw[-1] in _TRAILING:
            tail = raw[-1] + tail
            raw = raw[:-1]
        target = _target(raw)
        if not target:
            return tail
        return (target if keep_url else _label(target)) + tail

    out = _WRAPPER.sub(swap, text)
    return re.sub(r"[ \t]{2,}", " ", out).strip() if out != text else out
