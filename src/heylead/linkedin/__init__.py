"""HeyLead LinkedIn integration — via Unipile API or Backend proxy."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .backend_client import BackendClient

from .unipile import (
    NetworkPoolNotMemberError,
    UnipileAuthError,
    UnipileError,
    UnipileRateLimitError,
    UnipileResultFormatError,
    get_account_id,
    invalidate_account_id_cache,
    set_account_id,
)

__all__ = [
    "UnipileError",
    "UnipileAuthError",
    "UnipileRateLimitError",
    "UnipileResultFormatError",
    "NetworkPoolNotMemberError",
    "get_account_id",
    "set_account_id",
    "invalidate_account_id_cache",
    "get_linkedin_client",
]

# Singleton BackendClient — avoids creating a new httpx.AsyncClient per call.
# Multiple tools call client.close() after use; with a singleton the close()
# is a no-op so the connection stays alive (mirrors _UnclosableConnection for SQLite).
_backend_client: object | None = None


def get_linkedin_client() -> BackendClient:
    """Factory: the singleton BackendClient for a signed-in install.

    With no sign-in token it raises the not-signed-in ``UnipileError``
    (callers catch that type). A stored Unipile key never reaches LinkedIn
    from this machine (heylead-api#2320).
    """
    global _backend_client
    from .. import config

    if config.is_backend_mode():
        from .backend_client import BackendClient
        backend_url, jwt_token = config.get_backend_config()
        # Reuse existing singleton if URL and token match
        if (
            _backend_client is not None
            and isinstance(_backend_client, BackendClient)
            and _backend_client.base_url == backend_url.rstrip("/")
            and _backend_client.jwt_token == jwt_token
        ):
            return _backend_client
        _backend_client = BackendClient(backend_url, jwt_token)
        return _backend_client

    from .. import hosted_writes

    raise UnipileError(hosted_writes.not_signed_in_reply("nothing was done on LinkedIn"))
