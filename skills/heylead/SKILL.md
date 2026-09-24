---
name: heylead
description: LinkedIn outreach through the HeyLead MCP server. Use when the user wants to find and message people on LinkedIn - sales prospecting and lead generation, recruiting and candidate sourcing, user-interview or research participants, job-search networking with hiring managers, investor or partner outreach, vendor scouting, event invitations - or to check replies, campaign status and analytics, or publish LinkedIn posts in their own voice. Campaigns are saved as drafts and nothing is sent until the user launches them. Not for scraping profiles in bulk or for posting to company pages.
---

# HeyLead — Autonomous LinkedIn SDR

Your AI sales rep. One command to fill your pipeline.

HeyLead is an MCP-native autonomous LinkedIn SDR that gives your OpenClaw agent the ability to do LinkedIn outreach — find prospects, send personalized messages, follow up, and close deals.

## What This Skill Does

This skill connects HeyLead as an MCP server in your OpenClaw agent, giving it 22 specialized LinkedIn outreach tools:

- **ICP Generation** — RAG-powered buyer personas with pain points, fears, barriers, and LinkedIn search parameters
- **Campaign Management** — Create, pause, resume, archive, and compare outreach campaigns
- **Personalized Outreach** — Voice-matched connection invitations that sound like you, not a bot
- **Multi-Touch Sequences** — Follow-up DMs, engagement warm-ups (comments, likes, endorsements)
- **Reply Handling** — Sentiment classification, auto-responses, meeting scheduling
- **Analytics** — Funnel reports, conversion rates, stale lead detection, engagement ROI
- **Autonomous Scheduling** — Cloud is the default sender; move to this machine with `scheduler(action='send_from', host='local')`

## Setup

### Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/) installed (`brew install uv` on Mac; other systems: see uv's install page)

### Configuration

Add to your `openclaw.json`:

```json
{
  "mcp": {
    "servers": [
      {
        "name": "heylead",
        "command": "uvx",
        "args": ["heylead"]
      }
    ]
  }
}
```

### First-Time Account Setup

After adding the MCP server, tell your OpenClaw agent:

> "Set up my HeyLead profile"

**Hosted:** take the [90-second quiz](https://heylead.dev/quiz), Sign in, connect LinkedIn on Account, then launch the draft campaign. Or start at [heylead.dev/auth/login-url](https://heylead.dev/auth/login-url). Paste the token message into chat — the backend handles LinkedIn access and AI calls. The quiz waitlist is marketing-only.

**Self-hosted:** requires a [Unipile](https://www.unipile.com) account for LinkedIn access and your own LLM API key (a free [Gemini key](https://aistudio.google.com/apikey) works). Pass the key to setup_profile, open the LinkedIn link it returns, then run setup_profile again.

## Usage Examples

```
"Find me CTOs at fintech startups in New York"
"Generate an ICP for AI SaaS founders"
"Create a campaign targeting VP of Sales at Series B startups"
"Send outreach to the campaign"
"Check my replies"
"How's my outreach doing?"
"Suggest next action"
"Enable the cloud scheduler"
```

## Typical Workflow

```
1. setup_profile(backend_jwt="...")             → Connect LinkedIn
2. generate_icp(target_description="CTOs fintech") → Create buyer personas
3. create_campaign(target_description="...", icp_id="...") → Find prospects (saved as a draft)
4. campaign(action="launch", campaign_id="...") → Start outreach
5. scheduler(action="status")                   → Confirm cloud sending (or send_from host=local)
6. inspect() / check_replies() / show_status()  → Monitor pipeline and agent holds
7. prospect(action="close", outcome="won")      → Track conversions
```

## Agent ops

When the user asks what the agents did, who is held, or why a reply was skipped, call `inspect()` first. It is read-only and never writes. If they ask what the agents decided on a hosted account, call `inspect(action='journal')`. If a campaign looks idle in the send window, call `inspect(action='review')`. If they ask what the agents left for the next tick, what the swarm thinks, or who went dark, call `inspect(action='commons')`. A campaign-wide coordinator hold: `campaign(action='clear_coordinator_hold', campaign_id='...')`.

A hold: `prospect(action="conversation", outreach_id="...")` then `send_message(action="reply", outreach_id="...")`. Operator replies skip the reply agent.

Never paste model-authored text as the LinkedIn message. The send tools generate it.

Never launch a draft unless the user asked.

In-process agents default to act. Use `edit_campaign(enable_reply_agent="observe")`, `edit_campaign(enable_strategist_replan_agent="observe")`, `edit_campaign(enable_hot_lead_closer="observe")`, or `edit_campaign(enable_coordinator_agent="observe")` to return to logging-only, `"off"` to disable. `product(action='tick')` can patch this git checkout and open a PR — never from the send path; cloud workers and `uvx` installs without `.git` refuse.

## Safety Model

Campaigns are created as drafts and only start when explicitly launched. Every send passes rate limits, working-hours checks, and a 1st-degree connection guard.

On a hosted workspace that has not chosen otherwise, opening DMs and follow-ups wait for a person to approve them (`inspect(action="waiting")`, then `prospect(action="approve_message")` or `discard_message`); the approved text is what is sent. Autopilot sends them unread: `scheduler(action="approval_mode", mode="autopilot")`, or Settings → Sending in the dashboard. Invitations are never held.

## All 22 Tools

Brand and content, signals, bulk import, CRM sync and the shared network pool are registered only when the client is started with `HEYLEAD_TOOLS=all`, which brings the full set of 47.

| Tool | What it does |
|------|-------------|
| `setup_profile` | Connect LinkedIn, analyze writing style, create voice signature |
| `account` | Manage LinkedIn accounts — list, switch, or disconnect |
| `organization` | Hosted orgs — list, switch, invite editor/viewer, create a client workspace |
| `generate_icp` | Create Ideal Customer Profiles with buyer personas |
| `icp` | Preview which LinkedIn profiles a saved ICP matches, without creating a campaign |
| `profile_signals` | Compile a targeting request (country ties, interests) into LinkedIn recall queries and profile evidence |
| `create_campaign` | Find prospects and build an outreach campaign (as a draft) |
| `campaign` | Launch, pause, resume, archive, delete, emergency stop, retry failed |
| `edit_campaign` | Update name, mode, booking link, preferences, or agent act flags |
| `import_prospects` | Import prospects from CSV |
| `generate_and_send` | Send personalized connection invitations |
| `send_message` | Follow-up DMs and replies |
| `send_email` | Send email via Unipile (Gmail/Outlook). Never Mail.app. |
| `check_replies` | Monitor inbox, classify sentiment, surface hot leads |
| `book_meeting` | Put an agreed call on your Google Calendar and invite the prospect |
| `show_status` | Campaign dashboard with stats and health; links to the matching heylead.dev/dashboard page and, on hosted accounts, attaches a snapshot card |
| `engage_prospect` | Comment, react, follow, or endorse prospects |
| `inbox` | Browse and read LinkedIn inbox messages |
| `backfill_inbox` | Process unreplied inbox messages through the inbound pipeline |
| `create_post` | Generate and publish posts to LinkedIn, X/Twitter, or both |
| `prospect` | Skip, close with outcome, view conversation or timeline |
| `contacts` | Search and manage the global contact base |
| `partner` | Track follow-ups with partners, vendors, and investors |
| `crm_sync` | Sync won deals to HubSpot CRM |
| `analytics` | Reports, comparisons, and exports |
| `inspect` | Read-only digest of operator holds, replans, closer decisions, reply skips, gated jobs, and (hosted) `action='journal'` |
| `suggest_next_action` | AI-recommended next step |
| `signals` | View and analyze buying signals |
| `manage_watchlist` | Manage signal keyword watchlists |
| `network` | Network intelligence across pool members' connected accounts (reciprocal: join to use it) |
| `brand_strategy` | LinkedIn personal brand audit and strategy |
| `profile` | View and restore LinkedIn profile change history |
| `scheduler` | Autonomous scheduler — status, on/off, send_from (cloud default / local opt-in), always-on |
| `knowledge` | Knowledge base that grounds messages — list, add, remove, refresh, search sources. Hosted only |
| `product` | Local git checkout only — patch this repo and/or open a PR |

## Pricing

| Plan | Price | Limits |
|------|-------|--------|
| **Free** | $0 | Up to 2 follow-ups per prospect |
| **Pro** | $29 per connected LinkedIn account per month | Up to 5 follow-ups per prospect |

Invitation limits follow the LinkedIn account (free, Premium or Sales Navigator), not the HeyLead plan.
Self-hosted free installs have monthly quotas: 50 invitations, 20 messages, 30 engagements, 1 active campaign.

## Privacy

- Contacts and messages stored in local SQLite database
- AI calls routed through HeyLead backend or your own key
- No messages or contacts stored on HeyLead servers

## Links

- [PyPI](https://pypi.org/project/heylead/)
- [GitHub](https://github.com/D4umak/linkedin-outreach-mcp)
- [Issues](https://github.com/D4umak/linkedin-outreach-mcp/issues)

## License

MIT
