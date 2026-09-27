---
name: weekly-review
description: Review how HeyLead LinkedIn outreach campaigns are doing and what to do next. Use when the user asks how outreach is going, for a weekly or daily report, campaign analytics, a comparison of campaigns, what the agents decided, or what to do next.
---

# Weekly review

Read first, change nothing. Every tool in this skill only reads.

## Steps

1. **The overview.** `show_status` for every campaign: status, invitations, accepts, replies, hot leads, account health.
2. **What to do next.** `suggest_next_action` ranks it: messages waiting for approval, replies to answer, failed sends to retry, paused campaigns, drafts never launched. Lead with its first item.
3. **One campaign in depth.** `analytics(action='report', campaign_id=...)` for its funnel, replies and follow-ups. `inspect(action='scorecard', campaign_id=...)` for yesterday's numbers and the one stage that holds the campaign back, with the command that addresses it.
4. **Side by side.** `analytics(action='compare')` for every campaign that is not archived, or pass `campaign_ids`.
5. **What the agents decided.** `inspect(action='journal')` lists each decision, why, and what needs the user. `campaign_status(action='plan', campaign_id=...)` shows what happens next for a campaign.

## How to report

- Numbers as the tools give them, with the period they cover. Do not estimate or round up a rate from a handful of people.
- Name people when the tools name them, and say what each one needs from the user.
- End with at most three actions, each one a tool call the user can say yes to. Hand replies to the `answer-replies` skill and waiting messages to `approve-waiting-messages`.
