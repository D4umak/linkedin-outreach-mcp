"""Connection sync service — persist 1st-degree LinkedIn connections locally.

Fetches connections from Unipile's relations API and stores them in the local
SQLite `connections` table so we can do fast set-based dedup without hitting
the API every time. Also provides helpers to check/mark individual connections.

Key functions:
- sync_connections(): Full sync from Unipile → local DB
- get_local_connection_ids(): Fast set of provider_id + public_id from local DB
- is_first_degree(): Quick single-row check
- mark_connected(): Record a newly connected prospect
- get_sync_age(): Seconds since any row was last stamped
- get_last_full_sync_age(): Seconds since the last completed relations walk
  (not the same thing — mark_connected() stamps single rows)
- audit_campaign_connections(): Batch audit for existing campaigns
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from ..db.async_bridge import run_db
from ..db.schema import get_db

logger = logging.getLogger(__name__)

# Re-sync if local data is older than this (1 hour) — used by ensure_synced() for dedup
SYNC_STALE_SECONDS = 3600
# Full sync stale threshold (24 hours) — used for my_connections search
FULL_SYNC_STALE_SECONDS = 86400

# Wall-clock ceiling for the scheduled sync, handed in by the executor.
#
# Neither fetch strategy bounds itself: the relations walk is up to 200 pages
# and the inbox fallback up to 400, each page with a 30s HTTP timeout. The
# scheduler cancels a whole tick at 55s (engine._TICK_BUDGET_SECONDS) and that
# cancellation arrives as CancelledError — a BaseException, so the executor's
# `except Exception` never files it: the job row stays `running` until the
# 30-minute stuck-job sweeper releases it, and every planning block after job
# execution is dropped from that tick. A run that cannot finish inside the tick
# never finishes at all, because each retry restarts at page one.
#
# Interactive callers pass no budget: network(action='sync') and
# contacts(action='my_connections') are user-initiated and may take as long as
# a full network needs.
SCHEDULED_SYNC_BUDGET_SECONDS = 30.0

# Sentinel so "caller said nothing" and "caller explicitly wants no limit" are
# different things. They used to be the same value (None), and that is why this
# bug kept coming back: three review rounds found five separate in-tick callers
# that had simply never passed a budget, and the guard test written to catch
# them allowlisted the fifth by name. There are 19 call sites across four
# layers; enumerating them is not a strategy. Defaulting to bounded means a
# caller added tomorrow is safe without anyone remembering this.
_UNSET = object()

# Wall-clock ceiling for the opportunistic refresh the three send paths run
# before their 1st-degree check — executors._check_is_first_degree,
# generate_send and send_followup. Tighter than the scheduled job's, because
# this is one step of a job that must also generate and send a message inside
# the same tick, and because a miss is recoverable: the pre-check falls back to
# asking LinkedIn about the single prospect it actually cares about. The full
# walk belongs to JOB_SYNC_CONNECTIONS, which has the job window to itself.
#
# A fetch abandoned here writes nothing, so on an account too large to finish
# in this time the refresh is spent again on the next tick. That is deliberate:
# bounded and repeated beats unbounded and cancelled, which strands the job row
# at `running` for 30 minutes and drops the rest of the tick's planning.
PRECHECK_SYNC_BUDGET_SECONDS = 10.0

# Wall-clock ceiling for the opportunistic refresh that dedup runs before it
# compares a prospect list against the connections table — ensure_synced()
# reached from campaign_refill_service (JOB_CAMPAIGN_REFILL, hourly) and from
# run_generate_and_send's step-0 first-run sync.
#
# Same size and same reasoning as the pre-check budget, but a different failure
# to recover from, so it is named separately: dedup that misses the refresh
# compares against the rows already stored rather than falling back to a live
# per-prospect check, so a refill can re-queue somebody the user has connected
# with since the table was last written. That costs one redundant outreach
# candidate. Carrying an unbounded 200-page walk into a tick cancelled whole at
# 55s costs the job row (stranded `running` for 30 minutes) and the rest of that
# tick's planning.
DEDUP_SYNC_BUDGET_SECONDS = 10.0

# One opportunistic Relations walk per tick is enough. A stale cache then
# falls back to a single-prospect get_profile — repeating the 10s walk on
# every orphan DM is what produced 109 timeout warnings last night.
_PRECHECK_SYNC_WINDOW_SECONDS = 55.0
_last_precheck_sync_mono = 0.0
_stale_cache_window_start = 0.0
_stale_cache_logged_this_window = False


def should_run_precheck_sync() -> bool:
    """True once per ~tick for the 10s opportunistic Relations walk."""
    global _last_precheck_sync_mono
    now = time.monotonic()
    if now - _last_precheck_sync_mono < _PRECHECK_SYNC_WINDOW_SECONDS:
        return False
    _last_precheck_sync_mono = now
    return True


def log_stale_cache_once(log: logging.Logger, msg: str, *args: object) -> None:
    """INFO on the first stale-cache hit per tick; DEBUG after that."""
    global _stale_cache_window_start, _stale_cache_logged_this_window
    now = time.monotonic()
    if now - _stale_cache_window_start >= _PRECHECK_SYNC_WINDOW_SECONDS:
        _stale_cache_window_start = now
        _stale_cache_logged_this_window = False
    if not _stale_cache_logged_this_window:
        _stale_cache_logged_this_window = True
        log.info(msg, *args)
    else:
        log.debug(msg, *args)

# Per-account timestamp of the last sync that actually walked the relations
# API. Kept in settings rather than read off the connections table — see
# get_last_full_sync_age().
_LAST_FULL_SYNC_KEY = "connections_last_full_sync"
# Resume token so a 10s/30s budget does not restart the walk at page 1.
_RELATIONS_CURSOR_KEY = "connections_relations_cursor"
_RELATIONS_PAGE_SIZE = 100

# Per-account "everything already in the network on the day this account was
# bound to HeyLead" timestamp. 9 Sep 2026: a campaign must not reach a
# person who was already a 1st-degree connection, and the relations API does
# not date the edge. The first full sync of an account therefore stamps every
# row it inserts with this epoch, which is the honest statement we can make —
# "connected no later than the day we connected the account".
#
# Written per account under its own key (not one dict) so a second account
# cannot rewrite the first one's epoch through a read-modify-write race.
_BACKFILL_EPOCH_KEY_PREFIX = "connections_backfill_epoch:"


def backfill_epoch_key(account_id: str) -> str:
    return f"{_BACKFILL_EPOCH_KEY_PREFIX}{account_id}"


def get_backfill_epoch(account_id: str) -> int | None:
    """The connect-time epoch for an account, or None if never stamped."""
    from ..db.queries import get_setting

    raw = get_setting(backfill_epoch_key(account_id), None)
    try:
        return int(raw) if raw else None
    except (TypeError, ValueError):
        return None


def set_backfill_epoch(account_id: str, epoch: int | None = None) -> int:
    """Stamp (once) the backfill epoch for an account and return it.

    Never overwrites an existing value: the epoch is the account's own bind
    date, and re-stamping it would make every pre-existing connection look
    newer than the campaigns that must not reach them.
    """
    from ..db.queries import save_setting

    existing = get_backfill_epoch(account_id)
    if existing:
        return existing
    value = int(epoch or time.time())
    save_setting(backfill_epoch_key(account_id), value)
    return value

# The manual re-fetch named in the truncated-response warning below. Named once
# so the warning and the command that has to honour it cannot drift apart —
# tests/test_network_sync_local_cache.py dispatches this exact string.
_MANUAL_RESYNC_COMMAND = "network(action='sync')"


def _coerce_epoch(value: Any) -> int | None:
    """Best-effort epoch seconds from whatever a transport handed us.

    Unipile has been seen returning both epoch seconds and epoch milliseconds
    on date fields, and ISO-8601 strings elsewhere, so all three are accepted
    and anything else is dropped rather than stored as a bogus 1970 date (a
    1970 connected_at reads as "pre-existing", which is the safe direction but
    still a lie).
    """
    if value in (None, "", 0):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        ts = int(value)
        if ts > 100_000_000_000:  # milliseconds
            ts //= 1000
        return ts if ts > 0 else None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.isdigit():
            return _coerce_epoch(int(raw))
        try:
            from datetime import datetime

            return int(
                datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
            )
        except (ValueError, TypeError):
            return None
    return None


def get_last_full_sync_age(account_id: str) -> int | None:
    """Seconds since the last completed relations-API sync, or None if never.

    Deliberately not MAX(connections.synced_at), which get_sync_age() reads:
    mark_connected() stamps synced_at one row at a time whenever an outreach
    turns out to be connected, so on any account that is actually sending, a
    single accepted invite made the entire table look freshly synced and the
    24-hour gate below closed again before the next scheduled run.
    """
    from ..db.queries import get_setting

    marks = get_setting(_LAST_FULL_SYNC_KEY, {})
    if not isinstance(marks, dict):
        return None
    ts = marks.get(account_id)
    if not ts:
        return None
    try:
        return int(time.time()) - int(ts)
    except (TypeError, ValueError):
        return None


def _load_relations_cursor(account_id: str) -> str | None:
    """Resume token for the last unfinished relations walk, or None."""
    from ..db.queries import get_setting

    marks = get_setting(_RELATIONS_CURSOR_KEY, {})
    if not isinstance(marks, dict):
        return None
    cursor = marks.get(account_id)
    return str(cursor) if cursor else None


def _save_relations_cursor(account_id: str, cursor: str | None) -> None:
    from ..db.queries import get_setting, save_setting

    marks = get_setting(_RELATIONS_CURSOR_KEY, {})
    if not isinstance(marks, dict):
        marks = {}
    if cursor:
        marks[account_id] = cursor
    else:
        marks.pop(account_id, None)
    save_setting(_RELATIONS_CURSOR_KEY, marks)


def _call_get_relations(client: Any, account_id: str, limit: int, cursor: str | None):
    """Call get_relations with a resume cursor when the client accepts one."""
    try:
        return client.get_relations(account_id, limit=limit, cursor=cursor)
    except TypeError:
        return client.get_relations(account_id, limit=limit)


async def _fetch_relations(
    client: Any,
    account_id: str,
    max_relations: int,
    deadline: float | None = None,
) -> tuple[list[dict[str, Any]], bool, bool, bool | None]:
    """Walk relations one page at a time so a spent budget can resume.

    Returns (items this run, walk_finished, started_from_scratch, client_complete).
    ``client_complete`` is True/False when the client reported it, else None
    so the upsert guards can still tell a page-aligned prefix from a small
    network. A finished walk that resumed mid-network must not prune against
    the suffix — earlier pages already live in ``connections``.
    """
    if deadline is not None and time.monotonic() >= deadline:
        return [], False, True, None

    start_cursor = await run_db(_load_relations_cursor, account_id)
    cursor = start_cursor
    collected: list[dict[str, Any]] = []
    walk_finished = False
    client_complete: bool | None = None

    while len(collected) < max_relations:
        if deadline is not None and time.monotonic() >= deadline:
            break
        limit = min(_RELATIONS_PAGE_SIZE, max_relations - len(collected))
        fetch = _call_get_relations(client, account_id, limit, cursor)
        try:
            if deadline is None:
                page = await fetch
            else:
                page = await asyncio.wait_for(
                    fetch, timeout=max(0.0, deadline - time.monotonic()),
                )
        except asyncio.TimeoutError:
            break

        items = [item for item in (page or []) if isinstance(item, dict)]
        if not items:
            walk_finished = True
            client_complete = True
            cursor = None
            break

        collected.extend(items)
        next_cursor = getattr(page, "cursor", None)
        page_complete = getattr(page, "complete", None)
        if next_cursor:
            cursor = str(next_cursor)
            client_complete = False
            await run_db(_save_relations_cursor, account_id, cursor)
            continue
        if page_complete is False:
            walk_finished = False
            client_complete = False
            break
        walk_finished = True
        client_complete = page_complete
        cursor = None
        break

    if walk_finished:
        await run_db(_save_relations_cursor, account_id, None)
    elif cursor:
        await run_db(_save_relations_cursor, account_id, cursor)

    return collected, walk_finished, start_cursor is None, client_complete


def _record_full_sync(account_id: str) -> None:
    """Stamp a completed relations-API sync for this account."""
    from ..db.queries import get_setting, save_setting

    marks = get_setting(_LAST_FULL_SYNC_KEY, {})
    if not isinstance(marks, dict):
        marks = {}
    marks[account_id] = int(time.time())
    save_setting(_LAST_FULL_SYNC_KEY, marks)


async def sync_connections(
    client: Any,
    account_id: str,
    max_relations: int = 20000,
    force: bool = False,
    time_budget: float | None = _UNSET,   # type: ignore[assignment]
) -> int:
    """Fetch 1st-degree connections from Unipile and upsert into local DB.

    Tries the relations API first; if it fails/returns empty, falls back to
    extracting connections from inbox chats (each chat attendee = 1st degree).

    Args:
        client: LinkedIn client (BackendClient or UnipileClient).
        account_id: Unipile account ID.
        max_relations: Max relations to fetch (paginated).
        force: If True, bypass the stale check and always re-sync.
        time_budget: Wall-clock seconds the whole fetch may take, shared
            between the relations walk and the inbox fallback. None means no
            limit, which is right for a user-initiated sync and wrong for a
            scheduler job — see SCHEDULED_SYNC_BUDGET_SECONDS.

    Returns:
        Number of connections synced, or the existing local count when the
        cache was still fresh and nothing was fetched.
    """
    # Skip if the last full sync is still fresh (unless forced)
    if not force:
        age = await run_db(get_last_full_sync_age, account_id)
        if age is not None and age < FULL_SYNC_STALE_SECONDS:
            count = await run_db(get_connection_count, account_id)
            if count > 0:
                logger.info("Connection sync fresh (age=%ds, count=%d), skipping", age, count)
                return count

    # Unset means bounded. Only an explicit time_budget=None runs unbounded, and
    # that is for interactive, user-initiated syncs where the user is waiting
    # and no tick is going to be cancelled underneath them.
    if time_budget is _UNSET:
        time_budget = SCHEDULED_SYNC_BUDGET_SECONDS
    deadline = None if time_budget is None else time.monotonic() + time_budget

    relations: list[dict[str, Any]] = []
    from_fallback = False
    fetch_complete: bool | None = None
    mark_full_sync = False

    # Strategy 1: Relations API, one page at a time so a spent budget
    # writes the pages that finished and resumes from the saved cursor.
    try:
        relations, walk_finished, started_from_scratch, client_complete = (
            await _fetch_relations(
                client, account_id, max_relations, deadline=deadline,
            )
        )
        # A suffix from a resumed walk must not drive the prune. The 24h
        # gate still closes once the walk actually finished.
        if walk_finished and started_from_scratch:
            fetch_complete = client_complete
        else:
            fetch_complete = False
        mark_full_sync = bool(walk_finished and not started_from_scratch)
        if deadline is not None and not relations and not walk_finished:
            logger.warning(
                "Relations API did not finish inside the %gs sync budget for account %s "
                "— no connections written this run",
                time_budget, account_id,
            )
            return 0
    except Exception as e:
        logger.warning("Relations API failed, will try inbox fallback: %s", e)

    # Strategy 2: Inbox chats fallback (provider_ids only, no names)
    if not relations:
        if deadline is not None and time.monotonic() >= deadline:
            logger.warning(
                "Sync budget spent before the inbox fallback for account %s", account_id,
            )
            return 0
        logger.info("Falling back to inbox chats for connection sync")
        relations = await _sync_from_inbox(
            client, account_id, max_chats=max_relations, deadline=deadline,
        )
        from_fallback = True

    if not relations:
        logger.debug("No connections found for account %s", account_id)
        return 0

    def _upsert_connections() -> tuple[int, bool]:
        """Returns (rows written, response rejected as a prefix of the network).

        The second value exists because the prune guard below is the only place
        that can tell a truncated response from a complete one, and two
        decisions depend on that verdict: whether to delete the rows this
        response omits, and whether to record a completed full sync. Standing
        the prune down while stamping the marker says both "this is a prefix"
        and "this was the whole network" about the same fetch, and the marker
        wins — it suppresses the honest fetch for the next 24 hours.
        """
        db = get_db()
        now_ = int(time.time())
        synced_ = 0
        truncated_ = False
        committed_ = False

        # First full walk of an account: everything it returns was already in
        # the network before HeyLead was pointed at it, so it is dated with the
        # bind epoch rather than "now". Later walks only add genuinely new
        # edges, which really did appear since the last walk.
        first_walk_ = get_last_full_sync_age(account_id) is None
        default_connected_ = (
            set_backfill_epoch(account_id) if first_walk_ else now_
        )

        try:
            for rel in relations:
                provider_id = (rel.get("provider_id") or "").strip()
                if not provider_id:
                    continue

                public_id = (rel.get("public_id") or "").strip()
                name = (rel.get("name") or "").strip()
                headline = (rel.get("headline") or "").strip()
                company = (rel.get("company") or "").strip()
                location = (rel.get("location") or "").strip()
                profile_url = (rel.get("profile_url") or "").strip()
                if not profile_url and public_id:
                    profile_url = f"https://www.linkedin.com/in/{public_id}"

                # Order of preference for "connected since": a date the
                # transport actually carried, else the bind epoch on the first
                # walk, else now. See unipile.get_relations for the field names
                # inspected.
                connected_at = _coerce_epoch(rel.get("connected_at"))
                if connected_at is None:
                    connected_at = default_connected_

                # Upsert: INSERT OR REPLACE on the UNIQUE(account_id, provider_id) constraint.
                # connected_at is COALESCEd, never assigned from excluded: it is
                # first-seen, and a 4-hourly re-stamp would erase the very thing
                # the exclusion rule compares against. removed_at is cleared —
                # a re-appearing connection is live again but keeps its date.
                db.execute(
                    """INSERT INTO connections
                       (id, account_id, provider_id, public_id, name, headline,
                        company, location, profile_url, synced_at, connected_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(account_id, provider_id) DO UPDATE SET
                           public_id = excluded.public_id,
                           name = excluded.name,
                           headline = excluded.headline,
                           company = COALESCE(NULLIF(excluded.company, ''), connections.company),
                           location = COALESCE(NULLIF(excluded.location, ''), connections.location),
                           profile_url = COALESCE(NULLIF(excluded.profile_url, ''), connections.profile_url),
                           synced_at = excluded.synced_at,
                           connected_at = COALESCE(connections.connected_at, excluded.connected_at),
                           removed_at = NULL""",
                    (str(uuid.uuid4()), account_id, provider_id, public_id,
                     name, headline, company, location, profile_url, now_,
                     connected_at),
                )
                synced_ += 1

            # Prune stale connections: remove entries not in the fresh API response.
            # This handles people who disconnected since the last sync.
            # NEVER prune from fallback data: inbox chat partners are a small,
            # skewed subset of the real network, and treating them as the full
            # relations list would delete most genuine connections.
            fresh_pids = {(rel.get("provider_id") or "").strip() for rel in relations}
            fresh_pids.discard("")
            if not from_fallback and fresh_pids and synced_ > 0:
                # The prune is only correct if `relations` really is the whole
                # network. Three ways it is not, and deleting on any of them
                # costs far more than keeping a stale row: a connection missing
                # from this table blocks the DM to a real 1st-degree contact
                # (#74) and lets dedup re-invite someone already connected,
                # while a surplus row costs one redundant "already connected"
                # check.
                stored = db.execute(
                    "SELECT COUNT(*) FROM connections "
                    "WHERE account_id = ? AND removed_at IS NULL",
                    (account_id,),
                ).fetchone()[0]
                if len(relations) >= max_relations:
                    # The walk stopped at the cap, so this is a prefix of the
                    # network rather than all of it.
                    truncated_ = True
                    logger.warning(
                        "Connection sync: relations fetch hit the %d cap for account %s "
                        "— not pruning and not recording a full sync, the response is a "
                        "prefix of the network",
                        max_relations, account_id,
                    )
                elif fetch_complete is False:
                    truncated_ = True
                    logger.warning(
                        "Connection sync: relations walk for account %s ended on a "
                        "full page with no cursor — not pruning and not recording a "
                        "full sync, the response is a prefix of the network",
                        account_id,
                    )
                elif (
                    fetch_complete is None
                    and get_last_full_sync_age(account_id) is None
                    and len(fresh_pids) > 10
                    and len(fresh_pids) < max_relations
                    and (len(fresh_pids) % 100 == 0 or len(fresh_pids) % 500 == 0)
                ):
                    # First-ever sync: stored == just-upserted, so the
                    # half-drop guard can never fire. A page-aligned count
                    # (Unipile 100 / backend 500) is how a truncated walk
                    # looks when the client did not report completeness.
                    truncated_ = True
                    logger.warning(
                        "Connection sync: first relations walk for account %s "
                        "returned %d (page-aligned) with no prior full sync — "
                        "not recording a full sync",
                        account_id, len(fresh_pids),
                    )
                elif stored > 0 and len(fresh_pids) * 2 < stored:
                    # get_relations() ends its pagination as soon as a page
                    # comes back without a cursor, so one hiccup or one renamed
                    # field returns a fraction of the network and looks exactly
                    # like a fresh full response. A drop of more than half is
                    # far more often that than a real mass disconnection.
                    truncated_ = True
                    logger.warning(
                        "Connection sync: relations API returned %d for account %s against "
                        "%d stored — not pruning and not recording a full sync, a drop this "
                        "large reads as a truncated response. The stored rows are kept and "
                        "this run does not close the 24h gate; %s refetches now.",
                        len(fresh_pids), account_id, stored, _MANUAL_RESYNC_COMMAND,
                    )

                if truncated_:
                    pass  # there is nothing to prune against a prefix
                elif len(fresh_pids) <= 10:
                    # Too small to judge a network by, so the prune stands
                    # down — but not treated as truncation: a genuinely new
                    # account has a handful, and refusing it the marker would
                    # refetch every four hours for ever. The case that matters,
                    # a handful against a full table, is caught above.
                    pass
                else:
                    placeholders = ",".join("?" for _ in fresh_pids)
                    # Soft delete (9 Sep 2026). This used to be a
                    # DELETE, which threw away connected_at: one truncated page
                    # pruned a row, the next walk re-inserted it, and a
                    # ten-year-old connection re-dated itself to today and
                    # stopped being "pre-existing".
                    deleted = db.execute(
                        f"""UPDATE connections SET removed_at = ?
                            WHERE account_id = ? AND removed_at IS NULL
                              AND provider_id NOT IN ({placeholders})""",
                        (now_, account_id, *fresh_pids),
                    ).rowcount
                    if deleted:
                        logger.info("Connection sync: pruned %d stale connections for account %s", deleted, account_id)

            db.commit()
            committed_ = True
            logger.info("Connection sync: %d connections synced for account %s", synced_, account_id)
        except Exception as e:
            logger.warning("Connection sync failed (DB): %s", e)
            try:
                # Explicit, because close() cannot do it: get_db() hands back a
                # singleton proxy whose close() is a no-op by design, so the
                # statements above stay pending on the shared connection and
                # the next successful commit anywhere in the process writes
                # them — including the prune's DELETE. Without this the
                # "none of those rows exist" below is only a wish.
                db.rollback()
            except Exception:
                logger.warning("Connection sync: rollback failed", exc_info=True)
        finally:
            db.close()

        if not committed_:
            # The transaction rolled back, so none of those rows exist. The
            # count is what callers print and what the full-sync marker acts
            # on; reporting queued-but-discarded work as synced is how a failed
            # write closes the 24-hour gate.
            return 0, True
        return synced_, truncated_

    synced, truncated = await run_db(_upsert_connections)

    # Only a complete relations-API run counts as a full sync. Fallback rows are
    # a skewed subset and a truncated response is a prefix, so stamping either
    # here would suppress the real fetch for the next 24 hours.
    if synced > 0 and not from_fallback and (not truncated or mark_full_sync):
        await run_db(_record_full_sync, account_id)

    # Update global_contacts.network_degree for synced connections
    await run_db(_update_global_contacts_degree, relations)

    return synced


async def _sync_from_inbox(
    client: Any,
    account_id: str,
    max_chats: int = 500,
    deadline: float | None = None,
) -> list[dict[str, Any]]:
    """Extract 1st-degree connections from inbox chats as a fallback.

    Each LinkedIn chat has an attendee_provider_id which is a 1st-degree connection.
    This works when the relations API times out (common for accounts with many connections).
    Fetches raw chat data directly from the backend/Unipile chats endpoint.

    ``deadline`` is a time.monotonic() value past which no further page is
    requested. Without it this walks up to max_chats/50 pages — 400 at the
    caller's default — each with its own 30s HTTP timeout.
    """
    relations: list[dict[str, Any]] = []
    seen_pids: set[str] = set()

    try:
        # Use raw chats endpoint to get attendee_provider_id
        # (the normalized get_chats() loses this field)
        all_chats: list[dict[str, Any]] = []
        cursor: str | None = None
        page_size = min(max_chats, 50)
        pages = 0
        max_pages = (max_chats // page_size) + 1

        while pages < max_pages:
            if deadline is not None and time.monotonic() >= deadline:
                logger.warning(
                    "Inbox fallback stopped at page %d — sync budget spent", pages,
                )
                break
            chats_page, next_cursor = await _fetch_raw_chats(
                client, page_size, cursor,
            )
            if not chats_page:
                break
            all_chats.extend(chats_page)
            cursor = next_cursor
            pages += 1
            if not cursor or len(all_chats) >= max_chats:
                break

        for chat in all_chats:
            if not isinstance(chat, dict):
                continue

            # Extract attendee provider_id
            att_pid = chat.get("attendee_provider_id", "")
            if not att_pid or att_pid in seen_pids:
                continue
            seen_pids.add(att_pid)

            relations.append({
                "provider_id": str(att_pid),
                "name": "",
                "headline": "",
                "public_id": "",
            })

        logger.info(
            "Inbox fallback: found %d unique connections from %d chats",
            len(relations), len(all_chats),
        )
    except Exception as e:
        logger.warning("Inbox fallback failed: %s", e)

    return relations


async def _fetch_raw_chats(
    client: Any,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Fetch raw chat objects from the backend/Unipile chats endpoint.

    Returns (items, next_cursor). Items have attendee_provider_id for connections.
    Uses backend config directly for reliability (avoids client internal access issues).
    """
    import httpx

    try:
        # Get backend config directly — more reliable than accessing client internals
        from ..config import get_backend_config
        base_url, jwt_token = get_backend_config()
        if base_url and jwt_token:
            # Shared builder, not a hand-rolled Authorization line: it is the
            # only place X-Org-Id (the workspace the user has selected) is
            # attached.
            from .cloud_sync import _headers
            headers = _headers()
        else:
            # Try client internals as fallback
            base_url = getattr(client, "base_url", "")
            jwt_token = getattr(client, "jwt_token", "")
            if not base_url or not jwt_token:
                logger.debug("No backend config or client credentials for raw chats fetch")
                return [], None
            headers = client._headers()

        url = f"{base_url.rstrip('/')}/api/v1/chats?limit={limit}"
        if cursor:
            url += f"&cursor={cursor}"

        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http:
            resp = await http.get(url, headers=headers)

        if resp.status_code != 200:
            logger.warning("Raw chats fetch returned %d: %s", resp.status_code, resp.text[:200])
            return [], None

        data = resp.json()
        items = data.get("items", []) if isinstance(data, dict) else []
        next_cursor = data.get("cursor") if isinstance(data, dict) else None

        return items, next_cursor
    except Exception as e:
        logger.warning("Raw chats fetch failed: %s", e)
        return [], None


def _update_global_contacts_degree(relations: list[dict[str, Any]]) -> None:
    """Upsert 1st-degree connections into global_contacts.

    For existing contacts: sets network_degree=1.
    For new contacts: creates a global_contacts record from connection data
    so all 1st-degree connections are available across campaigns/accounts.
    """
    db = get_db()
    try:
        updated = 0
        inserted = 0
        now_ = int(time.time())

        for rel in relations:
            provider_id = (rel.get("provider_id") or "").strip()
            public_id = (rel.get("public_id") or "").strip()
            name = (rel.get("name") or "").strip()
            if not provider_id and not public_id:
                continue
            # Mirror of connections.connected_at, and mirrored the same way:
            # COALESCE, so a row that already has a date keeps it.
            rel_connected = _coerce_epoch(rel.get("connected_at"))

            # Try updating existing contact first (by provider_id or public_id)
            matched = False
            for identifier in [public_id, provider_id]:
                if not identifier:
                    continue
                cursor = db.execute(
                    """UPDATE global_contacts
                       SET network_degree = 1, updated_at = ?,
                           connected_at = COALESCE(connected_at, ?)
                       WHERE (linkedin_id = ? OR linkedin_id = ?) AND (network_degree IS NULL OR network_degree != 1)""",
                    (now_, rel_connected, identifier, identifier.lower()),
                )
                if cursor.rowcount > 0:
                    updated += cursor.rowcount
                    matched = True
                    break
                # Already 1st degree but never dated — backfill the date only.
                db.execute(
                    """UPDATE global_contacts SET connected_at = ?
                       WHERE (linkedin_id = ? OR linkedin_id = ?)
                         AND connected_at IS NULL AND ? IS NOT NULL""",
                    (rel_connected, identifier, identifier.lower(), rel_connected),
                )
                # Check if it exists but already marked 1st-degree
                exists = db.execute(
                    "SELECT 1 FROM global_contacts WHERE linkedin_id = ? OR linkedin_id = ? LIMIT 1",
                    (identifier, identifier.lower()),
                ).fetchone()
                if exists:
                    matched = True
                    break

            # Insert new contact if not found anywhere
            if not matched and name:
                headline = (rel.get("headline") or "").strip()
                company = (rel.get("company") or "").strip()
                location = (rel.get("location") or "").strip()
                profile_url = (rel.get("profile_url") or "").strip()
                linkedin_id = public_id or provider_id

                try:
                    db.execute(
                        """INSERT INTO global_contacts
                           (id, linkedin_id, linkedin_url, name, title, company,
                            location, network_degree, connected_at, source,
                            lifecycle_stage, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, 'connection_sync', 'prospect', ?, ?)""",
                        (str(uuid.uuid4()), linkedin_id, profile_url, name,
                         headline, company, location, rel_connected, now_, now_),
                    )
                    inserted += 1
                except Exception:
                    pass  # UNIQUE constraint — skip duplicates

        if updated or inserted:
            db.commit()
            if updated:
                logger.info("Updated network_degree=1 for %d global contacts", updated)
            if inserted:
                logger.info("Inserted %d new global contacts from connections", inserted)
    except Exception as e:
        logger.debug("Global contacts degree update skipped: %s", e)
    finally:
        db.close()


def get_all_connections(account_id: str) -> list[dict[str, str]]:
    """Return all locally stored 1st-degree connections with full data fields.

    Returns list of dicts with: provider_id, public_id, name, headline.
    Used by connections-only campaigns to source prospects directly from
    the connections table instead of LinkedIn search.
    """
    db = get_db()
    try:
        rows = db.execute(
            "SELECT provider_id, public_id, name, headline FROM connections "
            "WHERE account_id = ? AND removed_at IS NULL",
            (account_id,),
        ).fetchall()
    except Exception:
        return []
    finally:
        db.close()

    results = []
    for row in rows:
        r = dict(row) if hasattr(row, "keys") else {
            "provider_id": row[0] or "",
            "public_id": row[1] or "",
            "name": row[2] or "",
            "headline": row[3] or "",
        }
        if not (r.get("provider_id") or r.get("public_id")):
            continue
        results.append({
            "provider_id": (r.get("provider_id") or "").strip(),
            "public_id": (r.get("public_id") or "").strip(),
            "name": (r.get("name") or "").strip(),
            "headline": (r.get("headline") or "").strip(),
        })
    return results


def get_local_connection_ids(account_id: str) -> set[str]:
    """Fast set-based lookup of all locally stored 1st-degree connection identifiers.

    Returns a set of lowercase provider_ids + public_ids + linkedin URLs.
    """
    db = get_db()
    try:
        rows = db.execute(
            """SELECT provider_id, public_id FROM connections
               WHERE account_id = ? AND removed_at IS NULL""",
            (account_id,),
        ).fetchall()
    except Exception:
        return set()
    finally:
        db.close()

    ids: set[str] = set()
    for row in rows:
        pid = row[0] or ""
        pub = row[1] or ""
        if pid:
            ids.add(pid.lower().strip())
        if pub:
            ids.add(pub.lower().strip())
            ids.add(f"https://www.linkedin.com/in/{pub.lower().strip()}")
    return ids


def is_first_degree(account_id: str, provider_id: str) -> bool:
    """Quick check if a specific prospect is a 1st-degree connection in local DB."""
    if not provider_id:
        return False
    db = get_db()
    try:
        row = db.execute(
            "SELECT 1 FROM connections WHERE account_id = ? AND provider_id = ? "
            "AND removed_at IS NULL LIMIT 1",
            (account_id, provider_id.strip()),
        ).fetchone()
        return row is not None
    except Exception:
        return False
    finally:
        db.close()


def is_first_degree_by_public_id(account_id: str, public_id: str) -> bool:
    """Quick check if a specific prospect is a 1st-degree connection by public_id."""
    if not public_id:
        return False
    db = get_db()
    try:
        row = db.execute(
            "SELECT 1 FROM connections WHERE account_id = ? AND LOWER(public_id) = LOWER(?) "
            "AND removed_at IS NULL LIMIT 1",
            (account_id, public_id.strip()),
        ).fetchone()
        return row is not None
    except Exception:
        return False
    finally:
        db.close()


def get_connected_at(
    account_id: str, provider_id: str = "", public_id: str = "",
) -> int | None:
    """Epoch this person became a 1st-degree connection, or None if unknown.

    Only live rows count: a pruned (soft-deleted) connection is not a
    connection today, whatever date it carries.
    """
    pid = (provider_id or "").strip()
    pub = (public_id or "").strip()
    if not pid and not pub:
        return None
    db = get_db()
    try:
        row = db.execute(
            """SELECT connected_at FROM connections
               WHERE account_id = ? AND removed_at IS NULL
                 AND (
                     (? != '' AND provider_id = ?)
                     OR (? != '' AND LOWER(public_id) = LOWER(?))
                 )
               ORDER BY connected_at IS NULL, connected_at ASC
               LIMIT 1""",
            (account_id, pid, pid, pub, pub),
        ).fetchone()
    except Exception:
        return None
    finally:
        db.close()
    if not row:
        return None
    return int(row[0]) if row[0] else None


def is_known_connection(
    account_id: str, provider_id: str = "", public_id: str = "",
) -> bool:
    """True when a live connections row exists for either identifier."""
    if provider_id and is_first_degree(account_id, provider_id):
        return True
    return bool(public_id and is_first_degree_by_public_id(account_id, public_id))


def is_preexisting_connection(
    account_id: str,
    provider_id: str = "",
    public_id: str = "",
    campaign_created_at: int | None = None,
) -> bool:
    """Was this person already a 1st-degree connection before the campaign?

    9 Sep 2026. "Existing connection" deliberately does NOT mean
    "1st-degree right now": someone who accepts *our* invite becomes
    1st-degree mid-campaign and must still get the opener and the follow-ups.
    The test is therefore the edge's own date against the campaign's.

    An unknown ``connected_at`` (NULL) reads as pre-existing. That is the safe
    direction — the promise to the customer is "never message an existing
    connection", and the rows that predate this feature carry the bind epoch
    anyway.
    """
    if not is_known_connection(account_id, provider_id, public_id):
        return False
    if not campaign_created_at:
        return True
    connected_at = get_connected_at(account_id, provider_id, public_id)
    if connected_at is None:
        return True
    return connected_at < int(campaign_created_at)


def mark_connected(
    account_id: str,
    provider_id: str,
    name: str = "",
    public_id: str = "",
    headline: str = "",
    connected_at: Any = None,
) -> None:
    """Record a newly connected prospect in local connections table.

    Called when we discover a prospect is already connected (via profile check or 409).

    ``connected_at`` is only used when the row is new, and defaults to NULL —
    "we do not know when this edge appeared" — rather than now(). An existing
    row keeps the date it already has.
    """
    if not provider_id:
        return
    db = get_db()
    now_ = int(time.time())
    # NULL when the caller does not know, deliberately — never now(). This is
    # called from the send paths, which observe an edge long after it appeared,
    # and a "connected today" stamp would make a ten-year-old connection look
    # like someone who accepted this campaign's invitation, i.e. someone
    # exclude_connections must still message. NULL reads as pre-existing, which
    # is the safe direction (9 Sep 2026).
    connected_ = _coerce_epoch(connected_at)
    try:
        db.execute(
            """INSERT INTO connections (id, account_id, provider_id, public_id, name, headline, synced_at, connected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(account_id, provider_id) DO UPDATE SET
                   name = COALESCE(NULLIF(excluded.name, ''), connections.name),
                   public_id = COALESCE(NULLIF(excluded.public_id, ''), connections.public_id),
                   headline = COALESCE(NULLIF(excluded.headline, ''), connections.headline),
                   synced_at = excluded.synced_at,
                   connected_at = COALESCE(connections.connected_at, excluded.connected_at),
                   removed_at = NULL""",
            (str(uuid.uuid4()), account_id, provider_id.strip(), public_id, name, headline, now_, connected_),
        )
        db.commit()
    except Exception as e:
        logger.debug("mark_connected failed: %s", e)
    finally:
        db.close()


def get_sync_age(account_id: str) -> int | None:
    """Return seconds since last connection sync, or None if never synced."""
    db = get_db()
    try:
        row = db.execute(
            "SELECT MAX(synced_at) FROM connections "
            "WHERE account_id = ? AND removed_at IS NULL",
            (account_id,),
        ).fetchone()
        if row and row[0]:
            return int(time.time()) - int(row[0])
        return None
    except Exception:
        return None
    finally:
        db.close()


async def ensure_synced(
    client: Any,
    account_id: str,
    time_budget: float | None = _UNSET,  # type: ignore[assignment]
) -> set[str]:
    """Ensure connections are synced (auto-sync if stale), then return local IDs.

    This is the main entry point for dedup: checks sync age and refreshes if needed,
    then returns the fast local set.

    Args:
        client: LinkedIn client (BackendClient or UnipileClient).
        account_id: Unipile account ID.
        time_budget: Wall-clock seconds the refresh may take, handed straight to
            sync_connections(). Omitted (the default) is bounded. Only an
            explicit ``time_budget=None`` opts out — right for a user-initiated
            call, wrong for anything the scheduler reaches. This wrapper is the
            reason the budget has to be a parameter: its own gate is only
            `get_sync_age > SYNC_STALE_SECONDS`, which nothing but a written row
            advances, and a walk abandoned or cancelled writes nothing. So on an
            account too large to finish one, the gate re-opens an hour after
            whatever last touched the table and the next in-tick caller starts
            again at page one.

    Returns:
        The local set of provider_id and public_id values, whatever the refresh
        did. A refresh that ran out of budget is not an error here: the set is
        simply the rows already stored.
    """
    age = await run_db(get_sync_age, account_id)
    if age is None or age > SYNC_STALE_SECONDS:
        logger.info("Connection sync stale (age=%s) — re-syncing for account %s", age, account_id)
        await sync_connections(client, account_id, time_budget=time_budget)
    return await run_db(get_local_connection_ids, account_id)


async def enrich_prospects(
    client: Any,
    account_id: str,
    prospects: list[dict],
    max_enrich: int = 50,
    delay: float = 1.0,
) -> int:
    """Enrich prospects missing profile_json with full LinkedIn profiles.

    Calls get_profile() per prospect that lacks profile_json, then upserts
    the enriched data to global_contacts and updates the prospect dict in-place.
    Also fetches recent posts for enriched prospects.

    Args:
        client: LinkedIn client (UnipileClient or BackendClient).
        account_id: Unipile account ID.
        prospects: List of prospect dicts to enrich (modified in-place).
        max_enrich: Maximum number of profiles to enrich (rate limit safety).
        delay: Seconds to wait between API calls.

    Returns:
        Number of profiles successfully enriched.
    """
    import asyncio
    import json as _json

    from ..db.global_contact_queries import upsert_global_contact

    # Filter to prospects that need enrichment
    to_enrich = [
        p for p in prospects
        if not p.get("profile_json")
        and (p.get("provider_id") or p.get("public_id") or p.get("linkedin_id"))
    ][:max_enrich]

    if not to_enrich:
        return 0

    enriched = 0
    for i, prospect in enumerate(to_enrich):
        lid = (
            prospect.get("provider_id")
            or prospect.get("public_id")
            or prospect.get("linkedin_id", "")
        )
        if not lid:
            continue

        try:
            profile = await client.get_profile(account_id, lid)
            if not profile or not isinstance(profile, dict):
                continue

            from .prospect_email import attach_email_to_profile_json, extract_profile_email
            email = extract_profile_email(profile)
            profile_json = attach_email_to_profile_json(_json.dumps(profile), email) or _json.dumps(profile)
            # Update prospect dict in-place with enriched data
            prospect["profile_json"] = profile_json
            if email:
                prospect["email"] = email
            if profile.get("title") and not prospect.get("title"):
                prospect["title"] = profile["title"]
            if profile.get("headline"):
                prospect["title"] = prospect.get("title") or profile["headline"]
            if profile.get("company") and not prospect.get("company"):
                prospect["company"] = profile["company"]
            if profile.get("location") and not prospect.get("location"):
                prospect["location"] = profile["location"]
            if profile.get("provider_id") and not prospect.get("provider_id"):
                prospect["provider_id"] = profile["provider_id"]

            # Upsert to global_contacts
            try:
                await run_db(upsert_global_contact,
                    linkedin_id=lid,
                    name=prospect.get("name", ""),
                    title=prospect.get("title", ""),
                    company=prospect.get("company", ""),
                    linkedin_url=prospect.get("linkedin_url", ""),
                    email=email,
                    location=prospect.get("location", ""),
                    profile_json=profile_json,
                    source="connection_enrichment",
                )
            except Exception as e:
                logger.debug("Global contact upsert failed for %s: %s", lid, e)

            # Fetch recent posts
            try:
                from ..db.post_queries import upsert_post
                posts = await client.get_user_posts(account_id, lid, limit=10)
                if posts and isinstance(posts, list):
                    for post in posts:
                        pid = post.get("id", "")
                        txt = post.get("text", "")
                        if pid and txt:
                            await run_db(upsert_post,
                                pid,
                                author_linkedin_id=lid,
                                author_name=prospect.get("name", ""),
                                text=txt[:2000],
                                metrics_json=_json.dumps(post.get("metrics", {})),
                                source="enrichment",
                            )
            except Exception:
                pass  # Post fetch is non-critical

            enriched += 1
            logger.debug("Enriched %s (%d/%d)", prospect.get("name", lid), enriched, len(to_enrich))

        except Exception as e:
            logger.debug("Enrich failed for %s: %s", lid, e)

        # Rate limit delay (skip after last item)
        if i < len(to_enrich) - 1:
            await asyncio.sleep(delay)

    logger.info("Enriched %d/%d prospects with full profiles", enriched, len(to_enrich))
    return enriched


async def audit_campaign_connections(
    client: Any,
    account_id: str,
    campaign_id: str,
    time_budget: float | None = _UNSET,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Audit a campaign's pending outreaches against current 1st-degree connections.

    1. Ensure connections are synced
    2. Get all pending outreaches for the campaign
    3. Cross-reference against local connections
    4. For matches: update outreach to 'connected'
    5. Return audit report

    Args:
        time_budget: Passed to ensure_synced(). No production caller reaches
            this from a scheduler tick today; the parameter is here so that the
            next one that does has somewhere to put its budget.

    Returns:
        {"total_pending": int, "already_connected": int, "updated": list[str]}
    """
    from ..db.schema import get_db as _get_db

    # Step 1: Ensure fresh connections
    await ensure_synced(client, account_id, time_budget=time_budget)
    connection_ids = await run_db(get_local_connection_ids, account_id)

    # Step 2: Get pending outreaches
    def _get_pending_outreaches(cid):
        db = _get_db()
        try:
            return db.execute(
                """SELECT o.id, o.contact_id, c.linkedin_id, c.name, c.linkedin_url,
                          c.profile_json
                   FROM outreaches o
                   JOIN contacts c ON o.contact_id = c.id
                   WHERE o.campaign_id = ? AND o.status = 'pending'""",
                (cid,),
            ).fetchall()
        finally:
            db.close()

    rows = await run_db(_get_pending_outreaches, campaign_id)

    report = {
        "total_pending": len(rows),
        "already_connected": 0,
        "updated": [],
    }

    if not rows:
        return report

    # Step 3 & 4: Cross-reference and update
    import json

    for row in rows:
        outreach_id = row[0]
        linkedin_id = (row[2] or "").lower().strip()
        name = row[3] or ""
        linkedin_url = (row[4] or "").lower().strip()
        profile_json_str = row[5] or "{}"

        # Extract provider_id from profile_json
        provider_id = ""
        try:
            profile = json.loads(profile_json_str)
            provider_id = (profile.get("provider_id") or "").lower().strip()
        except Exception:
            pass

        # Build identifier set for this prospect
        identifiers = {x for x in (linkedin_id, provider_id, linkedin_url) if x}
        if linkedin_id:
            identifiers.add(f"https://www.linkedin.com/in/{linkedin_id}")

        # Check against local connections
        if identifiers & connection_ids:
            # Through update_outreach, not raw SQL: it logs the transition.
            # accepted_at is stamped explicitly — the audit is observing the
            # connection right now; a bare status flip no longer infers one.
            from ..db.queries import update_outreach

            await run_db(
                update_outreach, outreach_id,
                status="connected", accepted_at=int(time.time()),
            )

            report["already_connected"] += 1
            report["updated"].append(name or outreach_id)
            logger.info("Audit: %s is already 1st-degree connected — marked as 'connected'", name)

    return report


def get_connection_count(account_id: str) -> int:
    """Return the number of locally stored connections for an account."""
    db = get_db()
    try:
        row = db.execute(
            "SELECT COUNT(*) FROM connections "
            "WHERE account_id = ? AND removed_at IS NULL",
            (account_id,),
        ).fetchone()
        return row[0] if row else 0
    except Exception:
        return 0
    finally:
        db.close()


# ──────────────────────────────────────────────
# Excluding pre-existing connections (9 Sep 2026)
#
# Before this, "already connected" only ever changed the *channel*: the planner
# left 1st-degree people out of invite jobs and the channel picker routed them
# to a DM. A campaign told to leave existing connections alone still messaged
# every one of them, just over DM instead of an invitation. These helpers are
# the single place that decides otherwise, so the six send paths cannot drift.
# ──────────────────────────────────────────────


def _identifiers_from_row(row: dict[str, Any]) -> tuple[str, str]:
    """(provider_id, public_id) from an outreach/contact/prospect dict."""
    import json as _json

    blob: dict[str, Any] = {}
    raw = row.get("profile_json") or ""
    if isinstance(raw, dict):
        blob = raw
    elif raw:
        try:
            parsed = _json.loads(raw)
            if isinstance(parsed, dict):
                blob = parsed
        except (ValueError, TypeError):
            blob = {}
    provider_id = str(
        row.get("provider_id") or blob.get("provider_id") or ""
    ).strip()
    public_id = str(
        row.get("public_id")
        or row.get("linkedin_id")
        or blob.get("public_id")
        or blob.get("public_identifier")
        or ""
    ).strip()
    return provider_id, public_id


def is_excluded_connection_row(
    account_id: str,
    config: dict[str, Any] | None,
    campaign_created_at: int | None,
    row: dict[str, Any],
) -> bool:
    """Should this campaign refuse to contact this row at all?

    False unless the campaign's ``exclude_connections`` is on. An outreach we
    have already invited is never excluded, whatever the connections table says
    — accepting our invitation is exactly how a prospect becomes 1st-degree,
    and dropping them there would leave the campaign inviting people and then
    never speaking to them.
    """
    from .outreach_channel import exclude_connections_enabled

    if not exclude_connections_enabled(config):
        return False
    if row.get("invited_at"):
        return False
    if not account_id:
        return False
    provider_id, public_id = _identifiers_from_row(row)
    return is_preexisting_connection(
        account_id, provider_id, public_id, campaign_created_at,
    )


async def is_excluded_connection_outreach(
    campaign: dict[str, Any] | None,
    outreach: dict[str, Any] | None,
) -> bool:
    """Async wrapper: campaign row + outreach row in, verdict out."""
    import json as _json

    if not campaign or not outreach:
        return False
    try:
        config = _json.loads(campaign.get("config_json") or "{}")
    except (ValueError, TypeError):
        config = {}
    if not isinstance(config, dict):
        config = {}
    from ..linkedin.unipile import get_account_id

    account_id = await run_db(get_account_id)
    if not account_id:
        return False
    return await run_db(
        is_excluded_connection_row,
        account_id, config, campaign.get("created_at"), outreach,
    )


async def is_excluded_uninvited(
    campaign: dict[str, Any] | None,
    outreach: dict[str, Any] | None,
) -> bool:
    """Exclusion verdict for a caller that has *just* proved 1st-degree live.

    `is_excluded_connection_outreach` reads the connections table and compares
    dates. That is wrong here: the live check is the first time we have heard
    of this edge, so recording it would date it "now" and the comparison would
    conclude the person connected during the campaign. They did not — we never
    invited them (`invited_at` is NULL) and LinkedIn says we are connected, so
    the edge predates us by construction. 9 Sep 2026.
    """
    import json as _json

    if not campaign or not outreach:
        return False
    if outreach.get("invited_at"):
        return False
    try:
        config = _json.loads(campaign.get("config_json") or "{}")
    except (ValueError, TypeError):
        config = {}
    if not isinstance(config, dict):
        return False
    from .outreach_channel import exclude_connections_enabled

    return exclude_connections_enabled(config)


async def skip_excluded_connection(
    outreach_id: str,
    campaign_id: str = "",
    name: str = "",
    where: str = "",
) -> str:
    """Park an excluded row as ``skipped`` and return the human message.

    ``last_attempt_error`` carries the refusal reason verbatim so the reason a
    row stopped is legible from the outreach table alone — the same string the
    enrol gate logs and the backend's ``refused_first_degree`` funnel counts.
    """
    from ..db import aio as adb
    from .outreach_channel import FIRST_DEGREE_REASON

    try:
        await adb.update_outreach(
            outreach_id, status="skipped",
            last_attempt_error=FIRST_DEGREE_REASON,
        )
        await adb.log_action(
            "first_degree_excluded", outreach_id=outreach_id,
            result="skipped",
            details={"prospect": name, "where": where, "campaign_id": campaign_id},
        )
    except Exception:
        logger.warning("Could not park excluded connection %s", outreach_id, exc_info=True)
    return (
        f"Skipped {name or outreach_id}: already a 1st-degree connection before "
        "this campaign started and the campaign has exclude_connections on."
    )
