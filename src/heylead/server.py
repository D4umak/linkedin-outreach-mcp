"""HeyLead MCP Server — the heart of the product.

Registers all MCP tools and runs via stdio or streamable-http transport.

Usage (Claude Code):
    claude mcp add heylead -- uvx heylead

Usage (Cursor): add an MCP server with command `uvx heylead`.

Remote alternative (hosted endpoint, nothing runs locally):
    claude mcp add --transport http heylead https://heylead.dev/mcp
"""

from __future__ import annotations

import logging
from typing import Optional

from mcp.server.fastmcp import FastMCP, Image
from mcp.types import ToolAnnotations

from . import __version__, config
from .logging_setup import setup_logging
from .ops_log import run_traced


# ──────────────────────────────────────────────
# Changelog (exposed via heylead://changelog resource)
# ──────────────────────────────────────────────

_CHANGELOG = """\
# HeyLead Changelog

## v0.10.383 (2026-09-19)
- Fix: the inbox tool told your AI client it only reads, although replying and approving a drafted reply send a LinkedIn message. A client that runs read-only tools without asking could send without your say-so. It is now marked as sending, and its description says which actions only read

## v0.10.382 (2026-09-19)
- Fix: the edit_campaign description now says the default sending window (weekdays 08:00-22:00) is in your own timezone, and London only when your timezone is unknown. It said London for everyone, which has not been true since 19 Sep

## v0.10.381 (2026-09-19)
- public docs stop advertising voice memos, which are off by default and unused
- README, SKILL and clawhub state what Stripe charges and what the code limits

## v0.10.380 (2026-09-19)
- Change: campaigns send only in business hours unless you switch that off: Monday to Friday, 08:00-22:00 in your own timezone when your workspace has not set a window. show_status now names only a campaign that switched business hours off, and the create_campaign / edit_campaign docs say it is on by default

## v0.10.379 (2026-09-19)
- Fix: check_replies no longer says a declined outreach is closed when it is not. HeyLead's cloud answers a "no" with a short, polite close and then closes the outreach; the reply line now says so
- Fix: Needs attention lists a declined reply whose closing message could not be sent ("Declined, closing reply unsent") or that is held for you ("Declined, held for you"), and an unanswered engaged reply, as the dashboard already does
- Fix: asking HeyLead to reply to someone who said no no longer marks them opted out (which stopped all future contact); the cloud sends the polite close instead
- Change: in show_conversation, the reply move HeyLead chose for its own message shows as "[our move: ...]"

## v0.10.378 (2026-09-18)
- Removed an unused LinkedIn webhook path. Replies and accepted invitations were already detected only by check_replies, so nothing changes in behaviour.

## v0.10.377 (2026-09-18)
- Fix: on hosted accounts, show_status, suggest_next_action and the daily digest now show the daily and weekly invitation limits your HeyLead backend actually enforces for your LinkedIn seat, instead of a number built into this client (it could read "0/80 today" on a seat stopped at 20)

## v0.10.376 (2026-09-18)
- Change: new campaigns start with voice memos off (voice_mode='text_only'). Pass voice_mode='mixed' to turn them on for a campaign. Existing campaigns keep their setting

## v0.10.375 (2026-09-18)
- New: every tool tells your AI client whether it only reads or can act, so a client can ask before anything is sent, published or deleted; six tools are read-only (show_status, analytics, inspect, suggest_next_action, icp, inbox)
- New: HeyLead's description to your AI now covers every job it does, not only sales: recruiting, user-interview and research recruitment, job search, investor and partner outreach, vendor scouting, event invitations and posts. It also says which message sets are complete: selling and buying are; recruiting and partner outreach share the selling templates, so give those a clear project_brief
- Fix: docs no longer advertise copilot mode, which was removed; a campaign stays a draft until you launch it
- Fix: `heylead://capabilities` reports the real number of tools
- The PyPI source package now contains only the package itself

## v0.10.374 (2026-09-17)
- Setup now names the selected workspace and explains which LinkedIn seat sends.
- Hosted setup points to campaign launch and identifies the client to the dashboard.

## v0.10.373 (2026-09-16)
- Fix: a setup message copied inside one of your HeyLead workspaces now signs HeyLead into THAT workspace, so it works with that workspace's own LinkedIn seat instead of your default workspace's; setup says which workspace it landed in, and says so too when it could not find out (heylead-api #601)
- Fix: when the workspace HeyLead is using is no longer available — you left it, or it was deleted — the error now names the way out (`organization(action='list')`, then switch) instead of repeating "Organization not found" on every call
- Fix: `organization(action='create')` only switches you to the new workspace when it actually got one back, instead of clearing your current workspace and reporting a switch that did not happen

## v0.10.372 (2026-09-15)
- Fix: every LinkedIn tier shares one invitation ceiling — 20 a day, against the hosted 100 a week. Sales Navigator buys search depth and InMail, not a bigger invitation allowance: a real SN seat was refused by LinkedIn at 336 invitations over a rolling week.

## v0.10.371 (2026-09-15)
- Fix: the laptop no longer pushes cloud-owned outreach funnel state.
- Fix: a newer cloud decision can correct a stale locally closed outcome, while opt-outs remain terminal.

## v0.10.370 (2026-09-14)
- Fix: the objection cards behind reply handling carry short, concrete hooks drawn from the buyer profile instead of abstract phrasing; measured on replies, they beat sending no card (heylead-api #577)

## v0.10.369 (2026-09-14)
- Fix: a prospect's job title is read the way LinkedIn writes it -- a headline of roles, slogans and employers -- so "Vice President of Engineering" reaches the CTO card instead of the founder's, "Director, Product Management" reaches the product card, and a past role or a "founders office" no longer borrows a buyer's card (heylead-api #574)

## v0.10.368 (2026-09-14)
- Fix: signal activation, inbound handling, live threads and referrals pick a campaign with one matcher. A signal with no ICP overlap can only fall back to a campaign that has an ICP, and a campaign with discovery off or limited to existing connections is never a target on any path (#361)

## v0.10.367 (2026-09-14)
- Fix: buyer titles written with "of" ("VP of Engineering", "VP of Product", "Director of Marketing") and twenty-one more real decision-maker titles (Chief Commercial Officer, Head of Growth, Head of HR, Chief Technical Officer, Chief Transformation Officer, ...) now match their buyer-role card; a quarter of contacted prospects had been getting none (heylead-api #567)

## v0.10.366 (2026-09-14)
- Fix: the buyer-role cards behind ICP generation and goal matching carry their original short, concrete messaging hooks again (for example "benchmark comparisons", "ROI clarity"). The rebuilt v2 wording had been abstract consultant phrasing; measured on invitations, the concrete hooks beat sending no card at all (heylead-api #558)

## v0.10.365 (2026-09-14)
- Release version bump: the same code as v0.10.364, cut by two sessions a minute apart. The changes are listed under v0.10.364

## v0.10.364 (2026-09-14)
- Fix: HeyLead no longer records a message as sent when it was not. A follow-up that could not go out — no LinkedIn connected, the campaign missing, no email address for the prospect — was counted as outreach in your daily plan and your dashboards. It now shows as not sent, and is planned again. A DM whose delivery LinkedIn could not confirm now retries on the next tick, which is what its message always promised (#354)
- Fix: when booking a meeting fails because HeyLead has no access to your calendar, you get the link that grants it instead of a generic failure. `create_calendar_event` was defined twice and the older version won (#356)
- Fix: title and keyword matching compares whole terms, so "CTO" no longer matches inside "director" and the nineteen remaining substring matchers stop pulling in people who do not fit (#356)

## v0.10.363 (2026-09-14)
- Change: HeyLead sends from the cloud only. `generate_and_send` and `send_message` (followup, reply, voice, inmail) no longer send from your machine when the cloud owns sending — your campaigns send on their own, and `campaign(action='launch')` starts a deliberate send. `send_message(action='delete')` still works, so you can still undo a message you just sent (#350)

## v0.10.362 (2026-09-14)
- Change: the sales-methodology knowledge base is rebuilt from 22 sales and copywriting books. 383 technique cards, each one principle in its own words with a chapter citation, and 68 buyer-role cards covering 17 roles across four stages. ICP generation and goal matching read the new cards (#352, heylead-api #491)
- Fix: the 20 objection cards were empty, so a negative reply got no help from the knowledge base. They now carry real angles drawn from the linked techniques (#352)
- Fix: an invented "+15-30%" impact figure no longer appears on any card (#352)
- Fix: when a reply is recognised as a particular objection, HeyLead now answers that objection. It used to pick whichever technique shared the most words with the reply, so a price objection could be answered with something else entirely (#352)

## v0.10.361 (2026-09-14)
- Fix: `scheduler(action="report", hours=24)` now actually sets a daily report interval. It reported success and left the stored interval unchanged, because 24 — the default for the shared `hours` argument — was being used to mean "no interval given", and 24 is itself a valid interval (#351)

## v0.10.360 (2026-09-14)
- New: `knowledge` tool (hosted accounts): list, add, remove, refresh or search the sources that ground generated messages: your website, campaign briefs, uploads and anonymised reply-winning messages. HeyLead now has 35 tools (#327)
- Change: hosted invites, DMs, follow-ups, InMails, emails and replies are drafted from evidence retrieved from that knowledge, and a name the evidence grounds is no longer treated as invented (heylead-api #359, #360, #370, #371, #385)

## v0.10.359 (2026-09-13)
- Fix: when HeyLead refuses a LinkedIn action for the active workspace, you now see the server's reason (e.g. this workspace has no LinkedIn of its own, or your role cannot send) instead of "No LinkedIn account connected." (#349)

## v0.10.358 (2026-09-13)
- New: set a weekly meetings target per campaign with `edit_campaign(weekly_meeting_target=N)`; the daily report reads it as that campaign's Key Result, and 0 means no goal this week (#345)
- Fix: invite notes and DMs written in Chinese, Japanese or Thai are no longer refused as unreadable — those scripts do not put spaces between words (#348)
- Fix: the first message in a LinkedIn thread is written as an intro rather than a follow-up, and outgoing text keeps real spaces so it wraps on mobile (#344)

## v0.10.357 (2026-09-12)
- Fix: a first-time self-hosted LinkedIn connect no longer offers Unipile's cookie login, and a reconnect after a dead session keeps it (#341)
- Change: new campaigns follow up on days 1, 3, 7, 14 (#340)
- Fix: sync no longer stands down for a campaign the host omitted (#339)

## v0.10.356 (2026-09-11)
- New: status replies (show_status, campaign, analytics report, suggest_next_action) end with a link to the matching heylead.dev/dashboard page
- New: on hosted accounts those replies attach a PNG snapshot card of the dashboard state as a second content block
- New: `"dashboard_snapshots": false` in ~/.heylead/config.json keeps the link and drops the snapshot image
- Change: requires mcp>=1.10 (structured_output=False for tools that return an image)
- New: link status replies to the dashboard and attach snapshot cards (#337)
- Fix: ignore invite notes in the 24h conversation gap (#336)

## v0.10.355 (2026-09-11)
- Fix: first-run setup names the real buttons and the dashboard's Chat client setup message, and every setup message promises about 2 minutes (#331)
- Fix: a JWT pasted for another service can no longer replace a working HeyLead setup; a new token is checked before it is stored, and switching accounts drops the old workspace (#331)
- Fix: show_status tells a never-connected workspace to connect LinkedIn instead of saying its session expired (#331)
- Fix: MCP clients see HeyLead's own version, and docs state the real tool count, install command, privacy split and launch semantics (#331)
- Fix: trust the hosted LinkedIn composer on tier redetect (#332)
- New: exclude competitor employers from campaign outreach (#334)
- New: inspect can review a hosted campaign-stall watch (#335)

## v0.10.354 (2026-09-11)
- Fix: Sales Navigator and Premium use their own invite pace
- Fix: require campaign_id to clear a coordinator hold
- New: proxy hosted journal and clear coordinator holds

## v0.10.353 (2026-09-11)
- Fix: normalise the action before the viewer gate

## v0.10.352 (2026-09-11)
- New: skip existing connections by default; a founder is always a decision maker

## v0.10.351 (2026-09-10)
- Fix: send Unipile's Sales Navigator seniority names, not our keys

## v0.10.350 (2026-09-10)
- Fix: read a workspace seat's identity from its accounts entry, never /users/me

## v0.10.349 (2026-09-10)
- Fix: an ICP or campaign speaks as the workspace's seat, not the laptop
- Fix: a scoped conventional prefix renders as a Fix/New bullet

## v0.10.348 (2026-09-10)
- Fix: real reference titles, persona aliases, one seniority vocabulary

## v0.10.347 (2026-09-10)
- The published tree no longer ships live-campaign names, emails, or LinkedIn slugs in tests and comments
- Fix: a hosted pause can unpark because last_attempt_error now reaches the cloud (#320)

## v0.10.346 (2026-09-10)
- Fix: the PyPI-rollback reinstall hint uses this checkout, not a hardcoded path
- The published tree no longer ships internal audit notes or client identifiers

## v0.10.345 (2026-09-10)
- Fix: leaving observe resumes campaigns in the cloud; a failed cloud
  call names a command that actually retries it (#319)

## v0.10.344 (2026-09-10)
- New: brand_strategy(action="set_photo_library", focus="<folder>") lets
  brand-calendar posts attach the photo from your own folder that fits the
  post. Personal and family photos are never used, the previous post's photo
  is skipped, and any miss (including a photo LinkedIn rejects) posts text
  only. Local posting only: cloud-published brand posts stay text only (#318)
- Fix: local campaign copies follow pauses and archives made in the cloud, so
  the periodic push no longer resends "active" for them (#317)
- Fix: status shows the hosted weekly cap, free-tier caps, pause wording,
  campaign defaults and migrations as they really are (#316)
- Fix: partner reminders send body_html to the hosted proxy (#315)

## v0.10.343 (2026-09-10)
- New: create_campaign and edit_campaign take campaign_type — outbound or
  job_search (#306)
- New: the goal ↔ ICP judge runs where campaigns are made (#312)
- Fix: a job-search campaign never books a meeting, auto-replies, enrols a
  referral, or takes an inbound reply (#313)
- Fix: BackendClient keeps a relation's connection date (#311)

## v0.10.342 (2026-09-10)
- New: replies to comments on your own posts are drafted for you and wait
  in the inbox. inbox(action='comment_drafts') shows them as LinkedIn will
  render them; approve_draft sends one (optionally edited), discard_draft
  throws one away. Nothing is sent without approval (#309)

## v0.10.341 (2026-09-10)
- fix(replies): 14-day auto-book window, and advance revisit by threads asked (#310)

## v0.10.340 (2026-09-10)
- fix(agents): book only a prospect's slot, and close the leftover review holes (#307)
- Exclude existing 1st-degree connections (client) (#283)
- KB port + goal↔ICP match (client) — task 07 (#281)
- fix(agents): unknown reshares are not own voice, and a hold actually stops work (#305)

## v0.10.339 (2026-09-09)
- New: in-process agents default to act. Unset campaign flags apply
  decisions; edit_campaign(...="observe") persists the mode so clearing
  keys does not turn an agent back on (#304)
- New: product() can patch this HeyLead git checkout and open a PR.
  Cloud workers and uvx installs without .git refuse. Never from the
  send path (#304)
- Fix: commons notes write through the async DB bridge, so
  write_commons persists on the event loop (#304)

## v0.10.338 (2026-09-09)
- New: a reply to a comment now threads under the comment it answers, and can
  @mention the person so LinkedIn notifies them. Every reply used to land as a
  new top-level comment, and a name in the text was just text (#303)
- Fix: a freshly started server no longer fails on the first tool that needs
  the LinkedIn account. The account id was read on the event loop, which the
  database layer refuses; it cached after one success, so the error landed on
  whichever call happened to be first. 67 call sites, plus a check that stops
  the pattern returning (#295, #299, #300)
- Fix: every reply path uses the whole thread (#264)
- Fix: observe is log-only, and a failed book is a hold (#302)
- Fix: switch the cloud off before local sending starts (#273)
- New: one seniority vocabulary, enforced in scoring and the Classic filter (#274)
- New: Skip vs Stop — structured reasons and real terminal statuses (#278)

## v0.10.337 (2026-09-09)
- Fix: create_post no longer fails on a freshly started server. It read the
  account id on the asyncio event loop, which the database layer refuses, and
  the read caches after it succeeds once — so the error only appeared when
  create_post was the first thing in a process to want the account, which is
  exactly what a just-restarted MCP server is (#295)
- Fix: reply detection now reaches threads that scrolled off the recent inbox
  page; each sweep asks LinkedIn directly for a rotating slice of 15 open rows
  not on the page (#296)

## v0.10.336 (2026-09-09)
- Fix: a prospect deleted on this machine now leaves the hosted store too.
  The backend has accepted deleted_outreach_ids on the sync push since
  August and the client never sent the field, so a prospect the sendable-queue
  repair removed here stayed alive in the cloud and the very next pull put the
  row back — 577 hosted outreaches against 50 local. Each deletion now records
  a tombstone, the ids travel in their own trailing chunk on the next full
  sync, and until that push lands the pull refuses to re-adopt them, so a
  deleted prospect cannot return in the window between the two (#276)
- Fix: the post writer sounds like you again. A reshare is now recognised
  wherever a post is fetched and kept out of voice analysis, so someone
  else's words no longer shape your voice signature; drafts are judged as
  posts against that real signature; and the writer is shown your own posts
  as examples. no_go is also read as whole terms instead of being split on
  commas (#286)
- New: attach an image to a post (#289)

## v0.10.335 (2026-09-09)
- New: brand_strategy(action="set_summary", focus="...") sets the About
  section to exactly the text you pass, the way set_headline does for the
  headline. execute and makeover write model-generated copy, so a summary
  you had already written could not be applied from HeyLead at all. Only
  surrounding whitespace is trimmed, so paragraph breaks survive, and text
  over the 2600-character LinkedIn limit is refused rather than clipped.
  Both actions now share one write path (#282)

## v0.10.334 (2026-09-09)
- New: after each sibling loop a coordinator upserts a digest and
  beat. inspect(action='commons') shows it; act can persist a
  campaign hold. edit_campaign(enable_coordinator_agent=on).
  Never sends LinkedIn (#279)

## v0.10.333 (2026-09-09)
- New: in-process agents leave an inspectable commons — a heartbeat after
  every loop, and one short note per run for the next tick.
  inspect(action='commons') shows beats, live notes, and who went dark
  (#275)

## v0.10.332 (2026-09-09)
- New: brand_strategy(action="set_headline", focus="...") sets the LinkedIn
  headline to exactly the text you pass. execute and makeover both write a
  model-generated headline, and the headline A/B test is off, so a headline
  you had already decided on could not be applied from HeyLead at all. The
  write goes through the normal profile-change path, so it is logged and
  restorable with profile(action="restore"), and it is refused over the
  220-character LinkedIn limit instead of being silently clipped (#263)

## v0.10.331 (2026-09-09)
- New: edit_campaign can turn the reply, strategist, and closer agents
  to act, off, or observe. Hosts no longer have to invent those kwargs
  (#272)

## v0.10.330 (2026-09-09)
- New: inspect(action='jobs') lists pending scheduler jobs and recent
  gated-job refusals, and explains observe / cloud ownership when the
  queue is empty (#271)

## v0.10.329 (2026-09-09)
- New: host docs now tell Cursor/Claude/OpenClaw to call inspect()
  before sending when someone asks who is held, and to reply through
  send_message rather than inventing act-flag kwargs. OpenClaw
  quickstart uses target_description (#270)

## v0.10.328 (2026-09-09)
- New: inspect() is a read-only digest of operator holds, today's
  strategist replans, hot-lead closer decisions, and recent reply skips
  (#269)

## v0.10.327 (2026-09-09)
- New: a book-only hot-lead closer can place a Google Calendar meeting
  after a book intent or calendar sentiment, and only when the attendee
  email and an ISO start time are already in the thread. Observe holds so
  the auto-reply does not also fire; act with enable_hot_lead_closer=on
  (#268)

## v0.10.326 (2026-09-09)
- New: a signal-interrupt strategist replan can rewrite leftover daily-plan
  actions after a buying signal, invite acceptance, or first reply. Morning
  actions that already ran stay on the ledger. Observe is the default; act
  with enable_strategist_replan_agent=on. One replan per outreach per day
  (#267)

## v0.10.325 (2026-09-09)
- New: a short-loop reply exception agent can hold an ambiguous campaign
  reply for a human instead of auto-sending. Observe is the default; act
  with enable_reply_agent=on. Operator replies still skip the agent (#265)
- New: after generate_icp saves, a research loop previews who persona 1
  matches and can keep, recommend, or apply a titles/locations/industries
  patch. Observe is the default; act with enable_icp_research_agent=on.
  Failure keeps the saved ICP. No campaign or outreach rows (#266)

## v0.10.324 (2026-09-09)
- Fix: a message deleted locally now leaves the hosted store too. The
  backend has accepted deleted_message_ids on the sync push since the
  phantom-reply repair, but the client never sent the field, so a row marked
  deleted here kept riding up as a live message and stayed on every hosted
  timeline and counter. Deleted rows now travel as deleted_message_ids in
  their own trailing chunk (#259)

## v0.10.323 (2026-09-08)
- Fix: the cloud pull mints a local row for a dashboard-born campaign with a
  blank ICP (the campaign list carries no ICP body), and the next push sent
  that blank to the backend, which wrote it over the campaign's real ICP,
  settings and context. Every hosted discover/refill for the campaign then
  failed with "Campaign has no valid ICP JSON" and the invite queue starved.
  Blank icp_json/config_json/context_json are no longer sent; twin of
  heylead-api #212 (#258)

## v0.10.322 (2026-09-08)
- Fix: after an invitation sent WITH a note, the local scheduler's first
  message is now follow-up #1 over the thread the note opened, not a second
  cold opener. LinkedIn delivers the note as the thread's first message on
  acceptance, so the opener prompt re-introduced the sender and re-pitched
  the same hook. The follow-up tool's one-day gap no longer counts the note
  as a message, so the first follow-up goes out on acceptance. Without a
  note nothing changes; twin of heylead-api #209 (#257)

## v0.10.321 (2026-09-08)
- Fix: the cloud pull saved every hosted message under a fresh local id, and
  the next push handed that copy back to the backend as a new message, which
  stored it as a second "dm" row with no chat. One invite note and one real
  DM read as four "dm sent" on the dashboard, and the DMs counter read 128
  against 5 real sends (264 echo rows in one workspace, 183 of them invite
  notes re-typed as DMs). Pulled messages now keep the hosted id, the hosted
  send time, and the invite-note format; the push carries the format the
  backend has always looked for (#256)
- Fix: ICP scoring matched industry, location, keyword and pattern terms as
  substrings — "it" inside "hospitality", "uk" inside Ukraine, "us" inside
  Russia — so the include and exclude lists both fired on the wrong people.
  All four matchers now use whole-word matching, as titles and seniority
  already did (#255)

## v0.10.320 (2026-09-08)
- Fix: campaigns created in the cloud now appear on this machine. The pull
  carries no campaigns and refused every row whose campaign was not already
  local, so a cloud-born campaign could never materialise and its rows were
  re-fetched and re-discarded on every pull — 82% of the payload on the live
  install, including an actively sending campaign. Active and paused
  campaigns are now adopted; archived ones are still refused, so a deleted
  campaign is never resurrected
- Fix: a pull that drops rows says so, next to the sync freshness lines,
  instead of counting them into a debug log nobody reads
- Fix: "Pull failed: " with no message. httpx raises timeouts with no args,
  so the one fact about the failure was missing; the exception class is now
  named

## v0.10.319 (2026-09-07)
- Fix: resume tool text now quotes the cloud's 409 reason. The daemon already
  logged why an archived campaign could not resume; the model still saw
  "Could not sync". A refused resume now prints "The cloud refused: {detail}"
  so the next action is unarchive, not another resume

## v0.10.318 (2026-09-07)
- Fix: the website tools act on the workspace you have selected, not your
  default one. `signals(action='website_setup')` and
  `signals(action='website_stats')` built their own request without the
  workspace header, so with a second workspace selected they read and wrote
  the default workspace's tracking snippet and stats. Setup now also prints
  the excluded paths the service reports, and a viewer who may not run setup
  is shown the existing embed code instead of an error
- Fix: when the cloud refuses a campaign action, the daemon log now says why.
  The service now declines to resume an archived campaign ("unarchive it
  first"); before, that arrived as a bare "failed: 409" indistinguishable
  from any other failure, and it took the service's own request log to tell
  a refused resume from an accepted one

## v0.10.317 (2026-09-07)
- Fix: a daily invitation limit of zero can no longer spread from one day to
  the next. v0.10.316 stopped it being written, but each new day copies the
  previous day's limit, so a zero already stored seeded today, and today would
  have seeded tomorrow — it outlived the fix that stopped producing it. A
  stored zero is now ignored when the day rolls over; a real limit still
  carries forward untouched

## v0.10.316 (2026-09-07)
- Fix: the invitation limit on the dashboard is the one the scheduler keeps.
  It showed a fixed 30 that belonged to no account — a Premium seat is allowed
  50 — so the health score treated 28 invitations as nearly a full day, marked
  the account orange and advised slowing down when it was a little over half
  way. The same number is now read from the account's own allowance
- Fix: "Invitations sent today: 3/0". When the service does not state a daily
  limit it sends nothing at all, and that emptiness was stored as a limit of
  zero and then carried forward each day, so the figure never recovered on its
  own. A missing limit now leaves the last real one in place
- Fix: "what's next?" and the dashboard no longer disagree about the
  acceptance rate. One divided today's acceptances by today's invitations,
  which compares two different groups of people — an invitation accepted today
  was sent days ago — so it read 0% beside a real 24%
- Fix: the permanent "Stats drift — numbers may be inaccurate" warning is
  gone. It compared every invitation ever sent against counters that reset
  each month, so the two could only agree in an account's first month. A
  permanent warning is worse than none, because it teaches you to skip
  warnings. It now compares figures describing the same window, and stays
  quiet rather than guessing when the service cannot supply them

## v0.10.315 (2026-09-07)
- Fix: a brand post that published successfully is no longer reported as
  failed. The tool posted to LinkedIn, logged it, marked the action complete,
  and then crashed on the next line — and because the crash was caught by the
  handler below, it returned "Post generated but publish failed" with the
  draft attached. Anyone who followed that advice and pasted the draft in
  posted the same thing twice, and the "N of M actions completed" progress
  read as stalled while the posts were going out fine
- Fix: a prospect who asks to stop is marked opted-out again. The reply
  handler crashed before it could record the status, so the outreach stayed
  open with nothing showing they had asked. The same crash also skipped the
  status update after an ordinary reply, so recent conversations could sit at
  a stale stage. It only struck from the second reply onward in a thread,
  which is why it looked intermittent

## v0.10.314 (2026-09-06)
- Fix: the dashboard stopped answering from a stale local copy while the
  service was healthy. show_status read the daily invitation limit with a
  default that only applies when the field is missing, but the service sends
  it as an explicit null, so the null reached the health score and crashed it.
  The crash was caught by a broad handler that treats any failure as "backend
  unreachable", so every run silently fell back to the local mirror and
  labelled it "(cached)" — hiding the Needs attention list and Hot Leads
  entirely, and reporting campaign counts from the local copy instead of the
  service
- Fix: the acceptance rate is cumulative again rather than today-over-today.
  It divided invitations accepted today by invitations sent today, which
  compares two unrelated groups — an invitation accepted today was sent days
  ago, and one sent today cannot have been accepted yet. A workspace with 23
  of 95 invitations accepted read 0%, scored 2/25 on health, and was told to
  improve its targeting underneath a real 24%. It now pools the lifetime
  figures the campaign rows already carry, and a quiet day no longer erases
  the rate

## v0.10.313 (2026-09-06)
- Merge pull request #235 from D4umak/claude/campaign-prospect-auto-population-51b834
- Merge pull request #234 from D4umak/claude/hot-leads-format-3626h-95ea96
- Fix: a campaign is topped up whenever its queue runs below target, instead of
  only when it is completely empty. The old rule waited a full day for a single
  search page while the sender emptied the queue in an afternoon, so the deepest
  stock it could ever hold was zero. Set `target_queue_size` on a campaign to
  ask for a deeper bench than the default 50
- Fix: a job title now has to match whole words. "cto" is a substring of
  "successfa(cto)rs" and of "dire(cto)r", so every Director on LinkedIn scored a
  perfect CTO title match and cleared the fit gate — one campaign enrolled 29 SAP
  consultants and a film producer that way. An unreadable title also no longer
  counts as a perfect seniority match
- Fix: a search that has run out of results is retried after a week rather than
  switched off for the life of the campaign — nobody who joined or changed job
  afterwards could previously be found, however empty the queue got
- New: you can now clear a lead off the Needs attention list.
  `prospect(action="dismiss", outreach_id="…")` records it as lost, which also
  takes it out of the Hot Leads count. It asks once before doing anything and
  names who it is about, since a lost outcome counts against the campaign's
  conversion rate. There was previously no way to do this at all: answering the
  person or closing the outreach by hand were the only exits, and on a hosted
  account neither worked — the lead is not in the local database, so every
  attempt came back "Outreach not found". The list now also prints each lead's
  id, which is what you need in order to act on any of them
- Fix: a long wait now reads "unanswered 151d" rather than "unanswered 3626h".
  The Overview strip and the alert email both counted in bare hours with no
  rollover, so anyone left unanswered for months showed a four-digit number that
  took arithmetic to read
- Fix: the suggestions no longer tell you to run commands that do not exist.
  Eleven of them named close_outreach, skip_prospect, reply_to_prospect and
  send_followup — none of which are callable — so following HeyLead's own
  advice returned "unknown tool"

## v0.10.312 (2026-09-06)
- Merge pull request #233 from D4umak/claude/activate-signals-service-f2e68e
- Fix: an anonymous profile viewer's label is no longer treated as an identity.
  LinkedIn labels rather than names them ("Someone at ExampleBank"), and
  normalize_public_slug lower-cased that into a slug, so it reached
  signals.linkedin_id and the hosted backend built `/in/Someone at ExampleBank`
  from it — a URL with spaces — then truncated it to a contact id that merged
  two different people into one row. Such views are no longer recorded as
  signals, which is what the no-sendable-id rule always intended

## v0.10.311 (2026-09-06)
- Merge pull request #232 from D4umak/claude/prospect-outreach-stop-flow-27b1f5
- Fix: a stop made in the admin panel now reaches this client — a dashboard
  Skip or opt-out was previously ignored on pull, so the planner kept the
  prospect queued and the next push argued it back at the cloud

## v0.10.310 (2026-09-06)
- Merge pull request #231 from D4umak/feat/prospect-timezone-and-attribution-window
- New: per-prospect timezone windows and a dedicated reply-attribution window

## v0.10.309 (2026-09-06)
- Merge pull request #230 from D4umak/fix/daemon-help-and-tz-default
- Fix: help flags never start a daemon; timezone falls back to the machine's zone, not UTC

## v0.10.308 (2026-09-06)
- Merge pull request #229 from D4umak/fix/strategist-planner-audit
- Fix: strategist planner audit — attribution, voice memos, local-time windows, per-campaign feedback

## v0.10.307 (2026-09-05)
- New: Versioned Outreach Sync, Phase 2 — the pull applies the cloud's versioned funnel tuple whole (rewinds land with no marker), local facts defer overwrites until pushed, pushes carry base_version (#228)

## v0.10.306 (2026-09-05)
- Fix: an invite note no longer holds the 24h message gap — a fresh acceptance is immediately DM-eligible (#226)
- New: silent-accept sync matches on a provider_id/public_id/name multi-index, backdates accepted_at from the relation, backfills missing contact ids, and alerts a webhook on hot-lead replies (#226)
- Fix: outreaches stranded in 'sending' >30 min are recovered; email overflow only planned when a mailbox is connected; log_action survives FK races (#226)
- Fix: the silent-accept name tier only matches a name unique among relations AND invited prospects; every match records matched_by for audit (#227)

## v0.10.305 (2026-09-05)
- Fix: pull applies a cloud demotion of a voided connection — local invited/connected/messaged mirrors converge, replied+ never rewound (#224)
- New: a dead LinkedIn session reaches the user's screen — 30-min daemon health probe, show_status banner, deduped macOS notification (#224)
- Fix: repair the pre-existing CI baseline — doc tool counts, stale patch targets (#225)

## v0.10.304 (2026-09-05)
- Fix: apply a cloud phantom-DM reset instead of pushing it back (#222)

## v0.10.303 (2026-08-31)
- Fix: apply a cloud phantom-invite reset instead of pushing it back (#220)

## v0.10.302 (2026-08-29)
- New: adopt cloud-created outreaches from the changes pull (#218)

## v0.10.301 (2026-08-29)
- Fix: campaign delete/pause/archive follow the campaign across workspaces (#216)
- Fix: survive cloud messages for outreaches this DB has never seen (#217)

## v0.10.300 (2026-08-28)
- Fix: mirror identity lists on every sibling role_geo pass
- Fix: dismiss off-ICP inbound pitches instead of counter-pitching
- Fix: mirror blank-card geo deferral
- Fix: mirror role keep, location geo, and campaign identity
- Fix: mirror multi-list identity then geo search plans
- Fix: keep identity tokens out of ICP search AND

## v0.10.299 (2026-08-27)
- New: compile profile-signal targeting into ICP and a debug tool
- Fix: show backend job metrics and planner skips in activity

## v0.10.298 (2026-08-27)
- Fix: cancel leftover jobs on sync-only and treat hosted daemon as healthy
- Fix: stop unstructured ICP search dumping places into keywords
- New: stand down the local engine when cloud owns hosted jobs
- New: default hosted campaign sending to the cloud
- Fix: treat accepted_at as DM-eligible and stop logging expected gates as outages

## v0.10.297 (2026-08-27)
- Change: hosted sending defaults to the cloud for every existing and new campaign. This machine no longer takes over when the host is quiet, and no longer covers first-touch InMail or opening DMs. Move the whole account here with `scheduler(action='send_from', host='local')`. Launch no longer enables the local sender.

## v0.10.296 (2026-08-26)
- Fix: reschedule deferred jobs and use IANA business hours

## v0.10.295 (2026-08-26)
- Feat: hosted search uses the Premium/SN pool balancer instead of a pinned account

## v0.10.294 (2026-08-26)
- Fix: stop owner targeting, noisy parks, and comment 422 residue

## v0.10.293 (2026-08-26)
- Feat: organization tool — list, switch, invite, and create hosted workspaces

## v0.10.292 (2026-08-25)
- Feat: monitor action health across every campaign and email a skip firehose

## v0.10.291 (2026-08-25)
- Fix: walk past skip_today so warm-up can still enqueue

## v0.10.290 (2026-08-25)
- Fix: stop leftover local free-tier from blocking hosted comments

## v0.10.289 (2026-08-25)
- Fix: do not plan invite and DM jobs that cannot send

## v0.10.288 (2026-08-25)
- Fix: stop teaching hands-on in invite prompts

## v0.10.287 (2026-08-25)
- Fix: InMail first-touch and the 14-day fallback are on by default again (#213)

## v0.10.286 (2026-08-25)
- Fix: spend leftover daily invite slots immediately after a cap increase (#212)

## v0.10.285 (2026-08-25)
- Fix: hosted profiles are normalized so invite notes see title, company, About, and experience (#211)
- Fix: new campaigns default InMail off; paid seats already send 50 invites a day

## v0.10.284 (2026-08-25)
- Fix: let paid LinkedIn seats send 50 invites a day (#210)

## v0.10.283 (2026-08-25)
- Fix: overnight skip jobs and a below-fit accept no longer hide the opening DM after the 24h note gap

## v0.10.282 (2026-08-25)
- Fix: stop campaign refill dying inside the 44s tick before anyone is enrolled (#208)

## v0.10.281 (2026-08-25)
- Fix: existing DBs stamped at schema v6 now get outreaches.chat_id so planning no longer dies every tick

## v0.10.280 (2026-08-25)
- Fix: invite notes no longer count as already-messaged, so acceptances get a real opening DM
- Fix: refill dedups against stored connections instead of walking relations inside the tick
- Fix: send outcomes, leftover FK deletes, and author-less person signals stay honest and classifiable

## v0.10.279 (2026-08-25)
- Fix: "is this message for me?" replies recheck campaign fit — apologize and stop if it's the wrong person, confirm if it isn't
- Fix: first-touch skips a clear targeting mismatch so we do not send the opening note to the wrong contact

## v0.10.278 (2026-08-25)
- Fix: too-soon DMs, messaged-orphan rescues, and InMail skips no longer look like successful sends
- Fix: sendable-queue deletes and inbound enroll no longer leave FK failures or consumed signals
- Fix: cloud-off-by-choice no longer emails a both-down alert; tick timeouts defer instead of retrying as network errors
- Fix: one Relations pre-check per tick, cached chat lookups, and daily caps checked before the planner enqueues
- Fix: heylead-logs sorts by time, job_executed carries duration, and daemon.err.log no longer duplicates every INFO line

## v0.10.277 (2026-08-25)
- Fix: if the hosted scheduler is on but stops following up, this machine starts sending again — including the opening DM after someone accepts, not only new invites
- Fix: a send from this machine no longer looks like the host coming back, which used to silence outreach for another three hours
- Fix: observe no longer warns that the cloud is still sending when the host has already stood down

## v0.10.276 (2026-08-25)
- Fix: hosted headline A/B attributes each invite and applies the winner (or restores the original) only after LinkedIn accepts the change
- Fix: brand posts are capped by publishes, not completed jobs, on both laptop and cloud

## v0.10.275 (2026-08-25)
- Fix: first-touch email uses the LinkedIn-written draft instead of a separate cold-pitch rewrite

## v0.10.274 (2026-08-25)
- New: collect Reddit, HN, G2, and ATS hiring signals from watchlists
- Fix: referral first-touch email greets the contact and uses the intro, not "there"
- Fix: outbound email binds one healthy mailbox and honors campaign from_email

## v0.10.273 (2026-08-24)
- Fix: answers to our discovery DMs are not inbound buying signals
- Fix: discovery DMs, counter-pitches, emails, and posts now fail closed on the same consulting-speak guard as invites
- Merge pull request #197 from D4umak/release/v0.10.272
- Fix: first-touch notes that cite a 21x stat and sales jargon no longer ship
- Fix: skipped sends stay greppable, and the daemon writes its own JSON log
- Merge pull request #196 from D4umak/release/v0.10.271
- Fix: classified hooks can fire, and network lifestyle posts skip the LLM
- Merge pull request #195 from D4umak/release/v0.10.270
- Fix: the invite uses the best post, and parks no longer look successful

## v0.10.272 (2026-08-24)
- Fix: skipped sends, silent enriches, and forced channel picks now survive into JSON and daemon text logs
- Fix: a tool or job that returns a skip or error string is no longer logged as success
- Fix: heylead-logs can list events, treat outcome=error as an error, and correlate by request id
- Fix: first-touch notes that cite a prospect stat (that 21x) or sales-methodology jargon no longer ship

## v0.10.271 (2026-08-24)
- Fix: a classified pain, tech, or budget post can start outreach even when the model called it thought leadership, and one fresh hook now scores high enough to send
- Fix: profile viewers are matched against campaign segment titles (Head of Product vs Heads of Product), and a viewer is no longer dismissed just because intent came back not relevant
- Fix: network lifestyle posts are no longer sent to the classifier; keyword intent on those posts is recorded so it cannot be overwritten; personal followers are no longer saved as company followers

## v0.10.270 (2026-08-24)
- Fix: a buying signal that cannot act is marked skipped, not successful. The invite uses the best classified post for that person, and a classified hook can send once even when fit stays below 0.3
- Fix: company news attaches to people already in the campaign at that company; a named classified stranger can enroll when discovery is on
- Fix: invite notes no longer invent figures that were not in the post, in-campaign profile viewers are no longer ignored, and lifestyle posts are dismissed instead of actioned

## v0.10.269 (2026-08-24)
- Fix: "drop my colleague a note at …" enrolls that person even when the sender is already marked a vendor pitch
- Fix: a Premium seat may send 22 connection requests a day (15 if the account is confirmed free). LinkedIn's real ceiling is about 100 a week for every plan — the old 12/day cap and the 100-action total were ours, and warm-up used the total before invites could run

## v0.10.268 (2026-08-24)
- Fix: launching a campaign queues the first connection request now, instead of waiting for a later cycle and a 22-minute gap
- Fix: a prospect whose Open Profile flag was never stored can get a connection request again. The previous check treated "unknown" as "not allowed to invite", so the queue looked idle

## v0.10.267 (2026-08-24)
- Fix: a later "have you reached out to Jordan?" nudge enrolls that person even when the email was in an earlier message, the referrer's campaign is gone, or the host owns sending

## v0.10.266 (2026-08-24)
- Fix: a campaign that names a product (open banking, IDSP, Right to Work) no longer fills from a directory of generic CEOs, and a name-only profile no longer clears the send line
- Fix: after an InMail is refused, that person can get a connection request the same day instead of sitting unused for 24 hours
- Fix: starting a campaign no longer waits on uploading every other campaign to the host
- Fix: a hosted account is not limited to one campaign / 30 people by a leftover local free-tier flag; a draft does not use the free campaign slot
- Fix: this machine no longer stands down for a host that never actually sent — a routine upload no longer resets the wait, and if nobody has been contacted after 15 minutes the connection requests start here
- Fix: a Premium seat no longer holds every prospect for InMail first. Only Open Profile members go that path; everyone else gets a connection request. A refused InMail now records which campaign it belonged to, and the host's error text is kept so the next refusal is readable

## v0.10.265 (2026-08-24)
- Fix: a LinkedIn chat with someone who fits an active campaign is added to that campaign as replied or messaged, instead of staying only in your inbox
- Fix: a fit-skip parked with a refill sentence is restored when the score later clears the line

## v0.10.264 (2026-08-24)
- Fix: HeyLead no longer opens a cold conversation with someone you last spoke to months ago. The check for an existing thread only looked through your 150 most recent chats, so anything older read as "never spoken" — and a thread old enough to fall out of that list is exactly the one you are most likely to have forgotten when queueing a new campaign. It now asks about the person rather than about recent activity, so the age of the conversation no longer matters. Expect a campaign to occasionally pass over someone it would previously have written to: that is the check working, and the message it would have sent was one that opened as if you had never spoken

## v0.10.263 (2026-08-24)
- Fix: first-touch InMail stays on this machine so Premium and Open Profile people are not left pending while the host owns invites
- Fix: if the hosted scheduler is on but does not actually contact anyone for a few hours, this machine starts sending again
- Fix: the fit gate no longer parks someone already invited or messaged; a later higher score puts a never-contacted skip back in the queue
- Fix: company pages, numeric LinkedIn ids, and "Someone at …" names are refused at enroll so they never join the campaign
- Fix: the same person arriving with a slug and later a member id shares one outreach row
- Fix: skipping a live thread needs an operator reason (or do-not-contact); empty skips no longer park contacted people

## v0.10.262 (2026-08-24)
- Fix: invitations, follow-ups and InMails now reference what you already sent this person. The prior-touches block was read from the database on a thread that is not allowed to touch it, and the refusal was swallowed — so an always-thrown error was indistinguishable from "never contacted", and every message since the feature shipped was written as first contact
- Fix: the same LinkedIn member no longer lands in your contact base twice. v0.10.260 keyed people by the provider id, which is stable across vanity-URL changes and stays — but it discarded the public slug that arrived with them, so the next connection sync, which knows only the slug, created a second record. A contact base still holding an older pair also stopped updating that person at all, silently
- Fix: HeyLead no longer opens a cold conversation with someone it is already mid-thread with when LinkedIn is unreachable. A timeout, a rate limit and an expired session all answered "is there a conversation with this person?" with the same "no" as a genuine first contact; the send is now deferred and retried instead
- Fix: a campaign delete that fails partway no longer finishes itself minutes later. Nothing was rolled back, so the half-finished delete waited for the next unrelated write to commit it — messages included, which are the one thing here that cannot be recovered from LinkedIn
- Fix: deleting a campaign no longer keeps its prospects out of every campaign you create afterwards. The person's record went on naming the deleted campaign, which reads as "already in a campaign", and nothing in the tool surface could clear it (one migration backup expected on first start)
- Fix: a referral whose referrer has no name on file no longer opens with "A asked me to reach you"

## v0.10.261 (2026-08-24)
- New: outreach debug trail — MCP tool calls get a request id, and skips/sends now record channel pick, exclusion, enrichment source, reply classification, InMail/email no-ops, and cloud vs local ownership
- New: send logs hash the copy instead of the body, record attempt/result, inbox replies, strategist-owned skips, scheduler job outcomes, and prospects dropped below the fit threshold
- New: buying-signal skips record why outreach never started, kept fit scores are sampled, and inbound debug logs a hash instead of the message body

## v0.10.260 (2026-08-23)
- New: Premium seats can InMail strangers as first touch. Email is a later chapter except a named referral, which enrolls the same day
- New: a LinkedIn vanity-URL change or a later email enrich updates the existing person instead of creating a twin
- Fix: answer booking-link hot leads and shout when they stay silent
- Fix: keep score-sort enrichment fixtures below the send gate

## v0.10.259 (2026-08-23)
- Fix: turning a new buying signal into a contact no longer crashes. The first identity check already used the resolved person; the three re-checks just before insert still used the old LinkedIn-id lookup, which was no longer imported and would also miss a slug-keyed row when the signal carried a provider id

## v0.10.258 (2026-08-23)
- Fix: when the email mailbox is disconnected, show_status and suggest_next_action say so and point at reconnecting it, instead of failing only on the next send
- Fix: once today's LinkedIn invite budget is spent, remaining prospects with an email address are scheduled as email overflow instead of sitting idle until tomorrow
- Fix: a mid-send opt-out is no longer overwritten as sent, and a meeting time the prospect just named is no longer replaced by the previous slot we offered
- Fix: the first connection sync no longer stops halfway, a short handover no longer leaves the scheduler silent, and a large directory seed is not killed by one slow chunk

## v0.10.257 (2026-08-23)
- Fix: after someone replies, the daily planner no longer queues another LinkedIn follow-up. Those threads stay with you instead of getting a second autonomous DM
- Fix: an email send that failed because the mailbox was rate-limited or disconnected is retried later, instead of being marked permanently broken. A timeout or 502 is checked against Sent mail before it is treated as a failure

## v0.10.256 (2026-08-23)
- Fix: when the model decides someone is not a fit, that skip note is no longer sent as a LinkedIn message. The person is skipped instead of the refusal being polished and delivered
- Fix: a CEO title is no longer enough to pass as an AIS/PIS or open-banking match. Without those product terms on the profile, the fit score stays below the send gate. Ordinary search keywords such as fintech still work as before

## v0.10.255 (2026-08-23)
- Fix: after someone accepts an invitation, the opening message no longer waits forever when cloud sending is on. This machine used to assume the cloud would send that first DM; the cloud only sends DMs on campaigns that skip invitations. The opening message now goes out from here
- Fix: prospects already identified as unsendable (anonymized Sales Navigator results) are parked again if they slipped back to error, and first-degree connections who hit a recoverable send error are put back in line. Neither step sends a message on its own

## v0.10.254 (2026-08-22)
- Fix: withdrawing an old invitation no longer gets undone the next time HeyLead talks to the cloud. The invitation was gone on LinkedIn and the prospect was marked withdrawn, but the cloud still thought they were invited and wrote that back over the local record — seven people who had been waiting 151 days were put back on the outstanding list nine hours after they were cleared

## v0.10.253 (2026-08-22)
- New: hosted users share a professional directory; campaigns and inboxes stay private
- Fix: when prospect top-ups do nothing, HeyLead now says which campaigns were passed over and why. The hourly refill reported "4 skipped (cooldown/no ICP)" whatever the reason — so three campaigns sat four days without a single new prospect while the real cause, discovery being switched off on them, appeared only in a log file. Each situation is now counted separately: enriched, searched and found nobody, waiting on a cooldown, no usable ICP, discovery switched off, no LinkedIn account connected
- Fix: a top-up that never went looking no longer starts the 24-hour cooldown. It was stamped whatever happened, so a LinkedIn session that happened to be disconnected at the wrong moment cost a campaign a further day of discovery — and switching discovery back on took up to a day to have any effect. Now only a search that actually ran holds the cooldown

## v0.10.252 (2026-08-22)
- Fix: a cloud sync that wrote nothing no longer looks like a bulk edit. After HeyLead stopped re-saving prospects it had not changed, the log still said it had pulled 1,705 updates every 15 minutes — that was how many the cloud sent, not how many were written. It now prints both, so a no-op reads as 0/1705
- Fix: the leftover-profile job no longer reports one unlabeled pile. It had been adding people with no open-profile flag to the "still to fetch" count, so the figure disagreed with the list it actually walks. Those are now two named leftovers

## v0.10.251 (2026-08-22)
- Fix: invitations and messages the hosted scheduler sent while your laptop was closed now count against the daily limit the same way a send from this machine does. v0.10.250 made the limit glance at a second counter the cloud writes; that still missed a morning send after midnight, and it never saw cloud DMs at all. Both now write the same record the limit already reads, dated when they were sent, not when the laptop next opened

## v0.10.250 (2026-08-22)
- Fix: pausing a campaign now stops follow-ups that were already queued. Pause only affected prospects not yet contacted, so anyone already connected kept their scheduled follow-up and it went out overnight anyway
- Fix: your daily invitation limit can no longer be spent twice. Invitations the cloud scheduler sent while your laptop was closed were invisible to the limit when it reopened, so it approved a full day's worth on top of them
- Fix: if you run HeyLead with your own Unipile account, replies in some conversations were never seen. LinkedIn sometimes returns a conversation without its latest message, and those were skipped entirely — including someone replying "stop messaging me", who then received the next follow-up
- Fix: two different people who share a name are no longer offered as a duplicate to merge. Merging is irreversible, and a name is not proof of identity — a second detail has to match now
- Fix: contacts whose LinkedIn profile can't be read no longer crowd out everyone else. They were never marked as checked, so they returned to the front of every scan and consumed the batch indefinitely

## v0.10.249 (2026-08-22)
- Fix: HeyLead stopped re-saving prospects it had not changed. Every 15 minutes it sends the hosted scheduler the full state of every prospect in a running campaign — 1,705 of them here — and every 15 minutes it read them all back and wrote all of them to disk again, whether anything had moved or not, because it only checked for a real difference on two of the six fields it copies. That is roughly 153,000 pointless writes a day, and the cost is not the writing: each one reset the prospect's "last changed" time, so that time stopped meaning anything across the whole list. It is what three separate parts of HeyLead consult to decide whether a follow-up is due, and it is why a bulk edit that never happened appeared to have happened on 7 August — the day after cloud sync was first switched on. HeyLead now compares before it writes
- Fix: "DMs sent today" and "Follow-ups today" appear in your status for the first time. Both were counted in one step, that step asked the messages table for a column it does not have, and the error was caught and turned into zero for both — and a zero is not printed, so neither line has ever been shown to anyone
- Fix: and the DM figure, once it could be shown, would have been wrong. It counted prospects whose record had been touched today rather than prospects who were actually written to — so it would have reported 24 messages sent on a day when the true number was nought. Repairing only the missing column would have put that fabricated 24 in front of you; the two are fixed together, and both figures are now counted from the messages themselves. The first message to someone is a DM, any later one is a follow-up, so the two numbers no longer double-count the same message

## v0.10.248 (2026-08-22)
- Fix: a status the hosted scheduler reports that HeyLead has no name for no longer overwrites what it knows locally. "expired" is one of those — HeyLead can display it but never sets it — and because it was unrecognised it was scored as *forward progress*, so it beat any prospect recorded as having failed. 310 prospects already carry the label — 309 of them applied a few at a time over three weeks in March and April, the last one in August; 265 of those overwrote a live invitation that had been sent and was waiting on a reply. v0.10.246 had already shielded the 3,225 deliberately skipped prospects as a side effect of a narrower fix — the 991 recorded as failed were still exposed, and it is those this covers, along with any other status the cloud may report in future. Anything HeyLead cannot place in its own sequence is now ignored and noted once in the log, so the gap surfaces instead of being acted on. Dates and counts arriving in the same update still apply
- Fix: a prospect whose invitation has expired no longer keeps queued work waiting behind them. Follow-ups scheduled while the invitation was still outstanding stayed in the queue after it lapsed, because "expired" was missing from the list of endings that clear a prospect's pending jobs. None have fired — only because the scheduler declines to pick up a status it cannot name — so this closes it before something else opens it
- Fix: the record of what changed a prospect's status now names the code that asked for the change. Nearly every status change is made through a shared background database thread, and the record named that thread rather than the caller: 95,859 of 99,553 entries said the same uninformative thing. It is a diagnostic record only, but it is *the* record for working out why a prospect ended up where they did, and tracing the 310 relabelled prospects above cost a day for exactly this reason

## v0.10.247 (2026-08-22)
- Fix: a prospect whose message failed to send now shows up when you ask what to do next. Those are the only failures HeyLead is meant to retry, nothing retries them on its own, and they had no place in the suggestions list at all — so the tool would report "all caught up" over sends that had failed days earlier. Each one now names the prospect and quotes exactly why it failed, so you can tell someone who simply has not accepted yet from someone who can never be messaged, and retrying stays your decision
- Fix: the daily action limits no longer briefly forget themselves at midnight. Slots taken in the last minutes of a day were handed back the moment the date changed, so for a short window each night HeyLead could spend the same allowance twice — the exact double-spend the limit exists to prevent
- Fix: the job that fills in missing profile details now finishes inside the time it is allowed, instead of being killed partway and retried. Its per-run workload was sized on the assumption that looking someone up costs only the two-and-a-half second pause HeyLead takes between calls, with the call itself counted as free — so a full run needed about 75 seconds against the 44 it actually gets. It survived only by luck of the data: most of the records it picked were company pages, which are skipped and cost nothing. As soon as real people filled those places the job began timing out on nearly every run, and the profiles it had already saved were recorded as a network failure and fetched again from scratch. It now measures itself against the real limit, stops on its own before reaching it, gives up on any single lookup that stalls, and reports both what it finished and how much is still waiting
- Fix: what a scheduled job actually did is now kept where it can be read back. Every job produces a one-line account of its own work — how many profiles it filled in, how many records are left for next time — and the scheduler discarded it, keeping only the job's name and how long it took. That account survived in the text log and nowhere else, so there was no way to ask whether a job had been achieving anything, only whether it had finished. It is now stored alongside the job's record

## v0.10.246 (2026-08-22)
- Fix: "skip this prospect" now skips the prospect you were shown. It picked the lowest-scoring one instead, marked them skipped for good, and then printed the person you meant as "next up" — who stayed in the queue and was still contacted
- Fix: switching LinkedIn accounts is now all-or-nothing. If fetching the new profile or re-analysing your voice failed, HeyLead kept the new account active but the previous person's name, title, company and writing style — and carried on sending that way. The same fault existed in setup_profile, and the error message used to send you there
- Fix: a prospect whose name is blank no longer breaks sending. Fourteen places assumed a name had at least one word
- Fix: when reading your LinkedIn inbox fails, HeyLead no longer skips that period for good. It recorded the failed scan as "no new activity" and moved past it, so invitations and replies that arrived during an outage were never seen
- Fix: re-seeing a post no longer erases the engagement figures already recorded for it, and someone who posted once is no longer counted as an active poster and sourced for outreach

## v0.10.245 (2026-08-22)
- Fix: comments stop failing with "Post cannot be found" on posts that plainly exist. LinkedIn files posts under two different internal id types, and HeyLead was asking for every post as though it were the first type — so any post of the second type came back as missing, roughly a third to a half of every comment attempted. The giveaway was that liking the very same post worked every time, because that request identifies the post a different way. HeyLead now names the id type properly and tries the other one if the first is not found

## v0.10.244 (2026-08-22)
- Fix: a single contact with no stored profile could silently stop a campaign from progressing. The record left behind for a prospect nobody had looked up yet was empty rather than absent, and asking an empty record a question is an error rather than an empty answer — so one such prospect anywhere in a campaign made the whole search for who to escalate to fail, and the failure was logged as a warning and otherwise ignored. Everything scheduled after that point in the run — follow-ups, and the safety net that rescues prospects nothing else picked up — was skipped for that campaign, every minute, silently. 1,685 of 5,860 contacts were stored this way; they are now stored as genuinely absent, which asks nothing and breaks nothing
- Fix: HeyLead can see 1,252 more people it could message for free. The same empty records also hid whether someone is an Open Profile member, which is who a free account is allowed to InMail — so the answer came back as an error and the whole list came back empty
- Fix: 1,046 people whose LinkedIn address contains an accent, Cyrillic or an emoji now get their profile fetched. LinkedIn answers those addresses with a forwarding notice rather than the profile, and HeyLead was storing the notice as though it were the person — which then read as "already fetched", so they were never looked up again and nothing about them was ever known. Their records are cleared and requeued, and a reply that is not a profile is no longer stored as one
- Fix: someone who accepts your invitation after the campaign has finished now gets the opening message they were owed. Completed campaigns only ever scheduled follow-ups, and a follow-up is skipped for anyone who has not been written to yet — so a late acceptance was picked up by nothing at all, and five prospects have been sitting connected and unspoken-to since March. Only acceptances from the last 30 days are opened, so nobody hears from you for the first time months late, and anyone already messaged is left to the normal follow-up schedule rather than being greeted twice

## v0.10.243 (2026-08-22)
- Fix: a direct message to someone HeyLead cannot address is now refused before the message is written, not after. LinkedIn needs a member's underlying id to deliver a DM, and when that id was missing the refusal came at the very end — after the prospect had been researched and the message generated, improved and validated. Retrying such a prospect paid the whole bill again, every time. The invitation path learned this in v0.10.225; the DM path had been left out of the fix

## v0.10.242 (2026-08-22)
- New: a campaign you launch keeps running when your laptop is closed. Launching used to switch on the scheduler on your Mac, which stops when the Mac does; 24/7 sending sat behind a second setting most people never found. Launching or resuming now starts both, and says which machines are working. Nothing changes for observe mode, which still sends from nowhere, or for self-hosted installs, which have no cloud half
- Fix: with both schedulers running, nobody is messaged twice. They planned from separate copies of the same prospects that only met every few minutes, so the same follow-up could go out from both. A campaign the cloud has taken over is now the cloud's to send — checked when work is queued and again when it runs — while your Mac keeps signals, replies and strategy. If the cloud stops answering, your Mac takes sending back rather than going quiet
- Fix: HeyLead now tells you when results stop coming back from the cloud. A failed sync in that direction — an expired session, most often — was logged where nobody would see it, so the cloud kept sending while your own dashboard quietly stopped hearing about it. show_status names it now, with the link to sign in again when that is the cause
- Fix: HeyLead can see who wrote a comment again. LinkedIn reports a commenter's name and their profile id in two different places, and HeyLead only ever read the one without the id — so every commenter came back anonymous. Two things quietly did nothing as a result: mining competitors' posts for people worth reaching out to, which has produced no leads at all, and confirming that your own comment actually appeared, which could never match you and never recorded which comment it was

## v0.10.241 (2026-08-22)
- Fix: when LinkedIn says you have too many invitations outstanding, HeyLead withdraws one to make room — and it was withdrawing the newest instead of the oldest. An invitation more than a few weeks old carries only a vague "sent 5 months ago", which collapses a whole batch onto one moment in time, so the sort that was meant to find the oldest could not tell them apart and simply took the first in the list, which is the freshest. It now orders by the invitation's own identifier, which is the only reliable record of age
- Fix: a withdrawal LinkedIn refused was recorded as a withdrawal that happened. The hosted service reports LinkedIn's answer inside a successful reply of its own, and only the outer one was being read, so a refusal counted against your daily allowance, closed the prospect's record, and logged a success while the invitation was still sitting there. Withdrawals are landing correctly today; this is what would have hidden it if they stopped

## v0.10.240 (2026-08-22)
- Fix: the guard that stops a second campaign messaging someone you are already talking to now works. It has never once stopped a send — not because the situation never came up, but because it could not fire at all: it compared message ids that the LinkedIn history it reads does not include, and its other half asked the database for a column that does not exist, failing silently every single time. It no longer relies on ids, which are missing from most sent messages anyway, and asks the question it actually means — has this outreach sent anything into this conversation yet

## v0.10.239 (2026-08-22)
- Fix: a connected mailbox is no longer disconnected by mistake. HeyLead checked whether your mailbox still worked by looking for it in the account list — but a mailbox connected through the hosted service does not appear in that list, so the first check would have unbound a perfectly good one and asked you to connect another

## v0.10.238 (2026-08-22)
- New: HeyLead can send email. Connect Gmail or Outlook with account(action='connect_email'), then send_email(to=..., subject=..., body=...) — or let campaigns reach prospects by email when LinkedIn is capped. Mail.app is never used
- New: email obeys the same rules as every other channel — a daily ceiling of its own, your do-not-contact list, and a record of every send. None of that existed when the channel was first written
- Fix: HeyLead will no longer guess which mailbox to send from. If your workspace has more than one, it asks instead of picking the first — a colleague's inbox could otherwise have become your from-address. Choose with account(action='set_email_account'), and a mailbox whose password expired can now be replaced instead of silently blocking every send
- Fix: an email whose outcome is unknown — the connection dropped after it may already have gone out — is never automatically sent again. Both the first email and follow-ups now stop and flag it rather than writing a fresh one to the same person

## v0.10.237 (2026-08-22)
- Fix: comments that LinkedIn accepted are recorded again. A comment was posted, and then the bookkeeping for it crashed on a detail of how the AI reported its reasoning — after the post was already live. So the engagement was never logged, never counted, never verified, and the post was left marked as still being worked on, which meant it could never be engaged again. The scheduler filed the job as failed and retried it, paying for a fresh comment that was then refused. Every comment that worked between 18 and 21 August was lost this way

## v0.10.236 (2026-08-22)
- Fix: when LinkedIn declines a comment and HeyLead likes the post instead, that counts as the engagement it is. The wording said "Comment failed ... but liked their post instead", and the scheduler reads those words to decide the outcome — so it benched the prospect for 24 hours right after reaching them. 202 times

## v0.10.235 (2026-08-22)
- Fix: signal classification no longer reports a failure every time it runs. The job was being cut off partway through its batch by the scheduler's 44-second per-job limit and filed as a network timeout — even though every signal it had classified was already saved — so a genuine error was impossible to pick out from the noise. It now classifies a set number of signals per run and finishes cleanly, reporting what it left for next time, and runs every 5 minutes instead of every 15 so the backlog still keeps up with what arrives
- Fix: invitations you sent months ago are now withdrawn before newer ones. LinkedIn hands back your sent invitations newest-first, and HeyLead worked through that list from the top, so the oldest sat at the very end and each day's new invitations pushed in ahead of them — seven had been waiting 151 days, holding invitation quota they could never use
- Fix: withdrawing an invitation now closes the prospect's record. It only ever withdrew on LinkedIn's side, so the prospect stayed marked as invited forever — still counted as outstanding, and past the point where HeyLead could follow up another way
- Fix: if you connected LinkedIn through HeyLead's hosted service, a slow or failing gateway could deliver the same thing more than once. When a send timed out, HeyLead retried it — but the message had often already gone out, so the person received it two or three times. This affected DMs, voice notes, comments, InMail (where each duplicate also spent a credit) and email. The direct connection has always guarded against this; the hosted one could not, because the option did not exist there

## v0.10.234 (2026-08-21)
- Fix: a failed comment now records which post it was for. Success and the reaction fallback both logged it; the failure did not, so 203 of them piled up without naming the post — and LinkedIn's "post cannot be found" is misleading, since a reaction on that same post succeeds moments later

## v0.10.233 (2026-08-21)
- Fix: the same LinkedIn member no longer lands in your contact base twice. Unipile names people two ways — a public slug from connection sync, a provider id from profile backfill and inbound DMs — and an inbound DM carrying only the provider id created a second record beside the one that already held it, splitting interaction history and fit score

## v0.10.232 (2026-08-21)
- Fix: inbound invitations and messages were being read by the AI, scored, and then dropped before the verdict was saved — so the same message was re-analysed from scratch on every cycle and never reached your inbox as classified. Spotted in the live daemon log, not in testing

## v0.10.231 (2026-08-21)
- Improvement: if you connected LinkedIn through HeyLead's hosted service, your subscription can now be read directly rather than worked out by trying. Nothing changes until the hosted service ships its half; until then HeyLead keeps treating an unreadable subscription as unknown, never as a no

## v0.10.230 (2026-08-21)
- Fix: v0.10.229 freed Premium InMail credits everywhere except where it mattered. If you connected LinkedIn through HeyLead's hosted service, nothing was ever able to read your subscription — so "we could not check" was recorded as "you have no Premium", and your credits stayed locked by the very release that set out to unlock them. Unknown now buys the one attempt that settles the question instead of a silent no

## v0.10.229 (2026-08-21)
- Fix: your LinkedIn subscription is read from LinkedIn now instead of guessed from whether a search gets refused — that guess could not tell "no licence" from "session not authenticated", and reading it as the first produced a day of wrong conclusions, including advice to reconnect an account that was working fine
- Fix: the InMail credit check had never once worked — it asked for an address that does not exist, so every send went ahead blind
- Fix: Premium InMail credits are usable, not just Sales Navigator ones. Whether Premium InMails actually go through is not documented anywhere we can find, so HeyLead settles it by trying once and remembering the answer rather than assuming either way
- Fix: losing a subscription now takes a live confirmation before HeyLead believes it, so a timeout or a rate-limit can no longer quietly demote a paying account
- Fix: follow-ups reference the post that opened the thread. Invitations and InMails were fixed in v0.10.227; follow-ups were missed, so a thread that opened on someone's stadium project followed up about someone else's funding round
- Fix: a workspace scan that could not reach every account no longer records "there is no premium account here" for a week afterwards
- Fix: when Sales Navigator cannot be reached, HeyLead now says which of the two possible reasons it is

## v0.10.228 (2026-08-21)
- Fix: an interrupted send no longer comes back recorded as a delivered message — the invitation note was being read as a DM, inventing a conversation with someone who never accepted and hiding the invite from follow-up
- Fix: HeyLead no longer rewrites your real LinkedIn headline before every invitation batch; the A/B test that did it never recorded which variant anyone saw, so it is switched off until it works
- Fix: a scheduler that hands leadership to a newer process now waits and takes it back if nobody else does — on installs without the daemon, stepping down used to stop all scheduling until a restart
- Fix: the daemon and a chat session can no longer both publish the same brand post
- Fix: a broken config file left a prospect stuck mid-send for ten minutes instead of releasing them immediately
- Fix: a migration that dies partway no longer buys another 670MB database copy on every restart — one schema version now takes exactly one backup

## v0.10.227 (2026-08-21)
- Fix: signal-triggered messages reference the post that actually triggered the outreach, not whichever of the prospect's posts last scored highest
- Fix: the classifier's post-specific hook is preferred over templates — no more openers built from announcement filler like "Your work around Exciting News caught my attention", and "$1.5B" no longer truncates to "$1" in topic extraction
- Fix: signal activation no longer falls back into campaigns without an ICP; with no eligible campaign the signal is parked as no_matching_campaign instead of messaging strangers from a campaign that sells nothing

## v0.10.226 (2026-08-21)
- New: HeyLead now understands account tiers. A weekly probe keeps the Sales Navigator flag honest against purchases and lapses (a probe that cannot decide writes nothing), and account(action='refresh_tier') re-checks on demand
- New: an invitation that sits quiet for 14 days earns one InMail before the day-21 withdrawal — Sales Navigator credits finally do something; free accounts get the same escalation for Open Profile members, whose InMails cost nothing
- New: Sales Navigator searches now page to 2,500 results (was 1,000), and invitation notes use the 300-char allowance the licence grants
- Fix: an expired LinkedIn session no longer reads as "subscription cancelled" — auth failures were being stored as a confirmed downgrade
- Fix: existing installs no longer drop to classic search on upgrade — a cache key introduced this release was read as False when merely absent
- Fix: the InMail success marker landed after the claim was released, leaving a window where a concurrent job could spend a second metered credit on the same prospect
- Fix: escalation is bounded — no InMails to invites older than 35 days, one attempt per prospect per day, and permanently unsendable rows skip instead of retrying forever

## v0.10.225 (2026-08-21)
- Fix: the provider-id guard sat after message generation — an unsendable id burned a prospect analysis plus the full generate→improve→validate pipeline on every retry before erroring; the guard now runs before any LLM call
- Fix: the provider-id repair only selected rows that had already failed; it now also sweeps pending outreaches whose contact has no sendable identifier at all and parks them as terminal 'skipped' — applied to the live DB 21 Aug: 4 queued masked-name rows (auto_enrichment backlog from April, predating the v0.10.222 ingestion fix) parked before burning anything. A slug-shaped provider_id now counts as a usable identifier in classification, so the sweep can never park a row the send guard would accept

## v0.10.224 (2026-08-21)
- Fix: during a mixed-version rollout, every process start whose SCHEMA_VERSION differed from the database's user_version stamp re-ran the migration pass and took a full ~700MB pre-migration backup, then stamped its own number back — old and new versions ping-ponged the stamp for hours (nine backups on 21 Aug alone). The schema check is now "at or ahead counts as current" and the stamp is never written backward; safe because migrations are strictly additive

## v0.10.223 (2026-08-21)
- Fix: invitations could be attempted against ids Unipile can only 400 on — the send-time guard caught bare numerics but not SN-space ids or the masked Sales-Navigator display names ("Recruiter at JPMorganChase") that anonymized results leave in linkedin_id; all unsendable shapes are now rejected before any API call
- Fix: a repair pass for stored rows (services/provider_id_repair.py, dry-run by default) resolves classic ids where a usable slug exists and parks the rest as terminal 'skipped' — applied to the live DB 21 Aug: the 8 retry-looping invitation_failed rows (all masked-name contacts with empty profile_json) are parked and no longer burn an LLM generation plus a Unipile 400 per retry cycle
- Correction: v0.10.222's changelog attributed those 8 failures to SN-space ACw… ids in stored rows; live-DB evidence shows zero such rows existed — the failures were masked display names used as ids. v0.10.222's ingestion repair stands as the guard for the documented ACw/ACo id-space split

## v0.10.222 (2026-08-21)
- Fix: Sales-Navigator-mode searches fed prospects with SN-space provider ids (ACw…) into campaigns, and classic invitations 400 on those ("User ID does not match provider's expected format" — 8 failures on 21 Aug); ingestion now resolves the classic ACoAA id via a classic-api profile lookup, falls back to the public slug when it can't, and rejects prospects with no usable identifier instead of burning invite attempts

## v0.10.221 (2026-08-21)
- Fix: a cloud pull could overwrite a local opted_out/unsubscribed/bounced with a forward status, putting someone who asked to be left alone back into the planner's queue
- Fix: hosted-backend mode sent messages without the style guardrail direct mode enforces; both clients now share it, and voice memos with an explicit outreach_id no longer skip every send gate
- Fix: the entire email channel (outreach and follow-ups), create_post, reply_comment and the daily strategist were dead — nine call sites imported modules that never existed; a new test now resolves every import target so this class cannot ship again
- Fix: daily caps were enforced per process, so the daemon and each MCP session could each spend the full cap — measured at 48 invite slots against a cap of 12 across four processes; the decision now happens inside one serialized database transaction (schema v3, one migration backup expected on first start)
- Fix: send_inmail could charge two metered credits for one outreach — the dedup check raced the multi-second generation window; it now takes the same atomic claim the DM path uses
- Fix: a switched account never reached the long-running daemon, which kept sending from the old identity until restart; pending-invitation data was also served across account boundaries, so the wrong account's invite could be withdrawn
- Fix: ~80 further verified defects from a full-codebase audit — cohort reports collapsed into one bucket, meetings auto-booked at 3 AM local, refill re-enrolling do-not-contact people, an infinite loop hanging ICP ingestion — each pinned by a test (suite 1,843 → 2,294)
- Internal: CI now runs the test suite on every PR and push to main; releases previously reached PyPI with zero tests run

## v0.10.220 (2026-08-20)
- Fix: a reply was matched to a skipped duplicate of the same person, so a real answer to our invite was dropped as "not a reply"
- Fix: a stale cloud pull rewound a just-sent invite back to pending, and the planner spent the day re-inviting
- Fix: a connection note a few characters over the 200-char cap was cut mid-clause and shipped with "..."

## v0.10.218 (2026-08-19)
- Fix: signals were judged against every ICP on the account rather than the one belonging to their own campaign, so they came back rated against the wrong customer
- Fix: the signal backlog never drained — each run took only the newest signals, burying anything older than the arrival rate; 2,433 had gone unclassified across five days

## v0.10.217 (2026-08-19)
- Internal: the four places the version is written are now checked against each other, after two releases shipped with the MCP manifests a version behind

## v0.10.216 (2026-08-19)
- New: book_meeting() puts an agreed call on your Google Calendar and invites the prospect

## v0.10.215 (2026-08-19)
- Fix: a migration that reached get_db() opened a second database connection that nothing could close or reach again

## v0.10.214 (2026-08-19)
- New: send InMail via send_message(action='inmail')
- Fix: a tick claimed more jobs than its own 55s budget could run, so it was cancelled mid-flight and left the jobs it had not reached stuck until the 30-minute sweeper
- Fix: a short collector run now resumes where the last one stopped instead of restarting
- Fix: an ACoAA id in linkedin_id was looked up in the slug column
- Fix: a connections-cache miss blocked follow-ups to real connections
- Fix: signal_score is persisted at save and classify, and backfilled at startup
- Fix: stop writing ambiguous numeric author ids to signals.linkedin_id; watchlist signal authors are matched to campaign contacts by public slug

## v0.10.212 (2026-08-18)
- Fix: a rejected identifier never reached the code meant to stop retrying it — the profile backfill re-asked the same dead ids every 15 minutes
- Fix: three executors imported a module that was never added — the four-hourly connection sync had been failing silently on every run
- Fix: two backups written in the same millisecond overwrote each other, leaving fewer real rollback points than the backup count suggested

## v0.10.211 (2026-08-18)
- New: message brief decides WHAT every invitation says
- Fix: the suite wrote into the user's real log file
- Fix: no sync DB access may reach the event loop, and a test that enforces it

## v0.10.209 (2026-08-18)
- Fix: the cloud-fallback check asked the wrong door (status endpoint, honest alert reasons)

## v0.10.207 (2026-08-18)
- Fix: migration pass no longer copies the whole DB on every process start (user_version gating)
- Fix: dashboard shows when observe mode last reached the cloud (push freshness)

## v0.10.206 (2026-08-18)
- Fix: the phantom repair learns every acceptance signal and stops waiting for restarts
- Fix: a status flip is not an observed acceptance
- Fix: the reply gates must not swallow what they filter

## v0.10.188 (2026-08-07)
- Fix: the periodic 15-minute cloud push now chunks like backfill — a single 30s request failed every time on accounts with real history, leaving hosted dashboards stale

## v0.10.187 (2026-08-07)
- Fix: backfill_cloud ships full history in ordered 1000-row chunks — a single large request could exceed Cloud Run's 300s limit and fail (found pushing 15k rows of real history)
- Fix: legacy inbox-import messages without ids are skipped during sync instead of failing the whole payload with a 422

## v0.10.186 (2026-08-07)
- New: `scheduler(action='backfill_cloud')` — one-shot push of your FULL local history (completed and manual campaigns included) to the hosted dashboard at heylead.dev
- New: periodic cloud push — in backend mode, local state syncs to the hosted dashboard every 15 minutes, so it stays current without manual pushes
- Requires a linked hosted account (setup_profile with your heylead.dev token); direct-mode-only installs are unaffected

## v0.10.185 (2026-08-07)
- Fix: connection sync could mass-delete real relations — the prune step ran on inbox-fallback data (a small, skewed subset of the network); fallback data is still upserted but never drives the prune
- Fix: campaign_refill ran even with the scheduler disabled and enrolled recipients silently; it now sits behind the scheduler toggle and every refill enrollment writes an outreach_created audit record
- Fix: direct mode no longer warns "Could not sync to cloud scheduler" on pause/resume/emergency stop — there is no cloud scheduler to sync in direct mode

## v0.10.184 (2026-08-07)
- Fix: Unipile relations are keyed `member_id` now — connection sync stored 0 rows and silently fell back to inbox chats; the 1st-degree DM guard was starved of real data
- Fix: prompt rendering crashed on 4 of 15 prompts (JSON braces vs str.format) — voice analysis, engagement comments, follow-up reasoning, and prospect scoring were disabled
- Fix: rate-limit accounting counted the wrong action types, skewing daily caps in both directions
- Change: create_campaign saves a draft; launching is explicit via campaign(action="launch")
- Removed: copilot mode — it bypassed send-time safety guards
- Change: current LLM model ids (gemini-3.6-flash / claude-sonnet-5 / gpt-5.6-terra) with quality/fast tiers; thinking-token headroom so answers aren't truncated
- New: structured LLM outputs with server-side schema enforcement (replaces JSON repair)
- Fix: calendar booker could return a slot in the past
- Security: stale MCP-registry token files no longer ship in the sdist
- Docs: hosted mode (heylead.dev) re-documented alongside self-hosted; tool list updated to the 27 consolidated tools

## v0.10.103 (2026-03-20)
- Fix: prevent DM jobs racing against pending invites in invitation-based campaigns
- Root cause: _plan_dms() picked 'pending' prospects before invite was accepted, causing 403 subscription_required errors
- Now only schedules DMs for 'connected' prospects when invitations are enabled

## v0.10.75 (2026-03-18)
- Fix: add 4-hour cooldown to alert emails to prevent spam

## v0.10.59 (2026-03-16)
- Verify DM delivery before counting + block invitations to existing connections

## v0.10.0 (2026-03-02)
- New: database backup & restore system — automatic backups before reset and migrations
- New: `heylead backup` CLI command — create manual backups using SQLite's safe backup API
- New: `heylead restore` CLI command — interactive restore from available backups
- New: fresh-DB detection warning when config exists but database is missing (data loss alert)
- New: automatic backup rotation (keeps last 5 backups in ~/.heylead/backups/)

## v0.9.40 (2026-03-01)
- New: profile view warm-up action — lightest touch before following
- New: scheduler auto-views prospect profiles every 10 min (50/day limit)
- New: `engage_prospect(action="view")` MCP tool action
- New: `enable_profile_views` per-campaign toggle in edit_campaign
- Warm-up sequence: Profile View → Follow → Endorse → Engage → Invite → Follow-up DM

## v0.9.39 (2026-03-01)
- New: LinkedIn people lookup with full profile enrichment (email, phone, experience, education, skills, connections)
- New: enriched profile display in contacts(action='view') — contact info, network stats, experience, flags
- Fix: BackendClient.get_profile() endpoint corrected (/api/v1/linkedin/users/)
- New: expanded local contact search now includes email and location fields

## v0.9.38 (2026-02-28)
- New: global contact base with 9 actions (list, search, view, tag, note, stage, stats, export, linkedin_search)

## v0.9.37 (2026-02-28)
- New: global contact base + post intelligence + engagement improvements
- New: auto-assign inbound leads to best-matching campaign + pipeline fixes
- Fix: prevent duplicate comments on same LinkedIn post + anomaly detection
- v0.9.33: Full scheduler observability — event log, metrics, diagnostics, correlation IDs
- New: per-campaign flexible settings — toggles for warm-up, engagement, follow-ups, timing
- New: voice memos ON by default for new campaigns
- New: golden set messaging quality overhaul — anti-patterns + few-shot examples
- Fix: SQLite WAL locking — increase retries, add autocheckpoint + synchronous=NORMAL
- New: replace data-completeness scoring with ICP match + composite lead score
- New: async DB bridge, profile change signals, expanded profile editing

## v0.9.33 (2026-02-28)
- Full scheduler observability: event log table, job metrics, error categorization
- New MCP actions: scheduler(action='logs') and scheduler(action='diagnostics')
- Job duration tracking with duration_ms persisted to DB
- Timer state persistence to SQLite (survives MCP server restarts)
- Dual JSON+text logging (~/.heylead/logs/heylead.json.log)
- Correlation IDs across MCP client → backend (X-Correlation-ID header)
- 24h metrics summary in scheduler(action='status')
- Daily digest job with email delivery
- OpenTelemetry trace spans (opt-in, no-op when OTel not installed)
- heylead-logs CLI: summary, errors, slow, search, correlate, tail

## v0.9.32 (2026-02-28)
- Flexible campaign settings: 15 new per-campaign toggles via edit_campaign
- Warm-up sequence toggles: enable_follows, enable_endorsements, enable_engagements, enable_followups (on/off)
- Engagement mode: auto (30/70 react/comment), comment_only, or react_only
- Follow-up customization: max_followups (1-5), followup_delay_days (custom schedule)
- Send timing: send_in_business_hours (on/off), active_days (choose which days to send)
- Stale invite settings: withdraw_stale_invites (on/off), stale_invite_days (7-60)
- show_status now displays non-default campaign settings
- All settings stored in config_json with backwards-compatible defaults

## v0.9.31 (2026-02-28)
- Voice memos ON by default: new campaigns launch with voice_mode='mixed' (alternating text & voice)
- Added voice_mode parameter to create_campaign tool
- Agent instructions now ask users about voice preference before campaign creation
- suggest_next_action flipped: suggests disabling voice if acceptance is low instead of enabling

## v0.9.22 (2026-02-26)
- Fix: FK constraint on scheduler_jobs — global jobs now use NULL campaign_id instead of empty string
- Fix: missing `list_campaigns` import in planner (NameError on schedule cycle)
- Fix: `get_setting` import path in channel_selector (ImportError)
- Fix: `save_setting` UnboundLocalError in create_campaign (redundant local import)
- Fix: missing `updated_at` column in contacts table (added schema + migration)
- Fix: retry 502/503/504 on search params endpoint (use _retry_request)
- Fix: endorsement retry loop — detect permanent failures by error phrase, not just HTTP status
- Fix: duplicate MCP log lines — remove redundant stderr handler
- Fix: brand strategy auto-apply logging for headline/summary/photo failures
- Fix: add x-restli-method header for LinkedIn profile update Voyager calls

## v0.9.21 (2026-02-26)
- Fix: singleton BackendClient — httpx AsyncClient no longer closed prematurely between tool calls
- Fix: create_campaign dedup crash ("Cannot send a request, as the client has been closed")

## v0.9.20 (2026-02-26)
- Fix: scheduler leader election — only one instance runs the scheduler, others serve MCP tools in follower mode
- Fix: hardened DB retry logic (8 retries with jitter, busy_timeout 60s)

## v0.9.19 (2026-02-26)
- Fix: validate engagement API results in brand strategy, save to DB

## v0.9.18 (2026-02-26)
- Fix: retry DB operations on lock + silence httpx stderr noise

## v0.9.17 (2026-02-26)
- Update OpenClaw listing with 22 consolidated tools

## v0.9.16 (2026-02-26)
- New: custom domain heylead.dev, brand strategy execution, campaign UX

## v0.9.15 (2026-02-26)
- New: heylead://changelog MCP resource — clients can now query version history
- Fix: __version__ sync between pyproject.toml and runtime

## v0.9.14 (2026-02-26)
- Voice enhancement pipeline: text humanization, multi-utterance delivery, ambient noise overlay
- OpenClaw integration: SKILL.md + clawhub.json for agent marketplace
- Singleton DB connection for reliability

## v0.9.13 (2026-02-26)
- Fix signal funnel: scoring, classification, and activation pipeline

## v0.9.12 (2026-02-25)
- Voice memo test mocks fix, version bump

## v0.9.11 (2026-02-25)
- Always-on inbound pipeline with friend detection
- Tool consolidation: 39 → 22 tools via action-parameter pattern

## v0.9.10 (2026-02-24)
- Velocity count mismatch fix, connection sync improvements, voice memo fixes

## v0.9.9 (2026-02-23)
- Voice memos via Hume AI: TTS generation, LinkedIn delivery, A/B testing

## v0.9.8 (2026-02-22)
- Signal-based selling v1.0: keyword monitoring, prospect post scanning, compound intents
- Live dashboard sync with backend

## v0.9.7 (2026-02-21)
- Tracking accuracy: 9 gap fixes, engagement linking, E2E tests

## v0.9.6 (2026-02-20)
- Strategy engine: autonomous cross-campaign optimization for revenue
- Campaign optimizer, pattern detector, revenue estimator, campaign spawner

## v0.9.5 (2026-02-19)
- Fix: first DM after connection acceptance — create new chat fallback

## v0.9.4 (2026-02-18)
- Fix: check_replies UnboundLocalError + filter newsletter invitations

## v0.9.3 (2026-02-17)
- Mixed comment/react engagement strategy (30% react / 70% comment, first touch always comment)

## v0.9.2 (2026-02-16)
- Quick wins: 10-to-1 query optimization, stale lead warnings, cohort won/lost tracking

## v0.9.1 (2026-02-15)
- Inbound lead qualification pipeline: AI-qualified inbound leads with auto-DMs
- 3 signal types: invitations, DMs, post comments
- 2 new scheduler jobs: qualify_inbound (15 min), check_post_comments (30 min)

## v0.9.0 (2026-02-14)
- HubSpot CRM integration: sync won deals and hot leads
"""

# ──────────────────────────────────────────────
# Initialize MCP Server
# ──────────────────────────────────────────────

setup_logging()
logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Lifespan hook — background scheduler (Sprint 17)
# ──────────────────────────────────────────────

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator


@asynccontextmanager
async def _app_lifespan(app: FastMCP) -> AsyncIterator[dict]:
    """Start/stop the autonomous scheduler + cloud sync alongside the MCP server.

    Uses file-based leader election so only ONE process across all MCP clients
    (Claude Code, Cursor, etc.) runs the scheduler.  Other instances serve
    MCP tools normally in "follower" mode.
    """
    import asyncio
    from .scheduler.engine import SchedulerEngine
    from .scheduler.leader import acquire_leader_with_handover, release_leader

    # With a daemon installed, MCP servers are thin clients: they serve tools
    # and never schedule. This is what stops eight chat-session processes
    # racing for one lock, and it is why the flag is only ever set by
    # `heylead daemon --install` — with it on and no daemon running, nothing
    # schedules at all, which show_status reports as unhealthy.
    if config.load_config().get("scheduler_daemon", False):
        logger.info(
            "scheduler_daemon is set — not scheduling here; the daemon owns it. "
            "Check with: heylead daemon --status"
        )
        try:
            yield {"scheduler": None, "is_leader": False}
        finally:
            pass
        return

    from .services.cloud_sync import local_scheduler_engine_enabled

    # Waits only if it asked an older leader to stand down — see
    # acquire_leader_with_handover. Asking without waiting left nothing
    # scheduling at all.
    is_leader = await asyncio.to_thread(acquire_leader_with_handover, "mcp")

    engine: SchedulerEngine | None = None
    cloud_sync_task: asyncio.Task | None = None

    if is_leader:
        if local_scheduler_engine_enabled():
            engine = SchedulerEngine()
            await engine.start()
            logger.info("Scheduler engine started (leader mode, enabled=%s)",
                         config.is_scheduler_enabled())
        else:
            logger.info(
                "Hosted sending_host=cloud — local engine off; cloud owns every job"
            )
            try:
                from .db.async_bridge import run_db
                from .services.cloud_sync import (
                    stand_down_engine_off_leftovers,
                    warn_if_pypi_refresh_rolled_back,
                )
                warn_if_pypi_refresh_rolled_back()
                cancelled = await run_db(stand_down_engine_off_leftovers)
                if cancelled:
                    logger.info("Cancelled %d leftover local jobs (engine off)", cancelled)
            except Exception as e:
                logger.debug("Cancel cloud-owned jobs failed (non-fatal): %s", e)

        # Start cloud sync pull loop only for the leader
        cloud_sync_task = asyncio.create_task(_cloud_sync_loop())
    else:
        logger.info("Running in follower mode — scheduler delegated to leader process")

    try:
        yield {"scheduler": engine, "is_leader": is_leader}
    finally:
        if cloud_sync_task is not None:
            cloud_sync_task.cancel()
            try:
                await cloud_sync_task
            except asyncio.CancelledError:
                pass

        if engine is not None:
            await engine.stop()

        if is_leader:
            release_leader()


# Periodic push interval: keep the hosted dashboard current in backend mode.
CLOUD_PUSH_INTERVAL_SECONDS = 900  # 15 minutes

# How soon a *failed* push is tried again. The attempt used to be stamped
# whether or not it worked, so one dropped connection froze the hosted
# dashboard — and the campaign state the cloud scheduler sends from — for the
# full 15 minutes. This puts the retry on the next loop iteration instead.
CLOUD_PUSH_RETRY_SECONDS = 300  # one sync loop


async def _cloud_sync_tick(state: dict) -> None:
    """One iteration of the cloud sync loop body (extracted for testability).

    In backend mode:
    - Pushes local state to the backend at most once per
      ``CLOUD_PUSH_INTERVAL_SECONDS`` so the hosted dashboard stays current.
      A failed push logs at debug and never breaks the loop.
    - Pulls cloud scheduler changes (only when the cloud scheduler is enabled).

    ``state`` keys: ``last_pull_ts`` (unix ts of last successful pull) and
    ``last_push_ts`` (unix ts of last periodic push attempt, 0 = never).
    """
    import time

    if not config.is_backend_mode():
        return

    from .services import cloud_sync

    # Directory pull is independent of the cloud sender — keep the local
    # cache fresh even when the hosted scheduler is switched off.
    try:
        await cloud_sync.sync_directory_from_cloud()
    except Exception as e:
        logger.debug("Directory pull failed: %s", e)

    # ── Push: sync local state so the hosted dashboard doesn't go stale ──
    now = int(time.time())
    if now - state.get("last_push_ts", 0) >= CLOUD_PUSH_INTERVAL_SECONDS:
        # Stamped before the await so a slow push cannot be started twice, and
        # rewound below if it did not land.
        state["last_push_ts"] = now
        failure = ""
        try:
            result = await cloud_sync.sync_to_cloud()
            # sync_to_cloud reports HTTP failures by returning, not raising.
            failure = (result or {}).get("error", "")
        except Exception as e:
            failure = repr(e)
        if failure:
            logger.debug("Cloud sync push failed: %s", failure)
            state["last_push_ts"] = now - CLOUD_PUSH_INTERVAL_SECONDS + CLOUD_PUSH_RETRY_SECONDS

        # Deletes that never reached the backend replay here until they land.
        # The local rows are already gone, so nothing else can name these
        # campaigns — an orphaned cloud row keeps sending forever otherwise.
        try:
            await cloud_sync.retry_pending_cloud_deletes()
        except Exception as e:
            logger.debug("Pending cloud delete retry failed: %s", e)

    # ── Pull: only if cloud scheduler is enabled ──
    try:
        status = await cloud_sync.get_cloud_scheduler_status()
        if not status.get("enabled"):
            return
    except Exception:
        return

    # Pull changes since last successful pull
    try:
        changes = await cloud_sync.pull_changes(state["last_pull_ts"])
        if "error" not in changes:
            state["last_pull_ts"] = int(time.time())
    except cloud_sync.BackendAuthError as e:
        # The one failure the user has to act on, and the one this loop used to
        # bury at debug alongside every transient network blip. The cloud keeps
        # sending on an expired session; only the results stop arriving.
        logger.warning("Cloud sync pull rejected — session expired: %s", e)
        from .db.async_bridge import run_db
        await run_db(cloud_sync._record_pull, str(e))
    except Exception as e:
        logger.debug("Cloud sync pull failed: %s", e)


async def _session_health_loop() -> None:
    """Background loop: probe LinkedIn session health every 30 minutes.

    A separate task from the sync loop on purpose — the probe must never
    delay or interrupt the sync critical path. (28 Aug-5 Sep 2026: the
    session died — Unipile source CREDENTIALS — and nobody noticed for a
    week while LinkedIn silently discarded every send.) A failed probe
    changes nothing; see services.session_health.
    """
    import asyncio

    from .services.session_health import CHECK_INTERVAL_SECONDS, check_session_health

    while True:
        try:
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)
            await check_session_health()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("Session health check failed: %s", e)


async def _cloud_sync_loop() -> None:
    """Background loop: pull cloud scheduler changes every 5 minutes and push
    local state every 15 minutes (backend mode only).

    Only active when backend mode is configured. Pulls keep the local DB in
    sync with actions the backend took; periodic pushes keep the hosted
    dashboard current even when no push-triggering tool is used.
    """
    import asyncio

    from .db.async_bridge import run_db
    from .db.queries import get_setting

    SYNC_INTERVAL = 300  # 5 minutes
    # Seeding from the clock would silently drop everything the backend did
    # while this process was down: the first pull stamps last_pull_timestamp,
    # and ensure_synced reads that same setting, so the window is then gone.
    try:
        last_pull = int(await run_db(get_setting, "last_pull_timestamp", 0) or 0)
    except Exception:
        last_pull = 0
    state = {"last_pull_ts": last_pull, "last_push_ts": 0}

    while True:
        try:
            await asyncio.sleep(SYNC_INTERVAL)
            await _cloud_sync_tick(state)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("Cloud sync loop error: %s", e)


def _reads(title: str) -> ToolAnnotations:
    """Annotations for a tool that only reads."""
    return ToolAnnotations(title=title, readOnlyHint=True, openWorldHint=True)


def _acts(title: str) -> ToolAnnotations:
    """Annotations for a tool that sends, publishes, deletes or overwrites.

    Tools that multiplex actions behind ``action=`` carry the hint of their
    most dangerous action, so a client asks before any of them runs.
    """
    return ToolAnnotations(
        title=title, readOnlyHint=False, destructiveHint=True, openWorldHint=True,
    )


mcp = FastMCP(
    "heylead",
    lifespan=_app_lifespan,
    instructions=(
        "HeyLead is an AI agent for LinkedIn outreach: it finds the right people, "
        "writes to them in the user's own voice, follows up, and handles replies, "
        "on the user's own LinkedIn account.\n"
        "\n"
        "Use it when the user wants to reach people on LinkedIn for any of these jobs:\n"
        "- sales: lead generation, B2B prospecting, cold outreach, booking meetings\n"
        "- recruiting: sourcing candidates, hiring (edit_campaign campaign_intent='recruit')\n"
        "- research: finding user-interview participants, customer discovery, experts\n"
        "- job search: reaching hiring managers, referrals (campaign_type='job_search')\n"
        "- investor, partner and reseller outreach (campaign_intent='partner')\n"
        "- vendor scouting, where the user is the buyer (campaign_intent='buy')\n"
        "- event and webinar invitations, and re-engaging existing connections\n"
        "- LinkedIn posts in the user's voice (create_post, brand_strategy)\n"
        "Selling and buying have full message sets. Recruiting and partner outreach "
        "share the selling templates beyond their system prompt, and research and "
        "event outreach have no set of their own, so give those a clear "
        "project_brief that says what is being asked of the person.\n"
        "\n"
        "Safety: create_campaign saves a draft and sends nothing. Outreach starts only "
        "with campaign(action='launch'), and only when the user asks. Sends respect "
        "rate limits, quiet hours and opt-outs. campaign(action='emergency_stop') "
        "pauses everything.\n"
        "Not set up yet? Call setup_profile(); it returns a sign-in link.\n"
        "Workflow: setup_profile → generate_icp → create_campaign → automated outreach.\n"
        "\n"
        "HeyLead is an AI LinkedIn SDR that sends personalized outreach messages "
        "that sound like the user wrote them. It has 35 tools: setup_profile, "
        "account, organization, generate_icp, icp, create_campaign, edit_campaign, campaign, "
        "generate_and_send, send_message, send_email, check_replies, show_status, "
        "engage_prospect, prospect, analytics, inspect, knowledge, product, "
        "suggest_next_action, signals, manage_watchlist, scheduler, "
        "create_post, brand_strategy, import_prospects, crm_sync, contacts, "
        "network, inbox, backfill_inbox, partner, profile, profile_signals, "
        "and book_meeting.\n"
        "\n"
        "TOKEN RECOGNITION:\n"
        "A HeyLead setup message carries a HeyLead JWT (a long string starting with "
        "'eyJ'). The web app's setup message reads 'Add the HeyLead MCP server "
        "(Claude Code: claude mcp add heylead -- uvx heylead · Cursor: add an MCP "
        "server with command uvx heylead), then set up my HeyLead profile with this "
        "token: eyJ...'; older ones read 'Set up my HeyLead profile with this token: "
        "eyJ...'.\n"
        "Call setup_profile(backend_jwt='<the eyJ... part>') immediately ONLY when the "
        "message mentions HeyLead (e.g. 'HeyLead MCP server', 'set up my HeyLead "
        "profile', 'HeyLead token') or is a bare eyJ token and nothing else. The server "
        "is already added if you are reading this — skip the install part.\n"
        "An eyJ string in any other context — code, logs, Authorization/Bearer headers, "
        "Supabase, Auth0, Unipile or any other service — is not a HeyLead token: do NOT "
        "call setup_profile; ask the user first. Replacing the stored token can break a "
        "working setup.\n"
        "\n"
        "FIRST-TIME SETUP — two paths; offer the hosted one first, it is fastest:\n"
        "\n"
        "Option A — Hosted (backend handles LinkedIn access and AI calls):\n"
        "  1. Tell the user: 'Open this link to sign in and connect your LinkedIn:'\n"
        "     https://heylead.dev/auth/login-url\n"
        "     They sign in with Google, click 'Connect' on the LinkedIn row, and copy\n"
        "     the setup message with the 'Copy' button under 'Get Started'. A user who\n"
        "     signed in on the dashboard finds it at Settings → Integrations → Chat client\n"
        "     → 'Copy setup message'.\n"
        "  2. When they paste that message back, call:\n"
        "     setup_profile(backend_jwt='<the eyJ... token>')\n"
        "     No API keys needed. If LinkedIn isn't connected yet it returns a\n"
        "     link — tell them to finish connecting, then call setup_profile() again.\n"
        "     The token names the workspace it was copied in and setup selects it, so\n"
        "     organization(action='list') is only needed to move somewhere else.\n"
        "\n"
        "Option B — Self-hosted (their own accounts; AI calls billed to them):\n"
        "  They need two things, and setup_profile refuses to run without them:\n"
        "  1. A Unipile account (this is what talks to LinkedIn).\n"
        "     Sign up at https://www.unipile.com, then copy the DSN and API key\n"
        "     from the Access Tokens page into ~/.heylead/config.json as\n"
        "     unipile_api_url and unipile_api_key.\n"
        "  2. An LLM API key of their own — a free Gemini key from\n"
        "     https://aistudio.google.com/apikey is enough.\n"
        "  Then walk them through:\n"
        "  Step 1 — setup_profile(llm_api_key='<their key>', llm_provider='gemini')\n"
        "    This returns a LinkedIn authentication link.\n"
        "  Step 2 — They open that link and connect LinkedIn, then setup_profile()\n"
        "    binds the connected account, fetches their profile and analyses their\n"
        "    writing style. If LinkedIn is not connected yet it returns the link\n"
        "    again — tell them to finish connecting and call it once more.\n"
        "\n"
        "After setup is complete, the user can:\n"
        "  - generate_icp('target description') to create a rich ICP with buyer personas\n"
        "  - icp(action='preview', icp_id='...') to see which LinkedIn profiles a saved ICP\n"
        "    matches, and how each filter is shaping the result, WITHOUT creating a campaign\n"
        "    or any outreach records. Use this before create_campaign when the targeting is\n"
        "    unproven, or when the user asks for example profiles for an ICP.\n"
        "  - create_campaign('description') or create_campaign(icp_id='...') to find prospects.\n"
        "    This does NOT send anything — the campaign is saved as a draft so the user\n"
        "    can review the prospects first. Show them the result, then start outreach with\n"
        "    campaign(action='launch') once they confirm. Never launch without being asked.\n"
        "    Voice memos are OFF by default (voice_mode='text_only'). Pass voice_mode='mixed' only if the user asks for voice memos.\n"
        "    For a user's FIRST campaign, always ask for project_brief (what they are building, go-live, volume, what a vendor must confirm). A homepage or company_context paste is a fallback, not the whole brief.\n"
        "    If the user says 'existing connections', 'DM my network', 'message my connections', or similar,\n"
        "    pass connections_only='on'. This filters for 1st-degree connections only and sends DMs directly.\n"
        "  - show_status() — campaign dashboard with progress and stats\n"
        "  - check_replies() — see who responded, inbound invitations, and profile viewers\n"
        "  - inspect() — read-only digest of operator holds, replans, closer decisions, and reply skips.\n"
        "    AGENT OPS: if they ask what the agents did, who is held, or why a reply\n"
        "    was skipped, call inspect() first — it never writes. A hold →\n"
        "    prospect(action='conversation') then send_message(action='reply').\n"
        "    Never paste model-authored text as the LinkedIn message; the send\n"
        "    tools generate it. Agents default to act. Use\n"
        "    edit_campaign(enable_reply_agent='observe'),\n"
        "    edit_campaign(enable_strategist_replan_agent='observe'),\n"
        "    edit_campaign(enable_hot_lead_closer='observe'), or\n"
        "    edit_campaign(enable_coordinator_agent='observe') to return to\n"
        "    logging-only, 'off' to disable. If they ask what the\n"
        "    agents left for the next tick, what the swarm thinks, or who\n"
        "    went dark, call inspect(action='commons'). On a hosted account, "
        "    inspect(action='journal') is the agents' diary. "
        "    inspect(action='review') is the campaign watch: in-window stalls "
        "    and the safe adjustments the tick already applied. "
        "    campaign(action='clear_coordinator_hold', campaign_id=...) "
        "    releases a campaign-wide coordinator hold.\n"
        "  - product(action='tick', request='...') — local git checkout only: patch\n"
        "    this repo and/or open a PR. Never from the send path. Status with\n"
        "    product(action='status'). Cloud workers and uvx installs without .git refuse.\n"
        "  - book_meeting() — put an agreed call on your Google Calendar and invite them\n"
        "  - suggest_next_action() — AI-recommended next step\n"
        "  - analytics(action='report') — detailed analytics with outcomes and stale lead warnings\n"
        "  - generate_and_send() — manually trigger a single message\n"
        "  - send_message(action='followup') to send follow-up DMs after connection accepted\n"
        "  - send_message(action='reply') to reply to prospects who have messaged you\n"
        "  - send_message(action='voice') to send a voice memo on LinkedIn\n"
        "  - send_message(action='inmail', outreach_id='...') to InMail a non-connection (pending invite OK)\n"
        "  - send_email(to=..., subject=..., body=...) to send mail via Unipile "
        "(Gmail/Outlook). NEVER use Mail.app, osascript, or a local SMTP client.\n"
        "    If no mailbox is connected, call account(action='connect_email') and "
        "open the hosted-auth link, then retry send_email.\n"
        "  - engage_prospect() to comment on, react to, follow, or endorse a prospect on LinkedIn\n"
        
        "  - campaign(action='monitor') to activate a campaign for signal collection only —\n"
        "    it sends nothing and requires scheduler(action='observe')\n"
        "  - campaign(action='pause') / campaign(action='resume') to control campaign status\n"
        "  - campaign(action='archive') to archive a completed campaign\n"
        "  - campaign(action='delete') to permanently delete a campaign\n"
        "  - campaign(action='emergency_stop') to immediately pause all active campaigns\n"
        "  - campaign(action='retry_failed') to retry outreaches that failed with errors\n"
        "  - campaign(action='status_history') to view who changed campaign status and when\n"
        "  - analytics(action='export') to export campaign results as a table\n"
        "  - analytics(action='compare') to compare 2+ campaigns side by side\n"
        "  - prospect(action='skip', outreach_id='...') to skip a bad-fit prospect\n"
        "  - prospect(action='conversation', outreach_id='...') to view the full message thread\n"
        "  - prospect(action='close', outcome='won') to record a won/lost/opt_out outcome\n"
        "  - edit_campaign(name='...', mode='...') to edit campaign name, mode, or settings\n"
        "  - edit_campaign(enable_engagements='off') to disable comments/reactions\n"
        "  - edit_campaign(enable_followups='off') to disable follow-up DMs\n"
        "  - edit_campaign(engagement_mode='comment_only') to change engagement style\n"
        "  - edit_campaign(send_in_business_hours='off') to also send outside business hours (on by default)\n"
        "  - edit_campaign(max_followups=3) to limit follow-up count\n"
        "  - scheduler(action='status') to view the autonomous scheduler status\n"
        "  - scheduler(action='observe') to keep collecting and classifying signals while\n"
        "    this machine sends nothing — invitations, DMs and engagements all stop.\n"
        "    Observe is local: if 24/7 cloud scheduling is on, the backend keeps sending\n"
        "    from campaigns it already holds until you also turn that off\n"
        "  - scheduler(action='toggle', enabled=True, cloud=True) to enable 24/7 cloud scheduling\n"
        "  - signals(action='show') to view buying signals from LinkedIn\n"
        "  - signals(action='strategy') to view strategy engine insights\n"
        "  - crm_sync(filter='won') to sync won deals to HubSpot CRM\n"
        "  - contacts(action='list') to browse your global contact base\n"
        "  - contacts(action='view', contact_id='...') to see full cross-campaign history\n"
        "  - contacts(action='search', query='...') to search by name/company/title\n"
        "  - contacts(action='tag', contact_id='...', tag='enterprise') to tag contacts\n"
        "  - contacts(action='stats') to see contact base dashboard\n"
        "  - inbox(action='list') to browse all LinkedIn inbox conversations\n"
        "  - inbox(action='read', name='...') to read a full conversation thread\n"
        "  - Read the heylead://changelog resource for version history and release notes\n"
        "\n"
        "DASHBOARD LINKS AND SNAPSHOTS:\n"
        "On hosted accounts, status replies (show_status, campaign launch/pause/resume/"
        "monitor, analytics report, suggest_next_action) end with a link to the matching "
        "page of the web dashboard and attach a PNG snapshot of it as a second content "
        "block. Some clients (Claude Code CLI) do not show that image to the user, so "
        "always relay the link, tell the user a snapshot is attached and that the link "
        "opens the live view, and check the snapshot agrees with the text. Setting "
        "\"dashboard_snapshots\": false in ~/.heylead/config.json keeps the link and "
        "drops the image.\n"
        "\n"
        "IMPORTANT: If the user asks to find leads, send messages, or anything "
        "outreach-related and setup is not complete, do NOT try to call those tools. "
        "Instead, guide them through the setup steps above first.\n"
        "\n"
        "PREFER TOOLS OVER LLM:\n"
        "When the user asks for an ICP, buyer personas, targeting, campaign creation, "
        "or LinkedIn outreach, you MUST call the HeyLead tools (generate_icp, "
        "create_campaign, generate_and_send, etc.) — do not substitute with your own "
        "text or analysis. Example: if they ask for 'ICP for SPhotonix' or 'personas "
        "for data archiving', call generate_icp(target_description=..., "
        "company_context='https://sphotonix.com/') rather than writing personas yourself."
    ),
)

def _report_package_version(server) -> None:
    """Make initialize report HeyLead's version as serverInfo.version.

    FastMCP takes no version argument, so initialize reports the mcp library's
    own version. The low-level server uses its ``version`` attribute when set.
    Both are private to mcp, and ``uvx heylead`` installs the newest mcp<2 —
    so a release that renames them must cost the version string, not startup.
    """
    lowlevel = getattr(server, "_mcp_server", None)
    if lowlevel is not None and hasattr(lowlevel, "version"):
        lowlevel.version = __version__
    else:
        logger.debug(
            "mcp low-level server has no version attribute; "
            "serverInfo.version stays the library's",
        )


_report_package_version(mcp)


# ──────────────────────────────────────────────
# MCP Resource: capabilities
# ──────────────────────────────────────────────

import json as _json


def _registered_tool_count() -> int | None:
    """How many tools this server registers, or None if mcp hides it.

    The tool manager is private to mcp, and `uvx heylead` installs the newest
    mcp<2, so a rename must cost the count, not the resource.
    """
    lister = getattr(getattr(mcp, "_tool_manager", None), "list_tools", None)
    return len(lister()) if callable(lister) else None


@mcp.resource("heylead://capabilities")
def capabilities() -> str:
    """HeyLead server capabilities and tool inventory."""
    return _json.dumps({
        "name": "heylead",
        "version": __version__,
        "tools": _registered_tool_count(),
        "capabilities": [
            "linkedin_outreach",
            "icp_generation",
            "campaign_management",
            "reply_handling",
            "engagement_warmup",
            "analytics",
            "autonomous_scheduling",
            "dashboard_snapshots",
        ],
        "resources": ["heylead://capabilities", "heylead://changelog"],
        "transport": ["stdio", "sse", "streamable-http"],
        "install": "uvx heylead",
        "pypi": "https://pypi.org/project/heylead/",
        "repository": "https://github.com/D4umak/linkedin-outreach-mcp",
    }, indent=2)


# ──────────────────────────────────────────────
# MCP Resource: changelog
# ──────────────────────────────────────────────


@mcp.resource(
    "heylead://changelog",
    name="changelog",
    title="HeyLead Version History",
    description="Release notes and changelog for recent HeyLead versions",
    mime_type="text/markdown",
)
def changelog() -> str:
    """HeyLead version history and release notes."""
    return _CHANGELOG


# ──────────────────────────────────────────────
# Tool 1: setup_profile
# ──────────────────────────────────────────────

@mcp.tool(annotations=_acts("Set up HeyLead and connect LinkedIn"))
async def setup_profile(
    llm_api_key: str = "",
    llm_provider: str = "gemini",
    backend_url: str = "",
    backend_jwt: str = "",
) -> str:
    """Set up HeyLead by connecting your LinkedIn account and analyzing your writing style.

    REQUIRED for first-time users — must be called before any other tool.

    This analyzes your LinkedIn profile, posts, and writing style to create
    a "voice signature" so every outreach message sounds like YOU, not a bot.
    Handles LinkedIn automation setup, SDR onboarding, account connection,
    and voice analysis for personalized outreach.

    First-time setup: sign in at https://heylead.dev/auth/login-url, click
    'Connect' on the LinkedIn row, copy the setup message ('Copy' under
    'Get Started'), then call this tool with the eyJ... token from it as
    backend_jwt. No API keys needed on the hosted backend.

    Args:
        llm_api_key: Optional — only if you want to use your own AI key instead of the backend's.
        llm_provider: Which AI to use if providing your own key: "gemini", "claude", or "openai".
        backend_url: HeyLead Backend API URL. Leave empty — defaults to production server.
        backend_jwt: Your authentication token from HeyLead.
    """
    from .tools.setup_profile import run_setup_profile

    logger.info("Running setup_profile")
    try:
        result = await run_traced(
            "setup_profile",
            run_setup_profile(
                llm_api_key=llm_api_key,
                llm_provider=llm_provider,
                backend_url=backend_url,
                backend_jwt=backend_jwt,
            ),
        )
        logger.info("setup_profile completed")
        return result
    except Exception as e:
        logger.error(f"setup_profile failed: {e}", exc_info=True)
        return f"❌ Setup failed: {e}\n\nCheck ~/.heylead/logs/heylead.log for details."


# ──────────────────────────────────────────────
# Tool: account (consolidated)
# ──────────────────────────────────────────────

@mcp.tool(annotations=_acts("Manage connected LinkedIn and email accounts"))
async def account(
    action: str = "list",
    account_id: str = "",
) -> str:
    """Manage your LinkedIn accounts — list, switch, or disconnect.

    Args:
        action: What to do:
            "list"          — Show all connected LinkedIn accounts (default)
            "switch"        — List accounts and pick one to switch to
            "switch_to"     — Switch to a specific account by ID
            "unlink"        — Disconnect the current LinkedIn account
            "connect_email" — Connect Gmail/Outlook via Unipile hosted auth.
                              Never use Mail.app.
            "refresh_tier"  — Re-check Sales Navigator on your accounts and heal the stored tier flags
        account_id: The Unipile account ID (required for "switch_to").
    """
    from .tools.account import run_account

    logger.info(f"Running account: action={action}")
    try:
        return await run_traced("account", run_account(action, account_id), action=action)
    except Exception as e:
        logger.error(f"account failed: {e}", exc_info=True)
        return f"Account action failed: {e}"


@mcp.tool(annotations=_acts("List or switch HeyLead workspaces"))
async def organization(
    action: str = "list",
    org_id: str = "",
    email: str = "",
    role: str = "editor",
    user_id: str = "",
) -> str:
    """List, switch, or manage hosted HeyLead organizations.

    Use this to work across client workspaces. The person stays signed in;
    only the active organization changes.

    Args:
        action: list | switch | members | invite | remove_member | create
        org_id: Organization id (required for switch; optional if already switched).
        email: Invitee email (invite), or the new org name (create).
        role: editor or viewer (invite).
        user_id: Member to remove (remove_member).
    """
    from .tools.organization import run_organization

    logger.info(f"Running organization: action={action}")
    try:
        return await run_traced(
            "organization",
            run_organization(action, org_id, email, role, user_id),
            action=action,
        )
    except Exception as e:
        logger.error(f"organization failed: {e}", exc_info=True)
        return f"Organization action failed: {e}"


# ──────────────────────────────────────────────
# Tool 2a: generate_icp
# ──────────────────────────────────────────────

@mcp.tool(annotations=_acts("Generate an ideal customer or candidate profile"))
async def generate_icp(
    target_description: str,
    company_context: str = "",
    focus_query: str = "",
    decision_makers_only: bool = True,
) -> str:
    """Generate a rich Ideal Customer Profile with buyer personas.

    The same profile describes whoever the user needs to reach: buyers,
    candidates to recruit, research or user-interview participants, hiring
    managers for a job search, investors or partners.

    Creates 2-4 ICP personas with pain points, fears, barriers,
    LinkedIn search parameters, and confidence scores. The result
    is saved and can be reused with create_campaign(icp_id=...).
    Supports target audience analysis, customer segmentation, buyer persona
    creation, ideal customer profiling, and B2B market research.

    Args:
        target_description: Who to target (e.g., "CTOs at fintech startups",
            "freelance UX designers in London", "yoga studio owners in California")
        company_context: Optional URL or text about your company/product.
            Providing this makes the ICP more precise and evidence-backed.
        focus_query: Optional focus (e.g., "enterprise segment only",
            "focus on pain points around compliance")
        decision_makers_only: Keep every persona's seniority to people who
            hold budget authority — owner, cxo, vp, director. Default True.
            Pass False only when the target really is individual contributors
            (developers, designers, analysts); the ICP then keeps whatever
            levels the description implies.
    """
    from .tools.generate_icp import run_generate_icp

    logger.info(f"Running generate_icp: {target_description}")
    try:
        return await run_traced(
            "generate_icp",
            run_generate_icp(
                target_description, company_context, focus_query,
                decision_makers_only=decision_makers_only,
            ),
        )
    except Exception as e:
        logger.error(f"generate_icp failed: {e}", exc_info=True)
        return f"ICP generation failed: {e}"


# ──────────────────────────────────────────────
# Tool 2a-ter: profile_signals
# ──────────────────────────────────────────────

@mcp.tool(annotations=_acts("Compile targeting evidence from LinkedIn profiles"))
async def profile_signals(
    action: str = "compile",
    request: str = "",
    titles: str = "",
    location_codes: str = "",
    profiles_json: str = "",
) -> str:
    """Compile a targeting request into LinkedIn recall + profile evidence.

    Use this to see how HeyLead looks for country ties (school, language,
    worked-in) or interests (car lovers, esoteric) on a full LinkedIn
    profile. Campaigns still go through generate_icp / create_campaign.

    Args:
        action: compile (default), preview, or schools (exact LinkedIn school names).
        request: e.g. "Ukrainians in the US" or "classic car lovers".
        titles: Optional comma-separated titles for recall queries.
        location_codes: Optional comma-separated LinkedIn location codes.
        profiles_json: preview only — JSON list of full profiles to score.
    """
    from .tools.profile_signals import run_profile_signals

    logger.info("Running profile_signals: action=%s", action)
    try:
        return run_profile_signals(
            action=action,
            request=request,
            titles=titles,
            location_codes=location_codes,
            profiles_json=profiles_json,
        )
    except Exception as e:
        logger.error("profile_signals failed: %s", e, exc_info=True)
        return f"profile_signals failed: {e}"


# ──────────────────────────────────────────────
# Tool 2a-bis: icp
# ──────────────────────────────────────────────

@mcp.tool(annotations=_reads("Preview or audit a saved ICP"))
async def icp(
    action: str = "preview",
    icp_id: str = "",
    persona: int = 1,
    limit: int = 10,
    campaign_id: str = "",
    target_description: str = "",
) -> str:
    """Preview a saved ICP against LinkedIn, or audit it against a campaign goal, without creating anything.

    Runs the search create_campaign would run from the ICP's enriched LinkedIn
    codes and shows what comes back, how each filter is shaping the result, and
    how the profiles score against the persona. It creates no campaign, no
    outreach records and no contacts, so an ICP can be checked and rejected
    without cleanup. Use it when the targeting is unproven or when someone asks
    for example profiles for an ICP.

    Args:
        action: "preview" shows matched profiles, the exact filters sent to
            LinkedIn with their resolved code names, a per-filter contribution
            readout, and the fit scores. "goal_match" runs no search at all:
            it asks whether the ICP's personas actually hold budget authority
            for the campaign's goal, grounded in the shipped sales-methodology
            knowledge base, and returns match / partial / mismatch with the
            decision-maker coverage and concrete fixes.
        icp_id: ID of a saved ICP from generate_icp (a truncated id works).
            Leave empty to list your saved ICPs.
        persona: Which persona of the ICP to search with, 1-based (default 1).
            An ICP usually holds 2-4; each has its own filters.
        limit: How many matched profiles to list, 1-50 (default 10). The search
            itself always fetches one full page regardless.
        campaign_id: goal_match only — take the goal and the offer from this
            campaign's config/context instead of typing them.
        target_description: goal_match only — the goal to audit against when
            there is no campaign yet. Falls back to the ICP's own target.
    """
    from .tools.icp import run_icp

    logger.info("Running icp: action=%s icp_id=%s persona=%s", action, icp_id, persona)
    try:
        return await run_traced(
            "icp",
            run_icp(
                action=action, icp_id=icp_id, persona=persona, limit=limit,
                campaign_id=campaign_id, target_description=target_description,
            ),
            action=action,
        )
    except Exception as e:
        logger.error("icp failed: %s", e, exc_info=True)
        return (
            f"ICP preview failed: {e}\n\n"
            "No campaign, outreach or contact was created — this tool never "
            "creates any."
        )


# ──────────────────────────────────────────────
# Tool 2b: create_campaign
# ──────────────────────────────────────────────

@mcp.tool(annotations=_acts("Create a LinkedIn outreach campaign (draft)"))
async def create_campaign(
    target_description: str,
    campaign_name: str = "",
    icp_id: str = "",
    company_context: str = "",
    mode: str = "autopilot",
    company_url: str = "",
    voice_mode: str = "text_only",
    connections_only: str = "",
    exclude_connections: str = "",
    project_brief: str = "",
    campaign_type: str = "",
    force: bool = False,
) -> str:
    """Create a LinkedIn outreach campaign from a natural language description.

    Finds people on LinkedIn who match the description and saves a draft;
    nothing is sent until campaign(action='launch'). Use it for sales
    prospecting, recruiting and candidate sourcing, research and
    user-interview recruitment, job-search networking, investor and partner
    outreach, vendor scouting and event invitations.

    Describe your ideal customers and HeyLead will find them on LinkedIn.
    Supports lead generation, prospect discovery, SDR automation, cold outreach,
    and targeted B2B sales campaigns with AI-powered ICP-based targeting.
    On first campaign, project_brief is asked explicitly (what you are building,
    go-live, volume, what a vendor must confirm) — a homepage alone is not enough.

    Args:
        target_description: Who to target (e.g., "CTOs at fintech startups",
            "freelance UX designers in London", "yoga studio owners in California")
        campaign_name: Optional name for the campaign.
        icp_id: Optional ID of a saved ICP from generate_icp. If provided,
            uses the saved ICP's enriched LinkedIn codes for precise targeting
            instead of generating a new one.
        company_context: Optional. Your website URL or 1-2 sentences about your
            product/company. Copied into project_brief when project_brief is omitted.
        project_brief: Optional. Full project paste the model sees: what you are
            building, go-live, volume, what a vendor must confirm. Required before
            launch, resume, or auto-send.
        mode: Always autopilot. Copilot mode removed.
        company_url: Optional LinkedIn company URL for account-based targeting.
            Searches for employees at that specific company matching the ICP.
            Example: "https://www.linkedin.com/company/google"
        voice_mode: Voice memo mode for follow-ups and replies. "text_only"
            (default), "mixed" (alternates text and voice), "voice_only", or "ab_test".
        connections_only: "on" to create a DM-only campaign targeting existing
            LinkedIn connections. Skips invitations and warm-up — sends DMs
            directly to people you're already connected with. Use when user says
            "existing connections", "DM my network", "message my connections".
        exclude_connections: ON BY DEFAULT for a new campaign: nobody who was
            already a 1st-degree connection before this campaign started is
            reached — refused at enrolment and skipped at send time rather than
            DMed. People who accept this campaign's own invitation still get
            the opener. Pass "off" to include existing connections (or turn it
            off later in the campaign settings). Defaults off only for a
            connections_only campaign. Use "off" when the user says
            "include my existing connections"; the old "on" is still accepted
            for "don't message my existing connections", "cold only",
            "skip people I already know". Cannot be combined with
            connections_only, which is its exact inverse.
        campaign_type: Prompt family: "outbound" (default) or "job_search".
            job_search writes a job-search campaign: the invitation note and
            the first DM may name the recipient's company and the role, use
            one credible proof point at most and never list a CV. InMail is
            not routed by this switch. Pair with connections_only="on" to
            write to people the sender is already connected to.
        force: True to create the campaign even when the goal <-> ICP audit
            returns `mismatch` (the ICP holds no plausible buyer for the goal).
            Leave False; a `partial` verdict never blocks, it only warns.
    """
    from .tools.create_campaign import run_create_campaign
    from .tools.organization import refuse_if_viewer

    blocked = await refuse_if_viewer()
    if blocked:
        return blocked

    logger.info(
        f"Running create_campaign: {target_description} (mode={mode}, "
        f"connections_only={connections_only}, exclude_connections={exclude_connections})"
    )
    try:
        return await run_traced(
            "create_campaign",
            run_create_campaign(
                target_description, campaign_name, icp_id, company_context, mode,
                company_url, voice_mode, connections_only,
                exclude_connections=exclude_connections,
                project_brief=project_brief,
                campaign_type=campaign_type,
                force=force,
            ),
        )
    except Exception as e:
        logger.error(f"create_campaign failed: {e}", exc_info=True)
        return f"❌ Campaign creation failed: {e}"


# ──────────────────────────────────────────────
# Tool 3: generate_and_send
# ──────────────────────────────────────────────

@mcp.tool(annotations=_acts("Generate and send one LinkedIn message"))
async def generate_and_send(
    campaign_id: str = "",
) -> str:
    """Generate a personalized LinkedIn message and send it (or queue for review).

    Creates and sends cold outreach, connection requests, and personalized
    LinkedIn invitations using voice-matched AI messaging.
    Sends automatically after validation. A message that fails validation is
    never sent.

    Args:
        campaign_id: Which campaign to send from. Uses active campaign if empty.
    """
    from .tools.generate_send import run_generate_and_send
    from .tools.organization import refuse_if_viewer

    blocked = await refuse_if_viewer()
    if blocked:
        return blocked

    logger.info(f"Running generate_and_send: campaign={campaign_id}")
    try:
        return await run_traced(
            "generate_and_send",
            run_generate_and_send(campaign_id),
            campaign_id=campaign_id,
        )
    except Exception as e:
        logger.error(f"generate_and_send failed: {e}", exc_info=True)
        return f"❌ Message generation failed: {e}"


# ──────────────────────────────────────────────
# Tool: book_meeting
# ──────────────────────────────────────────────

@mcp.tool(annotations=_acts("Book a meeting on Google Calendar"))
async def book_meeting(
    attendee_email: str,
    start: str,
    duration_minutes: int = 30,
    summary: str = "",
    description: str = "",
) -> str:
    """Book a meeting on your Google Calendar and invite a prospect.

    Use this when a reply agrees to a call. Creates the event on the calendar
    you connected, attaches a Google Meet link, and emails the attendee an
    invitation.

    Args:
        attendee_email: Who to invite — the prospect who replied.
        start: When it starts, ISO 8601, e.g. 2026-09-01T10:00:00.
        duration_minutes: How long the meeting runs. Defaults to 30.
        summary: Event title. Defaults to naming the attendee.
        description: Optional agenda or notes included in the invitation.
    """
    from .tools.book_meeting import run_book_meeting

    logger.info("Running book_meeting")
    try:
        return await run_traced(
            "book_meeting",
            run_book_meeting(
                attendee_email=attendee_email,
                start=start,
                duration_minutes=duration_minutes,
                summary=summary,
                description=description,
            ),
        )
    except Exception as e:
        logger.error(f"book_meeting failed: {e}", exc_info=True)
        return f"❌ Booking failed: {e}"


# ──────────────────────────────────────────────
# Tool 4: check_replies
# ──────────────────────────────────────────────

@mcp.tool(annotations=_acts("Check LinkedIn replies and inbound invitations"))
async def check_replies() -> str:
    """Check for new LinkedIn replies across all campaigns.

    Fetches new messages, classifies sentiment (positive/negative/question),
    and surfaces hot leads that need your attention.
    Handles inbox monitoring, lead response tracking, and conversation management.
    """
    from .tools.check_replies import run_check_replies

    logger.info("Running check_replies")
    try:
        return await run_traced("check_replies", run_check_replies())
    except Exception as e:
        logger.error(f"check_replies failed: {e}", exc_info=True)
        return f"❌ Reply check failed: {e}"


# ──────────────────────────────────────────────
# Tool 5: show_status
# ──────────────────────────────────────────────

@mcp.tool(structured_output=False, annotations=_reads("Show outreach status"))
async def show_status(
    campaign_id: str = "",
) -> str | list[str | Image]:
    """Show your outreach dashboard — campaigns, stats, hot leads, account health.

    The chat is the front door to your dashboard. View pipeline metrics, prospect
    funnel, engagement rates, and campaign performance. Ask "how's my outreach?" anytime.

    Hosted accounts get a dashboard link and a snapshot card; relay the link,
    because some clients show the image only to the model.

    Args:
        campaign_id: Show stats for a specific campaign. Shows all if empty.
    """
    from .services.dashboard_snapshot import attach_snapshot
    from .tools.show_status import run_show_status

    logger.info("Running show_status")
    try:
        return await run_traced(
            "show_status", attach_snapshot(run_show_status(campaign_id)),
            campaign_id=campaign_id,
        )
    except Exception as e:
        logger.error(f"show_status failed: {e}", exc_info=True)
        return f"❌ Status check failed: {e}"


# ──────────────────────────────────────────────
# Tool: campaign (consolidated lifecycle)
# ──────────────────────────────────────────────

@mcp.tool(structured_output=False, annotations=_acts("Launch, pause, archive or delete a campaign"))
async def campaign(
    action: str,
    campaign_id: str = "",
    confirm: bool = False,
) -> str | list[str | Image]:
    """Control campaign lifecycle — launch, monitor, pause, resume, archive, delete, emergency stop, or retry failed.

    Hosted accounts get a dashboard link and a snapshot card; relay the link,
    because some clients show the image only to the model.

    Args:
        action: What to do:
            "launch"         — Start outreach for a draft campaign (create_campaign
                               leaves it as a draft; nothing sends until this runs).
                               On a hosted account this also commissions the cloud
                               scheduler, so the campaign keeps sending with the
                               laptop closed
            "monitor"        — Activate a campaign for signal collection only. Sends
                               nothing; requires scheduler(action='observe')
            "pause"          — Pause an active campaign
            "resume"         — Resume a paused campaign
            "archive"        — Archive a completed campaign
            "delete"         — Permanently delete a campaign (requires confirm=True)
            "emergency_stop" — Immediately pause ALL active campaigns (kill switch)
            "retry_failed"   — Reset error outreaches to pending
            "repair_queue"   — Drop never-contacted rows below min_fit_score
            "status_history" — View campaign status change audit log (who stopped/started and when)
            "clear_coordinator_hold" — Release a campaign-wide coordinator hold
        campaign_id: Which campaign to act on. Auto-selects if empty.
        confirm: Must be True for delete action. Safety guard.
    """
    from .services.dashboard_snapshot import attach_snapshot
    from .tools.campaign import run_campaign
    from .tools.organization import refuse_if_viewer

    # Normalise once, before the gate. run_campaign lowercases and strips too,
    # so comparing the raw string here let action="Launch" and the skip-list
    # pair ("status_history", "monitor") disagree with the real dispatcher.
    action = (action or "").lower().strip()

    if action not in ("status_history", "monitor"):
        blocked = await refuse_if_viewer()
        if blocked:
            return blocked

    logger.info(f"Running campaign: action={action}, campaign_id={campaign_id}")
    try:
        return await run_traced(
            "campaign",
            attach_snapshot(run_campaign(action, campaign_id, confirm)),
            action=action,
            campaign_id=campaign_id,
        )
    except Exception as e:
        logger.error(f"campaign failed: {e}", exc_info=True)
        return f"Campaign action failed: {e}"


# ──────────────────────────────────────────────
# Tool: send_message (consolidated)
# ──────────────────────────────────────────────

@mcp.tool(annotations=_acts("Send a LinkedIn follow-up, reply, voice memo or InMail"))
async def send_message(
    action: str = "followup",
    campaign_id: str = "",
    outreach_id: str = "",
    format: str = "text",
    text: str = "",
) -> str:
    """Send follow-ups, replies, voice memos, or InMail to prospects.

    Args:
        action: What to do:
            "followup" — Send a follow-up DM after connection accepted
            "reply"    — Reply to a prospect who has messaged you
            "voice"    — Send a voice memo on LinkedIn
            "delete"   — Delete a recently sent message (within 60 min on LinkedIn)
            "inmail"   — Send an InMail to a NON-connection. Preconditions
                         (each fails closed with no send): prospect has a
                         provider_id; they are NOT a 1st-degree connection
                         (use followup/DM for those); no InMail already sent
                         on this outreach; InMail credits remaining > 0.
                         A pending invitation to the same person is allowed
                         — that is the escalation path. Requires outreach_id.
        campaign_id: Which campaign to send from. Uses active if empty.
        outreach_id: Specific outreach to target. Required for inmail.
        format: 'text' (default) or 'voice' (audio via Hume TTS). For followup/reply.
        text: Custom text for voice memo. Auto-generates if empty.
            For delete: optionally pass a Unipile message_id directly.
    """
    from .tools.send_message import run_send_message
    from .tools.organization import refuse_if_viewer

    blocked = await refuse_if_viewer()
    if blocked:
        return blocked

    logger.info(f"Running send_message: action={action}, campaign={campaign_id}, outreach={outreach_id}")
    try:
        return await run_traced(
            "send_message",
            run_send_message(action, campaign_id, outreach_id, format, text),
            action=action,
            campaign_id=campaign_id,
            outreach_id=outreach_id,
        )
    except Exception as e:
        logger.error(f"send_message failed: {e}", exc_info=True)
        return f"Message send failed: {e}"


# ──────────────────────────────────────────────
# Tool: send_email (Unipile mailbox — never Mail.app)
# ──────────────────────────────────────────────

@mcp.tool(annotations=_acts("Send an email from the connected mailbox"))
async def send_email(
    to: str,
    subject: str,
    body: str,
    to_name: str = "",
) -> str:
    """Send an email through the connected Unipile mailbox (Gmail/Outlook).

    This is the only supported way to send email. Never use Mail.app, osascript,
    mailto: handlers, or a local SMTP client.

    If no mailbox is connected, the result includes a hosted-auth link.
    Have the user open it, then retry. Or call account(action='connect_email')
    first.

    Args:
        to: Recipient email address.
        subject: Subject line.
        body: Body text (HTML is fine).
        to_name: Optional display name for the recipient.
    """
    from .tools.send_email import run_send_email

    logger.info("Running send_email: to=%s", to)
    try:
        return await run_traced("send_email", run_send_email(to, subject, body, to_name))
    except Exception as e:
        logger.error("send_email failed: %s", e, exc_info=True)
        return f"Email send failed: {e}"


# ──────────────────────────────────────────────
# Tool 9: engage_prospect
# ──────────────────────────────────────────────

@mcp.tool(annotations=_acts("Comment, react, follow or endorse on LinkedIn"))
async def engage_prospect(
    campaign_id: str = "",
    outreach_id: str = "",
    action: str = "auto",
) -> str:
    """Comment on, react to, follow, or endorse a prospect on LinkedIn to build trust.

    Finds a prospect's recent posts, generates a voice-matched comment
    (or reacts with a Like), and sends it. Use action="follow" to follow
    a prospect's profile — this triggers a "X started following you"
    notification and warms them up before connecting. Use action="endorse"
    to endorse their skills — triggers a high-visibility notification.
    Great for social selling, warm-up engagement, and building familiarity
    before cold outreach.

    Args:
        campaign_id: Which campaign to engage from. Uses active campaign if empty.
        outreach_id: Specific outreach to engage with. Auto-picks next if empty.
        action: "auto" (comment if post has text, react otherwise),
            "comment" (always comment), "react" or "like" (just like the post),
            "view" (view their LinkedIn profile — lightest warm-up signal),
            "follow" (follow their LinkedIn profile as a warm-up signal),
            "endorse" (endorse their skills — highest visibility warm-up),
            "reply_comment" (reply to prospect's response on your comment thread).
    """
    from .tools.engage_prospect import run_engage_prospect
    from .tools.organization import refuse_if_viewer

    blocked = await refuse_if_viewer()
    if blocked:
        return blocked

    logger.info(f"Running engage_prospect: campaign={campaign_id}, outreach={outreach_id}, action={action}")
    try:
        return await run_traced(
            "engage_prospect",
            run_engage_prospect(campaign_id, outreach_id, action),
            campaign_id=campaign_id,
            outreach_id=outreach_id,
            action=action,
        )
    except Exception as e:
        logger.error(f"engage_prospect failed: {e}", exc_info=True)
        return f"Engagement failed: {e}"


# ──────────────────────────────────────────────
# Tool 10: suggest_next_action (approve_outreach removed — copilot mode removed)
# ──────────────────────────────────────────────

@mcp.tool(structured_output=False, annotations=_reads("Suggest the next outreach action"))
async def suggest_next_action(
    campaign_id: str = "",
) -> str | list[str | Image]:
    """Suggest the best next action for your outreach.

    Hosted accounts get a dashboard link and a snapshot card; relay the link,
    because some clients show the image only to the model.

    Analyzes all active campaigns and recommends what to do next,
    prioritized by impact: hot leads first, then pending approvals,
    follow-ups, engagement warm-ups, and new invitations.

    Args:
        campaign_id: Focus on a specific campaign. Analyzes all active if empty.
    """
    from .services.dashboard_snapshot import attach_snapshot
    from .tools.suggest_next_action import run_suggest_next_action

    logger.info(f"Running suggest_next_action: campaign_id={campaign_id}")
    try:
        return await run_traced(
            "suggest_next_action",
            attach_snapshot(run_suggest_next_action(campaign_id)),
            campaign_id=campaign_id,
        )
    except Exception as e:
        logger.error(f"suggest_next_action failed: {e}", exc_info=True)
        return f"Failed to suggest next action: {e}"






# ──────────────────────────────────────────────
# Tool 19: edit_campaign
# ──────────────────────────────────────────────

@mcp.tool(annotations=_acts("Edit campaign settings"))
async def edit_campaign(
    campaign_id: str = "",
    name: str = "",
    mode: str = "",
    booking_link: str = "",
    offerings: str = "",
    case_studies: str = "",
    social_proofs: str = "",
    campaign_preferences: str = "",
    campaign_intent: str = "",
    campaign_type: str = "",
    project_brief: str = "",
    product: str = "",
    go_live: str = "",
    volume: str = "",
    must_confirm: str = "",
    voice_mode: str = "",
    voice_noise: str = "",
    voice_humanize: str = "",
    enable_profile_views: str = "",
    enable_follows: str = "",
    enable_endorsements: str = "",
    enable_engagements: str = "",
    enable_followups: str = "",
    enable_auto_replies: str = "",
    enable_invitations: str = "",
    enable_discovery: str = "",
    exclude_connections: str = "",
    connections_only: str = "",
    exclude_competitors: str = "",
    competitor_companies: str = "",
    enable_reply_agent: str = "",
    enable_strategist_replan_agent: str = "",
    enable_hot_lead_closer: str = "",
    enable_coordinator_agent: str = "",
    engagement_mode: str = "",
    max_followups: int = 0,
    weekly_meeting_target: int = -1,
    followup_delay_days: str = "",
    withdraw_stale_invites: str = "",
    stale_invite_days: int = 0,
    inmail_fallback: str = "",
    inmail_fallback_days: int = 0,
    inmail_first_touch: str = "",
    send_in_business_hours: str = "",
    active_days: str = "",
) -> str:
    """Edit a campaign's name, mode, booking link, or context fields.

    Change the campaign name or configure campaign settings
    modes, set a booking link, or configure campaign context for
    better message personalization.

    Args:
        campaign_id: Which campaign to edit. Edits the first active campaign if empty.
        name: New campaign name. Leave empty to keep current name.
        mode: Only "autopilot" supported. Copilot mode removed.
        booking_link: Calendar/booking URL (e.g., "https://cal.com/you/15min").
            Used in reply_to_prospect() for positive replies to suggest meetings.
        offerings: What you offer (products, services, value props). Used in follow-up messages.
        case_studies: Brief case studies or success stories. Used for social proof in messages.
        social_proofs: Social proof (logos, metrics, testimonials). Used in follow-up messages.
        campaign_preferences: Custom messaging preferences (tone, topics to avoid, etc.).
        campaign_intent: Message stance: "sell", "buy", "partner", or "recruit".
        campaign_type: Prompt family: "outbound" (default) or "job_search".
            job_search replaces the intent-specific invitation note and first
            DM with ones that may name the company and the role, use one
            credible proof point at most and never list a CV. InMail is not
            routed by this switch, and campaign_intent still selects the
            system prompt. Empty keeps the current value.
        project_brief: Full project paste the model sees. Required before launch,
            resume, or auto-send.
        product: Optional structured fact: product / what you buy or sell.
        go_live: Optional structured fact: go-live date.
        volume: Optional structured fact: volume model.
        must_confirm: Optional comma-separated questions a vendor must confirm.
        voice_mode: Voice memo mode: "text_only", "voice_only", "mixed", or "ab_test".
            Leave empty to keep current value.
        voice_noise: Ambient noise type for voice memos: "office", "cafe", "street",
            "quiet", "none", "auto". Leave empty to keep current value.
        voice_humanize: Voice text humanization: "on" or "off".
            Leave empty to keep current value.
        enable_profile_views: View prospect profiles before following: "on" or "off".
        enable_follows: Follow prospects before inviting: "on" or "off".
        enable_endorsements: Endorse skills before inviting: "on" or "off".
        enable_engagements: Comment/react on posts before inviting: "on" or "off".
        enable_followups: Send follow-up DMs after connection: "on" or "off".
        enable_auto_replies: Auto-reply to prospect messages: "on" or "off".
        enable_invitations: Send connection invitations: "on" or "off".
            When off, campaign only DMs existing connections (no invitations sent).
        enable_discovery: Auto-find and enrol new prospects: "on" or "off".
        exclude_connections: "on" to never message anyone who was already a
            1st-degree connection before this campaign started — they are
            refused at enrolment and skipped at send time instead of being
            DMed. People who accept this campaign's own invitation still get
            the opener. Turning it on turns connections_only off.
        connections_only: "on" to target only your existing 1st-degree
            connections (DM-only, no invitations). Turning it on turns
            exclude_connections off.
            Turn off for curated campaigns with a fixed, hand-picked list.
        exclude_competitors: "on" to never first-touch people who work at
            competing companies. Default on. Empty list excludes nobody
            until research or competitor_companies names them.
        competitor_companies: Comma-separated employer names to skip.
        enable_reply_agent: Reply exception agent: "on" (act), "off", or
            "observe". Empty keeps the current value. Unset defaults to act.
        enable_strategist_replan_agent: Strategist replan: "on", "off", or
            "observe". Empty keeps the current value.
        enable_hot_lead_closer: Hot-lead closer: "on", "off", or "observe".
            Empty keeps the current value.
        enable_coordinator_agent: Coordinator digest/hold: "on", "off", or
            "observe". Empty keeps the current value.
        engagement_mode: Engagement style: "auto" (30% react / 70% comment),
            "comment_only", or "react_only".
        max_followups: Max follow-up messages (1-5). 0 to keep current.
        weekly_meeting_target: Meetings this campaign should book per week.
            The daily report reads it as the Key Result and says whether the
            campaign is on track. 0 means no goal this week; -1 keeps current.
        followup_delay_days: Custom day intervals as comma-separated list
            (e.g., "1,3,7,14"). Leave empty to keep current.
        withdraw_stale_invites: Auto-withdraw stale invites: "on" or "off".
        stale_invite_days: Days before withdrawing stale invites (7-60). 0 to keep current.
        inmail_fallback: Escalate quiet invitations with one InMail: "on" or "off".
            Free tier sends only to Open Profile members (zero credits).
        inmail_fallback_days: Quiet days before the InMail (1-60). 0 to keep current.
        inmail_first_touch: InMail as first touch: "on" or "off". Unset follows inmail_fallback.
        send_in_business_hours: Send only in business hours: "on" or "off". On by default
            (weekdays 08:00-22:00 in your own timezone, or London when it is unknown,
            unless the workspace set its own window).
        active_days: Active send days as comma-separated numbers (0=Mon, 6=Sun).
            E.g., "0,1,2,3,4" for weekdays. Leave empty to keep current.
    """
    from .tools.edit_campaign import run_edit_campaign
    from .tools.organization import refuse_if_viewer

    blocked = await refuse_if_viewer()
    if blocked:
        return blocked

    logger.info(f"Running edit_campaign: campaign={campaign_id}, name={name}, mode={mode}")
    try:
        return await run_traced(
            "edit_campaign",
            run_edit_campaign(
                campaign_id, name, mode, booking_link,
                offerings, case_studies, social_proofs, campaign_preferences,
                project_brief=project_brief,
                product=product,
                go_live=go_live,
                volume=volume,
                must_confirm=must_confirm,
                voice_mode=voice_mode,
                voice_noise=voice_noise,
                voice_humanize=voice_humanize,
                enable_profile_views=enable_profile_views,
                enable_follows=enable_follows,
                enable_endorsements=enable_endorsements,
                enable_engagements=enable_engagements,
                enable_followups=enable_followups,
                enable_auto_replies=enable_auto_replies,
                enable_invitations=enable_invitations,
                enable_discovery=enable_discovery,
                exclude_connections=exclude_connections,
                connections_only=connections_only,
                exclude_competitors=exclude_competitors,
                competitor_companies=competitor_companies,
                enable_reply_agent=enable_reply_agent,
                enable_strategist_replan_agent=enable_strategist_replan_agent,
                enable_hot_lead_closer=enable_hot_lead_closer,
                enable_coordinator_agent=enable_coordinator_agent,
                engagement_mode=engagement_mode,
                max_followups=max_followups,
                weekly_meeting_target=weekly_meeting_target,
                followup_delay_days=followup_delay_days,
                withdraw_stale_invites=withdraw_stale_invites,
                stale_invite_days=stale_invite_days,
                inmail_fallback=inmail_fallback,
                inmail_fallback_days=inmail_fallback_days,
                inmail_first_touch=inmail_first_touch,
                send_in_business_hours=send_in_business_hours,
                active_days=active_days,
                campaign_intent=campaign_intent,
                campaign_type=campaign_type,
            ),
            campaign_id=campaign_id,
        )
    except Exception as e:
        logger.error(f"edit_campaign failed: {e}", exc_info=True)
        return f"Edit failed: {e}"


# ──────────────────────────────────────────────
# Tool: prospect (consolidated)
# ──────────────────────────────────────────────

@mcp.tool(annotations=_acts("Skip, close or review a prospect"))
async def prospect(
    action: str,
    outreach_id: str = "",
    campaign_id: str = "",
    outcome: str = "won",
    reason: str = "",
    meeting_link: str = "",
    confirm: bool = False,
    reason_code: str = "",
    reason_note: str = "",
) -> str:
    """Manage prospects — skip, close, dismiss, view conversation, or timeline.

    Skip and Stop are different decisions:
      Skip — leave this person out of THIS campaign. No other effect.
      Stop — close(outcome='opt_out'): stop all outreach to this person
             across the workspace, and say why. That feedback improves
             targeting.

    Args:
        action: What to do:
            "skip"         — Leave the prospect out of this campaign only
            "close"        — Record outcome (won/lost/opt_out) for an outreach
            "dismiss"      — Clear a lead off the Needs attention strip.
                             Closes it as lost, so it also leaves the Hot
                             Leads count. Needs confirm=True; the first call
                             previews who would be dismissed.
            "conversation" — View the full message thread with a prospect
            "timeline"     — View chronological journey of all actions for a prospect
        outreach_id: The outreach ID. Auto-selects if empty (except 'conversation'/'timeline').
        campaign_id: Which campaign (for 'skip'). Uses active if empty.
        outcome: 'won', 'lost', or 'opt_out' (for 'close').
        reason: Optional notes for the outcome (for 'close').
        meeting_link: Meeting/calendar URL if outcome is 'won' (for 'close').
            This is the URL where the meeting was booked — could be
            the user's booking page or the prospect's shared calendar.
        confirm: Must be True to actually dismiss (for 'dismiss').
        reason_code: Why, for 'skip' and 'close'. One of 'not_a_fit',
            'negative_reply', 'asked_to_stop', 'handled_elsewhere', 'other'.
            Only the first two are evidence about targeting; the rest are
            facts about that one person and never move a segment's ranking.
        reason_note: Free text alongside the code.
    """
    from .tools.prospect import run_prospect

    logger.info(f"Running prospect: action={action}, outreach={outreach_id}")
    try:
        return await run_traced(
            "prospect",
            run_prospect(
                action, outreach_id, campaign_id, outcome, reason,
                meeting_link, confirm, reason_code, reason_note,
            ),
            action=action,
            outreach_id=outreach_id,
            campaign_id=campaign_id,
        )
    except Exception as e:
        logger.error(f"prospect failed: {e}", exc_info=True)
        return f"Prospect action failed: {e}"


# ──────────────────────────────────────────────
# Tool: analytics (consolidated)
# ──────────────────────────────────────────────

@mcp.tool(structured_output=False, annotations=_reads("Campaign analytics and exports"))
async def analytics(
    action: str = "report",
    campaign_id: str = "",
    campaign_ids: str = "",
    format: str = "table",
) -> str | list[str | Image]:
    """Campaign analytics — reports, comparisons, and exports.

    Hosted accounts get a dashboard link and a snapshot card; relay the link,
    because some clients show the image only to the model.

    Args:
        action: What to do:
            "report"  — Detailed analytics with outcomes, conversion rates, stale leads
            "compare" — Compare 2+ campaigns side by side
            "export"  — Export campaign results as table, CSV, or JSON
        campaign_id: Which campaign. Uses active if empty.
        campaign_ids: Comma-separated IDs (for 'compare'). Compares all if empty.
        format: Output format for 'export': 'table', 'csv', or 'json'.
    """
    from .services.dashboard_snapshot import attach_snapshot
    from .tools.analytics import run_analytics

    logger.info(f"Running analytics: action={action}, campaign={campaign_id}")
    try:
        return await run_traced(
            "analytics",
            attach_snapshot(run_analytics(action, campaign_id, campaign_ids, format)),
            action=action,
            campaign_id=campaign_id,
        )
    except Exception as e:
        logger.error(f"analytics failed: {e}", exc_info=True)
        return f"Analytics failed: {e}"


# ──────────────────────────────────────────────
# Tool: inspect (read-only agent ops)
# ──────────────────────────────────────────────

@mcp.tool(annotations=_reads("Inspect what the outreach agents decided"))
async def inspect(
    action: str = "agents",
    campaign_id: str = "",
    outreach_id: str = "",
    limit: int = 20,
) -> str:
    """Read-only digest of what the in-process agents decided — never writes.

    Surfaces operator holds, today's strategist replans, hot-lead closer
    decisions, recent reply skips, and gated scheduler jobs from the local
    log. Use it when someone asks what the agents did, who is held, why a
    reply was skipped, or why a campaign is not sending.
    It does not send, book, replan, or change outreach state.

    Args:
        action: What to show:
            "agents"  — one-screen digest of holds, replans, closer, skips, jobs (default)
            "holds"   — fresh hold_for_operator rows and coordinator campaign holds
            "replans" — today's strategist_replan_decision rows
            "closer"  — today's hot_lead_closer_decision rows
            "skips"   — recent reply skips (hard gates, cap, dedup, agent skip)
            "jobs"    — pending scheduler jobs and recent gated-job refusals
            "commons" — digest, beats (including product), live notes, coordinator hold, stale liveness
            "journal" — hosted agent diary (cloud workers). Self-hosted: use the other actions.
        campaign_id: Optional campaign filter (full id or prefix).
        outreach_id: Optional outreach filter (full id or prefix).
        limit: Max rows per slice, 1-100 (default 20).
    """
    from .tools.inspect import run_inspect

    logger.info(
        "Running inspect: action=%s campaign=%s outreach=%s",
        action, campaign_id, outreach_id,
    )
    try:
        return await run_traced(
            "inspect",
            run_inspect(
                action=action,
                campaign_id=campaign_id,
                outreach_id=outreach_id,
                limit=limit,
            ),
            action=action,
            campaign_id=campaign_id,
            outreach_id=outreach_id,
        )
    except Exception as e:
        logger.error("inspect failed: %s", e, exc_info=True)
        return f"Inspect failed: {e}"


# ──────────────────────────────────────────────
# Tool: knowledge (hosted retrieval corpus)
# ──────────────────────────────────────────────

@mcp.tool(annotations=_acts("Curate the knowledge base behind messages"))
async def knowledge(
    action: str = "list",
    title: str = "",
    text: str = "",
    source_uri: str = "",
    source_id: str = "",
    scope: str = "all",
    campaign_id: str = "",
    query: str = "",
    kinds: str = "",
    top_k: int = 6,
    sync: bool = False,
) -> str:
    """Curate the knowledge base that grounds generated messages. Hosted only.

    Four kinds of source live in it: "upload" (documents added here),
    "website" (crawled pages from your own site), "campaign" (campaign
    context and offerings) and "reply_exemplar" (replies that worked).
    Message generation quotes them, so what is in here decides what the
    agent may claim.

    Args:
        action: What to do:
            "list"    — Show every source with kind, chunk count, and embed status
            "add"     — Upload one document (needs title and text)
            "remove"  — Delete one source (needs source_id)
            "refresh" — Re-ingest the derived corpus (website, campaigns, exemplars)
            "search"  — Retrieve grounded evidence for a query
        title: Document title, for 'add'.
        text: Document body, for 'add'. Required.
        source_uri: Where the document came from, for 'add'. Optional.
        source_id: Which source to delete, for 'remove'. From 'list'.
        scope: What to re-ingest, for 'refresh': "all" (default), "website",
            "campaigns", or "exemplars".
        campaign_id: Restrict 'refresh' or 'search' to one campaign.
        query: What to retrieve, for 'search'. Required.
        kinds: Comma-separated source kinds — "upload,website,campaign,
            reply_exemplar". Filters 'search'; 'list' uses the first one.
        top_k: Max evidence chunks for 'search' (default 6, clamped to 1-50).
        sync: For 'refresh'. False (default) queues a background job and
            returns immediately — re-run knowledge(action='list') in a
            minute to watch the chunk counts land. True blocks until the
            re-ingest finishes and reports a summary; it can take minutes,
            and the backend only allows it for scope "campaigns" or
            "exemplars" (scope "all" is refused with the reason).
    """
    from .tools.knowledge import run_knowledge
    from .tools.organization import refuse_if_viewer

    # Normalise once, before the gate. run_knowledge lowercases and strips too,
    # so comparing the raw string here let action="Add" past the viewer check
    # and then become a real upload inside the tool.
    action = (action or "list").lower().strip()

    if action in ("add", "remove", "refresh"):
        blocked = await refuse_if_viewer()
        if blocked:
            return blocked

    logger.info("Running knowledge: action=%s scope=%s", action, scope)
    try:
        return await run_traced(
            "knowledge",
            run_knowledge(
                action=action,
                title=title,
                text=text,
                source_uri=source_uri,
                source_id=source_id,
                scope=scope,
                campaign_id=campaign_id,
                query=query,
                kinds=kinds,
                top_k=top_k,
                sync=sync,
            ),
            action=action,
            campaign_id=campaign_id,
        )
    except Exception as e:
        logger.error("knowledge failed: %s", e, exc_info=True)
        return f"Knowledge failed: {e}"


# ──────────────────────────────────────────────
# Tool: product (local-only code / product agent)
# ──────────────────────────────────────────────

@mcp.tool(annotations=_acts("Patch the local HeyLead checkout (developers)"))
async def product(action: str = "status", request: str = "") -> str:
    """Local-only product agent — patch this HeyLead git checkout or open a PR.

    Does not send LinkedIn, email, or calendar. Cloud workers and installs
    without a HeyLead .git checkout refuse. The coordinator never starts this
    loop; call it explicitly.

    Args:
        action: "status" (default) or "tick".
        request: What to change. Required for action='tick'.
    """
    from .tools.product import run_product

    logger.info("Running product: action=%s", action)
    try:
        return await run_traced(
            "product",
            run_product(action=action, request=request),
            action=action,
        )
    except Exception as e:
        logger.error("product failed: %s", e, exc_info=True)
        return f"Product failed: {e}"


# ──────────────────────────────────────────────
# Tool: scheduler (consolidated)
# ──────────────────────────────────────────────

@mcp.tool(annotations=_acts("Control the autonomous scheduler"))
async def scheduler(
    action: str = "status",
    enabled: bool = True,
    cloud: bool = False,
    hours: int | None = None,
    event_type: str = "",
    campaign_id: str = "",
    host: str = "",
) -> str:
    """Manage the autonomous scheduler — view status or toggle on/off.

    Args:
        action: What to do:
            "status" — Show scheduler status, pending jobs, and recent activity
            "toggle" — Enable or disable the scheduler
            "observe" — Collect and classify signals, and check replies, while this
                machine sends nothing: no invitations, DMs, engagements, or
                enrolments. Local only — it does not stop 24/7 cloud scheduling,
                which must be disabled separately with
                scheduler(action='toggle', enabled=False, cloud=True)
            "always_on" — Enable/disable always-on mode (auto-re-enable + immediate email alerts)
            "logs" — Event log with job metrics and recent failures
            "activity" — Real action results from DB: what happened, what didn't, and why
            "diagnostics" — Full system diagnostics: rate limits, timers, blockers
            "report" — Configure periodic email campaign reports
            "backfill_cloud" — One-shot push of ALL local history (every campaign,
                any mode/status) to the hosted dashboard (backend mode only)
            "send_from" — Move all campaign outbound to the cloud or this machine.
                Pass host="cloud" (default for hosted accounts) or host="local".
                Local turns the cloud scheduler off so both never send.
        enabled: True to enable, False to disable (for 'toggle').
            For 'report': True to enable email reports, False to disable.
        cloud: If True, toggle the cloud scheduler for 24/7 operation (for 'toggle').
            Launching or resuming a campaign already switches it on for hosted
            accounts; pass cloud=True, enabled=False to stop the backend sending
            while leaving this machine's scheduler alone.
        hours: Lookback window in hours for 'logs' and 'activity' (default 24).
            For 'report': report interval in hours (1, 2, 4, 8, or 24).
            Omitted, 'report' leaves the stored interval unchanged.
        event_type: Filter events by type for 'logs'.
            For 'report': recipient email (empty = use login email).
        campaign_id: Filter by campaign for 'logs', 'activity', and 'diagnostics'.
        host: For 'send_from': "cloud" or "local".
    """
    from .tools.scheduler import run_scheduler
    from .tools.organization import refuse_if_viewer

    # Normalise once, before the gate. run_scheduler lowercases and strips too,
    # so comparing the raw string here let action="Toggle" past the viewer
    # check and then become a real mutation inside the tool.
    action = (action or "status").lower().strip()

    if action in ("toggle", "observe", "always_on", "send_from"):
        blocked = await refuse_if_viewer()
        if blocked:
            return blocked

    logger.info(f"Running scheduler: action={action}")
    try:
        return await run_traced(
            "scheduler",
            run_scheduler(
                action, enabled=enabled, cloud=cloud,
                hours=hours, event_type=event_type, campaign_id=campaign_id,
                host=host,
            ),
            action=action,
            campaign_id=campaign_id,
        )
    except Exception as e:
        logger.error(f"scheduler failed: {e}", exc_info=True)
        return f"Scheduler action failed: {e}"


# ──────────────────────────────────────────────
# Tool 29: create_post
# ──────────────────────────────────────────────

@mcp.tool(annotations=_acts("Publish a post to LinkedIn or X"))
async def create_post(
    topic: str = "",
    tone: str = "professional",
    platforms: str = "linkedin",
    image: str = "",
) -> str:
    """Generate and publish a voice-matched post to LinkedIn, X/Twitter, or both.

    Creates posts using your voice signature for social selling.
    Builds authority and drives inbound connections across platforms.

    Args:
        topic: What to post about (e.g., "share a tip about cold outreach",
            "comment on AI in sales", "share a success story").
        tone: Post tone: "professional", "casual", "thought-leader", "storytelling".
        platforms: Comma-separated platforms: "linkedin", "x", or "linkedin,x".
        image: Path to a photo to attach. LinkedIn only — a tweet is posted
            without it. png, jpg, gif or webp, up to 10MB.
        mode: "autopilot" (publishes immediately).
    """
    from .tools.create_post import run_create_post
    from .tools.organization import refuse_if_viewer

    blocked = await refuse_if_viewer()
    if blocked:
        return blocked

    logger.info(f"Running create_post: topic={topic}, tone={tone}, platforms={platforms}")
    try:
        return await run_traced(
            "create_post", run_create_post(topic, tone, platforms, image),
        )
    except Exception as e:
        logger.error(f"create_post failed: {e}", exc_info=True)
        return f"Post creation failed: {e}"


# ──────────────────────────────────────────────
# Tool 30: brand_strategy
# ──────────────────────────────────────────────

@mcp.tool(annotations=_acts("Plan and run LinkedIn personal-brand content"))
async def brand_strategy(
    action: str = "analyze",
    focus: str = "",
    photo: str = "",
) -> str:
    """Analyze and improve your LinkedIn personal brand to drive more leads.

    Audits your profile, generates a personal brand strategy, executes
    actions (post topics, headline rewrites, engagement targets), and
    tracks improvement over time.

    Args:
        action: What to do:
            "analyze" — Full profile audit with scored areas and issues
            "plan"    — Generate a 4-week brand strategy with content calendar
            "execute" — Execute the next recommended action from your plan
            "progress" — Show before/after metrics and completed actions
            "upload_photo" — Upload a profile photo (provide file_path or base64 data)
            "upload_cover" — Upload a cover/banner photo (same input as upload_photo)
            "set_link" — Set custom CTA link on profile (pass URL via focus param)
            "set_headline" — Set the headline to exact text (pass the headline via focus)
            "set_summary" — Set the About section to exact text (pass the text via focus).
                Use these two when the user has already decided the wording; "execute"
                and "makeover" write model-generated copy instead.
            "set_photo_library" — Folder of the user's own photos that brand-calendar
                posts may attach (pass the folder path via focus; "off" clears it).
                Files named "NNN - what it shows.jpeg"; personal or family subfolders
                are never used. Local posting only: a cloud-owned seat posts text only.
        focus: Focus area for analyze/plan ("headline", "summary", "content", "engagement", ""),
            URL string for set_link, the literal text for set_headline / set_summary,
            or the folder path for set_photo_library.
        photo: File path or base64-encoded image for upload_photo / upload_cover actions.
    """
    from .tools.brand_strategy import run_brand_strategy

    logger.info("Running brand_strategy: action=%s, focus=%s", action, focus)
    try:
        return await run_traced(
            "brand_strategy", run_brand_strategy(action, focus, photo), action=action,
        )
    except Exception as e:
        logger.error("brand_strategy failed: %s", e, exc_info=True)
        return f"Brand strategy failed: {e}"


# ──────────────────────────────────────────────
# Tool 31: profile (change history + restore)
# ──────────────────────────────────────────────

@mcp.tool(annotations=_acts("View or restore LinkedIn profile changes"))
async def profile(
    action: str = "history",
    field: str = "",
    change_id: str = "",
    limit: int = 20,
) -> str:
    """View and restore LinkedIn profile change history.

    Every profile edit is tracked so you can see what changed and roll back
    if needed.  Supported fields: headline, summary, photo, cover_photo,
    custom_link, location, skills, experience.

    Args:
        action: What to do:
            "history"  — List recent profile changes (default)
            "restore"  — Revert a specific change by its ID
            "current"  — Show current cached profile snapshot
        field: Filter history by field name (e.g. "headline", "summary", "photo",
            "cover_photo", "custom_link", "location", "skills", "experience"). Optional.
        change_id: The change ID to restore (required for "restore" action).
        limit: Max number of history entries to show (default 20).
    """
    from .tools.profile_history import run_profile

    logger.info("Running profile: action=%s, field=%s, change_id=%s", action, field, change_id)
    try:
        return await run_traced(
            "profile", run_profile(action, field, change_id, limit), action=action,
        )
    except Exception as e:
        logger.error("profile failed: %s", e, exc_info=True)
        return f"Profile action failed: {e}"


# ──────────────────────────────────────────────
# Tool 32: import_prospects
# ──────────────────────────────────────────────

@mcp.tool(annotations=_acts("Import prospects from CSV or XLSX"))
async def import_prospects(
    campaign_id: str = "",
    csv_data: str = "",
    linkedin_enrich: bool = False,
    file_path: str = "",
    sheet: str = "",
    dry_run: bool = False,
) -> str:
    """Import prospects from a CSV/XLSX file into a campaign.

    Point HeyLead at a .csv or .xlsx file (or paste CSV text) and it will parse
    it, deduplicate against existing contacts and LinkedIn connections, score
    each prospect, and add them to the campaign for outreach. Every row of the
    file gets a disposition — imported, skipped:<reason>, or deduped-against a
    specific earlier row — and the totals are reconciled against the file's row
    count, so a partial import can never be reported as a success.

    Supports CSV import, XLSX/spreadsheet import, bulk prospect upload, lead
    list import, and contact list management for LinkedIn outreach campaigns.

    Args:
        campaign_id: Campaign to import into. Leave empty for the most recent.
        csv_data: CSV text with headers. Only use for a handful of rows —
            prefer file_path, which has no size limit. Ignored if file_path
            is given.
        linkedin_enrich: If true, fetch full LinkedIn profiles for imported
            prospects (slower but better personalization). Default: false.
        file_path: Path to a .csv or .xlsx file on disk. Preferred over
            csv_data — a large lead list must never be pasted through this
            argument, since anything that does not fit is silently lost.
        sheet: Worksheet name for .xlsx files. Defaults to the first sheet.
        dry_run: If true, report the full per-row disposition without creating
            any contacts or outreaches and without fetching any LinkedIn
            profiles — linkedin_enrich is not run. Your own connection list is
            still read, so the preview matches the real import. Default: false.

    Columns are auto-detected (case-insensitive): Name, Title, Company,
    LinkedIn URL, Email, Location. Each row needs Name + at least one of
    Title, Company, or LinkedIn URL.
    """
    from .tools.import_prospects import run_import_prospects
    from .tools.organization import refuse_if_viewer

    blocked = await refuse_if_viewer()
    if blocked:
        return blocked

    logger.info(
        "Running import_prospects: file=%r sheet=%r dry_run=%s %d chars CSV",
        file_path, sheet, dry_run, len(csv_data),
    )
    try:
        return await run_traced(
            "import_prospects",
            run_import_prospects(
                campaign_id, csv_data, linkedin_enrich, file_path, sheet, dry_run
            ),
        )
    except Exception as e:
        logger.error("import_prospects failed: %s", e, exc_info=True)
        return f"Import failed: {e}"


# ──────────────────────────────────────────────
# Tool 32: crm_sync
# ──────────────────────────────────────────────

@mcp.tool(annotations=_acts("Sync contacts and deals to HubSpot"))
async def crm_sync(
    campaign_id: str = "",
    filter: str = "won",
    hubspot_api_key: str = "",
) -> str:
    """Sync campaign contacts and deals to HubSpot CRM.

    Pushes won deals, hot leads, or all contacts to HubSpot as contacts + deals.
    Tracks sync status to avoid duplicates. Includes conversation history as notes.
    Supports CRM integration, deal pipeline sync, and lead handoff to sales teams.

    First-time setup: create a HubSpot Private App with CRM scopes
    (contacts, deals, notes), then pass the access token here.
    The key is saved for future syncs.

    Args:
        campaign_id: Campaign to sync. Uses the most recent if empty.
        filter: Which contacts to sync: "won" (default), "hot_leads", or "all".
        hubspot_api_key: Optional — your HubSpot Private App access token.
            Only needed on first use; saved for future syncs.
    """
    from .tools.crm_sync import run_crm_sync

    logger.info("Running crm_sync: campaign=%s, filter=%s", campaign_id, filter)
    try:
        return await run_traced(
            "crm_sync",
            run_crm_sync(campaign_id, filter, hubspot_api_key),
            campaign_id=campaign_id,
        )
    except Exception as e:
        logger.error("crm_sync failed: %s", e, exc_info=True)
        return f"CRM sync failed: {e}"


# ──────────────────────────────────────────────
# Signal-Based Selling (v1.0)
# ──────────────────────────────────────────────


@mcp.tool(annotations=_acts("Manage signal keyword watchlists"))
async def manage_watchlist(
    action: str = "list",
    name: str = "",
    watch_type: str = "keyword",
    keywords: str = "",
    watchlist_id: str = "",
    campaign_id: str = "",
) -> str:
    """Add, remove, and list signal keyword watchlists.

    Watchlists define keywords that HeyLead monitors on LinkedIn
    to detect buying signals from prospect posts. Watchlists are
    also auto-created when you generate an ICP.

    Args:
        action: What to do: 'list', 'add', 'remove', 'pause', 'resume'.
        name: Watchlist name (for 'add').
        watch_type: 'keyword', 'competitor', 'company', 'person', or 'industry' (for 'add').
        keywords: Comma-separated keywords (for 'add'). E.g., "cold outreach, SDR automation".
        watchlist_id: Watchlist ID (for 'remove', 'pause', 'resume').
        campaign_id: Optional campaign to link the watchlist to.
    """
    from .tools.manage_watchlist import run_manage_watchlist

    logger.info("Running manage_watchlist: action=%s", action)
    try:
        return await run_traced(
            "manage_watchlist",
            run_manage_watchlist(action, name, watch_type, keywords, watchlist_id, campaign_id),
            action=action,
            campaign_id=campaign_id,
        )
    except Exception as e:
        logger.error("manage_watchlist failed: %s", e, exc_info=True)
        return f"Watchlist management failed: {e}"


@mcp.tool(annotations=_acts("View LinkedIn buying signals"))
async def signals(
    action: str = "show",
    campaign_id: str = "",
    signal_type: str = "",
    status: str = "",
    limit: int = 20,
    days: int = 30,
    signal_id: str = "",
    feedback: str = "",
) -> str:
    """View and analyze buying signals from LinkedIn.

    Args:
        action: What to do:
            "show"     — Display detected buying signals (keyword mentions, job changes, etc.)
            "report"   — Signal analytics report with trends and ROI
            "strategy" — Show strategy engine insights, patterns, and autonomous actions
            "feedback" — Mark a signal as 'useful' or 'not_useful' (improves future classification)
            "website_setup" — Set up website visitor tracking (generates JS snippet to embed)
            "website_stats" — View website tracking analytics (visits, companies, high-intent)
            "optimize" — Run signal self-optimization (weights, keywords, warmup, thresholds)
            "optimize_history" — View optimization change log with rollback IDs
            "optimize_rollback" — Rollback a specific optimization change by entry ID
            "optimize_weights" — Show all signal weights (default vs effective overrides)
        campaign_id: Filter by campaign. Shows all if empty.
        signal_type: Filter by signal type, e.g. 'keyword_mention', 'job_change' (for 'show').
            For 'optimize_history': filter by optimization type (weight, keyword_added, warmup, threshold).
        status: Filter by status: 'new', 'classified', 'actioned' (for 'show').
        limit: Max signals to show (for 'show'). Default 20.
        days: Lookback window in days (for 'report'). Default 30.
        signal_id: Signal ID (for 'feedback' action). Entry ID (for 'optimize_rollback').
        feedback: 'useful' or 'not_useful' (for 'feedback' action).
    """
    from .tools.signals import run_signals

    logger.info("Running signals: action=%s", action)
    try:
        return await run_traced(
            "signals",
            run_signals(
                action, campaign_id, signal_type, status, limit, days,
                signal_id=signal_id, feedback=feedback,
            ),
            action=action,
        )
    except Exception as e:
        logger.error("signals failed: %s", e, exc_info=True)
        return f"Signals failed: {e}"


@mcp.tool(annotations=_acts("Track partner, vendor and investor follow-ups"))
async def partner(
    action: str = "list",
    name: str = "",
    company: str = "",
    email: str = "",
    context: str = "",
    next_followup: str = "",
    partner_id: str = "",
    note: str = "",
    days: int = 0,
) -> str:
    """Track follow-ups with business partners, vendors, and investors.

    Manages a CRM-style pipeline for non-prospect relationships (e.g., API
    vendors, investors, co-founders). Auto-sends email reminders when
    follow-ups are due using an escalating cadence (1, 3, 7, 14, 21 days).

    Args:
        action: What to do:
            "add"      — Add a new partner to track (auto-schedules follow-ups)
            "list"     — Show all active partner follow-ups with due dates
            "update"   — Update partner info or add a note
            "complete" — Mark a partner follow-up as done (got what you needed)
            "snooze"   — Push the next follow-up by N days
            "cancel"   — Stop tracking this partner
        name: Partner's name (for 'add'). E.g., "Julien Crépieux".
        company: Company name (for 'add'). E.g., "Unipile".
        email: Partner's email (for 'add'). E.g., "partner@example.com".
        context: What you're following up about (for 'add').
        next_followup: Next follow-up date as YYYY-MM-DD (for 'add'). Defaults to tomorrow.
        partner_id: Partner ID (for 'update', 'complete', 'snooze', 'cancel').
        note: Add a note to the partner record (for 'update').
        days: Number of days to snooze (for 'snooze'). Default 7.
    """
    from .tools.partner_followup import run_partner

    logger.info("Running partner: action=%s", action)
    try:
        return await run_traced(
            "partner",
            run_partner(action, name, company, email, context, next_followup, partner_id, note, days),
            action=action,
        )
    except Exception as e:
        logger.error("partner failed: %s", e, exc_info=True)
        return f"Partner tracking failed: {e}"


@mcp.tool(annotations=_acts("Search and manage contacts"))
async def contacts(
    action: str = "list",
    query: str = "",
    contact_id: str = "",
    lifecycle_stage: str = "",
    tag: str = "",
    note: str = "",
    min_fit_score: float = 0.0,
    limit: int = 25,
    format: str = "table",
    campaign_id: str = "",
    match: str = "name",
    dry_run: bool = True,
    connected_since: str = "",
    connected_before: str = "",
) -> str:
    """Search, browse, and manage your global contact base.

    One master record per person across all campaigns. View full interaction
    history, add tags/notes, track lifecycle stages, and build reusable
    prospect pools for future campaigns. Also search LinkedIn directly
    for people without creating a campaign.

    Args:
        action: What to do:
            "list"    — List contacts with optional filters (default)
            "search"  — Search contacts by name, company, or title
            "view"    — View full cross-campaign history for one contact
            "tag"     — Add a tag (or remove with '-tag_name')
            "note"    — Add a note to a contact
            "stage"   — Update lifecycle stage
            "stats"   — Contact base dashboard stats
            "export"  — Export contacts as table, CSV, or JSON
            "linkedin_search" — Search LinkedIn directly by name/company/title
            "link"    — Resolve a campaign's contact rows against the contact base
                        by name, so rows imported without a LinkedIn id pick one
                        up. Dry run unless dry_run=False.
            "enrich" — Enrich contacts with full LinkedIn profiles + posts
            "my_connections" — Search your 1st-degree LinkedIn connections (locally synced, guaranteed 1st degree)
        query: Search text for 'search', 'linkedin_search', and 'my_connections' actions.
            For 'enrich': search query to find contacts to enrich.
            For 'linkedin_search' the query is passed to LinkedIn as KEYWORDS,
            matched literally — a company name, a job title, a person's name, or a
            combination such as 'Acme Corp CTO' or 'Jane Doe'. A natural-language
            question ('who is the CTO of Acme?') is sent through unchanged and
            usually comes back empty, so prefer keywords. Nothing is filtered out
            locally. An empty result and a failed search are reported in different
            words, so a "no matches" line means LinkedIn really returned nobody
            rather than "the search broke".
            The profile and posts fetches this triggers are paced: they used to go
            out back to back and LinkedIn rate-limited them, so the call now spends
            up to a fixed wall-clock budget waiting between fetches and prints how
            much of it went on waiting. Expect tens of seconds.
        contact_id: Global contact ID for view/tag/note/stage actions.
        lifecycle_stage: Filter by stage (prospect/contacted/connected/engaged/customer/lost)
            or target stage for 'stage' action.
        tag: Tag to add/remove for 'tag' action, or filter for 'list'/'search'.
        note: Note text for 'note' action.
        min_fit_score: Minimum fit score filter (0.0-1.0).
        limit: Max results to return (default 25). For 'linkedin_search' this is
            capped at 25 per call because every result costs a profile fetch and a
            posts fetch; a result list says so when your limit was capped.
        format: Output format for 'export': 'table', 'csv', or 'json'.
        campaign_id: Campaign whose contact rows to resolve, for the 'link' action.
        match: How 'link' pairs campaign rows with contact base records. Only
            'name' is supported (exact, ignoring case and extra spaces).
        dry_run: For 'link' — True (the default) lists every row it would change
            and writes nothing. Pass False to apply.
        connected_since: For 'my_connections' — only people who became a
            1st-degree connection on or after this date (YYYY-MM-DD).
        connected_before: For 'my_connections' — only people who became a
            1st-degree connection before this date (YYYY-MM-DD). Connections
            synced before dates were recorded have no date and match neither
            filter; the result line says how many those are.
    """
    from .tools.contacts import run_contacts

    logger.info("Running contacts: action=%s", action)
    try:
        return await run_traced(
            "contacts",
            run_contacts(
                action=action, query=query, contact_id=contact_id,
                lifecycle_stage=lifecycle_stage, tag=tag, note=note,
                min_fit_score=min_fit_score, limit=limit, format=format,
                campaign_id=campaign_id, match=match, dry_run=dry_run,
                connected_since=connected_since,
                connected_before=connected_before,
            ),
            action=action,
        )
    except Exception as e:
        logger.error("contacts failed: %s", e, exc_info=True)
        return f"Contact management failed: {e}"


@mcp.tool(annotations=_acts("Search the shared network pool"))
async def network(
    action: str = "status",
    linkedin_id: str = "",
    linkedin_ids: str = "",
    query: str = "",
    title: str = "",
    max_accounts: int = 5,
    force_refresh: bool = False,
    insight_type: str = "",
    segment: str = "",
    min_confidence: float = 0.0,
) -> str:
    """Network Intelligence — a reciprocal pool of members' connected accounts.

    Pool members lend each other their LinkedIn accounts as "network sensors"
    for enrichment, search, network analysis, and anonymized message insights.

    The pool is reciprocal: it lends other members' connections and seats only
    to a workspace whose own LinkedIn seat is an active member. The nine
    consuming actions below are marked "members only" and are refused until
    you join; joining is free and takes two calls — `opt_in`, then `sync`.
    `status` reports whether you are opted in. The same is true of pooled
    Premium/Sales Navigator seats used for search elsewhere in HeyLead: a
    non-member is not lent one and quietly falls back to its own seat.

    Args:
        action: What to do:
            "status"     — Pool health, member accounts, your participation
            "opt_in"     — Join the network pool (share your connections; this is what unlocks the members-only actions)
            "opt_out"    — Leave the network pool (also ends your access to it)
            "sync"       — Refresh your connection graph snapshot
            "opt_in_all" — Admin: opt in all connected LinkedIn accounts
            "sync_all"   — Admin: sync connections for all pool accounts
            "enrich"     — Members only: smart profile lookup via closest-connected pool account
            "contact"    — Members only: get email/phone via a 1st-degree connected pool account
            "parallel"   — Members only: enrich up to 100 profiles in parallel across pool
            "search"     — Members only: distributed search across pool (merged, deduplicated)
            "reach"      — Members only: show which pool accounts can reach a prospect
            "intros"     — Members only: find warm introduction paths to a prospect
            "insights"   — Members only: query aggregated message insights (objections, trends, patterns)
            "trends"     — Members only: industry trend analysis from cross-account conversations
            "patterns"   — Members only: objection and response patterns with timing data
        linkedin_id: Target prospect's LinkedIn provider_id (for enrich/contact/reach/intros).
        linkedin_ids: Comma-separated LinkedIn IDs (for parallel action).
        query: Search keywords (for search action).
        title: Job title filter (for search action).
        max_accounts: Max pool accounts to use for search (default 5).
        force_refresh: Ignore cache for enrich (default False).
        insight_type: Filter insights by type (for insights/patterns actions).
        segment: Filter by industry:seniority segment (for insights/trends actions).
        min_confidence: Minimum confidence threshold 0.0-1.0 (for insights action).
    """
    from .tools.network import run_network

    logger.info("Running network: action=%s", action)
    try:
        return await run_traced(
            "network",
            run_network(
                action=action, linkedin_id=linkedin_id, linkedin_ids=linkedin_ids,
                query=query, title=title, max_accounts=max_accounts,
                force_refresh=force_refresh,
                insight_type=insight_type, segment=segment,
                min_confidence=min_confidence,
            ),
            action=action,
        )
    except Exception as e:
        logger.error("network failed: %s", e, exc_info=True)
        return f"Network intelligence failed: {e}"


@mcp.tool(annotations=_acts("Read and answer the LinkedIn inbox"))
async def inbox(
    action: str = "list",
    chat_id: str = "",
    name: str = "",
    limit: int = 30,
    text: str = "",
) -> str:
    """Read the LinkedIn inbox, and answer from it.

    Read any conversation in your LinkedIn inbox, not just campaign contacts.
    "list", "read" and "comment_drafts" only read. "reply" and "approve_draft"
    SEND a LinkedIn message and cannot be undone; "discard_draft" throws a
    draft away. Use send_message for a campaign contact, and this tool for
    anyone else.

    Args:
        action: What to do:
            "list" — List recent conversations with last message preview
            "read" — Read full conversation thread
            "reply" — Send a message to any inbox conversation
            "comment_drafts" — Replies drafted for comments on your own posts,
                waiting for your approval. Nothing is sent until you approve.
            "approve_draft" — Send one drafted reply (chat_id = draft id).
                Pass text= to send an edited version instead.
            "discard_draft" — Throw a draft away without sending (chat_id = draft id)
        chat_id: Chat ID to read (from list output). For 'read' and 'reply' actions.
            For 'approve_draft' and 'discard_draft', the draft id.
        name: Contact name to search for (partial match). For 'read' and 'reply' actions.
        limit: Max conversations (list) or messages (read) to show. Default 30.
        text: Message text to send. Required for 'reply' action.
    """
    from .tools.inbox import run_inbox

    logger.info("Running inbox: action=%s, name=%s", action, name)
    try:
        return await run_traced(
            "inbox",
            run_inbox(action=action, chat_id=chat_id, name=name, limit=limit, text=text),
            action=action,
        )
    except Exception as e:
        logger.error("inbox failed: %s", e, exc_info=True)
        return f"Inbox failed: {e}"


@mcp.tool(annotations=_acts("Process unreplied LinkedIn inbox messages"))
async def backfill_inbox(
    limit: int = 50,
    dry_run: bool = True,
    min_confidence: float = 0.0,
    send_only: bool = False,
) -> str:
    """Process unreplied LinkedIn inbox messages through the inbound pipeline.

    Scans your inbox for conversations where prospects messaged you but
    never got a reply. Classifies each message and sends discovery DMs.

    Args:
        limit: Max conversations to scan (default 50).
        dry_run: If True (default), only classify — don't send DMs. Set False to send.
        min_confidence: Only send DMs for signals >= this confidence (0.0-1.0).
        send_only: If True, skip inbox scan — process already-classified signals
            directly. Use after a dry_run to avoid re-scanning.
    """
    from .tools.backfill_inbox import run_backfill_inbox

    logger.info("Running backfill_inbox: limit=%d, dry_run=%s, send_only=%s", limit, dry_run, send_only)
    try:
        return await run_traced(
            "backfill_inbox",
            run_backfill_inbox(
                limit=limit, dry_run=dry_run, min_confidence=min_confidence,
                send_only=send_only,
            ),
        )
    except Exception as e:
        logger.error("backfill_inbox failed: %s", e, exc_info=True)
        return f"Backfill failed: {e}"


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────

def main(transport: str = "stdio", host: str = "0.0.0.0", port: int = 8080) -> None:
    """Run the HeyLead MCP server.

    Args:
        transport: Transport type — "stdio", "sse", or "streamable-http".
        host: Host to bind to for HTTP transports. Default: 0.0.0.0.
        port: Port to bind to for HTTP transports. Default: 8080.
    """
    logger.info(f"Starting HeyLead MCP server v{__version__} (transport={transport})")

    # Ensure directories and DB exist on first run
    config.ensure_dirs()

    # Initialize the database
    from .db.schema import get_db
    db = get_db()
    db.close()

    # Pre-populate account_id cache while still in sync context
    # (avoids sync DB calls on the event loop later)
    from .linkedin import get_account_id
    get_account_id()

    # Same for the email-channel cache: select_channel() runs on the loop
    # inside generate_and_send and reads this setting on first call.
    from .services.channel_selector import get_email_account_id
    get_email_account_id()

    # One-shot: score signals written before signal_score was persisted
    # (issue #65). Runs here as well as in the daemon because installs with
    # scheduler_daemon=false never start the daemon. Failure is logged, never
    # fatal — the server must come up even if the backfill cannot run.
    from .services.signal_scorer import backfill_signal_scores
    try:
        backfill_signal_scores()
    except Exception as e:
        logger.warning("Signal score backfill failed: %s", e)

    from .services.signal_activator import backfill_signal_stamps
    try:
        backfill_signal_stamps()
    except Exception as e:
        logger.warning("Signal stamp backfill failed: %s", e)

    # Configure HTTP transport settings if needed
    if transport in ("sse", "streamable-http"):
        mcp.settings.host = host
        mcp.settings.port = port
        logger.info(f"HTTP server binding to {host}:{port}")

    # Run MCP server
    mcp.run(transport=transport)
