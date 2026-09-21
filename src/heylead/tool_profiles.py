"""Which tools a HeyLead client sees by default, and how to see the rest.

Glama scored HeyLead 2 out of 5 on tool count at 35 tools; the read/write split
(v0.10.388) took it to 47. The number is not a vanity metric. A client loads
every tool's name, description and schema into its context before it can choose
one, so a long list costs the user on every single turn — and a first-time user
does not need a CRM sync, a shared network pool or the local git agent to send
their first campaign.

So the default surface is the core set below: the tools the ordinary path
through the product uses, each read half beside its write half. Everything else
is registered only when ``HEYLEAD_TOOLS=all``. Nothing is deleted and nothing
is renamed — a person who relies on ``brand_strategy`` or ``crm_sync`` sets the
variable once and has exactly what they had before.

Kept out, and why:

* **Brand and content** (brand_strategy, brand_progress, create_post) — a
  second product area. Someone who wants it knows they want it.
* **Signals** (signals, tune_signals, manage_watchlist, profile_signals) — the
  scheduler uses them; a person rarely drives them by hand.
* **Profile editing** (profile, restore_profile) and **the local agent**
  (product) — the last of these patches this checkout and has no business on a
  first run at all.
* **Bulk and integrations** (import_prospects, crm_sync, send_email,
  backfill_inbox, update_network, network) — real work, not first work.
* **Knowledge** (knowledge, update_knowledge), **icp** (the preview; the
  generator stays), **inspect** (an operator's debugger), **organization**
  (hosted workspaces), **partner/partners**, **engage_prospect** and
  **generate_and_send** (send_message and the scheduler cover the path).
"""

from __future__ import annotations

from typing import Mapping

# The variable that widens the surface, and what it accepts.
ENV_VAR = "HEYLEAD_TOOLS"
PROFILES = ("core", "all")
DEFAULT_PROFILE = "core"

# The default surface. Read halves and write halves travel together: shipping
# a writer without its reader would undo the split that made a read-only
# connection possible.
CORE: frozenset[str] = frozenset({
    # getting started
    "setup_profile",
    "show_status",
    "suggest_next_action",
    # who to reach
    "generate_icp",
    "contacts",
    "update_contact",
    # campaigns
    "create_campaign",
    "edit_campaign",
    "campaign",
    "campaign_status",
    # the conversation
    "check_replies",
    "inbox",
    "answer_inbox",
    "send_message",
    "prospect",
    "prospect_view",
    "book_meeting",
    # running it
    "scheduler",
    "scheduler_status",
    "account",
    "accounts",
    "analytics",
})


def profile_from_env(env: Mapping[str, str]) -> str:
    """Which surface this process serves.

    Anything unrecognised is the core set rather than a guess: a typo that
    silently served 47 tools would be the failure this module exists to stop.
    """
    asked = str(env.get(ENV_VAR) or "").strip().lower()
    return asked if asked in PROFILES else DEFAULT_PROFILE


def keep(name: str, profile: str) -> bool:
    """Is this tool part of the named surface?"""
    if profile == "all":
        return True
    return name in CORE


def narrow_instructions(text: str, served: list[str], profile: str) -> str:
    """Rewrite the "It has N tools: ..." sentence to name what is served.

    The sentence in ``server.py`` lists every tool, in an order somebody chose
    (reads first, then writes). Keeping a second, hand-maintained list for the
    default surface is how the two drift, so the sentence is filtered here
    instead: the order survives, the membership is whatever this process
    actually registered.

    On the core profile it also says how to get the rest, because a person
    whose agent cannot find ``create_post`` has no other way to learn why.
    """
    import re

    match = re.search(r"It has \d+ tools: (.*?)\.\n", text, re.S)
    if not match:
        return text
    authored = [t.strip() for t in re.split(r",\s*|\s+and\s+", match.group(1))]
    kept = [name for name in authored if name in set(served)]
    if not kept:
        return text
    listed = ", ".join(kept[:-1]) + " and " + kept[-1] if len(kept) > 1 else kept[0]
    sentence = f"It has {len(kept)} tools: {listed}.\n"
    if profile != "all":
        sentence += (
            "Brand posts, signals, bulk import, CRM sync, the shared network "
            "pool and the local repository agent are served only when the "
            "client is started with HEYLEAD_TOOLS=all in its environment.\n"
        )
    return text[:match.start()] + sentence + text[match.end():]
