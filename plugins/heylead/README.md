# HeyLead plugin for Claude

LinkedIn outreach from Claude. The plugin connects the hosted HeyLead (heylead.dev) server and adds skills that teach Claude the workflow: set up, start a campaign for a goal, approve waiting messages, answer replies, review results, pause or stop, and add a list of people.

HeyLead is an AI agent for LinkedIn outreach: it finds the right people, writes to them in the voice of your own LinkedIn posts, follows up, and handles replies. It runs from Claude Code, Cursor, any MCP client or a web dashboard.

## Install in Claude Code

```
/plugin marketplace add D4umak/linkedin-outreach-mcp
/plugin install heylead@heylead
```

Then sign in: run `/mcp`, pick `plugin:heylead:heylead` and choose Authenticate, or run `claude mcp login plugin:heylead:heylead` in a terminal. You sign in to your HeyLead account in the browser; there is no key to paste. If your LinkedIn account is not connected yet, connect it at https://heylead.dev/dashboard/settings/accounts.

Use only one HeyLead connection, not both. If you also added HeyLead as a connector on claude.ai, sign that one in and skip the plugin's server, or remove it: with both in one account, Claude Code drops the plugin's server partway through a session.

## What it contains

- `.mcp.json`: the hosted server, `https://heylead.dev/mcp` (streamable HTTP, OAuth).
- `skills/`: `get-started`, `start-a-campaign`, `approve-waiting-messages`, `answer-replies`, `weekly-review`, `pause-or-stop`, `add-people`.

## How sending works

HeyLead sends from your own LinkedIn account at a human pace: at most 20 invitations a day and 100 a week on a free LinkedIn account (more on Premium or Sales Navigator), Monday to Friday 08:00 to 22:00 in your time zone, minutes apart. It backs off when LinkedIn pushes back and resumes on its own. You can pause any campaign at any time.

A campaign is a draft until you launch it. Opening messages and follow-ups wait for your approval until you switch to autopilot.

Terms: https://heylead.dev/terms. Security: hello@heylead.dev.
