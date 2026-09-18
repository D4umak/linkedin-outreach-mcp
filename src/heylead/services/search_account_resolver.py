"""Search Account Resolver — find the best LinkedIn account for search operations.

Automatically detects Sales Navigator accounts among the seats this workspace
can reach and returns the optimal account for searching, separate from the
user's sending account.

Priority:
1. Hosted balancer (pick_search_account): least-used pooled Premium/SN seat
2. Cached premium search account from settings (re-detect every 24 hours)
3. Auto-detected Sales Nav account among reachable seats (prefer non-sending)
4. Fallback: user's own active account

Steps 1 and 3 both narrow with pool membership, and both already degrade the
right way. The shared pool is reciprocal, so it lends a pooled seat only to a
workspace that contributes one: for a non-contributor the balancer answers
with no account and check_sales_nav_all lists that workspace's own seat alone,
which walks the resolver down to step 4 — its own seat — rather than failing.
That is a smaller answer, not an error, so nothing here treats it as one.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..db.async_bridge import run_db
from ..db.queries import get_setting, save_setting
from ..tier import as_bool as _as_bool  # canonical coercion lives with the tier matrix

logger = logging.getLogger(__name__)

# Re-detect Sales Nav accounts every 24 hours
# Same period as the daily re-detect job, imported so the probe cadence and
# the cache lifetime cannot drift apart.
from ..constants import SN_REDETECT_SECONDS as _CACHE_TTL_SECONDS


# Unipile's refusal when the account holds no Sales Navigator entitlement.
# Matched on the stable machine-readable type, not the prose title.
_NO_ENTITLEMENT_MARKERS = ("feature_not_subscribed", "not been subscribed")


async def _clear_inmail_verdict() -> None:
    """Forget what a send proved once the entitlement behind it changed."""
    from ..db.queries import delete_setting
    from ..tier import INMAIL_CAPABILITY_KEY, _caps_cache

    await run_db(delete_setting, INMAIL_CAPABILITY_KEY)
    _caps_cache.clear()
    logger.info("Entitlements changed — cleared the learned InMail verdict")


async def _stored_can_send_credit_inmail() -> bool:
    """Credit InMail from the flags currently on disk — SN, Premium, verdict."""
    from ..tier import INMAIL_CAPABILITY_KEY, caps_for

    stored = await run_db(get_setting, "has_sales_navigator", False)
    premium_raw = await run_db(get_setting, "has_linkedin_premium", None)
    premium = None if premium_raw is None else _as_bool(premium_raw)
    capability = str(await run_db(get_setting, INMAIL_CAPABILITY_KEY, "") or "")
    return caps_for(_as_bool(stored), premium, capability).can_send_credit_inmail


async def _explain_absent_entitlement(
    client: Any, account_id: str, result: dict[str, Any],
) -> None:
    """Say WHICH of the two causes produced feature_not_subscribed.

    Unipile returns one error type for both, and says so in its own detail
    string: "either not been subscribed or not been authenticated properly".
    The stored flag is the same either way — Sales Navigator is unusable
    through Unipile in both cases — but the remedy is not, and "no Sales
    Navigator" is a dead end where "unreachable, reconnect" is an action.

    premiumFeatures records what Unipile associated with the account and has
    never been observed to change afterwards, so it separates them:
    bound-then-failing is a real lapse; never-bound is consistent with a
    licence acquired after the account was linked.

    The wording deliberately stops short of prescribing a reconnect. That
    remedy rests on OUR theory of Unipile's binding behaviour, not on its
    documentation: the reconnect endpoint is documented for accounts that are
    DISCONNECTED, nothing documents premiumFeatures being refreshed by it, and
    reconnecting re-supplies credentials — so on a healthy session a
    checkpoint or 2FA failure could leave the user worse off than a wrong tier
    flag. Say what is observed, name the likely cause, and let the user choose.

    Best effort — the hint only changes the wording, never the verdict, so a
    transport that cannot supply it costs nothing.
    """
    features: list | None = None
    getter = getattr(client, "premium_features", None)
    if getter is not None:
        try:
            features = list(await getter(account_id) or [])
        except Exception as e:
            logger.debug("premium features hint unavailable: %s", e)

    if features is not None and "sales_navigator" in features:
        result["errors"].append(
            "Sales Navigator was active on this connection and no longer is — "
            "the licence appears to have lapsed."
        )
        logger.info("Sales Navigator confirmed absent by live search (lapsed licence)")
        return

    # Unknown metadata lands here too: never-bound is the common cause, and a
    # wasted reconnect costs a minute where a missed one costs the licence.
    result["errors"].append(
        "No Sales Navigator seat on this LinkedIn account: LinkedIn refused an "
        "SN search and the account reports no SN entitlement. If you believe "
        "you hold a licence, check that it is on THIS LinkedIn account and "
        "that the seat is assigned to you, then re-run "
        "account(action='refresh_tier')."
    )
    logger.warning(
        "Sales Navigator unreachable and no SN entitlement on this connection",
    )


async def _confirm_no_sales_navigator(
    client: Any, account_id: str, result: dict[str, Any],
) -> bool | None:
    """Ask LinkedIn directly whether Sales Navigator still works.

    Returns True (it works — refuse the downgrade), False (LinkedIn itself
    says the feature is not subscribed), or None (no verdict; write nothing).

    A one-result search is the authoritative check: unlike cached account
    metadata it reflects the licence as it stands right now.
    """
    try:
        await client.search_people(
            account_id=account_id,
            keywords="test",
            count=1,
            use_sales_navigator=True,
            raise_on_error=True,
        )
    except Exception as e:
        text = str(e).lower()
        if any(marker in text for marker in _NO_ENTITLEMENT_MARKERS):
            await _explain_absent_entitlement(client, account_id, result)
            return False
        result["errors"].append(f"downgrade corroboration inconclusive: {e}")
        logger.warning(
            "redetect_tier: detector said no but the live search gave no "
            "verdict — keeping the stored licence: %s", e,
        )
        return None
    logger.warning(
        "redetect_tier: detector reported no Sales Navigator but a live "
        "search succeeded — keeping the licence. The detector is reading "
        "stale metadata.",
    )
    return True


async def redetect_tier(client: Any, sending_account_id: str) -> dict[str, Any]:
    """Re-probe Sales Navigator and persist only confirmed verdicts.

    The stored tier flags self-heal here: a confirmed True upgrades, a
    confirmed False downgrades (a lapsed licence must stop driving 300-char
    notes and SN-depth searches). A probe that cannot deliver a verdict
    writes NOTHING — unlike setup_profile's save-False-on-exception, a
    refresher that hit a transient error must not clobber a valid stored True.

    Returns:
        dict with:
            has_sales_navigator: bool | None — None means the probe failed
                and nothing was written.
            premium_search_account_id: str — '' when none cached/pinned.
            premium_search_has_sn: bool | None — None means not probed or
                probe failed; nothing was written.
            errors: list[str] — probe failures, for the caller to surface.
    """
    result: dict[str, Any] = {
        "has_sales_navigator": None,
        "premium_search_account_id": "",
        "premium_search_has_sn": None,
        "errors": [],
    }

    # Cancel leftover first-touch jobs only when credit InMail capability
    # actually changes. Sales Navigator can flip while Premium still grants
    # credits — that must not wipe pending InMails.
    old_can_credit = await _stored_can_send_credit_inmail()

    # Premium is recorded alongside the seat: it grants InMail credits, so the
    # InMail escalation must not be gated on Sales Navigator alone. A change in
    # entitlements also clears any learned send verdict — "refused" was an
    # answer about the old entitlement state, not a permanent property.
    getter = getattr(client, "account_entitlements", None)
    if getter is not None:
        try:
            ent = await getter(sending_account_id)
        except Exception as e:
            logger.debug("entitlement read failed: %s", e)
            ent = None
        if ent is not None and ent.get("premium") is not None:
            previous = await run_db(get_setting, "has_linkedin_premium", None)
            await run_db(save_setting, "has_linkedin_premium", ent["premium"])
            if previous is not None and _as_bool(previous) != ent["premium"]:
                await _clear_inmail_verdict()

    sending_sn: bool | None = None
    try:
        sending_sn = bool(await client.detect_sales_navigator(sending_account_id))
    except Exception as e:
        result["errors"].append(f"sending-account probe failed: {e}")
        logger.warning(
            "redetect_tier: sending-account probe failed — keeping stored tier: %s", e
        )

    if sending_sn is False and _as_bool(
        await run_db(get_setting, "has_sales_navigator", False)
    ):
        if getattr(client, "trusts_composed_tier", False):
            # Hosted composer already applied LinkedIn signals + override.
            # An SN search 403 must not flip the stored seat.
            pass
        else:
            # Direct Unipile: corroborate a downgrade. The search still
            # cannot tell "no licence" from "session not authenticated";
            # _confirm_no_sales_navigator keeps stored SN on ambiguity.
            sending_sn = await _confirm_no_sales_navigator(
                client, sending_account_id, result,
            )

    if sending_sn is not None:
        previous_sn = await run_db(get_setting, "has_sales_navigator", None)
        await run_db(save_setting, "has_sales_navigator", sending_sn)
        result["has_sales_navigator"] = sending_sn
        if previous_sn is not None and _as_bool(previous_sn) != sending_sn:
            await _clear_inmail_verdict()

    premium_id = str(await run_db(get_setting, "premium_search_account_id", "") or "")
    result["premium_search_account_id"] = premium_id

    if premium_id:
        premium_sn: bool | None = None
        if premium_id == sending_account_id:
            premium_sn = sending_sn  # same account — one probe answers both
        else:
            try:
                premium_sn = bool(await client.detect_sales_navigator(premium_id))
            except Exception as e:
                result["errors"].append(f"search-account probe failed: {e}")
                logger.warning(
                    "redetect_tier: search-account probe failed — keeping stored flag: %s", e
                )
        if premium_sn is not None:
            await run_db(save_setting, "premium_search_has_sn", premium_sn)
            await run_db(save_setting, "premium_search_detected_at", int(time.time()))
            result["premium_search_has_sn"] = premium_sn
    elif sending_sn:
        # Sending account has SN and nothing is cached — self-populate so SN
        # search depth kicks in without waiting for the next resolver run.
        await run_db(save_setting, "premium_search_account_id", sending_account_id)
        await run_db(save_setting, "premium_search_has_sn", True)
        await run_db(save_setting, "premium_search_detected_at", int(time.time()))
        result["premium_search_account_id"] = sending_account_id
        result["premium_search_has_sn"] = True

    new_can_credit = await _stored_can_send_credit_inmail()
    if old_can_credit != new_can_credit:
        from ..db.queries import cancel_stale_first_touch_jobs
        await run_db(
            cancel_stale_first_touch_jobs,
            lost_credit_inmail=old_can_credit and not new_can_credit,
            gained_credit_inmail=new_can_credit and not old_can_credit,
        )

    return result


async def resolve_search_account(
    client: Any,
    sending_account_id: str,
) -> tuple[str, bool]:
    """Resolve the best account for LinkedIn search.

    Checks for a cached premium search account, then auto-detects by
    scanning all workspace accounts for Sales Navigator access.

    Args:
        client: BackendClient or UnipileClient instance.
        sending_account_id: The user's active account used for sending.

    Returns:
        (search_account_id, has_sales_nav) tuple.
        search_account_id may equal sending_account_id if no premium found.
    """
    # Hosted balancer: least-used SN/Premium, never a 7-day pinned id.
    # object.__getattribute__ so a cache-only fake that traps __getattr__
    # is not treated as having a picker.
    try:
        picker = object.__getattribute__(client, "pick_search_account")
    except AttributeError:
        picker = None
    if callable(picker):
        try:
            picked = await picker()
        except Exception as e:
            logger.warning("pick_search_account failed: %s", e)
            picked = None
        if isinstance(picked, dict) and picked.get("account_id"):
            has_sn = bool(picked.get("has_sales_navigator"))
            logger.info(
                "Search account from hosted balancer: %s (sn=%s)",
                str(picked["account_id"])[:8], has_sn,
            )
            return str(picked["account_id"]), has_sn

    # 1. Check cache. Cache presence is a stored detection result, never
    #    proof of Sales Navigator — the tier bit is its own stored fact.
    cached_id = await run_db(get_setting, "premium_search_account_id", "")
    cached_at = await run_db(get_setting, "premium_search_detected_at", 0)

    if cached_at and (time.time() - cached_at) < _CACHE_TTL_SECONDS:
        age = time.time() - cached_at
        if cached_id:
            stored_sn = await run_db(get_setting, "premium_search_has_sn", None)
            if stored_sn is None:
                # Pre-upgrade installs cached the account id before this key
                # existed. Absent means unknown, never False — falling through
                # to detection is what writes it; returning False here would
                # silently downgrade every existing SN install to classic
                # search for a full TTL.
                logger.info(
                    "Cached premium account %s has no stored tier bit — re-detecting",
                    cached_id[:8],
                )
            else:
                has_sn = _as_bool(stored_sn)
                logger.info(
                    "Using cached premium search account %s (age: %dd, sales_nav=%s)",
                    cached_id[:8], int(age / 86400), has_sn,
                )
                return cached_id, has_sn
        # A fresh empty id is the cached negative written below: no premium
        # account exists, searches stay on the sending account. Without this
        # branch the negative cache was dead — the hit path demanded a truthy
        # id, so every campaign re-ran the full scan it was meant to avoid.
        return sending_account_id, _as_bool(
            await run_db(get_setting, "has_sales_navigator", False)
        )

    # 2. Auto-detect: scan all accounts for Sales Navigator
    logger.info("Auto-detecting Sales Navigator accounts...")

    from ..linkedin.backend_client import BackendClient

    scan_incomplete = False
    if isinstance(client, BackendClient):
        # Backend mode: use the dedicated endpoint that checks all pool accounts
        try:
            results = await client.check_sales_nav_all()
            sales_nav_accounts = [
                r for r in results if r.get("has_sales_navigator")
            ]
        except Exception as e:
            logger.warning("check_sales_nav_all failed: %s", e)
            sales_nav_accounts = []
            scan_incomplete = True
    else:
        # Direct Unipile mode: iterate all accounts manually
        sales_nav_accounts = []
        try:
            accounts = await client.list_accounts()
            for acc in accounts:
                acc_id = acc.get("id") or acc.get("account_id") or ""
                if not acc_id:
                    continue
                provider = acc.get("provider") or acc.get("provider_type") or ""
                if "LINKEDIN" not in str(provider).upper():
                    continue
                try:
                    has_sn = await client.detect_sales_navigator(acc_id)
                    if has_sn:
                        sales_nav_accounts.append({
                            "account_id": acc_id,
                            "name": acc.get("name", ""),
                            "has_sales_navigator": True,
                        })
                except Exception as e:
                    # No verdict for this account. Skipping it is right, but
                    # the scan can no longer claim it saw everything.
                    logger.debug("SN probe gave no verdict for %s: %s", acc_id[:8], e)
                    scan_incomplete = True
                    continue
        except Exception as e:
            logger.warning("Failed to list accounts for Sales Nav detection: %s", e)
            scan_incomplete = True

    if not sales_nav_accounts:
        logger.info("No Sales Navigator accounts found — using sending account")
        # Check if sending account itself has Sales Nav. A probe that cannot
        # deliver a verdict must not become a cached 7-day negative: answer
        # from the stored flag and leave the cache unstamped so the next call
        # re-detects.
        try:
            own_sn = await client.detect_sales_navigator(sending_account_id)
        except Exception as e:
            logger.warning("Sending-account SN probe gave no verdict: %s", e)
            return sending_account_id, _as_bool(
                await run_db(get_setting, "has_sales_navigator", False)
            )
        # Confirmed verdict for the sending account: persist it where the
        # cache-hit paths read it.
        await run_db(save_setting, "has_sales_navigator", own_sn)
        await run_db(save_setting, "premium_search_has_sn", own_sn)
        await run_db(
            save_setting,
            "premium_search_account_id",
            sending_account_id if own_sn else "",
        )
        if scan_incomplete and not own_sn:
            # Some account in the workspace never answered, so "no premium
            # account exists" is not established — leaving the stamp unwritten
            # costs a re-scan next call and avoids hiding a real search
            # account for the 7-day TTL.
            logger.info("SN scan incomplete — not caching the negative result")
            return sending_account_id, own_sn
        await run_db(save_setting, "premium_search_detected_at", int(time.time()))
        return sending_account_id, own_sn

    # 3. Pick the best one: prefer non-sending account to avoid rate limit conflicts
    best = None
    for acc in sales_nav_accounts:
        aid = acc.get("account_id", "")
        if aid != sending_account_id:
            best = aid
            break

    # If all Sales Nav accounts are the sending account, use it
    if not best:
        best = sales_nav_accounts[0].get("account_id", sending_account_id)

    # 4. Cache result — the tier bit is stored alongside the id, so cache
    #    hits report a recorded detection instead of asserting SN.
    await run_db(save_setting, "premium_search_account_id", best)
    await run_db(save_setting, "premium_search_has_sn", True)
    await run_db(save_setting, "premium_search_detected_at", int(time.time()))

    logger.info(
        "Premium search account resolved: %s (from %d Sales Nav accounts)",
        best[:8], len(sales_nav_accounts),
    )
    return best, True


def get_cached_search_account() -> str:
    """Return the cached premium search account ID, or empty string if none.

    Used by ICP enrichment to route LinkedIn code lookups through the
    premium account without re-running the full detection flow.
    """
    cached_id = get_setting("premium_search_account_id", "")
    cached_at = get_setting("premium_search_detected_at", 0)
    if cached_id and cached_at and (time.time() - cached_at < _CACHE_TTL_SECONDS):
        return cached_id
    return ""


def invalidate_search_account_cache() -> None:
    """Clear the cached premium search account (e.g., after auth failure).

    The tier bit is deleted, not set False: invalidation means "unknown,
    re-detect", and a stored False is a confirmed negative the cache-hit
    path would serve for a week.
    """
    from ..db.queries import delete_setting
    save_setting("premium_search_account_id", "")
    save_setting("premium_search_detected_at", 0)
    delete_setting("premium_search_has_sn")
    logger.info("Premium search account cache invalidated")
