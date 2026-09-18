"""Channel selector — decide LinkedIn vs Email for each prospect.

Decision logic:
1. If no email account connected → LinkedIn only
2. If LinkedIn rate limits hit + prospect has email → Email overflow
3. If prospect has email + LinkedIn unanswered for 14 days → Email fallback
4. If campaign explicitly sets channel preference → respect it
5. Default: LinkedIn first (higher acceptance rate for warm outreach)
"""

from __future__ import annotations

import logging
import time

from ..db.queries import get_outreach, get_setting

logger = logging.getLogger(__name__)

# Days before falling back to email after LinkedIn silence
EMAIL_FALLBACK_DAYS = 14
EMAIL_FALLBACK_SECONDS = EMAIL_FALLBACK_DAYS * 86400

# Channel constants
CHANNEL_LINKEDIN = "linkedin"
CHANNEL_EMAIL = "email"


# Module cache so select_channel() is callable from the event loop thread.
# get_db() deliberately raises there, and the invitation executor calls
# select_channel() on the loop — warm this in sync context at startup
# (prepare_process / server.main), exactly like the account-id cache.
# None = not loaded yet; "" = loaded, no email account connected.
_EMAIL_ACCOUNT_CACHE: str | None = None


def _read_email_account_setting() -> str:
    """The one place the setting key and its default live.

    Both the cold read and the refresh go through here so a change to either
    cannot apply to one caller and not the other. Sync DB call — only ever
    reached off the event loop thread.
    """
    return get_setting("email_account_id", "") or ""


def get_email_account_id() -> str:
    """Get the connected email account ID (cached after the first read)."""
    global _EMAIL_ACCOUNT_CACHE
    if _EMAIL_ACCOUNT_CACHE is None:
        _EMAIL_ACCOUNT_CACHE = _read_email_account_setting()
    return _EMAIL_ACCOUNT_CACHE


def refresh_email_account_cache() -> str:
    """Re-read the setting; call after connecting or unlinking an email account.

    Reads before assigning so the cache is never momentarily None: this runs on
    the DB thread while the event loop runs concurrently, and a cold read there
    would issue a sync DB call from the loop thread.
    """
    global _EMAIL_ACCOUNT_CACHE
    value = _read_email_account_setting()
    _EMAIL_ACCOUNT_CACHE = value
    return value


def has_email_channel() -> bool:
    """Check if an email account is connected and available."""
    return bool(get_email_account_id())


def select_channel(
    outreach: dict | None = None,
    prospect: dict | None = None,
    campaign_config: dict | None = None,
    force_channel: str = "",
    linkedin_limit_hit: bool = False,
) -> str:
    """Select the best channel for this outreach.

    Args:
        outreach: Existing outreach record (if any).
        prospect: Prospect/contact data.
        campaign_config: Campaign config_json.
        force_channel: Override channel choice.
        linkedin_limit_hit: If True, LinkedIn daily/weekly limits are hit.
            Email will be preferred for prospects who have email addresses.

    Returns:
        "linkedin" or "email"
    """
    # Explicit override
    if force_channel == "dm":
        return "dm"  # Direct message to existing connection
    if force_channel in (CHANNEL_LINKEDIN, CHANNEL_EMAIL):
        if force_channel == CHANNEL_EMAIL and not has_email_channel():
            return CHANNEL_LINKEDIN
        return force_channel

    # Campaign-level preference
    if campaign_config:
        pref = campaign_config.get("channel", "")
        if pref == CHANNEL_EMAIL and has_email_channel():
            return CHANNEL_EMAIL
        if pref == CHANNEL_LINKEDIN and not linkedin_limit_hit:
            return CHANNEL_LINKEDIN

    # No email account → LinkedIn only (even if limited)
    if not has_email_channel():
        return CHANNEL_LINKEDIN

    # LinkedIn limit overflow → email for prospects with email addresses
    if linkedin_limit_hit:
        if prospect:
            email = _extract_email(prospect)
            if email:
                logger.info(
                    "LinkedIn limit hit — routing to email for %s",
                    prospect.get("name", "unknown"),
                )
                return CHANNEL_EMAIL
        # No email address available → LinkedIn (will be queued for later)
        return CHANNEL_LINKEDIN

    # If this outreach has been on LinkedIn for 14+ days with no reply → email
    if outreach:
        channel = outreach.get("channel") or CHANNEL_LINKEDIN
        if channel == CHANNEL_LINKEDIN:
            invited_at = outreach.get("invited_at") or 0
            status = outreach.get("status", "")
            # Only fall back if invited but never got reply/accept
            if (
                invited_at
                and status == "invited"
                and (time.time() - invited_at) > EMAIL_FALLBACK_SECONDS
            ):
                return CHANNEL_EMAIL

    # Check if prospect has a known email address
    if prospect:
        email = _extract_email(prospect)
        if not email:
            return CHANNEL_LINKEDIN

    # Default: LinkedIn first
    return CHANNEL_LINKEDIN


def should_email_fallback(outreach_id: str) -> bool:
    """Check if an outreach should fall back to email.

    Returns True if:
    - Email account is connected
    - Outreach was sent via LinkedIn 14+ days ago
    - No reply received
    """
    if not has_email_channel():
        return False

    outreach = get_outreach(outreach_id)
    if not outreach:
        return False

    channel = outreach.get("channel") or CHANNEL_LINKEDIN
    if channel != CHANNEL_LINKEDIN:
        return False

    invited_at = outreach.get("invited_at") or 0
    status = outreach.get("status", "")

    # Only fall back if invited but no response
    if invited_at and status == "invited":
        return (time.time() - invited_at) > EMAIL_FALLBACK_SECONDS

    return False


def _extract_email(prospect: dict) -> str:
    """Try to extract email from prospect profile data."""
    from .prospect_email import extract_profile_email

    return extract_profile_email(prospect or {})
