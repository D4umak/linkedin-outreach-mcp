"""Booking link classification and confirmation, mirrored from the hosted API (#3021).

The pinned fixture tests hold the host/path table in sync across all clients.
"""
from __future__ import annotations
import json
import re
from typing import Any
from urllib.parse import urlsplit

KEY = "booking_link"
CONFIRMED_KEY = "booking_link_confirmed_page"
MAX_CHARS = 2048
INVALID_MESSAGE = "Use a full link that starts with https://"
SCHEDULER = "scheduler"
PAGE = "page"
SCHEDULER_HOSTS: tuple[str, ...] = (
    "calendly.com", "cal.com", "cal.eu", "savvycal.com", "tidycal.com", "zcal.co",
    "youcanbook.me", "acuityscheduling.com", "as.me", "chilipiper.com", "koalendar.com",
    "meetings.hubspot.com", "meetings-eu1.hubspot.com", "setmore.com",
    "zohobookings.com", "zohobookings.eu", "zohobookings.in", "zohobookings.com.au",
    "book.squareup.com", "book.vimcal.com", "book.ms", "calendar.app.google",
)

#: Hosts that serve booking pages under one path segment only: (host, segment).
SCHEDULER_HOST_SEGMENTS: tuple[tuple[str, str], ...] = (
    ("calendar.google.com", "appointments"),
    ("outlook.office365.com", "bookings"),
    ("outlook.office365.com", "bookwithme"),
    ("outlook.office.com", "bookings"),
    ("outlook.office.com", "bookwithme"),
    ("calendar.notion.so", "meet"),
    ("hubspot.com", "meetings"),
    ("app.reclaim.ai", "m"),
    ("app.usemotion.com", "meet"),
)

#: A path segment that names a booking page on any host, the owner's own
#: domain included (``example.com/book``). Ambiguous ones (a book for sale, an
#: agenda) count as a scheduler: a missed note costs less than a wrong one.
SCHEDULER_SEGMENTS: frozenset[str] = frozenset({
    "book", "booking", "bookings", "book-call", "schedule", "schedule-call",
    "meeting", "meetings", "appointment", "appointments",
})
#: A path segment that starts with one of these: /book-a-call, /schedule-a-meeting.
SCHEDULER_SEGMENT_PREFIXES: tuple[str, ...] = ("book-a-", "schedule-a-")


def _host_matches(host: str, entry: str) -> bool:
    return host == entry or host.endswith("." + entry)


def _host_and_segments(url: str) -> tuple[str, list[str]]:
    # A stored link from before ``normalise`` required https (``cal.com/x/15``)
    # has no scheme; urlsplit would read its host as path, and a real
    # scheduler would read as a page.
    text = str(url or "").strip()
    if text and "://" not in text and not text.startswith("//"):
        text = "//" + text
    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        return "", []
    return host, [seg for seg in parts.path.lower().split("/") if seg]


def _is_scheduler(
    host: str, segments: list[str], *,
    hosts: tuple[str, ...] = (), segment_names: frozenset[str] = frozenset(),
) -> bool:
    if not host:
        return False
    if any(_host_matches(host, h) for h in (*SCHEDULER_HOSTS, *hosts)):
        return True
    if any(_host_matches(host, h) and seg in segments for h, seg in SCHEDULER_HOST_SEGMENTS):
        return True
    names = SCHEDULER_SEGMENTS | segment_names
    return any(seg in names or seg.startswith(SCHEDULER_SEGMENT_PREFIXES) for seg in segments)


def link_kind(url: str) -> str:
    """``scheduler`` when the table knows ``url`` for a booking page, else ``page``.

    The host is parsed and matched by suffix; the path rules read the path
    only, never the query or the fragment.
    """
    host, segments = _host_and_segments(url)
    return SCHEDULER if _is_scheduler(host, segments) else PAGE


#: A link in a prospect's message, up to the first space, quote or bracket.
_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)
_TRAILING = ".,;:!?)]'"


def scheduler_link_in(
    text: str, *, hosts: tuple[str, ...] = (), segments: frozenset[str] = frozenset(),
) -> str:
    """The first link in ``text`` the table (plus the caller's own ``hosts``
    and ``segments``) reads as a booking page, or "". Linear in ``text``."""
    for found in _URL_IN_TEXT.finditer(text or ""):
        url = found.group(0).rstrip(_TRAILING)
        host, segs = _host_and_segments(url)
        if _is_scheduler(host, segs, hosts=hosts, segment_names=segments):
            return url
    return ""


#: The assistant's words for a stored link (designer review on #3021).
PAGE_LINE = (
    "Booking link: {host}/…. This looks like a website, not a booking page, so it is not "
    "offered yet: when someone agrees to a call, HeyLead holds the conversation for you. "
    'To keep it, run edit_campaign(booking_link="{link}", booking_link_page_ok=true). '
    "To change it, pass your booking page."
)
KEPT_PAGE_LINE = "Booking link: {host}/… (a website page you chose), offered when someone agrees to a call."
SCHEDULER_LINE = "Booking link: {host}/…, offered when someone agrees to a call."


def link_line(link: str, *, confirmed: bool) -> str:
    """What the assistant says about a saved link: a booking page, a website
    page the owner kept, or the note that asks about one."""
    host = host_of(link)
    if link_kind(link) == SCHEDULER:
        return SCHEDULER_LINE.format(host=host)
    if confirmed:
        return KEPT_PAGE_LINE.format(host=host)
    return PAGE_LINE.format(host=host, link=link)


class BookingLinkInvalid(ValueError):
    """The link is refused. ``str(exc)`` is the owner's sentence; ``reason`` is ours."""

    def __init__(self, reason: str):
        super().__init__(INVALID_MESSAGE)
        self.reason = reason
        self.user_message = INVALID_MESSAGE


def normalise(raw: Any) -> str:
    """The link to store, "" to clear it, or ``BookingLinkInvalid``.

    Only the whitespace around it is trimmed. A link with words around it
    ("my link: https://..."), any scheme but https, a bare ``https://``, a
    link without a dotted host, credentials before the host and anything
    over 2,048 characters are refused, not repaired: a repaired link is one
    the owner never typed.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    if len(text) > MAX_CHARS:
        raise BookingLinkInvalid("too_long")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise BookingLinkInvalid("surrounding_text")
    try:
        parts = urlsplit(text)
        host = parts.hostname or ""
        parts.port  # noqa: B018 - raises on a malformed port
    except ValueError:
        raise BookingLinkInvalid("malformed") from None
    if parts.scheme.lower() != "https":
        raise BookingLinkInvalid("not_https")
    if "@" in parts.netloc:
        raise BookingLinkInvalid("credentials")
    labels = host.split(".")
    if not host or len(labels) < 2 or not all(labels):
        raise BookingLinkInvalid("no_host")
    return "https" + text[len(parts.scheme):]


def stored_link(config: Any) -> str:
    """The link a campaign config carries, or ""."""
    cfg = config_of(config)
    return str(cfg.get(KEY) or "").strip()




def page_confirmed(config: Any) -> bool:
    """The owner kept this campaign's link though it reads as a website."""
    return config_of(config).get(CONFIRMED_KEY) is True


def stored_link_kind(config: Any) -> str:
    """``link_kind`` of the stored link, or "" with none."""
    link = stored_link(config)
    return link_kind(link) if link else ""


def offered_link(config: Any) -> str:
    """The link a prospect may be sent, or "" (#3021).

    The stored link when it is a scheduler, or a page the owner kept
    (``booking_link_page_ok``). An unconfirmed page is no booking target: a
    meeting reply on it is held with ``no_booking_target``, like no link.
    """
    cfg = config_of(config)
    link = stored_link(cfg)
    if not link:
        return ""
    if link_kind(link) == SCHEDULER or page_confirmed(cfg):
        return link
    return ""


def host_of(link: str) -> str:
    """``calendly.com`` for a link, for "Booking link: <host>/…" lines."""
    try:
        return urlsplit(link).hostname or ""
    except ValueError:
        return ""


def config_of(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def booking_context(config: Any) -> dict[str, Any]:
    """Keep classification evidence with a link in reduced prompt contexts."""
    return {KEY: offered_link(config), CONFIRMED_KEY: page_confirmed(config)}
