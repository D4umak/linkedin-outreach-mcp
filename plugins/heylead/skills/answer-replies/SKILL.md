---
name: answer-replies
description: Read and answer LinkedIn replies to HeyLead campaigns. Use when the user asks who replied, who is waiting on them, what someone said, or wants to answer, book a meeting with, or close a prospect.
---

# Answer replies

HeyLead reads replies in the cloud every few minutes, classifies them and answers the clear ones from the campaign's facts. Anything it should not answer alone is held for the user. This skill is for the part that needs the user.

## Steps

1. **Who is waiting.** Call `check_replies`. It lists who is waiting on the user first (unanswered replies, a phone number or booking link only they can use, replies held for them), then the latest replies. Show the waiting people first, by name, with what each one said.
2. **Read one conversation.** For the person the user picks, call `prospect_view(action='conversation', outreach_id=...)`. Use `action='timeline'` to see everything that happened with them.
3. **Answer.** The reply is the user's. Draft one if they ask, show it, and send only the text they approve:
   - in a campaign thread: `send_message(outreach_id=..., text=...)`;
   - in any other LinkedIn conversation: `answer_inbox(action='reply', chat_id=..., text=...)`.
   Both send on LinkedIn at once and cannot be undone.
4. **Book a meeting.** When the person agrees to meet and the user has an email for them: `book_meeting(attendee_email=..., start=..., duration_minutes=30)`. It creates the event on the Google Calendar the user connected on heylead.dev, adds a Google Meet link and sends the invitation. Confirm the time and time zone first.
5. **Record the outcome.** `prospect(action='close', outreach_id=..., outcome='won' | 'lost' | 'opt_out', reason=...)`. Use `opt_out` when the person asks not to be contacted: it stops all future contact with them. A won deal can carry `meeting_link` and `deal_value`.

## Other LinkedIn messages

`inbox` reads the whole LinkedIn inbox, not only campaign contacts, and the drafted replies to comments on the user's posts. A drafted comment reply is sent with `answer_inbox(action='approve_draft', chat_id=<draft id>)` or dropped with `action='discard_draft'`.
