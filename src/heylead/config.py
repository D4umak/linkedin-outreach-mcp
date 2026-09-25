"""HeyLead config management — ~/.heylead/config.json."""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

from . import constants

logger = logging.getLogger(__name__)


def _heylead_home() -> Path:
    """Return the HeyLead home directory (~/.heylead)."""
    return Path.home() / constants.HEYLEAD_DIR_NAME


def ensure_dirs() -> Path:
    """Create all required directories and return the home path."""
    home = _heylead_home()
    (home / constants.DB_DIR).mkdir(parents=True, exist_ok=True)
    (home / constants.AUTH_DIR).mkdir(parents=True, exist_ok=True)
    (home / constants.LOG_DIR).mkdir(parents=True, exist_ok=True)
    (home / constants.VOICE_MEMO_DIR).mkdir(parents=True, exist_ok=True)
    (home / constants.BACKUP_DIR).mkdir(parents=True, exist_ok=True)
    return home


def config_path() -> Path:
    return _heylead_home() / constants.CONFIG_FILE


def db_path() -> Path:
    return _heylead_home() / constants.DB_DIR / constants.DB_FILE


def cookie_path() -> Path:
    """Legacy — kept for cleanup of old installs."""
    return _heylead_home() / constants.AUTH_DIR / constants.COOKIE_FILE


def log_path() -> Path:
    return _heylead_home() / constants.LOG_DIR / constants.LOG_FILE


def json_log_path() -> Path:
    return _heylead_home() / constants.LOG_DIR / constants.LOG_FILE_JSON


# ──────────────────────────────────────────────
# Config read / write
# ──────────────────────────────────────────────

_DEFAULT_CONFIG: dict[str, Any] = {
    "llm_priority": constants.DEFAULT_LLM_PRIORITY,
    "api_keys": {
        "gemini": "",
        "claude": "",
        "openai": "",
        "serper": "",
        "hume": "",
    },
    "timezone": "",
    "working_hours": {
        "start": constants.DEFAULT_START_HOUR,
        "end": constants.DEFAULT_END_HOUR,
        "days": constants.DEFAULT_ACTIVE_DAYS,
    },
    "unipile_api_url": "",
    "unipile_api_key": "",
    "tier": constants.TIER_FREE,
    # Tool-call telemetry (api #1204): "on" unless the user ran
    # `heylead config telemetry off`. See telemetry_enabled().
    "telemetry": "on",
    "scheduler_enabled": False,
    "scheduler_always_on": False,
    # Hosted default is cloud. Unset reads as cloud in backend mode so every
    # existing install moves over without a migration write. Direct mode
    # ignores this and always sends from this machine.
    "sending_host": "",
    # Empty means "use the current tiered defaults from constants". Writing a
    # concrete model id here is what left installs pinned to gemini-2.0-flash
    # long after it was retired — only set these to override deliberately.
    "gemini_model": "",
    "claude_model": "",
    "openai_model": "",
    # Hosted status replies attach a PNG snapshot of the matching dashboard
    # page; the link is always shown, this only controls the image.
    "dashboard_snapshots": True,
}


def load_config() -> dict[str, Any]:
    """Load config from disk, creating default if missing."""
    ensure_dirs()
    path = config_path()
    if path.exists():
        try:
            with open(path, "r") as f:
                data = json.load(f)
            # Merge defaults for any missing keys
            merged = {**_DEFAULT_CONFIG, **data}
            # Deep-merge nested dicts
            for key in ("api_keys", "working_hours"):
                if key in _DEFAULT_CONFIG:
                    merged[key] = {**_DEFAULT_CONFIG[key], **data.get(key, {})}
            # Migrate: the pre-24/7 defaults were weekdays 09:00–17:00.
            # Only that exact legacy pair is rewritten. A user-set window
            # (8–20, 9–18, …) must survive load_config().
            _LEGACY_START, _LEGACY_END = 9, 17
            wh = merged.get("working_hours", {})
            needs_save = False
            if wh.get("days") == [0, 1, 2, 3, 4]:
                wh["days"] = constants.DEFAULT_ACTIVE_DAYS
                needs_save = True
            if wh.get("start") == _LEGACY_START and wh.get("end") == _LEGACY_END:
                wh["start"] = constants.DEFAULT_START_HOUR
                wh["end"] = constants.DEFAULT_END_HOUR
                needs_save = True
            if needs_save:
                merged["working_hours"] = wh
                save_config(merged)
                logger.info("Config migrated: working_hours upgraded to 24/7 autonomous")
            return merged
        except (json.JSONDecodeError, IOError) as e:
            # The unreadable bytes are the only copy of the backend JWT and the
            # stored keys; the next save_config() would overwrite them with the
            # defaults returned here, so move them aside first.
            try:
                path.replace(path.with_suffix(".json.corrupt"))
            except OSError as move_err:
                logger.error(f"Could not preserve corrupt config: {move_err}")
            logger.error(
                f"Config file corrupt, using defaults (previous file kept as "
                f"config.json.corrupt): {e}"
            )
            return _default_config()
    else:
        save_config(_DEFAULT_CONFIG)
        return _default_config()


def _default_config() -> dict[str, Any]:
    """A copy no caller can use to mutate the module-level defaults."""
    return copy.deepcopy(_DEFAULT_CONFIG)


def save_config(cfg: dict[str, Any]) -> None:
    """Persist config to disk.

    Written through a temp file in the same directory and renamed into place:
    a failure part-way through serialisation must leave the previous config
    readable rather than truncated.
    """
    ensure_dirs()
    path = config_path()
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".config-", suffix=".json")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(cfg, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def get_api_key(provider: str) -> str:
    """Return API key for a given LLM provider, or empty string."""
    cfg = load_config()
    return cfg.get("api_keys", {}).get(provider, "")


def set_api_key(provider: str, key: str) -> None:
    """Store an API key."""
    cfg = load_config()
    cfg.setdefault("api_keys", {})[provider] = key
    save_config(cfg)


def get_tier() -> str:
    """Return current tier ('free' or 'pro')."""
    cfg = load_config()
    return cfg.get("tier", constants.TIER_FREE)


def apply_free_monthly_caps() -> bool:
    """True when local product free-tier monthly quotas apply.

    Hosted accounts are billed by the host — leftover ``tier: free`` must
    not cap them. Self-hosted Pro is uncapped. Self-hosted free stays at
    the production monthly row (invites, messages, engagements).
    """
    return (not is_backend_mode()) and get_tier() != constants.TIER_PRO


def _system_timezone() -> str:
    """The machine's IANA zone name, or "" when it cannot be determined.

    macOS and most Linux installs symlink /etc/localtime into a zoneinfo
    tree; Debian-style installs also write /etc/timezone. TZ in the
    environment wins when it names a real zone.
    """
    import os

    candidates: list[str] = []
    env_tz = os.environ.get("TZ", "").strip()
    if env_tz and not env_tz.startswith(":"):
        candidates.append(env_tz)
    try:
        link = os.readlink("/etc/localtime")
        if "zoneinfo/" in link:
            candidates.append(link.split("zoneinfo/", 1)[1])
    except OSError:
        pass
    try:
        with open("/etc/timezone") as fh:
            candidates.append(fh.read().strip())
    except OSError:
        pass
    for name in candidates:
        if not name or name in ("UTC", "Etc/UTC", "localtime"):
            continue
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(name)
            return name
        except Exception:
            continue
    return ""


def get_timezone() -> str:
    """The timezone every local-time decision should use.

    Configured value first, then the machine's own zone, then UTC. The
    empty default used to reach the scheduler as "UTC" without anyone
    choosing that, which put a "morning" window an hour or more off.
    """
    cfg = load_config()
    configured = (cfg.get("timezone") or "").strip()
    if configured:
        return configured
    return _system_timezone() or "UTC"


def set_timezone(tz: str) -> None:
    cfg = load_config()
    cfg["timezone"] = tz
    save_config(cfg)


# ──────────────────────────────────────────────
# Unipile config helpers
# ──────────────────────────────────────────────

def get_unipile_config() -> tuple[str, str]:
    """Return (api_url, api_key) from config."""
    cfg = load_config()
    return cfg.get("unipile_api_url", ""), cfg.get("unipile_api_key", "")


def set_unipile_config(api_url: str, api_key: str) -> None:
    """Store Unipile credentials."""
    cfg = load_config()
    cfg["unipile_api_url"] = api_url.strip()
    cfg["unipile_api_key"] = api_key.strip()
    save_config(cfg)


# ──────────────────────────────────────────────
# Backend proxy config helpers
# ──────────────────────────────────────────────

def get_backend_config() -> tuple[str, str]:
    """Return (backend_url, jwt_token) from config.

    Defaults to the production backend URL if none is configured.
    Users only need to provide a JWT — the URL is automatic.
    """
    cfg = load_config()
    url = cfg.get("backend_url", "") or constants.DEFAULT_BACKEND_URL
    jwt_token = cfg.get("backend_jwt", "")
    return url, jwt_token


def set_backend_config(backend_url: str, jwt_token: str) -> None:
    """Store backend proxy credentials."""
    cfg = load_config()
    cfg["backend_url"] = backend_url.strip()
    cfg["backend_jwt"] = jwt_token.strip()
    save_config(cfg)


def get_active_org_id() -> str:
    """Return the selected hosted organization id, or empty."""
    return str(load_config().get("active_org_id") or "").strip()


def set_active_org_id(org_id: str) -> None:
    cfg = load_config()
    cfg["active_org_id"] = org_id.strip()
    save_config(cfg)


_TELEMETRY_OFF = frozenset({"off", "false", "0", "no"})


def telemetry_enabled() -> bool:
    """Whether tool-call telemetry (names, outcome, latency) may be sent.

    Off when the config says "off" (``heylead config telemetry off``) or
    ``DO_NOT_TRACK`` is set. A boolean ``false`` is the pre-#1204 default
    that every saved config carried and nothing ever read or set, so it is
    not a choice anybody made and reads as the default, on.
    """
    if str(os.environ.get("DO_NOT_TRACK") or "").strip() not in ("", "0"):
        return False
    value = load_config().get("telemetry", "on")
    if isinstance(value, bool):
        return True
    return str(value).strip().lower() not in _TELEMETRY_OFF


def set_telemetry(enabled: bool) -> None:
    """Record the user's telemetry choice as "on" or "off"."""
    cfg = load_config()
    cfg["telemetry"] = "on" if enabled else "off"
    save_config(cfg)


def is_backend_mode() -> bool:
    """Check if the MCP server should use the backend proxy."""
    url, jwt = get_backend_config()
    return bool(url and jwt)


def dashboard_snapshots_enabled() -> bool:
    """Whether hosted status replies may attach a dashboard snapshot card."""
    from .flags import flag_enabled

    return flag_enabled(load_config(), "dashboard_snapshots", default=True)


def has_local_llm_key() -> bool:
    """Check if the user has any local LLM API key configured."""
    cfg = load_config()
    return any(v for v in cfg.get("api_keys", {}).values() if v)


# ──────────────────────────────────────────────
# ICP / Ingestion config helpers
# ──────────────────────────────────────────────

def embeddings_path() -> Path:
    """Return the embeddings cache directory (~/.heylead/embeddings/)."""
    return _heylead_home() / constants.EMBEDDINGS_DIR


def kb_path() -> Path:
    """Return the knowledge base directory (~/.heylead/knowledge-base/)."""
    return _heylead_home() / constants.KB_DIR


def get_firecrawl_api_key() -> str:
    """Return the Firecrawl API key from config, or empty string."""
    cfg = load_config()
    return cfg.get("firecrawl_api_key", "")


def get_serper_api_key() -> str:
    """Return the SERPER API key from config, or empty string."""
    cfg = load_config()
    return cfg.get("api_keys", {}).get("serper", "")


# ──────────────────────────────────────────────
# Scheduler config helpers
# ──────────────────────────────────────────────

SCHEDULER_MODES = ("off", "observe", "full")


def get_scheduler_mode() -> str:
    """How much of the scheduler is allowed to run.

    off      inbound message handling and local reporting only
    observe  also collects and classifies signals, and checks replies. Never
             sends, invites, engages, or enrols anyone.
    full     everything

    A single on/off switch forced an all-or-nothing choice: stopping unreviewed
    sends also blinded the product to its own signals for days. observe is the
    setting that was missing.

    An unrecognised value resolves to "off" rather than being trusted.
    """
    cfg = load_config()
    mode = str(cfg.get("scheduler_mode") or "").strip().lower()
    if mode in SCHEDULER_MODES:
        return mode
    if mode:
        logger.warning("Unrecognised scheduler_mode %r — treating as off", mode)
        return "off"
    # Back-compat for configs written before modes existed.
    return "full" if cfg.get("scheduler_enabled", False) else "off"


def is_scheduler_enabled() -> bool:
    """Whether the scheduler may take outward action (i.e. mode is 'full').

    Deliberately stays False in observe mode, so every existing outbound gate
    that reads this keeps holding without needing to know modes exist.
    """
    return get_scheduler_mode() == "full"


def set_scheduler_mode(
    mode: str,
    *,
    caller: str = "unknown",
    reason: str = "",
) -> str:
    """Set how much of the scheduler may run, with attribution.

    Writes both keys every time, so the two can never disagree:

    * ``scheduler_mode`` — the authority for this build.
    * ``scheduler_enabled`` — the legacy boolean. It is what a pre-modes build
      reads, and what ``get_scheduler_mode()`` falls back to when
      ``scheduler_mode`` is absent. It is therefore False for BOTH "off" and
      "observe": observe never sends, so the only safe thing an older reader
      can conclude from it is "do not send".

    Args:
        mode: One of ``SCHEDULER_MODES``.
        caller: Who triggered this change (e.g., "mcp_tool", "campaign_launch",
                "always_on_guard", "config_edit").
        reason: Human-readable reason for the change.

    Returns:
        The mode that was in effect before this call.

    Raises:
        ValueError: If ``mode`` is not one of ``SCHEDULER_MODES``. An
            unrecognised mode is a caller bug; silently resolving it to "off"
            here would hide it.
    """
    import time as _time

    mode = str(mode).strip().lower()
    if mode not in SCHEDULER_MODES:
        raise ValueError(
            f"Unknown scheduler mode {mode!r} — expected one of {SCHEDULER_MODES}"
        )

    was_mode = get_scheduler_mode()
    cfg = load_config()
    cfg["scheduler_mode"] = mode
    cfg["scheduler_enabled"] = mode == "full"
    cfg["_last_scheduler_toggle"] = {
        "from": was_mode == "full",
        "to": mode == "full",
        "from_mode": was_mode,
        "to_mode": mode,
        "caller": caller,
        "reason": reason,
        "timestamp": int(_time.time()),
    }
    save_config(cfg)
    return was_mode


def set_scheduler_enabled(
    enabled: bool,
    *,
    caller: str = "unknown",
    reason: str = "",
) -> None:
    """Enable or disable the autonomous scheduler with attribution.

    Thin wrapper over :func:`set_scheduler_mode`: True means "full", False
    means "off". It writes the mode as well as the boolean deliberately —
    writing only ``scheduler_enabled`` while ``scheduler_mode`` said something
    else left the two disagreeing, so ``set_scheduler_enabled(True)`` could be
    followed by ``is_scheduler_enabled()`` returning False.

    Note that disabling from "observe" therefore lands in "off", which stops
    signal collection too. Call ``set_scheduler_mode`` directly when the
    distinction matters.

    Args:
        enabled: Whether to enable or disable.
        caller: Who triggered this change (e.g., "mcp_tool", "auto_campaign",
                "always_on_guard", "config_edit").
        reason: Human-readable reason for the change.
    """
    set_scheduler_mode(
        "full" if enabled else "off", caller=caller, reason=reason,
    )


SENDING_HOSTS = ("cloud", "local")


def get_sending_host() -> str:
    """Which machine is allowed to send campaign outbound.

    cloud  hosted default — this machine stands down for everything the
           backend sends. Existing installs with no key stored read as cloud.
    local  explicit opt-in — this machine sends; the cloud scheduler is off.

    Direct / self-hosted installs have no backend, so the answer is always
    local regardless of what is stored.
    """
    if not is_backend_mode():
        return "local"
    host = str(load_config().get("sending_host") or "").strip().lower()
    if host in SENDING_HOSTS:
        return host
    return "cloud"


def set_sending_host(
    host: str,
    *,
    caller: str = "unknown",
    reason: str = "",
) -> str:
    """Record where campaign outbound should run.

    Raises:
        ValueError: If ``host`` is not one of ``SENDING_HOSTS``.
    """
    host = str(host).strip().lower()
    if host not in SENDING_HOSTS:
        raise ValueError(
            f"Unknown sending_host {host!r} — expected one of {SENDING_HOSTS}"
        )
    was = get_sending_host()
    cfg = load_config()
    cfg["sending_host"] = host
    save_config(cfg)
    logger.info(
        "sending_host %s -> %s (caller=%s reason=%s)",
        was, host, caller, reason or "-",
    )
    return was


def is_scheduler_always_on() -> bool:
    """Check if scheduler always-on mode is active."""
    return load_config().get("scheduler_always_on", False)


def set_scheduler_always_on(enabled: bool) -> None:
    """Enable or disable scheduler always-on mode."""
    cfg = load_config()
    cfg["scheduler_always_on"] = enabled
    save_config(cfg)


# ──────────────────────────────────────────────
# Hume AI / Voice Memo config helpers
# ──────────────────────────────────────────────

def get_hume_api_key() -> str:
    """Return Hume AI API key from config, or empty string."""
    cfg = load_config()
    return cfg.get("api_keys", {}).get("hume", "")


def set_hume_api_key(key: str) -> None:
    """Store Hume AI API key."""
    cfg = load_config()
    cfg.setdefault("api_keys", {})["hume"] = key
    save_config(cfg)


def is_voice_memo_enabled() -> bool:
    """Check if voice memos are configured (Hume API key or backend mode)."""
    if get_hume_api_key():
        return True
    if is_backend_mode():
        return True  # Backend proxies Hume calls
    return False


def voice_memo_dir() -> Path:
    """Return the voice memos directory (~/.heylead/voice_memos/)."""
    return _heylead_home() / constants.VOICE_MEMO_DIR


def backups_dir() -> Path:
    """Return the backups directory (~/.heylead/backups/)."""
    return _heylead_home() / constants.BACKUP_DIR
