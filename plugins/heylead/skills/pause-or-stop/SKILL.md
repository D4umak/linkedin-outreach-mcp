---
name: pause-or-stop
description: Pause, resume, archive or stop HeyLead LinkedIn outreach, for one campaign, one person or everything at once. Use when the user says stop, pause, hold on, too many messages, or wants a person never contacted again.
---

# Pause or stop

Match the scope to what the user said, and say what will stop before calling anything.

| The user wants | Call |
| --- | --- |
| One campaign to stop sending for now | `campaign(action='pause', campaign_id=...)` |
| It to start again | `campaign(action='resume', campaign_id=...)` |
| A finished campaign out of the way | `campaign(action='archive', campaign_id=...)` |
| Everything to stop now | `campaign(action='emergency_stop')` |
| The scheduler off, nothing sent until it is on again | `scheduler(action='toggle', enabled=False)` |
| One person never contacted again | `prospect(action='close', outreach_id=..., outcome='opt_out', reason=...)` |
| One person left out of a campaign | `prospect(action='skip', outreach_id=...)` |

## Notes

- A paused campaign sends nothing: no invitations, no follow-ups and no replies.
- `emergency_stop` pauses every campaign in the workspace. Resuming is per campaign.
- If the user is unsure which campaign, call `show_status` first and ask. Do not pause a campaign they did not name.
- After the call, read the state back with `show_status` or `scheduler_status` and tell the user what is now stopped.
- Deleting a campaign is not offered here: it is done on heylead.dev.
