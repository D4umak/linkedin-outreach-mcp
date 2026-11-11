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
    """Factory: the singleton BackendClient for a signed-in install.

    With no sign-in token it raises the not-signed-in ``UnipileError``
    (callers catch that type): a stored Unipile key no longer reaches LinkedIn
    from this machine (heylead-api#2320; until then ``send_email`` sent with
    it past every gate). ``config.is_self_hosted()`` is False in production;
    the direct client behind it serves only the suite's tests of the engine
    until that code is deleted (heylead-api#5712).
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

    if not config.is_self_hosted():
        from .. import hosted_writes

        raise UnipileError(hosted_writes.not_signed_in_reply("nothing was done on LinkedIn"))
    return get_unipile_client()
