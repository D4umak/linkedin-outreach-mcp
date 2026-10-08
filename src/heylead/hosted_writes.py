"""A hosted account's laptop changes nothing on LinkedIn (heylead-api#2318).

Since 5 Oct 2026 (#2120) the laptop of a hosted account runs no scheduler and
sends no first touch. Every other LinkedIn write a laptop tool could reach --
likes, comments, follows, posts, profile edits, message deletes, chat archive
and mark-read, invitation withdrawals -- still went through the api's proxy
from this machine, outside the cloud's opt-ins, Approvals and budgets. The
worst: ``brand_strategy(action="execute")`` posted AI comments at once, past
the brand-engagement opt-in (#2221).

This module is the one place that decides. ``laptop_writes_refused()`` is true
for every hosted account; a tool asks it before it would write, and either
asks the cloud to do the thing (posts, inbox replies, comment replies,
headline and About) or answers with the sentence below that says where it is
done instead. ``ensure_laptop_may_write()`` is the same question as a guard
for the helpers below a tool: it raises in hosted mode, so a path a tool
forgot cannot reach the proxy. Self-hosted installs are untouched.

``.semgrep/a-hosted-laptop-writes-to-linkedin.yaml`` refuses a LinkedIn write
call that is not preceded by one of the two.

The sentences are the designer review's, verbatim, on the outcome issue. Every
dashboard page is built with ``dashboard_links.dashboard_url``.
"""

from __future__ import annotations

from . import config
from .dashboard_links import dashboard_url


def laptop_writes_refused() -> bool:
    """True when this machine must not write to LinkedIn: a hosted account.

    The same predicate the daemon uses to keep SchedulerEngine off
    (``cloud_sync.local_scheduler_engine_enabled`` is its negation), read
    live from config so a sign-in or sign-out takes effect at once.
    """
    return config.is_backend_mode()


class LaptopWriteRefused(RuntimeError):
    """A LinkedIn write reached a helper on a hosted account's laptop."""


def ensure_laptop_may_write(what: str = "") -> None:
    """Raise LaptopWriteRefused on a hosted account; a no-op self-hosted.

    For helpers below a tool entry that already answered with a sentence: if
    a path gets here anyway, it stops before the HTTP call rather than
    writing through the proxy.
    """
    if laptop_writes_refused():
        raise LaptopWriteRefused(generic_refusal() + (f" ({what})" if what else ""))


def _d(path: str) -> str:
    return dashboard_url(path)


def generic_refusal() -> str:
    """The fallback sentence, the same words the api's 403 uses."""
    return (
        "HeyLead changes LinkedIn only from the cloud now, so nothing was changed; "
        f"do this from your dashboard at {_d('')} (Campaigns, Approvals, Content or Brand)."
    )


# ── Refused actions ──


def engage_prospect_refused() -> str:
    return (
        "Nothing was changed on LinkedIn. HeyLead warms people up from the cloud as "
        "part of a campaign, so likes, comments and follows happen there. Switch on "
        f"warm-up on the campaign's page at {_d('campaigns')}."
    )


def brand_engagement_refused() -> str:
    return (
        "Nothing was posted. HeyLead comments from the cloud once you switch on "
        f"engagement on {_d('brand')}, and every comment waits in {_d('approvals')} "
        "until you approve it."
    )


def brand_post_in_cloud() -> str:
    """brand_strategy execute on a post action: the cloud runs the plan."""
    return (
        "Nothing was posted from here. HeyLead runs your brand plan from the cloud, "
        f"and its posts are in {_d('content/posts')}."
    )


def profile_visuals_refused() -> str:
    """Photo, cover, link, photo_enhance and makeover."""
    return (
        "Nothing was changed on your profile. HeyLead no longer edits your photo, "
        "cover or links. Change them on LinkedIn directly. Your headline and About "
        f"section can still be changed on {_d('brand')}."
    )


def profile_field_refused() -> str:
    """A profile field other than headline and About (education, skills...)."""
    return (
        "Nothing was changed on your profile. Change it on LinkedIn directly. Your "
        f"headline and About section can still be changed on {_d('brand')}."
    )


def headline_test_refused() -> str:
    return (
        "Nothing was changed on your profile. Headline tests no longer run. "
        f"Generate and apply a headline on {_d('brand')}."
    )


def restore_profile_refused() -> str:
    return (
        "Nothing was changed on your profile. HeyLead no longer puts fields back. "
        "To restore your headline or About section, apply the earlier text on "
        f"{_d('brand')}. Anything else, change on LinkedIn directly."
    )


def backfill_sending_refused() -> str:
    return (
        "Nothing was sent. HeyLead no longer messages people from your past inbox. "
        "For anyone worth writing to, add them to a campaign at "
        f"{_d('campaigns')} and it writes to them from the cloud."
    )


def message_delete_refused() -> str:
    return (
        "Nothing was deleted. HeyLead no longer deletes LinkedIn messages. "
        "Delete it in LinkedIn itself."
    )


def chat_archive_skipped() -> str:
    """prospect close: closed here, the LinkedIn chat is left alone."""
    return (
        "Closed in HeyLead. The LinkedIn conversation stays in your inbox. "
        "Archive it in LinkedIn if you want it out of sight."
    )


MARK_READ_SKIPPED = "Conversations stay unread on LinkedIn."


# ── create_post, done by the cloud ──


def post_published() -> str:
    return f"Posted to LinkedIn from the cloud. It is in {_d('content/posts')}."


def post_held() -> str:
    return (
        f"Saved as a draft in {_d('content/posts')}. Your posts need approval before "
        "they go out, so open it there and press Publish. Nothing is on LinkedIn yet."
    )


def post_published_without_photo() -> str:
    return (
        "Posted without the photo, which could not be added. Add photos in "
        f"{_d('content/photos')}."
    )


def post_photo_not_added() -> str:
    """Held draft whose photo could not be attached."""
    return f"The photo could not be added. Add photos in {_d('content/photos')}."


def post_no_seat() -> str:
    return (
        "Nothing was posted. This workspace has no LinkedIn account connected. "
        f"Connect one at {_d('settings/accounts')}."
    )


def post_publish_failed() -> str:
    return (
        "Nothing was posted. LinkedIn did not accept the post. It is saved as a draft "
        f"in {_d('content/posts')}, so try Publish there again in a few minutes."
    )


def post_still_drafting() -> str:
    """The cloud saved the post but has not written its words yet."""
    return (
        "Nothing was posted yet. HeyLead is still writing the draft; it will be in "
        f"{_d('content/posts')}, where you can press Publish once it is ready."
    )


# ── answer_inbox reply, done by the cloud ──


def reply_sent(first_name: str) -> str:
    """"Message sent to Dana." -- or "Message sent." when the reply went by
    chat id and the api named nobody (it fills ``recipient`` only for a
    reply found by name)."""
    return f"Message sent to {first_name}." if first_name else "Message sent."


def reply_sending_paused() -> str:
    return (
        "Nothing was sent. Sending is paused for this workspace. Turn it back on in "
        f"{_d('settings/sending')}, then ask again."
    )


def reply_no_seat() -> str:
    return f"Nothing was sent. Connect LinkedIn at {_d('settings/accounts')} first."


REPLY_LINKEDIN_REFUSED = (
    "Nothing was sent. LinkedIn did not accept the message. Try again in a few "
    "minutes. If it fails again, the person may have left LinkedIn or removed you."
)
REPLY_CHAT_GONE = "Nothing was sent. That conversation no longer exists on LinkedIn."
REPLY_EMPTY = "Please provide `text` — the message to send."


def reply_name_not_found(name: str) -> str:
    return (
        f"Nothing was sent. No conversation with {name or 'that person'} was found. "
        "Check the name with inbox(action='list'), or pass chat_id."
    )


def reply_failed(detail: str = "") -> str:
    detail = (detail or "").strip()
    return f"Nothing was sent. {detail}" if detail else "Nothing was sent. Try again in a few minutes."


# ── Headline and About, done by the cloud ──

HEADLINE_APPLIED = "Your headline is updated on LinkedIn."
ABOUT_APPLIED = "Your About section is updated on LinkedIn."


def profile_text_failed() -> str:
    return (
        "Nothing was changed on your profile. LinkedIn did not confirm the update. "
        f"Check your profile, then try again from {_d('brand')}."
    )
