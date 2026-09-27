---
name: get-started
description: Set up HeyLead (heylead.dev) for LinkedIn outreach from Claude. Use when the user first connects HeyLead, asks whether it is set up, asks what HeyLead is or how it paces sends, or a HeyLead tool says the profile or the LinkedIn account is missing.
---

# Get started with HeyLead

HeyLead is an AI agent for LinkedIn outreach: it finds the right people, writes to them in the voice of your own LinkedIn posts, follows up, and handles replies. It runs from Claude Code, Cursor, any MCP client or a web dashboard.

This plugin connects the hosted HeyLead server at `https://heylead.dev/mcp`. There is nothing to install and no API key to paste.

## Steps

1. **Sign in.** Connecting the server asks the user to sign in to their HeyLead account (with Google) in the browser. In Claude Code, run `/mcp`, pick `heylead` and choose Authenticate. The consent page asks for read access and write access; a campaign needs both. If the HeyLead tools are missing, or `/mcp` lists heylead as needing authentication, this step has not happened yet.
2. **Check the setup.** Call `setup_profile`. It shows the profile, the voice it learned from the user's LinkedIn posts, and whether a LinkedIn account is connected.
3. **Connect LinkedIn if it is missing.** LinkedIn is connected on heylead.dev, in Settings, Connected accounts (https://heylead.dev/dashboard/settings/accounts), not in chat. Give the user that link and wait until they say it is done, then call `setup_profile` again.
4. **Show where things stand.** Call `show_status`. A new account has no campaigns; offer the `start-a-campaign` skill.

## What to tell the user about sending

HeyLead sends from your own LinkedIn account at a human pace: at most 20 invitations a day and 100 a week on a free LinkedIn account (more on Premium or Sales Navigator), Monday to Friday 08:00 to 22:00 in your time zone, minutes apart. It backs off when LinkedIn pushes back and resumes on its own. You can pause any campaign at any time.

- It runs in the cloud, so the user's laptop can be closed.
- A campaign is a draft until the user launches it.
- Opening messages and follow-ups wait for the user's approval until they switch to autopilot (`scheduler_status` says which mode is on). Invitations are not held.
- HeyLead reads the user's LinkedIn posts only to learn their voice. It never publishes on their profile unless they ask for a post.

When the user asks what happens after launch, give them the campaign's plan from the create_campaign or launch result. Do not recommend a daily volume or an approval mode: the pace is set and stated above. If asked about LinkedIn's terms, point to https://heylead.dev/terms. Do not call the pace safe or unsafe for the account; say what HeyLead does.

## Workspaces

A HeyLead account can belong to more than one workspace. If the numbers look like someone else's, call `workspaces` and ask the user which one to use before switching with `organization(action='switch')`.
