"""HeyLead LinkedIn integration — via Unipile API or Backend proxy."""

from __future__ import annotations

from .unipile import (
    NetworkPoolNotMemberError,
    UnipileAuthError,
    UnipileClient,
    UnipileError,
    UnipileRateLimitError,
    UnipileResultFormatError,
    get_account_id,
    get_unipile_client,
    invalidate_account_id_cache,
    set_account_id,
)

__all__ = [
    "UnipileClient",
    "UnipileError",
    "UnipileAuthError",
    "UnipileRateLimitError",
    "UnipileResultFormatError",
    "NetworkPoolNotMemberError",
    "get_unipile_client",
    "get_account_id",
    "set_account_id",
    "invalidate_account_id_cache",
    "get_linkedin_client",
]

# Singleton BackendClient — avoids creating a new httpx.AsyncClient per call.
# Multiple tools call client.close() after use; with a singleton the close()
# is a no-op so the connection stays alive (mirrors _UnclosableConnection for SQLite).
_backend_client: object | None = None


def get_linkedin_client() -> UnipileClient:
    """Factory: return singleton BackendClient if backend config exists, else UnipileClient.

    Both implement the same interface so all tools work without changes.
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
            return _backend_client  # type: ignore[return-value]
        _backend_client = BackendClient(backend_url, jwt_token)
        return _backend_client  # type: ignore[return-value]

    return get_unipile_client()
