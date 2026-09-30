"""CLI commands that read settings and the hosted API without showing a secret.

Agent sessions checking production used to open the config file for the
backend URL and token and printed live keys into transcripts (api #2061).
These commands answer the same questions and never print a secret:

    heylead config get <setting>      one ordinary setting, as JSON
    heylead config set <setting> <v>  set one ordinary setting
    heylead api get /api/v1/...       authenticated GET, credentials masked
    heylead secrets status            which secrets are stored (names only)
    heylead secrets set <name>        store one secret (prompted, not echoed)
    heylead secrets clean [--yes]     move/remove secrets the install does not need
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Callable

from .. import config, secret_store

# Names that are secrets, in either form an agent might type.
_REFUSED = frozenset(secret_store.SECRET_FIELDS) | frozenset(
    secret_store.TOP_LEVEL_SECRET_FIELDS
) | {"api_keys"}

_CREDENTIAL_PATTERNS = [
    re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"),  # JWT
    re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{12,}"),  # OpenAI / Anthropic
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"),  # Google
    re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[0-9A-Za-z]{10,}"),  # Stripe
    re.compile(r"\bwhsec_[0-9A-Za-z]{10,}"),  # Stripe webhook
    re.compile(r"\bre_[0-9A-Za-z_]{16,}"),  # Resend
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
]
_SECRET_KEY_NAME = re.compile(r"(?i)(secret|token|password|api_?key|jwt|authorization)")
MASK = "***"


def mask_credentials(text: str) -> str:
    """Replace every credential-shaped substring with ***."""
    for pat in _CREDENTIAL_PATTERNS:
        text = pat.sub(MASK, text)
    return text


def _mask_json(value):
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(v, str) and v and _SECRET_KEY_NAME.search(str(k)):
                out[k] = MASK
            else:
                out[k] = _mask_json(v)
        return out
    if isinstance(value, list):
        return [_mask_json(v) for v in value]
    if isinstance(value, str):
        return mask_credentials(value)
    return value


def _is_secret_name(name: str) -> bool:
    n = name.strip()
    return n in _REFUSED or n.startswith("api_keys.") or n.startswith("api_key.")


# ── config get / set ──


def config_get(setting: str) -> int:
    if _is_secret_name(setting):
        print(
            f"heylead config get: {setting} is a secret and is never printed. "
            "Use `heylead secrets status` to see whether it is stored, or "
            "`heylead api get /api/v1/...` for an authenticated read.",
            file=sys.stderr,
        )
        return 2
    cfg = config.load_config()
    if setting == "backend_url":
        value = config.get_backend_config()[0]
    elif setting not in cfg:
        print(f"heylead config get: no setting named {setting}", file=sys.stderr)
        return 1
    else:
        value = cfg[setting]
    print(json.dumps(value))
    return 0


def config_set(setting: str, raw: str) -> int:
    if _is_secret_name(setting):
        print(
            f"heylead config set: {setting} is a secret; use `heylead secrets set {setting}`.",
            file=sys.stderr,
        )
        return 2
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = raw
    if setting == "backend_url":
        problem = config.backend_url_problem(str(value))
        if problem:
            print(f"heylead config set: refused: {problem}", file=sys.stderr)
            return 2
    cfg = config.load_config()
    cfg[setting] = value
    config.save_config(cfg)
    print(f"{setting} set.")
    return 0


# ── api get ──


def api_get(path: str, *, transport=None) -> int:
    import httpx

    from ..services import cloud_sync

    if not path.startswith("/"):
        print("heylead api get: the path must start with /, e.g. /api/v1/campaigns", file=sys.stderr)
        return 2
    url, jwt = config.get_backend_config()
    if not jwt:
        print(
            "heylead api get: no backend token stored; run setup_profile first.",
            file=sys.stderr,
        )
        return 1
    headers = cloud_sync._headers()
    try:
        with httpx.Client(transport=transport, timeout=30.0) as client:
            resp = client.get(url.rstrip("/") + path, headers=headers)
    except httpx.HTTPError as e:
        print(f"heylead api get: request failed: {mask_credentials(str(e))}", file=sys.stderr)
        return 1
    print(f"HTTP {resp.status_code}", file=sys.stderr)
    try:
        body = json.dumps(_mask_json(resp.json()), indent=2)
    except ValueError:
        body = mask_credentials(resp.text)
    for secret in (jwt,):
        if secret:
            body = body.replace(secret, MASK)
    print(body)
    return 0 if 200 <= resp.status_code < 300 else 1


# ── secrets ──


def secrets_status() -> int:
    config.load_config()  # migrates a legacy file first
    names = secret_store.stored_secret_names()
    print(f"Secret store: {secret_store.describe_backend()}")
    if names:
        print("Stored: " + ", ".join(names))
    else:
        print("Stored: none")
    leftovers = _backup_copies()
    if leftovers:
        print("Config backup copies that may hold secrets: "
              + ", ".join(p.name for p in leftovers)
              + " (heylead secrets clean removes them)")
    return 0


def secrets_set(name: str, *, read_value: Callable[[str], str] | None = None) -> int:
    if name not in secret_store.SECRET_FIELDS:
        print(
            f"heylead secrets set: unknown name {name}. Known: "
            + ", ".join(secret_store.SECRET_FIELDS),
            file=sys.stderr,
        )
        return 2
    if read_value is None:
        import getpass

        read_value = getpass.getpass
    value = read_value(f"{name} (input hidden, empty deletes): ")
    config.load_config()
    secret_store.set_secret(name, value)
    print(f"{name} {'stored' if value.strip() else 'deleted'} in {secret_store.backend_name()}.")
    return 0


def _all_backup_copies() -> list[Path]:
    path = config.config_path()
    home = path.parent
    if not home.exists():
        return []
    name = path.name
    found = [p for p in home.iterdir() if p.is_file() and (
        p.name.startswith(name + ".bak") or p.name == name + ".corrupt"
    )]
    return sorted(found)


def _safe_to_delete(copy: Path) -> bool:
    """True only when every secret the copy holds is already in the store with
    the same value. An unreadable copy may hold the only JWT: kept."""
    try:
        data = json.loads(copy.read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    for name, value in config._secret_items(data):
        if secret_store.get_secret(name) != value:
            return False
    return True


def _backup_copies() -> list[Path]:
    """Backup copies that can be deleted without losing a secret."""
    return [p for p in _all_backup_copies() if _safe_to_delete(p)]


def _kept_copies() -> list[Path]:
    return [p for p in _all_backup_copies() if not _safe_to_delete(p)]


def _unused_in_cloud() -> list[str]:
    """Stored secrets a cloud-sending backend install never uses."""
    if not (config.is_backend_mode() and config.get_sending_host() == "cloud"):
        return []
    candidates = ["unipile_api_key"] + [
        f"api_key.{p}" for p in secret_store.API_KEY_PROVIDERS
    ]
    return [n for n in candidates if secret_store.get_secret(n)]


def secrets_clean(*, yes: bool, confirm: Callable[[str], str] | None = None,
                  is_tty: bool | None = None) -> int:
    moved = config.migrate_secrets()
    if moved:
        print(f"Moved from the config file to {secret_store.backend_name()}: {', '.join(moved)}")
    unused = _unused_in_cloud()
    copies = _backup_copies()
    for p in _kept_copies():
        print(f"Kept file {p.name}: it may hold a secret the store does not have")
    if not unused and not copies:
        print("Nothing else to remove.")
        return 0
    print("Would remove:")
    for n in unused:
        print(f"  secret {n} (unused: this install sends from the cloud)")
    for p in copies:
        print(f"  file {p.name}")
    if not yes:
        if is_tty is None:
            is_tty = sys.stdin.isatty()
        if not is_tty:
            print("Not a terminal: nothing removed. Re-run with --yes.")
            return 1
        answer = (confirm or input)("Remove these? (yes/no): ").strip().lower()
        if answer not in ("y", "yes"):
            print("Cancelled.")
            return 1
    for n in unused:
        secret_store.delete_secret(n)
        print(f"Removed secret {n}")
    for p in copies:
        try:
            p.unlink()
            print(f"Removed file {p.name}")
        except OSError as e:
            print(f"Could not remove {p.name}: {type(e).__name__}", file=sys.stderr)
    return 0


def main_config(rest: list[str]) -> int | None:
    """Handle `config get|set`; None when the args are for another sub-command."""
    if rest and rest[0] == "get" and len(rest) == 2:
        return config_get(rest[1])
    if rest and rest[0] == "set" and len(rest) == 3:
        return config_set(rest[1], rest[2])
    return None


def main_api(rest: list[str]) -> int:
    if len(rest) != 2 or rest[0].lower() != "get":
        print("usage: heylead api get /api/v1/<path>", file=sys.stderr)
        return 2
    return api_get(rest[1])


def main_secrets(rest: list[str]) -> int:
    if rest == ["status"]:
        return secrets_status()
    if len(rest) == 2 and rest[0] == "set":
        return secrets_set(rest[1])
    if rest and rest[0] == "clean" and all(a == "--yes" for a in rest[1:]):
        return secrets_clean(yes="--yes" in rest[1:])
    print("usage: heylead secrets status | set <name> | clean [--yes]", file=sys.stderr)
    return 2
