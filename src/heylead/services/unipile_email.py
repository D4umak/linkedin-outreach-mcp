"""Send email through a connected Unipile mailbox.

Operators and agents must use this path — never Mail.app / osascript.
Discovers a GOOGLE/OUTLOOK/MAIL account when ``email_account_id`` is unset,
and mints a hosted-auth link for those providers when none is connected.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

EMAIL_CONNECT_PROVIDERS = ("GOOGLE", "OUTLOOK", "MAIL")

_EMAIL_PROVIDERS = frozenset({
    "GOOGLE",
    "GMAIL",
    "OUTLOOK",
    "OFFICE365",
    "MICROSOFT",
    "MAIL",
    "IMAP",
    "SMTP",
    "YAHOO",
    "EXCHANGE",
})

_NO_MAILBOX = (
    "No Unipile email mailbox connected. Connect Gmail or Outlook with "
    "account(action='connect_email'). Do not use Mail.app."
)


def is_email_account(acc: dict[str, Any]) -> bool:
    """True when a Unipile account payload is a mailbox, not LinkedIn."""
    provider = str(
        acc.get("provider") or acc.get("provider_type") or acc.get("type") or ""
    ).upper()
    if not provider or "LINKEDIN" in provider:
        return False
    return any(token in provider for token in _EMAIL_PROVIDERS)


def _account_id(acc: dict[str, Any]) -> str:
    return str(
        acc.get("id")
        or acc.get("account_id")
        or acc.get("accountId")
        or acc.get("uuid")
        or ""
    ).strip()


def _mailbox_address(acc: dict[str, Any]) -> str:
    """The address Unipile lists on a mailbox — email / name / identifier."""
    from .prospect_email import extract_profile_email

    return extract_profile_email(
        acc.get("email"),
        acc.get("identifier"),
        acc.get("name"),
        acc,
    ).strip()


def _norm_email(value: Any) -> str:
    from .prospect_email import extract_profile_email

    return extract_profile_email(value).strip().lower()


async def _sending_identity(explicit: str = "") -> str:
    """Campaign ``from_email`` if given, else the profile email."""
    from ..db import aio as db

    found = _norm_email(explicit)
    if found:
        return found
    profile = await db.get_setting("profile", {}) or {}
    return _norm_email(profile)


def _is_healthy(acc: dict[str, Any]) -> bool:
    """Whether Unipile reports this mailbox as usable.

    ``interpret_account_status`` reads ``sources[].status``, which is where the
    real signal lives — the legacy top-level field is rarely populated and
    reports credentials-expired accounts as connected. Binding a zombie
    mailbox flips has_email_channel() on and routes outreach into something
    that cannot send.
    """
    from ..linkedin.unipile import interpret_account_status

    ok, _ = interpret_account_status(acc)
    return ok


async def _clear_email_account_id() -> None:
    """Unbind the stored mailbox. Nothing else in the tree could do this."""
    from ..db import aio as db
    from ..db.async_bridge import run_db
    from .channel_selector import refresh_email_account_cache

    await db.save_setting("email_account_id", "")
    await run_db(refresh_email_account_cache)


async def _persist_email_account_id(account_id: str) -> None:
    from ..db import aio as db
    from ..db.async_bridge import run_db
    from .channel_selector import refresh_email_account_cache

    await db.save_setting("email_account_id", account_id)
    await run_db(refresh_email_account_cache)


async def _list_mailboxes(client: Any) -> list[dict[str, Any]]:
    from ..linkedin import UnipileError

    try:
        accounts = await client.list_accounts()
    except UnipileError:
        # Re-raised unchanged so the backend's own actionable text survives —
        # "Backend JWT expired or invalid. Run setup_profile again."
        raise
    except Exception as e:
        # "Could not ask" is not "no mailbox". Returning "" here told a user
        # whose token had expired to connect a mailbox they already had, and
        # threw away the only message that named the real remedy. Same rule
        # detect_sales_navigator follows: an unreachable API is an error, not
        # an answer.
        raise UnipileError(f"Could not check for a connected mailbox: {e}") from e
    return [
        a for a in (accounts or []) if is_email_account(a) and _is_healthy(a)
    ]


async def resolve_email_account_id(
    client: Any,
    *,
    identity_email: str = "",
    from_email: str = "",
) -> str:
    """Return a Unipile mailbox id that matches the sending identity.

    Identity is ``identity_email`` / ``from_email`` if given, else the
    profile email. A leftover ``email_account_id`` (or hosted bind) that
    does not match that address must not send.

    With no identity, discovery is unchanged: stored / hosted / the one
    healthy mailbox. Two live mailboxes is not a guess.
    """
    from ..db import aio as db
    from . import channel_selector

    identity = await _sending_identity(identity_email or from_email)
    if identity:
        return await _resolve_by_identity(client, identity)

    cached = channel_selector._EMAIL_ACCOUNT_CACHE
    if cached:
        return cached

    stored = await db.get_setting("email_account_id", "") or ""
    if stored:
        await _persist_email_account_id(stored)
        return stored

    if hasattr(client, "get_user_info"):
        try:
            info = await client.get_user_info()
            eid = str((info or {}).get("email_account_id") or "").strip()
            if eid:
                await _persist_email_account_id(eid)
                return eid
        except Exception:
            logger.debug("get_user_info did not yield an email_account_id", exc_info=True)

    candidates = await _list_mailboxes(client)
    if len(candidates) > 1:
        logger.warning(
            "Email discovery found %d live mailboxes and will not guess between "
            "them. Choose one with account(action='set_email_account', "
            "account_id='...'); account(action='list') shows the ids.",
            len(candidates),
        )
        return ""
    if candidates:
        eid = _account_id(candidates[0])
        if eid:
            await _persist_email_account_id(eid)
            return eid
    return ""


async def _resolve_by_identity(client: Any, identity: str) -> str:
    """Pick the mailbox whose address equals who we are sending as."""
    from ..db import aio as db

    candidates = await _list_mailboxes(client)
    matches = [
        a for a in candidates if _mailbox_address(a).lower() == identity
    ]

    stored = await db.get_setting("email_account_id", "") or ""
    if stored:
        stored_acc = next((a for a in candidates if _account_id(a) == stored), None)
        if stored_acc and _mailbox_address(stored_acc).lower() == identity:
            await _persist_email_account_id(stored)
            return stored

    if hasattr(client, "get_user_info"):
        try:
            info = await client.get_user_info()
        except Exception:
            logger.debug("get_user_info did not yield an email_account_id", exc_info=True)
            info = None
        hosted = str((info or {}).get("email_account_id") or "").strip()
        if hosted:
            hosted_acc = next(
                (a for a in candidates if _account_id(a) == hosted), None,
            )
            listed_addr = _mailbox_address(hosted_acc).lower() if hosted_acc else ""
            me_email = _norm_email(info)
            if hosted_acc and listed_addr == identity:
                await _persist_email_account_id(hosted)
                return hosted
            if me_email == identity and (not hosted_acc or not listed_addr):
                # Hosted mailbox is absent from the listing (live shape) or
                # listed without an address — /me email is the identity check.
                await _persist_email_account_id(hosted)
                return hosted
            # Hosted id is a different address — do not persist it.

    if len(matches) == 1:
        eid = _account_id(matches[0])
        if eid:
            await _persist_email_account_id(eid)
            return eid
    if len(matches) > 1:
        logger.warning(
            "Email discovery found %d mailboxes matching %s and will not guess. "
            "Choose one with account(action='set_email_account').",
            len(matches), identity,
        )
        return ""
    logger.warning(
        "No mailbox matches sending identity %s. Choose one with "
        "account(action='set_email_account').",
        identity,
    )
    return ""


async def _stored_mailbox_is_healthy(client: Any, stored: str) -> bool:
    """Whether the bound mailbox still works.

    A failure to check returns True: an unreachable API must not destroy a
    working binding — same rule the entitlement work landed.
    """
    try:
        accounts = await client.list_accounts()
    except Exception:
        logger.debug("could not re-validate the stored mailbox", exc_info=True)
        return True
    for acc in accounts or []:
        if _account_id(acc) == stored:
            return _is_healthy(acc)
    # Absent from the listing is NOT proof it is gone. The hosted binding comes
    # from get_user_info() and does not appear in list_accounts at all —
    # verified live on 22 Aug, where the workspace lists one LinkedIn account
    # while the backend reports a bound mailbox. Treating that as a dead
    # mailbox threw away a working one, which is the absent-as-denied collapse
    # in the one branch of this function that otherwise gets the rule right.
    # Only present-and-unhealthy is a confirmed dead mailbox.
    return True


async def connect_email_if_needed(client: Any) -> str:
    """Return a hosted-auth URL when there is no usable mailbox, else "".

    Re-validates the stored binding first. Without this, a mailbox whose
    credentials expired was a permanent dead end: the stored id short-circuited
    every resolve, so connect_email() answered "already connected" and never
    minted a link, and nothing in the tree could clear the setting.
    """
    from ..db import aio as db

    stored = await db.get_setting("email_account_id", "") or ""
    if stored:
        if await _stored_mailbox_is_healthy(client, stored):
            return ""
        logger.warning(
            "Stored mailbox %s… is no longer usable — unbinding so a new one "
            "can be connected", stored[:8],
        )
        await _clear_email_account_id()

    # Deliberately NOT wrapped in a bare except: swallowing here would
    # re-collapse "could not ask" into "no mailbox connected" and bury the
    # actionable remedy, which is the exact conflation resolve_email_account_id
    # was changed to stop making. An unreachable API is an error, not an answer.
    if await resolve_email_account_id(client):
        return ""
    return await connect_email_link(client)


async def bind_email_account(client: Any, account_id: str) -> str:
    """Bind a specific mailbox, answering the ambiguity refusal.

    Discovery declines to choose between two live mailboxes — correct, but only
    if the user can answer. Without this there was nothing in the tree that
    could bind a chosen one, so a workspace holding both Gmail and Outlook lost
    the email channel permanently while every message advised connecting
    another, which would have made it worse.

    Validated against the live workspace: binding an id that is not a healthy
    mailbox is the same mistake as guessing, with extra steps.
    """
    account_id = (account_id or "").strip()
    if not account_id:
        return "Error: 'account_id' is required."

    accounts = await client.list_accounts()
    for acc in accounts or []:
        if _account_id(acc) != account_id:
            continue
        if not is_email_account(acc):
            return f"❌ `{account_id}` is not a mailbox — it looks like a LinkedIn account."
        if not _is_healthy(acc):
            return (
                f"❌ `{account_id}` is not usable — Unipile reports its "
                "credentials as expired. Reconnect it, then try again."
            )
        await _persist_email_account_id(account_id)
        return (
            f"✅ Outbound email will send from `{account_id}`.\n\n"
            "Use send_email(to=..., subject=..., body=...) to send via Unipile."
        )
    return (
        f"❌ No mailbox `{account_id}` on this workspace. "
        "Use account(action='connect_email') to attach one."
    )


async def connect_email_link(client: Any) -> str:
    """Mint a hosted-auth URL for Gmail/Outlook (never LinkedIn-only)."""
    return await client.create_hosted_auth_link(
        providers=list(EMAIL_CONNECT_PROVIDERS),
    )


async def connect_email() -> str:
    """MCP helper: connect a Unipile mailbox or report one already bound."""
    from ..linkedin import UnipileError, get_linkedin_client

    try:
        client = get_linkedin_client()
    except UnipileError as e:
        return f"❌ {e}"

    try:
        # Re-validates the stored binding rather than trusting it: a mailbox
        # whose credentials expired used to answer "already connected" here
        # for ever, with no way to attach a new one.
        url = await connect_email_if_needed(client)
        if not url:
            existing = await resolve_email_account_id(client)
            return (
                f"Email mailbox already connected ({existing[:8]}…).\n"
                "Use send_email(to=..., subject=..., body=...) to send via Unipile."
            )
        return (
            "Open this link to connect Gmail or Outlook to HeyLead (Unipile).\n\n"
            f"  {url}\n\n"
            "Do not use Mail.app. After connecting, run send_email(...) again."
        )
    except UnipileError as e:
        return f"❌ {e}"
    except Exception as e:
        logger.error("connect_email failed: %s", e, exc_info=True)
        return f"❌ Could not create an email connect link: {e}"
    finally:
        await client.close()


async def send_unipile_email(
    *,
    to_email: str,
    subject: str,
    body: str,
    to_name: str = "",
    client: Any | None = None,
) -> dict[str, Any]:
    """Send one email through Unipile. Never falls back to a local mailer.

    Returns ``{success, email_id, error, connect_url?}``.
    """
    from ..linkedin import UnipileError, get_linkedin_client

    own_client = client is None
    if own_client:
        try:
            client = get_linkedin_client()
        except UnipileError as e:
            return {"success": False, "email_id": "", "error": str(e)}

    try:
        account_id = await resolve_email_account_id(client)
        if not account_id:
            connect_url = ""
            try:
                connect_url = await connect_email_link(client)
            except Exception:
                logger.debug("could not mint email connect URL", exc_info=True)
            return {
                "success": False,
                "email_id": "",
                "error": _NO_MAILBOX,
                "connect_url": connect_url,
            }
        result = await client.send_email(
            account_id=account_id,
            to_email=to_email,
            to_name=to_name or to_email.split("@")[0],
            subject=subject,
            body=body,
        )
        return result
    except UnipileError as e:
        return {"success": False, "email_id": "", "error": str(e)}
    except Exception as e:
        logger.error("send_unipile_email failed: %s", e, exc_info=True)
        return {"success": False, "email_id": "", "error": f"Email send failed: {e}"}
    finally:
        if own_client:
            await client.close()


def email_send_known_denied(result: dict[str, Any] | None) -> bool:
    """True when Unipile is sure nothing was delivered (429 or auth)."""
    if not result:
        return False
    return bool(result.get("blocked") or result.get("auth_error"))


def sent_mail_matches(mail: dict[str, Any], *, to_email: str, subject: str) -> bool:
    """A SENT item counts only when subject and recipient both match.

    Subject alone is not enough — that would treat an earlier mail to someone
    else as proof this send landed.
    """
    if (mail.get("subject") or "").strip().lower() != (subject or "").strip().lower():
        return False
    want = (to_email or "").strip().lower()
    if not want:
        return False
    recipients: list[str] = []
    for key in ("to_emails", "to"):
        raw = mail.get(key) or []
        if isinstance(raw, str):
            recipients.append(raw)
        elif isinstance(raw, list):
            for item in raw:
                if isinstance(item, str):
                    recipients.append(item)
                elif isinstance(item, dict):
                    recipients.append(
                        item.get("identifier") or item.get("email") or ""
                    )
    for att in mail.get("to_attendees") or []:
        if isinstance(att, dict):
            recipients.append(att.get("identifier") or att.get("email") or "")
    return want in {r.strip().lower() for r in recipients if r}


async def confirm_email_in_sent(
    client: Any,
    account_id: str,
    to_email: str,
    subject: str,
) -> dict[str, Any] | None:
    """Read the SENT folder after an unknown send. Never POSTs again."""
    try:
        emails = await client.list_emails(account_id, limit=25, folder="SENT")
    except Exception as e:
        logger.warning("SENT folder check failed: %s", e)
        return None
    for mail in emails or []:
        if isinstance(mail, dict) and sent_mail_matches(
            mail, to_email=to_email, subject=subject,
        ):
            return mail
    return None


MAILBOX_DISCONNECT_HEADING = "⚠️ **Email mailbox disconnected**"


def mailbox_disconnect_lines(
    *,
    stored_id: str,
    accounts: list[dict[str, Any]] | None,
    check_failed: bool,
    last_send_said_disconnected: bool,
) -> list[str]:
    """User-facing lines when the mailbox is confirmed dead. Else empty.

    Absent-from-list is not proof: hosted bindings often never appear in
    ``list_accounts``. An unreachable API is not a disconnect either. A send
    that already came back ``Email account disconnected`` is enough on its own.
    """
    confirmed = False
    if not check_failed and stored_id and accounts is not None:
        for acc in accounts:
            if _account_id(acc) == stored_id and not _is_healthy(acc):
                confirmed = True
                break
    if not confirmed and last_send_said_disconnected:
        confirmed = True
    if not confirmed:
        return []
    return [
        MAILBOX_DISCONNECT_HEADING,
        "   Gmail/Outlook is no longer usable. Email overflow will sit until you reconnect.",
        "   Run: account(action='connect_email')",
        "",
    ]


def last_send_said_email_disconnected() -> bool:
    """Whether a send already recorded the mailbox as disconnected."""
    from ..db.schema import get_db

    db = get_db()
    try:
        row = db.execute(
            "SELECT 1 FROM outreaches "
            "WHERE last_attempt_error LIKE 'Email account disconnected%' "
            "LIMIT 1",
        ).fetchone()
        return row is not None
    finally:
        db.close()


async def mailbox_disconnect_banner() -> list[str]:
    """Gather live mailbox health and return dashboard lines, or nothing."""
    from ..db import aio as db
    from ..db.async_bridge import run_db
    from ..linkedin import get_linkedin_client

    stored = await db.get_setting("email_account_id", "") or ""
    last_send = False
    try:
        last_send = await run_db(last_send_said_email_disconnected)
    except Exception:
        last_send = False

    accounts: list[dict[str, Any]] | None = None
    check_failed = False
    try:
        client = get_linkedin_client()
        try:
            accounts = await client.list_accounts()
        finally:
            await client.close()
    except Exception:
        check_failed = True
        accounts = None

    return mailbox_disconnect_lines(
        stored_id=stored,
        accounts=accounts,
        check_failed=check_failed,
        last_send_said_disconnected=last_send,
    )
