---
name: add-people
description: Add a list of named people to a HeyLead LinkedIn outreach campaign from a pasted table or CSV. Use when the user has specific people or LinkedIn profile URLs to reach instead of a search, such as a list from an event, a spreadsheet or a CRM export.
---

# Add people to a campaign

`import_prospects` adds a pasted list to a campaign. It sends nothing by itself: imported people start as pending and go out with the campaign's next sends, at its usual pace.

## Steps

1. **Pick the campaign.** Ask which one, or call `show_status` and offer the choices. A new list for a new goal needs a campaign first (the `start-a-campaign` skill).
2. **Shape the rows as CSV** with a header row: `name, title, company, linkedin_url`. Columns can be in any order; `linkedin` or `url` also work for the URL column. At most 500 rows per call. Keep the user's data as they gave it; do not guess a missing profile URL.
3. **Dry run first.** `import_prospects(campaign_id=..., csv_data=..., dry_run=True)`. Every row comes back with what would happen: imported, already there, or skipped and why. The totals add up to the row count. Show the skipped rows and their reasons.
4. **Import** when the user agrees: the same call with `dry_run=False`.
5. **Say when they will be contacted.** If the campaign is a draft, nothing goes out until the user launches it. If it is running, they join its next sends.

A file on disk, a spreadsheet file and LinkedIn enrichment of the rows are the local HeyLead client's `import_prospects` (`uvx heylead`); this hosted connector takes pasted text.
