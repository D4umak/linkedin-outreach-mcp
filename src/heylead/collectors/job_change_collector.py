"""Profile change signal collector — detects company changes, promotions,
headline intent, and generic headline changes for campaign contacts.

Periodically re-scans campaign contact profiles, comparing current profile data
against the stored ``profile_json`` snapshot.  Detected changes are classified
into specific signal sub-types instead of a single ``job_change`` bucket:

- **company_change** (weight 0.55) — moved to a new company
- **promotion** (weight 0.45) — same company, higher seniority title
- **headline_intent** (weight 0.40) — headline contains intent keywords
  ("hiring", "building", "evaluating", …)
- **headline_change** (weight 0.25) — generic headline wording change (fallback)

Runs every 4 hours via the scheduler, scanning up to 50 contacts per run.
"""

from __future__ import annotations

import json
from ..textutil import contains_term
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# Max profiles to fetch per run (rate limit safety)
JOB_CHANGE_BATCH_SIZE = 50

# Min days between re-scans for the same contact
RESCAN_INTERVAL_DAYS = 7

# Key inside contacts.profile_json holding this collector's own last scan time.
PROFILE_SCAN_KEY = "_profile_scan_at"

# Seniority rank for promotion detection (reuses SENIORITY_KEYWORDS from
# revenue_estimator via _infer_seniority, but we need a numeric ordering here).
_SENIORITY_RANK: dict[str, int] = {
    "entry": 0,
    "senior": 1,
    "manager": 2,
    "director": 3,
    "vp": 4,
    "cxo": 5,
    "owner": 6,
}


async def collect_job_changes() -> str:
    """Scan campaign contacts for profile changes.

    For each active campaign:
    1. Fetch contacts not scanned in the last RESCAN_INTERVAL_DAYS
    2. Re-fetch their LinkedIn profile via get_profile()
    3. Classify changes into sub-types (company_change, promotion,
       headline_intent, headline_change)
    4. Save one or more signals per detected change
    5. Update contact's profile_json snapshot + last_scanned_at

    Returns summary string.
    """
    from ..db.async_bridge import run_db
    from ..db.queries import get_contacts_for_campaign, list_campaigns
    from ..db.signal_queries import (
        save_signal,
        signal_exists,
        upsert_signal_account,
    )
    from ..linkedin import UnipileError, get_account_id, get_linkedin_client

    account_id = await run_db(get_account_id)
    if not account_id:
        return "No LinkedIn account connected."

    campaigns = await run_db(list_campaigns, status="active")
    if not campaigns:
        return "No active campaigns."

    try:
        client = get_linkedin_client()
    except UnipileError as e:
        return f"LinkedIn client error: {e}"

    now = int(time.time())
    rescan_cutoff = now - (RESCAN_INTERVAL_DAYS * 86400)

    total_scanned = 0
    total_changes = 0
    errors = 0

    try:
        for camp in campaigns:
            if total_scanned >= JOB_CHANGE_BATCH_SIZE:
                break

            cid = camp["id"]
            contacts = await run_db(get_contacts_for_campaign, cid)
            if not contacts:
                continue

            for contact in contacts:
                if total_scanned >= JOB_CHANGE_BATCH_SIZE:
                    break

                contact_id = contact["id"]
                linkedin_id = contact.get("linkedin_id", "")
                linkedin_url = contact.get("linkedin_url", "")

                identifier = linkedin_id or _extract_public_id(linkedin_url)
                if not identifier:
                    continue

                # Parse existing profile snapshot
                old_profile = _parse_profile_json(
                    contact.get("profile_json", "")
                )

                # contacts.last_scanned_at belongs to the post scanners, which
                # re-stamp it every few hours; gating on it starved this scan.
                last_scanned = old_profile.get(PROFILE_SCAN_KEY) or 0
                if last_scanned > rescan_cutoff:
                    continue

                old_title = (
                    old_profile.get("title", "") or contact.get("title", "")
                )
                old_company = (
                    old_profile.get("company", "")
                    or contact.get("company", "")
                )
                old_headline = old_profile.get("headline", "")
                old_experience = old_profile.get("experience", [])

                try:
                    profile = await client.get_profile(account_id, identifier)
                    total_scanned += 1

                    if not profile or isinstance(profile, list):
                        # Stamp it anyway. The gate above reads PROFILE_SCAN_KEY,
                        # which only the success path writes, so an unreadable
                        # profile never acquired it and re-entered every 4h run
                        # — sorting to the FRONT, because the same success path
                        # is the only writer of contacts.last_scanned_at that
                        # this scan uses for ordering. A campaign with 30 dead
                        # profiles spent its entire batch on them for ever.
                        #
                        # This records that we looked, not that it worked: the
                        # retry now happens on the normal rescan schedule.
                        await run_db(
                            _mark_profile_scanned, contact["id"],
                            contact.get("profile_json", ""), int(time.time()),
                        )
                        continue

                    new_title = (
                        profile.get("title", "")
                        or profile.get("headline", "")
                        or ""
                    )
                    new_company = (
                        profile.get("company", "")
                        or _extract_company_from_experience(profile)
                        or ""
                    )
                    new_headline = profile.get("headline", "") or ""
                    new_name = (
                        profile.get("name", "")
                        or profile.get("full_name", "")
                        or contact.get("name", "")
                    )
                    new_experience = profile.get("experience", [])

                    # Classify changes into sub-type signals
                    detected = _classify_profile_change(
                        old_title=old_title,
                        new_title=new_title,
                        old_company=old_company,
                        new_company=new_company,
                        old_headline=old_headline,
                        new_headline=new_headline,
                        old_experience=old_experience,
                        new_experience=new_experience,
                    )

                    lid = linkedin_id or identifier

                    for sig_info in detected:
                        sig_type = sig_info["signal_type"]
                        sig_ttl = sig_info["ttl"]
                        sig_metadata = sig_info["metadata"]
                        sig_metadata["campaign_id"] = cid
                        sig_metadata["contact_id"] = contact_id

                        if await run_db(
                            signal_exists,
                            sig_type,
                            linkedin_id=lid,
                            lookback_seconds=RESCAN_INTERVAL_DAYS * 86400,
                        ):
                            continue

                        content = _build_change_content(
                            sig_type, new_name, sig_metadata
                        )

                        await run_db(
                            save_signal,
                            signal_type=sig_type,
                            source="profile_scan",
                            prospect_id=contact_id,
                            prospect_name=new_name or None,
                            prospect_title=new_title or None,
                            linkedin_id=lid,
                            campaign_id=cid,
                            content=content[:1000],
                            metadata_json=json.dumps(sig_metadata),
                            expires_at=now + sig_ttl,
                        )
                        total_changes += 1

                        await run_db(
                            upsert_signal_account,
                            linkedin_id=lid,
                            prospect_name=new_name or None,
                            company=new_company or old_company or None,
                        )

                        logger.info(
                            "Profile change detected (%s) for %s: %s",
                            sig_type,
                            new_name,
                            content[:120],
                        )

                    # Update profile snapshot and last_scanned_at
                    await run_db(
                        _update_contact_profile,
                        contact_id=contact_id,
                        profile_json=json.dumps(profile),
                        timestamp=now,
                    )

                except UnipileError as e:
                    logger.warning(
                        "Profile fetch failed for %s: %s", identifier, e
                    )
                    errors += 1
                except Exception as e:
                    logger.warning(
                        "Job change scan error for %s: %s", identifier, e
                    )
                    errors += 1
                    # Same reasoning as the empty-profile branch above: an
                    # unstamped contact is a permanent resident of the batch.
                    try:
                        await run_db(
                            _mark_profile_scanned, contact["id"],
                            contact.get("profile_json", ""), int(time.time()),
                        )
                    except Exception:
                        logger.debug("could not stamp a failed scan", exc_info=True)

    finally:
        await client.close()

    summary = (
        f"Scanned {total_scanned} profiles, "
        f"detected {total_changes} profile changes"
    )
    if errors:
        summary += f", {errors} errors"
    return summary


# ──────────────────────────────────────────────
# Profile change classification
# ──────────────────────────────────────────────


def _classify_profile_change(
    *,
    old_title: str,
    new_title: str,
    old_company: str,
    new_company: str,
    old_headline: str,
    new_headline: str,
    old_experience: list[dict[str, Any]],
    new_experience: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Classify profile changes into specific signal sub-types.

    Returns a list of signal dicts, each with ``signal_type``, ``ttl``,
    and ``metadata``.  Can return multiple signals (e.g. company_change +
    headline_intent simultaneously).
    """
    from ..constants import (
        SIGNAL_COMPANY_CHANGE,
        SIGNAL_HEADLINE_CHANGE,
        SIGNAL_HEADLINE_INTENT,
        SIGNAL_PROMOTION,
        SIGNAL_TTL_COMPANY_CHANGE,
        SIGNAL_TTL_HEADLINE_CHANGE,
        SIGNAL_TTL_HEADLINE_INTENT,
        SIGNAL_TTL_PROMOTION,
    )

    signals: list[dict[str, Any]] = []

    title_changed = _has_significant_change(old_title, new_title, strict=True)
    company_changed = _has_significant_change(old_company, new_company, strict=True)
    headline_changed = _has_significant_change(old_headline, new_headline)

    old_seniority = _infer_seniority(old_title)
    new_seniority = _infer_seniority(new_title)

    # Priority 1: Company change (moved to new company)
    # Require non-empty new_company so we know WHERE they went
    if company_changed and new_company:
        signals.append({
            "signal_type": SIGNAL_COMPANY_CHANGE,
            "ttl": SIGNAL_TTL_COMPANY_CHANGE,
            "metadata": {
                "old_title": old_title,
                "new_title": new_title,
                "old_company": old_company,
                "new_company": new_company,
                "old_seniority": old_seniority,
                "new_seniority": new_seniority,
                "title_changed": title_changed,
                "company_changed": True,
            },
        })

    # Priority 2: Promotion (same company, higher seniority)
    elif title_changed and _is_promotion(old_seniority, new_seniority):
        signals.append({
            "signal_type": SIGNAL_PROMOTION,
            "ttl": SIGNAL_TTL_PROMOTION,
            "metadata": {
                "old_title": old_title,
                "new_title": new_title,
                "company": old_company or new_company,
                "old_seniority": old_seniority,
                "new_seniority": new_seniority,
                "title_changed": True,
                "company_changed": False,
            },
        })

    # Priority 3: Headline intent keywords (independent — can stack with above)
    if headline_changed:
        intent_matches = _scan_headline_intent(new_headline)
        if intent_matches:
            signals.append({
                "signal_type": SIGNAL_HEADLINE_INTENT,
                "ttl": SIGNAL_TTL_HEADLINE_INTENT,
                "metadata": {
                    "old_headline": old_headline,
                    "new_headline": new_headline,
                    "intent_categories": list(intent_matches.keys()),
                    "matched_keywords": {
                        cat: kws for cat, kws in intent_matches.items()
                    },
                },
            })

    # Priority 4: Generic headline change (fallback — only if nothing else)
    if not signals and headline_changed:
        signals.append({
            "signal_type": SIGNAL_HEADLINE_CHANGE,
            "ttl": SIGNAL_TTL_HEADLINE_CHANGE,
            "metadata": {
                "old_headline": old_headline,
                "new_headline": new_headline,
                "old_title": old_title,
                "new_title": new_title,
            },
        })

    # Bonus: new positions added to experience array (advisory, consulting)
    if not company_changed:
        new_positions = _detect_new_positions(old_experience, new_experience)
        for pos in new_positions:
            signals.append({
                "signal_type": SIGNAL_HEADLINE_CHANGE,
                "ttl": SIGNAL_TTL_HEADLINE_CHANGE,
                "metadata": {
                    "new_position_title": pos.get("title", ""),
                    "new_position_company": pos.get("company", ""),
                    "is_additional_role": True,
                    "old_title": old_title,
                    "new_title": new_title,
                },
            })

    return signals


def _is_promotion(old_seniority: str, new_seniority: str) -> bool:
    """Return True if the seniority change represents a promotion."""
    old_rank = _SENIORITY_RANK.get(old_seniority, 2)
    new_rank = _SENIORITY_RANK.get(new_seniority, 2)
    return new_rank > old_rank


def _infer_seniority(title: str) -> str:
    """Infer seniority level from job title.

    Delegates to the one vocabulary in ``services/seniority.py``. It used to
    walk ``SENIORITY_KEYWORDS`` with ``kw in title_lower`` — substring, not
    whole word — so "cto" matched inside "dire(cto)r" and every Director was
    classified cxo before the ladder ever reached the director rung. A
    Director -> VP promotion then read as a demotion and lost its job-change
    signal, the same defect that was fixed in the backend's
    ``hosted_signals._infer_seniority`` and in ``revenue_estimator``.

    The historic "a title that states no level reads as manager" default is
    kept: the promotion comparison in this module is calibrated on it.
    """
    from ..services.seniority import infer_seniority_level

    if not (title or "").strip():
        return "entry"
    return infer_seniority_level(title) or "manager"


def _scan_headline_intent(headline: str) -> dict[str, list[str]]:
    """Scan headline for intent keywords.

    Returns ``{category: [matched_keywords]}`` or empty dict.
    """
    from ..constants import HEADLINE_INTENT_KEYWORDS

    headline_lower = headline.lower()
    matches: dict[str, list[str]] = {}
    for category, keywords in HEADLINE_INTENT_KEYWORDS.items():
        matched = [kw for kw in keywords if contains_term(headline_lower, kw)]
        if matched:
            matches[category] = matched
    return matches


def _detect_new_positions(
    old_experience: list[dict[str, Any]],
    new_experience: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Detect positions added to the experience array.

    Compares by (company, title) tuples. Returns list of new position
    dicts not present in old.
    """
    if not new_experience:
        return []

    old_keys: set[tuple[str, str]] = set()
    for pos in old_experience or []:
        if isinstance(pos, dict):
            company = (
                pos.get("company") or pos.get("company_name") or ""
            ).lower().strip()
            title = (
                pos.get("title") or pos.get("role") or ""
            ).lower().strip()
            old_keys.add((company, title))

    new_positions: list[dict[str, str]] = []
    for pos in new_experience:
        if not isinstance(pos, dict):
            continue
        company = (
            pos.get("company") or pos.get("company_name") or ""
        ).strip()
        title = (pos.get("title") or pos.get("role") or "").strip()
        key = (company.lower(), title.lower())
        if key not in old_keys and company and title:
            new_positions.append({"title": title, "company": company})
    return new_positions


def _build_change_content(
    signal_type: str,
    name: str,
    metadata: dict[str, Any],
) -> str:
    """Build a human-readable content string for the signal."""
    if signal_type == "company_change":
        old_co = metadata.get("old_company", "")
        new_co = metadata.get("new_company", "")
        new_t = metadata.get("new_title", "")
        return f"{name} joined {new_co} as {new_t} (from {old_co})"

    if signal_type == "promotion":
        old_t = metadata.get("old_title", "")
        new_t = metadata.get("new_title", "")
        co = metadata.get("company", "")
        return f"{name} promoted to {new_t} at {co} (was {old_t})"

    if signal_type == "headline_intent":
        cats = metadata.get("intent_categories", [])
        headline = metadata.get("new_headline", "")
        return (
            f'{name} headline signals: {", ".join(cats)} '
            f'— "{headline[:100]}"'
        )

    if signal_type == "headline_change":
        if metadata.get("is_additional_role"):
            pos_title = metadata.get("new_position_title", "")
            pos_co = metadata.get("new_position_company", "")
            return f"{name} added new role: {pos_title} at {pos_co}"
        old_h = metadata.get("old_headline", "")
        new_h = metadata.get("new_headline", "")
        return f'{name} updated headline: "{old_h[:60]}" → "{new_h[:60]}"'

    return f"{name} profile updated"


# ──────────────────────────────────────────────
# Shared helpers
# ──────────────────────────────────────────────


def _extract_public_id(linkedin_url: str) -> str:
    """Extract the public identifier from a LinkedIn URL.

    e.g., 'https://www.linkedin.com/in/johndoe' → 'johndoe'
    """
    if not linkedin_url:
        return ""
    url = linkedin_url.rstrip("/")
    if "/in/" in url:
        return url.split("/in/")[-1].split("?")[0]
    return ""


def _parse_profile_json(raw: str) -> dict[str, Any]:
    """Safely parse profile_json field."""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def _extract_company_from_experience(profile: dict[str, Any]) -> str:
    """Extract current company from profile experience section."""
    experiences = profile.get("experiences", [])
    if not experiences:
        experiences = profile.get("experience", [])
    if experiences and isinstance(experiences, list):
        first = experiences[0]
        if isinstance(first, dict):
            return first.get("company_name", "") or first.get("company", "")
    return ""


def _has_significant_change(
    old: str, new: str, *, strict: bool = False,
) -> bool:
    """Check if two values represent a significant change.

    Ignores case differences, minor whitespace changes, and empty → empty.

    When *strict* is True, also rejects substring expansions and
    near-identical strings (>80 pct word overlap) — useful for company
    and title fields where LinkedIn reformatting produces false positives.
    Headlines should use ``strict=False`` (the default) because appended
    taglines like "| We're hiring!" carry real signal value.
    """
    if not old and not new:
        return False
    old_norm = (old or "").strip().lower()
    new_norm = (new or "").strip().lower()
    if old_norm == new_norm:
        return False
    # Treat empty old as "we had no data" → not a real change
    if not old_norm:
        return False
    if strict:
        # Reject substring expansions (e.g. "SportsTech Cricket Connect"
        # -> "SportsTech Cricket Connect & The Banking 50 (100k-...")
        if old_norm in new_norm or new_norm in old_norm:
            return False
        # Reject near-matches: >80% word overlap is likely reformatting
        old_words = set(old_norm.split())
        new_words = set(new_norm.split())
        if old_words and new_words:
            overlap = len(old_words & new_words) / max(
                len(old_words), len(new_words)
            )
            if overlap > 0.8:
                return False
    return True


def _mark_profile_scanned(contact_id: str, profile_json: str, timestamp: int) -> None:
    """Record that this profile was attempted, without claiming it was read.

    Only the snapshot key is written — contacts.last_scanned_at is deliberately
    left alone, because it is shared with the post scanners and moving it here
    would distort their ordering. This is exactly enough to stop a permanently
    unreadable profile from re-entering every batch.
    """
    from ..db.schema import get_db

    snapshot = _parse_profile_json(profile_json)
    snapshot[PROFILE_SCAN_KEY] = timestamp

    db = get_db()
    try:
        db.execute(
            "UPDATE contacts SET profile_json = ? WHERE id = ?",
            (json.dumps(snapshot), contact_id),
        )
        db.commit()
    finally:
        db.close()


def _update_contact_profile(
    contact_id: str,
    profile_json: str,
    timestamp: int,
) -> None:
    """Update a contact's profile_json and last_scanned_at directly via SQL.

    We bypass update_contact() since profile_json is not in _VALID_CONTACT_COLS.
    The profile-scan timestamp rides inside the snapshot because last_scanned_at
    is shared with the post scanners.
    """
    from ..db.schema import get_db

    snapshot = _parse_profile_json(profile_json)
    snapshot[PROFILE_SCAN_KEY] = timestamp
    profile_json = json.dumps(snapshot)

    db = get_db()
    try:
        db.execute(
            "UPDATE contacts SET profile_json = ?, last_scanned_at = ? WHERE id = ?",
            (profile_json, timestamp, contact_id),
        )
        db.commit()
    finally:
        db.close()
