# HeyLead — Agent Integration Guide

## What HeyLead Does

HeyLead is an autonomous LinkedIn SDR (Sales Development Representative) that runs entirely via MCP tools. It finds prospects, sends personalized outreach, follows up, tracks replies, and closes deals — all through natural language commands.

**Use cases:** LinkedIn lead generation, cold outreach automation, B2B prospecting, SDR automation, campaign management, ICP (Ideal Customer Profile) generation, multi-touch drip sequences, engagement warm-ups, and outreach analytics.

## Capabilities

| Category | What it does |
|----------|-------------|
| **ICP Generation** | RAG-powered buyer personas with pain points, fears, barriers, and LinkedIn search parameters |
| **Campaign Management** | Create, launch, pause, resume, archive, delete, and compare outreach campaigns |
| **Outreach Automation** | Personalized connection invitations, follow-up DMs, engagement warm-ups (comments, likes) |
| **Reply Handling** | Sentiment classification (positive/negative/question/neutral), auto-responses, meeting scheduling |
| **Intent Signals** | Company news, page engagement, website visitors, profile viewers — compounded into outreach angles |
| **Analytics** | Funnel reports, conversion rates, stale lead detection, engagement ROI |
| **Autonomous Scheduling** | Cloud is the default sender for hosted accounts (existing and new campaigns). Launching commissions the cloud: invitations, opening DMs, first-touch InMail, follow-ups, engagements, follows, endorsements, email fallbacks, campaign top-ups, brand posts, auto-replies, inbound, warmup, signal collectors, post-intel, and housekeeping. This machine stays silent for that work unless the user runs `scheduler(action='send_from', host='local')`, which turns the cloud scheduler off. The local engine does not start on a hosted cloud account. Observe still means nobody sends, including the cloud. Direct / self-hosted installs send from this machine only. |

## Typical Workflow

```
1. setup_profile(backend_jwt="...")             → Connect LinkedIn account
2. generate_icp(target_description="CTOs fintech") → Create buyer personas
3. create_campaign(target_description="...", icp_id="...") → Find prospects (saved as a draft)
4. campaign(action="launch", campaign_id="...") → Start outreach — nothing sends before this
                                                   (hosted accounts: also starts 24/7 cloud sending)
5. scheduler(action="status")                   → Confirm sending is on the cloud (or send_from host=local)
6. inspect() / check_replies() / show_status()  → Monitor pipeline and agent holds
7. prospect(action="close", outcome="won")      → Track conversions
```

## Agent ops

When the user asks what the agents did, who is held, or why a reply was skipped, call `inspect()` first. It is read-only and never writes. If they ask what the agents decided on a hosted account, call `inspect(action='journal')`. If a campaign looks idle in the send window, call `inspect(action='review')`. If they ask what the agents left for the next tick, what the swarm thinks, or who went dark, call `inspect(action='commons')`. A campaign-wide coordinator hold: `campaign(action='clear_coordinator_hold', campaign_id='...')`.

A hold: `prospect(action="conversation", outreach_id="...")` then `send_message(action="reply", outreach_id="...")`. Operator replies skip the reply agent.

Never paste model-authored text as the LinkedIn message. The send tools generate it.

Never launch a draft unless the user asked.

In-process agents default to act. Use `edit_campaign(enable_reply_agent="observe")`, `edit_campaign(enable_strategist_replan_agent="observe")`, `edit_campaign(enable_hot_lead_closer="observe")`, or `edit_campaign(enable_coordinator_agent="observe")` to return to logging-only, `"off"` to disable. `product(action='tick')` can patch this git checkout and open a PR — never from the send path; cloud workers and `uvx` installs without `.git` refuse.

## Authentication

- **Hosted (easiest):** take the 90-second quiz at <https://heylead.dev/quiz>, then Sign in (Google). That claims the quiz as an Account brief and a **draft** campaign. Connect LinkedIn on Account, launch the draft — nothing sends before that. Or sign in at <https://heylead.dev/auth/login-url> and land on Account. Copy the token message and paste it into chat → `setup_profile(backend_jwt="...")`. Hosted users share a professional directory; campaigns and inboxes stay private. Organizations let an owner invite editors (run campaigns) and viewers (stats only). Switch in the dashboard sidebar or with `organization(action="switch", org_id="...")`. The waitlist on `/quiz` is marketing-only and does not create a campaign.
- **Self-hosted:** a Unipile account (LinkedIn access) plus the user's own LLM key in `~/.heylead/config.json`; run `setup_profile()` with no token.
- Optional: bring your own key (Gemini/Claude/OpenAI) via `setup_profile(llm_api_key="...")`.

## All 22 Tools

Brand and content, signals, bulk import, CRM sync and the shared network pool are registered only when the client is started with `HEYLEAD_TOOLS=all`, which brings the full set of 47.

### Setup & Account
| Tool | Description |
|------|-------------|
| `setup_profile` | Connect LinkedIn, analyze writing style, create voice signature. Required first step. |
| `account` | Manage LinkedIn accounts — list, switch, or disconnect |
| `organization` | Hosted orgs — list, switch, invite editor/viewer, create a client workspace |

### ICP & Targeting
| Tool | Description |
|------|-------------|
| `generate_icp` | Create Ideal Customer Profiles with buyer personas, pain points, fears, barriers, and LinkedIn search parameters |
| `icp` | Preview which LinkedIn profiles a saved ICP matches, and how each filter shapes the result, without creating a campaign or any outreach records |
| `profile_signals` | Compile a targeting request (country ties, interests) into LinkedIn recall queries and profile-evidence scoring |

### Campaign Lifecycle
| Tool | Description |
|------|-------------|
| `create_campaign` | Create an outreach campaign (as a draft) from a natural language description, with ICP-based targeting |
| `campaign` | Campaign lifecycle — launch, pause, resume, archive, delete, emergency stop, retry failed |
| `edit_campaign` | Update campaign name, mode, booking link, offerings, case studies, messaging preferences, or agent act flags |
| `import_prospects` | Import prospects from CSV data into a campaign |

### Outreach Execution
| Tool | Description |
|------|-------------|
| `generate_and_send` | Generate and send a personalized LinkedIn message — cold outreach, connection requests, voice-matched messaging |
| `send_message` | Send follow-ups and replies to prospects — drip sequences, multi-touch nurture |
| `send_email` | Send an email to a prospect through a connected Gmail/Outlook mailbox |
| `engage_prospect` | Comment on, react to, follow, or endorse a prospect — social-selling warm-up |
| `book_meeting` | Book a meeting on Google Calendar and send the prospect an invite |
| `inbox` | Browse and read LinkedIn inbox messages directly |
| `backfill_inbox` | Process unreplied inbox messages through the inbound qualification pipeline |
| `create_post` | Generate and publish a voice-matched post to LinkedIn, X/Twitter, or both |

### Prospects & Contacts
| Tool | Description |
|------|-------------|
| `prospect` | Manage prospects — skip, close with outcome (won/lost/opted out), view conversation or timeline |
| `contacts` | Search, browse, and manage the global contact base; direct LinkedIn people lookup |
| `partner` | Track follow-ups with business partners, vendors, and investors |
| `crm_sync` | Sync campaign contacts and deals to HubSpot CRM |

### Insights & Analytics
| Tool | Description |
|------|-------------|
| `show_status` | Dashboard — campaigns, stats, hot leads, account health. Links to the matching heylead.dev/dashboard page and, on hosted accounts, attaches a snapshot card |
| `check_replies` | Check for new replies, classify sentiment, surface hot leads |
| `analytics` | Campaign analytics — reports, comparisons, and exports |
| `inspect` | Read-only digest of operator holds, strategist replans, closer decisions, reply skips, gated jobs, hosted `action='journal'`, and `action='review'` (campaign watch / stall adjustments) |
| `knowledge` | Knowledge base that grounds generated messages — list, add, remove, refresh, or search sources. Hosted only. |
| `suggest_next_action` | Recommend the best next action, prioritized by impact |
| `signals` | View and analyze buying signals — news, company engagement, website visitors, profile viewers |
| `manage_watchlist` | Add, remove, and list signal keyword watchlists |
| `network` | Network intelligence — a reciprocal pool of members' connected accounts; join to use it |

### Brand & Profile
| Tool | Description |
|------|-------------|
| `brand_strategy` | Analyze and improve the user's LinkedIn personal brand to drive inbound leads |
| `profile` | View and restore LinkedIn profile change history |

### Automation
| Tool | Description |
|------|-------------|
| `scheduler` | Autonomous scheduler — status, toggle on/off, send_from (cloud default / local opt-in), always-on |
| `product` | Local git checkout only — patch this repo and/or open a PR. Never from the send path. |

## Installation

**Claude Code:**
```bash
claude mcp add heylead -- uvx heylead
```

**Cursor:** Settings → MCP → Add new MCP server → Name: `heylead`, Command: `uvx heylead`

**OpenClaw** (`openclaw.json`):
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

**Any MCP client:**
```json
{
  "heylead": {
    "command": "uvx",
    "args": ["heylead"]
  }
}
```

## Transport

- **stdio** — `uvx heylead`; update with `uvx --refresh heylead`
- **streamable-http** — `heylead --transport streamable-http` for self-hosted HTTP serving
