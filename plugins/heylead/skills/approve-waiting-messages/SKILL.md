---
name: approve-waiting-messages
description: Review HeyLead messages waiting for approval before they go out on LinkedIn, and switch between approval mode and autopilot. Use when the user asks what is waiting, wants to approve, edit or discard a drafted message, or asks to turn approval on or off.
---

# Approve waiting messages

In approval mode, HeyLead writes opening messages and follow-ups and holds them until the user approves them. Invitations are not held. A workspace is in approval mode until the user switches to autopilot.

## Steps

1. **Which mode is on.** `scheduler_status` says whether the workspace is in approval mode or on autopilot.
2. **What is waiting.** Call `inspect(action='waiting')`. For each message show who it is for, which campaign, and the text exactly as it will be sent.
3. **Decide, one message at a time, as the user says:**
   - send it as written: `prospect(action='approve_message', draft_id=...)`;
   - send an edited version: `prospect(action='approve_message', draft_id=..., text=<the user's edit>)`;
   - drop it: `prospect(action='discard_message', draft_id=...)`.
   An approved message is sent on LinkedIn and cannot be taken back. Never approve a batch the user has not read.
4. **Skip a person** instead of a message: `prospect(action='skip', outreach_id=...)`.

## Switching the mode

Only when the user asks:

- `scheduler(action='approval_mode', mode='autopilot')`: messages go out without review, and whatever was waiting is released at once. Say that before switching.
- `scheduler(action='approval_mode', mode='require_approval')`: messages wait for the user again.

Do not recommend a mode. Describe what each one does and let the user choose.
