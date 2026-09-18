"""HeyLead constants — limits, defaults, tier definitions."""

from __future__ import annotations

# ──────────────────────────────────────────────
# Booking Link Naming Convention
# ──────────────────────────────────────────────
# Two distinct concepts exist — never conflate them:
#
# "booking_link" = the HeyLead USER's calendar URL (outbound).
#   Stored in campaigns.config_json["booking_link"].
#   Set via edit_campaign(booking_link=...).
#   Used by LLM to weave into positive replies and follow-ups.
#
# "prospect_calendar_url" = the PROSPECT's calendar URL (inbound).
#   Detected by detect_calendar_url() from prospect reply text.
#   Stored in outreaches.next_action as {"prospect_calendar_url": "..."}.
#   Used by calendar_booker.py to auto-book meetings.
#
# "meeting_link" = the final meeting URL when closing an outreach.
#   Could be either the user's or prospect's link — stored in outcome_json.
#
# Rules:
#   - NEVER use "booking_link" to store a prospect's URL.
#   - Prospect's link ALWAYS uses the "prospect_calendar_url" key.
#   - When closing, use "meeting_link" (source-agnostic).
# ──────────────────────────────────────────────

# ──────────────────────────────────────────────
# Rate Limits — Reactive (no hardcoded caps)
# ──────────────────────────────────────────────
# All proactive daily/weekly/hourly limits have been removed.
# The scheduler now attempts actions freely and backs off only
# when Unipile returns 429 or 422 temporary_provider_limit.

# Email send pacing (delay between sends, not a cap)
EMAIL_INVITE_DELAY_MIN = 10 * 60     # 10 min between email sends
EMAIL_INVITE_DELAY_MAX = 25 * 60     # 25 min

# Collector API call timeouts
COLLECTOR_API_TIMEOUT = 45           # Per-API-call timeout in collectors (seconds)
CIRCUIT_BREAKER_THRESHOLD = 3        # Consecutive timeouts before tripping
CIRCUIT_BREAKER_RESET_SECONDS = 300  # 5 min cooldown after circuit breaker trips

# Timing
MIN_DELAY_MINUTES = 15               # Min gap between invitations
MAX_DELAY_MINUTES = 45               # Max gap (randomized)
DEFAULT_START_HOUR = 0               # Autonomous bot: 24/7
DEFAULT_END_HOUR = 24                # Autonomous bot: 24/7
DEFAULT_ACTIVE_DAYS = [0, 1, 2, 3, 4, 5, 6]  # All days

# Message limits
INVITATION_NOTE_MAX_CHARS = 200      # Regular LinkedIn
INVITATION_NOTE_MAX_CHARS_SALES_NAV = 300  # Sales Navigator


def invitation_note_max_chars(has_sales_navigator: bool = False) -> int:
    """LinkedIn connection-note cap: 300 with Sales Nav, 200 otherwise."""
    if has_sales_navigator:
        return INVITATION_NOTE_MAX_CHARS_SALES_NAV
    return INVITATION_NOTE_MAX_CHARS

# ──────────────────────────────────────────────
# Free Tier Caps
# ──────────────────────────────────────────────
# Production free-tier caps. Do not raise these for local development —
# tests/test_free_tier_caps.py pins the row so 9999 cannot ship again.
# FOLLOWUPS is also an account-safety limit, not just a monetization one.

FREE_MONTHLY_INVITATIONS = 50
FREE_MONTHLY_MESSAGES = 20
FREE_MAX_CAMPAIGNS = 1
FREE_MAX_CONTACTS_ANALYZED = 30
FREE_MAX_ICP_GENERATIONS = 1
FREE_MAX_ICP_V2_GENERATIONS = 3
FREE_MAX_FOLLOWUPS = 2
FREE_MAX_ENGAGEMENTS = 30
FREE_MAX_LINKEDIN_ACCOUNTS = 1

# ──────────────────────────────────────────────
# Pro Tier
# ──────────────────────────────────────────────

PRO_PRICE_MONTHLY = 29               # USD
PRO_PRICE_ANNUAL_MONTHLY = 24        # USD (billed annually)
PRO_MAX_FOLLOWUPS = 5
PRO_MAX_LINKEDIN_ACCOUNTS = 5
PRO_FOLLOWUP_SCHEDULE_DAYS = [1, 3, 7, 14]

# ──────────────────────────────────────────────
# Daily Safety Caps (proactive — prevent LinkedIn bans)
# ──────────────────────────────────────────────
# These are HARD daily caps per LinkedIn account. The scheduler will NOT
# execute actions beyond these limits, regardless of Unipile response codes.
# Values are set ~10% below known LinkedIn safe thresholds.
# Total daily budget: ~180 visible actions (was uncapped → ~350/day → ban).

# The invitation ceiling belongs to the LinkedIn ACCOUNT, not to its tier.
# This used to read "an SN operator routinely sends 200–300 connection
# requests in a day from the SN UI" and gave Sales Navigator 250 a day.
# Production refuted it twice: 12 Sep 2026 at 123 invitations over the rolling
# week, and 15 Sep 2026 at 336 (177 of them in one day) — both on a seat
# holding a genuine SN contract, both answered with
#
#     422 errors/cannot_resend_yet
#     "You have reached a temporary provider limit. Please try again later."
#
# Sales Navigator buys search depth and InMail, not a bigger invitation
# allowance, so every tier shares one ceiling: 20 a day against LinkedIn's
# published ~100 a week (heylead-api #446 sets the same figures server-side —
# these two must not drift). The hosted weekly cap still arrives per-seat in
# the stats payload; HOSTED_WEEKLY_INVITE_CAP is only the fallback when it
# omits `weekly_cap`.
DAILY_CAP_INVITATIONS = 20           # Premium daily
DAILY_CAP_INVITATIONS_SALES_NAV = 20  # Sales Navigator daily — the same ceiling
DAILY_CAP_INVITATIONS_FREE = 20      # Confirmed free — 20 keeps a 5-day week under 100
HOSTED_WEEKLY_INVITE_CAP = 100       # Free-seat fallback only
DAILY_CAP_FOLLOWS = 15               # Halved — reduce visible footprint
DAILY_CAP_PROFILE_VIEWS = 35         # Halved — high volume = detectable pattern
DAILY_CAP_PROFILE_VIEWS_SALES_NAV = 50  # SN accounts absorb more views; tier.caps_for picks
DAILY_CAP_COMMENTS = 20              # Visible LinkedIn action; still well under old ~350/day total
DAILY_CAP_REACTIONS = 20             # Halved — less visible but still counted
DAILY_CAP_DMS = 12                   # Reduced ~33% — keep reasonable for active campaigns
DAILY_CAP_AUTO_REPLIES = 12          # Match DM cap
DAILY_CAP_TOTAL_ACTIONS = 160        # Warm-up must not exhaust the day before invites
DAILY_CAP_WITHDRAWALS = 15           # Stale invite withdrawals per day
DAILY_CAP_INMAILS = 8                # Own bucket; Sales Nav Core is 50 credits/month
# Email is the overflow channel: once the LinkedIn caps are hit, select_channel
# routes remaining prospects here. It needs its own ceiling or hitting the
# LinkedIn cap at midday turns the rest of the queue into cold email. Set above
# DAILY_CAP_DMS because email is not rate-limited by LinkedIn, and well below a
# mailbox provider's hard limit because the binding constraint is sender
# reputation, not the provider's quota. NOT counted in DAILY_CAP_TOTAL_ACTIONS —
# that ceiling bounds what LinkedIn can observe, and email is invisible to it.
DAILY_CAP_EMAILS = 25
DAILY_CAP_BRAND_POSTS = 1            # One published brand post per day
DAILY_CAP_BRAND_ENGAGE_JOBS = 3      # A job that lands one comment can run again the same day
DAILY_CAP_BRAND_PROFILE_JOBS = 1     # One brand_profile scheduler job per day

# Ban risk thresholds (% of daily cap consumed)
BAN_RISK_WARNING_PCT = 0.75          # Log warning at 75% of any cap
BAN_RISK_CRITICAL_PCT = 0.90         # Log critical at 90% of any cap

# ──────────────────────────────────────────────
# Engagement Limits
# ──────────────────────────────────────────────

COMMENT_MAX_CHARS = 200              # Keep comments concise for best engagement

# How long a reservation in `engagements` may sit at status='pending' before a
# later attempt may take the slot over.
#
# A reservation covers exactly one LinkedIn call, so its honest lifetime is
# seconds; anything past this outlived the process that made it. The window has
# to be generous because the only cost of waiting is a delayed engagement,
# while reclaiming a reservation that is merely slow would comment twice on the
# same post.
ENGAGEMENT_RESERVATION_TTL_SECONDS = 3600

# ──────────────────────────────────────────────
# Fit Score Threshold
# ──────────────────────────────────────────────
MIN_FIT_SCORE_THRESHOLD = 0.3        # Skip prospects below this score
# The cloud scheduler's own floor (heylead-api `DEFAULT_MIN_FIT_SCORE`,
# app/services/scheduler.py). On a hosted account the cloud is the sender, so
# this is the number that decides whether a prospect is queued — not the 0.3
# above. Kept here so `icp preview` can print the floor that will apply
# instead of always printing the local one.
HOSTED_MIN_FIT_SCORE = 0.5
MIN_ENGAGEMENT_DELAY_MINUTES = 5     # Min gap between engagements
AUTO_REACT_PROBABILITY = 0.50        # 50% react / 50% comment in auto mode

# ──────────────────────────────────────────────
# Post Intelligence
# ──────────────────────────────────────────────

POST_ANALYSIS_BATCH_SIZE = 5         # Max posts to analyze per batch
POST_TOPIC_UNKNOWN = "unknown"       # Default topic when analysis fails

# Distributed multi-account post collection
DISTRIBUTED_SCAN_BATCH_PER_ACCOUNT = 20    # Contacts per account per run
DISTRIBUTED_SCAN_CONCURRENCY = 5           # Max parallel account workers
DISTRIBUTED_POST_LIMIT = 15                # Posts fetched per contact (up from 5)
DISTRIBUTED_SCAN_SECONDS = 1800            # Every 30 min (down from 3600)
DISTRIBUTED_RESCAN_HOURS = 2               # Per-contact cooldown (down from 4h)
DISTRIBUTED_SEARCH_PER_ACCOUNT_DAILY = 25  # Keyword collector's slice of an account's day
                                           # (see "LinkedIn search budget" below)
DISTRIBUTED_POST_LOOKBACK_DAYS = 14        # Analyze posts from last 14 days (up from 7)
METRIC_SNAPSHOT_MIN_INTERVAL = 7200        # Min 2h between snapshots for same post

# Contact research pipeline
RESEARCH_BATCH_SIZE = 10                   # Contacts researched per scheduler run
RESEARCH_POST_LIMIT = 20                   # Posts fetched per contact during deep research

# Viral post detection
VIRAL_GROWTH_RATE_THRESHOLD = 3.0          # 3x growth between snapshots = viral
VIRAL_MIN_ABSOLUTE_ENGAGEMENT = 20         # Min likes+comments to flag as viral
VIRAL_DETECTION_SECONDS = 3600             # Check every 1 hour

# Keyword watchlist auto-tuning
WATCHLIST_TUNE_MIN_SIGNALS = 10            # Min signals before evaluating a keyword
WATCHLIST_TUNE_MIN_DAYS = 14               # Min days of data before auto-tuning
WATCHLIST_LOW_ROI_THRESHOLD = 0.0          # 0% actioned = low ROI
WATCHLIST_TUNE_SECONDS = 86400             # Run tuning once per day

# Paginated keyword search
KEYWORD_SEARCH_MAX_PAGES = 3               # Fetch up to 3 pages per keyword search

# Author pattern detection
AUTHOR_PATTERN_MIN_POSTS = 3              # Min posts on same topic for pattern
AUTHOR_PATTERN_WINDOW_DAYS = 14           # Window for repeated topic detection
AUTHOR_PATTERN_CONFIDENCE_BOOST = 0.15    # Confidence boost for repeated-topic signals

# ──────────────────────────────────────────────
# LLM Defaults
# ──────────────────────────────────────────────

DEFAULT_LLM_PRIORITY = ["gemini", "claude", "openai"]

# Two tiers, because a 20-token sentiment label and a message a real prospect
# will read do not need the same model. `quality` is for anything a human sees
# or that drives targeting; `fast` is for classification, extraction and short
# utility calls.
LLM_TIER_QUALITY = "quality"
LLM_TIER_FAST = "fast"
# A third, since 17 Sep 2026: `reasoning` is for a JUDGEMENT — every agent
# loop. The hosted backend split judging from writing the same day
# (heylead-api app/services/llm.py); the gemini ids below match it.
LLM_TIER_REASONING = "reasoning"

DEFAULT_LLM_MODELS: dict[str, dict[str, str]] = {
    "gemini": {
        LLM_TIER_REASONING: "gemini-3.1-pro-preview",
        LLM_TIER_QUALITY: "gemini-3.8-flash",
        LLM_TIER_FAST: "gemini-3.5-flash-lite",
    },
    "claude": {
        LLM_TIER_REASONING: "claude-opus-5",
        LLM_TIER_QUALITY: "claude-sonnet-5",
        LLM_TIER_FAST: "claude-haiku-4-5",
    },
    "openai": {
        LLM_TIER_REASONING: "gpt-5.6-terra",
        LLM_TIER_QUALITY: "gpt-5.6-terra",
        LLM_TIER_FAST: "gpt-5.6-luna",
    },
}

# Models that no longer answer. A config file written before these were retired
# would otherwise pin the user to a dead model forever — see _model_for() in
# ai/llm.py, which ignores these and falls back to the current default.
# Models the provider no longer serves. resolve_model() *discards* a user's
# configured model when it appears here, so an entry that is merely old rather
# than dead silently overrides a deliberate choice — and can cost the user more
# per token than the model they picked. Only add an id after confirming the
# provider returns 404 NOT_FOUND for it; a 429 means the model is alive and the
# account is out of quota.
#
# Gemini entries verified against GET /v1beta/models on 2026-08-06:
# gemini-2.0-flash and gemini-2.0-flash-lite are still listed and still serve
# generateContent, so they are deliberately absent from this set.
RETIRED_LLM_MODELS = frozenset({
    "gemini-1.5-flash",
    "gemini-1.5-pro",
})

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models"
CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"

# ──────────────────────────────────────────────
# Unipile API
# ──────────────────────────────────────────────

UNIPILE_POLL_INTERVAL_SECONDS = 4   # How often to check for account during setup
UNIPILE_POLL_TIMEOUT_SECONDS = 120  # Max wait for LinkedIn OAuth completion

# ──────────────────────────────────────────────
# Paths
# ──────────────────────────────────────────────

HEYLEAD_DIR_NAME = ".heylead"
CONFIG_FILE = "config.json"
DB_DIR = "data"
DB_FILE = "heylead.db"
AUTH_DIR = "auth"
COOKIE_FILE = "linkedin.enc"  # Legacy — kept for cleanup of old installs
LOG_DIR = "logs"
LOG_FILE = "heylead.log"
LOG_FILE_JSON = "heylead.json.log"
KB_DIR = "knowledge-base"
EMBEDDINGS_DIR = "embeddings"
BACKUP_DIR = "backups"
MAX_BACKUPS = 5

# ICP generation
DEFAULT_EMBEDDING_MODEL = "all-MiniLM-L6-v2"
FIRECRAWL_API_URL = "https://api.firecrawl.dev/v1"
SERPER_API_URL = "https://google.serper.dev/news"
SERPER_SEARCH_API_URL = "https://google.serper.dev/search"

# ──────────────────────────────────────────────
# Backend Defaults
# ──────────────────────────────────────────────

DEFAULT_BACKEND_URL = "https://heylead.dev"
LOGIN_URL_PATH = "/auth/login-url"
GEMINI_KEY_URL = "https://aistudio.google.com/apikey"

# ──────────────────────────────────────────────
# Misc
# ──────────────────────────────────────────────

MAX_LOG_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
LOG_BACKUP_COUNT = 3
COPILOT_APPROVAL_THRESHOLD = 10  # Auto-prompt for Autopilot after N approvals

# Tier enum-like
TIER_FREE = "free"
TIER_PRO = "pro"

# ──────────────────────────────────────────────
# Campaign type (first-touch prompt family)
# ──────────────────────────────────────────────
# "outbound" is the ordinary cold/warm outreach stack. "job_search" selects
# prompts/outreach_job_search.json (ai/message_generator.select_outreach_prompt),
# the first touch that may name the recipient's company and the role.
CAMPAIGN_TYPE_OUTBOUND = "outbound"
CAMPAIGN_TYPE_JOB_SEARCH = "job_search"
VALID_CAMPAIGN_TYPES = (CAMPAIGN_TYPE_OUTBOUND, CAMPAIGN_TYPE_JOB_SEARCH)
DEFAULT_CAMPAIGN_TYPE = CAMPAIGN_TYPE_OUTBOUND

# Campaign statuses
STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_COMPLETED = "completed"
STATUS_DRAFT = "draft"

# Warm-up enforcement
MIN_WARMUP_ENGAGEMENTS = 0           # Invites go out immediately; engagements run in parallel

# Outreach statuses
OUTREACH_PENDING = "pending"
OUTREACH_INVITED = "invited"
OUTREACH_CONNECTED = "connected"
OUTREACH_MESSAGED = "messaged"
OUTREACH_REPLIED = "replied"
OUTREACH_HOT_LEAD = "hot_lead"
OUTREACH_CLOSED_HAPPY = "closed_happy"
OUTREACH_CLOSED_UNHAPPY = "closed_unhappy"
OUTREACH_OPTED_OUT = "opted_out"
OUTREACH_REVIEW_PENDING = "review_pending"
OUTREACH_SKIPPED = "skipped"

# Sentiment labels
SENTIMENT_POSITIVE = "positive"
SENTIMENT_NEGATIVE = "negative"
SENTIMENT_QUESTION = "question"
SENTIMENT_NEUTRAL = "neutral"
SENTIMENT_OOO = "out_of_office"
SENTIMENT_OPT_OUT = "opt_out"

# ──────────────────────────────────────────────
# Scheduler (Sprint 17)
# ──────────────────────────────────────────────

# Loop intervals (seconds)
SCHEDULER_TICK_SECONDS = 60          # Main loop frequency
SCHEDULER_REPLY_CHECK_SECONDS = 300  # Check replies every 5 min
SCHEDULER_AUTO_RESUME_CHECK_SECONDS = 300  # Check for auto-resumable campaigns every 5 min
SCHEDULER_ENGAGEMENT_SECONDS = 600   # Schedule engagements every 10 min

# Action delays (seconds, randomized — increased ~10% for ban safety)
INVITE_DELAY_MIN = 22 * 60           # 22 min between invitations (was 14)
INVITE_DELAY_MAX = 45 * 60           # 45 min (was 33)
DM_DELAY_MIN = 7 * 60               # 7 min between DMs (was 4)
DM_DELAY_MAX = 15 * 60              # 15 min (was 9)
FOLLOWUP_DELAY_MIN = 30 * 60         # 30 min between follow-up messages (was 22)
FOLLOWUP_DELAY_MAX = 55 * 60         # 55 min (was 38)
ENGAGEMENT_DELAY_MIN = 10 * 60       # 10 min between engagements (was 6)
ENGAGEMENT_DELAY_MAX = 25 * 60       # 25 min (was 17)
FOLLOW_DELAY_MIN = 18 * 60           # 18 min between follows (was 11)
FOLLOW_DELAY_MAX = 40 * 60           # 40 min (was 28)
SCHEDULER_FOLLOW_SECONDS = 900       # Schedule follows every 15 min

# Invite attempt limits
MAX_INVITE_ATTEMPTS = 3              # Max invite attempts per prospect before skipping

# Retry & cleanup
SCHEDULER_MAX_RETRIES = 3            # Max retries per failed job
SCHEDULER_RETRY_DELAY = 300          # 5 min retry delay
SCHEDULER_CLEANUP_DAYS = 7           # Purge completed jobs after N days

# Inbound invitation auto-accept
INBOUND_CHECK_SECONDS = 900          # DEPRECATED: use INBOUND_PIPELINE_SECONDS
INBOUND_PIPELINE_SECONDS = 900       # Unified inbound pipeline every 15 min
# Skill endorsement warm-up
ENDORSE_DELAY_MIN = 10 * 60          # 10 min between endorsements
ENDORSE_DELAY_MAX = 25 * 60          # 25 min
SCHEDULER_ENDORSE_SECONDS = 900      # Schedule endorsements every 15 min

# Profile view warm-up (lightest touch — first step before follow)
PROFILE_VIEW_DELAY_MIN = 10 * 60     # 10 min between profile views (was 6)
PROFILE_VIEW_DELAY_MAX = 25 * 60     # 25 min (was 17)
SCHEDULER_PROFILE_VIEW_WARMUP_SECONDS = 660  # Schedule profile views every 11 min (was 10)

# Auto-reply delays (seconds, randomized — feel human, not robotic)
AUTO_REPLY_DELAY_MIN = 10 * 60       # 10 min minimum delay after reply detected (was 6)
AUTO_REPLY_DELAY_MAX = 25 * 60       # 25 min maximum delay (was 17)
# Upper bound on how stale a prospect message may be and still get an automated
# reply. Without it, an outage leaves months-old messages queued forever and
# they all fire at once on reconnect.
AUTO_REPLY_MAX_AGE_DAYS = 14
SCHEDULER_AUTO_REPLY_SECONDS = 300   # Check for auto-reply candidates every 5 min

# Unanswered-lead alerts (operator email + Overview "Needs attention")
UNANSWERED_LEAD_GRACE_SECONDS = 2 * 3600      # Auto-reply window; alert only after this
UNANSWERED_LEAD_SCAN_SECONDS = 900            # Scan every 15 min
UNANSWERED_LEAD_ALERT_COOLDOWN_SECONDS = 86400  # One email per lead per day
UNANSWERED_LEAD_SCAN_CAP = 10                 # Max POSTs per scan (reconnect backlog)
# Stale invite withdrawal
STALE_INVITE_DAYS = 21               # Withdraw invites older than 21 days
WITHDRAW_CHECK_SECONDS = 3600        # Check stale invites every hour
# A long disconnection builds a large backlog. Withdraw it in small paced
# batches rather than all at once — the run also has to finish inside the
# scheduler's 55s tick budget, so keep batch x max delay well under that.
WITHDRAW_BATCH_PER_RUN = 5           # Max withdrawals per hourly run
WITHDRAW_DELAY_MIN = 2               # Seconds between withdrawals
WITHDRAW_DELAY_MAX = 5

# InMail fallback escalation: invitation → quiet days → one InMail →
# withdrawal at STALE_INVITE_DAYS (unchanged). The gap must stay inside the
# withdrawal window or the escalation fires on an invite already withdrawn.
INMAIL_FALLBACK_AFTER_DAYS = 14      # Quiet days after an invitation before an InMail
# Ceiling for escalation: withdrawal (day 21) never touches the outreach row,
# so without this bound the entire history of long-withdrawn invites becomes
# an InMail queue the first tick after the feature turns on.
INMAIL_FALLBACK_MAX_AGE_DAYS = 35

# How recently a prospect must have accepted for a *completed* campaign to
# still open the conversation. Active campaigns are unbounded — they are still
# running, so their orphan rescue keeps its old reach. A finished campaign is
# different: the only thing it can still owe someone is a reply to an
# acceptance, and past this window a first message stops reading as one.
LATE_ACCEPTER_MAX_AGE_DAYS = 30
INMAIL_DELAY_MIN = 30 * 60           # 30 min between InMail sends
INMAIL_DELAY_MAX = 55 * 60           # 55 min

# Sales Navigator / Premium re-detection. The stored flags are a detection
# result, not a licence feed — a purchase or lapse after setup stays invisible
# until re-probed. Daily, plus on scheduler start and after an entitlement refusal.
SN_REDETECT_SECONDS = 24 * 3600  # Re-probe tier daily

# Pending invitation cache
PENDING_CACHE_TTL_SECONDS = 300      # Cache pending count for 5 minutes

# Job types
JOB_INVITE = "invite"
JOB_SEND_DM = "send_dm"
JOB_FOLLOWUP = "followup"
JOB_ENGAGE = "engage"
JOB_FOLLOW = "follow"
JOB_CHECK_REPLIES = "check_replies"
JOB_ACCEPT_INBOUND = "accept_inbound"       # DEPRECATED: replaced by JOB_PROCESS_INBOUND
JOB_ENDORSE = "endorse"
JOB_PROFILE_VIEW = "profile_view_warmup"
JOB_WITHDRAW_INVITE = "withdraw_invite"
JOB_RECOVER_STUCK_OUTREACHES = "recover_stuck_outreaches"
JOB_REDETECT_SALES_NAV = "redetect_sales_nav"
JOB_EMAIL_INVITE = "email_invite"
JOB_INMAIL = "inmail"
JOB_QUALIFY_INBOUND = "qualify_inbound"      # DEPRECATED: replaced by JOB_PROCESS_INBOUND
JOB_PROCESS_INBOUND = "process_inbound"      # Unified classify-first inbound pipeline
JOB_CHECK_POST_COMMENTS = "check_post_comments"
JOB_AUTO_REPLY = "auto_reply"
JOB_BRAND_POST = "brand_post"
JOB_BRAND_ENGAGE = "brand_engage"
JOB_BRAND_ANALYZE = "brand_analyze"
JOB_BRAND_PROFILE = "brand_profile"

# Signal collection jobs (v1.0)
JOB_COLLECT_KEYWORD_SIGNALS = "collect_keyword_signals"
JOB_SCAN_PROSPECT_POSTS = "scan_prospect_posts"
JOB_SCAN_NETWORK_POSTS = "scan_network_posts"
JOB_COLLECT_POSTS_DISTRIBUTED = "collect_posts_distributed"
JOB_RESEARCH_CONTACTS = "research_contacts"
JOB_BACKFILL_POST_ANALYSIS = "backfill_post_analysis"
JOB_DETECT_VIRAL_POSTS = "detect_viral_posts"
JOB_TUNE_WATCHLISTS = "tune_watchlists"
JOB_OPTIMIZE_SIGNALS = "optimize_signals"
SIGNAL_OPTIMIZE_SECONDS = 86400    # Daily signal self-optimization
JOB_CLASSIFY_SIGNALS = "classify_signals"
JOB_COLLECT_PROFILE_VIEWS = "collect_profile_views"
JOB_DETECT_JOB_CHANGES = "detect_job_changes"
JOB_COLLECT_COMPETITOR_SIGNALS = "collect_competitor_signals"
JOB_COLLECT_HIRING_SIGNALS = "collect_hiring_signals"
JOB_COLLECT_NEWS_SIGNALS = "collect_news_signals"
JOB_COLLECT_WATCHLIST_WEB_SIGNALS = "collect_watchlist_web_signals"

# Job statuses
JOB_PENDING = "pending"
JOB_RUNNING = "running"
JOB_COMPLETED = "completed"
JOB_FAILED = "failed"

# Experiment analysis & A/B testing
SCHEDULER_AB_EVAL_SECONDS = 3600       # Evaluate A/B tests every hour
SCHEDULER_EXPERIMENT_SECONDS = 21600   # Run experiment analysis every 6 hours
HEADLINE_TEST_DURATION_DAYS = 14       # Auto-complete headline tests after 2 weeks
HEADLINE_MIN_SAMPLE = 15              # Min prospects per variant for headline eval

# Inbound qualification pipeline
INBOUND_QUALIFY_SECONDS = 900          # Qualify new signals every 15 min
POST_COMMENT_CHECK_SECONDS = 1800      # Check post comments every 30 min
INBOUND_ENGAGE_CONFIDENCE = 0.7        # Auto-engage threshold
INBOUND_ASK_PURPOSE_CONFIDENCE = 0.4   # Ask-purpose threshold
INBOUND_MAX_DM_ATTEMPTS = 3           # Max chat resolution retries before dismissing
PUBLISHED_POST_MONITOR_DAYS = 14       # Monitor comments for 14 days

# ──────────────────────────────────────────────
# Brand Strategy Automation
# ──────────────────────────────────────────────

# Scheduling check intervals
BRAND_POST_CHECK_SECONDS = 3600          # Check if brand post is due every hour
BRAND_ENGAGE_CHECK_SECONDS = 4 * 3600    # Check brand engagement every 4 hours
BRAND_PROFILE_CHECK_SECONDS = 3600       # Check profile/photo actions every hour
BRAND_LIFECYCLE_CHECK_SECONDS = 3600     # Check lifecycle every hour

# Action delays (randomized for humanization)
BRAND_POST_DELAY_MIN = 30 * 60          # 30 min
BRAND_POST_DELAY_MAX = 4 * 3600         # 4 hours
BRAND_ENGAGE_DELAY_MIN = 5 * 60         # 5 min
BRAND_ENGAGE_DELAY_MAX = 20 * 60        # 20 min
BRAND_PROFILE_DELAY_MIN = 5 * 60        # 5 min
BRAND_PROFILE_DELAY_MAX = 20 * 60       # 20 min

# Lifecycle
BRAND_REANALYZE_DAYS = 28               # Re-analyze every 4 weeks
BRAND_DEFAULT_WEEKLY_POSTS = 2
BRAND_DEFAULT_DAILY_COMMENTS = 5

# ──────────────────────────────────────────────
# Signal-Based Selling (v1.0)
# ──────────────────────────────────────────────

# Signal types
SIGNAL_KEYWORD_MENTION = "keyword_mention"
SIGNAL_PROSPECT_POST = "prospect_post"
SIGNAL_JOB_CHANGE = "job_change"
SIGNAL_COMPETITOR_MENTION = "competitor_mention"
SIGNAL_HIRING_SURGE = "hiring_surge"
SIGNAL_FUNDING_EVENT = "funding_event"
SIGNAL_NEWS_EVENT = "news_event"
SIGNAL_PROFILE_VIEW = "profile_view"
SIGNAL_POST_ENGAGEMENT = "post_engagement"
SIGNAL_COMMENTER_MATCH = "commenter_match"

# Profile change sub-types (refined from job_change)
SIGNAL_COMPANY_CHANGE = "company_change"
SIGNAL_PROMOTION = "promotion"
SIGNAL_HEADLINE_CHANGE = "headline_change"
SIGNAL_HEADLINE_INTENT = "headline_intent"
SIGNAL_VIRAL_POST = "viral_post"

# Granular news sub-types (Phase 1 intent expansion)
SIGNAL_NEWS_FUNDING = "news_funding"
SIGNAL_NEWS_ACQUISITION = "news_acquisition"
SIGNAL_NEWS_EXEC_HIRE = "news_exec_hire"
SIGNAL_NEWS_EXPANSION = "news_expansion"
SIGNAL_NEWS_PRODUCT_LAUNCH = "news_product_launch"
SIGNAL_NEWS_LAYOFFS = "news_layoffs"

# Company page engagement sub-types (Phase 2 intent expansion)
SIGNAL_COMPANY_POST_COMMENT = "company_post_comment"
SIGNAL_COMPANY_POST_REACTION = "company_post_reaction"
SIGNAL_COMPANY_FOLLOWER = "company_follower"

# Website visitor tracking sub-types (Phase 3 intent expansion)
SIGNAL_WEBSITE_VISIT = "website_visit"
SIGNAL_WEBSITE_HIGH_INTENT = "website_high_intent"

# Post content intent sub-types (Phase 5 — post intelligence expansion)
SIGNAL_POST_PAIN_POINT = "post_pain_point"           # Prospect posts about a problem we solve
SIGNAL_POST_TECH_EVALUATION = "post_tech_evaluation"  # Prospect evaluating tools/vendors
SIGNAL_POST_BUDGET_SIGNAL = "post_budget_signal"      # Mentions budget, investment, ROI
SIGNAL_POST_SEEKING_RECS = "post_seeking_recs"        # Asks network for recommendations
SIGNAL_POST_NEGATIVE_EXPERIENCE = "post_negative_experience"  # Complaints about competitor/category

# Reaction type intelligence sub-types (Phase 5)
SIGNAL_REACTION_INSIGHTFUL_COMPETITOR = "reaction_insightful_competitor"  # INSIGHTFUL on competitor post
SIGNAL_REACTION_PATTERN = "reaction_pattern"           # 3+ reactions in 1 week (active on LI)

# Repost/share intelligence sub-types (Phase 5)
SIGNAL_RESHARES_COMPETITOR = "reshares_competitor"     # Prospect reposts competitor content
SIGNAL_RESHARES_OUR_CONTENT = "reshares_our_content"   # Prospect reposts our company content

# Company post topic sub-types (Phase 5)
SIGNAL_COMPANY_HIRING_POST = "company_hiring_post"     # Company posts about open roles
SIGNAL_COMPANY_MILESTONE_POST = "company_milestone_post"  # Revenue/customer/growth metrics
SIGNAL_COMPANY_TECH_STACK_POST = "company_tech_stack_post"  # Company shares tools they use

# Comment mining sub-types (Phase 5 — Tier 3)
SIGNAL_COMPETITOR_POST_COMMENTER = "competitor_post_commenter"  # Non-contact comments on competitor post
SIGNAL_INDUSTRY_THREAD_PARTICIPANT = "industry_thread_participant"  # ICP match in industry post comments
SIGNAL_PROSPECT_COMMENTS_ON_COMPETITOR = "prospect_comments_on_competitor"  # Known prospect on competitor post
# Dedup bookmark that comment mining already processed a post. Not a person.
SIGNAL_COMMENT_MINE_MARKER = "comment_mine_marker"

# Seller detection — prospect does LinkedIn outreach manually (high-value for SDR tools)
SIGNAL_LINKEDIN_SELLER = "linkedin_seller"

# Off-LinkedIn watchlist web signals (Serper site: search)
SIGNAL_REDDIT_MENTION = "reddit_mention"
SIGNAL_HN_MENTION = "hn_mention"
SIGNAL_G2_REVIEW = "g2_review"
SIGNAL_ATS_HIRING = "ats_hiring"

# Signal intents
SIGNAL_INTENT_BUYING = "buying_signal"
SIGNAL_INTENT_PAIN_POINT = "pain_point"
SIGNAL_INTENT_COMPETITOR_EVAL = "competitor_eval"
SIGNAL_INTENT_JOB_SEEKING = "job_seeking"
SIGNAL_INTENT_THOUGHT_LEADERSHIP = "thought_leadership"
SIGNAL_INTENT_UNKNOWN = "unknown"
SIGNAL_INTENT_NOT_RELEVANT = "not_relevant"

# Signal statuses
SIGNAL_STATUS_NEW = "new"
SIGNAL_STATUS_CLASSIFIED = "classified"
SIGNAL_STATUS_ACTIONED = "actioned"
SIGNAL_STATUS_DISMISSED = "dismissed"
SIGNAL_STATUS_EXPIRED = "expired"
SIGNAL_STATUS_SKIPPED = "skipped"

# One-invite exception when a classified hook lands on someone below min_fit.
SIGNAL_FIT_OVERRIDE = "signal_fit_override"

# Post-level hooks that may write the invite note and the fit override.
CLASSIFIED_POST_HOOK_TYPES = frozenset({
    SIGNAL_POST_PAIN_POINT,
    SIGNAL_POST_TECH_EVALUATION,
    SIGNAL_POST_BUDGET_SIGNAL,
    SIGNAL_POST_SEEKING_RECS,
    SIGNAL_POST_NEGATIVE_EXPERIENCE,
})
# Event types write a note only when the classifier produced a hook.
CLASSIFIED_EVENT_HOOK_TYPES = frozenset({
    SIGNAL_COMPETITOR_MENTION,
    SIGNAL_COMPANY_CHANGE,
    SIGNAL_PROMOTION,
    SIGNAL_JOB_CHANGE,
    SIGNAL_HEADLINE_INTENT,
    SIGNAL_KEYWORD_MENTION,
})
NEWS_SIGNAL_TYPES = frozenset({
    SIGNAL_FUNDING_EVENT,
    SIGNAL_NEWS_EVENT,
    SIGNAL_NEWS_FUNDING,
    SIGNAL_NEWS_ACQUISITION,
    SIGNAL_NEWS_EXEC_HIRE,
    SIGNAL_NEWS_EXPANSION,
    SIGNAL_NEWS_PRODUCT_LAUNCH,
    SIGNAL_NEWS_LAYOFFS,
})
WEB_SIGNAL_TYPES = frozenset({
    SIGNAL_REDDIT_MENTION,
    SIGNAL_HN_MENTION,
    SIGNAL_G2_REVIEW,
    SIGNAL_ATS_HIRING,
})
# Company-level rows have no person LinkedIn id. Activator attaches them to
# in-campaign contacts at that company, or parks them quietly.
COMPANY_LEVEL_SIGNAL_TYPES = NEWS_SIGNAL_TYPES | WEB_SIGNAL_TYPES | frozenset({
    SIGNAL_HIRING_SURGE,
    SIGNAL_COMPANY_HIRING_POST,
    SIGNAL_COMPANY_MILESTONE_POST,
    SIGNAL_COMPANY_TECH_STACK_POST,
    SIGNAL_COMMENT_MINE_MARKER,
})
# Lower rank wins when several classified signals name the same person.
SIGNAL_TYPE_RANK: dict[str, int] = {
    SIGNAL_POST_BUDGET_SIGNAL: 0,
    SIGNAL_POST_NEGATIVE_EXPERIENCE: 1,
    SIGNAL_POST_TECH_EVALUATION: 2,
    SIGNAL_POST_PAIN_POINT: 3,
    SIGNAL_POST_SEEKING_RECS: 4,
    SIGNAL_COMPETITOR_MENTION: 10,
    SIGNAL_COMPANY_CHANGE: 11,
    SIGNAL_PROMOTION: 12,
    SIGNAL_HEADLINE_INTENT: 13,
    SIGNAL_KEYWORD_MENTION: 14,
    SIGNAL_JOB_CHANGE: 15,
    SIGNAL_PROFILE_VIEW: 20,
    SIGNAL_PROSPECT_POST: 30,
}

# Collection intervals (seconds)
SIGNAL_KEYWORD_POLL_SECONDS = 1800       # Keyword search every 30 min
SIGNAL_PROSPECT_SCAN_SECONDS = 3600      # Scan prospect posts every 1 hour
# Classify new signals every 5 min. This is the drain-rate lever, not
# SIGNAL_CLASSIFY_MAX_PER_RUN — that one is capped by the 44s a job is given.
#
# It was 900s, which with a bound of 8 is 32/hour. Measured over 816 hours of
# live history to 22 Aug 2026, signals arrive at a mean of 41.6/hour, so 900s
# is under water — survivable before this branch only because the run timed
# out and the queue retried it SCHEDULER_MAX_RETRIES times per scheduled row,
# running the job ~4x as often as this constant said and recording every run
# as a failure. That churn is what kept the backlog moving. Removing it means
# the cadence has to carry what the retries were carrying.
#
# 8 x 12 runs/hour = 96/hour, 2.3x the sustained mean, and slightly fewer model
# calls than the retry churn was already making. Sized against the mean, not
# the peak: hourly arrivals are p90 100, p99 319, max 2308, and matching p99
# would need ~27 model calls per run — ~90s against a 44s job. Bursts are
# absorbed by the backlog instead and drained in the quiet hours either side;
# a p99 hour clears in ~3h against a 7-day shortest TTL.
SIGNAL_CLASSIFY_SECONDS = 300
SIGNAL_PROFILE_VIEW_SECONDS = 3600       # Check profile viewers every 1 hour
SIGNAL_JOB_CHANGE_SCAN_SECONDS = 14400   # Scan for job changes every 4 hours
SIGNAL_COMPETITOR_POLL_SECONDS = 1800    # Poll competitor keywords every 30 min
SIGNAL_HIRING_SCAN_SECONDS = 14400      # Scan for hiring surges every 4 hours
SIGNAL_NEWS_SCAN_SECONDS = 14400        # Scan for news/funding every 4 hours
SIGNAL_WATCHLIST_WEB_SCAN_SECONDS = 14400  # Serper watchlist web search every 4 hours

# ── LinkedIn search budget (issue #76) ────────────────────────────────
# Three collectors book their searches against this budget:
#
#   keyword_collector     search_posts, spread over the whole account pool
#   competitor_collector  search_posts, primary account only
#   hiring_collector      search_jobs,  primary account only
#
# They all spend the same account's daily search allowance, which is what
# the cap exists to protect, so there is one budget and it is split three
# ways. Each collector books its searches against its OWN counter key.
# A single shared counter does not work: whichever collector runs first
# drains it and the other two are dead for the rest of the day, and
# keyword_collector (every 30 min) wins that race every time.
#
# What makes three counters into one budget is that the shares add up on
# the account that carries all three — the primary:
#
#     DISTRIBUTED_SEARCH_PER_ACCOUNT_DAILY  25   keyword, per account
#   + SIGNAL_DAILY_COMPETITOR_SEARCHES      10   primary account only
#   + SIGNAL_DAILY_HIRING_SEARCHES          15   primary account only
#   = LINKEDIN_SEARCH_PER_ACCOUNT_DAILY     50
#
# keyword_collector also keeps a day-stamped per-account ledger
# (get_daily_account_search_counts) and skips an account that has spent
# its 25. Round-robin alone was not enough — the index restarts at 0 every
# run, so a keyword queue shorter than the pool sent every run's first
# search to the same account: measured at 48 keyword searches a day on the
# first account with two others idle.
#
# WHAT THAT LEDGER DOES AND DOES NOT BOUND. It bounds what THIS CLIENT
# ASKS FOR — at most 50 requests a day attributed to the first account and
# 25 to each other, which is what keeps the three collectors from
# compounding. It is NOT a bound on what any particular LinkedIn account
# is asked for, and it cannot be, in either mode:
#
#   self-hosted   UnipileClient has no network_pool_status, so
#                 _get_pool_accounts always falls through to
#                 [fallback_account_id]. The pool is exactly one account
#                 and "every other pool account" is an empty set.
#   backend       The only mode where the pool exceeds one — and
#                 BackendClient.search_posts / search_jobs send
#                 {keywords, limit, cursor} and NOT the account id, so the
#                 backend decides which account serves each search. The
#                 ledger is then bookkeeping over a distinction this
#                 process does not control, and the real per-account limit
#                 is the backend's to enforce.
#
# So: three collectors, one client-side budget, split three ways so none
# starves the others. Per-account enforcement lives upstream.
# test_collector_search_budget.py measures the client-side split over a
# simulated day at queue lengths 1, 2, 4, 8 and 120, using a fake pool —
# note the fake reports pool status AND honours a per-call account id,
# which no production client does at once. Each run reads its counter and
# the ledger once at the start, so the bound is per run: it rests on the
# scheduler never having two runs of one collector in flight
# (scheduler/planner.py returns early while one is still pending).
#
# That is a bound on THIS BUDGET's traffic, not on the account's. Other
# search call sites exist and book against nothing:
#
#   comment_mining_collector   scheduled every 2 h; up to 5 competitor
#                              watchlists × 3 terms + 3 keyword watchlists
#                              × 2 terms = 21 search_posts a run, all on the
#                              primary, so up to 252 a day on its own
#   company_page_collector     scheduled hourly; one search_posts per
#                              company watchlist that falls through to the
#                              search strategy, on the primary
#   services/intent_signals    search_jobs, on the primary
#   tools/brand_strategy.py    on demand
#   tools/engage_prospect.py   on demand
#
# So the primary's real daily search traffic can be several times 50, and
# nothing here bounds it. Bringing those under one cap means giving them
# counter keys and shares in this block; until then, read 50 as this
# budget's ceiling and not as the account's.
#
# One unit = one search call site, and it is booked only when a request
# actually reaches LinkedIn (linkedin/search_traffic.py). One 30-minute
# tick of connect failures used to fill every counter for the day.
# keyword_collector fetches up to KEYWORD_SEARCH_MAX_PAGES pages per unit;
# the other two fetch one page.
LINKEDIN_SEARCH_PER_ACCOUNT_DAILY = 50   # The allowance this budget is sized to
SIGNAL_DAILY_COMPETITOR_SEARCHES = 10    # Competitor collector's share of it
SIGNAL_DAILY_HIRING_SEARCHES = 15        # Hiring collector's share of it

# Counter keys, one per collector. get_daily_signal_search_count and
# record_signal_search are keyed by these; a collector that reads one key
# and writes another (or writes nothing) is the #76 bug.
SIGNAL_SEARCH_TYPE_KEYWORD = "keyword"
SIGNAL_SEARCH_TYPE_COMPETITOR = "competitor"
SIGNAL_SEARCH_TYPE_HIRING = "hiring"
SIGNAL_SEARCH_TYPE_COMMENT_MINING = "comment_mining"
SIGNAL_SEARCH_TYPE_COMPANY_PAGE = "company_page"

# Batch sizes & limits
SIGNAL_PROSPECT_SCAN_BATCH = 20          # Max prospects scanned per run
SIGNAL_CLASSIFY_BATCH = 30               # Ceiling on rows READ per run, not a target
# What ends a classify run: a COUNT of model calls, not a stopwatch.
#
# A wall-clock bound made the run's size a function of model latency, and the
# number was picked against the wrong ceiling. A tick is cancelled at
# _TICK_BUDGET_SECONDS (55s), but a JOB never gets the whole tick: _tick holds
# back _TICK_PLANNING_RESERVE_SECONDS for the planning that follows job
# execution, and _process_ready_jobs clamps each job's timeout to
# int(deadline - now) — 44s once the tick's startup has taken the fractional
# second. The old 45.0s self-stop was one second ABOVE that, so it could never
# fire: asyncio.wait_for reached the run first on every single tick, and the
# live log for 21 Aug 2026 is an unbroken column of
# "failed in 44005ms [network_error]: TimeoutError" over a classified count
# that was climbing the whole time. Rows commit one at a time, so nothing was
# lost — but a job that did real work was filed as a network failure, which
# buries genuine failures and earns a retry the queue did not need.
#
# A count is knowable before the run starts, so the run ends by RETURNING its
# summary. That is what makes success-with-remaining-work reportable at all:
# a killed job has no return value to report it with.
#
# 8 model calls at the ~3.3s a classification measures in the live log is
# ~26s, inside the 30s backstop below and well inside the 44s the job gets.
# Drain rate is therefore 8 per run x 4 runs/hour = 32/hour, chosen rather
# than observed. Raise it by running the job MORE OFTEN
# (SIGNAL_CLASSIFY_SECONDS) rather than by classifying more per run: per-run
# is capped by the job timeout, frequency is not.
SIGNAL_CLASSIFY_MAX_PER_RUN = 8
# Backstop for the count above, for when latency is worse than the ~3.3s the
# count is sized against. It is not the normal exit — the count is — and it
# must stay far enough under the 44s a job is given to cover the last row the
# loop is allowed to start, which can begin just under this and then run to
# SIGNAL_CLASSIFY_ROW_TIMEOUT_SECONDS. 30 + 10 = 40 against 44.
# tests/test_signal_classify_backlog_drain.py holds those numbers together;
# the version of that test which compared this against the 55s TICK budget was
# green throughout the outage described above.
SIGNAL_CLASSIFY_BUDGET_SECONDS = 30.0
# Cap on a single classification. Without one, a stalled call blows the job's
# budget on its own however the batch is bounded: ai/llm.py allows a 120s read,
# nearly three times the 44s the job has. A row that hits this cap is counted
# as an error and the run carries on with the next one.
SIGNAL_CLASSIFY_ROW_TIMEOUT_SECONDS = 10.0
# Share of each classify run spent draining the OLDEST unclassified signals.
# The rest goes to the newest. A pure newest-first batch buried everything
# older than the arrival rate for good (#68); a pure oldest-first one would
# delay every fresh, actionable signal behind the backlog instead. Two thirds
# to the old end because the backlog is larger than a day of arrivals.
# This is a RATIO, not a row count: _select_classification_batch interleaves in
# this proportion so a bounded run keeps it, and the SIGNAL_CLASSIFY_MAX_PER_RUN
# rows a run really reaches come out roughly 5 old / 3 fresh. That does NOT clear a day of
# arrivals from the fresh end (~415/day against a measured ~1,000/day), so a
# fresh signal is reached in hours rather than minutes; what it does buy is
# that it is reached at all, instead of sitting behind the whole backlog. The
# remaining gap is SIGNAL_CLASSIFY_MAX_PER_RUN and how often the job runs, not
# this ratio.
SIGNAL_CLASSIFY_OLDEST_SHARE = 2 / 3
SIGNAL_PROSPECT_POST_LOOKBACK_DAYS = 7   # Only analyze posts from last 7 days
SIGNAL_JOB_CHANGE_BATCH = 50             # Max profiles per job change scan
SIGNAL_JOB_CHANGE_RESCAN_DAYS = 7        # Re-scan each contact weekly

# Signal scoring weights
SIGNAL_WEIGHT_KEYWORD_MENTION = 0.30
SIGNAL_WEIGHT_PROSPECT_POST = 0.35
SIGNAL_WEIGHT_JOB_CHANGE = 0.50
SIGNAL_WEIGHT_COMPETITOR_MENTION = 0.40
SIGNAL_WEIGHT_HIRING_SURGE = 0.25
SIGNAL_WEIGHT_FUNDING_EVENT = 0.30
SIGNAL_WEIGHT_NEWS_EVENT = 0.20
SIGNAL_WEIGHT_PROFILE_VIEW = 0.50

# Curated / job-search signals. These are hand-verified rather than inferred:
# a named person at a target company with a specific open role, or a recruiter
# whose job is filling it. They outrank inferred signals deliberately — they
# previously fell to the 0.2 default and ranked below a stranger's random post.
SIGNAL_WEIGHT_TARGET_COMPANY_RECRUITER = 0.90
SIGNAL_WEIGHT_TARGET_COMPANY_CONTACT = 0.85
SIGNAL_WEIGHT_NETWORK_HUB_CONTACT = 0.70
SIGNAL_WEIGHT_POST_ENGAGEMENT = 0.20
SIGNAL_WEIGHT_COMMENTER_MATCH = 0.35
SIGNAL_WEIGHT_COMPANY_CHANGE = 0.55
SIGNAL_WEIGHT_PROMOTION = 0.45
SIGNAL_WEIGHT_HEADLINE_CHANGE = 0.25
SIGNAL_WEIGHT_HEADLINE_INTENT = 0.40
SIGNAL_WEIGHT_VIRAL_POST = 0.45
# Dedup bookmark, not outreach evidence. Must be registered so backfill
# does not warn and default it to 0.20 as if it were a weak person signal.
SIGNAL_WEIGHT_COMMENT_MINE_MARKER = 0.0

# Granular news weights (Phase 1 intent expansion)
SIGNAL_WEIGHT_NEWS_FUNDING = 0.35
SIGNAL_WEIGHT_NEWS_ACQUISITION = 0.30
SIGNAL_WEIGHT_NEWS_EXEC_HIRE = 0.40
SIGNAL_WEIGHT_NEWS_EXPANSION = 0.25
SIGNAL_WEIGHT_NEWS_PRODUCT_LAUNCH = 0.20
SIGNAL_WEIGHT_NEWS_LAYOFFS = -0.15    # Negative: budget cuts likely

# Company page engagement weights (Phase 2 intent expansion)
SIGNAL_WEIGHT_COMPANY_POST_COMMENT = 0.35
SIGNAL_WEIGHT_COMPANY_POST_REACTION = 0.40
SIGNAL_WEIGHT_COMPANY_FOLLOWER = 0.55

# Website visitor tracking weights (Phase 3 intent expansion)
SIGNAL_WEIGHT_WEBSITE_VISIT = 0.25
SIGNAL_WEIGHT_WEBSITE_HIGH_INTENT = 0.45

# Post content intent weights (Phase 5 — post intelligence expansion)
# 0.55 × 1.0 fresh / 0.60 ceiling × 0.80 = 0.733, so one classified hook
# clears SIGNAL_AUTO_OUTREACH_THRESHOLD (0.65) on its own.
SIGNAL_WEIGHT_POST_PAIN_POINT = 0.55
SIGNAL_WEIGHT_POST_TECH_EVALUATION = 0.55
SIGNAL_WEIGHT_POST_BUDGET_SIGNAL = 0.55
SIGNAL_WEIGHT_POST_SEEKING_RECS = 0.55
SIGNAL_WEIGHT_POST_NEGATIVE_EXPERIENCE = 0.55

# Reaction type intelligence weights (Phase 5)
SIGNAL_WEIGHT_REACTION_INSIGHTFUL_COMPETITOR = 0.40
SIGNAL_WEIGHT_REACTION_PATTERN = 0.25

# Repost/share intelligence weights (Phase 5)
SIGNAL_WEIGHT_RESHARES_COMPETITOR = 0.45
SIGNAL_WEIGHT_RESHARES_OUR_CONTENT = 0.60

# Company post topic weights (Phase 5)
SIGNAL_WEIGHT_COMPANY_HIRING_POST = 0.40
SIGNAL_WEIGHT_COMPANY_MILESTONE_POST = 0.35
SIGNAL_WEIGHT_COMPANY_TECH_STACK_POST = 0.45

# Comment mining weights (Phase 5 — Tier 3)
SIGNAL_WEIGHT_COMPETITOR_POST_COMMENTER = 0.40
SIGNAL_WEIGHT_INDUSTRY_THREAD_PARTICIPANT = 0.35
SIGNAL_WEIGHT_PROSPECT_COMMENTS_ON_COMPETITOR = 0.50

# Seller detection weight — prospect does LinkedIn outreach manually
SIGNAL_WEIGHT_LINKEDIN_SELLER = 0.35

# Off-LinkedIn watchlist web weights
SIGNAL_WEIGHT_REDDIT_MENTION = 0.30
SIGNAL_WEIGHT_HN_MENTION = 0.30
SIGNAL_WEIGHT_G2_REVIEW = 0.40
SIGNAL_WEIGHT_ATS_HIRING = 0.25

# Negative signal weights (subtract from composite score)
SIGNAL_WEIGHT_COMPETITOR_USER = -0.30    # Uses a known competitor product
SIGNAL_WEIGHT_RECENT_CHURN = -0.20       # Previously closed_unhappy
SIGNAL_WEIGHT_BAD_FIT = -0.15            # ICP match score below threshold

# Negative signal type identifiers
SIGNAL_COMPETITOR_USER = "competitor_user"
SIGNAL_RECENT_CHURN = "recent_churn"
SIGNAL_BAD_FIT = "bad_fit"

# Signal freshness TTLs (seconds)
SIGNAL_TTL_KEYWORD_MENTION = 7 * 86400    # 7 days
SIGNAL_TTL_PROSPECT_POST = 14 * 86400     # 14 days
SIGNAL_TTL_JOB_CHANGE = 30 * 86400        # 30 days
SIGNAL_TTL_COMPETITOR_MENTION = 14 * 86400 # 14 days
SIGNAL_TTL_FUNDING_EVENT = 30 * 86400      # 30 days
SIGNAL_TTL_HIRING_SURGE = 14 * 86400       # 14 days
SIGNAL_TTL_NEWS_EVENT = 14 * 86400         # 14 days
SIGNAL_TTL_PROFILE_VIEW = 3 * 86400        # 3 days
SIGNAL_TTL_COMMENTER_MATCH = 14 * 86400    # 14 days
SIGNAL_TTL_DEFAULT = 14 * 86400            # 14 days fallback
SIGNAL_TTL_COMPANY_CHANGE = 30 * 86400     # 30 days
SIGNAL_TTL_PROMOTION = 30 * 86400          # 30 days
SIGNAL_TTL_HEADLINE_CHANGE = 14 * 86400    # 14 days
SIGNAL_TTL_HEADLINE_INTENT = 7 * 86400     # 7 days (intent decays fast)
SIGNAL_TTL_VIRAL_POST = 7 * 86400          # 7 days

# Granular news TTLs (Phase 1 intent expansion)
SIGNAL_TTL_NEWS_FUNDING = 30 * 86400       # 30 days (funding = long-term opportunity)
SIGNAL_TTL_NEWS_ACQUISITION = 30 * 86400   # 30 days (M&A transition takes time)
SIGNAL_TTL_NEWS_EXEC_HIRE = 30 * 86400     # 30 days (new exec evaluates for weeks)
SIGNAL_TTL_NEWS_EXPANSION = 14 * 86400     # 14 days
SIGNAL_TTL_NEWS_PRODUCT_LAUNCH = 14 * 86400  # 14 days
SIGNAL_TTL_NEWS_LAYOFFS = 14 * 86400       # 14 days

# Company page engagement TTLs (Phase 2)
SIGNAL_TTL_COMPANY_POST_COMMENT = 14 * 86400  # 14 days
SIGNAL_TTL_COMPANY_POST_REACTION = 7 * 86400  # 7 days
SIGNAL_TTL_COMPANY_FOLLOWER = 14 * 86400      # 14 days

# Website visitor tracking TTLs (Phase 3 intent expansion)
SIGNAL_TTL_WEBSITE_VISIT = 7 * 86400            # 7 days
SIGNAL_TTL_WEBSITE_HIGH_INTENT = 7 * 86400      # 7 days

# Post content intent TTLs (Phase 5)
SIGNAL_TTL_POST_PAIN_POINT = 14 * 86400         # 14 days
SIGNAL_TTL_POST_TECH_EVALUATION = 14 * 86400    # 14 days
SIGNAL_TTL_POST_BUDGET_SIGNAL = 14 * 86400      # 14 days
SIGNAL_TTL_POST_SEEKING_RECS = 7 * 86400        # 7 days (urgent, decays fast)
SIGNAL_TTL_POST_NEGATIVE_EXPERIENCE = 14 * 86400  # 14 days

# Reaction/repost/company post TTLs (Phase 5)
SIGNAL_TTL_REACTION_INSIGHTFUL_COMPETITOR = 7 * 86400  # 7 days
SIGNAL_TTL_REACTION_PATTERN = 7 * 86400          # 7 days
SIGNAL_TTL_RESHARES_COMPETITOR = 14 * 86400      # 14 days
SIGNAL_TTL_RESHARES_OUR_CONTENT = 7 * 86400      # 7 days
SIGNAL_TTL_COMPANY_HIRING_POST = 14 * 86400      # 14 days
SIGNAL_TTL_COMPANY_MILESTONE_POST = 14 * 86400   # 14 days
SIGNAL_TTL_COMPANY_TECH_STACK_POST = 14 * 86400  # 14 days

# Comment mining TTLs (Phase 5)
SIGNAL_TTL_COMPETITOR_POST_COMMENTER = 14 * 86400  # 14 days
SIGNAL_TTL_INDUSTRY_THREAD_PARTICIPANT = 14 * 86400  # 14 days
SIGNAL_TTL_PROSPECT_COMMENTS_ON_COMPETITOR = 14 * 86400  # 14 days

# Off-LinkedIn watchlist web TTLs
SIGNAL_TTL_REDDIT_MENTION = 7 * 86400   # 7 days (same band as keyword_mention)
SIGNAL_TTL_HN_MENTION = 7 * 86400       # 7 days
SIGNAL_TTL_G2_REVIEW = 14 * 86400       # 14 days
SIGNAL_TTL_ATS_HIRING = 14 * 86400      # 14 days (same band as hiring_surge)

# Comment mining collection interval (Phase 5)
SIGNAL_COMMENT_MINING_SECONDS = 7200    # Mine comments every 2 hours
COMMENT_MINING_MAX_POSTS = 10           # Max viral posts to mine per run
COMMENT_MINING_MIN_COMMENTS = 20        # Only mine posts with 20+ comments
COMMENT_MINING_MAX_COMMENTERS_PER_POST = 50  # Max commenters to extract per post

# Post intent classification constants (Phase 5)
POST_INTENT_BATCH_SIZE = 15             # Max posts to classify per run
POST_INTENT_CLASSIFY_SECONDS = 1800     # Classify post intents every 30 min

# Company page collection intervals (Phase 2)
SIGNAL_COMPANY_PAGE_POLL_SECONDS = 3600    # Poll company page posts every 1 hour
SIGNAL_COMPANY_FOLLOWER_POLL_SECONDS = 14400  # Check followers every 4 hours

# Layoff keyword patterns (used by news_collector)
NEWS_LAYOFF_PATTERNS = [
    "layoffs", "laid off", "lays off", "laying off",
    "downsizing", "workforce reduction", "job cuts",
    "restructuring", "headcount reduction", "rif ",
    "reduction in force", "furlough",
]

# ──────────────────────────────────────────────
# Signal Activation (v1.1)
# ──────────────────────────────────────────────

# Activation thresholds (calibrated for best-signal-as-baseline scoring)
SIGNAL_AUTO_OUTREACH_THRESHOLD = 0.65   # Auto-create outreach with signal context
SIGNAL_HOT_SKIP_WARMUP_THRESHOLD = 0.80 # Skip warm-up, go direct to invite
SIGNAL_BOOST_THRESHOLD = 0.40           # Add to campaign as warm lead
SIGNAL_WARM_THRESHOLD = 0.20            # Boost engagement priority for existing
SIGNAL_ICP_MIN_OVERLAP = 0.2            # Min ICP overlap to auto-add to campaign
SIGNAL_ICP_MATCH_THRESHOLD = 0.25       # Min ICP match score for signal-activated prospects

# Activation-qualifying intents (sets, not lists — used for `in` checks)
SIGNAL_OUTREACH_INTENTS = frozenset({"buying_signal", "pain_point", "competitor_eval"})
SIGNAL_BOOST_INTENTS = frozenset({"buying_signal", "pain_point", "competitor_eval", "thought_leadership"})

# Behavioral signal types — express direct interest in YOU (not in a topic).
# These bypass the text-classified intent gate when ICP-matched.
SIGNAL_BEHAVIORAL_TYPES = frozenset({
    "profile_view", "company_follower", "website_visit", "website_high_intent",
    "company_post_reaction", "company_post_comment",
})
SIGNAL_BEHAVIORAL_ICP_THRESHOLD = 0.25  # Min ICP match score for behavioral → outreach
SIGNAL_BEHAVIORAL_AUTO_THRESHOLD = 0.40  # Lower threshold for behavioral signals (direct interest)

# Scheduler
JOB_ACTIVATE_SIGNALS = "activate_signals"
SIGNAL_ACTIVATE_SECONDS = 900           # Activate signals every 15 min

# Company page engagement scheduler jobs (Phase 2)
JOB_COLLECT_COMPANY_PAGE = "collect_company_page"
JOB_COLLECT_COMPANY_FOLLOWERS = "collect_company_followers"

# Compound intent detection (Week 10 — signal stacking)
COMPOUND_INTENT_LOOKBACK_DAYS = 14      # Window for detecting stacked signals
COMPOUND_INTENT_MIN_TYPES = 2           # Minimum distinct signal types for compound event

# Compound intent patterns: frozenset of signal types → (event_type, score_boost)
# When 2+ matching signal types appear within LOOKBACK_DAYS for the same prospect,
# a synthetic intent_event is created with the given boost.
COMPOUND_INTENT_PATTERNS: dict[frozenset[str], tuple[str, float]] = {
    # Legacy job_change patterns (backward compat for existing signals in DB)
    frozenset({"job_change", "hiring_surge"}): ("new_leader_building", 0.90),
    frozenset({"job_change", "keyword_mention"}): ("new_role_exploring", 0.80),
    frozenset({"job_change", "competitor_mention"}): ("new_role_evaluating", 0.85),
    # Refined profile change patterns
    frozenset({"company_change", "hiring_surge"}): ("new_leader_building", 0.90),
    frozenset({"company_change", "keyword_mention"}): ("new_role_exploring", 0.80),
    frozenset({"company_change", "competitor_mention"}): ("new_role_evaluating", 0.85),
    frozenset({"promotion", "hiring_surge"}): ("promoted_and_building", 0.85),
    frozenset({"promotion", "keyword_mention"}): ("promoted_and_exploring", 0.75),
    frozenset({"headline_intent", "keyword_mention"}): ("active_market_participant", 0.80),
    frozenset({"headline_intent", "hiring_surge"}): ("hiring_and_signaling", 0.85),
    # Non-profile patterns
    frozenset({"competitor_mention", "keyword_mention"}): ("active_evaluation", 0.85),
    frozenset({"prospect_post", "commenter_match"}): ("engaged_thought_leader", 0.70),
    frozenset({"funding_event", "hiring_surge"}): ("growth_mode", 0.80),
    frozenset({"funding_event", "keyword_mention"}): ("funded_and_searching", 0.75),
    frozenset({"competitor_mention", "prospect_post"}): ("vocal_evaluator", 0.80),
    # Granular news compound patterns (Phase 1)
    frozenset({"news_funding", "hiring_surge"}): ("funded_and_hiring", 0.85),
    frozenset({"news_funding", "keyword_mention"}): ("funded_and_searching", 0.80),
    frozenset({"news_exec_hire", "hiring_surge"}): ("new_leader_building_v2", 0.90),
    frozenset({"news_exec_hire", "keyword_mention"}): ("new_exec_exploring", 0.85),
    frozenset({"news_acquisition", "keyword_mention"}): ("post_acquisition_eval", 0.80),
    # Company page engagement patterns (Phase 2)
    frozenset({"company_post_comment", "profile_view"}): ("deeply_engaged", 0.80),
    frozenset({"company_follower", "keyword_mention"}): ("follower_and_searching", 0.80),
    frozenset({"company_post_comment", "keyword_mention"}): ("engaged_and_searching", 0.85),
    # Website visitor tracking patterns (Phase 3)
    frozenset({"website_high_intent", "keyword_mention"}): ("website_and_searching", 0.85),
    frozenset({"website_high_intent", "company_post_comment"}): ("website_and_engaged", 0.90),
    frozenset({"website_visit", "profile_view"}): ("researching_you", 0.75),
    frozenset({"website_high_intent", "profile_view"}): ("high_intent_researcher", 0.85),
    # Post content intent compound patterns (Phase 5)
    frozenset({"post_pain_point", "keyword_mention"}): ("pain_and_searching", 0.85),
    frozenset({"post_tech_evaluation", "competitor_mention"}): ("active_evaluator_v2", 0.90),
    frozenset({"post_budget_signal", "competitor_mention"}): ("budget_and_evaluating", 0.90),
    frozenset({"post_seeking_recs", "keyword_mention"}): ("actively_seeking_solution", 0.90),
    frozenset({"post_negative_experience", "reshares_our_content"}): ("competitor_dissatisfied", 0.95),
    frozenset({"post_negative_experience", "keyword_mention"}): ("frustrated_and_searching", 0.85),
    frozenset({"post_tech_evaluation", "hiring_surge"}): ("scaling_and_evaluating", 0.85),
    # Repost/share compound patterns (Phase 5)
    frozenset({"reshares_our_content", "company_post_comment"}): ("warm_and_aware", 0.85),
    frozenset({"reshares_competitor", "post_tech_evaluation"}): ("deep_competitor_eval", 0.90),
    frozenset({"reshares_competitor", "keyword_mention"}): ("competitor_follower_searching", 0.80),
    # Company post compound patterns (Phase 5)
    frozenset({"company_hiring_post", "news_funding"}): ("funded_and_growing", 0.85),
    frozenset({"company_hiring_post", "company_milestone_post"}): ("company_in_growth_mode", 0.80),
    frozenset({"company_tech_stack_post", "keyword_mention"}): ("tech_evaluator_company", 0.80),
    # Comment mining compound patterns (Phase 5)
    frozenset({"prospect_comments_on_competitor", "post_tech_evaluation"}): ("deep_competitor_engagement", 0.90),
    frozenset({"industry_thread_participant", "post_seeking_recs"}): ("industry_active_seeker", 0.90),
    frozenset({"competitor_post_commenter", "profile_view"}): ("competitor_user_researching_us", 0.85),
    # Reaction type compound patterns (Phase 5)
    frozenset({"reaction_insightful_competitor", "competitor_mention"}): ("studying_competitor", 0.85),
    frozenset({"reaction_pattern", "keyword_mention"}): ("active_linkedin_user_interested", 0.75),
}

# Headline intent keywords — categorized by sales relevance
HEADLINE_INTENT_KEYWORDS: dict[str, list[str]] = {
    "hiring": [
        "hiring", "we're hiring", "building a team", "growing the team",
        "looking for talent", "join my team", "open roles",
    ],
    "open_to_opportunities": [
        "open to work", "open to opportunities", "seeking new",
        "looking for my next", "exploring opportunities", "#opentowork",
        "available for", "in transition",
    ],
    "building": [
        "building the next", "building a", "launching", "just launched",
        "starting a new", "co-founding", "bootstrapping",
    ],
    "evaluating": [
        "rethinking our", "transforming", "modernizing",
        "upgrading our", "overhauling", "revamping",
    ],
}

# Post content intent keywords — used by post_intent_classifier (Phase 5)
POST_INTENT_KEYWORDS: dict[str, list[str]] = {
    "seeking_recommendations": [
        "anyone recommend", "any recommendations", "can anyone suggest",
        "what tool do you use", "which platform", "looking for a tool",
        "looking for a solution", "who's the best", "suggestions for",
        "what are people using", "what do you use for",
    ],
    "budget_signal": [
        "budget for", "investing in", "allocated budget",
        "roi on", "return on investment", "cost of",
        "worth the investment", "pricing", "total cost",
        "this quarter we're", "q1 priority", "q2 priority",
        "q3 priority", "q4 priority",
    ],
    "tech_evaluation": [
        "evaluating", "comparing", "testing out",
        "piloting", "proof of concept", "poc",
        "shortlist", "vendor selection", "rfp",
        "request for proposal", "tool selection",
        "switching from", "migrating to", "moving from",
    ],
    "negative_experience": [
        "frustrated with", "tired of", "disappointed by",
        "worst experience", "terrible support", "overpriced",
        "not worth", "broken", "doesn't work",
        "switched away from", "cancelled our", "dropped",
    ],
    "pain_point": [
        "biggest challenge", "struggling with", "bottleneck",
        "time-consuming", "manual process", "not scalable",
        "can't keep up", "falling behind", "burning out",
        "too much time on", "wasting hours", "need to automate",
    ],
}

# Company post topic keywords — used by company post topic classifier (Phase 5)
COMPANY_POST_TOPIC_KEYWORDS: dict[str, list[str]] = {
    "hiring": [
        "we're hiring", "join our team", "open role", "open position",
        "growing our team", "looking for", "apply now", "#hiring",
        "new role", "come work with us",
    ],
    "milestone": [
        "customers", "revenue", "arr", "mrr", "milestone",
        "reached", "crossed", "surpassed", "grew by",
        "year over year", "yoy", "growth rate",
    ],
    "tech_stack": [
        "we use", "our stack", "tech stack", "built with",
        "powered by", "integrated with", "tools we use",
        "how we use", "our workflow",
    ],
    "product_launch": [
        "just launched", "introducing", "announcing",
        "new feature", "now available", "we're excited to",
        "product update", "release notes",
    ],
}

# LinkedIn reaction types and their sentiment (Phase 5)
REACTION_SENTIMENT: dict[str, str] = {
    "LIKE": "neutral",
    "CELEBRATE": "positive",
    "SUPPORT": "empathy",
    "LOVE": "strong_positive",
    "INSIGHTFUL": "intellectual",
    "FUNNY": "casual",
}

# Phase 5 scheduler jobs
JOB_CLASSIFY_POST_INTENT = "classify_post_intent"
JOB_MINE_COMMENTS = "mine_comments"

# Scheduler job for compound intent detection
JOB_DETECT_COMPOUND_INTENT = "detect_compound_intent"
SIGNAL_COMPOUND_DETECT_SECONDS = 1800   # Check for compound intents every 30 min

# Signal decay engine (Week 11)
JOB_SIGNAL_DECAY_CYCLE = "signal_decay_cycle"
SIGNAL_DECAY_CYCLE_SECONDS = 21600      # Run decay cycle every 6 hours

# Signal linker — retroactive matching (signals ↔ campaigns)
JOB_REMATCH_SIGNALS = "rematch_signals"
SIGNAL_REMATCH_SECONDS = 3600           # Re-match homeless signals every 1 hour
JOB_BACKFILL_ORPHAN_SIGNALS = "backfill_orphan_signals"
SIGNAL_BACKFILL_SECONDS = 7200          # Backfill orphan signals every 2 hours

# ──────────────────────────────────────────────
# Strategy Engine (Autonomous Campaign Optimization)
# ──────────────────────────────────────────────

JOB_STRATEGY_CYCLE = "strategy_cycle"
SCHEDULER_STRATEGY_SECONDS = 14400      # Run strategy cycle every 4 hours

# Revenue estimation heuristics (annual contract value in USD)
HEADCOUNT_BASE_ACV: dict[tuple[int, int | None], int] = {
    (1, 10): 2_000,
    (11, 50): 5_000,
    (51, 200): 15_000,
    (201, 500): 30_000,
    (501, 1000): 50_000,
    (1001, 5000): 80_000,
    (5001, 10000): 120_000,
    (10001, None): 200_000,
}

INDUSTRY_MULTIPLIERS: dict[str, float] = {
    "financial services": 1.5,
    "fintech": 1.4,
    "banking": 1.5,
    "insurance": 1.4,
    "healthcare": 1.3,
    "pharmaceutical": 1.3,
    "technology": 1.2,
    "saas": 1.3,
    "software": 1.2,
    "cybersecurity": 1.3,
    "telecommunications": 1.1,
    "energy": 1.2,
    "oil & gas": 1.3,
    "consulting": 1.1,
    "legal": 1.2,
    "real estate": 1.0,
    "e-commerce": 1.0,
    "retail": 0.9,
    "manufacturing": 0.9,
    "logistics": 0.9,
    "media": 0.8,
    "advertising": 0.8,
    "education": 0.7,
    "non-profit": 0.5,
    "government": 0.6,
}

SENIORITY_MULTIPLIERS: dict[str, float] = {
    "owner": 1.5,
    "cxo": 1.4,
    "vp": 1.2,
    "director": 1.0,
    "manager": 0.7,
    "senior": 0.5,
    "entry": 0.3,
}

# Canonical seniority vocabulary. This literal is copied verbatim into the
# backend at heylead-api `app/services/seniority.py`; the two must stay
# byte-identical, and `tests/fixtures/seniority_cases.json` (also copied into
# both repos) pins the behaviour on both sides.
#
# The individual-contributor role nouns in "entry" ("engineer", "developer",
# "representative", "sdr", "bdr") are what let a decision-maker ICP reject a
# Software Engineer or an SDR. Without them `states_seniority` reads those
# titles as stating no level at all and they pass every seniority gate. They
# are boundary-matched, so "VP Engineering" and "Engineering Manager" still
# resolve at their own level: the ladder is walked highest-first.
SENIORITY_KEYWORDS: dict[str, list[str]] = {
    "owner": ["owner", "founder", "co-founder", "cofounder", "proprietor"],
    "cxo": ["ceo", "cto", "cfo", "coo", "cmo", "cpo", "ciso", "cro",
            "chief", "president", "managing partner"],
    "vp": ["vp", "vice president", "svp", "evp", "avp"],
    "director": ["director", "head of"],
    "manager": ["manager", "lead", "team lead", "supervisor"],
    "senior": ["senior", "sr.", "sr ", "principal", "staff"],
    "entry": ["associate", "analyst", "coordinator", "intern", "junior",
              "jr.", "jr ", "assistant", "specialist",
              "engineer", "developer", "representative", "sdr", "bdr"],
}

# Strategy engine thresholds
STRATEGY_MIN_SAMPLE_SIZE = 30           # Min prospects before pattern detection
STRATEGY_SKIP_ACCEPTANCE_THRESHOLD = 0.05  # Skip segment if acceptance < 5%
STRATEGY_SPAWN_MIN_CONFIDENCE = 0.5     # Min pattern confidence to spawn campaign
STRATEGY_SPAWN_MIN_REVENUE = 10_000     # Min estimated revenue impact to spawn
STRATEGY_CONFIDENCE_BOOST = 0.1         # Confidence increase on validation
STRATEGY_CONFIDENCE_PENALTY = 0.2       # Confidence decrease on rollback
STRATEGY_MAX_SPAWNED_CAMPAIGNS = 3      # Max auto-spawned campaigns per cycle

# ──────────────────────────────────────────────
# Communication Strategist (Daily AI Planning)
# ──────────────────────────────────────────────

JOB_DAILY_STRATEGY = "daily_strategy"
JOB_EXECUTE_STRATEGY_PLANS = "execute_strategy_plans"
SCHEDULER_DAILY_STRATEGY_SECONDS = 86400       # Run strategy planner once per day
SCHEDULER_EXECUTE_PLANS_SECONDS = 900          # Execute daily plans every 15 min
STRATEGIST_PLANNING_HOUR = 6                   # Local hour the daily planner runs at

STRATEGIST_BATCH_SIZE = 50                     # Prospects per LLM batch
STRATEGIST_MAX_ACTIONS_PER_DAY = 3             # Cap actions per prospect per day
STRATEGIST_MAX_PROSPECTS = 250                 # Max prospects to plan for per cycle
STRATEGIST_FEEDBACK_LOOKBACK_DAYS = 7          # Days of feedback to include in prompt
STRATEGIST_ATTRIBUTION_DAYS = 3                # A reply/accept credits actions taken within this many days before it
STRATEGIST_FEEDBACK_TOP_N = 10                 # Best/worst plans to feed back to LLM

# Timing preferences (mapped to hour ranges in prospect's local timezone)
STRATEGIST_TIMING_MORNING = "morning"          # 8AM-12PM
STRATEGIST_TIMING_AFTERNOON = "afternoon"      # 12PM-4PM
STRATEGIST_TIMING_EVENING = "evening"          # 4PM-7PM
STRATEGIST_TIMING_ANYTIME = "anytime"          # Any business hour

# Available action types the LLM can plan
# ──────────────────────────────────────────────
# Reply exception agent (short loop + budget)
# ──────────────────────────────────────────────

REPLY_AGENT_MAX_STEPS = 4
REPLY_AGENT_MAX_TOKENS = 800
REPLY_AGENT_TIMEOUT_SECONDS = 45
REPLY_AGENT_RESULT_CHARS = 2000
HOLD_FOR_OPERATOR = "hold_for_operator"

ICP_RESEARCH_MAX_STEPS = 4
ICP_RESEARCH_MAX_TOKENS = 800
ICP_RESEARCH_TIMEOUT_SECONDS = 45
ICP_RESEARCH_RESULT_CHARS = 2000
ICP_RESEARCH_PREVIEW_LIMIT = 8
ICP_RESEARCH_VALID_DECISIONS = frozenset({"keep", "revise", "hold", "none"})

STRATEGIST_REPLAN_MAX_STEPS = 4
STRATEGIST_REPLAN_MAX_TOKENS = 800
STRATEGIST_REPLAN_TIMEOUT_SECONDS = 45
STRATEGIST_REPLAN_RESULT_CHARS = 2000
STRATEGIST_REPLAN_MAX_PER_TICK = 8
STRATEGIST_REPLAN_VALID_DECISIONS = frozenset({"keep", "revise", "hold", "none"})

HOT_LEAD_CLOSER_MAX_STEPS = 4
HOT_LEAD_CLOSER_MAX_TOKENS = 800
HOT_LEAD_CLOSER_TIMEOUT_SECONDS = 45
HOT_LEAD_CLOSER_RESULT_CHARS = 2000
HOT_LEAD_CLOSER_MAX_BOOKS_PER_DAY = 5
HOT_LEAD_CLOSER_VALID_DECISIONS = frozenset({"book", "hold", "none"})

COORDINATOR_MAX_STEPS = 2
COORDINATOR_MAX_TOKENS = 600
COORDINATOR_TIMEOUT_SECONDS = 20
COORDINATOR_RESULT_CHARS = 1500
COORDINATOR_VALID_DECISIONS = frozenset({"none", "note", "hold"})

PRODUCT_AGENT_MAX_STEPS = 4
PRODUCT_AGENT_MAX_TOKENS = 800
PRODUCT_AGENT_TIMEOUT_SECONDS = 45
PRODUCT_AGENT_RESULT_CHARS = 2000
PRODUCT_AGENT_MAX_FILES = 8
PRODUCT_AGENT_MAX_DIFF_LINES = 400
PRODUCT_AGENT_VALID_DECISIONS = frozenset({"draft", "apply", "pr", "hold", "none"})

STRATEGIST_AVAILABLE_ACTIONS = frozenset({
    "profile_view",
    "follow",
    "endorse",
    "engage_comment",
    "engage_react",
    "invite",
    "inmail",
    "send_dm",
    "followup",
    "voice_memo",
    "email",
    "skip_today",
})

# Feedback scoring weights
STRATEGIST_SCORE_REPLIED = 3.0
STRATEGIST_SCORE_ACCEPTED = 2.0
STRATEGIST_SCORE_PROFILE_VIEWED_BACK = 1.0
STRATEGIST_SCORE_NO_RESPONSE = 0.0
STRATEGIST_SCORE_DECLINED = -1.0

# ──────────────────────────────────────────────
# Voice Memos / Hume AI TTS
# ──────────────────────────────────────────────

HUME_TTS_API_URL = "https://api.hume.ai/v0/tts"
HUME_DEFAULT_OUTPUT_FORMAT = "mp3"
HUME_DEFAULT_SPEED = 1.0
VOICE_MEMO_MAX_TEXT_CHARS = 500           # Keep voice messages concise
VOICE_MEMO_MAX_DURATION_SECONDS = 60     # LinkedIn voice message limit
VOICE_MEMO_DIR = "voice_memos"          # Subdir under ~/.heylead/

# Voice mode options for campaigns
VOICE_MODE_TEXT_ONLY = "text_only"       # Default: all messages as text
VOICE_MODE_VOICE_ONLY = "voice_only"     # All follow-ups/replies as voice (text fallback)
VOICE_MODE_MIXED = "mixed"               # Alternate between text and voice
VOICE_MODE_AB_TEST = "ab_test"           # A/B test voice vs text
VALID_VOICE_MODES = frozenset({
    VOICE_MODE_TEXT_ONLY, VOICE_MODE_VOICE_ONLY,
    VOICE_MODE_MIXED, VOICE_MODE_AB_TEST,
})

# Voice Memo Enhancement (v0.10)
VALID_NOISE_TYPES = frozenset({"office", "cafe", "street", "quiet", "none", "auto"})
VALID_NOISE_VOLUMES = frozenset({"subtle", "moderate", "noticeable"})
DEFAULT_NOISE_TYPE = "auto"
DEFAULT_NOISE_VOLUME = "subtle"
DEFAULT_VOICE_HUMANIZE = True

# ──────────────────────────────────────────────
# Partner Follow-Up Tracking
# ──────────────────────────────────────────────

JOB_PARTNER_REMINDER = "partner_reminder"
SCHEDULER_PARTNER_REMINDER_SECONDS = 3600   # Check every hour
PARTNER_DEFAULT_SCHEDULE_DAYS = [1, 3, 7, 14, 21]  # Escalating cadence between reminders
PARTNER_MAX_AUTO_FOLLOWUPS = 5              # Stop after 5 automated reminders

# Daily digest
JOB_DAILY_DIGEST = "daily_digest"
SCHEDULER_DAILY_DIGEST_SECONDS = 86400      # Once per day

# Account-wide action-health snapshot (skip vs send across every campaign)
SCHEDULER_ACTION_HEALTH_SECONDS = 3600      # Once per hour

# Action verification — async post-send double-check
JOB_VERIFY_ACTIONS = "verify_actions"
VERIFICATION_CHECK_SECONDS = 900            # Every 15 min
VERIFICATION_MAX_CHECKS = 10               # Max outreaches to verify per run
VERIFICATION_WINDOW_HOURS = 24             # Verify actions from last 24h (catch-up stale ones)

# Connection sync — periodically refresh local 1st-degree connections cache
JOB_SYNC_CONNECTIONS = "sync_connections"
SCHEDULER_SYNC_CONNECTIONS_SECONDS = 14400  # Every 4 hours

# Campaign prospect enrichment — proactively discover new prospects for active campaigns
JOB_CAMPAIGN_REFILL = "campaign_refill"
SCHEDULER_CAMPAIGN_REFILL_SECONDS = 3600     # Enrich every 1 hour
CAMPAIGN_REFILL_BATCH_SIZE = 50              # Max new prospects per enrichment cycle
CAMPAIGN_REFILL_MAX_PAGES = 5                # Max LinkedIn search pages per cycle
CAMPAIGN_REFILL_COOLDOWN_HOURS = 24          # Min hours between enrichments for same campaign
# Sendable-queue depth a campaign is kept topped up to. The cooldown above
# only applies once this is met: refill used to trigger on an EMPTY queue, so
# the deepest stock it could hold was zero and a campaign draining 10
# invitations a day waited a full day for one search page. Override per
# campaign with config_json["target_queue_size"].
CAMPAIGN_TARGET_QUEUE_SIZE = 50

# Capability flag: this checkout stands the local engine down on hosted cloud.
# A PyPI install older than the cutover will not have this name in constants.
CLOUD_OWNS_ALL_JOBS = True

# Profile backfill — enrich contacts with missing profile_json
JOB_BACKFILL_PROFILES = "backfill_profiles"
SCHEDULER_BACKFILL_PROFILES_SECONDS = 900    # Every 15 min

# Engagement verification
ENGAGEMENT_VERIFY_MAX_CHECKS = 20          # More than outreach (cheaper to verify)
ENGAGEMENT_VERIFY_DELAY_SECONDS = 3        # Brief pause before immediate verify
# Disabled since 2026-03-04: Unipile's POST /profile/{id}/skill/endorse returns
# 404 always.  Re-enable once Unipile confirms the endpoint is operational.
ENDORSEMENT_ENABLED = False

# Verification status values
VERIFIED = "verified"
UNVERIFIED = "unverified"
TRUST_API = "trust_api"

# Post-send DM verification + auto-delete safety net
POST_SEND_VERIFY_DELAY_SECONDS = 8       # Wait for LinkedIn to propagate the DM
POST_SEND_VERIFY_MATCH_CHARS = 100       # Characters to match for text verification
POST_SEND_VERIFY_MIN_CHARS = 20          # Below this = truncated/garbled = auto-delete
POST_SEND_VERIFY_ENABLED = True          # Master kill switch

# Engagement anomaly detection
ANOMALY_DUPLICATE_POST_THRESHOLD = 1        # Alert if same post engaged >1 time by same account
ANOMALY_SAME_COMPANY_THRESHOLD = 3          # Alert if >3 comments on same company's posts in 24h
ANOMALY_BURST_THRESHOLD = 18                # Alert if >18 engagements in 30 min window (8 comments + 10 reactions)
ANOMALY_BURST_WINDOW_MINUTES = 30           # Window for burst detection
ANOMALY_FAILED_SPIKE_THRESHOLD = 5          # Alert if >5 failed engagements in 24h
ANOMALY_SCAN_SECONDS = 3600                 # Run anomaly scan every 1 hour
ANOMALY_ALERT_COOLDOWN_SECONDS = 14400      # Suppress duplicate alert emails for 4 hours per anomaly type
STALL_ALERT_COOLDOWN_SECONDS = 14400        # Max 1 stall/stuck-job alert email per 4 hours

# Metric trend anomaly detection
ANOMALY_METRIC_DEGRADATION_PCT = 0.20       # Flag if >20% degradation from baseline mean
ANOMALY_METRIC_STD_DEV_THRESHOLD = 2.0      # Flag if >2 standard deviations below baseline
ANOMALY_METRIC_MIN_DATAPOINTS = 5           # Minimum baseline datapoints to detect anomalies

# ──────────────────────────────────────────────
# Prospect Source Tracking
# ──────────────────────────────────────────────

SOURCE_LINKEDIN_SEARCH = "linkedin_search"
SOURCE_CSV_IMPORT = "csv_import"
SOURCE_INBOUND_INVITATION = "inbound_invitation"
SOURCE_INBOUND_DM = "inbound_dm"
SOURCE_INBOUND_COMMENT = "inbound_comment"
SOURCE_SIGNAL_DISCOVERY = "signal_discovery"
SOURCE_STRATEGY_SPAWN = "strategy_spawn"
SOURCE_LINKEDIN_LOOKUP = "linkedin_lookup"
SOURCE_MANUAL = "manual"

SOURCE_LABELS: dict[str, str] = {
    SOURCE_LINKEDIN_SEARCH: "LinkedIn Search",
    SOURCE_CSV_IMPORT: "CSV Import",
    SOURCE_INBOUND_INVITATION: "Inbound Invitation",
    SOURCE_INBOUND_DM: "Inbound DM",
    SOURCE_INBOUND_COMMENT: "Post Comment",
    SOURCE_SIGNAL_DISCOVERY: "Signal Discovery",
    SOURCE_STRATEGY_SPAWN: "Auto-Spawned",
    SOURCE_LINKEDIN_LOOKUP: "LinkedIn Lookup",
    SOURCE_MANUAL: "Manual",
    "search": "LinkedIn Search",  # backwards compat for old data
}
