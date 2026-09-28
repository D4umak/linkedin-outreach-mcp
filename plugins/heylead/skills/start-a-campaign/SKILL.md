---
name: start-a-campaign
description: Plan and create a HeyLead LinkedIn outreach campaign for a goal - find customers (lead generation, B2B prospecting), find a job (hiring managers and referrals), hire, find partners or investors, find a vendor, or recruit research interviews. Use when the user wants to reach a kind of person on LinkedIn. Creates a draft; launching is a separate, explicit step.
---

# Start a campaign

A campaign has one of six goals, and the ICP, the fit check and the messages follow it: Sell a product or service, Find a job, Hire people, Find partners or investors, Find a vendor and Research interviews.

| The user wants to | `goal=` |
| --- | --- |
| Find customers, leads, first users | `sell` |
| Find a job: the people who hire for or refer into a role | `job_search` |
| Hire candidates | `hire` |
| Reach investors, partners, resellers | `partner` |
| Find a vendor (the user is the buyer) | `buy` |
| Recruit people for interviews, surveys or beta tests | `research` |

Hire people, Find partners or investors and Research interviews run on a custom brief until their message sets exist. For those three, the project brief must say plainly what is being asked of the person.

A job search is its own goal, not a sales campaign: replies are held for the user to answer.

## Steps

1. **Ask for what is missing, in one message.** Who they want to reach (role, company type, place), what they offer or want, and in their own words what they need from the person. Do not invent an offer, a customer, a number or a case study.
2. **Build the personas.** Call `generate_icp(target_description=..., goal=...)`. Pass `company_context` with what the user offers when they gave it. Show the 2 to 4 personas it returns: title, pain points, and the search it will run. Ask which to keep.
3. **Create the draft.** Call `create_campaign` with the same `target_description` and `goal`, the `icp_id` and the `selected_persona_ids` the user kept, and a `project_brief` in the user's words. Use `connections_only=True` only when the user wants to write to people they are already connected with.
4. **Show the plan.** The result ends with what happens after launch, step by step. Show it as it is. Point the user to the draft page on heylead.dev for a sample first message.
5. **Stop.** `create_campaign` saves a draft and sends nothing. Ask whether to launch. Do not launch in the same turn unless the user already said "launch".

## Launching

Launch only when the user says so, for this campaign: `campaign(action='launch', campaign_id=...)`. If it asks for a `project_brief`, get one from the user; a placeholder is refused.

Say what launch does before calling it: HeyLead starts sending from the user's own LinkedIn account, at its stated pace, Monday to Friday 08:00 to 22:00 in their time zone. Opening messages and follow-ups wait for approval unless the workspace is on autopilot; `scheduler_status` says which.

## Changing a draft

Settings (name, booking link, offerings, follow-ups, invitations on or off) go through `edit_campaign`. To add named people instead of searching, use the `add-people` skill.
