"""Fit-score send gate and restore for people parked below threshold.

The gate exists to keep auto-discovered junk out of the send path. It must
not rewind someone we already invited or messaged, and a later rescore that
clears the line must put a never-contacted skip back in the queue.
"""

from __future__ import annotations

from typing import Any

FIT_SKIP_ERROR = "fit_score_below_threshold"
_SENT_STATUSES = frozenset({
    "invited", "connected", "messaged", "replied", "hot_lead",
})


def fit_gate_verdict(prospect: dict, campaign_cfg: dict) -> tuple[bool, float, float]:
    """Decide whether the pre-send fit gate skips this prospect.

    Returns (should_skip, fit_score, threshold).

    csv_import prospects are exempt: a human hand-picked them, and the gate
    exists to filter auto-discovered prospects. On 18 Aug 2026 the optimizer
    re-scored eleven hand-imported founders to an identical composite below
    the threshold and this gate silently buried all of them.
    """
    from ..constants import MIN_FIT_SCORE_THRESHOLD, SIGNAL_FIT_OVERRIDE

    fit = prospect.get("fit_score") or 0
    threshold = campaign_cfg.get("min_fit_score", MIN_FIT_SCORE_THRESHOLD)
    if prospect.get("contact_source") == "csv_import":
        return False, fit, threshold
    if prospect.get("next_action") == SIGNAL_FIT_OVERRIDE:
        return False, fit, threshold
    return fit < threshold, fit, threshold


def outreach_was_ever_sent(outreach_id: str) -> bool:
    """True if we already invited, messaged, or recorded an SDR send."""
    if not outreach_id:
        return False
    from ..db.schema import get_db

    db = get_db()
    row = db.execute(
        "SELECT status, invited_at FROM outreaches WHERE id = ?",
        (outreach_id,),
    ).fetchone()
    if not row:
        db.close()
        return False
    if row["status"] in _SENT_STATUSES or row["invited_at"]:
        db.close()
        return True
    msg = db.execute(
        """SELECT 1 FROM messages
           WHERE outreach_id = ? AND role = 'sdr' LIMIT 1""",
        (outreach_id,),
    ).fetchone()
    db.close()
    return msg is not None


def should_fit_skip(
    prospect: dict,
    campaign_cfg: dict,
    *,
    outreach_id: str,
    outreach_status: str = "",
) -> tuple[bool, float, float]:
    """Like fit_gate_verdict, but never parks a person we already reached."""
    skip, fit, thr = fit_gate_verdict(prospect, campaign_cfg)
    if not skip:
        return skip, fit, thr
    if outreach_status in _SENT_STATUSES or outreach_was_ever_sent(outreach_id):
        return False, fit, thr
    return True, fit, thr


def _campaign_min_fit(campaign_id: str) -> float:
    import json

    from ..constants import MIN_FIT_SCORE_THRESHOLD
    from ..db.queries import get_campaign

    campaign = get_campaign(campaign_id)
    try:
        cfg = json.loads((campaign or {}).get("config_json") or "{}")
        return float(cfg.get("min_fit_score", MIN_FIT_SCORE_THRESHOLD))
    except (TypeError, ValueError, json.JSONDecodeError):
        return MIN_FIT_SCORE_THRESHOLD


def restore_fit_skipped_for_campaign(campaign_id: str) -> int:
    return restore_fit_skipped_if_eligible(campaign_id, _campaign_min_fit(campaign_id))


def restore_fit_skipped_if_eligible(campaign_id: str, min_fit: float) -> int:
    """Flip fit-parked, never-sent skips back to pending when score clears."""
    from ..db.schema import get_db

    db = get_db()
    rows = db.execute(
        """SELECT o.id
           FROM outreaches o
           JOIN contacts c ON c.id = o.contact_id
           WHERE o.campaign_id = ?
             AND o.status = 'skipped'
             AND COALESCE(c.fit_score, 0) >= ?
             AND COALESCE(o.invited_at, 0) = 0
             AND o.status NOT IN ('invited', 'connected', 'messaged', 'replied', 'hot_lead')
             AND o.id NOT IN (
                 SELECT outreach_id FROM messages
                 WHERE outreach_id IS NOT NULL AND role = 'sdr'
             )
             AND COALESCE(o.last_attempt_error, '') NOT IN ('operator_skip', 'do_not_contact')
             AND EXISTS (
                 SELECT 1 FROM actions_log al
                 WHERE al.outreach_id = o.id
                   AND al.action_type = 'fit_score_below_threshold'
             )
             AND NOT EXISTS (
                 SELECT 1 FROM actions_log al
                 WHERE al.outreach_id = o.id
                   AND al.action_type IN ('invitation_sent', 'inmail_sent', 'dm_sent')
             )""",
        (campaign_id, min_fit),
    ).fetchall()
    db.close()

    restored = 0
    from ..db.queries import log_action, update_outreach

    for row in rows:
        oid = row["id"]
        if outreach_was_ever_sent(oid):
            continue
        if update_outreach(
            oid,
            status="pending",
            last_attempt_error=None,
        ):
            log_action(
                "fit_skip_restored",
                outreach_id=oid,
                campaign_id=campaign_id,
                result="pending",
                details={"reason": "score_cleared_threshold"},
            )
            restored += 1
    return restored


def restore_fit_skipped_after_contact(campaign_id: str) -> int:
    """Carwin: fit-skipped after a real send — put them back on the last live status."""
    from ..db.schema import get_db

    db = get_db()
    rows = db.execute(
        """SELECT o.id, o.invited_at, o.accepted_at,
                  (SELECT COUNT(*) FROM messages m
                   WHERE m.outreach_id = o.id AND m.role = 'sdr') AS sdr_count
           FROM outreaches o
           WHERE o.campaign_id = ?
             AND o.status = 'skipped'
             AND EXISTS (
                 SELECT 1 FROM actions_log al
                 WHERE al.outreach_id = o.id
                   AND al.action_type = 'fit_score_below_threshold'
             )
             AND (
                 COALESCE(o.invited_at, 0) > 0
                 OR EXISTS (
                     SELECT 1 FROM messages m
                     WHERE m.outreach_id = o.id AND m.role = 'sdr'
                 )
             )""",
        (campaign_id,),
    ).fetchall()
    db.close()

    restored = 0
    from ..db.queries import log_action, update_outreach

    for row in rows:
        if row["accepted_at"] or row["sdr_count"]:
            new_status = "messaged" if row["sdr_count"] and row["accepted_at"] else (
                "connected" if row["accepted_at"] else "messaged" if row["sdr_count"] else "invited"
            )
        elif row["invited_at"]:
            new_status = "invited"
        else:
            continue
        if update_outreach(row["id"], status=new_status, last_attempt_error=None):
            log_action(
                "fit_skip_restored",
                outreach_id=row["id"],
                campaign_id=campaign_id,
                result=new_status,
                details={"reason": "fit_skip_after_contact"},
            )
            restored += 1
    return restored
