"""Who is waiting on the user: one reader, and every surface is a view of it.

Client twin of heylead-api app/services/waiting_on_you.py (api #1458, outcome
D4umak/heylead-api#1437). On 25 Sep 2026, in Denys's workspace, the question
had a different answer on each hosted surface, and the client carried the same
split: ``db.queries.get_unanswered_leads`` (show_status' Needs attention, the
unanswered-lead alert, the daily digest) listed replies and handoffs but no
holds; ``tools.inspect._list_holds`` kept a hold "fresh" after a person had
answered it and in archived campaigns; ``check_replies`` named only the people
whose message arrived in that run, called any hold on the row "HELD FOR
OPERATOR", fresh or not, and named nobody when nothing new arrived.

Now get_unanswered_leads, inspect(action='holds'), inspect(action='waiting'),
check_replies and suggest_next_action all read ``who_is_waiting``. Needs
attention's meaning is the reference: a person waits on you when their reply
is unanswered, when they handed over a way to meet that only you can use
(until a person acts, even after the lane has said thanks), or when the lane
held their reply for you. Drafts waiting for approval are hosted-only and
come from the host (inspect(action='waiting')).

tests/test_one_reader_says_who_is_waiting.py holds the surfaces to one
answer. The same file's AST guards fail the suite when another module queries
holds, judges a hold for who waits, or reads the candidates directly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterable

from ..db import queries as q
from ..db.schema import get_db
from .reply_agent import is_fresh_hold, parse_operator_hold

REPLY = q.REPLY
HANDOFF = q.HANDOFF
HOLD = q.HOLD
# Someone wrote to this workspace and waits on a person here.
PEOPLE_KINDS: tuple[str, ...] = (REPLY, HANDOFF, HOLD)

# Hold rows read per request. A cap only against a flood; it is not a page.
_HOLDS_MAX = 200
# A closed outreach waits on nobody. The same statuses as the handoff query.
_OPEN_STATUSES = ("hot_lead", "replied")
# The roles a person's message is stored under (queries._INBOUND_ROLES).
_INBOUND_ROLES = ("prospect", "them", "inbound")
_PREVIEW_CHARS = 200
_RENDER_PREVIEW_CHARS = 120


@dataclass(frozen=True)
class Waiting:
    """One person waiting on the user."""

    kind: str
    name: str
    outreach_id: str
    campaign_id: str
    campaign_name: str
    since: int
    why: str
    preview: str
    company: str = ""
    title: str = ""
    linkedin_url: str = ""
    # HOLD: the reason the lane gave for leaving it to a person.
    held_because: str = ""
    # The rest of get_unanswered_leads' row.
    status: str = ""
    sentiment: str = ""
    calendar_url: str = ""


def fresh_operator_holds(*, campaign_id: str = "", outreach_id: str = "") -> list[dict[str, Any]]:
    """Operator holds a person still has to answer, oldest first.

    A hold is fresh when:

    * it was stamped for their latest message or a later one
      (reply_agent.is_fresh_hold, the rule the lane refuses a reply on); a
      newer message from them makes it stale and the lane answers it;
    * nobody has written since: their message is the thread's last. Until
      25 Sep 2026 inspect counted a hold as fresh while the person had been
      answered days earlier;
    * the campaign is not archived or deleted, and the outreach is still
      open. An archived campaign's reply is not an action anyone takes
      (api #1080), as in Needs attention.

    Rows carry the same keys as queries.waiting_candidates' rows, plus
    ``hold`` (the parsed payload).
    """
    params: list[Any] = []
    narrowing = q.narrowing_sql(campaign_id, outreach_id, params)
    stopped = sorted(q.STOPPED_CAMPAIGN_STATUSES)
    params += [*stopped, *_OPEN_STATUSES, *_INBOUND_ROLES, _HOLDS_MAX]
    db = get_db()
    try:
        rows = db.execute(
            f"""SELECT o.id AS outreach_id, o.campaign_id, o.status, o.next_action,
                       c.name AS contact_name, c.title, c.company, c.linkedin_url,
                       ca.name AS campaign_name, ca.status AS campaign_status,
                       m.text AS last_reply_text, m.sentiment AS last_sentiment,
                       m.timestamp AS last_message_ts
                FROM outreaches o
                JOIN contacts c ON o.contact_id = c.id
                JOIN messages m ON m.outreach_id = o.id
                LEFT JOIN campaigns ca ON ca.id = o.campaign_id
                WHERE json_valid(o.next_action)
                  AND json_extract(o.next_action, '$.type') = 'hold_for_operator'{narrowing}
                  AND COALESCE(ca.status, '') NOT IN ({", ".join("?" for _ in stopped)})
                  AND o.status IN ({", ".join("?" for _ in _OPEN_STATUSES)})
                  AND m.id = (
                      SELECT m2.id FROM messages m2
                      WHERE m2.outreach_id = o.id
                      ORDER BY m2.timestamp DESC LIMIT 1
                  )
                  AND m.role IN ({", ".join("?" for _ in _INBOUND_ROLES)})
                  AND COALESCE(m.sentiment, '') NOT IN ('opt_out', 'out_of_office')
                ORDER BY m.timestamp ASC
                LIMIT ?""",
            tuple(params),
        ).fetchall()
    finally:
        db.close()
    out: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        hold = parse_operator_hold(row.get("next_action"))
        if not hold or not is_fresh_hold(hold, int(row.get("last_message_ts") or 0)):
            continue
        out.append({**row, "hold": hold})
    return out


def who_is_waiting(
    *,
    kinds: Iterable[str] = PEOPLE_KINDS,
    campaign_id: str = "",
    outreach_id: str = "",
    now: int | None = None,
    min_age_seconds: int | None = None,
    limit: int | None = None,
) -> list[Waiting]:
    """Who is waiting on the user, oldest first.

    ``kinds`` narrows the whole answer, so a subset is exactly the subset:
    inspect(action='holds') is ``kinds=(HOLD,)``. ``limit`` caps the list;
    None keeps every row, as Needs attention always has.
    """
    now = int(time.time()) if now is None else int(now)
    wanted = set(kinds)
    held = fresh_operator_holds(campaign_id=campaign_id, outreach_id=outreach_id)
    rows = q.waiting_candidates(
        now=now, min_age_seconds=min_age_seconds, campaign_id=campaign_id,
        outreach_id=outreach_id, held=held,
    )
    held_because = {
        str(h["outreach_id"]): str((h.get("hold") or {}).get("reason") or "").strip()
        for h in held
    }
    people: list[Waiting] = []
    for row in rows:
        if row["kind"] not in wanted:
            continue
        if limit is not None and len(people) >= limit:
            break
        oid = str(row["outreach_id"])
        people.append(Waiting(
            kind=row["kind"],
            name=str(row.get("contact_name") or ""),
            outreach_id=oid,
            campaign_id=str(row.get("campaign_id") or ""),
            campaign_name=str(row.get("campaign_name") or ""),
            since=int(row.get("last_message_ts") or 0),
            why=str(row.get("reason") or ""),
            preview=str(row.get("last_reply_text") or "")[:_PREVIEW_CHARS],
            company=str(row.get("company") or ""),
            title=str(row.get("title") or ""),
            linkedin_url=str(row.get("linkedin_url") or ""),
            held_because=(held_because.get(oid) or "needs a human") if row["kind"] == HOLD else "",
            status=str(row.get("status") or ""),
            sentiment=str(row.get("last_sentiment") or ""),
            calendar_url=str(row.get("prospect_calendar_url") or ""),
        ))
    return people


def as_needs_attention_row(person: Waiting, *, now: int | None = None) -> dict[str, Any]:
    """The row get_unanswered_leads has always returned (show_status, the
    unanswered-lead alert and the daily digest read it)."""
    now = int(time.time()) if now is None else int(now)
    return {
        "outreach_id": person.outreach_id,
        "campaign_id": person.campaign_id,
        "campaign_name": person.campaign_name,
        "contact_name": person.name,
        "company": person.company,
        "title": person.title,
        "linkedin_url": person.linkedin_url,
        "status": person.status,
        "last_sentiment": person.sentiment,
        "last_message_ts": person.since,
        "last_message_preview": person.preview,
        "hours_unanswered": max(0, (now - person.since) // 3600),
        "reason": person.why,
        "prospect_calendar_url": person.calendar_url,
    }


def needs_attention(*, min_age_seconds: int | None = None, now: int | None = None) -> list[dict[str, Any]]:
    """Needs attention's rows: the people, in get_unanswered_leads' shape."""
    now = int(time.time()) if now is None else int(now)
    return [
        as_needs_attention_row(person, now=now)
        for person in who_is_waiting(now=now, min_age_seconds=min_age_seconds)
    ]


def _one_line(text: str, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _person_lines(person: Waiting) -> list[str]:
    from ..formatter import person_line

    who = person_line(person.name or "Unknown", person.linkedin_url,
                      title=person.title, company=person.company)
    lines = [f"• {who}", f"  {person.why}"]
    if person.held_because:
        lines.append(f"  held: {_one_line(person.held_because, _RENDER_PREVIEW_CHARS)}")
    if person.preview:
        lines.append(f'  "{_one_line(person.preview, _RENDER_PREVIEW_CHARS)}"')
    where = f"{person.campaign_name} · " if person.campaign_name else ""
    lines.append(f"  {where}outreach `{person.outreach_id[:8]}`")
    return lines


def render_waiting(people: list[Waiting]) -> str:
    """The text the tools print: the people waiting, oldest first.

    Short on purpose: check_replies puts it first, above a list that can run
    long, so each person costs a few lines, not a paragraph.
    """
    if not people:
        return "Nobody is waiting on you."
    lines = [f"Waiting on you ({len(people)}):", ""]
    for person in people:
        lines += [*_person_lines(person), ""]
    return "\n".join(lines).rstrip()
