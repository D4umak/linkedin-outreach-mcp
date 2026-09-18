"""SQLite schema creation and database access."""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import sys
import threading
import time

from .. import config

logger = logging.getLogger(__name__)

# Max retries and backoff for database-locked errors across processes.
# With 12 retries starting at 0.1s the total window is ~60s
# (0.1+0.2+0.4+...+204.8) matching busy_timeout=60000ms,
# plus random jitter (50%-150%) to avoid thundering-herd collisions.
_DB_RETRY_MAX = 12
_DB_RETRY_BACKOFF = 0.1  # seconds, doubles each attempt

# Stamped into PRAGMA user_version once a migration pass completes, and the
# reason an already-current database can skip the pass entirely. BUMP THIS
# whenever a migration is added to _apply_migrations(), or existing databases
# will never run it. test_migration_backup_gating.py fails if you forget.
# 2: idx_signals_status_detected, so the classifier's two ORDER BY detected_at
#    halves stop sorting the whole 'new' backlog into a temp B-tree per tick.
# 4: contacts.profile_json = '' normalised to NULL — json_extract raises on the
#    empty string and the raise aborts the statement, not the row.
# 5: global_contacts.profile_json holding a non-JSON blob normalised to NULL —
#    a redirect page read as "already enriched" and blocked the row for ever.
# 6: global_contacts.first_campaign_id pointing at a deleted campaign released —
#    dedup read it as "already in a campaign" and never let the person back in.
# 7: outreaches.chat_id (and headline A/B columns) — a v6 stamp skipped the
#    ALTER, so planning died with `no such column: o.chat_id`.
SCHEMA_VERSION = 12  # 12: connections.connected_at/removed_at; 11: outreach_tombstones; 10: agent_commons (beats + notes); 9: contacts.timezone (per-prospect planning windows); 8: versioned outreach sync

# Singleton connection — avoids opening multiple connections per process
# which causes "database is locked" errors with WAL mode.
_conn: sqlite3.Connection | None = None

# The connection this thread is still setting up, published before migrating.
# The migration pass reaches get_db() again on its way through — the engagements
# backfill calls get_account_id(), which reads a setting — and _conn is not
# assigned until the pass finishes, so the nested call used to open a second
# connection to the same file, store it in _conn, and then have the outer call
# overwrite it. Nothing ever closed the second one. Thread-local on purpose:
# other threads keep opening their own connection exactly as before.
_opening = threading.local()


class _UnclosableConnection:
    """Wrapper that prevents closing the shared singleton connection.

    Existing code calls db.close() after each query. With a singleton
    connection this would break subsequent queries. This wrapper makes
    close() a no-op while proxying everything else to the real connection.

    Additionally, execute(), executemany(), executescript(), and commit()
    automatically retry on "database is locked" errors with exponential
    backoff to handle concurrent MCP server processes.
    """

    __slots__ = ("_real",)

    def __init__(self, conn: sqlite3.Connection) -> None:
        object.__setattr__(self, "_real", conn)

    def close(self) -> None:  # noqa: D102
        pass  # No-op: keep the singleton alive

    # -- Retry wrappers for write operations --

    def _retry(self, method_name: str, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        import random

        real = object.__getattribute__(self, "_real")
        method = getattr(real, method_name)
        for attempt in range(_DB_RETRY_MAX):
            try:
                return method(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
                if attempt == _DB_RETRY_MAX - 1:
                    raise
                # Exponential backoff with jitter (50%-150%) to avoid thundering herd
                base_wait = _DB_RETRY_BACKOFF * (2 ** attempt)
                wait = base_wait * (0.5 + random.random())
                logger.warning("DB locked on %s, retry %d/%d after %.2fs", method_name, attempt + 1, _DB_RETRY_MAX, wait)
                time.sleep(wait)

    def execute(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        return self._retry("execute", *args, **kwargs)

    def executemany(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        return self._retry("executemany", *args, **kwargs)

    def executescript(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        return self._retry("executescript", *args, **kwargs)

    def commit(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        return self._retry("commit", *args, **kwargs)

    def __getattr__(self, name: str):  # noqa: ANN204
        return getattr(object.__getattribute__(self, "_real"), name)

    def __setattr__(self, name: str, value: object) -> None:
        setattr(object.__getattribute__(self, "_real"), name, value)


_SCHEMA_SQL = """
-- Core settings (voice signature, preferences, etc.)
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);

-- Campaigns
CREATE TABLE IF NOT EXISTS campaigns (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    icp_json    TEXT,
    status      TEXT NOT NULL DEFAULT 'draft',
    mode        TEXT NOT NULL DEFAULT 'autopilot',
    config_json TEXT,
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    updated_at  INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);

-- Contacts (prospects)
CREATE TABLE IF NOT EXISTS contacts (
    id            TEXT PRIMARY KEY,
    campaign_id   TEXT,
    name          TEXT,
    title         TEXT,
    company       TEXT,
    linkedin_url  TEXT,
    linkedin_id   TEXT,
    profile_json  TEXT,
    analysis_json TEXT,
    fit_score     REAL DEFAULT 0.0,
    status        TEXT NOT NULL DEFAULT 'pending',
    created_at    INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    updated_at    INTEGER,
    source        TEXT DEFAULT 'search',
    source_detail TEXT DEFAULT '',
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
);

-- Outreaches (one per contact per campaign)
CREATE TABLE IF NOT EXISTS outreaches (
    id              TEXT PRIMARY KEY,
    campaign_id     TEXT NOT NULL,
    contact_id      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    next_action     TEXT,
    scheduled_at    INTEGER,
    followup_count  INTEGER NOT NULL DEFAULT 0,
    outcome_json    TEXT,
    invited_at      INTEGER,
    accepted_at     INTEGER,
    first_reply_at  INTEGER,
    created_at      INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    updated_at      INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    chat_id         TEXT,
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id),
    FOREIGN KEY (contact_id) REFERENCES contacts(id)
);

-- Messages (conversation thread)
CREATE TABLE IF NOT EXISTS messages (
    id          TEXT PRIMARY KEY,
    outreach_id TEXT NOT NULL,
    role        TEXT NOT NULL,  -- 'sdr' or 'prospect'
    text        TEXT NOT NULL,
    sentiment   TEXT,
    read_at     INTEGER,  -- Unix timestamp when prospect read our message (NULL = unread/unknown)
    timestamp   INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    FOREIGN KEY (outreach_id) REFERENCES outreaches(id)
);

-- Actions log (audit trail)
CREATE TABLE IF NOT EXISTS actions_log (
    id           TEXT PRIMARY KEY,
    outreach_id  TEXT,
    action_type  TEXT NOT NULL,
    result       TEXT,
    details_json TEXT,
    timestamp    INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    FOREIGN KEY (outreach_id) REFERENCES outreaches(id)
);

-- Rate limits (per day)
CREATE TABLE IF NOT EXISTS rate_limits (
    id          TEXT PRIMARY KEY,
    date        TEXT NOT NULL UNIQUE,
    sent        INTEGER NOT NULL DEFAULT 0,
    accepted    INTEGER NOT NULL DEFAULT 0,
    daily_limit INTEGER NOT NULL DEFAULT 15,
    blocked     INTEGER NOT NULL DEFAULT 0,
    updated_at  INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);

-- Email rate limits (overflow channel — separate from LinkedIn limits)
CREATE TABLE IF NOT EXISTS email_rate_limits (
    id          TEXT PRIMARY KEY,
    date        TEXT NOT NULL UNIQUE,
    sent        INTEGER NOT NULL DEFAULT 0,
    updated_at  INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);

-- Usage tracking (free tier monthly counters)
CREATE TABLE IF NOT EXISTS usage (
    month              TEXT PRIMARY KEY,  -- 'YYYY-MM'
    invitations_sent   INTEGER NOT NULL DEFAULT 0,
    messages_sent      INTEGER NOT NULL DEFAULT 0,
    campaigns_created  INTEGER NOT NULL DEFAULT 0,
    icps_generated     INTEGER NOT NULL DEFAULT 0,
    engagements_sent   INTEGER NOT NULL DEFAULT 0,
    updated_at         INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);

-- ICPs (persisted Ideal Customer Profiles)
CREATE TABLE IF NOT EXISTS icps (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    icp_json    TEXT NOT NULL,      -- full IcpResult serialized as JSON
    target_desc TEXT,               -- original target description
    source_url  TEXT,               -- company URL if provided
    status      TEXT NOT NULL DEFAULT 'active',
    confidence  REAL DEFAULT 0.5,   -- best ICP confidence score
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    updated_at  INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);

-- ICP data sources (websites, text, KB)
CREATE TABLE IF NOT EXISTS icp_sources (
    id          TEXT PRIMARY KEY,
    icp_id      TEXT,
    source_type TEXT NOT NULL,      -- 'website', 'text', 'kb'
    uri         TEXT,
    title       TEXT,
    content_hash TEXT,
    chunk_count INTEGER DEFAULT 0,
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    FOREIGN KEY (icp_id) REFERENCES icps(id)
);

-- ICP chunks (for RAG, Phase 2+)
CREATE TABLE IF NOT EXISTS icp_chunks (
    id          TEXT PRIMARY KEY,
    source_id   TEXT NOT NULL,
    text        TEXT NOT NULL,
    ctx_text    TEXT,               -- chunk + surrounding context
    header_path TEXT DEFAULT '',
    position    INTEGER DEFAULT 0,
    embedding   BLOB,              -- numpy float32 array (Phase 3+)
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    FOREIGN KEY (source_id) REFERENCES icp_sources(id)
);

-- Engagements (post comments and reactions)
CREATE TABLE IF NOT EXISTS engagements (
    id            TEXT PRIMARY KEY,
    outreach_id   TEXT,
    action_type   TEXT NOT NULL,       -- 'comment' or 'react'
    post_id       TEXT NOT NULL,       -- LinkedIn post ID/URN
    post_text     TEXT,                -- Snapshot of the post text (for context)
    text          TEXT,                -- Comment text (null for reactions)
    reaction_type TEXT,                -- 'LIKE', 'CELEBRATE', etc. (null for comments)
    status        TEXT NOT NULL DEFAULT 'sent',  -- 'sent', 'pending_review', 'failed'
    reasoning     TEXT,                -- JSON: LLM reasoning for comment choice
    created_at    INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    FOREIGN KEY (outreach_id) REFERENCES outreaches(id)
);

-- Scheduler jobs (Sprint 17: autonomous scheduling)
CREATE TABLE IF NOT EXISTS scheduler_jobs (
    id           TEXT PRIMARY KEY,
    campaign_id  TEXT,              -- NULL for account-level (non-campaign) jobs
    outreach_id  TEXT,
    job_type     TEXT NOT NULL,     -- 'invite', 'followup', 'engage', 'check_replies'
    status       TEXT NOT NULL DEFAULT 'pending',  -- 'pending', 'running', 'completed', 'failed'
    scheduled_at INTEGER NOT NULL,
    started_at   INTEGER,
    completed_at INTEGER,
    retry_count  INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    created_at   INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id),
    FOREIGN KEY (outreach_id) REFERENCES outreaches(id)
);

-- Experiments (PM hypothesis tracking)
CREATE TABLE IF NOT EXISTS experiments (
    id          TEXT PRIMARY KEY,
    snapshot    TEXT NOT NULL,
    result_json TEXT NOT NULL,
    campaign_ids TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);

-- A/B Tests (controlled experiments on messaging variants and headlines)
CREATE TABLE IF NOT EXISTS ab_tests (
    id           TEXT PRIMARY KEY,
    campaign_id  TEXT NOT NULL,
    name         TEXT NOT NULL,          -- e.g., "Pain-first vs Question-first hook"
    hypothesis   TEXT,                   -- what we expect to learn
    variant_a    TEXT NOT NULL,          -- description of variant A (control)
    variant_b    TEXT NOT NULL,          -- description of variant B (treatment)
    test_type    TEXT NOT NULL DEFAULT 'message',  -- 'message' or 'headline'
    status       TEXT NOT NULL DEFAULT 'running',  -- running, completed, cancelled
    winner       TEXT,                   -- 'A', 'B', or 'inconclusive'
    result_json  TEXT,                   -- per-variant stats at completion
    created_at   INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    completed_at INTEGER,
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
);

-- Inbound signals (connection requests, unsolicited DMs, post comments)
CREATE TABLE IF NOT EXISTS inbound_signals (
    id                 TEXT PRIMARY KEY,
    signal_type        TEXT NOT NULL,       -- 'invitation', 'message', 'comment'
    sender_name        TEXT,
    sender_id          TEXT,                -- LinkedIn provider_id
    sender_headline    TEXT,
    sender_company     TEXT,
    sender_url         TEXT,                -- LinkedIn profile URL
    content            TEXT,                -- invite message, DM text, or comment text
    post_id            TEXT,                -- only for comments (our post)
    profile_json       TEXT,                -- full profile if fetched
    intent             TEXT,                -- 'buying_signal', 'networking', 'job_seeking', 'spam', 'partnership', 'unknown'
    matched_icp_id     TEXT,
    confidence         REAL DEFAULT 0.0,
    recommended_action TEXT,                -- 'engage_immediately', 'ask_purpose', 'accept_and_monitor', 'ignore', 'hold_for_operator'
    reasoning          TEXT,
    status             TEXT DEFAULT 'new',  -- 'new', 'classified', 'accepted', 'ignored', 'declined', 'engaged', 'converted', 'dismissed', 'held'
    campaign_id        TEXT,
    created_at         INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    qualified_at       INTEGER,
    invitation_id      TEXT,                -- Unipile invitation ID for accept/decline
    actioned_at        INTEGER,             -- when accept/decline/DM was executed
    decline_reason     TEXT,                -- why declined (for auditing)
    message_id         TEXT,                -- LinkedIn message ID (for reactions)
    reaction_sent      INTEGER DEFAULT 0,   -- was a reaction sent?
    FOREIGN KEY (matched_icp_id) REFERENCES icps(id)
);

-- Published posts (for comment monitoring)
CREATE TABLE IF NOT EXISTS published_posts (
    id           TEXT PRIMARY KEY,
    post_id      TEXT NOT NULL,              -- LinkedIn post ID/URN
    text         TEXT,                       -- post content snapshot
    topic        TEXT,
    published_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    last_checked INTEGER,                    -- last time we checked for comments
    comment_count INTEGER DEFAULT 0
);

-- Proactive signals (buying intent from prospect behavior)
CREATE TABLE IF NOT EXISTS signals (
    id             TEXT PRIMARY KEY,
    signal_type    TEXT NOT NULL,             -- 'keyword_mention', 'prospect_post', 'job_change',
                                             -- 'competitor_mention', 'hiring_surge', 'funding_event',
                                             -- 'news_event', 'profile_view', 'post_engagement',
                                             -- 'commenter_match', 'company_change', 'promotion',
                                             -- 'headline_change', 'headline_intent'
    source         TEXT NOT NULL,             -- 'keyword_search', 'prospect_scan', 'profile_scan',
                                             -- 'job_search', 'news_search', 'comment_xref'
    prospect_id    TEXT,                      -- FK to contacts.id (NULL if new prospect)
    prospect_name  TEXT,
    prospect_title TEXT,
    linkedin_id    TEXT,                      -- LinkedIn provider_id (for dedup + linking)
    campaign_id    TEXT,                      -- FK to campaigns.id (if linked to campaign)

    -- Signal content
    content        TEXT,                      -- Post text, job description, news snippet
    post_id        TEXT,                      -- LinkedIn post ID (if post-related)
    metadata_json  TEXT,                      -- Flexible: {keywords_matched, company, old_title, new_title, etc.}

    -- Classification
    intent         TEXT,                      -- 'buying_signal', 'pain_point', 'competitor_eval',
                                             -- 'job_seeking', 'thought_leadership', 'unknown'
    confidence     REAL DEFAULT 0.0,          -- 0.0-1.0 (from classifier)
    signal_score   REAL DEFAULT 0.0,          -- 0.0-1.0 (composite weighted score)
    reasoning      TEXT,

    -- State
    status         TEXT DEFAULT 'new',        -- 'new', 'classified', 'actioned', 'dismissed', 'expired'
    action_taken   TEXT,                      -- 'campaign_created', 'outreach_triggered', 'engaged', NULL
    actioned_at    INTEGER,
    expires_at     INTEGER,                   -- Signal freshness expiry (auto-decay)

    detected_at    INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    classified_at  INTEGER,

    FOREIGN KEY (prospect_id) REFERENCES contacts(id),
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
);

-- Signal watchlists (keyword monitoring configuration)
CREATE TABLE IF NOT EXISTS signal_watchlists (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,                -- e.g., "Competitor mentions", "Pain point keywords"
    watch_type  TEXT NOT NULL,                -- 'keyword', 'competitor', 'company', 'person', 'industry'
    keywords    TEXT NOT NULL,                -- JSON array: ["cold outreach", "SDR automation"]
    campaign_id TEXT,                         -- Optional: link signals to specific campaign
    is_active   INTEGER DEFAULT 1,
    last_polled_at INTEGER,                   -- Last time this watchlist was polled
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
);

-- Intent events (compound intent from stacked signals)
CREATE TABLE IF NOT EXISTS intent_events (
    id              TEXT PRIMARY KEY,
    linkedin_id     TEXT NOT NULL,
    company         TEXT,
    event_type      TEXT NOT NULL,             -- 'new_leader_building', 'active_evaluation',
                                               -- 'growth_mode', 'engaged_thought_leader',
                                               -- 'warm_inbound', 'multi_signal_hot'
    signal_ids      TEXT NOT NULL,             -- JSON array of contributing signal IDs
    signal_types    TEXT NOT NULL,             -- JSON array of contributing signal types
    composite_score REAL NOT NULL,
    action_taken    TEXT,                       -- 'outreach_created', 'priority_boosted', NULL
    detected_at     INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    expires_at      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_intent_events_linkedin ON intent_events(linkedin_id);
CREATE INDEX IF NOT EXISTS idx_intent_events_type ON intent_events(event_type);
CREATE INDEX IF NOT EXISTS idx_intent_events_score ON intent_events(composite_score DESC);

-- Signal accounts (aggregated signal scores per prospect)
CREATE TABLE IF NOT EXISTS signal_accounts (
    linkedin_id     TEXT PRIMARY KEY,
    prospect_name   TEXT,
    company         TEXT,
    total_signals   INTEGER DEFAULT 0,
    composite_score REAL DEFAULT 0.0,
    top_signal_type TEXT,                     -- Most impactful signal type
    last_signal_at  INTEGER,
    updated_at      INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);

-- Strategy patterns (cross-campaign learned patterns)
CREATE TABLE IF NOT EXISTS strategy_patterns (
    id                      TEXT PRIMARY KEY,
    pattern_type            TEXT NOT NULL,
    pattern_key             TEXT NOT NULL,
    description             TEXT,
    evidence_json           TEXT NOT NULL,
    confidence              REAL DEFAULT 0.0,
    estimated_revenue_impact REAL DEFAULT 0.0,
    acceptance_rate         REAL,
    reply_rate              REAL,
    conversion_rate         REAL,
    sample_size             INTEGER DEFAULT 0,
    status                  TEXT DEFAULT 'active',
    discovered_at           INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    last_validated          INTEGER,
    created_at              INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);

-- Strategy actions (audit trail of autonomous actions)
CREATE TABLE IF NOT EXISTS strategy_actions (
    id              TEXT PRIMARY KEY,
    action_type     TEXT NOT NULL,
    campaign_id     TEXT,
    pattern_id      TEXT,
    details_json    TEXT NOT NULL,
    outcome_json    TEXT,
    status          TEXT DEFAULT 'applied',
    created_at      INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    measured_at     INTEGER,
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id),
    FOREIGN KEY (pattern_id) REFERENCES strategy_patterns(id)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_experiments_created ON experiments(created_at);
CREATE INDEX IF NOT EXISTS idx_ab_tests_campaign ON ab_tests(campaign_id);
CREATE INDEX IF NOT EXISTS idx_ab_tests_status ON ab_tests(status);
CREATE INDEX IF NOT EXISTS idx_scheduler_jobs_status ON scheduler_jobs(status, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_scheduler_jobs_campaign ON scheduler_jobs(campaign_id);
CREATE INDEX IF NOT EXISTS idx_contacts_campaign ON contacts(campaign_id);
CREATE INDEX IF NOT EXISTS idx_contacts_linkedin_id ON contacts(linkedin_id);
CREATE INDEX IF NOT EXISTS idx_outreaches_campaign ON outreaches(campaign_id);
CREATE INDEX IF NOT EXISTS idx_outreaches_campaign_status ON outreaches(campaign_id, status);
CREATE INDEX IF NOT EXISTS idx_outreaches_contact ON outreaches(contact_id);
CREATE INDEX IF NOT EXISTS idx_outreaches_status ON outreaches(status);
CREATE INDEX IF NOT EXISTS idx_messages_outreach ON messages(outreach_id);
CREATE INDEX IF NOT EXISTS idx_actions_log_outreach ON actions_log(outreach_id);
CREATE INDEX IF NOT EXISTS idx_rate_limits_date ON rate_limits(date);
CREATE INDEX IF NOT EXISTS idx_icps_status ON icps(status);
CREATE INDEX IF NOT EXISTS idx_icp_sources_icp ON icp_sources(icp_id);
CREATE INDEX IF NOT EXISTS idx_icp_chunks_source ON icp_chunks(source_id);
CREATE INDEX IF NOT EXISTS idx_engagements_outreach ON engagements(outreach_id);
CREATE INDEX IF NOT EXISTS idx_engagements_created ON engagements(created_at);
CREATE INDEX IF NOT EXISTS idx_engagements_post_id ON engagements(post_id);
CREATE INDEX IF NOT EXISTS idx_campaigns_status ON campaigns(status);
CREATE INDEX IF NOT EXISTS idx_inbound_signals_status ON inbound_signals(status);
CREATE INDEX IF NOT EXISTS idx_inbound_signals_type ON inbound_signals(signal_type);
CREATE INDEX IF NOT EXISTS idx_inbound_signals_sender ON inbound_signals(sender_id);
CREATE INDEX IF NOT EXISTS idx_published_posts_post ON published_posts(post_id);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);
CREATE INDEX IF NOT EXISTS idx_signals_status_detected ON signals(status, detected_at);
CREATE INDEX IF NOT EXISTS idx_signals_type ON signals(signal_type);
CREATE INDEX IF NOT EXISTS idx_signals_prospect ON signals(linkedin_id);
CREATE INDEX IF NOT EXISTS idx_signals_detected ON signals(detected_at);
CREATE INDEX IF NOT EXISTS idx_signals_score ON signals(signal_score DESC);
CREATE INDEX IF NOT EXISTS idx_signals_action ON signals(action_taken);
CREATE INDEX IF NOT EXISTS idx_signal_watchlists_active ON signal_watchlists(is_active);
CREATE INDEX IF NOT EXISTS idx_signal_watchlists_campaign ON signal_watchlists(campaign_id);

CREATE INDEX IF NOT EXISTS idx_strategy_patterns_type ON strategy_patterns(pattern_type);
CREATE INDEX IF NOT EXISTS idx_strategy_patterns_status ON strategy_patterns(status);
CREATE INDEX IF NOT EXISTS idx_strategy_patterns_revenue ON strategy_patterns(estimated_revenue_impact);
CREATE INDEX IF NOT EXISTS idx_strategy_actions_campaign ON strategy_actions(campaign_id);
CREATE INDEX IF NOT EXISTS idx_strategy_actions_type ON strategy_actions(action_type);
CREATE INDEX IF NOT EXISTS idx_strategy_actions_status ON strategy_actions(status);

-- Posts (external posts encountered during engagement/scanning)
CREATE TABLE IF NOT EXISTS posts (
    id                 TEXT PRIMARY KEY,
    post_id            TEXT NOT NULL UNIQUE,
    author_linkedin_id TEXT,
    author_name        TEXT,
    text               TEXT,
    topic              TEXT,
    analysis_json      TEXT,
    metrics_json       TEXT,
    source             TEXT DEFAULT 'search',
    first_seen_at      INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    last_seen_at       INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_posts_post_id ON posts(post_id);
CREATE INDEX IF NOT EXISTS idx_posts_author ON posts(author_linkedin_id);
CREATE INDEX IF NOT EXISTS idx_posts_topic ON posts(topic);
CREATE INDEX IF NOT EXISTS idx_posts_seen ON posts(last_seen_at DESC);

-- Post authors (accumulated author intelligence)
CREATE TABLE IF NOT EXISTS post_authors (
    linkedin_id     TEXT PRIMARY KEY,
    name            TEXT,
    headline        TEXT,
    company         TEXT,
    posts_seen      INTEGER DEFAULT 1,
    topics_json     TEXT DEFAULT '[]',
    first_seen_at   INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    last_post_at    INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    contact_id      TEXT,
    FOREIGN KEY (contact_id) REFERENCES contacts(id)
);
CREATE INDEX IF NOT EXISTS idx_post_authors_company ON post_authors(company);
CREATE INDEX IF NOT EXISTS idx_post_authors_last ON post_authors(last_post_at DESC);

-- Contacts unique constraint (one contact per campaign per LinkedIn profile)
CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_campaign_linkedin
    ON contacts(campaign_id, linkedin_id)
    WHERE linkedin_id IS NOT NULL AND linkedin_id != '';

-- Outreaches unique constraint (one active outreach per contact per campaign)
CREATE UNIQUE INDEX IF NOT EXISTS idx_outreaches_campaign_contact
    ON outreaches(campaign_id, contact_id);

-- Communication Strategist: daily action plans per prospect
CREATE TABLE IF NOT EXISTS prospect_daily_plans (
    id              TEXT PRIMARY KEY,
    outreach_id     TEXT NOT NULL,
    campaign_id     TEXT NOT NULL,
    plan_date       TEXT NOT NULL,
    planned_actions TEXT NOT NULL DEFAULT '[]',
    executed_actions TEXT NOT NULL DEFAULT '[]',
    feedback_score  REAL,
    plan_source     TEXT DEFAULT 'llm',
    created_at      INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    updated_at      INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    FOREIGN KEY (outreach_id) REFERENCES outreaches(id),
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_plans_outreach_date
    ON prospect_daily_plans(outreach_id, plan_date);
CREATE INDEX IF NOT EXISTS idx_daily_plans_campaign_date
    ON prospect_daily_plans(campaign_id, plan_date);
CREATE INDEX IF NOT EXISTS idx_daily_plans_date
    ON prospect_daily_plans(plan_date);

-- Company page posts tracked for engagement monitoring (Phase 2 intent expansion)
CREATE TABLE IF NOT EXISTS company_posts_tracked (
    id                   TEXT PRIMARY KEY,
    watchlist_id         TEXT NOT NULL,
    post_id              TEXT NOT NULL,
    post_text            TEXT,
    last_checked_at      INTEGER,
    known_commenters     TEXT DEFAULT '[]',
    known_reactors       TEXT DEFAULT '[]',
    created_at           INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    FOREIGN KEY (watchlist_id) REFERENCES signal_watchlists(id)
);
CREATE INDEX IF NOT EXISTS idx_cpt_watchlist ON company_posts_tracked(watchlist_id);
CREATE INDEX IF NOT EXISTS idx_cpt_post_id ON company_posts_tracked(post_id);

-- Outreach rows hard-deleted here, kept until the hosted store has been told.
-- The push sends these as deleted_outreach_ids and clears them on a 2xx; the
-- pull refuses to re-adopt any id listed here.
CREATE TABLE IF NOT EXISTS outreach_tombstones (
    outreach_id TEXT PRIMARY KEY,
    campaign_id TEXT,
    deleted_at  INTEGER NOT NULL
);

-- In-process agent scratch (heartbeat + short notes for the next tick)
CREATE TABLE IF NOT EXISTS agent_commons (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    agent        TEXT NOT NULL,
    campaign_id  TEXT NOT NULL DEFAULT '',
    outreach_id  TEXT,
    body         TEXT,
    decision     TEXT,
    reason       TEXT,
    created_at   INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    expires_at   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_agent_commons_beat
    ON agent_commons(campaign_id, agent, kind);
CREATE INDEX IF NOT EXISTS idx_agent_commons_notes
    ON agent_commons(campaign_id, kind, expires_at);
"""


def get_db() -> sqlite3.Connection:
    """Return a singleton SQLite connection with WAL mode enabled.

    Re-uses the same connection across the process lifetime to avoid
    "database is locked" errors from multiple open connections competing
    for the write lock.

    Raises RuntimeError if called directly from the asyncio event loop
    thread.  Use ``db.aio`` or ``await run_db(fn, ...)`` instead.
    """
    # Enforce: sync DB calls must not run on the event loop thread.
    # run_db() executes in a ThreadPoolExecutor so it bypasses this check.
    try:
        asyncio.get_running_loop()
        if threading.current_thread() is threading.main_thread():
            import traceback
            tb = "".join(traceback.format_stack())
            raise RuntimeError(
                "Sync DB call on event loop thread — use `from ..db import aio as db` "
                "and `await db.func(...)`, or `await run_db(func, ...)`.\n"
                f"Call stack:\n{tb}"
            )
    except RuntimeError as e:
        if "event loop" in str(e).lower() and "Sync DB" in str(e):
            raise  # re-raise our own error
        pass  # no running event loop — sync context is fine

    global _conn
    if _conn is not None:
        return _conn

    # Re-entered on this thread while its connection is still being migrated.
    # Hand back the one already in flight rather than opening another.
    inflight = getattr(_opening, "conn", None)
    if inflight is not None:
        return inflight

    config.ensure_dirs()
    path = config.db_path()
    # Do not treat path.exists() as "the schema is present". A killed first
    # run leaves a WAL header and no tables; the next start would skip
    # _SCHEMA_SQL forever (F37).

    conn = sqlite3.connect(str(path), timeout=60, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")       # Safe for WAL; reduces fsync overhead
    conn.execute("PRAGMA wal_autocheckpoint=100")    # Checkpoint every ~400KB (default 1000 = ~4MB)

    # Published before migrating so a re-entrant get_db() on this thread gets
    # this connection instead of opening one that nothing would ever close.
    wrapped = _UnclosableConnection(conn)
    _opening.conn = wrapped
    try:
        _run_migrations(conn)

        # Compact WAL after migrations to prevent lock contention from large WAL files
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            pass  # Not critical — checkpoint will happen naturally
    finally:
        _opening.conn = None

    _conn = wrapped
    return _conn


def _schema_is_current(conn: sqlite3.Connection) -> bool:
    """True when this database is stamped at or ahead of this code's version.

    Ahead counts as current: migrations are strictly additive, so a database a
    newer heylead has stamped already holds everything an older pass could add.
    This used to be an equality check, and the pass stamped its own number on
    completion — so during a rollout, when old and new versions start processes
    interleaved for hours, each alternation "migrated" the database back and
    forth and took the full pre-migration backup on every turn. Observed
    21 Aug 2026 as nine ~700MB backups in one day.
    """
    try:
        row = conn.execute("PRAGMA user_version").fetchone()
    except sqlite3.Error:
        return False  # Can't tell — migrate, which is the safe direction.
    return bool(row) and row[0] >= SCHEMA_VERSION


def _stamp_schema_version(conn: sqlite3.Connection) -> None:
    """Record that this database is current. Only ever called after a full pass."""
    try:
        # PRAGMA takes no bound parameters; SCHEMA_VERSION is our own int.
        conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
        conn.commit()
    except sqlite3.Error:
        # Losing the stamp costs a repeated pass next time, not correctness.
        logger.warning("Could not stamp schema version", exc_info=True)


# Per-thread re-entrancy flag: a migration can reach get_db() again before the
# first one has published its connection. Serialises threads within the process;
# _MigrationLock serialises processes.
_migration_state = threading.local()
_migration_thread_lock = threading.Lock()


class _MigrationLock:
    """Blocking cross-process lock held across the migration pass.

    Two MCP servers starting together both saw an unstamped database and both
    copied it — observed 18 Aug 2026 as two 668MB backups 45ms apart. The loser
    of this lock waits, re-reads the stamp, and finds nothing left to do.

    Failing to take the lock is not a reason to skip migrating: we fall through
    and migrate unlocked, which is exactly the old behaviour.
    """

    def __init__(self) -> None:
        self._fd: int | None = None

    def __enter__(self) -> _MigrationLock:
        try:
            path = config.db_path().with_name("migration.lock")
            fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
        except OSError:
            logger.debug("No migration lock; migrating unlocked", exc_info=True)
            return self

        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX)
            self._fd = fd
        except OSError:
            os.close(fd)
            logger.debug("Could not take migration lock", exc_info=True)
        return self

    def __exit__(self, *exc: object) -> bool:
        if self._fd is not None:
            if sys.platform == "win32":
                try:
                    import msvcrt

                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            os.close(self._fd)  # closing the fd releases the flock
            self._fd = None
        return False


# Settings key holding the SCHEMA_VERSION whose pre-migration copy is already
# on disk. Deliberately not PRAGMA user_version: that one says "the pass
# finished", and the copy is bought at the top of the pass.
_BACKUP_CLAIM_KEY = "pre_migration_backup_version"


def _claim_backup_for_version(conn: sqlite3.Connection, version: int) -> bool:
    """Reserve the single pre-migration copy this SCHEMA_VERSION is entitled to.

    Committed before the copy starts and left standing if the pass then dies,
    which is the whole point: the stamp that closes the migration gate is
    written only once the pass returns, so an interrupted pass — a statement
    raising, an MCP server torn down mid-migration, a stamp write that lost to
    a writer — used to sell the next process start another ~670MB copy of the
    same database. Observed 21 Aug 2026 as five of them between 11:47 and 16:07
    BST, which is MAX_BACKUPS, i.e. a floor. The copy already on disk is also
    the better one: it predates the half-applied pass.

    BEGIN IMMEDIATE because the exclusion has to hold between processes. Module
    state cannot see the other ten `uvx heylead` servers, and the lock file is
    not enough on its own — flock quietly no-ops on some filesystems and
    _MigrationLock treats a failed open as "migrate unlocked".

    Returns True for the one caller that owes the copy.
    """
    try:
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.Error:
        logger.debug("No write lock to claim the backup with", exc_info=True)
        return True  # A spare copy beats no copy.

    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (_BACKUP_CLAIM_KEY,)
        ).fetchone()
        claimed = -1
        if row is not None:
            try:
                claimed = int(row[0])
            except (TypeError, ValueError):
                claimed = -1
        if claimed >= version:
            conn.rollback()
            return False

        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) "
            "VALUES (?, ?, strftime('%s', 'now'))",
            (_BACKUP_CLAIM_KEY, str(version)),
        )
        conn.commit()
        return True
    except sqlite3.Error:
        # No settings table on a database old enough to lack one, or the write
        # failed: fall back to copying, which is the old behaviour.
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        logger.debug("Could not record the backup claim", exc_info=True)
        return True


def _release_backup_claim(conn: sqlite3.Connection) -> None:
    """Hand the claim back when the copy it reserved never landed.

    Without this a create_backup() that raises (no space, permissions) would
    leave the version marked as copied and this database would migrate with no
    safety copy at all.
    """
    try:
        conn.execute("DELETE FROM settings WHERE key = ?", (_BACKUP_CLAIM_KEY,))
        conn.commit()
    except sqlite3.Error:
        logger.warning("Could not release the pre-migration backup claim", exc_info=True)


def _backup_before_migrating(conn: sqlite3.Connection) -> None:
    """One safety copy per SCHEMA_VERSION — if there is data to lose."""
    try:
        row = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()
    except sqlite3.Error:
        return  # Table may not exist yet on fresh DBs; nothing to copy.
    if not row or not row[0]:
        return

    if not _claim_backup_for_version(conn, SCHEMA_VERSION):
        logger.info(
            "Pre-migration backup for schema v%d already taken; not copying again",
            SCHEMA_VERSION,
        )
        return

    from .backup import create_backup, rotate_backups

    started = time.monotonic()
    logger.info(
        "Pre-migration backup starting: copying %s (%.1f MB) to %s. "
        "Large databases take minutes; progress is logged every %.0fs",
        config.db_path().name, _db_size_mb(), config.backups_dir(),
        _backup_progress_interval(),
    )
    try:
        path = create_backup("pre-migration")
        rotate_backups()
    except Exception:
        _release_backup_claim(conn)
        logger.warning(
            "Pre-migration backup failed after %.1fs", time.monotonic() - started,
            exc_info=True,
        )
        return
    size_mb = path.stat().st_size / (1024 * 1024) if path and path.exists() else 0.0
    logger.info(
        "Pre-migration backup complete: %s (%.1f MB) in %.1fs",
        path.name if path else "(no file)", size_mb, time.monotonic() - started,
    )


def _db_size_mb() -> float:
    """Main file plus WAL: what a copy has to read."""
    total = 0
    base = config.db_path()
    for p in (base, base.with_name(base.name + "-wal")):
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return total / (1024 * 1024)


def _backup_progress_interval() -> float:
    from .backup import BACKUP_PROGRESS_LOG_SECONDS

    return BACKUP_PROGRESS_LOG_SECONDS


# A statement slower than this is logged at INFO; every other step at DEBUG.
_SLOW_MIGRATION_STEP_SECONDS = 1.0


class _TimedMigrationConnection:
    """Passes everything to the real connection; times and logs each statement.

    _apply_migrations is ~1,400 lines of independent try/except blocks, so the
    step boundary it actually has is the statement. Logging there covers every
    migration, including ones added later, without touching each block.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.steps = 0
        self.total_seconds = 0.0
        self.slowest_seconds = 0.0
        self.slowest_sql = ""

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def _timed(self, fn, sql, *args, **kwargs):
        started = time.monotonic()
        try:
            return fn(sql, *args, **kwargs)
        finally:
            elapsed = time.monotonic() - started
            self.steps += 1
            self.total_seconds += elapsed
            label = " ".join(str(sql).split())[:160]
            if elapsed > self.slowest_seconds:
                self.slowest_seconds, self.slowest_sql = elapsed, label
            logger.log(
                logging.INFO if elapsed >= _SLOW_MIGRATION_STEP_SECONDS else logging.DEBUG,
                "Migration step %d (%.2fs): %s", self.steps, elapsed, label,
            )

    def execute(self, sql, *args, **kwargs):
        return self._timed(self._conn.execute, sql, *args, **kwargs)

    def executemany(self, sql, *args, **kwargs):
        return self._timed(self._conn.executemany, sql, *args, **kwargs)

    def executescript(self, sql, *args, **kwargs):
        return self._timed(self._conn.executescript, sql, *args, **kwargs)


def _stamped_version(conn: sqlite3.Connection) -> int | None:
    try:
        row = conn.execute("PRAGMA user_version").fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Migrate the database, copying it first if anything will actually change.

    A database already stamped current skips the pass outright. That matters
    more than it reads: every migration below is an idempotent
    `ALTER TABLE ... except OperationalError: pass`, so on a current schema the
    pass altered nothing and the full-database copy at the top of it was the
    only work being done — once per process start, and a working machine tends
    to have around ten `uvx heylead` MCP servers alive at once.

    A database stamped ahead of this code is also skipped, and must be: the
    pass is only nominally a no-op — it still copies the whole database first.
    Re-stamping the older number would then re-arm the newer version's pass,
    and during a rollout the two sides alternate for hours (pinned installs,
    uvx caches, dev checkouts), paying a full backup per alternation.

    Reaching the pass is not the same as owing the copy: the pass re-runs
    whenever it does not finish, and _backup_before_migrating() copies only for
    the SCHEMA_VERSION that has not been copied yet.
    """
    if getattr(_migration_state, "active", False):
        # Re-entered on this thread: a migration below reached get_db() (the
        # engagements backfill calls get_account_id(), which reads a setting),
        # and get_db() publishes its connection only after migrating — so it
        # opened a second connection and arrived back here. The outer pass owns
        # the work; recursing would deadlock on the lock it already holds and
        # would take a second backup on the way.
        return

    if _schema_is_current(conn) and _core_tables_present(conn):
        return

    _migration_state.active = True
    try:
        # flock would serialise threads on its own — it contends across
        # separate file descriptions even inside one process — but the
        # in-process lock still covers the paths where the file lock is
        # unavailable: the open fails, or a filesystem no-ops flock.
        with _migration_thread_lock, _MigrationLock():
            _create_schema_if_missing(conn)
            # Someone may have finished the pass while we waited for either.
            if _schema_is_current(conn):
                return
            # Logged before anything slow starts: the pass holds no scheduler
            # lock and can run for minutes on a large file, and with nothing
            # in the log that is indistinguishable from a hang (10 Sep 2026).
            started = time.monotonic()
            logger.info(
                "Database migration starting: %s (%.1f MB), schema v%s -> v%d",
                config.db_path(), _db_size_mb(), _stamped_version(conn),
                SCHEMA_VERSION,
            )
            _backup_before_migrating(conn)
            backup_seconds = time.monotonic() - started
            logger.info("Applying schema migrations")
            timed = _TimedMigrationConnection(conn)
            try:
                _apply_migrations(timed)  # type: ignore[arg-type]
            except BaseException:
                logger.warning(
                    "Database migration failed after %.1fs, %d steps in; "
                    "not stamped, it will run again on the next open",
                    time.monotonic() - started, timed.steps, exc_info=True,
                )
                raise
            _stamp_schema_version(conn)
            logger.info(
                "Database migration complete in %.1fs (backup %.1fs; %d "
                "migration steps in %.1fs; slowest %.2fs: %s)",
                time.monotonic() - started, backup_seconds, timed.steps,
                timed.total_seconds, timed.slowest_seconds, timed.slowest_sql,
            )
    finally:
        _migration_state.active = False


def _reraise_if_migration_interrupted(exc: sqlite3.OperationalError) -> None:
    """Locked/busy is not 'column already exists'. Re-raise so the pass is not stamped."""
    msg = str(exc).lower()
    if "locked" in msg or "busy" in msg:
        raise exc


def _core_tables_present(conn: sqlite3.Connection) -> bool:
    """True when this file already has the schema, not merely a WAL header."""
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='contacts'"
        ).fetchone()
    except sqlite3.Error:
        return False
    return bool(row)


def _create_schema_if_missing(conn: sqlite3.Connection) -> None:
    """Apply `_SCHEMA_SQL` to a file that exists but has no tables (F37)."""
    if _core_tables_present(conn):
        return
    if config.config_path().exists():
        logger.warning(
            "Database was recreated but config already exists — "
            "previous data may have been lost. "
            "Check ~/.heylead/backups/ for recovery options."
        )
    logger.info("Creating new database at %s", config.db_path())
    conn.executescript(_SCHEMA_SQL)
    conn.commit()


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply schema migrations to existing databases.

    Every statement here must stay idempotent — the pass re-runs in full
    whenever SCHEMA_VERSION moves. Adding one means bumping SCHEMA_VERSION, or
    existing databases never see it.
    """
    # Sprint 2: Add followup_count to outreaches
    try:
        conn.execute("ALTER TABLE outreaches ADD COLUMN followup_count INTEGER NOT NULL DEFAULT 0")
        conn.commit()
        logger.info("Migration: added followup_count to outreaches")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        pass  # Column already exists

    # Sprint 3: Add engagements table (for existing DBs created before Sprint 3)
    try:
        conn.execute("SELECT 1 FROM engagements LIMIT 0")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS engagements (
                id            TEXT PRIMARY KEY,
                outreach_id   TEXT,
                action_type   TEXT NOT NULL,
                post_id       TEXT NOT NULL,
                post_text     TEXT,
                text          TEXT,
                reaction_type TEXT,
                status        TEXT NOT NULL DEFAULT 'sent',
                reasoning     TEXT,
                created_at    INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                FOREIGN KEY (outreach_id) REFERENCES outreaches(id)
            );
            CREATE INDEX IF NOT EXISTS idx_engagements_outreach ON engagements(outreach_id);
            CREATE INDEX IF NOT EXISTS idx_engagements_created ON engagements(created_at);
        """)
        conn.commit()
        logger.info("Migration: added engagements table")

    # Sprint 3: Add engagements_sent to usage
    try:
        conn.execute("ALTER TABLE usage ADD COLUMN engagements_sent INTEGER NOT NULL DEFAULT 0")
        conn.commit()
        logger.info("Migration: added engagements_sent to usage")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        pass  # Column already exists

    # Sprint 12: Add outcome_json to outreaches (close reason, notes, booking)
    try:
        conn.execute("ALTER TABLE outreaches ADD COLUMN outcome_json TEXT")
        conn.commit()
        logger.info("Migration: added outcome_json to outreaches")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        pass  # Column already exists

    # Sprint 14: Add context_json to campaigns (offerings, case_studies, social_proofs, preferences)
    try:
        conn.execute("ALTER TABLE campaigns ADD COLUMN context_json TEXT")
        conn.commit()
        logger.info("Migration: added context_json to campaigns")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        pass  # Column already exists

    # Sprint 16: Add memory_json to outreaches (structured follow-up decisions)
    try:
        conn.execute("ALTER TABLE outreaches ADD COLUMN memory_json TEXT")
        conn.commit()
        logger.info("Migration: added memory_json to outreaches")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        pass  # Column already exists

    # Sprint 21: Add event timestamps to outreaches
    for col in ("invited_at", "accepted_at", "first_reply_at"):
        try:
            conn.execute(f"ALTER TABLE outreaches ADD COLUMN {col} INTEGER")
            conn.commit()
            logger.info(f"Migration: added {col} to outreaches")
        except sqlite3.OperationalError as e:
            _reraise_if_migration_interrupted(e)
            pass  # Column already exists

    # There was a backfill here that estimated invited_at/accepted_at/first_reply_at
    # from status — invited_at = created_at for anything not pending or skipped,
    # the other two = updated_at. Do not bring it back. It manufactured events
    # that never happened: an outreach whose invite errored got an invite time,
    # and updated_at, which only means "when the row was last touched", became an
    # acceptance and a reply time. It also ran on every init rather than once, so
    # it re-created values a repair had just cleared. On the live DB it accounted
    # for 1,540 invited_at with no invite behind them (970 of those error rows)
    # and every invited bar on the 30-day chart.
    #
    # These columns are stamped by update_outreach when the event is observed.
    # A NULL means it did not happen, which is the honest answer.

    # Sprint 17: Add scheduler_jobs table (for existing DBs created before Sprint 17)
    try:
        conn.execute("SELECT 1 FROM scheduler_jobs LIMIT 0")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS scheduler_jobs (
                id           TEXT PRIMARY KEY,
                campaign_id  TEXT,
                outreach_id  TEXT,
                job_type     TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'pending',
                scheduled_at INTEGER NOT NULL,
                started_at   INTEGER,
                completed_at INTEGER,
                retry_count  INTEGER NOT NULL DEFAULT 0,
                error        TEXT,
                created_at   INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                FOREIGN KEY (campaign_id) REFERENCES campaigns(id),
                FOREIGN KEY (outreach_id) REFERENCES outreaches(id)
            );
            CREATE INDEX IF NOT EXISTS idx_scheduler_jobs_status ON scheduler_jobs(status, scheduled_at);
            CREATE INDEX IF NOT EXISTS idx_scheduler_jobs_campaign ON scheduler_jobs(campaign_id);
        """)
        conn.commit()
        logger.info("Migration: added scheduler_jobs table")

    # Feature 1.4: Add read_at to messages (read receipt tracking)
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN read_at INTEGER")
        conn.commit()
        logger.info("Migration: added read_at to messages")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        pass  # Column already exists

    # Sprint 22: Add experiments table
    try:
        conn.execute("SELECT 1 FROM experiments LIMIT 0")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS experiments (
                id TEXT PRIMARY KEY, snapshot TEXT NOT NULL, result_json TEXT NOT NULL,
                campaign_ids TEXT, status TEXT NOT NULL DEFAULT 'pending',
                created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
            );
            CREATE INDEX IF NOT EXISTS idx_experiments_created ON experiments(created_at);
        """)
        conn.commit()
        logger.info("Migration: added experiments table")


    # Feature 3.2: Add variant column to outreaches (A/B testing)
    try:
        conn.execute("ALTER TABLE outreaches ADD COLUMN variant TEXT")
        conn.commit()
        logger.info("Migration: added variant to outreaches")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        pass  # Column already exists

    # Feature 3.2: Add ab_tests table
    try:
        conn.execute("SELECT 1 FROM ab_tests LIMIT 0")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS ab_tests (
                id           TEXT PRIMARY KEY,
                campaign_id  TEXT NOT NULL,
                name         TEXT NOT NULL,
                hypothesis   TEXT,
                variant_a    TEXT NOT NULL,
                variant_b    TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'running',
                winner       TEXT,
                result_json  TEXT,
                created_at   INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                completed_at INTEGER,
                FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
            );
            CREATE INDEX IF NOT EXISTS idx_ab_tests_campaign ON ab_tests(campaign_id);
            CREATE INDEX IF NOT EXISTS idx_ab_tests_status ON ab_tests(status);
        """)
        conn.commit()
        logger.info("Migration: added ab_tests table")


    # Feature 3.5: Add channel column to outreaches (linkedin/email)
    try:
        conn.execute("ALTER TABLE outreaches ADD COLUMN channel TEXT DEFAULT 'linkedin'")
        conn.commit()
        logger.info("Migration: added channel to outreaches")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        pass  # Column already exists

    # Feature 3.5: Add channel column to messages
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN channel TEXT DEFAULT 'linkedin'")
        conn.commit()
        logger.info("Migration: added channel to messages")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        pass  # Column already exists

    # Feature 4.1: Add source column to contacts (search/import/discovery)
    try:
        conn.execute("ALTER TABLE contacts ADD COLUMN source TEXT DEFAULT 'search'")
        conn.commit()
        logger.info("Migration: added source to contacts")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        pass  # Column already exists

    # Feature 4.4: CRM mappings table for HubSpot integration
    conn.execute("""
        CREATE TABLE IF NOT EXISTS crm_mappings (
            id              TEXT PRIMARY KEY,
            contact_id      TEXT NOT NULL,
            crm_type        TEXT NOT NULL DEFAULT 'hubspot',
            crm_contact_id  TEXT,
            crm_deal_id     TEXT,
            synced_at       INTEGER,
            created_at      INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
            FOREIGN KEY (contact_id) REFERENCES contacts(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_crm_mappings_contact ON crm_mappings(contact_id)")
    conn.commit()

    # Inbound pipeline: inbound_signals table
    try:
        conn.execute("SELECT 1 FROM inbound_signals LIMIT 0")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS inbound_signals (
                id                 TEXT PRIMARY KEY,
                signal_type        TEXT NOT NULL,
                sender_name        TEXT,
                sender_id          TEXT,
                sender_headline    TEXT,
                sender_company     TEXT,
                sender_url         TEXT,
                content            TEXT,
                post_id            TEXT,
                profile_json       TEXT,
                intent             TEXT,
                matched_icp_id     TEXT,
                confidence         REAL DEFAULT 0.0,
                recommended_action TEXT,
                reasoning          TEXT,
                status             TEXT DEFAULT 'new',
                campaign_id        TEXT,
                created_at         INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                qualified_at       INTEGER,
                FOREIGN KEY (matched_icp_id) REFERENCES icps(id)
            );
            CREATE INDEX IF NOT EXISTS idx_inbound_signals_status ON inbound_signals(status);
            CREATE INDEX IF NOT EXISTS idx_inbound_signals_type ON inbound_signals(signal_type);
            CREATE INDEX IF NOT EXISTS idx_inbound_signals_sender ON inbound_signals(sender_id);
        """)
        conn.commit()
        logger.info("Migration: added inbound_signals table")

    # Inbound pipeline: published_posts table
    try:
        conn.execute("SELECT 1 FROM published_posts LIMIT 0")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS published_posts (
                id           TEXT PRIMARY KEY,
                post_id      TEXT NOT NULL,
                text         TEXT,
                topic        TEXT,
                published_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                last_checked INTEGER,
                comment_count INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_published_posts_post ON published_posts(post_id);
        """)
        conn.commit()
        logger.info("Migration: added published_posts table")

    # Signal-based selling: signals table (v1.0)
    try:
        conn.execute("SELECT 1 FROM signals LIMIT 0")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS signals (
                id             TEXT PRIMARY KEY,
                signal_type    TEXT NOT NULL,
                source         TEXT NOT NULL,
                prospect_id    TEXT,
                prospect_name  TEXT,
                prospect_title TEXT,
                linkedin_id    TEXT,
                campaign_id    TEXT,
                content        TEXT,
                post_id        TEXT,
                metadata_json  TEXT,
                intent         TEXT,
                confidence     REAL DEFAULT 0.0,
                signal_score   REAL DEFAULT 0.0,
                reasoning      TEXT,
                status         TEXT DEFAULT 'new',
                action_taken   TEXT,
                actioned_at    INTEGER,
                expires_at     INTEGER,
                detected_at    INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                classified_at  INTEGER,
                FOREIGN KEY (prospect_id) REFERENCES contacts(id),
                FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
            );
            CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);
            CREATE INDEX IF NOT EXISTS idx_signals_type ON signals(signal_type);
            CREATE INDEX IF NOT EXISTS idx_signals_prospect ON signals(linkedin_id);
            CREATE INDEX IF NOT EXISTS idx_signals_detected ON signals(detected_at);
            CREATE INDEX IF NOT EXISTS idx_signals_score ON signals(signal_score DESC);
            CREATE INDEX IF NOT EXISTS idx_signals_action ON signals(action_taken);
        """)
        conn.commit()
        logger.info("Migration: added signals table")

    # Signal-based selling: signal_watchlists table (v1.0)
    try:
        conn.execute("SELECT 1 FROM signal_watchlists LIMIT 0")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS signal_watchlists (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                watch_type  TEXT NOT NULL,
                keywords    TEXT NOT NULL,
                campaign_id TEXT,
                is_active   INTEGER DEFAULT 1,
                last_polled_at INTEGER,
                created_at  INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
            );
            CREATE INDEX IF NOT EXISTS idx_signal_watchlists_active ON signal_watchlists(is_active);
            CREATE INDEX IF NOT EXISTS idx_signal_watchlists_campaign ON signal_watchlists(campaign_id);
        """)
        conn.commit()
        logger.info("Migration: added signal_watchlists table")

    # Signal-based selling: signal_accounts table (v1.0)
    try:
        conn.execute("SELECT 1 FROM signal_accounts LIMIT 0")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS signal_accounts (
                linkedin_id     TEXT PRIMARY KEY,
                prospect_name   TEXT,
                company         TEXT,
                total_signals   INTEGER DEFAULT 0,
                composite_score REAL DEFAULT 0.0,
                top_signal_type TEXT,
                last_signal_at  INTEGER,
                updated_at      INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
            );
        """)
        conn.commit()
        logger.info("Migration: added signal_accounts table")

    # Signal-based selling: last_scanned_at on contacts (v1.0)
    try:
        conn.execute("ALTER TABLE contacts ADD COLUMN last_scanned_at INTEGER")
        conn.commit()
        logger.info("Migration: added last_scanned_at to contacts")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        pass  # Column already exists

    # Signal activation: signal_id on outreaches (v1.1)
    try:
        conn.execute("ALTER TABLE outreaches ADD COLUMN signal_id TEXT")
        conn.commit()
        logger.info("Migration: added signal_id to outreaches")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        pass  # Column already exists

    # Strategy engine: estimated_revenue on contacts
    try:
        conn.execute("ALTER TABLE contacts ADD COLUMN estimated_revenue REAL DEFAULT 0.0")
        conn.commit()
        logger.info("Migration: added estimated_revenue to contacts")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        pass  # Column already exists

    # Communication strategist: per-prospect IANA timezone, inferred from the
    # LinkedIn location at planning time so morning/afternoon/evening windows
    # are the prospect's, not the user's.
    try:
        conn.execute("ALTER TABLE contacts ADD COLUMN timezone TEXT DEFAULT ''")
        conn.commit()
        logger.info("Migration: added timezone to contacts")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        pass  # Column already exists

    # Strategy engine: strategy_json on outreaches
    try:
        conn.execute("ALTER TABLE outreaches ADD COLUMN strategy_json TEXT")
        conn.commit()
        logger.info("Migration: added strategy_json to outreaches")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        pass  # Column already exists

    # Versioned Outreach Sync (Phase 2): the cloud's funnel clock, mirrored,
    # plus the unpushed-local-fact flag. See docs in services/cloud_sync.py.
    for _vcol, _vtype in (
        ("cloud_status_version", "INTEGER NOT NULL DEFAULT 0"),
        ("local_news", "INTEGER NOT NULL DEFAULT 0"),
    ):
        try:
            conn.execute(f"ALTER TABLE outreaches ADD COLUMN {_vcol} {_vtype}")
            conn.commit()
            logger.info("Migration: added %s to outreaches", _vcol)
        except sqlite3.OperationalError as e:
            _reraise_if_migration_interrupted(e)
            pass  # Column already exists

    # Strategy engine: spawned_by on campaigns
    try:
        conn.execute("ALTER TABLE campaigns ADD COLUMN spawned_by TEXT")
        conn.commit()
        logger.info("Migration: added spawned_by to campaigns")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        pass  # Column already exists

    # Strategy engine: strategy_patterns table
    try:
        conn.execute("SELECT 1 FROM strategy_patterns LIMIT 0")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS strategy_patterns (
                id                      TEXT PRIMARY KEY,
                pattern_type            TEXT NOT NULL,
                pattern_key             TEXT NOT NULL,
                description             TEXT,
                evidence_json           TEXT NOT NULL,
                confidence              REAL DEFAULT 0.0,
                estimated_revenue_impact REAL DEFAULT 0.0,
                acceptance_rate         REAL,
                reply_rate              REAL,
                conversion_rate         REAL,
                sample_size             INTEGER DEFAULT 0,
                status                  TEXT DEFAULT 'active',
                discovered_at           INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                last_validated          INTEGER,
                created_at              INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
            );
            CREATE INDEX IF NOT EXISTS idx_strategy_patterns_type ON strategy_patterns(pattern_type);
            CREATE INDEX IF NOT EXISTS idx_strategy_patterns_status ON strategy_patterns(status);
            CREATE INDEX IF NOT EXISTS idx_strategy_patterns_revenue ON strategy_patterns(estimated_revenue_impact);
        """)
        conn.commit()
        logger.info("Migration: added strategy_patterns table")

    # Strategy engine: strategy_actions table
    try:
        conn.execute("SELECT 1 FROM strategy_actions LIMIT 0")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS strategy_actions (
                id              TEXT PRIMARY KEY,
                action_type     TEXT NOT NULL,
                campaign_id     TEXT,
                pattern_id      TEXT,
                details_json    TEXT NOT NULL,
                outcome_json    TEXT,
                status          TEXT DEFAULT 'applied',
                created_at      INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                measured_at     INTEGER,
                FOREIGN KEY (campaign_id) REFERENCES campaigns(id),
                FOREIGN KEY (pattern_id) REFERENCES strategy_patterns(id)
            );
            CREATE INDEX IF NOT EXISTS idx_strategy_actions_campaign ON strategy_actions(campaign_id);
            CREATE INDEX IF NOT EXISTS idx_strategy_actions_type ON strategy_actions(action_type);
            CREATE INDEX IF NOT EXISTS idx_strategy_actions_status ON strategy_actions(status);
        """)
        conn.commit()
        logger.info("Migration: added strategy_actions table")


    # Tracking fix: add outreach_id to inbound_signals for message tracking
    try:
        conn.execute("ALTER TABLE inbound_signals ADD COLUMN outreach_id TEXT")
        conn.commit()
        logger.info("Migration: added outreach_id to inbound_signals")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        pass  # Column already exists

    # Tracking fix: invite attempt tracking on outreaches
    for col, ctype in [("invite_attempts", "INTEGER NOT NULL DEFAULT 0"),
                        ("last_attempt_error", "TEXT")]:
        try:
            conn.execute(f"ALTER TABLE outreaches ADD COLUMN {col} {ctype}")
            conn.commit()
            logger.info("Migration: added %s to outreaches", col)
        except sqlite3.OperationalError as e:
            _reraise_if_migration_interrupted(e)
            pass  # Column already exists

    # Tracking fix: campaign_id on engagements for manual engagement linking
    try:
        conn.execute("ALTER TABLE engagements ADD COLUMN campaign_id TEXT")
        conn.commit()
        logger.info("Migration: added campaign_id to engagements")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        pass  # Column already exists

    # Compound intent events table (Week 10)
    try:
        conn.execute("SELECT 1 FROM intent_events LIMIT 0")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS intent_events (
                id              TEXT PRIMARY KEY,
                linkedin_id     TEXT NOT NULL,
                company         TEXT,
                event_type      TEXT NOT NULL,
                signal_ids      TEXT NOT NULL,
                signal_types    TEXT NOT NULL,
                composite_score REAL NOT NULL,
                action_taken    TEXT,
                detected_at     INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                expires_at      INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_intent_events_linkedin ON intent_events(linkedin_id);
            CREATE INDEX IF NOT EXISTS idx_intent_events_type ON intent_events(event_type);
            CREATE INDEX IF NOT EXISTS idx_intent_events_score ON intent_events(composite_score DESC);
        """)
        conn.commit()
        logger.info("Migration: added intent_events table")


    # Voice memo support: format column on messages (voice memo sprint)
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN format TEXT DEFAULT 'text'")
        conn.commit()
        logger.info("Migration: added format column to messages")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        pass  # Column already exists

    # Fix: add updated_at to contacts (was missing from original schema)
    try:
        conn.execute("ALTER TABLE contacts ADD COLUMN updated_at INTEGER")
        conn.commit()
        logger.info("Migration: added updated_at to contacts")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        pass  # Column already exists

    # Fix: convert empty-string campaign_id to NULL in scheduler_jobs
    # (global jobs used "" which violated FK constraint)
    try:
        updated = conn.execute(
            "UPDATE scheduler_jobs SET campaign_id = NULL WHERE campaign_id = ''"
        ).rowcount
        if updated:
            conn.commit()
            logger.info("Migration: converted %d scheduler_jobs campaign_id '' → NULL", updated)
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        pass

    # Partner follow-up tracking table
    try:
        conn.execute("SELECT 1 FROM partner_followups LIMIT 0")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS partner_followups (
                id              TEXT PRIMARY KEY,
                name            TEXT NOT NULL,
                company         TEXT DEFAULT '',
                email           TEXT DEFAULT '',
                context         TEXT DEFAULT '',
                status          TEXT DEFAULT 'active',
                followup_count  INTEGER DEFAULT 0,
                next_followup   INTEGER,
                last_contacted  INTEGER,
                notes           TEXT DEFAULT '[]',
                created_at      INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                updated_at      INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
            );
            CREATE INDEX IF NOT EXISTS idx_partner_followups_status ON partner_followups(status);
            CREATE INDEX IF NOT EXISTS idx_partner_followups_next ON partner_followups(next_followup);
        """)
        conn.commit()
        logger.info("Migration: added partner_followups table")

    # Fix: make scheduler_jobs.campaign_id nullable for account-level jobs
    # Old migration created it as NOT NULL; recreate table with correct schema
    try:
        col_info = conn.execute("PRAGMA table_info(scheduler_jobs)").fetchall()
        for col in col_info:
            if col[1] == "campaign_id" and col[3] == 1:  # notnull=1
                conn.executescript("""
                    CREATE TABLE scheduler_jobs_new (
                        id           TEXT PRIMARY KEY,
                        campaign_id  TEXT,
                        outreach_id  TEXT,
                        job_type     TEXT NOT NULL,
                        status       TEXT NOT NULL DEFAULT 'pending',
                        scheduled_at INTEGER NOT NULL,
                        started_at   INTEGER,
                        completed_at INTEGER,
                        retry_count  INTEGER NOT NULL DEFAULT 0,
                        error        TEXT,
                        created_at   INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                        FOREIGN KEY (campaign_id) REFERENCES campaigns(id),
                        FOREIGN KEY (outreach_id) REFERENCES outreaches(id)
                    );
                    INSERT INTO scheduler_jobs_new SELECT * FROM scheduler_jobs;
                    DROP TABLE scheduler_jobs;
                    ALTER TABLE scheduler_jobs_new RENAME TO scheduler_jobs;
                    CREATE INDEX IF NOT EXISTS idx_scheduler_jobs_status ON scheduler_jobs(status, scheduled_at);
                    CREATE INDEX IF NOT EXISTS idx_scheduler_jobs_campaign ON scheduler_jobs(campaign_id);
                """)
                conn.commit()
                logger.info("Migration: made scheduler_jobs.campaign_id nullable")
                break
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        pass

    # Profile change history: track all LinkedIn profile edits with restore
    try:
        conn.execute("SELECT 1 FROM profile_changes LIMIT 0")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS profile_changes (
                id          TEXT PRIMARY KEY,
                field       TEXT NOT NULL,
                old_value   TEXT,
                new_value   TEXT NOT NULL,
                source      TEXT DEFAULT 'manual',
                status      TEXT DEFAULT 'applied',
                created_at  INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
            );
            CREATE INDEX IF NOT EXISTS idx_profile_changes_field ON profile_changes(field);
            CREATE INDEX IF NOT EXISTS idx_profile_changes_created ON profile_changes(created_at DESC);
        """)
        conn.commit()
        logger.info("Migration: added profile_changes table")

    # Headline A/B testing: add test_type column to ab_tests
    try:
        conn.execute("SELECT test_type FROM ab_tests LIMIT 0")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        conn.execute("ALTER TABLE ab_tests ADD COLUMN test_type TEXT NOT NULL DEFAULT 'message'")
        conn.commit()
        logger.info("Migration: added test_type column to ab_tests")

    # Observability: add duration_ms to scheduler_jobs for execution time tracking
    try:
        conn.execute("SELECT duration_ms FROM scheduler_jobs LIMIT 0")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        conn.execute("ALTER TABLE scheduler_jobs ADD COLUMN duration_ms INTEGER")
        conn.commit()
        logger.info("Migration: added duration_ms column to scheduler_jobs")

    # Observability: scheduler_events table for structured event logging
    try:
        conn.execute("SELECT 1 FROM scheduler_events LIMIT 0")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS scheduler_events (
                id          TEXT PRIMARY KEY,
                event_type  TEXT NOT NULL,
                campaign_id TEXT,
                outreach_id TEXT,
                job_id      TEXT,
                context     TEXT DEFAULT '{}',
                duration_ms INTEGER,
                created_at  INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
            );
            CREATE INDEX IF NOT EXISTS idx_sched_evt_type ON scheduler_events(event_type);
            CREATE INDEX IF NOT EXISTS idx_sched_evt_time ON scheduler_events(created_at);
        """)
        conn.commit()
        logger.info("Migration: added scheduler_events table")

    # Engagement dedup: add account_id to track which LinkedIn account made each engagement
    try:
        conn.execute("SELECT account_id FROM engagements LIMIT 0")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        conn.execute("ALTER TABLE engagements ADD COLUMN account_id TEXT")
        # Backfill existing rows with current account_id
        try:
            from ..linkedin import get_account_id
            aid = get_account_id() or ""
            if aid:
                conn.execute(
                    "UPDATE engagements SET account_id = ? WHERE account_id IS NULL",
                    (aid,),
                )
        except Exception:
            pass  # May fail during first init when no account is connected yet
        conn.commit()
        logger.info("Migration: added account_id to engagements")

    # Engagement dedup: index on post_id for fast global dedup queries
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_engagements_post_id ON engagements(post_id)"
    )

    # Engagement dedup: unique constraint — one engagement per post per account
    # Uses partial index to skip rows with NULL/empty account_id or post_id
    try:
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_engagements_post_account
            ON engagements(post_id, account_id)
            WHERE account_id IS NOT NULL AND account_id != '' AND post_id != ''
        """)
        conn.commit()
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        # May fail if duplicates already exist — clean them up first
        conn.execute("""
            DELETE FROM engagements WHERE id IN (
                SELECT id FROM (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY post_id, account_id ORDER BY created_at ASC
                    ) as rn
                    FROM engagements
                    WHERE account_id IS NOT NULL AND account_id != '' AND post_id != ''
                ) WHERE rn > 1
            )
        """)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_engagements_post_account
            ON engagements(post_id, account_id)
            WHERE account_id IS NOT NULL AND account_id != '' AND post_id != ''
        """)
        conn.commit()
        logger.info("Migration: cleaned duplicate engagements and added unique index")


    # Inbound pipeline: add dm_attempts for chat resolution retry tracking
    try:
        conn.execute("ALTER TABLE inbound_signals ADD COLUMN dm_attempts INTEGER DEFAULT 0")
        conn.commit()
        logger.info("Migration: added dm_attempts to inbound_signals")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        pass  # Column already exists

    # ── Post Intelligence: contacts unique constraint ──
    try:
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_campaign_linkedin
            ON contacts(campaign_id, linkedin_id)
            WHERE linkedin_id IS NOT NULL AND linkedin_id != ''
        """)
        conn.commit()
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        # Duplicates exist — clean up first (keep oldest per campaign+linkedin_id)
        conn.execute("""
            DELETE FROM contacts WHERE id IN (
                SELECT id FROM (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY campaign_id, linkedin_id ORDER BY created_at ASC
                    ) as rn
                    FROM contacts
                    WHERE linkedin_id IS NOT NULL AND linkedin_id != ''
                ) WHERE rn > 1
            )
        """)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_campaign_linkedin
            ON contacts(campaign_id, linkedin_id)
            WHERE linkedin_id IS NOT NULL AND linkedin_id != ''
        """)
        conn.commit()
        logger.info("Migration: cleaned duplicate contacts and added unique index")

    # ── Post Intelligence: posts table ──
    try:
        conn.execute("SELECT 1 FROM posts LIMIT 0")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS posts (
                id                 TEXT PRIMARY KEY,
                post_id            TEXT NOT NULL UNIQUE,
                author_linkedin_id TEXT,
                author_name        TEXT,
                text               TEXT,
                topic              TEXT,
                analysis_json      TEXT,
                metrics_json       TEXT,
                source             TEXT DEFAULT 'search',
                first_seen_at      INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                last_seen_at       INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_posts_post_id ON posts(post_id);
            CREATE INDEX IF NOT EXISTS idx_posts_author ON posts(author_linkedin_id);
            CREATE INDEX IF NOT EXISTS idx_posts_topic ON posts(topic);
            CREATE INDEX IF NOT EXISTS idx_posts_seen ON posts(last_seen_at DESC);
        """)
        conn.commit()
        logger.info("Migration: added posts table")

    # ── Post Intelligence: post_authors table ──
    try:
        conn.execute("SELECT 1 FROM post_authors LIMIT 0")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS post_authors (
                linkedin_id     TEXT PRIMARY KEY,
                name            TEXT,
                headline        TEXT,
                company         TEXT,
                posts_seen      INTEGER DEFAULT 1,
                topics_json     TEXT DEFAULT '[]',
                first_seen_at   INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                last_post_at    INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                contact_id      TEXT,
                FOREIGN KEY (contact_id) REFERENCES contacts(id)
            );
            CREATE INDEX IF NOT EXISTS idx_post_authors_company ON post_authors(company);
            CREATE INDEX IF NOT EXISTS idx_post_authors_last ON post_authors(last_post_at DESC);
        """)
        conn.commit()
        logger.info("Migration: added post_authors table")


    # Global contact base: global_contacts table (master record per person)
    try:
        conn.execute("SELECT 1 FROM global_contacts LIMIT 0")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS global_contacts (
                id                TEXT PRIMARY KEY,
                linkedin_id       TEXT,
                linkedin_url      TEXT,
                name              TEXT NOT NULL,
                title             TEXT DEFAULT '',
                company           TEXT DEFAULT '',
                email             TEXT DEFAULT '',
                location          TEXT DEFAULT '',
                profile_json      TEXT,
                analysis_json     TEXT,
                fit_score         REAL DEFAULT 0.0,
                estimated_revenue REAL DEFAULT 0.0,
                lifecycle_stage   TEXT DEFAULT 'prospect',
                tags_json         TEXT DEFAULT '[]',
                notes_json        TEXT DEFAULT '[]',
                source            TEXT DEFAULT 'search',
                source_detail     TEXT DEFAULT '',
                first_campaign_id TEXT,
                total_campaigns   INTEGER DEFAULT 0,
                first_contacted_at INTEGER,
                last_interaction_at INTEGER,
                created_at        INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                updated_at        INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_global_contacts_linkedin
                ON global_contacts(linkedin_id)
                WHERE linkedin_id IS NOT NULL AND linkedin_id != '';
            CREATE INDEX IF NOT EXISTS idx_global_contacts_name ON global_contacts(name);
            CREATE INDEX IF NOT EXISTS idx_global_contacts_company ON global_contacts(company);
            CREATE INDEX IF NOT EXISTS idx_global_contacts_lifecycle ON global_contacts(lifecycle_stage);
            CREATE INDEX IF NOT EXISTS idx_global_contacts_score ON global_contacts(fit_score DESC);
            CREATE INDEX IF NOT EXISTS idx_global_contacts_updated ON global_contacts(updated_at DESC);
        """)
        conn.commit()
        logger.info("Migration: added global_contacts table")

    # Global contact base: FK column on contacts
    try:
        conn.execute("ALTER TABLE contacts ADD COLUMN global_contact_id TEXT")
        conn.commit()
        logger.info("Migration: added global_contact_id to contacts")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        pass  # Column already exists

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_contacts_global ON contacts(global_contact_id)"
    )
    conn.commit()

    # Communication Strategist: prospect_daily_plans table
    try:
        conn.execute("SELECT 1 FROM prospect_daily_plans LIMIT 0")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS prospect_daily_plans (
                id              TEXT PRIMARY KEY,
                outreach_id     TEXT NOT NULL,
                campaign_id     TEXT NOT NULL,
                plan_date       TEXT NOT NULL,
                planned_actions TEXT NOT NULL DEFAULT '[]',
                executed_actions TEXT NOT NULL DEFAULT '[]',
                feedback_score  REAL,
                plan_source     TEXT DEFAULT 'llm',
                created_at      INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                updated_at      INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                FOREIGN KEY (outreach_id) REFERENCES outreaches(id),
                FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_plans_outreach_date
                ON prospect_daily_plans(outreach_id, plan_date);
            CREATE INDEX IF NOT EXISTS idx_daily_plans_campaign_date
                ON prospect_daily_plans(campaign_id, plan_date);
            CREATE INDEX IF NOT EXISTS idx_daily_plans_date
                ON prospect_daily_plans(plan_date);
        """)
        conn.commit()
        logger.info("Migration: added prospect_daily_plans table")

    # Network Intelligence: add columns to global_contacts
    for col_name, col_def in [
        ("contact_info_json", "TEXT DEFAULT '{}'"),
        ("network_degree", "INTEGER DEFAULT NULL"),
        # Mirror of connections.connected_at — "1st-degree since when".
        # 9 Sep 2026: excluding pre-existing connections needs a date,
        # and network_degree alone cannot say when the edge appeared.
        ("connected_at", "INTEGER DEFAULT NULL"),
        ("enriched_via", "TEXT DEFAULT ''"),
    ]:
        try:
            conn.execute(f"ALTER TABLE global_contacts ADD COLUMN {col_name} {col_def}")
            conn.commit()
            logger.info("Migration: added %s to global_contacts", col_name)
        except sqlite3.OperationalError as e:
            _reraise_if_migration_interrupted(e)
            pass  # Column already exists

    # Convert permanent skip_engagement to 1h time-limited cooldown.
    # Permanent blocks are no longer set (replaced with JSON cooldowns in v0.9.42).
    try:
        import json as _json
        _cooldown = _json.dumps({"skip_engagement_until": int(time.time()) + 3600})
        cursor = conn.execute(
            "UPDATE outreaches SET next_action = ? WHERE next_action = 'skip_engagement'",
            (_cooldown,),
        )
        conn.commit()  # Always commit to avoid dangling write transaction
        if cursor.rowcount > 0:
            logger.info(
                "Migration: converted %d permanent skip_engagement to 1h cooldown",
                cursor.rowcount,
            )
    except Exception:
        pass  # Safe to skip — new code already writes JSON cooldowns

    # Prospect source tracking: add source_detail column
    for _tbl in ("contacts", "global_contacts"):
        try:
            conn.execute(f"ALTER TABLE {_tbl} ADD COLUMN source_detail TEXT DEFAULT ''")
            conn.commit()
            logger.info("Migration: added source_detail to %s", _tbl)
        except sqlite3.OperationalError as e:
            _reraise_if_migration_interrupted(e)
            pass  # Column already exists

    # Action tracking: add campaign_id to actions_log for time-windowed campaign queries
    try:
        conn.execute("ALTER TABLE actions_log ADD COLUMN campaign_id TEXT")
        conn.commit()
        logger.info("Migration: added campaign_id to actions_log")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        pass  # Column already exists

    # Action tracking: backfill actions_log.campaign_id from outreaches
    try:
        updated = conn.execute("""
            UPDATE actions_log SET campaign_id = (
                SELECT o.campaign_id FROM outreaches o WHERE o.id = actions_log.outreach_id
            ) WHERE campaign_id IS NULL AND outreach_id IS NOT NULL
        """).rowcount
        if updated:
            conn.commit()
            logger.info("Migration: backfilled campaign_id on %d actions_log rows", updated)
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        pass

    # Action tracking: indexes on actions_log for time-windowed reporting
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_actions_log_timestamp ON actions_log(timestamp)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_actions_log_type_time ON actions_log(action_type, timestamp)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_actions_log_campaign_time ON actions_log(campaign_id, timestamp)"
    )
    conn.commit()

    # Action verification: add verified_at and verified_status to outreaches
    for col, ctype in [("verified_at", "INTEGER"), ("verified_status", "TEXT")]:
        try:
            conn.execute(f"ALTER TABLE outreaches ADD COLUMN {col} {ctype}")
            conn.commit()
            logger.info("Migration: added %s to outreaches", col)
        except sqlite3.OperationalError as e:
            _reraise_if_migration_interrupted(e)
            pass  # Column already exists

    # Engagement verification: add verified_at, verified_status, external_id to engagements
    for col, ctype in [
        ("verified_at", "INTEGER"),
        ("verified_status", "TEXT"),
        ("external_id", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE engagements ADD COLUMN {col} {ctype}")
            conn.commit()
            logger.info("Migration: added %s to engagements", col)
        except sqlite3.OperationalError as e:
            _reraise_if_migration_interrupted(e)
            pass  # Column already exists

    # Local connections cache: store 1st-degree LinkedIn connections locally
    # so we don't need to re-fetch from Unipile API every time.
    try:
        conn.execute("SELECT 1 FROM connections LIMIT 0")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS connections (
                id              TEXT PRIMARY KEY,
                account_id      TEXT NOT NULL,
                provider_id     TEXT NOT NULL,
                public_id       TEXT DEFAULT '',
                name            TEXT DEFAULT '',
                headline        TEXT DEFAULT '',
                network_distance TEXT DEFAULT 'FIRST_DEGREE',
                synced_at       INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                UNIQUE(account_id, provider_id)
            );
            CREATE INDEX IF NOT EXISTS idx_connections_account ON connections(account_id);
            CREATE INDEX IF NOT EXISTS idx_connections_provider ON connections(provider_id);
            CREATE INDEX IF NOT EXISTS idx_connections_public_id ON connections(public_id);
        """)
        conn.commit()
        logger.info("Migration: added connections table")

    # ── Connections table v2: add searchable fields for local connection search ──
    for col, ctype in [
        ("company", "TEXT DEFAULT ''"),
        ("location", "TEXT DEFAULT ''"),
        ("profile_url", "TEXT DEFAULT ''"),
        ("profile_json", "TEXT DEFAULT ''"),
        # Network post scanning rotates through connections least-recently
        # scanned first, so the 16k are covered evenly without re-reading the
        # same few every cycle.
        ("last_scanned_at", "INTEGER"),
        # Tier-1 watch list: scanned every cycle rather than rotated. Plain
        # rotation reaches a given person about every ten days, which is a
        # lottery for a post worth acting on within a day.
        ("watch_priority", "INTEGER DEFAULT 0"),
        # ── v3 (9 Sep 2026) ──
        # connected_at: first time we saw this edge. Written once on INSERT and
        # never overwritten — the upsert's DO UPDATE clause and mark_connected()
        # both COALESCE it — because "existing connection" means connected
        # before the campaign invited them, and a value re-stamped on every
        # 4-hourly sync answers "when did we last look", not "since when".
        # removed_at: prune is a soft delete for the same reason. A hard DELETE
        # followed by a re-appearance reset connected_at to today.
        ("connected_at", "INTEGER"),
        ("removed_at", "INTEGER"),
    ]:
        try:
            conn.execute(f"ALTER TABLE connections ADD COLUMN {col} {ctype}")
            conn.commit()
            logger.info("Migration: added %s to connections", col)
        except sqlite3.OperationalError as e:
            _reraise_if_migration_interrupted(e)
            pass

    for idx_sql in [
        "CREATE INDEX IF NOT EXISTS idx_connections_name ON connections(name COLLATE NOCASE)",
        "CREATE INDEX IF NOT EXISTS idx_connections_company ON connections(company COLLATE NOCASE)",
        "CREATE INDEX IF NOT EXISTS idx_connections_location ON connections(location COLLATE NOCASE)",
        "CREATE INDEX IF NOT EXISTS idx_connections_connected_at ON connections(account_id, connected_at)",
        "CREATE INDEX IF NOT EXISTS idx_connections_removed_at ON connections(account_id, removed_at)",
    ]:
        try:
            conn.execute(idx_sql)
            conn.commit()
        except sqlite3.OperationalError as e:
            _reraise_if_migration_interrupted(e)
            pass

    # ── Post Intelligence v2: expanded post columns ──
    for col, ctype in [
        ("hashtags", "TEXT"),
        ("media_type", "TEXT"),
        ("media_url", "TEXT"),
        ("language", "TEXT"),
        ("visibility", "TEXT"),
        ("engagement_rate", "REAL"),
        ("author_followers", "INTEGER"),
        ("reactions_breakdown", "TEXT"),
        ("reposts_count", "INTEGER DEFAULT 0"),
        ("is_repost", "INTEGER DEFAULT 0"),
        ("original_post_id", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE posts ADD COLUMN {col} {ctype}")
            conn.commit()
        except sqlite3.OperationalError as e:
            _reraise_if_migration_interrupted(e)
            pass  # Column already exists

    # ── Post Intelligence Phase 5: impressions_count column ──
    try:
        conn.execute("ALTER TABLE posts ADD COLUMN impressions_count INTEGER DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        pass  # Column already exists

    # ── Post metric snapshots: add impressions column ──
    try:
        conn.execute("ALTER TABLE post_metric_snapshots ADD COLUMN impressions INTEGER DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        pass  # Column already exists

    # ── Post metric snapshots (time-series tracking) ──
    try:
        conn.execute("SELECT 1 FROM post_metric_snapshots LIMIT 0")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS post_metric_snapshots (
                id          TEXT PRIMARY KEY,
                post_id     TEXT NOT NULL,
                likes       INTEGER DEFAULT 0,
                comments    INTEGER DEFAULT 0,
                reposts     INTEGER DEFAULT 0,
                snapshot_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
            );
            CREATE INDEX IF NOT EXISTS idx_snapshots_post ON post_metric_snapshots(post_id);
            CREATE INDEX IF NOT EXISTS idx_snapshots_at ON post_metric_snapshots(snapshot_at);
        """)
        conn.commit()
        logger.info("Migration: added post_metric_snapshots table")

    # ── Contact research pipeline columns ──
    for col, ctype in [
        ("research_status", "TEXT DEFAULT 'pending'"),
        ("research_completed_at", "INTEGER"),
        ("research_summary", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE contacts ADD COLUMN {col} {ctype}")
            conn.commit()
        except sqlite3.OperationalError as e:
            _reraise_if_migration_interrupted(e)
            pass  # Column already exists

    # ── Signal improvements: watchlist_id, user_feedback, experiment_variant ──
    for col, ctype in [
        ("watchlist_id", "TEXT"),
        ("user_feedback", "TEXT"),
        ("feedback_at", "INTEGER"),
        ("experiment_variant", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE signals ADD COLUMN {col} {ctype}")
            conn.commit()
        except sqlite3.OperationalError as e:
            _reraise_if_migration_interrupted(e)
            pass  # Column already exists

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_signals_watchlist ON signals(watchlist_id)"
    )
    conn.commit()

    # ── Watchlist auto-tuning columns ──
    for col, ctype in [
        ("auto_tuned_at", "INTEGER"),
        ("disabled_keywords", "TEXT DEFAULT '[]'"),
    ]:
        try:
            conn.execute(f"ALTER TABLE signal_watchlists ADD COLUMN {col} {ctype}")
            conn.commit()
        except sqlite3.OperationalError as e:
            _reraise_if_migration_interrupted(e)
            pass  # Column already exists

    # ── Collection metrics table (observability) ──
    try:
        conn.execute("SELECT 1 FROM collection_metrics LIMIT 0")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS collection_metrics (
                id                TEXT PRIMARY KEY,
                collector_type    TEXT NOT NULL,
                account_id        TEXT,
                contacts_scanned  INTEGER DEFAULT 0,
                posts_collected   INTEGER DEFAULT 0,
                posts_analyzed    INTEGER DEFAULT 0,
                signals_created   INTEGER DEFAULT 0,
                errors            INTEGER DEFAULT 0,
                api_calls         INTEGER DEFAULT 0,
                duration_ms       INTEGER DEFAULT 0,
                error_details     TEXT,
                run_at            INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
            );
            CREATE INDEX IF NOT EXISTS idx_collection_metrics_type ON collection_metrics(collector_type);
            CREATE INDEX IF NOT EXISTS idx_collection_metrics_at ON collection_metrics(run_at);
        """)
        conn.commit()
        logger.info("Migration: added collection_metrics table")

    # ── Signal threshold A/B testing ──
    try:
        conn.execute("SELECT 1 FROM signal_experiments LIMIT 0")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS signal_experiments (
                id                TEXT PRIMARY KEY,
                campaign_id       TEXT NOT NULL,
                experiment_type   TEXT DEFAULT 'threshold',
                control_config    TEXT NOT NULL,
                treatment_config  TEXT NOT NULL,
                status            TEXT DEFAULT 'running',
                started_at        INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                completed_at      INTEGER,
                result_json       TEXT,
                FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
            );
            CREATE INDEX IF NOT EXISTS idx_signal_experiments_campaign ON signal_experiments(campaign_id);
            CREATE INDEX IF NOT EXISTS idx_signal_experiments_status ON signal_experiments(status);
        """)
        conn.commit()
        logger.info("Migration: added signal_experiments table")

    # ── Company posts tracked table (Phase 2 intent expansion) ──
    try:
        conn.execute("SELECT 1 FROM company_posts_tracked LIMIT 0")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS company_posts_tracked (
                id                   TEXT PRIMARY KEY,
                watchlist_id         TEXT NOT NULL,
                post_id              TEXT NOT NULL,
                post_text            TEXT,
                last_checked_at      INTEGER,
                known_commenters     TEXT DEFAULT '[]',
                known_reactors       TEXT DEFAULT '[]',
                created_at           INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                FOREIGN KEY (watchlist_id) REFERENCES signal_watchlists(id)
            );
            CREATE INDEX IF NOT EXISTS idx_cpt_watchlist ON company_posts_tracked(watchlist_id);
            CREATE INDEX IF NOT EXISTS idx_cpt_post_id ON company_posts_tracked(post_id);
        """)
        conn.commit()
        logger.info("Migration: added company_posts_tracked table")

    # ── Composite indexes for signal_exists() performance ──
    # idx_signals_status_detected additionally serves the classifier's two
    # ORDER BY detected_at halves. With only idx_signals_status the LIMIT
    # bounded the rows returned but not the rows read: EXPLAIN QUERY PLAN on
    # the live DB showed "SEARCH signals USING INDEX idx_signals_status" plus
    # "USE TEMP B-TREE FOR ORDER BY", i.e. every 'new' row (SELECT *, content
    # and metadata_json included) sorted and thrown away, twice per tick.
    for idx_sql in [
        "CREATE INDEX IF NOT EXISTS idx_signals_type_post_id ON signals(signal_type, post_id)",
        "CREATE INDEX IF NOT EXISTS idx_signals_type_linkedin_id ON signals(signal_type, linkedin_id)",
        "CREATE INDEX IF NOT EXISTS idx_signals_status_detected ON signals(status, detected_at)",
    ]:
        conn.execute(idx_sql)
    conn.commit()

    # Global contact base: backfill from existing campaign contacts
    _backfill_global_contacts(conn)

    # Inbound Pipeline v2 + delete message support
    # (was previously inside _backfill_global_contacts by mistake)
    _migrate_inbound_v2_and_delete_support(conn)
    # SCHEMA_VERSION 11: outreach tombstones so a local hard delete reaches the cloud
    _migrate_outreach_tombstones(conn)

    # v0.10.131: Deduplicate outreaches — keep oldest per (campaign_id, contact_id),
    # delete duplicates, then add unique index to prevent future duplicates.
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_outreaches_campaign_contact'"
        ).fetchone()
        if not row:
            # Delete duplicate outreaches, keeping the one with the smallest rowid per pair
            deleted = conn.execute("""
                DELETE FROM outreaches WHERE rowid NOT IN (
                    SELECT MIN(rowid) FROM outreaches
                    GROUP BY campaign_id, contact_id
                )
            """).rowcount
            if deleted:
                logger.info("Migration: removed %d duplicate outreach records", deleted)
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_outreaches_campaign_contact
                    ON outreaches(campaign_id, contact_id)
            """)
            conn.commit()
            logger.info("Migration: added unique index idx_outreaches_campaign_contact")
    except Exception as e:
        if isinstance(e, sqlite3.OperationalError):
            _reraise_if_migration_interrupted(e)
        logger.warning("Migration: outreach dedup failed: %s", e)

    try:
        conn.execute("SELECT 1 FROM agent_commons LIMIT 0")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS agent_commons (
                id           TEXT PRIMARY KEY,
                kind         TEXT NOT NULL,
                agent        TEXT NOT NULL,
                campaign_id  TEXT NOT NULL DEFAULT '',
                outreach_id  TEXT,
                body         TEXT,
                decision     TEXT,
                reason       TEXT,
                created_at   INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                expires_at   INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_agent_commons_beat
                ON agent_commons(campaign_id, agent, kind);
            CREATE INDEX IF NOT EXISTS idx_agent_commons_notes
                ON agent_commons(campaign_id, kind, expires_at);
        """)
        conn.commit()
        logger.info("Migration: added agent_commons table")


def _backfill_global_contacts(conn: sqlite3.Connection) -> None:
    """One-time backfill: create global_contacts from existing campaign contacts.

    Groups by linkedin_id, picks best data (most recent, highest fit_score),
    and links all campaign contacts via global_contact_id.
    """
    import uuid as _uuid

    # Check if backfill already ran (any global contacts exist)
    row = conn.execute("SELECT COUNT(*) as cnt FROM global_contacts").fetchone()
    if row and row[0] > 0:
        return

    # Check if there are any contacts to backfill
    contact_count = conn.execute(
        "SELECT COUNT(*) as cnt FROM contacts WHERE global_contact_id IS NULL"
    ).fetchone()
    if not contact_count or contact_count[0] == 0:
        return

    now = int(time.time())
    backfilled = 0

    # Group contacts by linkedin_id (non-empty ones)
    grouped_rows = conn.execute("""
        SELECT linkedin_id,
               GROUP_CONCAT(id) as contact_ids,
               MAX(name) as name,
               MAX(title) as title,
               MAX(company) as company,
               MAX(linkedin_url) as linkedin_url,
               MAX(fit_score) as fit_score,
               MAX(profile_json) as profile_json,
               MAX(analysis_json) as analysis_json,
               MIN(campaign_id) as first_campaign_id,
               COUNT(*) as campaign_count,
               MIN(created_at) as first_seen,
               MAX(source) as source
        FROM contacts
        WHERE linkedin_id IS NOT NULL AND linkedin_id != ''
        GROUP BY linkedin_id
    """).fetchall()

    for row in grouped_rows:
        gid = str(_uuid.uuid4())
        linkedin_id = row["linkedin_id"]

        # Derive lifecycle from best outreach status across all campaigns
        best_status = conn.execute("""
            SELECT o.status, o.invited_at, MAX(o.updated_at) as last_interaction
            FROM outreaches o
            JOIN contacts c ON o.contact_id = c.id
            WHERE c.linkedin_id = ?
            ORDER BY
                CASE o.status
                    WHEN 'closed_happy' THEN 1
                    WHEN 'hot_lead' THEN 2
                    WHEN 'replied' THEN 3
                    WHEN 'connected' THEN 4
                    WHEN 'messaged' THEN 4
                    WHEN 'invited' THEN 5
                    ELSE 6
                END
            LIMIT 1
        """, (linkedin_id,)).fetchone()

        lifecycle = "prospect"
        first_contacted = None
        last_interaction = None
        if best_status:
            s = best_status["status"]
            if s == "closed_happy":
                lifecycle = "customer"
            elif s == "closed_unhappy":
                lifecycle = "lost"
            elif s in ("replied", "hot_lead"):
                lifecycle = "engaged"
            elif s in ("connected", "messaged"):
                lifecycle = "connected"
            elif s == "invited":
                lifecycle = "contacted"
            first_contacted = best_status["invited_at"]
            last_interaction = best_status["last_interaction"]

        conn.execute("""
            INSERT INTO global_contacts
                (id, linkedin_id, linkedin_url, name, title, company,
                 profile_json, analysis_json, fit_score,
                 lifecycle_stage, source, first_campaign_id, total_campaigns,
                 first_contacted_at, last_interaction_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            gid, linkedin_id, row["linkedin_url"],
            row["name"] or "", row["title"] or "", row["company"] or "",
            row["profile_json"], row["analysis_json"],
            row["fit_score"] or 0.0,
            lifecycle, row["source"] or "search",
            row["first_campaign_id"],
            row["campaign_count"],
            first_contacted, last_interaction,
            row["first_seen"] or now, now,
        ))

        # Link all campaign contacts to this global contact
        contact_ids = row["contact_ids"].split(",")
        for cid in contact_ids:
            conn.execute(
                "UPDATE contacts SET global_contact_id = ? WHERE id = ?",
                (gid, cid.strip()),
            )
        backfilled += 1

    # Handle contacts without linkedin_id (name-only)
    orphan_rows = conn.execute("""
        SELECT id, name, title, company, linkedin_url, fit_score,
               profile_json, analysis_json, campaign_id, source, created_at
        FROM contacts
        WHERE (linkedin_id IS NULL OR linkedin_id = '')
          AND global_contact_id IS NULL
    """).fetchall()

    for row in orphan_rows:
        gid = str(_uuid.uuid4())
        conn.execute("""
            INSERT INTO global_contacts
                (id, name, title, company, linkedin_url,
                 profile_json, analysis_json, fit_score,
                 lifecycle_stage, source, first_campaign_id, total_campaigns,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'prospect', ?, ?, 1, ?, ?)
        """, (
            gid, row["name"] or "", row["title"] or "", row["company"] or "",
            row["linkedin_url"],
            row["profile_json"], row["analysis_json"],
            row["fit_score"] or 0.0,
            row["source"] or "search", row["campaign_id"],
            row["created_at"] or now, now,
        ))
        conn.execute(
            "UPDATE contacts SET global_contact_id = ? WHERE id = ?",
            (gid, row["id"]),
        )
        backfilled += 1

    if backfilled > 0:
        conn.commit()
        logger.info("Migration: backfilled %d global contacts from existing campaign contacts", backfilled)


def _migrate_inbound_v2_and_delete_support(conn: sqlite3.Connection) -> None:
    """Inbound Pipeline v2 columns + delete message support.

    Previously these were inside _backfill_global_contacts() by mistake,
    so they were skipped on DBs that already had global contacts.
    """
    # ── Inbound Pipeline v2: classify-first columns ──
    for col, ctype in [
        ("invitation_id", "TEXT"),
        ("actioned_at", "INTEGER"),
        ("decline_reason", "TEXT"),
        ("message_id", "TEXT"),
        ("reaction_sent", "INTEGER DEFAULT 0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE inbound_signals ADD COLUMN {col} {ctype}")
            conn.commit()
            logger.info("Migration: added %s to inbound_signals", col)
        except sqlite3.OperationalError as e:
            _reraise_if_migration_interrupted(e)
            pass  # Column already exists

    # Inbound Pipeline v2: rename 'qualified' → 'classified' status
    try:
        updated = conn.execute(
            "UPDATE inbound_signals SET status = 'classified' WHERE status = 'qualified'"
        ).rowcount
        if updated:
            conn.commit()
            logger.info("Migration: renamed %d inbound_signals 'qualified' → 'classified'", updated)
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        pass

    # Inbound Pipeline v2: index on invitation_id
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_inbound_signals_invitation_id "
            "ON inbound_signals(invitation_id)"
        )
        conn.commit()
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        pass

    # Delete message support: track deleted messages and Unipile message IDs
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN deleted_at INTEGER")
        conn.commit()
        logger.info("Migration: added deleted_at to messages")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        pass  # Column already exists

    try:
        conn.execute("ALTER TABLE messages ADD COLUMN external_message_id TEXT")
        conn.commit()
        logger.info("Migration: added external_message_id to messages")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        pass  # Column already exists

    # Signal optimization tables (self-tuning system)
    try:
        conn.execute("SELECT 1 FROM signal_weight_overrides LIMIT 0")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS signal_weight_overrides (
                signal_type     TEXT PRIMARY KEY,
                weight          REAL NOT NULL,
                previous_weight REAL NOT NULL,
                source          TEXT NOT NULL DEFAULT 'auto',
                applied_at      INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                applied_by      TEXT DEFAULT 'optimizer'
            );
            CREATE TABLE IF NOT EXISTS signal_threshold_overrides (
                threshold_name  TEXT PRIMARY KEY,
                value           REAL NOT NULL,
                previous_value  REAL NOT NULL,
                source          TEXT NOT NULL DEFAULT 'auto',
                applied_at      INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                applied_by      TEXT DEFAULT 'optimizer'
            );
            CREATE TABLE IF NOT EXISTS optimization_history (
                id                TEXT PRIMARY KEY,
                optimization_type TEXT NOT NULL,
                target            TEXT NOT NULL,
                before_value      TEXT,
                after_value       TEXT,
                reason            TEXT,
                data_points       INTEGER DEFAULT 0,
                confidence        REAL DEFAULT 0.0,
                status            TEXT DEFAULT 'applied',
                applied_at        INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
                rolled_back_at    INTEGER,
                rolled_back_by    TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_opt_history_type ON optimization_history(optimization_type);
            CREATE INDEX IF NOT EXISTS idx_opt_history_applied ON optimization_history(applied_at);
        """)
        logger.info("Migration: added signal optimization tables")

    # Calendar events table (Google Calendar integration)
    try:
        conn.execute("SELECT 1 FROM calendar_events LIMIT 0")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS calendar_events (
                id              TEXT PRIMARY KEY,
                outreach_id     TEXT REFERENCES outreaches(id),
                campaign_id     TEXT,
                google_event_id TEXT,
                event_link      TEXT,
                summary         TEXT,
                start_time      INTEGER NOT NULL,
                end_time        INTEGER NOT NULL,
                attendee_email  TEXT,
                attendee_name   TEXT,
                prospect_name   TEXT,
                status          TEXT DEFAULT 'created',
                created_at      INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
            );
            CREATE INDEX IF NOT EXISTS idx_cal_events_outreach ON calendar_events(outreach_id);
            CREATE INDEX IF NOT EXISTS idx_cal_events_campaign ON calendar_events(campaign_id);
        """)
        logger.info("Migration: added calendar_events table")

    # ── Cross-process daily-cap reservations ──
    # A slot is booked here the moment a cap check approves an action, because
    # the actions_log row only appears after the action completes. Keeping the
    # ledger in the database rather than in module state is what makes the cap
    # hold across the daemon and every MCP session instead of per process.
    try:
        conn.execute("SELECT 1 FROM daily_slot_reservations LIMIT 0")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS daily_slot_reservations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                day         TEXT NOT NULL,
                pattern     TEXT NOT NULL,
                created_at  INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_slot_res_lookup
                ON daily_slot_reservations(day, pattern, created_at);
        """)
        conn.commit()
        logger.info("Migration: added daily_slot_reservations table")

    # ── contacts.profile_json: '' is a landmine, NULL is not ──
    # json_extract raises 'malformed JSON' on the empty string, and the raise
    # aborts the whole statement rather than skipping the row — so a single
    # legacy contact blanks every query that reads the column, however many
    # good rows sit beside it. 1685 of 5860 rows carried '' when this landed
    # (22 Aug 2026). Guarding the readers alone would leave the trap in place
    # for the next one to forget; NULL removes it, because json_extract simply
    # returns NULL for NULL.
    #
    # NULL rather than '{}': the readers on the Python side branch on
    # `if contact.get("profile_json")` and fall through
    # `json.loads(row.get("profile_json") or "{}")`. '' is falsy and '{}' is
    # truthy, so only NULL preserves what all of them do today. The SQL side
    # is unaffected either way — every `profile_json IS NOT NULL AND
    # profile_json != ''` filter excluded these rows before and still does,
    # and the enrichment queue's `profile_json IS NULL OR = '' OR = '{}'`
    # still selects them.
    #
    # TRIM covers whitespace-only blobs, which json_extract rejects too.
    updated = conn.execute(
        "UPDATE contacts SET profile_json = NULL WHERE TRIM(profile_json) = ''"
    ).rowcount
    conn.commit()
    if updated:
        logger.info(
            "Migration: normalised %d blank contacts.profile_json to NULL", updated,
        )


    # ── global_contacts.profile_json: a redirect page is not a profile ──
    # 1046 rows held `<!DOCTYPE html>… <title>Redirecting</title>` — Unipile's
    # 3xx body for the percent-encoded path, written verbatim by a
    # `str(profile)` fallback in the backfill. Every one of them had a
    # non-ASCII linkedin_id, which is what provoked the redirect.
    #
    # They do not crash anything — every global_contacts reader carries a
    # json_valid guard — they are worse than that: the enrichment queue
    # selected `IS NULL OR '' OR '{}'`, so a blob of HTML read as "already
    # enriched" and the row was never asked about again. NULL is the column's
    # honest "nothing known", and it is a value that queue already selects, so
    # these heal on the next backfill pass without depending on the widened
    # predicate staying widened.
    #
    # Deliberately not restricted to HTML: whatever is in there, if it is not
    # JSON then no reader can use it. Valid payloads are untouched, and
    # json_valid is NULL-safe so NULL rows are left alone.
    invalid = conn.execute(
        "UPDATE global_contacts SET profile_json = NULL "
        "WHERE profile_json IS NOT NULL AND NOT json_valid(profile_json)"
    ).rowcount
    conn.commit()
    if invalid:
        logger.info(
            "Migration: cleared %d global_contacts.profile_json blobs that were "
            "not JSON", invalid,
        )


    # ── global_contacts.first_campaign_id: a deleted campaign is not a campaign ──
    # delete_campaign removed the campaign, its contacts and its outreaches and
    # left this column naming the id it had just deleted. Dedup
    # (get_all_known_linkedin_ids) counts any row with first_campaign_id set as
    # "already in a campaign", so every prospect a deleted campaign had merely
    # queued was filtered out of every campaign created afterwards — forever,
    # and with no tool able to clear the flag. delete_campaign now maintains the
    # column itself; this releases the rows deleted before it did.
    #
    # Repoint rather than blank where the person is still in a live campaign:
    # the column means "the first campaign this person was attached to", and one
    # of the surviving ones is now that. The subquery reads
    # contacts.global_contact_id, which the backfill above populates for every
    # row, so it is answerable here and yields NULL only when nothing is left.
    #
    # first_contacted_at is left alone on purpose — it records a message that
    # really was sent, deleting the campaign did not unsend it, and those people
    # stay deduped through that column instead. Only rows that were queued and
    # never messaged come free, which is exactly the set that was wrongly held.
    #
    # total_campaigns is deliberately not recomputed. It is a display counter
    # with no decision riding on it, and the honest count for a row whose
    # contacts are long gone is unknowable — guessing would trade a harmless
    # overcount for a wrong one.
    released = conn.execute(
        """UPDATE global_contacts
              SET first_campaign_id = (
                      SELECT c.campaign_id FROM contacts c
                       WHERE c.global_contact_id = global_contacts.id
                         AND c.campaign_id IS NOT NULL AND c.campaign_id != ''
                       ORDER BY c.created_at LIMIT 1),
                  updated_at = strftime('%s', 'now')
            WHERE first_campaign_id IS NOT NULL AND first_campaign_id != ''
              AND NOT EXISTS (
                  SELECT 1 FROM campaigns
                   WHERE campaigns.id = global_contacts.first_campaign_id)"""
    ).rowcount
    conn.commit()
    if released:
        logger.info(
            "Migration: released %d global_contacts rows still pointing at a "
            "deleted campaign", released,
        )

    # Persist the Unipile chat so later DMs skip a 3-page inbox scan.
    try:
        conn.execute("ALTER TABLE outreaches ADD COLUMN chat_id TEXT")
        conn.commit()
        logger.info("Migration: added chat_id to outreaches")
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        pass

    # Headline A/B: per-send attribution, separate from message-test `variant`.
    for col, ctype in [
        ("headline_variant", "TEXT"),
        ("headline_test_id", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE outreaches ADD COLUMN {col} {ctype}")
            conn.commit()
            logger.info("Migration: added %s to outreaches", col)
        except sqlite3.OperationalError as e:
            _reraise_if_migration_interrupted(e)
            pass


def _migrate_outreach_tombstones(conn: sqlite3.Connection) -> None:
    """Tombstones for outreach rows deleted here (SCHEMA_VERSION 11).

    _SCHEMA_SQL only runs on an empty file, so an existing database gets the
    table from here. Idempotent: CREATE TABLE IF NOT EXISTS.
    """
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS outreach_tombstones ("
            "outreach_id TEXT PRIMARY KEY, campaign_id TEXT, "
            "deleted_at INTEGER NOT NULL)"
        )
        conn.commit()
    except sqlite3.OperationalError as e:
        _reraise_if_migration_interrupted(e)
        raise


def reset_db() -> None:
    """Delete the database file entirely, creating a backup first."""
    path = config.db_path()
    if path.exists():
        try:
            from .backup import create_backup, rotate_backups

            create_backup("pre-reset")
            rotate_backups()
        except Exception as exc:
            logger.warning("Could not create pre-reset backup: %s", exc)
        path.unlink()
        logger.info("Database deleted")
