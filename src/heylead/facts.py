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

# Pro is priced per connected account, a LinkedIn seat or an email mailbox, the
# way Unipile bills (billing.billable_quantity_for_org; Denys, 7 Oct 2026, api
# #2366), monthly. Never "$29/mo" and never "unlimited".
PRO_PRICE = f"${_c.PRO_PRICE_MONTHLY} per connected account per month"
# Hosted Free runs until the workspace's 2nd hot lead (api #5890, 10 Nov 2026):
# from then HeyLead keeps the open conversations going and reaches new people
# on Pro. Pro is sold on that alone: never on the follow-up count, the campaign
# count or a second connected account (Denys, 12 Nov 2026: "We DO NOT sell
# follow-ups"; api #6596). Those limits are stated once, as plain facts, in
# PLAN_FACTS_LINE. Denys approved these words one by one (api #6596 comment
# 6983917491); change none of them without his yes. Every listing prints the
# two long lines, PLAN_FACTS_LINE once after them, and the definition once
# (tests/test_listings_carry_the_facts.py).
FREE_HOSTED_LINE = (
    "$0, one connected account (a LinkedIn seat or an email mailbox), cloud "
    "sending included, until your 2nd hot lead. From then HeyLead keeps working "
    "the conversations already open and reaches new people on Pro."
)
PRO_HOSTED_LINE = (
    f"{PRO_PRICE} (a LinkedIn seat or an email mailbox). HeyLead keeps "
    "reaching new people after your 2nd hot lead. Monthly, cancel any time, no setup fee."
)
HOT_LEAD_DEFINITION = "A hot lead is a person whose reply HeyLead reads as interested."
# One plain literal, character for character the dashboard's PLAN_FACTS_LINE
# (src/marketing/facts.ts); its md5 is pinned in both repos' tests, and its
# figures are checked against constants.FREE/PRO_MAX_FOLLOWUPS there.
PLAN_FACTS_LINE = (
    "Plan limits. Active campaigns at once: 1 on Free, more than 1 on Pro. "
    "Follow-ups per person: up to 2 on Free, up to 5 on Pro. Connected accounts "
    "in one workspace: one on Free (a LinkedIn seat or an email mailbox), a "
    "LinkedIn seat and an email mailbox together on Pro."
)
# What the unit is, under every price table (api #2366): Stripe counts one
# seat per connected LinkedIn account or email mailbox in the workspace.
CONNECTED_ACCOUNT_SENTENCE = (
    "A connected account is a LinkedIn seat or an email mailbox. Free includes one of either."
)

INVITE_LIMITS_LINE = (
    "Invitation limits follow the LinkedIn account (free, Premium or Sales "
    "Navigator), not the HeyLead plan."
)

# What a self-hosted free install reads when a monthly quota is reached. Until
# 7 Oct 2026 seven tools said "Upgrade to Pro ($29/mo) for unlimited ...", the
# two forms this module forbids above (api #2328); Pro lifts the install's
# monthly quota, and the LinkedIn account's invitation ceiling still applies.
PRO_UPGRADE_LINE = f"Pro ({PRO_PRICE}) lifts this monthly quota. {INVITE_LIMITS_LINE}"


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


# The pace, as what HeyLead does. On 24 Sep 2026 a new user's first chat
# session, with no HeyLead sentence about pace to repeat, warned him off
# LinkedIn automation and told him to hold back to a small daily volume behind a
# review step. This sentence is what every surface says instead, and
# tests/test_onboarding_never_warns.py fails any surface that warns.
#
# Built at call time (heylead-api #7686, words approved by Denys on 23 Nov 2026,
# issue comment 7100616867, key pace_sentence_en): the old PACE_SENTENCE
# constants were built at import, and told every reader that a paid account
# grows two-fold weekly up to its limit, while the scheduler holds HeyLead Free at
# 8/40 and Pro with Premium or Sales Navigator at 320 a week, 168 at most in one
# day, after weekly steps. heylead-api app/facts.py pace_sentence() writes the
# same words from plan_pace.invite_limits_matrix(); this one reads the client's
# copy of those caps (constants.PLAN_FREE_* and HOSTED_*, held to the backend by
# tests/test_plan_pace_facts_6254.py and test_invite_caps_agree_with_the_backend.py).
SENDING_WINDOW = "Monday to Friday 08:00 to 22:00 in your time zone"

_PACE_TAIL = (
    f"{SENDING_WINDOW}, minutes apart. "
    "It backs off when LinkedIn pushes back, even below a ceiling, and resumes on its own; "
    "a step up then waits. You can pause any campaign at any time."
)


def invite_limits_matrix() -> dict:
    """The invitation ceilings by HeyLead plan and LinkedIn account, the shape of the api's
    ``plan_pace.invite_limits_matrix()`` (``GET /api/v1/public/invite-limits``), from the
    client's copy of the backend defaults. Read at call time, never at import."""
    from heylead.services.invite_pace import RAMP_DAILY_STEPS

    def tier(daily: int, weekly: int) -> dict:
        steps = []
        for step in range(1, len(RAMP_DAILY_STEPS) + 2):
            d = daily if step > len(RAMP_DAILY_STEPS) else min(daily, RAMP_DAILY_STEPS[step - 1])
            w = weekly if step > len(RAMP_DAILY_STEPS) else min(weekly, d * 5)
            steps.append({"daily": d, "weekly": w})
        return {"daily": daily, "weekly": weekly, "steps": steps}

    free = {"daily": _c.PLAN_FREE_DAILY_INVITES, "weekly": _c.PLAN_FREE_WEEKLY_INVITES}
    paid = (_c.HOSTED_DAILY_INVITE_CAP_PAID, _c.HOSTED_WEEKLY_INVITE_CAP_PAID)
    return {
        "free": {t: dict(free) for t in ("free", "premium", "sales_nav")},
        "pro": {
            "free": tier(_c.HOSTED_DAILY_INVITE_CAP_FREE, _c.HOSTED_WEEKLY_INVITE_CAP),
            "premium": tier(*paid),
            "sales_nav": tier(*paid),
        },
    }


_TIER_NAMES = {"premium": "Premium", "sales_nav": "Sales Navigator"}


def pace_sentence(matrix: dict | None = None) -> str:
    """The approved pace sentence (api #7686, key pace_sentence_en), figures from ``matrix``
    (default: ``invite_limits_matrix()``). The twin of heylead-api ``facts._pace_sentence_en``:
    every ceiling pair goes through ``invite_pace.public_pace_clause`` and the ramp through
    ``public_ramp_clause``, so a paid week is stated first and its day only as a ceiling."""
    from heylead.services import invite_pace as ip

    m = matrix or invite_limits_matrix()
    free, pro = m["free"]["free"], m["pro"]
    parts = [
        "HeyLead sends from your own LinkedIn account at a human pace.",
        f"On HeyLead Free: {ip.public_pace_clause(free['daily'], free['weekly'])}, on any LinkedIn account.",
        f"On Pro: {ip.public_pace_clause(pro['free']['daily'], pro['free']['weekly'])} on a free LinkedIn account.",
    ]
    paid = ["premium", "sales_nav"]
    groups = [paid] if pro["premium"] == pro["sales_nav"] else [[t] for t in paid]
    for tiers in groups:
        tier = pro[tiers[0]]
        names = " or ".join(_TIER_NAMES[t] for t in tiers)
        parts.append(f"On Pro with {names}: {ip.public_pace_clause(tier['daily'], tier['weekly'])}.")
        ramp = ip.public_ramp_clause([s["daily"] for s in tier["steps"]])
        if ramp and int(tier["daily"]) > int(tier["steps"][0]["daily"]):
            parts.append(f"Such an account {ramp}, counting from its first invitation or its move to Pro.")
    parts.append(_PACE_TAIL)
    return " ".join(parts)


# What a tool says about cloud sending. The cloud keeps working when the
# laptop is closed, but it sends only inside the window above: on 30 Sep 2026
# (heylead-api#2025) the launch, resume and scheduler tools still said "24/7".
CLOUD_KEEPS_SENDING = (
    "The cloud keeps running with your laptop closed and sends inside the "
    f"sending window, {SENDING_WINDOW}."
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
    "HeyLead learns the user's voice from their own LinkedIn posts. On heylead.dev, "
    "the user can also let it learn from messages they wrote themselves on LinkedIn: "
    "that is off until they turn it on, it never learns from messages other people "
    "sent them, and it never quotes those messages. It also learns their style "
    "from the edits they make to drafts before approving them, never the words "
    "themselves, and one switch on heylead.dev turns that off for the workspace. "
    "It never publishes on their profile unless they ask for a post."
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
