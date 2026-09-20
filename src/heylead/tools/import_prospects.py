"""Tool: import_prospects — Import prospects from a CSV/XLSX file into a campaign.

Reads rows (from a file path, or from pasted CSV text), deduplicates against
existing contacts and LinkedIn connections, scores prospects, and creates
contact + outreach records in the campaign.

Every row in the source file gets exactly one disposition — ``imported``,
``skipped:<reason>`` or ``deduped-against:<row>`` — and the counts are
reconciled against the file's row count before anything is written.  A 527-row
import that produced 187 contacts used to look like a success; now it cannot.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

from ..db.queries import (
    assign_variant,
    enroll_prospect,
    find_active_campaign,
    get_outreach,
    get_contacts_for_campaign,
    get_setting,
    list_ab_tests,
    list_campaigns,
    save_contact,
)
from ..formatter import table
from ..linkedin import get_account_id, get_linkedin_client
from ..services.dedup_service import (
    dedup_prospects,
    fetch_connection_ids,
    format_dedup_summary,
    get_all_known_linkedin_ids,
    get_enrolled_people,
)
from ..services.icp_match_scorer import compute_icp_match
from ..services.tabular_reader import TabularReadError, read_table
from ..db.async_bridge import run_db

logger = logging.getLogger(__name__)

# How many individual non-imported rows to spell out in the summary. The
# per-reason counts above the list are always complete.
_MAX_DISPOSITION_LINES = 200

# Auto-detected column name mappings (case-insensitive)
_COLUMN_ALIASES: dict[str, list[str]] = {
    "name": ["name", "full_name", "fullname", "contact_name", "person", "lead"],
    "title": ["title", "job_title", "jobtitle", "position", "role"],
    "company": ["company", "company_name", "companyname", "organization", "org"],
    "linkedin_url": [
        "linkedin_url", "linkedin", "profile_url", "profileurl",
        "linkedin_profile", "url", "link",
    ],
    "linkedin_id": ["linkedin_id", "linkedinid", "public_id", "publicid", "slug"],
    "email": ["email", "email_address", "emailaddress", "e-mail"],
    "location": ["location", "city", "region", "country", "geo"],
}

# Only a /in/ URL carries a public id. Sales Navigator (/sales/lead/...),
# /pub/ and /company/ URLs do not.
_PUBLIC_ID_RE = re.compile(r"/in/([^/?#]+)", re.IGNORECASE)


class ImportReconciliationError(RuntimeError):
    """Raised when file rows != imported + skipped + deduped."""


@dataclass
class RowOutcome:
    """What happened to one row of the source file."""

    row: int
    name: str
    status: str  # "imported" | "skipped:<reason>" | "deduped-against:<row>"


def _detect_columns(headers: list[str]) -> dict[str, int]:
    """Map CSV column headers to known field names. Returns {field: col_index}."""
    mapping: dict[str, int] = {}
    normalized = [h.strip().lower().replace(" ", "_").replace("-", "_") for h in headers]

    for field, aliases in _COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in normalized:
                mapping[field] = normalized.index(alias)
                break

    return mapping


def extract_public_id(url: str) -> str:
    """Public id from a LinkedIn profile URL, or "" if the URL has no /in/ path.

    The old one-liner (``url.split("/in/")[-1].split("/")[0]``) fell through to
    the scheme for every non-/in/ URL, so an entire Sales Navigator export
    imported with ``linkedin_id = "https:"``. Because ``contacts`` is unique on
    (campaign_id, linkedin_id), every one of those rows after the first was
    silently folded into a single contact while still being counted as
    imported. That is how 527 rows became 187 contacts.
    """
    match = _PUBLIC_ID_RE.search(url or "")
    if not match:
        return ""
    return unquote(match.group(1).strip())


def linkedin_url_key(url: str) -> str:
    """Identity key for a LinkedIn URL that carries no public id, else "".

    ``_COLUMN_ALIASES['linkedin_url']`` matches bare headers like ``URL`` and
    ``Link``, so a company-website column lands in ``linkedin_url``. Two rows
    sharing ``https://acme.com`` are two colleagues, not one person — only a
    URL that actually points at linkedin.com may be used to fold rows together.
    """
    raw = (url or "").strip().rstrip("/").lower()
    if not raw:
        return ""
    host, _, path = raw.split("://", 1)[-1].partition("/")
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return ""
    if not path:
        return ""
    return f"linkedin.com/{path}"


def _row_to_prospect(
    cells: list[str], mapping: dict[str, int]
) -> tuple[dict[str, str], str]:
    """Turn one row into a prospect dict. Returns (prospect, skip_reason)."""
    if not cells or all(not cell.strip() for cell in cells):
        return {}, "empty-row"

    prospect: dict[str, str] = {}
    for field, col_idx in mapping.items():
        if col_idx < len(cells):
            prospect[field] = cells[col_idx].strip()

    name = prospect.get("name", "").strip()
    if not name:
        return prospect, "no-name"

    has_context = (
        prospect.get("title")
        or prospect.get("company")
        or prospect.get("linkedin_url")
    )
    if not has_context:
        return prospect, "no-title-company-or-linkedin-url"

    if prospect.get("linkedin_url") and not prospect.get("linkedin_id"):
        public_id = extract_public_id(prospect["linkedin_url"])
        if public_id:
            prospect["linkedin_id"] = public_id

    return prospect, ""


def _score_imported_prospect(prospect: dict[str, str]) -> float:
    """Score an imported prospect based on data completeness (0.0-1.0)."""
    score = 0.0
    max_score = 0.0

    # Has name (always true at this point)
    max_score += 1.0
    score += 1.0

    # Has title
    max_score += 2.0
    if prospect.get("title"):
        score += 2.0

    # Has company
    max_score += 1.5
    if prospect.get("company"):
        score += 1.5

    # Has LinkedIn URL
    max_score += 2.0
    if prospect.get("linkedin_url") or prospect.get("linkedin_id"):
        score += 2.0

    # Has email
    max_score += 1.0
    if prospect.get("email"):
        score += 1.0

    # Has location
    max_score += 0.5
    if prospect.get("location"):
        score += 0.5

    return round(score / max_score, 2) if max_score > 0 else 0.0


def _dedup_reason(
    prospect: dict[str, Any], known_ids: set[str], connection_ids: set[str]
) -> str:
    """Why dedup_prospects rejected this one prospect.

    Re-runs the shared filter on a single prospect so the reason can never
    drift away from the decision that was actually made.
    """
    _kept, stats = dedup_prospects([prospect], known_ids, connection_ids)
    for key, label in (
        ("company_profile", "company-page-not-a-person"),
        ("existing_connection", "already-a-linkedin-connection"),
        ("cross_campaign_duplicate", "already-contacted-in-another-campaign"),
        ("exclusion_list", "on-exclusion-list"),
    ):
        if stats.get(key):
            return label
    return "filtered-by-dedup"


def reconcile_dispositions(total_rows: int, outcomes: list[RowOutcome]) -> dict[str, int]:
    """Every file row must be accounted for exactly once. Raises if not.

    This is the guard the original import lacked: it reported "Imported N"
    without ever checking N against the size of the file.
    """
    counts = {"imported": 0, "skipped": 0, "deduped": 0}
    seen: set[int] = set()

    for outcome in outcomes:
        if outcome.row in seen:
            raise ImportReconciliationError(
                f"Row {outcome.row} was given more than one disposition — "
                "refusing to report an import that does not add up."
            )
        seen.add(outcome.row)

        if outcome.status == "imported":
            counts["imported"] += 1
        elif outcome.status.startswith("deduped-against:"):
            counts["deduped"] += 1
        elif outcome.status.startswith("skipped:"):
            counts["skipped"] += 1
        else:
            raise ImportReconciliationError(
                f"Row {outcome.row} has an unrecognised disposition "
                f"{outcome.status!r} — refusing to report an import that does "
                "not add up."
            )

    accounted = counts["imported"] + counts["skipped"] + counts["deduped"]
    if accounted != total_rows:
        raise ImportReconciliationError(
            f"Import reconciliation failed: the file had {total_rows} data rows "
            f"but only {accounted} were accounted for "
            f"({counts['imported']} imported, {counts['skipped']} skipped, "
            f"{counts['deduped']} deduped). Rows were dropped without a reason — "
            "aborting rather than reporting a partial import as a success."
        )
    return counts


def _format_dispositions(outcomes: list[RowOutcome]) -> list[str]:
    """Per-reason counts (complete) plus a capped row-by-row list."""
    by_status: dict[str, int] = {}
    for outcome in outcomes:
        key = outcome.status
        if key.startswith("deduped-against:"):
            key = "deduped-against-earlier-row"
        by_status[key] = by_status.get(key, 0) + 1

    parts: list[str] = ["", "### Row disposition"]
    rows_out = [[status, str(count)] for status, count in sorted(by_status.items())]
    parts.append(table(["Disposition", "Rows"], rows_out))

    not_imported = sorted(
        (o for o in outcomes if o.status != "imported"), key=lambda o: o.row
    )
    if not_imported:
        parts.append("")
        parts.append(f"**Rows not imported ({len(not_imported)})**:")
        for outcome in not_imported[:_MAX_DISPOSITION_LINES]:
            label = outcome.name or "(no name)"
            parts.append(f"- row {outcome.row} — {label[:40]} — {outcome.status}")
        if len(not_imported) > _MAX_DISPOSITION_LINES:
            parts.append(
                f"- ... and {len(not_imported) - _MAX_DISPOSITION_LINES} more "
                "(the counts above are complete)"
            )

    return parts


def _usage() -> str:
    return (
        "**import_prospects** — Import prospects from a CSV/XLSX file.\n\n"
        "**Usage**: pass `file_path` to a .csv or .xlsx file (preferred — no row "
        "limit), or paste CSV text as `csv_data`.\n\n"
        "**Supported columns** (auto-detected, case-insensitive):\n"
        "- `Name` (required)\n"
        "- `Title` / `Job Title` / `Position`\n"
        "- `Company` / `Organization`\n"
        "- `LinkedIn URL` / `LinkedIn` / `Profile URL`\n"
        "- `Email` / `Email Address`\n"
        "- `Location` / `City`\n\n"
        "**Example**:\n"
        "```\n"
        "import_prospects(file_path=\"~/Downloads/leads.xlsx\", sheet=\"Sheet1\", dry_run=True)\n"
        "```\n\n"
        "Must have name + at least one of: title, company, or LinkedIn URL.\n"
        "Use `dry_run=True` first to see the per-row disposition without writing."
    )


async def run_import_prospects(
    campaign_id: str = "",
    csv_data: str = "",
    linkedin_enrich: bool = False,
    file_path: str = "",
    sheet: str = "",
    dry_run: bool = False,
) -> str:
    """Import prospects from a CSV/XLSX file (or pasted CSV text) into a campaign."""

    # Check setup
    setup_done = await run_db(get_setting, "setup_complete", False)
    if not setup_done:
        return "Setup required. Run setup_profile first."

    if not csv_data.strip() and not file_path.strip():
        return _usage()

    # Resolve campaign
    campaign, err = await run_db(find_active_campaign, campaign_id)
    if not campaign and not campaign_id:
        campaigns = await run_db(list_campaigns)
        if campaigns:
            campaign = campaigns[0]

    if not campaign:
        return err or "No campaign found. Create one first with create_campaign."

    campaign_id = campaign["id"]
    campaign_name = campaign.get("name", "")

    # Read every row of the source, keeping its real row number
    try:
        headers, data_rows = read_table(
            csv_data=csv_data, file_path=file_path, sheet=sheet
        )
    except TabularReadError as e:
        return f"Could not read prospect data: {e}"

    if not headers:
        return "No header row found — the file appears to be empty."

    mapping = _detect_columns(headers)
    if "name" not in mapping:
        return (
            f"Could not detect a 'Name' column in headers: {headers}\n\n"
            "Make sure your file has a column named 'Name', 'Full Name', or 'Contact Name'."
        )

    if not data_rows:
        return "The file has a header row but no data rows."

    total_rows = len(data_rows)

    # ── Row validation + in-file duplicate detection ──
    # save_contact() folds a second row with the same linkedin_id into the
    # existing contact and returns its id, so counting that row as "imported"
    # inflates the total. Catch it here instead, where it can be reported.
    outcomes: list[RowOutcome] = []
    prospects: list[dict[str, str]] = []
    seen_keys: dict[str, int] = {}

    existing_ids = {
        (c.get("linkedin_id") or "")
        for c in await run_db(get_contacts_for_campaign, campaign_id)
        if c.get("linkedin_id")
    }

    for row_no, cells in data_rows:
        prospect, reason = _row_to_prospect(cells, mapping)
        name = prospect.get("name", "")
        if reason:
            outcomes.append(RowOutcome(row_no, name, f"skipped:{reason}"))
            continue

        linkedin_id = prospect.get("linkedin_id", "")
        if linkedin_id and linkedin_id in existing_ids:
            outcomes.append(
                RowOutcome(row_no, name, "deduped-against:existing-campaign-contact")
            )
            continue

        # Same profile twice in one file is one person, whether the file
        # identifies them by public id or only by a LinkedIn URL.
        key = linkedin_id or linkedin_url_key(prospect.get("linkedin_url", ""))
        if key:
            if key in seen_keys:
                outcomes.append(
                    RowOutcome(row_no, name, f"deduped-against:{seen_keys[key]}")
                )
                continue
            seen_keys[key] = row_no

        prospect["_row"] = str(row_no)
        prospects.append(prospect)

    # ── Deduplication against connections / other campaigns ──
    known_ids = await run_db(get_all_known_linkedin_ids)
    enrolled = await run_db(get_enrolled_people)
    connection_ids: set[str] = set()
    connections_read = False

    try:
        account_id = await run_db(get_account_id)
        if account_id:
            client = get_linkedin_client()
            connection_ids = await fetch_connection_ids(client, account_id, max_pages=5)
            connections_read = True
    except Exception as e:
        logger.warning("Connection fetch for dedup failed: %s", e)

    # Convert prospects to dedup-compatible format
    dedup_ready = []
    for p in prospects:
        dedup_ready.append({
            "name": p.get("name", ""),
            "title": p.get("title", ""),
            "company": p.get("company", ""),
            "linkedin_url": p.get("linkedin_url", ""),
            "public_id": p.get("linkedin_id", ""),
            "provider_id": "",
            "email": p.get("email", ""),
            "location": p.get("location", ""),
            "_row": p.get("_row", "0"),
        })

    filtered, dedup_stats = dedup_prospects(
        dedup_ready, known_ids, connection_ids, enrolled_people=enrolled,
    )
    dedup_summary = format_dedup_summary(dedup_stats)

    kept = {id(p) for p in filtered}
    for p in dedup_ready:
        if id(p) in kept:
            continue
        reason = _dedup_reason(p, known_ids, connection_ids)
        outcomes.append(
            RowOutcome(int(p.get("_row", "0")), p.get("name", ""), f"skipped:{reason}")
        )

    # Score and sort — use ICP match if campaign has ICP data
    campaign_icp_json = campaign.get("icp_json", "")
    for p in filtered:
        if campaign_icp_json:
            result = compute_icp_match(p, campaign_icp_json)
            p["_fit_score"] = result["icp_match_score"]
        else:
            p["_fit_score"] = _score_imported_prospect(p)
    filtered.sort(key=lambda p: p.get("_fit_score", 0), reverse=True)

    # Every remaining row is destined for import — record it and check the
    # books BEFORE writing anything.
    for p in filtered:
        outcomes.append(
            RowOutcome(int(p.get("_row", "0")), p.get("name", ""), "imported")
        )

    counts = reconcile_dispositions(total_rows, outcomes)

    # Optional: LinkedIn enrichment.
    # A dry run must not spend the LinkedIn budget: this is up to 50 profile
    # fetches against the rate-limited account, and a dry run exists precisely
    # so the user can look before committing.
    enriched_count = 0
    if linkedin_enrich and filtered and not dry_run:
        try:
            account_id = await run_db(get_account_id)
            client = get_linkedin_client()
            for p in filtered[:50]:  # Cap at 50 to avoid rate limits
                lid = p.get("linkedin_id") or p.get("public_id") or ""
                if not lid:
                    continue
                try:
                    profile = await client.get_profile(account_id, lid)
                    if profile and isinstance(profile, dict):
                        p["_profile_json"] = json.dumps(profile)
                        if not p.get("title") and profile.get("headline"):
                            p["title"] = profile["headline"]
                        if not p.get("company"):
                            headline = profile.get("headline", "")
                            if " at " in headline:
                                p["company"] = headline.rsplit(" at ", 1)[1]
                        enriched_count += 1
                except Exception as e:
                    logger.debug("Enrich failed for %s: %s", lid, e)
        except Exception as e:
            logger.warning("LinkedIn enrichment setup failed: %s", e)

    # ── Insert contacts + outreaches ──
    imported = 0
    outreaches_created = 0
    folded_onto: dict[int, int] = {}
    if not dry_run:
        from ..db.queries import has_running_message_ab_test
        has_ab_test = await run_db(has_running_message_ab_test, campaign_id)
        source_detail = (
            f"{os.path.basename(file_path.strip())} ({len(filtered)} rows)"
            if file_path.strip()
            else f"CSV ({len(filtered)} rows)"
        )
        contact_ids: list[str] = []
        outreach_ids: list[str] = []
        for p in filtered:
            variant = await run_db(assign_variant, campaign_id) if has_ab_test else None
            outreach_id = await run_db(
                enroll_prospect,
                campaign_id,
                {
                    "name": p.get("name", ""),
                    "title": p.get("title", ""),
                    "company": p.get("company", ""),
                    "linkedin_url": p.get("linkedin_url", ""),
                    "linkedin_id": p.get("linkedin_id") or p.get("public_id") or "",
                    "profile_json": p.get("_profile_json", ""),
                    "fit_score": p.get("_fit_score", 0.0),
                    "email": p.get("email", ""),
                },
                source="csv_import",
                source_detail=source_detail,
                variant=variant,
            )
            if not outreach_id:
                contact_ids.append("")
                continue
            outreach = await run_db(get_outreach, outreach_id)
            contact_id = (outreach or {}).get("contact_id") or ""
            contact_ids.append(contact_id)
            outreach_ids.append(outreach_id)
            imported += 1
        outreaches_created = len(set(outreach_ids))

        # save_contact() returns the *existing* contact when (campaign_id,
        # linkedin_id) is already taken, so a row can be counted as imported
        # while creating nothing. That is how 527 rows reported as imported
        # left 187 contacts behind. Never report more imports than contacts.
        #
        # The writes have already landed by this point, so raising here would
        # only surface as "Import failed: ..." and throw away the per-row
        # report — exactly when the user needs it to find the folded rows.
        # Re-point those rows at the row that actually created the contact,
        # re-balance the books, and say so in the summary instead.
        first_row_for_contact: dict[str, int] = {}
        for p, cid in zip(filtered, contact_ids):
            row_no = int(p.get("_row", "0"))
            if cid in first_row_for_contact:
                folded_onto[row_no] = first_row_for_contact[cid]
            else:
                first_row_for_contact[cid] = row_no

        if folded_onto:
            logger.warning(
                "import_prospects: %d rows folded onto existing contacts",
                len(folded_onto),
            )
            outcomes = [
                RowOutcome(o.row, o.name, f"deduped-against:{folded_onto[o.row]}")
                if o.status == "imported" and o.row in folded_onto
                else o
                for o in outcomes
            ]
            counts = reconcile_dispositions(total_rows, outcomes)
            imported = counts["imported"]

    # ── Summary ──
    parts: list[str] = []
    if dry_run:
        parts.append(f"## 🔍 Dry run — {counts['imported']} of {total_rows} rows would import")
    elif imported:
        parts.append(f"## ✅ Imported {imported} of {total_rows} rows")
    else:
        parts.append(f"## ⚠️ Imported 0 of {total_rows} rows")

    parts.append(f"**Campaign**: {campaign_name}")
    source_label = file_path.strip() or "pasted CSV text"
    if sheet:
        source_label += f" (sheet {sheet!r})"
    parts.append(f"**Source**: {source_label}")
    parts.append(
        f"**Reconciled**: {total_rows} file rows = {counts['imported']} imported "
        f"+ {counts['skipped']} skipped + {counts['deduped']} deduped"
    )

    if dedup_summary:
        parts.append(f"**Dedup**: {dedup_summary}")

    if enriched_count:
        parts.append(f"**LinkedIn enriched**: {enriched_count}")

    if dry_run:
        parts.append(
            "**Dry run** — no contacts, outreaches or messages were created, "
            "and no LinkedIn profiles were fetched. "
            "Re-run with `dry_run=False` to write them."
        )
        if linkedin_enrich:
            parts.append(
                "**Enrichment**: `linkedin_enrich=True` was not run — profile "
                "fetches are saved for the real import so a dry run costs "
                "nothing against the LinkedIn rate limit."
            )
        if connections_read:
            parts.append(
                "**Note**: your own connection list was read (the only LinkedIn "
                "call a dry run makes) so this preview matches the real import."
            )
    else:
        # Outreach rows are created with status 'pending'. Nothing is sent here,
        # but the scheduler picks pending outreaches up on its next tick when
        # the campaign is running.
        parts.append(
            f"**Queued**: {outreaches_created} outreaches created with status "
            "`pending`. No messages were sent by this import — the scheduler "
            "sends them while the campaign is active, or run "
            "`generate_and_send()` yourself."
        )
        if folded_onto:
            parts.append(
                f"**Warning**: {len(folded_onto)} rows were folded into contacts "
                "that already existed — they share a LinkedIn id with an earlier "
                "row, so no separate contact or outreach was created for them. "
                "They are listed below as `deduped-against:<row>`."
            )

    parts.extend(_format_dispositions(outcomes))

    # Show sample of imported prospects
    if filtered:
        parts.append("")
        parts.append("**Top prospects**:")
        rows_out = []
        for p in filtered[:10]:
            rows_out.append([
                p.get("name", "")[:25],
                (p.get("title") or "—")[:30],
                (p.get("company") or "—")[:20],
                f"{p.get('_fit_score', 0):.0%}",
            ])
        parts.append(table(["Name", "Title", "Company", "Fit"], rows_out))
        if len(filtered) > 10:
            parts.append(f"... and {len(filtered) - 10} more")

    # These contacts exist only on this machine until something carries them
    # up, and the cloud is what sends to them. Until 19 Sep 2026 the periodic
    # bulk push did it within 15 minutes; that push is refused in a
    # cloud-owned workspace now (it overwrote what the cloud wrote), so the
    # import says so itself. Scoped to this campaign: an import is authoring,
    # and the cloud has nothing newer for rows it has never seen.
    if not dry_run and imported:
        try:
            from ..services.cloud_sync import sync_to_cloud

            await sync_to_cloud(campaign_id=campaign_id)
        except Exception as e:  # noqa: BLE001 — the rows are saved either way
            logger.warning("Import did not reach the cloud (will retry on launch): %s", e)

    parts.append("")
    if dry_run:
        parts.append("**Next**: re-run with `dry_run=False` to import.")
    else:
        parts.append(
            "**Next**: Run `generate_and_send()` to start outreach, or `show_status()` to review."
        )

    return "\n".join(parts)
