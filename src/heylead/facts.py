"""The facts every public listing repeats, in one place.

A stranger meets HeyLead in the README, on PyPI, in the MCP registry's
server.json, on clawhub, in llms.txt or in a directory's listing copy before
they ever reach heylead.dev. On 24 Sep 2026 those files led with four
different sentences ("MCP-native autonomous LinkedIn SDR", "AI LinkedIn SDR
that runs inside Cursor", ...), the README's quota line omitted the ICP
generations the landing page prints, and the listing copy said "Pro: $29/mo
unlimited". A careful buyer reads two of them and assumes the product is as
unfinished as its copy.

Every string below is asserted verbatim in tests/test_listings_carry_the_facts.py
against each listing file, so a listing cannot drift from the facts without a
red test. The numbers come from constants.py, the same place the code reads
them. heylead-dashboard/src/marketing/facts.ts holds the same strings for
heylead.dev: change both in the same sitting.
"""

from __future__ import annotations

from . import constants as _c
from . import goals as _g

# The definition sentence from the copywriter skill
# (.claude/skills/heylead-copywriter/SKILL.md, rule 9), word for word, wherever
# a person is told what HeyLead is.
DEFINITION_SENTENCE = (
    "HeyLead is an AI agent for LinkedIn outreach: it finds the right people, "
    "writes to them in the voice of your own LinkedIn posts, follows up, and "
    "handles replies. It runs from Claude Code, Cursor, any MCP client or a "
    "web dashboard."
)

# The MCP registry's server.json caps description at 100 characters
# (server.schema.json 2025-12-11), so it carries this cut of the sentence.
DEFINITION_SHORT = (
    "AI agent for LinkedIn outreach: finds the right people, writes in your "
    "voice, follows up, replies."
)
assert len(DEFINITION_SHORT) <= 100

# The same definition for an assistant reading the MCP server's instructions:
# it is not the person whose posts carry the voice, so "your" becomes "the
# user's".
DEFINITION_FOR_AGENTS = (
    "HeyLead is an AI agent for LinkedIn outreach: it finds the right people, "
    "writes to them in the user's own voice, follows up, and handles replies, "
    "on the user's own LinkedIn account."
)

# What a self-hosted free install may do in a month. Hosted accounts are not
# capped this way (config.apply_free_monthly_caps); their invitation ceiling
# is the LinkedIn account's.
FREE_QUOTA_LIST = (
    f"{_c.FREE_MONTHLY_INVITATIONS} invitations, "
    f"{_c.FREE_MONTHLY_MESSAGES} messages, "
    f"{_c.FREE_MAX_ENGAGEMENTS} engagements, "
    f"{_c.FREE_MAX_CAMPAIGNS} active campaign, "
    f"{_c.FREE_MAX_ICP_V2_GENERATIONS} ICP generations"
)
FREE_QUOTA_SENTENCE = f"Self-hosted free installs have monthly quotas: {FREE_QUOTA_LIST}."

# Pro is priced per connected LinkedIn account, monthly. Never "$29/mo" and
# never "unlimited": the follow-up count and the seat count are the caps.
PRO_PRICE = f"${_c.PRO_PRICE_MONTHLY} per connected LinkedIn account per month"
FREE_PLAN_LINE = f"Free: $0, up to {_c.FREE_MAX_FOLLOWUPS} follow-ups per prospect"
PRO_PLAN_LINE = f"Pro: {PRO_PRICE}, up to {_c.PRO_MAX_FOLLOWUPS} follow-ups per prospect"

INVITE_LIMITS_LINE = (
    "Invitation limits follow the LinkedIn account (free, Premium or Sales "
    "Navigator), not the HeyLead plan."
)


# The six things a campaign can be for, in the order the dashboard shows them.
# One source: the goal table (goals.py, twin of heylead-api's). A listing that
# says "built for sales" has drifted: on 24 Sep 2026 an assistant told a job
# seeker exactly that, because nothing carried the goal (heylead-api#1153).
USE_CASES = tuple((g.key, g.label, g.line, g.complete) for g in _g.GOALS.values())
GOAL_LABELS = tuple(label for _, label, _, _ in USE_CASES)
USE_CASES_SENTENCE = (
    "A campaign has one of six goals, and the ICP, the fit check and the messages follow it: "
    + ", ".join(GOAL_LABELS[:-1]) + " and " + GOAL_LABELS[-1] + "."
)
_INCOMPLETE = [label for _, label, _, complete in USE_CASES if not complete]
INCOMPLETE_GOALS_SENTENCE = (
    ", ".join(_INCOMPLETE[:-1]) + " and " + _INCOMPLETE[-1]
    + " run on a custom brief until their message sets exist."
)


def use_cases_markdown() -> str:
    """The list as Markdown bullets, for the listing files."""
    return "\n".join(
        f"- **{label}**: {line}" + ("" if complete else " Works with a custom brief.")
        for _, label, line, complete in USE_CASES
    )


def use_cases_block() -> str:
    """The whole block a listing carries: sentence, bullets, caveat."""
    return f"{USE_CASES_SENTENCE}\n\n{use_cases_markdown()}\n\n{INCOMPLETE_GOALS_SENTENCE}"

# The pace, as what HeyLead does. Built from the ceilings the hosted scheduler
# enforces (constants.HOSTED_*_INVITE_CAP*) and the default sending window
# (heylead-api working_hours.DEFAULT_SENDING_WINDOW: Mon-Fri 08:00-22:00 in
# the owner's zone). On 24 Sep 2026 a new user's first chat session, with no
# HeyLead sentence about pace to repeat, warned him off LinkedIn automation
# and told him to hold back to a small daily volume behind a review step. This
# sentence is what every surface says instead; heylead-api app/facts.py and
# heylead-dashboard src/marketing/facts.ts carry it verbatim, and
# tests/test_onboarding_never_warns.py fails any surface that warns.
PACE_SENTENCE = (
    "HeyLead sends from your own LinkedIn account at a human pace: at most "
    f"{_c.HOSTED_DAILY_INVITE_CAP_FREE} invitations a day and "
    f"{_c.HOSTED_WEEKLY_INVITE_CAP} a week on a free LinkedIn account (more on "
    "Premium or Sales Navigator), Monday to Friday 08:00 to 22:00 in your time "
    "zone, minutes apart. It backs off when LinkedIn pushes back and resumes on "
    "its own. You can pause any campaign at any time."
)

# How an assistant talks about pace. It fills the vacuum the model otherwise
# fills with generic LinkedIn-automation lore.
MODEL_PACE_INSTRUCTION = (
    "When the user asks what happens after launch, give them the campaign's plan "
    "from the create_campaign or launch result. Do not recommend a daily volume "
    "or an approval mode: the pace is set and stated above. If asked about "
    "LinkedIn's terms, point to https://heylead.dev/terms. Do not call the "
    "pace safe or unsafe for the account; say what HeyLead does."
)

# A silent capability is assumed to be on: the same user assumed HeyLead would
# start publishing posts from his profile.
NO_POSTING_SENTENCE = (
    "HeyLead reads the user's LinkedIn posts only to learn their voice. It never "
    "publishes on their profile unless they ask for a post."
)

# What a first campaign can expect, from the founder's own workspace on
# 21 Sep 2026, the numbers the landing page already prints (heylead-api
# docs/website/2026-09-21-brand-and-messaging.md). A new user who launched on
# 24 Sep 2026 had no sense of what a normal first week looks like. Every number
# a person reads about acceptance or replies is built from this block and
# carries its n and its date; heylead-api app/facts.py and heylead-dashboard
# src/marketing/facts.ts pin the same sentences. A hosted workspace with enough
# of its own history gets its own numbers from the api instead.
# tests/test_expectation.py pins the text and fails any tool result that types
# an acceptance or reply percentage of its own.
FOUNDER_COHORT = {
    "label": "From the founder's own account, 21 Sep 2026 (n=405)",
    "invited": 405,
    "accepted_pct": 33,
    "accept_days_hours": "2 days 8 hours",
    "new_connections": 79,
    "replied": 14,
    "dated": "2026-09-21",
}


_MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _cohort_date(dated: str) -> str:
    # Spelled out rather than strftime("%b"), which follows the locale.
    from datetime import date

    day = date.fromisoformat(dated)
    return f"{day.day} {_MONTH_ABBR[day.month - 1]} {day.year}"


def expectation_sentences(cohort: dict) -> tuple[str, str, str]:
    """(accept sentence, reply sentence, label), built from a cohort block."""
    accept = (
        f"On the founder's own account, {cohort['accepted_pct']}% of "
        f"{cohort['invited']} invitations were accepted, "
        f"{cohort['accept_days_hours']} after sending on average."
    )
    reply = (
        f"On the same account, {cohort['replied']} of {cohort['new_connections']} "
        "new connections replied."
    )
    label = (
        f"From the founder's own account, {_cohort_date(cohort['dated'])} "
        f"(n={cohort['invited']})"
    )
    return accept, reply, label


ACCEPT_EXPECTATION, REPLY_EXPECTATION, EXPECTATION_LABEL = expectation_sentences(FOUNDER_COHORT)
assert EXPECTATION_LABEL == FOUNDER_COHORT["label"]


def with_expectation_label(sentence: str, label: str = EXPECTATION_LABEL) -> str:
    """A sentence as it is shown: the sentence, then its label in brackets."""
    return f"{sentence} ({label})"
