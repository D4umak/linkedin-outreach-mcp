<!-- mcp-name: io.github.D4umak/heylead -->
# HeyLead

**Your AI sales rep. One command to fill your pipeline.**

HeyLead is an MCP-native autonomous LinkedIn SDR that runs inside Cursor, Claude Code, or any MCP-compatible editor. Sign in on the web, then talk to your AI and say "find me leads."

---

## Getting Started

MCP (Model Context Protocol) lets AI assistants use external tools. HeyLead gives your AI the ability to do LinkedIn outreach for you.

**You need:** [Cursor](https://cursor.com) or [Claude Code](https://docs.anthropic.com/en/docs/claude-code) — any MCP-compatible AI editor.

### Step 1: Install HeyLead

HeyLead runs locally over stdio. You need [uv](https://docs.astral.sh/uv/):

**Claude Code:**
```bash
claude mcp add heylead -- uvx heylead
```

**Cursor:** Settings > MCP > "Add new MCP server" > Name: `heylead`, Command: `uvx heylead`

**Any MCP client:**
```json
{
  "heylead": {
    "command": "uvx",
    "args": ["heylead"]
  }
}
```

Update with `uvx --refresh heylead`.

### Step 2: Set up your account

**Option A — Hosted (easiest):** take the [90-second quiz](https://heylead.dev/quiz)
or sign in at [heylead.dev/dashboard/login](https://heylead.dev/dashboard/login).
Your quiz personas become a draft campaign. Connect LinkedIn in
Settings → Connected accounts, then launch from the dashboard — or copy your
setup message from Settings → Integrations → Chat client into your AI chat.
Nothing sends before you launch. Hosted users share a professional directory;
campaigns and inboxes stay private.

**Option B — Self-hosted:** run everything against your own accounts. You need two things first:

1. **A Unipile account** — this is what talks to LinkedIn. Sign up at
   [unipile.com](https://www.unipile.com), then put the DSN and API key from
   the Access Tokens page into `~/.heylead/config.json` as `unipile_api_url`
   and `unipile_api_key`.
2. **An LLM API key** — AI calls are billed to you. A free
   [Gemini key](https://aistudio.google.com/apikey) is enough to start.

Then open your AI chat and say:

> **"Set up my HeyLead profile with this Gemini key: YOUR_KEY"**

You'll get a LinkedIn authentication link. Open it, connect LinkedIn, then say
**"finish setup"**. HeyLead fetches your profile and analyses your writing style.

### Step 3: Find leads

```
"Find me CTOs at fintech startups in New York"
"Send outreach to the campaign"
"Check my replies"
"How's my outreach doing?"
```

---

## How It Works

1. **Define your ICP** — "Generate an ICP for AI SaaS founders" → RAG-powered personas with pain points, barriers, and LinkedIn targeting
2. **Create a campaign** — "Find me fintech CTOs" → searches LinkedIn, scores prospects by fit
3. **Warm up prospects** — Engages with their posts (comments, likes) before reaching out
4. **Send personalized invitations** — Voice-matched messages that sound like you, not a bot
5. **Follow up automatically** — Multi-touch sequences after connections are accepted
6. **Handle replies** — Detects sentiment, advances positive leads toward meetings, answers questions
7. **Track outcomes** — Won/lost/opted-out tracking with conversion analytics

**Safety model:** campaigns are created as drafts and only start when you
explicitly launch them. Every send passes rate limits, working-hours checks,
and a 1st-degree connection guard before it goes out. Launching is also what
commissions 24/7 cloud sending — in `observe` mode nothing is commissioned and
nothing is sent, from either machine.

---

## Tools

HeyLead gives your AI 22 tools:

### Core Workflow

| Tool | What it does |
|------|-------------|
| `setup_profile` | Connects LinkedIn and analyzes your writing style into a voice signature |
| `generate_icp` | Generates a rich Ideal Customer Profile with buyer personas |
| `icp` | Previews which LinkedIn profiles a saved ICP matches, without creating a campaign |
| `profile_signals` | Compiles a targeting request (country ties, interests) into LinkedIn recall queries and profile-evidence scoring |
| `create_campaign` | Creates an outreach campaign (as a draft) from a natural language description |
| `generate_and_send` | Generates a personalized LinkedIn message and sends it |
| `check_replies` | Checks for new replies across campaigns, classifies sentiment, surfaces hot leads |
| `book_meeting` | Puts an agreed call on your Google Calendar and sends the prospect an invite |
| `show_status` | Your dashboard — campaigns, stats, hot leads, account health. Links to the matching heylead.dev/dashboard page and, on hosted accounts, attaches a snapshot card |

### Outreach & Engagement

| Tool | What it does |
|------|-------------|
| `send_message` | Sends follow-ups and replies to prospects |
| `send_email` | Sends email via a connected Unipile mailbox (Gmail/Outlook). Never Mail.app. |
| `engage_prospect` | Comments on, reacts to, follows, or endorses a prospect to build trust |
| `inbox` | Browses and reads LinkedIn inbox messages directly |
| `backfill_inbox` | Processes unreplied inbox messages through the inbound pipeline |
| `create_post` | Generates and publishes a voice-matched post to LinkedIn, X/Twitter, or both |

### Campaign Management

| Tool | What it does |
|------|-------------|
| `campaign` | Campaign lifecycle — launch, pause, resume, archive, delete, emergency stop, retry failed |
| `edit_campaign` | Edits a campaign's name, mode, booking link, or context fields |
| `prospect` | Manages prospects — skip, close with outcome, view conversation or timeline |
| `import_prospects` | Imports prospects from CSV data into a campaign |

### Insights & Analytics

| Tool | What it does |
|------|-------------|
| `analytics` | Campaign analytics — reports, comparisons, and exports |
| `inspect` | Read-only digest of operator holds, strategist replans, closer decisions, reply skips, and gated jobs |
| `knowledge` | Curates the knowledge base that grounds messages — lists, adds, removes, refreshes, and searches sources. Hosted only |
| `suggest_next_action` | Recommends the best next action, prioritized by impact |
| `signals` | Views and analyzes buying signals — news, company engagement, website visits, profile viewers |
| `manage_watchlist` | Adds, removes, and lists signal keyword watchlists |
| `network` | Network intelligence — a reciprocal pool of members' connected accounts; join to use it |

### Growth & Relationships

| Tool | What it does |
|------|-------------|
| `brand_strategy` | Analyzes and improves your LinkedIn personal brand |
| `profile` | Views and restores LinkedIn profile change history |
| `partner` | Tracks follow-ups with business partners, vendors, and investors |
| `contacts` | Searches, browses, and manages your global contact base |
| `crm_sync` | Syncs campaign contacts and deals to HubSpot CRM |

### Automation & Account

| Tool | What it does |
|------|-------------|
| `scheduler` | Manages the autonomous scheduler — status, on/off, send_from (cloud default / local opt-in) |
| `product` | Local git checkout only — patch this repo and/or open a PR |
| `account` | Manages LinkedIn accounts — list, switch, or disconnect |
| `organization` | Hosted orgs — list, switch, invite editor/viewer, create a client workspace |

---

## Key Features

**Voice Matching** — Analyzes your LinkedIn profile and posts to capture your writing style. Every message sounds like you wrote it.

**ICP Generation** — RAG-powered pipeline that crawls company context, generates buyer personas with pain points, fears, barriers, and maps them to LinkedIn search parameters.

**Autonomous Scheduler** — Runs in the background, respects working hours and rate limits. On a hosted account, cloud is the default sender for every campaign. Launching commissions the cloud, so outreach continues 24/7 with your laptop closed: invitations, opening DMs, first-touch InMail, follow-ups, engagements, follows, endorsements, email fallbacks, prospect top-ups, brand posts, auto-replies, inbound, warmup, signal collectors, and post-intel. This machine does not start a local scheduler engine for that work. Move the whole account here with `scheduler(action='send_from', host='local')`, which turns the cloud scheduler off. Observe still means nobody sends. Direct / self-hosted installs send from this machine only.

**Engagement Warm-ups** — Automatically engages with prospect posts before sending connection requests, building familiarity.

**Adaptive Rate Limiting** — Starts conservative, ramps up when acceptance rate is high, pulls back when it drops. Respects LinkedIn safety limits.

**Outcome Tracking** — Mark deals as won/lost, track conversion rates, identify stale leads, measure engagement ROI.

---

## Pricing

| Plan | Price | What you get |
|------|-------|-------------|
| **Free** | $0 | Up to 2 follow-ups per prospect |
| **Pro** | $29 per connected LinkedIn account per month | Up to 5 follow-ups per prospect |

Invitation limits follow the LinkedIn account (free, Premium or Sales Navigator), not the HeyLead plan.
Self-hosted free installs have monthly quotas: 50 invitations, 20 messages, 30 engagements, 1 active campaign.

---

## Privacy

- AI calls — routed through HeyLead's backend or your own key
- Cloud MCP — your data is processed server-side but never shared with third parties
- Local mode — contacts and messages stay on your machine in a local SQLite database

> **Power users:** Pass your own LLM key (Gemini/Claude/OpenAI) during setup to use your own AI. Completely optional.

---

## Backend mode & env

When the MCP client talks to a HeyLead backend (e.g. `heylead-api`), the backend uses these environment variables. Operators running their own backend should set them as required.

| Purpose | Example env vars |
|--------|-------------------|
| LLM | `GEMINI_API_KEY`, or `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` if using other providers |
| Search / crawl | `SERPER_API_KEY`, `FIRECRAWL_API_KEY` (or similar) for ICP and company context |
| Auth / storage | `GOOGLE_*` (OAuth), `UNIPILE_*` (LinkedIn provider), plus DB/Redis if used |
| Optional | Feature flags, rate limits, logging — see backend repo |

For full backend configuration and deployment, see the **heylead-api** (or backend) repo and its docs.

---

## Optional Dependencies

The base install covers all core features. For advanced ICP generation:

```bash
pip install heylead[icp]    # Embeddings for RAG-powered ICP generation
pip install heylead[crawl]  # Web crawling for company context ingestion
pip install heylead[all]    # Both
```

---

## Troubleshooting

**"uvx: command not found"**
Install `uv` first: `curl -LsSf https://astral.sh/uv/install.sh | sh` (or `brew install uv` on Mac)

**"MCP server not connecting"**
Restart your editor after adding the MCP server. In Cursor, check Settings > MCP — the server should show a green dot.

**"Setup failed" or "LinkedIn not connected"**
Make sure you clicked "Connect" on the LinkedIn row of the sign-in page (dashboard: Settings → Connected accounts) and completed the LinkedIn login. Then run setup again.

**Need help?** Open an [issue](https://github.com/D4umak/linkedin-outreach-mcp/issues).

---

## Publishing to PyPI (maintainers)

To make HeyLead available on PyPI (or to publish a new version):

### Option A: Publish via GitHub Release (recommended)

1. **One-time:** Create a [PyPI account](https://pypi.org/account/register/) and an [API token](https://pypi.org/manage/account/token/). In your repo: **Settings → Secrets and variables → Actions** → add secret `PYPI_TOKEN` with the token value.
2. Bump version in `pyproject.toml` (`version = "0.2.4"`).
3. Commit, push, then create a **GitHub Release** (tag e.g. `v0.2.4`, release title optional). The workflow [`.github/workflows/publish.yml`](.github/workflows/publish.yml) runs on release and publishes to PyPI.

### Option B: Publish manually

```bash
pip install build twine
python -m build          # creates dist/
twine check dist/*       # optional: validate
twine upload dist/*      # prompts for PyPI username + password (use __token__ and your API token)
```

After publishing, anyone can add it with `claude mcp add heylead -- uvx heylead` (Cursor: command `uvx heylead`).

---

## For AI Agents

HeyLead is designed as an MCP-native tool — built for AI agents, not humans clicking buttons.

**Install as MCP server (stdio):**
```json
{
  "heylead": {
    "command": "uvx",
    "args": ["heylead"]
  }
}
```

**OpenClaw:** Add the same entry to your `openclaw.json` under `mcp.servers`.
Also available on [ClawHub](https://clawhub.ai) — search "HeyLead".

Sign in at [heylead.dev](https://heylead.dev) (hosted), or bring your own Unipile account and LLM API key (self-hosted).

**Capabilities:** LinkedIn lead generation, cold outreach automation, ICP generation with buyer personas, voice-matched personalized messaging, multi-touch drip sequences, reply sentiment classification, engagement warm-ups, campaign analytics, and autonomous 24/7 scheduling.

**22 tools** covering the full SDR workflow: prospect discovery → outreach → follow-up → reply handling → deal closing.

See [`AGENTS.md`](AGENTS.md) for the full agent integration guide.

---

## Links

- [PyPI](https://pypi.org/project/heylead/)
- [MCP Registry](https://registry.modelcontextprotocol.io)
- [Smithery](https://smithery.ai)
- [ClawHub](https://clawhub.ai) (OpenClaw skill store)
- [Issues](https://github.com/D4umak/linkedin-outreach-mcp/issues)

## License

MIT (code) — see [LICENSE](LICENSE)

Knowledge base and prompt configurations are proprietary.
