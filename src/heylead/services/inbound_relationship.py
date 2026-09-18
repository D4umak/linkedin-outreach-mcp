"""Resolve an inbound sender against existing campaigns, referrals, and threads.

Stranger-path classification only scores active ICPs. That sent Sam Rivera
an AI counter-pitch after Pat referred him on a paused buy campaign.
This module runs before reply generation and decides continue / hold / stranger.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from ..ai.intent import resolve_intent
from ..ai.referral_extractor import detect_referral
from ..db.queries import get_campaign, list_campaigns
from ..db.schema import get_db
from .inbox_match import find_best_inbox_match
from .job_search_guard import is_job_search_config_json
from .signal_linker import _match_best_campaign
from ..textutil import contains_term

_REFERRED_BY_RE = re.compile(r"^Referred by\s+(.+)$", re.I)


@dataclass(frozen=True)
class Relationship:
    kind: str
    action: str
    campaign_id: str = ""
    contact_id: str = ""
    reason: str = ""


def resolve_inbound_relationship(signal: dict[str, Any] | None) -> Relationship:
    """Return the campaign relationship for an inbound signal, if any."""
    signal = signal or {}
    candidates: list[Relationship] = []

    reverse = _reverse_referral(signal)
    if reverse:
        candidates.append(reverse)

    referral = _referral_contact(signal)
    if referral:
        candidates.append(referral)

    inbox = _known_thread(signal)
    if inbox:
        candidates.append(inbox)

    named = _unique_name_match(signal)
    if named:
        candidates.append(named)

    content = _content_campaign(signal)
    if content:
        candidates.append(content)

    chosen = _prefer_paused_buy(candidates)
    if chosen is None:
        return Relationship(kind="none", action="stranger", reason="no campaign relationship")
    return chosen


def campaign_id_from_referrer_thread(referrer: dict[str, Any] | None) -> str:
    """Campaign for a referrer who has no contact.campaign_id.

    Looks at the referrer's prior inbound text (and name/company) against
    every campaign, including paused. Empty string means the caller may
    fall back to the active-only path.
    """
    referrer = referrer or {}
    texts: list[str] = []
    sender_id = (
        (referrer.get("linkedin_id") or referrer.get("sender_id") or "")
        .strip()
    )
    if sender_id:
        texts.extend(_inbound_contents(sender_id))
    name = (referrer.get("name") or "").strip()
    company = (referrer.get("company") or "").strip()
    if name or company:
        texts.append(f"{name} {company}")
    blob = " ".join(t for t in texts if t).strip()
    if not blob:
        return ""
    camp = _best_campaign_any_status(blob, referrer.get("title") or "")
    return (camp or {}).get("id") or ""


def persist_hold(signal_id: str, rel: Relationship) -> None:
    """Mark an inbound signal held and write the audit row."""
    from ..db.queries import log_action, update_inbound_signal

    if not signal_id:
        return
    update_inbound_signal(
        signal_id,
        status="held",
        recommended_action="hold_for_operator",
        decline_reason=(rel.reason or rel.kind)[:240],
        **({"campaign_id": rel.campaign_id} if rel.campaign_id else {}),
    )
    log_action(
        "inbound_held",
        result="held",
        details={
            "signal_id": signal_id,
            "kind": rel.kind,
            "campaign_id": rel.campaign_id,
            "contact_id": rel.contact_id,
            "reason": rel.reason,
        },
        campaign_id=rel.campaign_id,
    )


def stamp_matched_identity(contact_id: str, sender_id: str) -> None:
    """Write provider_id so the next inbound hits find_best_inbox_match."""
    if not contact_id or not sender_id:
        return
    from ..db.queries import update_contact
    from ..db.schema import get_db as _get_db

    db = _get_db()
    row = db.execute(
        "SELECT profile_json, linkedin_id FROM contacts WHERE id = ?",
        (contact_id,),
    ).fetchone()
    db.close()
    if not row:
        return
    try:
        profile = json.loads(row["profile_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        profile = {}
    if not isinstance(profile, dict):
        profile = {}
    if not profile.get("provider_id"):
        profile["provider_id"] = sender_id
    kwargs: dict[str, Any] = {"profile_json": json.dumps(profile)}
    # linkedin_id is not always in the public update set; write it directly
    # when the column is empty so inbox_match can find the row.
    if not (row["linkedin_id"] or "").strip():
        db = _get_db()
        db.execute(
            "UPDATE contacts SET linkedin_id = ?, profile_json = ?, updated_at = "
            "(CAST(strftime('%s', 'now') AS INTEGER)) WHERE id = ?",
            (sender_id, json.dumps(profile), contact_id),
        )
        db.commit()
        db.close()
        return
    update_contact(contact_id, **kwargs)


def _prefer_paused_buy(candidates: list[Relationship]) -> Relationship | None:
    if not candidates:
        return None
    for rel in candidates:
        if rel.action == "hold":
            return rel
    return candidates[0]


def _relationship_for_campaign(
    *,
    kind: str,
    campaign_id: str,
    contact_id: str = "",
    reason: str,
) -> Relationship | None:
    if not campaign_id:
        return None
    campaign = get_campaign(campaign_id)
    if not campaign:
        return None
    action = _action_for_campaign(campaign)
    if is_job_search_config_json(campaign.get("config_json")):
        reason = f"job-search campaign, held for operator ({reason})"
    return Relationship(
        kind=kind,
        action=action,
        campaign_id=campaign_id,
        contact_id=contact_id,
        reason=reason,
    )


def _action_for_campaign(campaign: dict[str, Any]) -> str:
    # A job-search campaign's contacts are answered by hand, never by the pipeline.
    if is_job_search_config_json(campaign.get("config_json")):
        return "hold"
    intent = resolve_intent(campaign.get("config_json"))
    if intent == "buy" and (campaign.get("status") or "") == "paused":
        return "hold"
    return "continue"


def _reverse_referral(signal: dict[str, Any]) -> Relationship | None:
    sender_name = (signal.get("sender_name") or "").strip()
    sender_email = _email_from_signal(signal)
    if not sender_name and not sender_email:
        return None
    db = get_db()
    rows = db.execute(
        """SELECT id, sender_name, sender_id, sender_company, content, campaign_id
           FROM inbound_signals
           WHERE content IS NOT NULL AND TRIM(content) != ''
           ORDER BY created_at DESC LIMIT 80""",
    ).fetchall()
    db.close()
    self_id = (signal.get("id") or "").strip()
    for row in rows:
        if self_id and row["id"] == self_id:
            continue
        handoff = detect_referral(
            row["content"] or "",
            referrer_company=row["sender_company"] or "",
        )
        if handoff is None:
            continue
        if not _handoff_matches_sender(handoff, sender_name, sender_email):
            continue
        campaign_id = (row["campaign_id"] or "").strip()
        if not campaign_id:
            camp = _best_campaign_any_status(
                row["content"] or "",
                row["sender_company"] or "",
            )
            campaign_id = (camp or {}).get("id") or ""
        if not campaign_id:
            referrer_inbox = find_best_inbox_match(row["sender_id"] or "")
            campaign_id = ((referrer_inbox or {}).get("campaign_id") or "").strip()
        return _relationship_for_campaign(
            kind="reverse_referral",
            campaign_id=campaign_id,
            reason=f"named in {row['sender_name'] or 'prior inbound'} referral",
        )
    return None


def _referral_contact(signal: dict[str, Any]) -> Relationship | None:
    sender_name = (signal.get("sender_name") or "").strip()
    sender_email = _email_from_signal(signal)
    rows = _referral_contact_rows(sender_name, sender_email)
    if not rows:
        return None
    row = rows[0]
    campaign_id = _campaign_from_referrer_detail(row.get("source_detail") or "")
    if not campaign_id:
        campaign_id = _campaign_from_referrer_name(row.get("source_detail") or "")
    return _relationship_for_campaign(
        kind="referral_contact",
        campaign_id=campaign_id,
        contact_id=row.get("id") or "",
        reason=row.get("source_detail") or "referral contact",
    )


def _known_thread(signal: dict[str, Any]) -> Relationship | None:
    sender_id = (signal.get("sender_id") or "").strip()
    if not sender_id:
        return None
    inbox = find_best_inbox_match(sender_id)
    if not inbox:
        return None
    return _relationship_for_campaign(
        kind="known_thread",
        campaign_id=(inbox.get("campaign_id") or "").strip(),
        contact_id=(inbox.get("contact_id") or inbox.get("id") or ""),
        reason="existing outreach thread",
    )


def _unique_name_match(signal: dict[str, Any]) -> Relationship | None:
    name = (signal.get("sender_name") or "").strip()
    if not name:
        return None
    db = get_db()
    rows = db.execute(
        """SELECT id, campaign_id, source, source_detail
           FROM contacts WHERE LOWER(name) = LOWER(?)
           ORDER BY created_at DESC""",
        (name,),
    ).fetchall()
    db.close()
    if not rows:
        return None
    campaign_ids = {r["campaign_id"] for r in rows if r["campaign_id"]}
    if len(campaign_ids) != 1 and len({r["id"] for r in rows}) != 1:
        # Same person copied across campaigns: prefer a referral row's referrer
        referrals = [dict(r) for r in rows if (r["source"] or "") == "referral"]
        if len(referrals) == 1:
            return _referral_contact_from_row(referrals[0])
        return None
    row = dict(rows[0])
    if (row.get("source") or "") == "referral":
        return _referral_contact_from_row(row)
    return _relationship_for_campaign(
        kind="name_match",
        campaign_id=(row.get("campaign_id") or "").strip(),
        contact_id=row.get("id") or "",
        reason="unique name match",
    )


def _referral_contact_from_row(row: dict[str, Any]) -> Relationship | None:
    campaign_id = _campaign_from_referrer_detail(row.get("source_detail") or "")
    if not campaign_id:
        campaign_id = _campaign_from_referrer_name(row.get("source_detail") or "")
    return _relationship_for_campaign(
        kind="referral_contact",
        campaign_id=campaign_id,
        contact_id=row.get("id") or "",
        reason=row.get("source_detail") or "referral contact",
    )


def _content_campaign(signal: dict[str, Any]) -> Relationship | None:
    text = (signal.get("content") or "").strip()
    if not text:
        return None
    camp = _best_campaign_any_status(text, signal.get("sender_headline") or "")
    if not camp:
        return None
    return _relationship_for_campaign(
        kind="content_campaign",
        campaign_id=camp["id"],
        reason="content matches campaign including paused",
    )


def _best_campaign_any_status(text: str, title: str = "") -> dict[str, Any] | None:
    campaigns = list_campaigns()
    if not campaigns:
        return None
    scored: list[tuple[float, int, dict[str, Any]]] = []
    for camp in campaigns:
        overlap = _campaign_overlap(text, title, camp)
        if overlap <= 0:
            continue
        buy_bonus = 1 if resolve_intent(camp.get("config_json")) == "buy" else 0
        scored.append((overlap, buy_bonus, camp))
    if not scored:
        # Fall back to the shared matcher so enable_discovery / ICP shape stay
        # consistent with inbound campaign assignment when overlap is sparse.
        matched_id = _match_best_campaign(
            {"content": text, "prospect_title": title, "metadata_json": ""},
            campaigns,
        )
        return get_campaign(matched_id) if matched_id else None
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    best_overlap, _bonus, best = scored[0]
    from ..constants import SIGNAL_ICP_MIN_OVERLAP
    if best_overlap < SIGNAL_ICP_MIN_OVERLAP:
        return None
    near = [
        c for ov, bonus, c in scored
        if abs(ov - best_overlap) < 0.05 and resolve_intent(c.get("config_json")) == "buy"
    ]
    if near:
        return near[0]
    return best


def _campaign_overlap(text: str, title: str, camp: dict[str, Any]) -> float:
    match_text = f"{text} {title}".lower()
    keywords: set[str] = set()
    icp_raw = camp.get("icp_json") or ""
    try:
        icp_data = json.loads(icp_raw) if isinstance(icp_raw, str) else icp_raw
    except (json.JSONDecodeError, TypeError):
        icp_data = {}
    if isinstance(icp_data, dict):
        from .signal_linker import _extract_icp_keywords
        _extract_icp_keywords(icp_data, keywords)
        for persona in icp_data.get("icps") or []:
            if isinstance(persona, dict):
                _extract_icp_keywords(persona, keywords)
    elif isinstance(icp_data, list):
        from .signal_linker import _extract_icp_keywords
        for persona in icp_data:
            if isinstance(persona, dict):
                _extract_icp_keywords(persona, keywords)
    try:
        cfg = json.loads(camp.get("config_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        cfg = {}
    extra: set[str] = set()
    extra_src = " ".join(
        p for p in (
            camp.get("name"),
            (cfg or {}).get("target_description"),
        ) if p
    )
    for word in re.findall(r"[a-z0-9]{4,}", extra_src.lower()):
        extra.add(word)
    keywords.discard("")
    extra.discard("")
    if keywords:
        matched = sum(1 for kw in keywords if contains_term(match_text, kw))
        bonus = sum(1 for kw in extra if contains_term(match_text, kw))
        return (matched + 0.25 * bonus) / len(keywords)
    if not extra:
        return 0.0
    matched = sum(1 for kw in extra if contains_term(match_text, kw))
    return matched / len(extra)


def _handoff_matches_sender(handoff: Any, sender_name: str, sender_email: str) -> bool:
    if sender_email and (handoff.email or "").strip().lower() == sender_email:
        return True
    return _names_match(sender_name, handoff.name or "")


def _names_match(left: str, right: str) -> bool:
    a = " ".join((left or "").lower().split())
    b = " ".join((right or "").lower().split())
    if not a or not b:
        return False
    if a == b:
        return True
    aw, bw = a.split(), b.split()
    if aw[0] != bw[0]:
        return False
    if len(aw) == 1 or len(bw) == 1:
        return True
    return aw[-1] == bw[-1]


def _email_from_signal(signal: dict[str, Any]) -> str:
    blob = signal.get("profile_json") or ""
    if isinstance(blob, dict):
        email = str(blob.get("email") or "").strip().lower()
        if email:
            return email
    elif blob:
        try:
            parsed = json.loads(blob)
            email = str((parsed or {}).get("email") or "").strip().lower()
            if email:
                return email
        except (json.JSONDecodeError, TypeError):
            pass
    text = signal.get("content") or ""
    match = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", text, re.I)
    return (match.group(0).rstrip(".,;:)").lower() if match else "")


def _referral_contact_rows(name: str, email: str) -> list[dict[str, Any]]:
    db = get_db()
    rows: list[Any] = []
    if name:
        rows.extend(db.execute(
            """SELECT id, campaign_id, source, source_detail, profile_json, name
               FROM contacts WHERE source = 'referral' AND LOWER(name) = LOWER(?)
               ORDER BY created_at DESC""",
            (name,),
        ).fetchall())
    if email:
        like = f"%{email}%"
        rows.extend(db.execute(
            """SELECT id, campaign_id, source, source_detail, profile_json, name
               FROM contacts
               WHERE source = 'referral' AND LOWER(COALESCE(profile_json, '')) LIKE ?
               ORDER BY created_at DESC""",
            (like.lower(),),
        ).fetchall())
    db.close()
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        rid = row["id"]
        if rid in seen:
            continue
        seen.add(rid)
        out.append(dict(row))
    return out


def _campaign_from_referrer_detail(source_detail: str) -> str:
    referrer_name = _referrer_name(source_detail)
    if not referrer_name:
        return ""
    db = get_db()
    contact = db.execute(
        """SELECT campaign_id FROM contacts
           WHERE LOWER(name) = LOWER(?) AND campaign_id IS NOT NULL
           ORDER BY created_at DESC LIMIT 1""",
        (referrer_name,),
    ).fetchone()
    inbound = db.execute(
        """SELECT campaign_id, content, sender_company
           FROM inbound_signals
           WHERE LOWER(sender_name) = LOWER(?)
           ORDER BY created_at DESC LIMIT 5""",
        (referrer_name,),
    ).fetchall()
    db.close()
    if contact and contact["campaign_id"]:
        return contact["campaign_id"]
    for row in inbound:
        if row["campaign_id"]:
            return row["campaign_id"]
    for row in inbound:
        camp = _best_campaign_any_status(row["content"] or "", row["sender_company"] or "")
        if camp:
            return camp["id"]
    return ""


def _campaign_from_referrer_name(source_detail: str) -> str:
    """When the referrer has no campaign row, match their inbound text."""
    return _campaign_from_referrer_detail(source_detail)


def _referrer_name(source_detail: str) -> str:
    match = _REFERRED_BY_RE.match((source_detail or "").strip())
    return (match.group(1) if match else "").strip()


def _inbound_contents(sender_id: str) -> list[str]:
    db = get_db()
    rows = db.execute(
        """SELECT content FROM inbound_signals
           WHERE sender_id = ? AND content IS NOT NULL AND TRIM(content) != ''
           ORDER BY created_at DESC LIMIT 10""",
        (sender_id,),
    ).fetchall()
    db.close()
    return [r["content"] for r in rows if r["content"]]
