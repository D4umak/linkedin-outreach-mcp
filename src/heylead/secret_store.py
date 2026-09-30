"""Where HeyLead keeps its secrets: the OS credential store, never config.json.

config.json used to hold the LLM provider keys, the Unipile key and the backend
JWT next to ordinary settings. Agent sessions checking production opened that
file for the backend URL and printed live keys into their transcripts on 7, 17
and 29 Sep 2026 (api #2061). Secrets now live in the OS keyring (macOS
Keychain, Windows Credential Manager, Linux Secret Service) through
``keyring``; with no usable keyring they live in ``<heylead home>/secrets.json``
created owner-only (0600). ``HEYLEAD_SECRET_BACKEND=file|keyring`` overrides
the detection.

Nothing here ever logs or raises with a secret value: messages name the key.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

SERVICE = "heylead"

# The provider keys an install can hold, in their legacy config.json shape
# (``api_keys.<provider>``).
API_KEY_PROVIDERS: tuple[str, ...] = ("gemini", "claude", "openai", "serper", "hume")

# Top-level config.json fields that are secrets.
TOP_LEVEL_SECRET_FIELDS: tuple[str, ...] = (
    "unipile_api_key",
    "backend_jwt",
    "firecrawl_api_key",
)

# Every secret name the store knows. Defined once; config.py and the CLI read it.
SECRET_FIELDS: tuple[str, ...] = tuple(
    f"api_key.{p}" for p in API_KEY_PROVIDERS
) + TOP_LEVEL_SECRET_FIELDS

# A keyring call that has not answered in this long is treated as unavailable
# for that call: the daemon runs under launchd with no GUI and a locked or
# prompting keychain must not stall a tick.
KEYRING_TIMEOUT_S = 5.0

# After a keyring timeout or error, leave it alone this long: a hung keychain
# must cost one stuck thread and one wait, not 5 s on every read of a tick.
KEYRING_COOLDOWN_S = 300.0
_THREAD_NAME = "heylead-keyring"
_unavailable_until = 0.0
_inflight: threading.Thread | None = None


def _monotonic() -> float:
    return time.monotonic()


BACKENDS = ("keyring", "file")

# No value cache: every get reads the store, so a JWT another process saved
# (setup_profile in the MCP server, the daemon) is seen on the next call.

_warned: set[str] = set()


class SecretStoreError(RuntimeError):
    """A store operation failed. The message names the key, never the value."""


def _warn_once(key: str, msg: str, *args) -> None:
    if key not in _warned:
        _warned.add(key)
        logger.warning(msg, *args)


def _home() -> Path:
    from . import config

    return config._heylead_home()


def _ensure_home() -> Path:
    home = _home()
    if not home.exists():
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(home, 0o700)
    return home


def _file_path() -> Path:
    return _home() / "secrets.json"


def _keyring_usable() -> bool:
    try:
        import keyring
        from keyring.backends import chainer, fail
    except Exception:
        return False
    try:
        kr = keyring.get_keyring()
    except Exception:
        return False
    if isinstance(kr, fail.Keyring):
        return False
    mod = type(kr).__module__
    if mod.startswith("keyring.backends.null") or mod.startswith("keyring.backends.fail"):
        return False
    if isinstance(kr, chainer.ChainerBackend) and not kr.backends:
        return False
    return True


def _recorded_backend() -> str:
    from . import config

    value = str(config.read_raw_config().get("secret_backend") or "").strip().lower()
    return value if value in BACKENDS else ""


def backend_name() -> str:
    """'keyring' or 'file'.

    The env override wins (tests); then the backend this install recorded in
    the config file on its first write, so every process (MCP server, daemon)
    uses the same one; then detection.
    """
    forced = os.environ.get("HEYLEAD_SECRET_BACKEND", "").strip().lower()
    if forced in BACKENDS:
        return forced
    return _recorded_backend() or ("keyring" if _keyring_usable() else "file")


def _record_backend(name: str) -> None:
    from . import config

    if _recorded_backend():
        return
    try:
        config.record_secret_backend(name)
    except OSError as e:
        _warn_once("record", "could not record the secret backend: %s", type(e).__name__)


def describe_backend() -> str:
    """Human-readable location of the store, for status lines."""
    if backend_name() == "keyring":
        try:
            import keyring

            return f"OS keyring ({type(keyring.get_keyring()).__name__})"
        except Exception:
            return "OS keyring"
    return f"owner-only file {_file_path()}"


def _check_name(name: str) -> None:
    if name not in SECRET_FIELDS:
        raise ValueError(f"unknown secret name: {name!r}")


# ── file backend ──


def _load_file() -> tuple[dict[str, str], bool]:
    """(data, corrupt). A missing file is ({}, False)."""
    path = _file_path()
    if not path.exists():
        return {}, False
    try:
        with open(path, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except (OSError, ValueError) as e:
        _warn_once(f"corrupt:{path}", "secrets file unreadable (%s): %s", path, type(e).__name__)
        return {}, True
    return {k: str(v) for k, v in data.items() if isinstance(v, str) and v}, False


def _read_file() -> dict[str, str]:
    return _load_file()[0]


def _move_corrupt_aside() -> None:
    path = _file_path()
    aside = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
    os.replace(path, aside)
    os.chmod(aside, 0o600)
    logger.error("secrets file was unreadable; moved aside to %s", aside.name)


def _file_for_update() -> dict[str, str]:
    data, corrupt = _load_file()
    if corrupt:
        _move_corrupt_aside()
    return data


def _write_file(data: dict[str, str]) -> None:
    _ensure_home()
    path = _file_path()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".secrets-", suffix=".json")
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def ensure_file_exists() -> Path:
    """Create the fallback file (0600) if missing; return its path."""
    _ensure_home()
    path = _file_path()
    if not path.exists():
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("{}")
    os.chmod(path, 0o600)
    return path


# ── keyring backend, every call bounded ──


def _with_timeout(fn, *args):
    """Run a keyring call in a daemon thread. Raises SecretStoreError on
    timeout or failure; the message carries no value."""
    global _unavailable_until, _inflight
    if _monotonic() < _unavailable_until:
        raise SecretStoreError("keyring cooling down after a failure")
    if _inflight is not None and _inflight.is_alive():
        raise SecretStoreError("a previous keyring call has not returned")
    box: dict = {}

    def run():
        try:
            box["result"] = fn(*args)
        except BaseException as e:  # noqa: BLE001 - reported by type only
            box["error"] = e

    t = threading.Thread(target=run, daemon=True, name=_THREAD_NAME)
    _inflight = t
    t.start()
    t.join(KEYRING_TIMEOUT_S)
    if t.is_alive():
        _unavailable_until = _monotonic() + KEYRING_COOLDOWN_S
        raise SecretStoreError(f"keyring did not answer within {KEYRING_TIMEOUT_S:g}s")
    _inflight = None
    if "error" in box:
        _unavailable_until = _monotonic() + KEYRING_COOLDOWN_S
        raise SecretStoreError(f"keyring call failed ({type(box['error']).__name__})")
    return box.get("result")


def _kr_get(name: str) -> str:
    import keyring

    return _with_timeout(keyring.get_password, SERVICE, name) or ""


def _kr_set(name: str, value: str) -> None:
    import keyring

    _with_timeout(keyring.set_password, SERVICE, name, value)


def _kr_delete(name: str) -> None:
    import keyring
    from keyring.errors import PasswordDeleteError

    def delete():
        try:
            keyring.delete_password(SERVICE, name)
        except PasswordDeleteError:
            pass

    _with_timeout(delete)


# ── public API ──


def get_secret(name: str) -> str:
    """The stored value, or "" when unset or the store cannot answer.

    With a keyring that is unavailable (locked, no GUI, timed out) the file
    backend is read instead, and callers fall back to a legacy in-file value
    (config._secret).
    """
    _check_name(name)
    if backend_name() == "keyring":
        try:
            if not _keyring_usable():
                raise SecretStoreError("no usable keyring backend")
            return _kr_get(name)
        except SecretStoreError as e:
            _warn_once("kr-read", "keyring unavailable (%s); reading %s from the file store", e, name)
            return _read_file().get(name, "")
    return _read_file().get(name, "")


def set_secret(name: str, value: str) -> None:
    """Store ``value`` under ``name``; an empty value deletes it.

    Raises SecretStoreError (naming the key, never the value) on failure.
    """
    _check_name(name)
    value = (value or "").strip()
    if not value:
        delete_secret(name)
        return
    backend = backend_name()
    if backend == "keyring":
        try:
            _kr_set(name, value)
        except SecretStoreError as e:
            raise SecretStoreError(
                f"could not store secret {name} in the OS keyring: {e}"
            ) from None
    else:
        try:
            data = _file_for_update()
            data[name] = value
            _write_file(data)
        except OSError as e:
            raise SecretStoreError(
                f"could not store secret {name} in {_file_path().name} ({type(e).__name__})"
            ) from None
    _record_backend(backend)


def delete_secret(name: str) -> None:
    _check_name(name)
    if backend_name() == "keyring":
        try:
            _kr_delete(name)
        except SecretStoreError as e:
            logger.error("keyring delete failed for %s: %s", name, e)
    data = _file_for_update() if _file_path().exists() else {}
    if name in data:
        del data[name]
        _write_file(data)


def stored_secret_names() -> list[str]:
    """Names with a non-empty stored value. Never the values."""
    return [n for n in SECRET_FIELDS if get_secret(n)]


def clear_cache() -> None:
    """Kept for callers; there is no value cache any more."""
    _warned.clear()
