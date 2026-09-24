"""One goal vocabulary for a campaign, and what follows from it (client twin).

The client's twin of heylead-api's `app/services/goals.py`
(D4umak/heylead-api#1153). Serhii Kucher, 24 Sep 2026: a job seeker
described the role he wanted and the product, which had no field for a goal,
sold to the people holding that role and then told him "0% of the titles
decide". A campaign now carries `campaign_goal`; saving it also writes
`campaign_type` and `campaign_intent`, the two keys every existing reader
already understands. The tables are byte-identical to the api's and
`tests/test_campaign_goal.py` compares them when that checkout is found.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

SELL = "sell"
JOB_SEARCH = "job_search"
HIRE = "hire"
PARTNER = "partner"
BUY = "buy"
RESEARCH = "research"
VALID_GOALS = (SELL, JOB_SEARCH, HIRE, PARTNER, BUY, RESEARCH)
DEFAULT_GOAL = SELL


@dataclass(frozen=True)
class Goal:
    key: str
    label: str
    line: str
    # The person a persona must be, as a noun phrase after "someone who".
    persona: str
    # The noun the ICP summary and the fit judge use for one of them.
    noun: str
    fit_question: str
    complete: bool
    campaign_type: str
    campaign_intent: str
    # The fit judge's role vocabulary; the first entry is the deciding role.
    roles: tuple[str, ...]
    # What moves this persona: three prompt lines replacing "Buyer Psychology".
    moves: tuple[str, str, str]


GOALS: dict[str, Goal] = {
    SELL: Goal(
        key=SELL, label="Sell a product or service",
        line="Reach the people who buy what you built.",
        persona="can buy or champion the purchase", noun="customer",
        fit_question="Does this audience hold the budget?",
        complete=True, campaign_type="outbound", campaign_intent="sell",
        roles=("economic_buyer", "champion", "influencer", "user", "none"),
        moves=(
            "2-4 specific pain points this persona faces",
            "2-3 underlying fears driving buying decisions",
            "2-3 obstacles preventing them from buying",
        ),
    ),
    JOB_SEARCH: Goal(
        key=JOB_SEARCH, label="Find a job",
        line="Reach the people who hire for the role you want.",
        persona="hires for, or refers into, the role at a target company",
        noun="hiring manager",
        fit_question="Can this audience hire you or refer you?",
        complete=True, campaign_type="job_search", campaign_intent="sell",
        roles=("hiring_manager", "recruiter", "referrer", "peer", "none"),
        moves=(
            "2-4 things that make this persona open a message from a candidate",
            "2-3 reasons they ignore one",
            "2-3 things they need to see before they refer or interview",
        ),
    ),
    HIRE: Goal(
        key=HIRE, label="Hire people",
        line="Reach candidates for a role you are filling.",
        persona="would take the role", noun="candidate",
        fit_question="Would this audience take the role?",
        complete=False, campaign_type="outbound", campaign_intent="recruit",
        roles=("candidate", "manager_of_candidates", "none"),
        moves=(
            "2-4 things this persona wants from their next role",
            "2-3 reasons they would not move",
            "2-3 things they need to hear before they reply to a recruiter",
        ),
    ),
    PARTNER: Goal(
        key=PARTNER, label="Find partners or investors",
        line="Reach the people who can sign a partnership or an investment.",
        persona="owns partnerships or business development, or is the founder",
        noun="partner",
        fit_question="Can this audience sign a partnership?",
        complete=False, campaign_type="outbound", campaign_intent="partner",
        roles=("decides", "influences", "none"),
        moves=(
            "2-4 things this persona wants from a partner",
            "2-3 reasons a partnership stalls on their side",
            "2-3 things they need to see before they commit",
        ),
    ),
    BUY: Goal(
        key=BUY, label="Find a vendor",
        line="You are the buyer. Reach the people who sell what you need.",
        persona="sells or builds what you want to buy", noun="vendor",
        fit_question="Can this audience sell you this?",
        complete=True, campaign_type="outbound", campaign_intent="buy",
        roles=("sells_it", "builds_it", "none"),
        moves=(
            "2-4 things this persona needs to know to quote",
            "2-3 reasons they turn a buyer down",
            "2-3 things that make them prioritise a request",
        ),
    ),
    RESEARCH: Goal(
        key=RESEARCH, label="Research interviews",
        line="Reach people to interview, survey or test with.",
        persona="holds the experience you are researching", noun="participant",
        fit_question="Is this audience who you need to hear from?",
        complete=False, campaign_type="outbound", campaign_intent="research",
        roles=("has_the_experience", "adjacent", "none"),
        moves=(
            "2-4 things this persona has first-hand experience of",
            "2-3 reasons they decline an interview",
            "2-3 things that make them say yes to one",
        ),
    ),
}

_GOAL_BY_INTENT = {"recruit": HIRE, "partner": PARTNER, "buy": BUY, "research": RESEARCH}


def normalize_goal(value: Any) -> str | None:
    """The stored form of a submitted goal; None when it is not one.

    None and "" mean no preference, which is sell, today's behaviour. Anything
    else must be one of VALID_GOALS after trimming and lower-casing.
    """
    if value is None:
        return DEFAULT_GOAL
    if not isinstance(value, str):
        return None
    raw = value.strip().lower()
    if not raw:
        return DEFAULT_GOAL
    return raw if raw in VALID_GOALS else None


def derived_keys(goal: str) -> dict[str, str]:
    """The three config keys a goal writes, so every old reader keeps working."""
    g = GOALS[goal]
    return {"campaign_goal": g.key, "campaign_type": g.campaign_type, "campaign_intent": g.campaign_intent}


def goal_from_config(config: Mapping[str, Any] | str | Any) -> str:
    """The campaign's goal, from `campaign_goal` or, for older rows, the old keys.

    Takes a dict or the raw config_json string, like the client's
    intent.resolve_intent, so both repos read a row the same way.
    """
    if isinstance(config, str):
        try:
            config = json.loads(config or "{}")
        except (ValueError, TypeError):
            config = {}
    if not isinstance(config, Mapping):
        return DEFAULT_GOAL
    stored = normalize_goal(config.get("campaign_goal"))
    if stored and config.get("campaign_goal"):
        return stored
    if str(config.get("campaign_type") or "").strip().lower() == "job_search":
        return JOB_SEARCH
    intent = str(config.get("campaign_intent") or "").strip().lower()
    return _GOAL_BY_INTENT.get(intent, DEFAULT_GOAL)


def seniority_floor_applies(goal: str) -> bool:
    """Only a purchase has an owner/cxo/vp/director floor; managers hire."""
    return goal in (SELL, PARTNER, BUY)


def uses_sales_kb(goal: str) -> bool:
    """The sales-methodology cards are wrong evidence for hiring or research."""
    return goal in (SELL, PARTNER, BUY)


def needs_brief(goal: str) -> bool:
    """Goals that run on the project brief: job search names the role, and
    the incomplete goals have no template of their own."""
    return goal == JOB_SEARCH or not GOALS[goal].complete


def fit_copy(goal: str) -> dict[str, str]:
    """The words the fit box shows. The dashboard and the client render
    these and keep none of their own."""
    g = GOALS[goal]
    if goal == SELL:
        return {
            "heading": g.fit_question,
            "subtitle": "Checks the selected personas against your goal before you spend invites.",
            "match": "These are buyers for this goal",
            "partial": "Partly: the economic buyer may be missing",
            "mismatch": "This audience cannot buy what the goal asks for",
            "unknown": "Not enough to judge yet",
            "coverage": "{pct}% of the titles decide",
            "who": "Who buys",
        }
    per_goal = {
        JOB_SEARCH: (
            "These people hire for this role",
            "Partly: the hiring manager may be missing",
            "This audience holds the role you want; they do not hire for it",
            "{pct}% of the titles hire or refer", "Who hires",
        ),
        HIRE: (
            "These people would take the role",
            "Partly: some personas manage candidates rather than being one",
            "This audience would not take the role",
            "{pct}% of the titles are candidates", "Who would take it",
        ),
        PARTNER: (
            "These people can sign a partnership",
            "Partly: the person who decides may be missing",
            "This audience cannot sign what the goal asks for",
            "{pct}% of the titles decide", "Who decides",
        ),
        BUY: (
            "These people sell what you need",
            "Partly: some personas build it but do not sell it",
            "This audience does not sell what you need",
            "{pct}% of the titles sell it", "Who sells it",
        ),
        RESEARCH: (
            "These people have the experience you need",
            "Partly: some personas are adjacent to it",
            "This audience does not hold the experience you are researching",
            "{pct}% of the titles have the experience", "Who has the experience",
        ),
    }
    match, partial, mismatch, coverage, who = per_goal[goal]
    return {
        "heading": g.fit_question,
        "subtitle": "Checks the selected personas against your goal before you spend invites.",
        "match": match, "partial": partial, "mismatch": mismatch,
        "unknown": "Not enough to judge yet", "coverage": coverage, "who": who,
    }


def as_table() -> list[dict[str, Any]]:
    """The whole table as plain data, for the cross-repo parity test."""
    return [
        {
            "key": g.key, "label": g.label, "line": g.line, "persona": g.persona,
            "noun": g.noun, "fit_question": g.fit_question, "complete": g.complete,
            "campaign_type": g.campaign_type, "campaign_intent": g.campaign_intent,
            "roles": list(g.roles), "moves": list(g.moves), "copy": fit_copy(g.key),
        }
        for g in GOALS.values()
    ]
