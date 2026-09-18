"""BackendClient — calls HeyLead Backend API proxy instead of Unipile directly.

Same method signatures as UnipileClient so all 5 MCP tools work unchanged.
The backend handles Unipile auth, account scoping, and API key security.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import quote

import httpx

from heylead import __version__

from ..author_identity import parse_author_identity
from ..constants import UNIPILE_POLL_INTERVAL_SECONDS, UNIPILE_POLL_TIMEOUT_SECONDS
from ..guardrails import check_message, prepare_outbound_text
from .api_metrics import api_metrics
from .relations import RelationsPage
from .search_traffic import SearchTraffic
from .unipile import (
    ChatLookupUnavailable,
    detect_reshare,
    NetworkPoolNotMemberError,
    POST_URN_CLASSES,
    UnipileInvalidRecipientError,
    UnipileAuthError,
    UnipileError,
    UnipileRateLimitError,
    UnipileResultFormatError,
    _is_invalid_post_error,
    _is_newsletter_invitation,
    _normalize_comment,
    _post_urn,
    interpret_account_status,
    parse_retry_after,
)
from .voyager_health import voyager_health

logger = logging.getLogger(__name__)

# User-facing message when backend has no LinkedIn account bound (no new APIs).
_NO_LINKEDIN_MSG = (
    "No LinkedIn account connected on the backend. "
    "Re-run setup_profile to connect LinkedIn, then try again."
)

# Shown whenever a knowledge route answers 5xx: the corpus is down, and the
# caller has nothing to fix.
_KNOWLEDGE_UNAVAILABLE = "Knowledge base is unavailable right now — try again shortly."

_TIMEOUT = httpx.Timeout(30.0, connect=30.0, read=30.0, write=60.0)

# The /api/v1/llm/* endpoints run model inference server-side, so they answer on
# a completely different timescale to the Unipile REST proxy. generate-icp
# produces several personas and then enriches them; generate-icp-rag also
# ingests, embeds and retrieves first. Both routinely exceed 30s and were
# failing as ReadTimeout — intermittently, because it depends on model latency,
# which is the worst way for it to fail. Slow syncs already carry their own
# 300s override further down; this gives the LLM routes the same treatment.
_LLM_TIMEOUT = httpx.Timeout(300.0, connect=30.0, read=300.0, write=60.0)
_LLM_PATH = "/api/v1/llm/"

# Retry configuration for transient failures (send operations only)
_MAX_RETRIES = 3
_RETRY_BACKOFF = [2, 4, 8]  # seconds
_RETRYABLE_STATUS_CODES = {502, 503, 504}

# find_chat_for_user used to fall back to POST /api/v1/chats when the recent-chat
# scan came up empty. The proxy rejects that body with
# 422 {"detail":[{"type":"missing","loc":["body","text"]}]} — 9,783 failures in two
# days (~30 per reply-check cycle), not one success. The endpoint only accepts a
# body that carries a message, and sending one would DM the prospect, which a
# lookup must never do, so the fallback is gone. Announce that once per process
# instead of once per prospect: the burst of identical warnings was the loudest
# thing in the log and told nobody anything.
_chat_fallback_notice_logged = False


# Slugs that mean "we don't actually have one". Upstream sometimes returns a
# profile_url of ".../in/undefined" — a non-empty string, so a truthiness check
# accepts it and it beats the perfectly good public_identifier sitting next to
# it in the same payload. 31 contacts were saved with a dead URL that way.
_BAD_SLUGS = frozenset({"undefined", "null", "none", "unknown", ""})


def _profile_url(raw: str, public_id: str) -> str:
    """Best usable LinkedIn profile URL, or "" when neither source has one.

    Prefers the URL upstream gave us, but only when its slug is real; otherwise
    rebuilds from public_identifier. Returning "" is deliberate — an empty URL
    is detectable by callers, a ".../in/undefined" one silently is not.
    """
    raw = (raw or "").strip()
    if raw:
        slug = raw.rstrip("/").rsplit("/", 1)[-1].split("?")[0].strip().lower()
        if slug not in _BAD_SLUGS:
            return raw
    pid = (public_id or "").strip()
    if pid and pid.lower() not in _BAD_SLUGS:
        return f"https://www.linkedin.com/in/{pid}"
    return ""


def _raise_rate_limit(resp: httpx.Response) -> None:
    """Raise a clear error for 429 responses. Optional Retry-After hint.

    Raises UnipileRateLimitError, a UnipileError subclass, so every existing
    ``except UnipileError`` handler behaves exactly as before while callers that
    pace themselves can read ``.retry_after`` off it.
    """
    retry_after = (resp.headers.get("Retry-After") or "").strip()
    if retry_after.isdigit():
        secs = int(retry_after)
        if secs >= 60:
            msg = f"Rate limited. Try again in {secs // 60} minutes."
        else:
            msg = f"Rate limited. Try again in {secs} seconds."
    else:
        msg = "Rate limited. Try again in a few minutes."
    raise UnipileRateLimitError(msg, retry_after=parse_retry_after(resp))


def _raise_if_pool_denied(resp: httpx.Response) -> None:
    """Turn a consumer route's 403 into NetworkPoolNotMemberError.

    The shared network pool became reciprocal: the /network/* consumer routes
    lend other members' reach only to a workspace whose own LinkedIn seat is an
    active pool member, and refuse everyone else with 403. Left to
    ``raise_for_status()`` that arrives at the user as "Client error '403
    Forbidden' for url ..." plus a link to MDN, which names no remedy.

    The backend's own ``detail`` is carried through rather than replaced, so
    the tool layer can lead with the server's reason. Nothing here decides
    *why* the caller was refused — the 403 is the server's verdict, and the
    client does not add a membership claim of its own on top of it.
    """
    if resp.status_code != 403:
        return
    detail = ""
    try:
        body = resp.json()
    except Exception:
        body = None
    if isinstance(body, dict):
        raw = body.get("detail") or body.get("message") or body.get("error") or ""
        if isinstance(raw, str):
            detail = raw.strip()
    raise NetworkPoolNotMemberError(detail=detail)


# What the client used to say for every 400 from a LinkedIn proxy route. Kept as
# the fallback when the backend gives no reason of its own.
_NO_LINKEDIN_SHORT = "No LinkedIn account connected."

# Status codes on which the LinkedIn proxy routes refuse the call with a reason
# meant for the user: 400 the active workspace has no LinkedIn seat of its own,
# 403 a viewer tried to act, 404 the active workspace is stale or gone.
_REFUSAL_STATUSES = (400, 403, 404)

# Framework defaults that name the status and nothing else. Showing "Not Found"
# instead of today's message would lose information, so treat them as absent.
_GENERIC_DETAILS = frozenset({"bad request", "forbidden", "not found"})

# resolve_org_context raises 404 "Organization not found" on EVERY route when
# the workspace the client pins is one the caller is not a member of — removed
# from it, left it, or it was deleted. Since setup pins the workspace the token
# names, that pin no longer degrades to the backend's default: the LinkedIn
# path just 404s forever, and the server's own detail names no way out.
#
# The remedy is a message, not a retry. Clearing active_org_id here and trying
# again would move the user between workspaces without their knowledge, which
# is the bug class this pinning exists to close.
_STALE_ORG_DETAIL = "organization not found"
STALE_WORKSPACE_MSG = (
    "The workspace HeyLead is using is no longer available — you may have been "
    "removed from it, or it may have been deleted. Run organization(action='list') "
    "to see your workspaces, then "
    "organization(action='switch', org_id='...') to move to one."
)


def _refusal_detail(resp: httpx.Response) -> str:
    """The backend's own reason from an error body, or "" when there is none.

    Only a non-generic string counts: a missing body, non-JSON, FastAPI's
    validation list, or a bare "Not Found" all return "".
    """
    try:
        body = resp.json()
    except Exception:
        return ""
    if not isinstance(body, dict):
        return ""
    for key in ("detail", "message", "error"):
        raw = body.get(key)
        if isinstance(raw, str) and raw.strip():
            text = raw.strip()
            if text.lower().rstrip(".") in _GENERIC_DETAILS:
                return ""
            return text[:500]
    return ""


def _proxy_refusal_message(
    resp: httpx.Response, fallback_400: str = _NO_LINKEDIN_SHORT,
) -> str | None:
    """User-facing message for a 400/403/404 from a LinkedIn proxy route.

    The backend acts only through the active workspace's own LinkedIn seat and
    says why when it cannot (no seat in this workspace, viewer role, stale
    workspace). Replacing that with "No LinkedIn account connected." told a
    user who had just created a workspace that their personal LinkedIn was
    gone. So the server's ``detail`` wins.

    The one detail that does not win is 404 "Organization not found": it names
    the failure but no remedy, and since setup pins a workspace, nothing
    recovers from it on its own. STALE_WORKSPACE_MSG names the two calls that
    do.

    Returns None when the caller should carry on exactly as before: any other
    status, or a 403/404 with no usable detail (those keep today's handling).
    A 400 always yields a message, falling back to *fallback_400*.
    """
    status = resp.status_code
    if status not in _REFUSAL_STATUSES:
        return None
    detail = _refusal_detail(resp)
    if status == 404 and detail.strip().lower().rstrip(".") == _STALE_ORG_DETAIL:
        return STALE_WORKSPACE_MSG
    if detail:
        return detail
    if status == 400:
        return fallback_400
    return None


def _raise_proxy_refusal(
    resp: httpx.Response,
    fallback_400: str = _NO_LINKEDIN_SHORT,
    *,
    auth_on_403: bool = False,
) -> None:
    """Raise for a 400/403/404 refusal, keeping each status's exception type.

    - 400 -> UnipileError(detail or *fallback_400*), as before.
    - 403 -> UnipileAuthError when the method already mapped 403 to it
      (*auth_on_403*), otherwise httpx.HTTPStatusError, as raise_for_status did.
    - 404 -> httpx.HTTPStatusError, as raise_for_status did.

    Only the message changes. A 403/404 without a usable detail raises nothing
    (except the *auth_on_403* case, which raises today's default
    UnipileAuthError), so the caller's raise_for_status() still produces
    today's error.
    """
    status = resp.status_code
    if status == 403 and auth_on_403:
        raise UnipileAuthError(_refusal_detail(resp))
    message = _proxy_refusal_message(resp, fallback_400)
    if message is None:
        return
    if status == 400:
        raise UnipileError(message)
    raise httpx.HTTPStatusError(message, request=resp.request, response=resp)


def _wrap_connection_error(e: Exception, base_url: str) -> UnipileError:
    """Wrap low-level httpx errors with user-friendly messages."""
    if isinstance(e, httpx.ConnectError):
        return UnipileError(
            f"Cannot connect to HeyLead backend at {base_url}. "
            "Is the URL correct? Check your internet connection."
        )
    if isinstance(e, httpx.ConnectTimeout):
        return UnipileError(
            f"Connection to {base_url} timed out. "
            "The backend may be starting up — try again in 30 seconds."
        )
    if isinstance(e, httpx.ReadTimeout):
        return UnipileError(
            f"Request to {base_url} timed out waiting for response. "
            "Try again — the backend may be under heavy load."
        )
    if isinstance(e, httpx.TimeoutException):
        return UnipileError(f"Request to {base_url} timed out. Try again.")
    return UnipileError(f"Backend request failed: {e}")


def _ensure_dict(data: Any) -> dict[str, Any]:
    """Coerce resp.json() output to a dict.

    If the backend returns a JSON string instead of an object (e.g. an error
    message), wrap it so callers can safely use .get().
    """
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        return {"items": data}
    # str, int, etc.
    return {"_raw": data}


def _unipile_error_in_body(resp: Any) -> str:
    """Return an error string if the proxy wrapped a Unipile failure, else "".

    The hosted proxy answers with its own HTTP status and relays Unipile's
    inside the body as ``{"status_code": ..., "body": ...}``; only a transport
    failure in the backend surfaces as a 502. So an outer 200 says the proxy
    worked, not that LinkedIn did, and a caller that reads only the outer
    status cannot tell a refusal from a success.
    """
    try:
        data = _ensure_dict(resp.json())
    except (ValueError, AttributeError):
        return ""
    inner_status = data.get("status_code", 200)
    if not isinstance(inner_status, int) or inner_status < 400:
        return ""
    detail = data.get("body", {})
    if isinstance(detail, dict):
        detail = detail.get("detail", detail)
    return f"Unipile returned {inner_status}: {str(detail)[:200]}"


def _hosted_comment_status(resp: Any) -> tuple[int, str]:
    """HTTP or nested proxy status plus body text for the invalid_post check."""
    status = int(getattr(resp, "status_code", 0) or 0)
    text = str(getattr(resp, "text", "") or "")[:500]
    if status < 400:
        try:
            data = resp.json()
        except Exception:
            data = None
        if isinstance(data, dict) and data.get("status_code") is not None:
            try:
                status = int(data["status_code"])
            except (TypeError, ValueError):
                pass
            text = str(data.get("body") or text)[:500]
    return status, text


def _classify_nested_response(
    data: dict[str, Any],
    result: dict[str, Any],
    *,
    accept_codes: tuple[int, ...] = (200, 201),
) -> None:
    """Parse nested Unipile status_code from backend proxy response.

    The backend proxy returns ``{"status_code": N, "body": ...}`` for send
    operations. This helper classifies the nested code and mutates *result*
    in-place with the appropriate flags (success/error/blocked/auth_error/permanent).
    """
    status_code = data.get("status_code")
    if status_code is None:
        logger.warning("Backend response missing status_code field: %s", data)
        result["error"] = "Backend response missing status_code"
        return
    if status_code in accept_codes:
        result["success"] = True
        # Extract external IDs from the nested Unipile response body
        body = data.get("body")
        if isinstance(body, dict):
            for key in ("id", "message_id", "invitation_id", "comment_id",
                        "reaction_id", "chat_id"):
                val = body.get(key)
                if val:
                    result[key] = str(val)
        return

    resp_body = data.get("body") or {}
    resp_text = str(resp_body).lower()

    if status_code == 429:
        result["error"] = "Rate limited by LinkedIn. Will retry later."
        result["blocked"] = True
    elif status_code in (401, 403):
        detail = str(resp_body)[:300] if resp_body else "no body"
        # Distinguish subscription/permission errors from real auth failures
        if "subscription_required" in resp_text or "subscription required" in resp_text:
            result["error"] = f"LinkedIn Premium required for this action. Unipile {status_code}: {detail}"
            result["permanent"] = True
        elif "not connected" in resp_text or "not_connected" in resp_text:
            result["error"] = f"Cannot message — not a connection. Unipile {status_code}: {detail}"
            result["permanent"] = True
        elif status_code == 401:
            logger.error(
                "Auth error from Unipile: status=%d body=%s", status_code, detail,
            )
            result["error"] = f"LinkedIn account disconnected. Unipile {status_code}: {detail}"
            result["auth_error"] = True
            result["blocked"] = True
        else:
            # Generic 403 — could be permission, not necessarily disconnected
            logger.warning(
                "Permission error from Unipile: status=%d body=%s", status_code, detail,
            )
            result["error"] = f"Permission denied by LinkedIn. Unipile {status_code}: {detail}"
            result["permanent"] = True
    elif status_code == 409:
        result["error"] = "Already connected or action already taken."
    elif status_code == 404:
        result["error"] = f"Not found: {str(resp_body)[:200]}"
        result["permanent"] = True
    elif status_code == 422:
        if "temporary provider limit" in resp_text or "temporary_provider_limit" in resp_text:
            result["error"] = "Unipile returned 422: temporary_provider_limit"
            result["blocked"] = True
            result["rate_limited_422"] = True
        elif "cannot_resend_yet" in resp_text or "cannot resend yet" in resp_text:
            result["success"] = True
        elif "already_invited_recently" in resp_text or "already invited" in resp_text:
            # Invite was already sent — treat as success so outreach is marked "invited"
            result["success"] = True
        elif "no_connection_with_recipient" in resp_text or "not to be first degree" in resp_text:
            result["error"] = "Cannot message — not a 1st-degree connection."
            result["permanent"] = True
        elif "user_unreachable" in resp_text or "does not allow incoming" in resp_text:
            result["error"] = "Recipient does not allow incoming messages."
            result["permanent"] = True
        elif "invalid_recipient" in resp_text or "profile is not locked" in resp_text:
            result["error"] = "Recipient profile is locked or invalid."
            result["permanent"] = True
        else:
            result["error"] = f"Unipile returned 422: {str(resp_body)[:200]}"
            result["permanent"] = True
    else:
        detail = str(resp_body)[:300] if resp_body else "no body"
        result["error"] = f"Unipile returned {status_code}: {detail}"


def _parse_timestamp(ts_raw: Any) -> int:
    """Parse a timestamp from various formats into unix seconds."""
    if isinstance(ts_raw, (int, float)):
        return int(ts_raw)
    if isinstance(ts_raw, str) and ts_raw:
        try:
            from datetime import datetime
            return int(datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp())
        except (ValueError, TypeError):
            pass
    return 0


def multipart_headers(base: dict[str, str]) -> dict[str, str]:
    """*base* with every spelling of content-type removed.

    httpx sets the multipart boundary only when nothing else claims the
    header, and a dict pop is case-sensitive. In the backend repo that cost
    every voice DM ever sent: _headers() returned a lowercase content-type,
    the sender popped "Content-Type", the JSON one survived, and Unipile
    parsed --<boundary> as JSON. This client spells it the other way today,
    which is one rename away from the same outage.
    """
    return {k: v for k, v in base.items() if k.lower() != "content-type"}


class BackendClient:
    """Async HTTP client that calls HeyLead Backend API proxy.

    Provides the same interface as UnipileClient so tools don't need to know
    whether they're talking to Unipile directly or via the backend.
    """

    def __init__(self, backend_url: str, jwt_token: str) -> None:
        self.base_url = backend_url.rstrip("/")
        self.jwt_token = jwt_token
        self._client = httpx.AsyncClient(timeout=_TIMEOUT)
        # Search requests that actually went on the wire. search_posts and
        # search_jobs swallow transport errors and return ([], None), so this
        # is the only place a collector can learn whether its search budget
        # bought a request or an outage. See linkedin/search_traffic.py.
        self.search_traffic = SearchTraffic()

    async def _post(self, url: str, **kwargs: Any) -> httpx.Response:
        """POST with a timeout chosen by endpoint class.

        Model-inference routes get _LLM_TIMEOUT; everything else keeps the
        30s default so a hung REST call still fails fast. Call sites that pass
        an explicit timeout (the 300s sync overrides) keep theirs.
        """
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = _LLM_TIMEOUT if _LLM_PATH in url else _TIMEOUT
        return await self._client.post(url, **kwargs)

    def _headers(self) -> dict[str, str]:
        from ..correlation import get_correlation_id

        headers = {
            "Authorization": f"Bearer {self.jwt_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-HeyLead-Client": f"heylead/{__version__}",
        }
        from ..config import get_active_org_id
        org_id = get_active_org_id()
        if org_id:
            headers["X-Org-Id"] = org_id
        cid = get_correlation_id()
        if cid:
            headers["X-Correlation-ID"] = cid
        return headers

    async def close(self) -> None:
        """No-op: BackendClient is a singleton; the httpx client stays alive.

        Multiple tools call client.close() after use. With a singleton this
        would break subsequent requests ("client has been closed"). Mirroring
        the _UnclosableConnection pattern used for SQLite.
        """
        pass

    async def _real_close(self) -> None:
        """Actually close the underlying httpx client (for shutdown only)."""
        await self._client.aclose()

    # ── Voyager Health Check ──

    _voyager_available: bool | None = None
    _voyager_checked_at: float = 0.0

    async def is_voyager_available(self, endpoint: str = "wvmpCards") -> bool:
        """Check if Voyager magic route is working (cached for 1 hour).

        Tests the voyager-test endpoint with a known wvmpCards URL.
        Also feeds the per-endpoint ``voyager_health`` tracker.

        Args:
            endpoint: Optional endpoint name for per-endpoint health queries.
                      Pass a specific name (e.g. "follow", "ssi") to check
                      that endpoint's health in the tracker; defaults to the
                      general "wvmpCards" probe.

        Returns True if Voyager responds with 200, False otherwise.
        """
        import time as _time

        # Fast path: if the per-endpoint tracker says this endpoint is unhealthy,
        # skip the network call entirely.
        if not voyager_health.is_healthy(endpoint):
            return False

        now = _time.time()
        # Cache the wvmpCards probe for 1 hour
        if self._voyager_available is not None and (now - self._voyager_checked_at) < 3600:
            return self._voyager_available

        # Use wvmpCards (Who Viewed My Profile) as the health check URL.
        # This is the most reliable Voyager endpoint — other routes like SSI,
        # company feed, and followers return 400 on some LinkedIn sessions.
        test_url = "https://www.linkedin.com/voyager/api/identity/wvmpCards"
        try:
            url = f"{self.base_url}/api/v1/linkedin/voyager-test"
            resp = await self._post(
                url,
                json={"url": test_url, "method": "GET"},
                headers=self._headers(),
            )
            if resp.status_code == 200:
                data = _ensure_dict(resp.json())
                # Voyager working if inner status is 200
                inner_status = data.get("status_code", 0)
                inner_body = str(data.get("body", ""))[:200]
                self._voyager_available = inner_status == 200
                voyager_health.record("wvmpCards", success=self._voyager_available)
                logger.info(
                    "Voyager health check: outer=200 inner_status=%d available=%s body=%s",
                    inner_status, self._voyager_available, inner_body,
                )
            else:
                self._voyager_available = False
                voyager_health.record("wvmpCards", success=False)
                logger.warning(
                    "Voyager health check: outer_status=%d body=%s",
                    resp.status_code, str(resp.text)[:300],
                )
        except Exception as e:
            self._voyager_available = False
            voyager_health.record("wvmpCards", success=False)
            logger.warning("Voyager health check exception: %s", e)

        self._voyager_checked_at = now
        return self._voyager_available

    async def _retry_request(
        self,
        method: str,
        url: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        no_retry: bool = False,
    ) -> httpx.Response:
        """HTTP request with retry on transient failures.

        Used for send operations (invitation, message, comment, reaction, posts).
        Setup/auth operations should NOT use this — they fail fast.

        ``no_retry`` is for actions the backend may already have carried out
        when the answer never arrives — a DM, an InMail, a comment, an email.
        Unipile offers no idempotency key, so a second POST after a read
        timeout or a 502/504 delivers the action twice. Those callers take the
        unknown outcome over the duplicate.

        This mirrors UnipileClient._retry_request deliberately: the same rule
        has to hold on both transports, and it did not — the parameter did not
        exist here, so every hosted send retried. tests/test_backend_send_no_retry
        compares the two clients' guarded sets to keep them from drifting again.
        """
        import time as _time

        max_attempts = 1 if no_retry else _MAX_RETRIES

        # Derive a short endpoint label from the URL path for metrics
        endpoint_label = url.rsplit("/", 1)[-1] if "/" in url else url

        last_error: Exception | None = None
        for attempt in range(max_attempts):
            t0 = _time.monotonic()
            try:
                if method.upper() == "GET":
                    # follow_redirects on GET only. httpx defaults to False, so
                    # a 3xx came back as a response whose body is a redirect
                    # page — which is how HTML reached a profile column. GETs
                    # are idempotent; a redirected POST could deliver twice,
                    # and Unipile has no idempotency key, so it stays off there.
                    resp = await self._client.get(
                        url, headers=self._headers(), params=params,
                        follow_redirects=True,
                    )
                else:
                    resp = await self._post(url, json=json, headers=self._headers())
                elapsed = int((_time.monotonic() - t0) * 1000)
                # Extract backend processing time from response header
                backend_ms = 0
                _pt = resp.headers.get("x-processing-time", "")
                if _pt.isdigit():
                    backend_ms = int(_pt)
                api_metrics.record(
                    endpoint_label,
                    status_code=resp.status_code,
                    duration_ms=elapsed,
                    error=resp.text[:200] if resp.status_code >= 400 else "",
                    backend_ms=backend_ms,
                )
                if resp.status_code == 429:
                    if attempt < max_attempts - 1:
                        # Parse Retry-After header if present, else use backoff
                        retry_after = resp.headers.get("retry-after")
                        if retry_after and retry_after.isdigit():
                            delay = min(int(retry_after), 60)
                        else:
                            delay = _RETRY_BACKOFF[attempt] * 2  # double backoff for rate limits
                        logger.info(
                            "Rate limited (429), retrying %s %s in %ds (attempt %d)",
                            method, url, delay, attempt + 1,
                        )
                        await asyncio.sleep(delay)
                        continue
                    _raise_rate_limit(resp)
                if resp.status_code in _RETRYABLE_STATUS_CODES and attempt < max_attempts - 1:
                    delay = _RETRY_BACKOFF[attempt]
                    logger.info(
                        f"Retry {method} {url} (HTTP {resp.status_code}, "
                        f"attempt {attempt + 1}, {delay}s)"
                    )
                    await asyncio.sleep(delay)
                    continue
                return resp
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                elapsed = int((_time.monotonic() - t0) * 1000)
                api_metrics.record(
                    endpoint_label, status_code=0, duration_ms=elapsed, error=str(e),
                )
                last_error = e
                if attempt < max_attempts - 1:
                    delay = _RETRY_BACKOFF[attempt]
                    logger.info(
                        f"Retry {method} {url} ({type(e).__name__}, "
                        f"attempt {attempt + 1}, {delay}s)"
                    )
                    await asyncio.sleep(delay)
                else:
                    raise
        raise last_error or httpx.TimeoutException("All retries exhausted")

    def _check_rate_limit(self, resp: httpx.Response) -> None:
        """Raise a clear error for 429 so direct request paths surface rate-limit message."""
        if resp.status_code == 429:
            _raise_rate_limit(resp)

    # ── Auth / Account Management ──

    async def create_hosted_auth_link(
        self,
        success_redirect_url: str = "",
        providers: list[str] | None = None,
    ) -> str:
        """Create a hosted auth link via the backend proxy.

        LinkedIn (the default) uses /api/v1/auth/connect. Email providers
        use /api/v1/auth/connect-email so Gmail/Outlook never share the
        LinkedIn connect path.
        """
        providers = list(providers) if providers else ["LINKEDIN"]
        wants_email = any(str(p).upper() != "LINKEDIN" for p in providers)
        if wants_email:
            return await self._create_email_auth_link(success_redirect_url, providers)

        url = f"{self.base_url}/api/v1/auth/connect"
        params = {}
        if success_redirect_url:
            params["success_redirect_url"] = success_redirect_url
        try:
            resp = await self._post(url, params=params, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url)
        self._check_rate_limit(resp)
        if resp.status_code == 401:
            raise UnipileAuthError("Backend JWT expired or invalid. Run setup_profile again.")
        if resp.status_code == 409:
            data = _ensure_dict(resp.json())
            raise UnipileError(data.get("detail", "Account already connected"))
        resp.raise_for_status()
        result = resp.json()
        auth_url = result.get("url")
        if not auth_url:
            raise UnipileError(f"Backend response missing 'url': {result}")
        return auth_url

    async def _create_email_auth_link(
        self,
        success_redirect_url: str,
        providers: list[str],
    ) -> str:
        """Mint a Unipile hosted-auth link for a mailbox (Gmail/Outlook/IMAP)."""
        url = f"{self.base_url}/api/v1/auth/connect-email"
        params = {}
        if success_redirect_url:
            params["success_redirect_url"] = success_redirect_url
        payload = {"providers": providers}
        try:
            resp = await self._post(
                url, json=payload, params=params, headers=self._headers(),
            )
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url)
        self._check_rate_limit(resp)
        if resp.status_code == 401:
            raise UnipileAuthError("Backend JWT expired or invalid. Run setup_profile again.")
        if resp.status_code == 404:
            raise UnipileError(
                "This HeyLead backend has no email-connect endpoint yet. "
                "Connect Gmail or Outlook at heylead.dev, then retry send_email. "
                "Do not use Mail.app."
            )
        if resp.status_code == 409:
            data = _ensure_dict(resp.json())
            raise UnipileError(data.get("detail", "Email account already connected"))
        resp.raise_for_status()
        result = _ensure_dict(resp.json())
        auth_url = result.get("url")
        if not auth_url:
            raise UnipileError(f"Backend response missing 'url': {result}")
        return auth_url

    async def list_accounts(self) -> list[dict[str, Any]]:
        """List accounts via the backend proxy (filtered to user's account)."""
        url = f"{self.base_url}/api/v1/accounts"
        try:
            resp = await self._client.get(url, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url)
        self._check_rate_limit(resp)
        if resp.status_code == 401:
            raise UnipileAuthError("Backend JWT expired or invalid.")
        resp.raise_for_status()
        data = _ensure_dict(resp.json())
        return data.get("accounts", [])

    async def find_linkedin_account(self) -> tuple[str | None, str]:
        """Find a LinkedIn account from the backend."""
        try:
            accounts = await self.list_accounts()
        except Exception as e:
            return None, f"Failed to list accounts: {e}"

        if not accounts:
            return None, "No accounts found."

        for acc in accounts:
            provider = acc.get("provider") or acc.get("provider_type") or acc.get("type") or ""
            if "LINKEDIN" in str(provider).upper():
                account_id = (
                    acc.get("id") or acc.get("account_id")
                    or acc.get("accountId") or acc.get("uuid")
                )
                if account_id:
                    return str(account_id), "Found LinkedIn account"
        return None, "No LinkedIn accounts found."

    async def get_user_info(self) -> dict[str, Any]:
        """Get the authenticated user's account bindings from the backend.

        Returns dict with account_id, email_account_id, email, name.
        """
        url = f"{self.base_url}/api/v1/auth/me"
        try:
            resp = await self._client.get(url, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url)
        self._check_rate_limit(resp)
        if resp.status_code == 401:
            raise UnipileAuthError("Backend JWT expired or invalid.")
        resp.raise_for_status()
        return resp.json()

    async def verify_account(self, account_id: str) -> tuple[bool, str]:
        """Check if a Unipile account is still connected via the backend."""
        try:
            url = f"{self.base_url}/api/v1/accounts/{account_id}"
            resp = await self._client.get(url, headers=self._headers())
            self._check_rate_limit(resp)
            if resp.status_code == 403:
                return False, "Access denied: not your account"
            if resp.status_code == 404:
                return False, "Account not found"
            if resp.status_code == 401:
                return False, "Backend JWT expired"
            resp.raise_for_status()
            data = _ensure_dict(resp.json())
            return interpret_account_status(data)
        except Exception as e:
            return False, f"Verification error: {e}"

    async def get_reconnect_link(self) -> str:
        """Fetch a Unipile hosted-auth link so the user can re-sign in to LinkedIn.

        Returns the URL string, or empty string if the backend couldn't mint one.
        """
        url = f"{self.base_url}/auth/reconnect-linkedin"
        try:
            resp = await self._client.get(url, headers=self._headers())
            self._check_rate_limit(resp)
            if resp.status_code != 200:
                return ""
            body = _ensure_dict(resp.json())
            link = body.get("url") or ""
            return str(link) if link else ""
        except Exception:
            return ""

    async def bind_account(self, account_id: str) -> None:
        """Bind a Unipile account_id to the authenticated user on the backend."""
        url = f"{self.base_url}/api/v1/accounts/{account_id}/bind"
        resp = await self._post(url, headers=self._headers())
        self._check_rate_limit(resp)
        if resp.status_code == 409:
            pass  # Already bound — that's fine
        elif resp.status_code in (401, 403):
            # A refused bind is a verdict, not a transient gap. Callers that
            # retry unknown outcomes (poll_for_new_account) must not retry this.
            detail = resp.text[:200]
            raise UnipileAuthError(
                f"Failed to bind account: {resp.status_code} — {detail}"
            )
        elif resp.status_code != 200:
            detail = resp.text[:200]
            raise UnipileError(f"Failed to bind account: {resp.status_code} — {detail}")

    async def create_calendar_event(
        self,
        summary: str,
        start_datetime: str,
        end_datetime: str,
        attendee_email: str = "",
        description: str = "",
    ) -> dict[str, Any]:
        """Book a meeting on the user's connected Google Calendar.

        The backend holds the OAuth refresh token; we never see it. A 403
        means no calendar grant, and its detail carries the URL that fixes
        it, so it comes back in the ``{"success": False, "error": ...}``
        shape both callers read rather than flattened into a generic
        failure. This method was defined twice until 14 Sep 2026 and the
        older body won (heylead #356); this is the one definition.
        """
        url = f"{self.base_url}/calendar/create-event"
        payload = {
            "summary": summary,
            "start_datetime": start_datetime,
            "end_datetime": end_datetime,
            "attendee_email": attendee_email,
            "description": description,
        }
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url) from e
        self._check_rate_limit(resp)
        if resp.status_code == 401:
            raise UnipileAuthError("Backend JWT expired or invalid.")
        if resp.status_code == 403:
            detail = ""
            try:
                body = resp.json()
                detail = str(body.get("detail") or "") if isinstance(body, dict) else ""
            except ValueError:
                pass
            return {"success": False, "error": detail or "Google Calendar is not connected."}
        if resp.status_code != 200:
            raise UnipileError(
                f"Calendar booking failed: {resp.status_code} — {resp.text[:200]}"
            )
        return resp.json()

    async def unlink_account(self) -> None:
        """Remove the LinkedIn account binding for the authenticated user on the backend.

        Backend clears the user→account mapping. Does not delete the Unipile account.
        Caller should clear local account_id (e.g. unipile_account_id) after this.
        """
        url = f"{self.base_url}/api/v1/accounts/unlink"
        resp = await self._client.delete(url, headers=self._headers())
        self._check_rate_limit(resp)
        if resp.status_code == 400:
            # No account to unlink — treat as success (idempotent)
            return
        if resp.status_code not in (200, 204):
            detail = resp.text[:200]
            raise UnipileError(f"Failed to unlink account: {resp.status_code} — {detail}")

    async def get_existing_account_ids(self) -> set[str]:
        """Snapshot all current account IDs via the backend."""
        try:
            accounts = await self.list_accounts()
        except Exception:
            return set()
        ids: set[str] = set()
        for acc in accounts:
            aid = acc.get("id") or acc.get("account_id") or acc.get("accountId") or acc.get("uuid")
            if aid:
                ids.add(str(aid))
        return ids

    async def poll_for_new_account(
        self,
        known_ids: set[str],
        timeout_seconds: int = UNIPILE_POLL_TIMEOUT_SECONDS,
        interval: int = UNIPILE_POLL_INTERVAL_SECONDS,
    ) -> tuple[str | None, str]:
        """Poll for a NEW LinkedIn account that wasn't in known_ids."""
        elapsed = 0
        while elapsed < timeout_seconds:
            try:
                accounts = await self.list_accounts()
            except Exception:
                await asyncio.sleep(interval)
                elapsed += interval
                continue

            for acc in accounts:
                aid = acc.get("id") or acc.get("account_id") or acc.get("accountId") or acc.get("uuid")
                if not aid or str(aid) in known_ids:
                    continue
                provider = acc.get("provider") or acc.get("provider_type") or acc.get("type") or ""
                if "LINKEDIN" not in str(provider).upper():
                    continue
                account_id = str(aid)
                connected, status = await self.verify_account(account_id)
                if connected:
                    # Bind this account to the user on the backend.
                    # A transient bind failure must not abort the poll —
                    # LinkedIn is already connected; list_accounts and
                    # verify_account already tolerate their own hiccups.
                    try:
                        await self.bind_account(account_id)
                    except UnipileAuthError:
                        raise
                    except Exception as e:
                        logger.warning(
                            "poll_for_new_account: bind_account(%s) failed: %s — retrying",
                            account_id, e,
                        )
                        break
                    return account_id, "LinkedIn account connected!"

            await asyncio.sleep(interval)
            elapsed += interval

        return None, f"Timed out after {timeout_seconds}s waiting for LinkedIn connection."

    # ── Profile ──

    async def get_own_profile(self, account_id: str) -> dict[str, Any]:
        """Fetch the user's LinkedIn profile via the backend proxy.

        The backend returns raw Unipile data; we normalize here (same as UnipileClient).
        """
        url = f"{self.base_url}/api/v1/profile"
        try:
            resp = await self._client.get(url, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url)
        if resp.status_code == 401:
            raise UnipileAuthError()
        _raise_proxy_refusal(resp, _NO_LINKEDIN_MSG, auth_on_403=True)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and data:
            data = data[0]

        # Normalize — same logic as UnipileClient.get_own_profile
        first_name = data.get("first_name") or data.get("firstName") or ""
        last_name = data.get("last_name") or data.get("lastName") or ""
        headline = data.get("headline") or data.get("occupation") or ""
        public_id = data.get("public_identifier") or data.get("publicIdentifier") or ""
        provider_id = data.get("provider_id") or data.get("id") or ""

        # Use direct URL from Unipile if available, else construct from public_id
        profile_url = _profile_url(
            data.get("profile_url")
            or data.get("public_profile_url")
            or data.get("url")
            or "",
            public_id,
        )

        title = headline
        company = ""
        if " at " in headline:
            parts = headline.rsplit(" at ", 1)
            title = parts[0]
            company = parts[1]

        from .experience import apply_current_role, experience_from_payload
        experience = experience_from_payload(data)

        location = data.get("location") or ""
        if isinstance(location, dict):
            location = location.get("name") or location.get("default") or str(location)

        skills_raw = data.get("skills") or []
        skills: list[str] = []
        if isinstance(skills_raw, list):
            for s in skills_raw:
                if isinstance(s, str):
                    skills.append(s)
                elif isinstance(s, dict):
                    skills.append(s.get("name") or s.get("skill") or str(s))

        # Profile picture URL (for photo backup before replacement)
        profile_picture_url = (
            data.get("profile_picture_url")
            or data.get("picture_url")
            or data.get("displayPictureUrl")
            or ""
        )

        profile = {
            "name": f"{first_name} {last_name}".strip(),
            "first_name": first_name,
            "last_name": last_name,
            "headline": headline,
            "title": title,
            "company": company,
            "location": location,
            "summary": data.get("summary") or data.get("about") or "",
            "industry": data.get("industry") or "",
            "public_id": public_id,
            "provider_id": str(provider_id),
            "profile_url": profile_url,
            "profile_picture_url": profile_picture_url,
            "connections": data.get("connections_count") or data.get("network_info", {}).get("connections_count", 0),
            "skills": skills,
            "experience": experience if isinstance(experience, list) else [],
            "posts": [],
        }
        if not profile["experience"]:
            identifier = public_id or str(provider_id)
            full = await self._own_profile_sections(account_id, identifier)
            if full.get("experience"):
                profile["experience"] = full["experience"]
            if full.get("summary"):
                profile["summary"] = full["summary"]
            if full.get("skills"):
                profile["skills"] = full["skills"]
            if full.get("headline"):
                profile["headline"] = full["headline"]
        return apply_current_role(profile)

    async def _own_profile_sections(
        self, account_id: str, identifier: str,
    ) -> dict[str, Any]:
        """/users/me has no sections. Hosted /profile is that same thin payload.

        The dated roles are on Unipile GET /users/{id}?linkedin_sections=*.
        The backend proxy does not forward that param, so when Unipile
        credentials exist we ask Unipile directly.
        """
        if not identifier:
            return {}
        from ..config import get_unipile_config
        api_url, api_key = get_unipile_config()
        if not api_url or not api_key:
            return {}
        from .unipile import UnipileClient
        uni = UnipileClient(api_url, api_key)
        try:
            return await uni.get_profile(account_id, identifier)
        except Exception as e:
            logger.warning("own profile sections via Unipile failed: %s", e)
            return {}
        finally:
            await uni.close()

    async def get_posts(
        self, account_id: str, provider_id: str = "", limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Fetch recent LinkedIn posts via the backend proxy.

        Same shape as UnipileClient.get_posts, reshare flag included — a
        hosted account fetches through here, so a fix in only one of the two
        would leave every hosted user's voice signature built on a mixture of
        their own posts and other people's.
        """
        params = {"limit": str(limit)}
        if provider_id:
            params["provider_id"] = provider_id

        url = f"{self.base_url}/api/v1/posts"
        try:
            resp = await self._client.get(url, params=params, headers=self._headers())
            if resp.status_code in (404, 400):
                return []
            resp.raise_for_status()
            data = resp.json()

            items = []
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("items") or data.get("data") or data.get("posts") or []

            posts: list[dict[str, Any]] = []
            for item in items[:limit]:
                text = ""
                if isinstance(item, dict):
                    text = item.get("text") or item.get("body") or item.get("content") or ""
                elif isinstance(item, str):
                    text = item
                if not text:
                    continue
                urn = ""
                is_repost, original = False, ""
                if isinstance(item, dict):
                    urn = item.get("id") or item.get("urn") or item.get("social_id") or ""
                    is_repost, original = detect_reshare(item)
                posts.append({
                    "text": str(text),
                    "urn": str(urn),
                    "is_repost": is_repost,
                    "original_post_id": original,
                })
            return posts
        except Exception as e:
            logger.warning(f"Failed to fetch posts via backend: {e}")
            return []

    # ── Search ──

    async def search_people(
        self,
        account_id: str,
        keywords: str = "",
        title: str = "",
        location: str = "",
        count: int = 25,
        *,
        industry_codes: list[str] | None = None,
        location_codes: list[str] | None = None,
        title_keywords: list[str] | None = None,
        seniority: list[str] | None = None,
        company_headcount: dict[str, int] | None = None,
        company_types: list[str] | None = None,
        department_codes: list[str] | None = None,
        tenure: dict[str, int] | None = None,
        use_sales_navigator: bool = False,
        role_codes: list[str] | None = None,
        cursor: str | None = None,
        spotlight: dict[str, bool] | None = None,
        annual_revenue: dict[str, int] | None = None,
        company_headcount_growth: str | None = None,
        search_account_id: str | None = None,
        network_distance: list[int] | None = None,
        raise_on_error: bool = False,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Search LinkedIn for people via the backend proxy with structured filters.

        Args:
            raise_on_error: Accepted for interface parity with UnipileClient, which
                swallows upstream failures into an empty result unless asked not to.
                This client already raised UnipileError on every upstream failure
                before that flag existed and still does, for either value; it is
                ignored here deliberately rather than being made to change
                behaviour on the path most installs actually run.
            search_account_id: Optional override to route search through a different
                account (e.g., a premium Sales Navigator account). Must be the user's
                own account or an active pool account on the backend.
        """
        search_query = keywords
        if title and title.lower() not in keywords.lower():
            search_query = f"{title} {keywords}"

        url = f"{self.base_url}/api/v1/search"
        payload: dict[str, Any] = {
            "category": "people",
            "limit": min(count, 100 if use_sales_navigator else 50),
            "keywords": search_query or "",  # Backend requires keywords field
        }

        if use_sales_navigator:
            payload["api"] = "sales_navigator"
            if industry_codes:
                payload["industry"] = {"include": industry_codes}
            if location_codes:
                payload["location"] = {"include": location_codes}
            if role_codes:
                payload["role"] = {"include": role_codes}
            if seniority:
                # Unipile's own names, never our canonical keys (400 otherwise).
                from ..services.seniority import to_unipile_seniority
                unipile_levels = to_unipile_seniority(seniority)
                if unipile_levels:
                    payload["seniority"] = {"include": unipile_levels}
            if company_headcount:
                payload["company_headcount"] = [company_headcount]
            if company_types:
                payload["company_type"] = company_types
            if department_codes:
                payload["function"] = {"include": department_codes}
            if tenure:
                payload["tenure"] = [tenure]
            if spotlight:
                payload["spotlight"] = spotlight
            if annual_revenue:
                payload["annual_revenue"] = annual_revenue
            if company_headcount_growth:
                payload["company_headcount_growth"] = company_headcount_growth
        else:
            payload["api"] = "classic"
            if industry_codes:
                payload["industry"] = industry_codes
            if location_codes:
                payload["location"] = location_codes
            if title_keywords:
                payload["advanced_keywords"] = {
                    "title": " OR ".join(title_keywords),
                }

        if cursor:
            payload["cursor"] = cursor
        if network_distance:
            payload["network_distance"] = network_distance

        # Route through a different account for premium search
        if search_account_id:
            payload["search_account_id"] = search_account_id

        try:
            try:
                resp = await self._post(url, json=payload, headers=self._headers())
            except (httpx.ConnectError, httpx.TimeoutException) as e:
                raise _wrap_connection_error(e, self.base_url)
            self._check_rate_limit(resp)
            if resp.status_code == 401:
                raise UnipileAuthError()
            _raise_proxy_refusal(resp, _NO_LINKEDIN_MSG, auth_on_403=True)
            if resp.status_code >= 400:
                try:
                    err_body = resp.json()
                    detail = err_body.get("detail", err_body.get("message", resp.text[:200]))
                except Exception:
                    detail = resp.text[:200]
                raise UnipileError(f"Backend search failed ({resp.status_code}): {detail}")
            data = resp.json()

            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("items") or data.get("results") or data.get("data") or []
            else:
                items = []

            next_cursor = None
            if isinstance(data, dict):
                next_cursor = data.get("cursor") or data.get("next_cursor") or data.get("paging", {}).get("cursor")

            results: list[dict[str, Any]] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                name_parts = []
                if item.get("first_name") or item.get("firstName"):
                    name_parts.append(item.get("first_name") or item.get("firstName") or "")
                if item.get("last_name") or item.get("lastName"):
                    name_parts.append(item.get("last_name") or item.get("lastName") or "")
                name = " ".join(name_parts).strip() or item.get("name") or item.get("full_name") or item.get("display_name") or ""
                if not name and isinstance(item.get("profile"), dict):
                    prof = item.get("profile") or {}
                    name = prof.get("name") or " ".join(filter(None, [prof.get("first_name"), prof.get("last_name")])).strip()
                if not name:
                    continue

                headline_val = item.get("headline") or item.get("headline_text") or ""
                pub_id = item.get("public_identifier") or item.get("publicIdentifier") or item.get("public_id") or ""
                prov_id = item.get("provider_id") or item.get("id") or item.get("member_urn") or item.get("urn") or ""

                parsed_title = headline_val
                parsed_company = ""
                if " at " in headline_val:
                    parts = headline_val.rsplit(" at ", 1)
                    parsed_title = parts[0]
                    parsed_company = parts[1]

                loc = item.get("location") or ""
                if isinstance(loc, dict):
                    loc = loc.get("name") or loc.get("default") or loc.get("display_name") or ""

                profile_url = _profile_url(
                    item.get("profile_url") or item.get("public_profile_url") or item.get("url") or "",
                    pub_id,
                )

                parsed: dict[str, Any] = {
                    "name": name,
                    "title": parsed_title,
                    "company": parsed_company,
                    "headline": headline_val,
                    "location": str(loc),
                    "linkedin_url": profile_url,
                    "public_id": pub_id,
                    "provider_id": str(prov_id),
                }
                # Carried only when the transport reports them — absence means
                # unknown, so the flag backfill still heals these rows. Hosted
                # mode is the recommended path; dropping the flags here left
                # the free tier's InMail selector permanently unpopulated.
                if "is_open_profile" in item:
                    parsed["is_open_profile"] = bool(item["is_open_profile"])
                if "is_premium" in item:
                    parsed["is_premium"] = bool(item["is_premium"])
                results.append(parsed)
            if items and not results:
                logger.warning(
                    "Backend returned %s items but none could be parsed. Sample keys: %s",
                    len(items),
                    list(items[0].keys()) if items else [],
                )
                raise UnipileResultFormatError(
                    "Backend search returned results in an unexpected format "
                    f"({len(items)} items, none usable). "
                    "Check heylead-api and Unipile response shape (expected first_name/last_name or name)."
                )
            return results, next_cursor
        except UnipileAuthError:
            raise
        except UnipileError:
            raise
        except Exception as e:
            logger.warning(f"Backend search error: {e}", exc_info=True)
            raise UnipileError(f"LinkedIn search failed: {e}") from e

    # Hosted /check-sales-nav composes LinkedIn About + override. A False
    # from that route is already a verdict — do not "confirm" it with an
    # SN search 403 (that 403 must not downgrade the seat).
    trusts_composed_tier = True

    async def detect_sales_navigator(self, account_id: str) -> bool:
        """Check if the LinkedIn account has Sales Navigator via backend.

        Raises UnipileError when the backend cannot deliver a verdict, so a
        transient outage is never persisted as a confirmed downgrade. Only a
        200 with an explicit boolean is an answer.
        """
        url = f"{self.base_url}/api/v1/check-sales-nav"
        params: dict[str, str] = {}
        if account_id:
            params["account_id"] = account_id
        try:
            resp = await self._client.get(url, params=params, headers=self._headers())
            status = resp.status_code
            data = _ensure_dict(resp.json()) if status == 200 else {}
        except Exception as e:
            raise UnipileError(f"Sales Navigator probe failed: {e}") from e
        if status != 200:
            raise UnipileError(
                f"Sales Navigator probe returned {status} — no verdict"
            )
        return bool(data.get("has_sales_navigator", False))

    async def account_entitlements(self, account_id: str) -> dict[str, Any] | None:
        """Which LinkedIn products this account holds, via the backend.

        Mirrors ``UnipileClient.account_entitlements`` field for field so the
        hosted path stops being a special case — including the tri-state:
        None means "the payload never named this product", which is unknown
        and never a denial.

        That distinction is the whole point of this method. Without it,
        ``has_linkedin_premium`` was never written on a hosted install and was
        then read as False, so a premium account's InMail credits stayed
        unreachable (fixed client-side in v0.10.230). The backend sources all
        three products from ``/users/me``; an older deploy returns the seat
        alone, and premium then reads unknown rather than absent, leaving one
        attempt to settle it instead of a silent refusal.

        Returns None when the read itself could not happen — non-200 (the
        backend answers 502 when the seat has no verdict), a transport
        failure, or a payload naming no product at all.
        """
        url = f"{self.base_url}/api/v1/check-sales-nav"
        params: dict[str, str] = {}
        if account_id:
            params["account_id"] = account_id
        try:
            resp = await self._client.get(url, params=params, headers=self._headers())
            if resp.status_code != 200:
                logger.debug("entitlement read status=%d", resp.status_code)
                return None
            data = _ensure_dict(resp.json())
        except Exception as e:
            logger.debug("entitlement read failed: %s", e)
            return None

        # The seat travels under the backend's own name; the other two keep
        # Unipile's.
        fields = {
            "premium": "premium",
            "sales_navigator": "has_sales_navigator",
            "recruiter": "recruiter",
        }
        if not any(key in data for key in fields.values()):
            logger.debug("entitlement read named no products: %s", list(data)[:6])
            return None
        return {
            product: (None if data.get(key) is None else bool(data[key]))
            for product, key in fields.items()
        }

    async def check_sales_nav_all(self) -> list[dict[str, Any]]:
        """Check Sales Navigator status for all accessible accounts (own + pool).

        Returns list of {account_id, name, has_sales_navigator} dicts.

        "Own + pool" was always the intended contract and is now what the
        route actually answers: the caller's own seat, plus active pool seats
        only while the caller is a pool member. A non-member gets its own seat
        alone — a shorter list, not an error — so both callers (the search
        account resolver and switch_account's Sales Nav badge) degrade to the
        own-seat answer without a code change.
        """
        url = f"{self.base_url}/api/v1/check-sales-nav-all"
        try:
            resp = await self._client.get(url, headers=self._headers())
            if resp.status_code != 200:
                return []
            data = _ensure_dict(resp.json())
            return data.get("accounts", [])
        except Exception:
            return []

    async def pick_search_account(self) -> dict[str, Any]:
        """Least-used Premium/SN seat lent by the hosted pool, when it lends.

        The pool is reciprocal, so it lends a pooled Sales Navigator or
        Premium seat only to workspaces that contribute one. A non-contributor
        gets ``{}`` here and the resolver falls through to its own cached
        detection on the sending account, which is the correct outcome and not
        an error — so this stays a soft empty dict rather than raising.
        """
        url = f"{self.base_url}/api/v1/search-account"
        try:
            resp = await self._client.get(url, headers=self._headers())
            if resp.status_code != 200:
                return {}
            return _ensure_dict(resp.json())
        except Exception:
            return {}

    async def get_profile(
        self,
        account_id: str,
        identifier: str,
        use_sales_navigator: bool = False,
        raise_on_rate_limit: bool = False,
        raise_on_invalid: bool = False,
    ) -> dict[str, Any]:
        """Fetch full LinkedIn profile via backend proxy.

        ``raise_on_invalid`` surfaces a 422 as UnipileInvalidRecipientError
        instead of an empty dict. The default stays soft because eight callers
        rely on it, but a caller that keeps a queue needs to tell "no such
        profile" apart from "this identifier is not resolvable": without it the
        profile backfill re-asked the same dead identifiers every 15 minutes
        for ever, and two attempts to stop it never fired because the error was
        swallowed here, one frame below where they were watching.

        ``raise_on_rate_limit`` mirrors UnipileClient.get_profile: with it set,
        a 429 that survives _retry_request's own backoff reaches the caller as
        UnipileRateLimitError instead of becoming an empty dict.

        There is deliberately no ``resp.status_code == 429`` check below.
        _retry_request never hands a 429 back as a response — it retries and
        then raises — so such a check would be a branch that cannot run. The
        ``except UnipileRateLimitError`` clause is the whole rate-limit path;
        test_retry_request_never_hands_a_429_back_to_its_caller pins that.
        """
        url = f"{self.base_url}/api/v1/linkedin/users/{identifier}"
        params: dict[str, str] = {}
        if use_sales_navigator:
            params["linkedin_api"] = "sales_navigator"
        try:
            resp = await self._retry_request("GET", url, params=params)
            if resp.status_code in (404, 400):
                return {}
            if resp.status_code == 422 and raise_on_invalid:
                raise UnipileInvalidRecipientError(
                    f"LinkedIn will not resolve {identifier[:40]}")
            if resp.status_code in (401, 403):
                raise UnipileAuthError()
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list) and data:
                data = data[0]
            if not isinstance(data, dict):
                # UnipileClient.get_profile has always ended this way. Without
                # the same line here a decoded str reached the callers, and the
                # profile backfill wrote it into global_contacts.profile_json
                # verbatim — 1046 rows holding a redirect page.
                logger.warning(
                    "get_profile: %s payload for identifier=%s, not a profile",
                    type(data).__name__, identifier[:20],
                )
                return {}
            from .profile_normalize import normalize_linkedin_profile
            return normalize_linkedin_profile(data)
        except UnipileAuthError:
            raise
        except UnipileInvalidRecipientError:
            raise
        except UnipileRateLimitError:
            if raise_on_rate_limit:
                raise
            logger.warning("Profile fetch rate limited for %s", identifier[:20])
            return {}
        except Exception as e:
            logger.warning(f"Profile fetch error: {e}")
            return {}

    async def check_existing_relation(
        self,
        account_id: str,
        provider_id: str,
    ) -> dict[str, Any]:
        """Check existing relation with a prospect via backend."""
        result = {"connected": False, "pending_invite": False, "has_chat": False, "chat_id": ""}
        try:
            chat_id = await self.find_chat_for_user(account_id, provider_id)
            if chat_id:
                result["has_chat"] = True
                result["chat_id"] = chat_id
                result["connected"] = True
        except Exception as e:
            logger.debug(f"Relation check skipped: {e}")
        return result

    # ── Messaging ──

    async def send_invitation(
        self,
        account_id: str,
        provider_id: str,
        message: str = "",
    ) -> dict[str, Any]:
        """Send a LinkedIn connection invitation via the backend proxy."""
        result: dict[str, Any] = {"success": False, "error": "", "blocked": False, "auth_error": False}

        url = f"{self.base_url}/api/v1/invite"
        payload: dict[str, Any] = {"provider_id": provider_id}
        if message:
            message = prepare_outbound_text(message, kind="invite")
            violations = check_message(message)
            if violations:
                result["error"] = f"Guardrail: message {', '.join(violations)}"
                logger.warning("send_invitation blocked by guardrail: %s provider_id=%s",
                               "; ".join(violations), provider_id)
                return result
            from ..tier import get_caps
            note_max = (await get_caps()).invite_note_max_chars
            payload["message"] = message[:note_max]

        try:
            resp = await self._retry_request("POST", url, json=payload)
            if resp.status_code == 401:
                result["error"] = "Backend JWT expired."
                result["auth_error"] = True
                result["blocked"] = True
                return result
            refusal = _proxy_refusal_message(resp)
            if refusal is not None:
                result["error"] = refusal
                if resp.status_code == 400:
                    result["blocked"] = True
                return result

            resp.raise_for_status()
            data = _ensure_dict(resp.json())
            _classify_nested_response(data, result)

        except (httpx.TimeoutException, httpx.ConnectError) as e:
            result["error"] = str(_wrap_connection_error(e, self.base_url))
            result["blocked"] = True
        except UnipileRateLimitError as e:
            # blocked=True keeps the outreach pending; without it generate_send
            # treats a throttle as terminal and drops the prospect.
            result["error"] = f"Rate limited. Will retry later. {e}"
            result["blocked"] = True
        except Exception as e:
            result["error"] = f"Send failed: {e}"

        return result

    async def get_chats(
        self,
        account_id: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Fetch recent LinkedIn messages via the backend proxy.

        The Unipile GET /api/v1/chats endpoint returns chat metadata without
        embedded messages. For chats missing message content, we fetch the
        last message via GET /api/v1/chats/{id}/messages in parallel.
        """
        url = f"{self.base_url}/api/v1/chats"
        params = {"limit": str(limit)}

        try:
            try:
                resp = await self._retry_request("GET", url, params=params)
            except (httpx.ConnectError, httpx.TimeoutException) as e:
                raise _wrap_connection_error(e, self.base_url)
            if resp.status_code == 401:
                raise UnipileAuthError()
            _raise_proxy_refusal(resp, auth_on_403=True)
            resp.raise_for_status()
            data = resp.json()

            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("items") or data.get("chats") or data.get("data") or []
            else:
                return []

            # Normalize chat items — extract embedded messages or fetch them
            messages: list[dict[str, Any]] = []

            for chat in items:
                if not isinstance(chat, dict):
                    continue
                chat_id = chat.get("id") or chat.get("chat_id") or ""
                if not chat_id:
                    continue

                chat_messages = chat.get("messages") or chat.get("last_messages") or []
                if isinstance(chat_messages, list) and chat_messages:
                    last_msg = chat_messages[-1] if isinstance(chat_messages[-1], dict) else {}
                elif isinstance(chat_messages, dict):
                    last_msg = chat_messages
                else:
                    last_msg = {}

                text = last_msg.get("text") or last_msg.get("body") or last_msg.get("content") or ""
                # Extract URLs from attachments (LinkedIn link previews may not be in text)
                for att in (last_msg.get("attachments") or []):
                    if isinstance(att, dict):
                        att_url = att.get("url") or att.get("link") or att.get("href") or ""
                        if att_url and att_url not in text:
                            text = f"{text} {att_url}".strip()
                if not text:
                    # No embedded message — return lightweight entry using
                    # attendee_provider_id so check_replies can match contacts
                    # and selectively fetch only relevant chats.
                    att_pid = chat.get("attendee_provider_id") or ""
                    attendee_ids = []
                    for att in (chat.get("attendees") or []):
                        if isinstance(att, dict):
                            att_id = att.get("provider_id") or att.get("id") or ""
                            if att_id:
                                attendee_ids.append(str(att_id))
                    if att_pid and str(att_pid) not in attendee_ids:
                        attendee_ids.append(str(att_pid))

                    messages.append({
                        "sender_name": "",
                        "sender_id": "",
                        "text": "",
                        "timestamp": _parse_timestamp(
                            chat.get("timestamp") or chat.get("last_activity") or ""
                        ),
                        "conversation_urn": str(chat_id),
                        "attendee_ids": attendee_ids,
                        "_needs_fetch": True,  # signal to caller
                    })
                    continue

                sender_id = last_msg.get("sender_id") or last_msg.get("sender", {}).get("provider_id", "")
                sender_name = last_msg.get("sender_name") or last_msg.get("sender", {}).get("display_name", "")

                attendees = chat.get("attendees") or []
                if not sender_name:
                    for att in attendees:
                        if isinstance(att, dict):
                            att_id = att.get("provider_id") or att.get("id") or ""
                            if att_id and att_id == sender_id:
                                sender_name = att.get("display_name") or att.get("name") or ""
                                break
                            elif not sender_name:
                                sender_name = att.get("display_name") or att.get("name") or ""

                # Extract attendee provider_ids for reply matching
                attendee_ids = []
                for att in attendees:
                    if isinstance(att, dict):
                        att_id = att.get("provider_id") or att.get("id") or ""
                        if att_id:
                            attendee_ids.append(str(att_id))
                # Fallback: Unipile returns attendee_provider_id as a flat field
                att_pid = chat.get("attendee_provider_id") or ""
                if att_pid and str(att_pid) not in attendee_ids:
                    attendee_ids.append(str(att_pid))

                ts_raw = last_msg.get("timestamp") or last_msg.get("created_at") or last_msg.get("date") or ""
                timestamp = _parse_timestamp(ts_raw)

                messages.append({
                    "sender_name": sender_name,
                    "sender_id": str(sender_id),
                    "text": text,
                    "timestamp": timestamp,
                    "conversation_urn": str(chat_id),
                    "attendee_ids": attendee_ids,
                })

            # NOTE: Chats without embedded messages are returned as
            # lightweight entries with _needs_fetch=True and attendee_ids.
            # The caller (check_replies) matches these to contacts first,
            # then selectively fetches only relevant chats via
            # get_chat_messages(). This avoids 100+ API calls that would
            # hit the rate limit.

            return messages
        except UnipileAuthError:
            raise
        except Exception as e:
            logger.warning(f"Failed to fetch chats via backend: {e}")
            return []

    async def get_chats_with_replies(
        self,
        account_id: str,
        provider_ids: list[str],
        our_provider_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch chats with prospect replies in a single server-side call.

        The backend fetches all chats, matches attendees to provided
        provider_ids, fetches messages for matched chats, and returns
        only chats where the most recent message is from the prospect.
        """
        url = f"{self.base_url}/api/v1/chats/with-replies"
        body = {
            "provider_ids": provider_ids,
            "our_provider_id": our_provider_id,
            "limit": limit,
        }
        try:
            resp = await self._retry_request("POST", url, json=body)
            if resp.status_code in (401, 403):
                raise UnipileAuthError()
            resp.raise_for_status()
            data = _ensure_dict(resp.json())
            return data.get("replies") or []
        except UnipileAuthError:
            raise
        except Exception as e:
            logger.warning(f"Failed to fetch chats with replies via backend: {e}")
            return []

    # ── DM Messaging ──

    async def send_message(
        self,
        account_id: str,
        chat_id: str,
        text: str,
    ) -> dict[str, Any]:
        """Send a DM message in an existing LinkedIn chat via the backend proxy."""
        result: dict[str, Any] = {"success": False, "error": ""}
        text = prepare_outbound_text(text, kind="dm")
        violations = check_message(text)
        if violations:
            result["error"] = f"Guardrail: message {', '.join(violations)}"
            logger.warning("send_message blocked by guardrail: %s chat_id=%s",
                           "; ".join(violations), chat_id)
            return result
        url = f"{self.base_url}/api/v1/message"
        payload = {"chat_id": chat_id, "text": text}
        try:
            resp = await self._retry_request("POST", url, json=payload, no_retry=True)
            from ..ops_log import log_outbound_send, message_hash
            logger.info(
                "send_message chat_id=%s http_status=%d text_hash=%s text_len=%d",
                chat_id[:30], resp.status_code, message_hash(text), len(text or ""),
            )
            log_outbound_send(
                "result",
                channel="dm",
                chat_id=chat_id,
                text=text,
                http_status=resp.status_code,
                success=resp.status_code < 400,
            )
            if resp.status_code == 401:
                result["error"] = "Backend JWT expired."
                return result
            refusal = _proxy_refusal_message(resp)
            if refusal is not None:
                result["error"] = refusal
                return result
            resp.raise_for_status()
            data = _ensure_dict(resp.json())
            _classify_nested_response(data, result)
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            result["error"] = str(_wrap_connection_error(e, self.base_url))
        except Exception as e:
            result["error"] = f"Send failed: {e}"
        if not result["success"]:
            logger.warning("send_message failed: %s", result)
        return result

    async def send_new_message(
        self,
        account_id: str,
        provider_id: str,
        text: str,
    ) -> dict[str, Any]:
        """Start a new DM conversation with a connected LinkedIn user via backend proxy.

        Creates a new chat and sends the first message. Use when no existing
        chat exists (e.g., first follow-up after connection acceptance).

        Args:
            account_id: The user's Unipile account ID.
            provider_id: LinkedIn provider_id of the recipient.
            text: The message text to send.

        Returns:
            {"success": bool, "error": str, "chat_id": str}
        """
        result: dict[str, Any] = {"success": False, "error": "", "chat_id": ""}
        text = prepare_outbound_text(text, kind="dm")
        violations = check_message(text)
        if violations:
            result["error"] = f"Guardrail: message {', '.join(violations)}"
            logger.warning("send_new_message blocked by guardrail: %s provider_id=%s",
                           "; ".join(violations), provider_id)
            return result
        url = f"{self.base_url}/api/v1/chats"
        payload = {"attendees_ids": [provider_id], "text": text}
        try:
            resp = await self._retry_request("POST", url, json=payload, no_retry=True)
            from ..ops_log import log_outbound_send, message_hash
            logger.info(
                "send_new_message provider_id=%s http_status=%d text_hash=%s text_len=%d",
                provider_id[:30], resp.status_code, message_hash(text), len(text or ""),
            )
            log_outbound_send(
                "result",
                channel="dm",
                provider_id=provider_id,
                text=text,
                http_status=resp.status_code,
                success=resp.status_code < 400,
            )
            if resp.status_code == 401:
                result["error"] = "Backend JWT expired."
                return result
            refusal = _proxy_refusal_message(resp)
            if refusal is not None:
                result["error"] = refusal
                return result
            resp.raise_for_status()
            data = _ensure_dict(resp.json())
            _classify_nested_response(data, result)
            if result["success"]:
                result["chat_id"] = data.get("chat_id") or data.get("id") or ""
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            result["error"] = str(_wrap_connection_error(e, self.base_url))
        except Exception as e:
            result["error"] = f"Send new message failed: {e}"
        if not result["success"]:
            logger.warning("send_new_message failed: %s", result)
        return result

    # ── Voice Messages ──

    async def send_voice_message(
        self,
        account_id: str,
        chat_id: str,
        audio_path: str,
        text: str = "",
    ) -> dict[str, Any]:
        """Send a voice message in an existing LinkedIn chat via backend proxy.

        Reads the audio file, base64-encodes it, and sends to the backend
        ``POST /api/v1/voice/send`` endpoint which decodes and forwards to
        Unipile as multipart/form-data.

        Returns:
            {"success": bool, "error": str}
        """
        import base64

        result: dict[str, Any] = {"success": False, "error": ""}
        url = f"{self.base_url}/api/v1/voice/send"
        try:
            with open(audio_path, "rb") as f:
                audio_b64 = base64.b64encode(f.read()).decode()
        except FileNotFoundError:
            result["error"] = f"Audio file not found: {audio_path}"
            return result
        payload = {
            "chat_id": chat_id,
            "audio_base64": audio_b64,
            "text": text,
        }
        try:
            resp = await self._retry_request("POST", url, json=payload, no_retry=True)
            if resp.status_code == 401:
                result["error"] = "Backend JWT expired."
                return result
            refusal = _proxy_refusal_message(resp)
            if refusal is not None:
                result["error"] = refusal
                return result
            resp.raise_for_status()
            data = _ensure_dict(resp.json())
            _classify_nested_response(data, result)
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            result["error"] = str(_wrap_connection_error(e, self.base_url))
        except Exception as e:
            result["error"] = f"Voice send failed: {e}"
        return result

    async def send_new_voice_message(
        self,
        account_id: str,
        provider_id: str,
        audio_path: str,
        text: str = "",
    ) -> dict[str, Any]:
        """Start a new DM with a voice message via backend proxy.

        Returns:
            {"success": bool, "error": str, "chat_id": str}
        """
        import base64

        result: dict[str, Any] = {"success": False, "error": "", "chat_id": ""}
        url = f"{self.base_url}/api/v1/voice/send"
        try:
            with open(audio_path, "rb") as f:
                audio_b64 = base64.b64encode(f.read()).decode()
        except FileNotFoundError:
            result["error"] = f"Audio file not found: {audio_path}"
            return result
        payload = {
            "provider_id": provider_id,
            "audio_base64": audio_b64,
            "text": text,
        }
        try:
            resp = await self._retry_request("POST", url, json=payload, no_retry=True)
            if resp.status_code == 401:
                result["error"] = "Backend JWT expired."
                return result
            refusal = _proxy_refusal_message(resp)
            if refusal is not None:
                result["error"] = refusal
                return result
            resp.raise_for_status()
            data = _ensure_dict(resp.json())
            _classify_nested_response(data, result)
            if result["success"]:
                result["chat_id"] = data.get("chat_id") or ""
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            result["error"] = str(_wrap_connection_error(e, self.base_url))
        except Exception as e:
            result["error"] = f"Send new voice message failed: {e}"
        return result

    async def get_chat_messages(
        self,
        account_id: str,
        chat_id: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Fetch messages from a specific chat via the backend proxy."""
        url = f"{self.base_url}/api/v1/chats/{chat_id}/messages"
        params = {"limit": str(limit)}
        try:
            try:
                resp = await self._retry_request("GET", url, params=params)
            except (httpx.ConnectError, httpx.TimeoutException) as e:
                raise _wrap_connection_error(e, self.base_url)
            if resp.status_code == 401:
                raise UnipileAuthError()
            _raise_proxy_refusal(resp, auth_on_403=True)
            resp.raise_for_status()
            data = resp.json()

            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("items") or data.get("messages") or data.get("data") or []
            else:
                return []

            messages: list[dict[str, Any]] = []
            for msg in items[:limit]:
                if not isinstance(msg, dict):
                    continue
                text = msg.get("text") or msg.get("body") or msg.get("content") or ""
                sender_id = msg.get("sender_id") or msg.get("sender", {}).get("provider_id", "")
                sender_name = msg.get("sender_name") or msg.get("sender", {}).get("display_name", "")
                ts_raw = msg.get("timestamp") or msg.get("created_at") or msg.get("date") or ""
                timestamp = 0
                if isinstance(ts_raw, (int, float)):
                    timestamp = int(ts_raw)
                elif isinstance(ts_raw, str) and ts_raw:
                    try:
                        from datetime import datetime
                        timestamp = int(datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp())
                    except (ValueError, TypeError):
                        pass
                message_id = str(msg.get("id") or msg.get("message_id") or "")
                messages.append({
                    "message_id": message_id,
                    "sender_id": str(sender_id),
                    "sender_name": str(sender_name),
                    "text": text,
                    "timestamp": timestamp,
                })
            return messages
        except UnipileAuthError:
            raise
        except Exception as e:
            logger.warning(f"Failed to fetch chat messages via backend: {e}")
            return []

    async def verify_message_sent(
        self,
        account_id: str,
        chat_id: str,
        expected_text: str,
    ) -> bool:
        """Read back conversation to verify our message was actually delivered.

        Checks the most recent messages in the chat for a match against
        the expected text (first 50 chars). Returns True if found.
        """
        try:
            messages = await self.get_chat_messages(account_id, chat_id, limit=3)
            snippet = expected_text[:50]
            for msg in messages:
                if snippet in (msg.get("text") or ""):
                    return True
            return False
        except Exception as e:
            logger.warning("DM verification failed for chat %s: %s", chat_id, e)
            return False

    # ── User Posts (for any user) ──

    async def get_followers(
        self,
        account_id: str,
        limit: int = 100,
        company_id: str = "",
    ) -> list[dict[str, Any]]:
        """Fetch followers via the backend proxy.

        If company_id is provided, fetches company page followers via Voyager.
        Otherwise falls back to the legacy user followers endpoint.
        """
        if company_id:
            url = f"{self.base_url}/api/v1/linkedin/company/{company_id}/followers"
            params = {"count": str(limit)}
        else:
            url = f"{self.base_url}/api/v1/users/followers"
            params = {"limit": str(limit)}
        try:
            resp = await self._retry_request("GET", url, params=params)
            if resp.status_code in (404, 400):
                logger.warning(
                    "get_followers: status=%d company_id=%s body=%s",
                    resp.status_code, company_id or "personal",
                    str(resp.text)[:300],
                )
                return []
            resp.raise_for_status()
            data = resp.json()

            # Check for voyager error forwarded by backend
            voyager_status = data.get("_status_code") if isinstance(data, dict) else None
            if voyager_status and voyager_status not in (200, 201):
                logger.warning(
                    "get_followers: voyager_status=%s company_id=%s",
                    voyager_status, company_id or "personal",
                )

            items = []
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("items") or data.get("data") or data.get("followers") or []

            followers: list[dict[str, Any]] = []
            for item in items[:limit]:
                if not isinstance(item, dict):
                    continue
                provider_id = item.get("provider_id") or item.get("id") or ""
                name = item.get("name") or item.get("full_name") or ""
                if not provider_id and not name:
                    continue
                followers.append({
                    "provider_id": str(provider_id),
                    "name": str(name),
                    "headline": str(item.get("headline") or ""),
                    "profile_url": str(item.get("profile_url") or item.get("public_identifier") or ""),
                })
            logger.info(
                "get_followers: found %d followers for company_id=%s",
                len(followers), company_id or "personal",
            )
            return followers
        except UnipileError:
            # Re-raise rate limit / auth errors so circuit breaker can see them
            raise
        except Exception as e:
            logger.warning("Failed to fetch followers via backend: %s (company_id=%s)", e, company_id or "personal")
            return []

    async def get_company_feed(
        self,
        account_id: str,
        company_id: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Fetch company page posts via the Voyager company feed endpoint.

        Args:
            account_id: Unipile account ID.
            company_id: LinkedIn company numeric ID.
            limit: Max posts to return.

        Returns:
            List of post dicts with id, social_id, text, likes, comments.
        """
        url = f"{self.base_url}/api/v1/linkedin/company/{company_id}/feed"
        params = {"count": str(limit)}
        try:
            resp = await self._retry_request("GET", url, params=params)
            if resp.status_code in (404, 400):
                logger.warning(
                    "get_company_feed: status=%d company=%s body=%s",
                    resp.status_code, company_id, str(resp.text)[:300],
                )
                return []
            resp.raise_for_status()
            data = _ensure_dict(resp.json())

            posts = data.get("posts", [])
            voyager_status = data.get("_status_code") if isinstance(data, dict) else None
            # 200/201 are success — the backend forwards Unipile's status
            # inside its own 200. Treating a truthy code as an error discarded
            # every healthy feed (siblings get_followers / get_user_posts
            # already exempt 200/201).
            if voyager_status and voyager_status not in (200, 201):
                logger.warning(
                    "get_company_feed: voyager_status=%s company=%s",
                    voyager_status, company_id,
                )
                return []
            logger.info("get_company_feed: found %d posts for company=%s", len(posts) if isinstance(posts, list) else 0, company_id)
            return posts if isinstance(posts, list) else []
        except Exception as e:
            logger.warning("Failed to fetch company feed via backend: %s (company=%s)", e, company_id)
            return []

    async def get_user_posts(
        self,
        account_id: str,
        identifier: str,
        limit: int = 10,
        raise_on_rate_limit: bool = False,
    ) -> list[dict[str, Any]]:
        """Fetch recent posts for any LinkedIn user via the backend proxy.

        Returns a list of post dicts. If the Unipile API returned a non-200
        status (e.g. 422), the list will be empty but a ``_status_code`` key
        will be present on the *first* element as a sentinel so callers can
        detect the error and fall back to ``search_posts``.

        ``raise_on_rate_limit`` surfaces a 429 — whether _retry_request raises
        it after exhausting its own backoff, or the proxy reports it as an
        upstream ``_status_code`` in the body — as UnipileRateLimitError. Off by
        default, so the sentinel behaviour every other caller relies on is
        unchanged.

        As in get_profile, there is no ``resp.status_code == 429`` check:
        _retry_request raises on a surviving 429 rather than returning it, so
        that branch could never run.
        """
        url = f"{self.base_url}/api/v1/users/{identifier}/posts"
        params = {"limit": str(limit)}
        try:
            resp = await self._retry_request("GET", url, params=params)
            if resp.status_code in (404, 400):
                return []
            resp.raise_for_status()
            data = resp.json()

            # Detect _status_code from backend (Unipile returned non-200)
            upstream_status = None
            items = []
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                upstream_status = data.get("_status_code")
                items = data.get("items") or data.get("data") or data.get("posts") or []

            # If backend flagged an upstream error (e.g. 422), return a
            # sentinel so callers can detect and fall back to search_posts.
            if upstream_status and upstream_status not in (200, 201):
                if raise_on_rate_limit and str(upstream_status) == "429":
                    raise UnipileRateLimitError(
                        "Rate limited. Try again in a few minutes.",
                        retry_after=None,
                    )
                return [{"_status_code": upstream_status}]

            posts: list[dict[str, Any]] = []
            for item in items[:limit]:
                if not isinstance(item, dict):
                    continue
                text = item.get("text") or item.get("body") or item.get("content") or ""
                if not text:
                    continue
                post_id = item.get("id") or item.get("urn") or item.get("social_id") or ""
                post_date = item.get("date") or item.get("created_at") or item.get("parsed_datetime") or ""
                metrics = item.get("metrics") or item.get("social_counts") or {}
                if not isinstance(metrics, dict):
                    metrics = {}

                # Merge top-level counters into metrics
                for counter_key, metric_key in [
                    ("reaction_counter", "reactions_count"),
                    ("comment_counter", "comments_count"),
                    ("impressions_counter", "impressions_count"),
                    ("repost_counter", "reposts_count"),
                ]:
                    val = item.get(counter_key) or item.get(metric_key)
                    if val and metric_key not in metrics:
                        metrics[metric_key] = val

                is_repost, original = detect_reshare(item)
                post_data: dict[str, Any] = {
                    "id": str(post_id),
                    "text": str(text),
                    "date": str(post_date),
                    "metrics": metrics,
                    "share_url": item.get("share_url") or "",
                    "is_repost": is_repost,
                    "original_post_id": original,
                    "is_edited": bool(item.get("is_edited")),
                    "visibility": item.get("visibility") or "",
                }

                # Forward raw fields for expanded extraction downstream
                for fwd_key in ("image", "images", "video", "video_url", "article",
                                "article_url", "document", "document_url", "poll",
                                "reshared", "repost", "shared_post", "original_post_id",
                                "attachments"):
                    if item.get(fwd_key) is not None:
                        post_data[fwd_key] = item[fwd_key]

                posts.append(post_data)
            return posts
        except UnipileRateLimitError:
            if raise_on_rate_limit:
                raise
            logger.warning("User posts fetch rate limited for %s", identifier[:20])
            return []
        except Exception as e:
            logger.warning(f"Failed to fetch user posts via backend: {e}")
            return []

    # ── Post Engagement ──

    async def send_post_comment(
        self,
        account_id: str,
        post_id: str,
        text: str,
        outreach_id: str = "",
        post_text: str = "",
    ) -> dict[str, Any]:
        """Comment on a LinkedIn post via the backend proxy.

        A bare numeric id is addressed as ``activity`` first, then ``ugcPost``
        on the one safe error (422 ``errors/invalid_post``). Timeouts and 5xx
        are not retried — those may have landed the comment.
        """
        result: dict[str, Any] = {"success": False, "error": ""}
        url = f"{self.base_url}/api/v1/posts/comment"
        already_urn = (post_id or "").startswith("urn:")
        classes = (POST_URN_CLASSES[0],) if already_urn else POST_URN_CLASSES
        try:
            resp = None
            for urn_class in classes:
                payload: dict[str, str] = {
                    "post_id": _post_urn(post_id, urn_class),
                    "text": text,
                }
                if outreach_id:
                    payload["outreach_id"] = outreach_id
                if post_text:
                    payload["post_text"] = post_text[:500]
                resp = await self._retry_request(
                    "POST", url, json=payload, no_retry=True,
                )
                if resp.status_code == 401:
                    result["error"] = "Backend JWT expired."
                    return result
                status, body_text = _hosted_comment_status(resp)
                if _is_invalid_post_error(status, body_text):
                    if already_urn:
                        break
                    logger.debug(
                        "send_post_comment: %s not found as %s, trying next class",
                        post_id, urn_class,
                    )
                    continue
                if resp.status_code >= 400:
                    resp.raise_for_status()
                data = _ensure_dict(resp.json())
                _classify_nested_response(data, result, accept_codes=(200, 201, 202))
                break
            if resp is not None and not result.get("success") and not result.get("error"):
                status, body_text = _hosted_comment_status(resp)
                result["error"] = f"Unipile returned {status}: {body_text[:200]}"
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            result["error"] = str(_wrap_connection_error(e, self.base_url))
        except Exception as e:
            result["error"] = f"Comment failed: {e}"
        return result

    async def send_post_reaction(
        self,
        account_id: str,
        post_id: str,
        reaction_type: str = "LIKE",
        outreach_id: str = "",
        post_text: str = "",
    ) -> dict[str, Any]:
        """React to a LinkedIn post via the backend proxy."""
        result: dict[str, Any] = {"success": False, "error": ""}
        url = f"{self.base_url}/api/v1/posts/react"
        payload: dict[str, str] = {"post_id": post_id, "reaction_type": reaction_type}
        if outreach_id:
            payload["outreach_id"] = outreach_id
        if post_text:
            payload["post_text"] = post_text[:500]
        try:
            resp = await self._retry_request("POST", url, json=payload)
            if resp.status_code == 401:
                result["error"] = "Backend JWT expired."
                return result
            resp.raise_for_status()
            data = _ensure_dict(resp.json())
            _classify_nested_response(data, result, accept_codes=(200, 201, 202))
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            result["error"] = str(_wrap_connection_error(e, self.base_url))
        except Exception as e:
            result["error"] = f"Reaction failed: {e}"
        return result

    # ── Relations ──

    async def get_relations(
        self,
        account_id: str,
        limit: int = 50,
        cursor: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch LinkedIn connections/relations via the backend proxy with pagination."""
        all_relations: list[dict[str, Any]] = []
        page_size = min(limit, 500)
        url = f"{self.base_url}/api/v1/relations"
        complete = True

        try:
            while len(all_relations) < limit:
                params: dict[str, str] = {"limit": str(page_size)}
                if cursor:
                    params["cursor"] = cursor

                try:
                    resp = await self._client.get(url, params=params, headers=self._headers())
                except (httpx.ConnectError, httpx.TimeoutException) as e:
                    raise _wrap_connection_error(e, self.base_url)
                if resp.status_code == 401:
                    raise UnipileAuthError()
                _raise_proxy_refusal(resp, auth_on_403=True)
                resp.raise_for_status()
                data = resp.json()

                if isinstance(data, list):
                    items = data
                elif isinstance(data, dict):
                    items = data.get("items") or data.get("relations") or data.get("data") or []
                else:
                    break

                if not items:
                    break

                for item in items:
                    if not isinstance(item, dict):
                        continue
                    provider_id = item.get("provider_id") or item.get("member_id") or item.get("id") or ""
                    name = item.get("display_name") or item.get("name") or ""
                    if not name and (item.get("first_name") or item.get("last_name")):
                        name = f"{item.get('first_name', '')} {item.get('last_name', '')}".strip()
                    headline = item.get("headline") or ""
                    public_id = item.get("public_identifier") or item.get("publicIdentifier") or ""

                    # Extract company/location/profile_url for local search
                    company = item.get("company") or item.get("company_name") or ""
                    if not company and headline and " at " in headline:
                        company = headline.rsplit(" at ", 1)[1].strip()
                    location = item.get("location") or ""
                    if isinstance(location, dict):
                        location = location.get("name") or location.get("default") or ""
                    profile_url = _profile_url(
                        item.get("profile_url") or item.get("public_profile_url") or "",
                        public_id,
                    )

                    # "Connected since" (9 Sep 2026). The backend's
                    # /relations proxy returns Unipile's UserRelation items
                    # untouched, so `created_at` (epoch milliseconds, the date
                    # of the connection) arrives here exactly as UnipileClient
                    # sees it. This mapper dropped it, so every hosted account
                    # fell back to the backfill epoch.
                    connected_at = (
                        item.get("created_at")
                        or item.get("connected_at")
                        or item.get("connection_date")
                    )

                    all_relations.append({
                        "provider_id": str(provider_id),
                        "name": name,
                        "headline": headline,
                        "public_id": public_id,
                        "company": company,
                        "location": str(location),
                        "profile_url": profile_url,
                        "connected_at": connected_at,
                        # Same shape as UnipileClient.get_relations.
                        "raw": item,
                    })

                cursor = data.get("cursor") if isinstance(data, dict) else None
                if not cursor:
                    n_this_page = len(items) if isinstance(items, list) else 0
                    complete = n_this_page < page_size
                    break

            return RelationsPage(all_relations[:limit], complete=complete, cursor=cursor)
        except UnipileAuthError:
            raise
        except Exception as e:
            logger.warning(f"Failed to fetch relations via backend: {e}")
            return []

    # ── InMail ──

    async def send_inmail(
        self,
        account_id: str,
        provider_id: str,
        subject: str,
        body: str,
    ) -> dict[str, Any]:
        """Send an InMail to a non-connection via the backend proxy.

        Args:
            account_id: The Unipile account ID (unused, taken from backend JWT).
            provider_id: LinkedIn provider_id of the recipient.
            subject: InMail subject line.
            body: InMail message body.

        Returns:
            {"success": bool, "error": str, "chat_id": str}
        """
        result: dict[str, Any] = {"success": False, "error": "", "chat_id": ""}
        body = prepare_outbound_text(body, kind="dm")
        violations = check_message(body)
        if violations:
            result["error"] = f"Guardrail: message {', '.join(violations)}"
            logger.warning("send_inmail blocked by guardrail: %s provider_id=%s",
                           "; ".join(violations), provider_id)
            return result
        url = f"{self.base_url}/api/v1/linkedin/inmail"
        payload = {"provider_id": provider_id, "subject": subject, "body": body}
        try:
            resp = await self._retry_request("POST", url, json=payload, no_retry=True)
            if resp.status_code == 401:
                result["error"] = "Backend JWT expired."
                return result
            resp.raise_for_status()
            data = _ensure_dict(resp.json())
            status_code = data.get("status_code", 200)
            if status_code in (200, 201):
                result["success"] = True
                inner = data.get("body", {})
                if isinstance(inner, dict):
                    result["chat_id"] = inner.get("chat_id") or inner.get("id") or ""
            elif status_code == 402:
                result["error"] = "No InMail credits remaining."
            elif status_code == 429:
                result["error"] = "Rate limited by LinkedIn."
            else:
                inner = data.get("body", data)
                result["error"] = f"Unipile returned {status_code}: {str(inner)[:200]}"
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            result["error"] = str(_wrap_connection_error(e, self.base_url))
        except Exception as e:
            result["error"] = f"InMail send failed: {e}"
        return result

    async def get_inmail_balance(
        self,
        account_id: str,
    ) -> dict[str, Any]:
        """Get InMail credit balance via the backend proxy.

        Args:
            account_id: The Unipile account ID (unused, taken from backend JWT).

        Returns:
            {"credits": int, "error": str}
        """
        result: dict[str, Any] = {"credits": -1, "error": ""}
        url = f"{self.base_url}/api/v1/linkedin/inmail-balance"
        try:
            resp = await self._client.get(url, headers=self._headers())
            if resp.status_code == 401:
                result["error"] = "Backend JWT expired."
                return result
            if resp.status_code in (200, 201):
                data = _ensure_dict(resp.json())
                # Accept either the backend's flat shape or Unipile's
                # per-product one proxied through. An unrecognised shape must
                # not fall to 0 — callers refuse on 0, so that would block
                # every InMail; -1 is the "unknown, do not refuse" value.
                flat = data.get("balance") or data.get("credits") or data.get("remaining")
                if isinstance(flat, int) and not isinstance(flat, bool):
                    result["credits"] = flat
                else:
                    from .unipile import _credits_from_balance
                    result["credits"] = _credits_from_balance(data)
            else:
                result["error"] = f"Status {resp.status_code}"
        except Exception as e:
            result["error"] = f"InMail balance check failed: {e}"
        return result

    # ── Skill Endorsement ──

    async def endorse_skill(
        self,
        account_id: str,
        identifier: str,
    ) -> dict[str, Any]:
        """Endorse a LinkedIn profile's skills via the backend proxy.

        Args:
            account_id: The Unipile account ID (unused, taken from backend JWT).
            identifier: Profile public_id or provider_id.

        Returns:
            {"success": bool, "error": str}
        """
        result: dict[str, Any] = {"success": False, "error": ""}
        url = f"{self.base_url}/api/v1/linkedin/profile/{identifier}/skill/endorse"
        try:
            resp = await self._retry_request("POST", url)
            if resp.status_code == 401:
                result["error"] = "Backend JWT expired."
                return result
            refusal = _proxy_refusal_message(resp)
            if refusal is not None:
                result["error"] = refusal
                return result
            resp.raise_for_status()
            data = _ensure_dict(resp.json())
            status_code = data.get("status_code", 200)
            if status_code in (200, 201, 204):
                result["success"] = True
            elif status_code == 404:
                result["error"] = "Profile or skills not found."
            elif status_code == 422:
                result["error"] = "Cannot endorse skills (may require connection)."
            elif status_code == 429:
                result["error"] = "Rate limited by LinkedIn."
            else:
                result["error"] = f"Unipile returned {status_code}"
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            result["error"] = str(_wrap_connection_error(e, self.base_url))
        except Exception as e:
            result["error"] = f"Skill endorsement failed: {e}"
        return result

    # ── Company Profile ──

    async def get_company_profile(
        self,
        account_id: str,
        identifier: str,
    ) -> dict[str, Any]:
        """Fetch a LinkedIn company profile via the backend proxy.

        Args:
            account_id: The Unipile account ID (unused, taken from backend JWT).
            identifier: Company public_id, provider_id, or URL slug.

        Returns:
            Company dict with name, industry, size, employee_count, etc.
        """
        url = f"{self.base_url}/api/v1/linkedin/company/{identifier}"
        try:
            resp = await self._client.get(url, headers=self._headers())
            if resp.status_code in (404, 400):
                return {}
            if resp.status_code in (401, 403):
                raise UnipileAuthError()
            resp.raise_for_status()
            data = _ensure_dict(resp.json())
            if not isinstance(data, dict):
                return {}
            return {
                "name": data.get("name") or data.get("company_name") or "",
                "industry": data.get("industry") or "",
                "size": data.get("company_size") or data.get("size") or "",
                "employee_count": data.get("employee_count") or data.get("employees_count") or 0,
                "description": data.get("description") or data.get("about") or "",
                "website": data.get("website") or data.get("url") or "",
                "headquarters": data.get("headquarters") or data.get("location") or "",
                "founded": data.get("founded") or data.get("founded_year") or "",
                "specialties": data.get("specialties") or [],
                "type": data.get("type") or data.get("company_type") or "",
                "provider_id": str(data.get("provider_id") or data.get("id") or ""),
            }
        except UnipileAuthError:
            raise
        except Exception as e:
            logger.warning(f"Company profile fetch error: {e}")
            return {}

    # ── Inbound Invitations ──

    async def get_received_invitations(
        self,
        account_id: str,
        raise_on_error: bool = False,
    ) -> list[dict[str, Any]]:
        """Fetch received (inbound) LinkedIn invitations via the backend proxy.

        Args:
            account_id: The Unipile account ID (unused, taken from backend JWT).

        Returns:
            List of invitation dicts with id, sender_name, sender_id, headline, message, timestamp.
        """
        url = f"{self.base_url}/api/v1/linkedin/invitations/received"
        try:
            resp = await self._client.get(url, headers=self._headers())
            if resp.status_code in (401, 403):
                raise UnipileAuthError()
            if resp.status_code != 200:
                if raise_on_error:
                    raise UnipileError(
                        f"get_received_invitations: HTTP {resp.status_code}"
                    )
                return []
            data = resp.json()

            # Diagnostic logging — raw response shape
            if isinstance(data, dict):
                items_key = "items" if "items" in data else ("data" if "data" in data else None)
                raw_count = len(data.get(items_key, [])) if items_key else 0
                logger.info("get_received_invitations (backend): keys=%s, items_key=%s, raw_count=%d",
                            list(data.keys()), items_key, raw_count)
            elif isinstance(data, list):
                logger.info("get_received_invitations (backend): list count=%d", len(data))

            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("items") or data.get("data") or []
            else:
                logger.warning("get_received_invitations: unexpected response type %s", type(data).__name__)
                return []

            invitations: list[dict[str, Any]] = []
            for inv in items:
                if not isinstance(inv, dict):
                    continue
                inv_id = inv.get("id") or inv.get("invitation_id") or ""
                if not inv_id:
                    continue

                # Skip non-connection invitations (newsletters, events, etc.)
                if _is_newsletter_invitation(inv):
                    logger.debug("Filtered non-connection invitation: id=%s", inv_id)
                    continue

                # Sender info — expanded field coverage + nested objects
                sender_name = inv.get("sender_name") or inv.get("display_name") or inv.get("name") or ""
                if not sender_name:
                    fn = inv.get("first_name") or inv.get("firstName") or ""
                    ln = inv.get("last_name") or inv.get("lastName") or ""
                    sender_name = f"{fn} {ln}".strip()
                sender_id = (
                    inv.get("provider_id") or inv.get("sender_id")
                    or inv.get("from_member_id") or inv.get("inviter_id")
                    or inv.get("from_id") or inv.get("member_id") or ""
                )
                # Check nested sender/inviter objects
                for nested_key in ("sender", "inviter", "from", "user"):
                    nested = inv.get(nested_key)
                    if isinstance(nested, dict):
                        sender_id = sender_id or nested.get("id") or nested.get("provider_id") or ""
                        if not sender_name:
                            sender_name = nested.get("name") or nested.get("display_name") or ""
                            if not sender_name:
                                fn = nested.get("first_name") or nested.get("firstName") or ""
                                ln = nested.get("last_name") or nested.get("lastName") or ""
                                sender_name = f"{fn} {ln}".strip()

                headline = inv.get("headline") or ""
                message = inv.get("message") or inv.get("custom_message") or ""

                ts_raw = inv.get("timestamp") or inv.get("created_at") or inv.get("date") or ""
                timestamp = 0
                if isinstance(ts_raw, (int, float)):
                    timestamp = int(ts_raw)
                elif isinstance(ts_raw, str) and ts_raw:
                    try:
                        from datetime import datetime
                        timestamp = int(datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp())
                    except (ValueError, TypeError):
                        pass
                # Extract shared_secret for acceptance
                specifics = inv.get("specifics") or {}
                shared_secret = specifics.get("shared_secret") or ""

                invitations.append({
                    "id": str(inv_id),
                    "sender_name": sender_name,
                    "sender_id": str(sender_id),
                    "headline": headline,
                    "message": message,
                    "timestamp": timestamp,
                    "shared_secret": shared_secret,
                })

            logger.info("get_received_invitations (backend): %d raw -> %d after filter",
                        len(items), len(invitations))
            return invitations
        except UnipileAuthError:
            raise
        except UnipileError:
            raise
        except Exception as e:
            status = getattr(e, "status_code", None) or getattr(e, "status", None)
            logger.warning(
                "Failed to fetch received invitations via backend: %s (%s)%s",
                e or "(empty)",
                type(e).__name__,
                f" HTTP {status}" if status else "",
            )
            if raise_on_error:
                raise UnipileError(
                    f"get_received_invitations: {type(e).__name__}: {e or '(empty)'}"
                    + (f" HTTP {status}" if status else "")
                ) from e
            return []

    async def handle_invitation(
        self,
        account_id: str,
        invitation_id: str,
        action: str = "accept",
        *,
        shared_secret: str = "",
    ) -> dict[str, Any]:
        """Accept or decline a received invitation via the backend proxy.

        Args:
            account_id: The Unipile account ID (unused, taken from backend JWT).
            invitation_id: The invitation ID.
            action: "accept" or "decline".
            shared_secret: LinkedIn shared secret from invitation specifics.

        Returns:
            {"success": bool, "error": str}
        """
        result: dict[str, Any] = {"success": False, "error": ""}
        url = f"{self.base_url}/api/v1/linkedin/invitations/received/{invitation_id}"
        payload: dict[str, Any] = {"action": action}
        if shared_secret:
            payload["shared_secret"] = shared_secret
        try:
            resp = await self._retry_request("POST", url, json=payload)
            if resp.status_code == 401:
                result["error"] = "Backend JWT expired."
                return result
            refusal = _proxy_refusal_message(resp)
            if refusal is not None:
                result["error"] = refusal
                return result
            resp.raise_for_status()
            data = _ensure_dict(resp.json())
            status_code = data.get("status_code", 200)
            if status_code in (200, 201, 204):
                result["success"] = True
            elif status_code == 404:
                result["error"] = "Invitation not found or already handled."
            else:
                result["error"] = f"Unipile returned {status_code}"
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            result["error"] = str(_wrap_connection_error(e, self.base_url))
        except Exception as e:
            result["error"] = f"Handle invitation failed: {e}"
        return result

    async def withdraw_invitation(
        self,
        account_id: str,
        invitation_id: str,
    ) -> dict[str, Any]:
        """Withdraw a sent LinkedIn invitation via the backend proxy.

        Args:
            account_id: The Unipile account ID (unused, taken from backend JWT).
            invitation_id: The invitation ID to withdraw.

        Returns:
            {"success": bool, "error": str}
        """
        result: dict[str, Any] = {"success": False, "error": ""}
        url = f"{self.base_url}/api/v1/linkedin/invitations/{invitation_id}"
        try:
            resp = await self._client.delete(url, headers=self._headers())
            if resp.status_code == 401:
                result["error"] = "Backend JWT expired."
                return result
            if resp.status_code in (200, 201, 204):
                # The proxy relays Unipile's status inside a 200 body; only a
                # transport failure in the backend becomes a 502. Reading the
                # outer status alone reports every refusal as a withdrawal, and
                # the caller acts on that: it spends daily budget, closes the
                # outreach row and logs success while the invitation is still
                # pending on LinkedIn.
                inner_error = _unipile_error_in_body(resp)
                if inner_error:
                    result["error"] = inner_error
                    return result
                result["success"] = True
            elif resp.status_code == 404:
                result["error"] = "Invitation not found."
            else:
                body = resp.text[:200]
                result["error"] = f"Unipile returned {resp.status_code}: {body}"
        except Exception as e:
            result["error"] = f"Withdraw failed: {e}"
        return result

    # ── Get Pending (Sent) Invitations ──

    async def get_pending_invitations(
        self,
        account_id: str,
    ) -> list[dict[str, Any]]:
        """Fetch all pending sent LinkedIn invitations via the backend proxy.

        The backend handles cursor pagination server-side.

        Returns:
            List of invitation dicts (id, provider_id, name, timestamp, etc.).
        """
        url = f"{self.base_url}/api/v1/linkedin/invitations/sent"
        try:
            resp = await self._client.get(url, headers=self._headers())
            if resp.status_code == 401:
                logger.warning("get_pending_invitations: backend JWT expired")
                return []
            if resp.status_code != 200:
                logger.debug("get_pending_invitations: status %d", resp.status_code)
                return []
            data = resp.json()
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("items") or []
            else:
                return []
            logger.info("get_pending_invitations: fetched %d invitations", len(items))
            return [inv for inv in items if isinstance(inv, dict)]
        except Exception as e:
            logger.debug("get_pending_invitations error: %s", e)
            return []

    async def delete_message(
        self,
        account_id: str,
        message_id: str,
    ) -> dict[str, Any]:
        """Delete a LinkedIn message via the backend proxy.

        LinkedIn only allows deletion within 60 minutes of sending.

        Returns:
            {"success": bool, "error": str}
        """
        result: dict[str, Any] = {"success": False, "error": ""}
        url = f"{self.base_url}/api/v1/linkedin/messages/{message_id}"
        try:
            resp = await self._client.delete(url, headers=self._headers())
            if resp.status_code == 401:
                result["error"] = "Backend JWT expired."
                return result
            if resp.status_code in (200, 201, 204):
                inner_error = _unipile_error_in_body(resp)
                if inner_error:
                    result["error"] = inner_error
                    return result
                result["success"] = True
            elif resp.status_code == 404:
                result["error"] = "Message not found on LinkedIn."
            else:
                body = resp.text[:200]
                result["error"] = f"Unipile returned {resp.status_code}: {body}"
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            result["error"] = str(_wrap_connection_error(e, self.base_url))
        except Exception as e:
            result["error"] = f"Delete message failed: {e}"
        return result

    # ── Messages (comprehensive) ──

    async def list_all_messages(
        self,
        account_id: str,
        limit: int = 50,
        raise_on_error: bool = False,
    ) -> list[dict[str, Any]]:
        """Fetch ALL messages across all conversations via the backend proxy.

        GET /api/v1/messages

        Args:
            account_id: The Unipile account ID (unused, taken from backend JWT).
            limit: Max messages to return.

        Returns:
            List of message dicts with sender_id, sender_name, text,
            timestamp, chat_id, message_id.
        """
        url = f"{self.base_url}/api/v1/linkedin/messages?limit={limit}"
        try:
            resp = await self._client.get(url, headers=self._headers())
            if resp.status_code in (401, 403):
                raise UnipileAuthError()
            if resp.status_code != 200:
                if raise_on_error:
                    raise UnipileError(
                        f"list_all_messages: HTTP {resp.status_code}"
                    )
                return []
            data = resp.json()

            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("items") or data.get("messages") or data.get("data") or []
            else:
                return []

            messages: list[dict[str, Any]] = []
            for msg in items[:limit]:
                if not isinstance(msg, dict):
                    continue
                text = msg.get("text") or msg.get("body") or msg.get("content") or ""
                sender_id = msg.get("sender_id") or msg.get("sender", {}).get("provider_id", "")
                sender_name = msg.get("sender_name") or msg.get("sender", {}).get("display_name", "")
                chat_id = msg.get("chat_id") or msg.get("conversation_id") or ""
                message_id = msg.get("id") or msg.get("message_id") or ""
                ts_raw = msg.get("timestamp") or msg.get("created_at") or msg.get("date") or ""
                timestamp = 0
                if isinstance(ts_raw, (int, float)):
                    timestamp = int(ts_raw)
                elif isinstance(ts_raw, str) and ts_raw:
                    try:
                        from datetime import datetime
                        timestamp = int(datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp())
                    except (ValueError, TypeError):
                        pass
                messages.append({
                    "sender_id": str(sender_id),
                    "sender_name": str(sender_name),
                    "text": text,
                    "timestamp": timestamp,
                    "chat_id": str(chat_id),
                    "message_id": str(message_id),
                })
            return messages
        except UnipileAuthError:
            raise
        except UnipileError:
            raise
        except Exception as e:
            logger.warning("Failed to list all messages via backend: %s", e)
            if raise_on_error:
                raise UnipileError(f"list_all_messages: {e}") from e
            return []

    async def get_messages_by_sender(
        self,
        account_id: str,
        sender_id: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Fetch messages from a specific person via the backend proxy.

        GET /api/v1/chat_attendees/{sender_id}/messages

        Args:
            account_id: The Unipile account ID (unused, taken from backend JWT).
            sender_id: The attendee's provider_id or public_id.
            limit: Max messages to return.

        Returns:
            List of message dicts with sender_id, sender_name, text,
            timestamp, chat_id, message_id.
        """
        url = f"{self.base_url}/api/v1/linkedin/chat_attendees/{sender_id}/messages?limit={limit}"
        try:
            resp = await self._client.get(url, headers=self._headers())
            if resp.status_code in (401, 403):
                raise UnipileAuthError()
            if resp.status_code != 200:
                return []
            data = resp.json()

            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("items") or data.get("messages") or data.get("data") or []
            else:
                return []

            messages: list[dict[str, Any]] = []
            for msg in items[:limit]:
                if not isinstance(msg, dict):
                    continue
                text = msg.get("text") or msg.get("body") or msg.get("content") or ""
                msg_sender_id = msg.get("sender_id") or msg.get("sender", {}).get("provider_id", "")
                sender_name = msg.get("sender_name") or msg.get("sender", {}).get("display_name", "")
                chat_id = msg.get("chat_id") or msg.get("conversation_id") or ""
                message_id = msg.get("id") or msg.get("message_id") or ""
                ts_raw = msg.get("timestamp") or msg.get("created_at") or msg.get("date") or ""
                timestamp = 0
                if isinstance(ts_raw, (int, float)):
                    timestamp = int(ts_raw)
                elif isinstance(ts_raw, str) and ts_raw:
                    try:
                        from datetime import datetime
                        timestamp = int(datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp())
                    except (ValueError, TypeError):
                        pass
                messages.append({
                    "sender_id": str(msg_sender_id),
                    "sender_name": str(sender_name),
                    "text": text,
                    "timestamp": timestamp,
                    "chat_id": str(chat_id),
                    "message_id": str(message_id),
                })
            return messages
        except UnipileAuthError:
            raise
        except Exception as e:
            logger.warning("Failed to get messages by sender via backend: %s", e)
            return []

    async def add_message_reaction(
        self,
        account_id: str,
        message_id: str,
        reaction: str = "\U0001f44d",
    ) -> dict[str, Any]:
        """React to a LinkedIn DM message via the backend proxy.

        POST /api/v1/messages/{message_id}/reaction

        Args:
            account_id: The Unipile account ID (unused, taken from backend JWT).
            message_id: The message ID to react to.
            reaction: Emoji reaction (default: thumbs up).

        Returns:
            {"success": bool, "error": str}
        """
        result: dict[str, Any] = {"success": False, "error": ""}
        url = f"{self.base_url}/api/v1/linkedin/messages/{message_id}/reaction"
        payload = {"reaction": reaction}
        try:
            resp = await self._retry_request("POST", url, json=payload)
            if resp.status_code == 401:
                result["error"] = "Backend JWT expired."
                return result
            if resp.status_code in (200, 201, 204):
                result["success"] = True
            elif resp.status_code == 404:
                result["error"] = "Message not found."
            else:
                body = resp.text[:200]
                result["error"] = f"Unipile returned {resp.status_code}: {body}"
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            result["error"] = str(_wrap_connection_error(e, self.base_url))
        except Exception as e:
            result["error"] = f"Add reaction failed: {e}"
        return result

    # ── Follow ──

    async def follow_profile(
        self,
        account_id: str,
        provider_id: str,
    ) -> dict[str, Any]:
        """Follow a LinkedIn profile via the backend proxy.

        Args:
            account_id: The Unipile account ID (unused, taken from backend JWT).
            provider_id: The prospect's LinkedIn provider_id.

        Returns:
            {"success": bool, "error": str}
        """
        if not voyager_health.is_healthy("follow"):
            logger.info("follow_profile: skipped — Voyager 'follow' endpoint unhealthy")
            return {"success": False, "error": "Voyager follow endpoint unavailable", "voyager_down": True}
        result: dict[str, Any] = {"success": False, "error": ""}
        url = f"{self.base_url}/api/v1/linkedin/follow"
        payload = {"provider_id": provider_id}
        try:
            resp = await self._retry_request("POST", url, json=payload)
            if resp.status_code == 401:
                result["error"] = "Backend JWT expired."
                return result
            refusal = _proxy_refusal_message(resp)
            if refusal is not None:
                voyager_health.record("follow", success=False)
                result["error"] = refusal
                return result
            resp.raise_for_status()
            data = _ensure_dict(resp.json())
            _classify_nested_response(data, result)
            voyager_health.record("follow", success=result.get("success", False))
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            result["error"] = str(_wrap_connection_error(e, self.base_url))
        except Exception as e:
            result["error"] = f"Follow failed: {e}"
            voyager_health.record("follow", success=False)
        return result

    async def check_follow_status(
        self,
        account_id: str,
        provider_id: str,
    ) -> dict[str, Any]:
        """Check if we follow a LinkedIn profile via backend proxy.

        Returns: {"following": bool, "error": str}
        """
        result: dict[str, Any] = {"following": False, "error": ""}
        if not provider_id:
            result["error"] = "No provider_id"
            return result
        if not voyager_health.is_healthy("check_follow"):
            logger.info("check_follow_status: skipped — Voyager endpoint unhealthy")
            result["error"] = "Voyager check-follow endpoint unavailable"
            result["voyager_down"] = True
            return result
        url = f"{self.base_url}/api/v1/linkedin/check-follow"
        payload = {"provider_id": provider_id}
        try:
            resp = await self._retry_request("POST", url, json=payload)
            if resp.status_code == 401:
                result["error"] = "Backend JWT expired."
                return result
            refusal = _proxy_refusal_message(resp)
            if refusal is not None:
                voyager_health.record("check_follow", success=False)
                result["error"] = refusal
                return result
            resp.raise_for_status()
            data = _ensure_dict(resp.json())
            result["following"] = bool(data.get("following", False))
            voyager_health.record("check_follow", success=True)
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            result["error"] = str(_wrap_connection_error(e, self.base_url))
        except Exception as e:
            result["error"] = str(e)
            voyager_health.record("check_follow", success=False)
        return result

    async def view_profile(
        self,
        account_id: str,
        provider_id: str,
    ) -> dict[str, Any]:
        """View a LinkedIn profile via the backend proxy.

        Args:
            account_id: The Unipile account ID (unused, taken from backend JWT).
            provider_id: The prospect's LinkedIn provider_id or public_id.

        Returns:
            {"success": bool, "error": str}
        """
        result: dict[str, Any] = {"success": False, "error": ""}
        url = f"{self.base_url}/api/v1/linkedin/view-profile"
        payload = {"provider_id": provider_id}
        try:
            resp = await self._retry_request("POST", url, json=payload)
            if resp.status_code == 401:
                result["error"] = "Backend JWT expired."
                return result
            refusal = _proxy_refusal_message(resp)
            if refusal is not None:
                result["error"] = refusal
                return result
            resp.raise_for_status()
            data = _ensure_dict(resp.json())
            status_code = data.get("status_code", 200)
            if status_code in (200, 201):
                result["success"] = True
            else:
                result["error"] = f"Unipile returned {status_code}"
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            result["error"] = str(_wrap_connection_error(e, self.base_url))
        except Exception as e:
            result["error"] = f"Profile view failed: {e}"
        return result

    # ── Profile Viewers ──

    async def get_profile_viewers(
        self,
        account_id: str,
    ) -> list[dict[str, Any]]:
        """Get list of people who viewed your LinkedIn profile via the backend proxy.

        Tries the v2 (wvmpCards) endpoint first (more reliable), then falls
        back to the v1 (GraphQL) endpoint only if the tracker says v1 is healthy.

        Args:
            account_id: The Unipile account ID (unused, taken from backend JWT).

        Returns:
            List of viewer dicts with name, title, company, relation, url.
        """
        # Try v2 (wvmpCards) first — more reliable
        url_v2 = f"{self.base_url}/api/v1/linkedin/profile-viewers-v2"
        try:
            resp = await self._client.get(url_v2, headers=self._headers())
            if resp.status_code == 200:
                data = _ensure_dict(resp.json())
                viewers = data.get("viewers", [])
                voyager_health.record("profile_viewers_v2", success=True)
                if viewers:
                    logger.info("get_profile_viewers v2: found %d viewers", len(viewers))
                    return viewers
                logger.debug("get_profile_viewers v2: 200 but 0 viewers, trying v1")
            else:
                voyager_health.record("profile_viewers_v2", success=False)
                logger.warning(
                    "get_profile_viewers v2: status=%d body=%s",
                    resp.status_code, str(resp.text)[:300],
                )
        except Exception as e:
            voyager_health.record("profile_viewers_v2", success=False)
            logger.warning("get_profile_viewers v2 exception: %s", e)

        # Fall back to v1 (GraphQL) only if it's been healthy recently
        if not voyager_health.is_healthy("profile_viewers_v1"):
            logger.info("get_profile_viewers: skipping v1 — endpoint unhealthy")
            return []

        url = f"{self.base_url}/api/v1/linkedin/profile-viewers"
        try:
            resp = await self._client.get(url, headers=self._headers())
            data = _ensure_dict(resp.json()) if resp.status_code == 200 else {}
            if resp.status_code == 200:
                viewers = data.get("viewers", [])
                # A 200 with zero viewers is a quiet profile, not a dead
                # endpoint. Recording it as failure disabled this fallback
                # after three empty polls (min_calls=3, min_success_rate=0.3).
                voyager_health.record("profile_viewers_v1", success=True)
                if viewers:
                    logger.info("get_profile_viewers v1: found %d viewers", len(viewers))
                    return viewers
                logger.debug("get_profile_viewers v1: 200 but 0 viewers")
            else:
                voyager_health.record("profile_viewers_v1", success=False)
                logger.warning(
                    "get_profile_viewers v1: status=%d body=%s",
                    resp.status_code, str(resp.text)[:300],
                )
        except Exception as e:
            voyager_health.record("profile_viewers_v1", success=False)
            logger.warning("get_profile_viewers v1 exception: %s", e)

        logger.warning("get_profile_viewers: both v1 and v2 returned no viewers")
        return []

    # ── Post Search ──

    async def search_posts(
        self,
        account_id: str,
        keywords: str,
        limit: int = 25,
        *,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Search LinkedIn posts by keywords via the backend proxy."""
        url = f"{self.base_url}/api/v1/linkedin/search/posts"
        payload: dict[str, Any] = {
            "keywords": keywords,
            "limit": min(limit, 50),
        }
        if cursor:
            payload["cursor"] = cursor
        try:
            # send() counts the request iff it reached the wire — the budget
            # a collector books is denominated in requests LinkedIn saw.
            resp = await self.search_traffic.send(
                self._post(url, json=payload, headers=self._headers())
            )
            if resp.status_code != 200:
                return [], None
            data = resp.json()

            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("items") or data.get("results") or data.get("data") or []
            else:
                items = []

            next_cursor = None
            if isinstance(data, dict):
                next_cursor = data.get("cursor") or data.get("next_cursor") or data.get("paging", {}).get("cursor")

            posts: list[dict[str, Any]] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                author = item.get("author") or {}
                if isinstance(author, str):
                    author = {"name": author}
                # Issue #64: the numeric author id upstream sends is not a
                # per-author identity — classify every identity field instead.
                identity = parse_author_identity(author)
                posts.append({
                    "post_id": item.get("id") or item.get("post_id") or "",
                    "text": item.get("text") or item.get("body") or item.get("content") or "",
                    "author_name": author.get("name") or author.get("display_name") or "",
                    "author_id": identity["provider_id"],
                    "author_public_id": identity["public_id"],
                    "author_numeric_id": identity["numeric_id"],
                    "author_headline": author.get("headline") or "",
                    "author_url": author.get("profile_url") or author.get("public_profile_url") or "",
                    "reactions_count": item.get("reactions_count") or item.get("reaction_counter") or item.get("likes") or 0,
                    "comments_count": item.get("comments_count") or item.get("comment_counter") or item.get("comments") or 0,
                    "impressions_count": item.get("impressions_counter") or item.get("impressions_count") or item.get("views_count") or 0,
                    "reposts_count": item.get("repost_counter") or item.get("reposts_count") or item.get("shares_count") or 0,
                    "timestamp": item.get("timestamp") or item.get("created_at") or item.get("parsed_datetime") or "",
                    "share_url": item.get("share_url") or "",
                    "is_repost": bool(item.get("is_repost")),
                    "visibility": item.get("visibility") or "",
                    "is_company": bool(author.get("is_company")),
                })
            return posts, next_cursor
        except Exception as e:
            logger.warning(f"Backend post search error: {e}")
            return [], None

    # ── Company Search ──

    async def search_companies(
        self,
        account_id: str,
        keywords: str,
        limit: int = 25,
        *,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Search LinkedIn companies by keywords via the backend proxy."""
        url = f"{self.base_url}/api/v1/linkedin/search/companies"
        payload: dict[str, Any] = {
            "keywords": keywords,
            "limit": min(limit, 50),
        }
        if cursor:
            payload["cursor"] = cursor
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
            if resp.status_code != 200:
                return [], None
            data = resp.json()

            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("items") or data.get("results") or data.get("data") or []
            else:
                items = []

            next_cursor = None
            if isinstance(data, dict):
                next_cursor = data.get("cursor") or data.get("next_cursor") or data.get("paging", {}).get("cursor")

            companies: list[dict[str, Any]] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                companies.append({
                    "company_id": item.get("id") or item.get("company_id") or "",
                    "name": item.get("name") or item.get("title") or "",
                    "industry": item.get("industry") or "",
                    "size": item.get("size") or item.get("company_size") or "",
                    "employee_count": item.get("employee_count") or item.get("employeeCount") or 0,
                    "description": item.get("description") or item.get("tagline") or "",
                    "website": item.get("website") or "",
                    "logo_url": item.get("logo_url") or item.get("logo") or "",
                    "linkedin_url": item.get("url") or item.get("linkedin_url") or item.get("public_profile_url") or "",
                    "location": item.get("location") or item.get("headquarters") or "",
                })
            return companies, next_cursor
        except Exception as e:
            logger.warning(f"Backend company search error: {e}")
            return [], None

    # ── Job Search (Intent Signals) ──

    async def search_jobs(
        self,
        account_id: str,
        keywords: str,
        limit: int = 25,
        *,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Search LinkedIn job postings by keywords via the backend proxy."""
        url = f"{self.base_url}/api/v1/linkedin/search/jobs"
        payload: dict[str, Any] = {
            "keywords": keywords,
            "limit": min(limit, 50),
        }
        if cursor:
            payload["cursor"] = cursor
        try:
            # send() counts the request iff it reached the wire — the budget
            # a collector books is denominated in requests LinkedIn saw.
            resp = await self.search_traffic.send(
                self._post(url, json=payload, headers=self._headers())
            )
            if resp.status_code != 200:
                return [], None
            data = resp.json()

            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("items") or data.get("results") or data.get("data") or []
            else:
                items = []

            next_cursor = None
            if isinstance(data, dict):
                next_cursor = data.get("cursor") or data.get("next_cursor") or data.get("paging", {}).get("cursor")

            jobs: list[dict[str, Any]] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                company = item.get("company") or {}
                if isinstance(company, str):
                    company = {"name": company}
                jobs.append({
                    "job_id": item.get("id") or item.get("job_id") or "",
                    "title": item.get("title") or item.get("name") or "",
                    "company_name": company.get("name") or item.get("company_name") or "",
                    "company_id": str(company.get("id") or company.get("provider_id") or ""),
                    "company_url": company.get("url") or company.get("linkedin_url") or "",
                    "location": item.get("location") or "",
                    "description": (item.get("description") or item.get("body") or "")[:500],
                    "posted_at": item.get("posted_at") or item.get("timestamp") or item.get("created_at") or "",
                    "applicants": item.get("applicants") or item.get("applicant_count") or 0,
                    "linkedin_url": item.get("url") or item.get("linkedin_url") or "",
                })
            return jobs, next_cursor
        except Exception as e:
            logger.warning(f"Backend job search error: {e}")
            return [], None

    # ── SSI Score ──

    async def get_ssi_score(self, account_id: str) -> dict[str, Any]:
        """Get Social Selling Index (SSI) score via the backend proxy."""
        if not voyager_health.is_healthy("ssi"):
            logger.info("get_ssi_score: skipped — Voyager SSI endpoint unhealthy")
            return {}
        url = f"{self.base_url}/api/v1/linkedin/ssi-score"
        try:
            resp = await self._client.get(url, headers=self._headers())
            if resp.status_code != 200:
                voyager_health.record("ssi", success=False)
                logger.warning(
                    "get_ssi_score: status=%d body=%s",
                    resp.status_code, str(resp.text)[:300],
                )
                return {}
            voyager_health.record("ssi", success=True)
            data = _ensure_dict(resp.json())
            return data
        except Exception as e:
            voyager_health.record("ssi", success=False)
            logger.warning("get_ssi_score exception: %s", e)
            return {}

    # ── Webhooks ──

    # ── Email ──

    async def send_email(
        self,
        account_id: str,
        to_email: str,
        to_name: str,
        subject: str,
        body: str,
        reply_to_provider_id: str = "",
        tracking_label: str = "",
        body_html: str = "",
    ) -> dict[str, Any]:
        """Send an email via a connected email account (backend proxy)."""
        url = f"{self.base_url}/api/v1/email/send"
        payload: dict[str, Any] = {
            "account_id": account_id,
            "to_email": to_email,
            "to_name": to_name,
            "subject": subject,
            "body": body,
        }
        # The API treats body as plain text. Markup callers send body_html so
        # it is not escaped twice. Omit the field when empty so campaign
        # first-touch stays on the convert-plain path.
        if body_html:
            payload["body_html"] = body_html
        if reply_to_provider_id:
            payload["reply_to_provider_id"] = reply_to_provider_id
        if tracking_label:
            payload["tracking_label"] = tracking_label

        result: dict[str, Any] = {"success": False, "email_id": "", "error": ""}
        try:
            resp = await self._retry_request("POST", url, json=payload, no_retry=True)
            if resp.status_code in (200, 201):
                data = _ensure_dict(resp.json())
                result["success"] = True
                result["email_id"] = data.get("email_id") or data.get("id") or ""
            elif resp.status_code == 400:
                result["error"] = "No email account connected."
            elif resp.status_code == 401:
                raise UnipileAuthError("Backend JWT expired or invalid.")
            else:
                result["error"] = f"Backend returned {resp.status_code}: {resp.text[:200]}"
        except UnipileAuthError:
            raise
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url)
        except Exception as e:
            result["error"] = f"Email send failed: {e}"
        return result

    async def list_emails(
        self,
        account_id: str,
        limit: int = 20,
        folder: str = "",
    ) -> list[dict[str, Any]]:
        """List emails from connected email account (backend proxy)."""
        url = f"{self.base_url}/api/v1/email/list"
        params: dict[str, str] = {"limit": str(limit)}
        if folder:
            params["folder"] = folder
        try:
            resp = await self._client.get(url, params=params, headers=self._headers())
            if resp.status_code != 200:
                return []
            data = _ensure_dict(resp.json())
            return data.get("emails", [])
        except Exception as e:
            logger.debug(f"list_emails failed: {e}")
            return []

    async def get_email(
        self,
        account_id: str,
        email_id: str,
    ) -> dict[str, Any]:
        """Get a single email by ID (backend proxy)."""
        url = f"{self.base_url}/api/v1/email/{email_id}"
        try:
            resp = await self._client.get(url, headers=self._headers())
            if resp.status_code != 200:
                return {}
            return resp.json()
        except Exception as e:
            logger.debug(f"get_email failed: {e}")
            return {}

    async def mark_email(
        self,
        account_id: str,
        email_id: str,
        action: str = "setRead",
    ) -> dict[str, Any]:
        """Mark an email as read/unread/archived (backend proxy)."""
        url = f"{self.base_url}/api/v1/email/{email_id}/mark"
        payload = {"action": action}
        result: dict[str, Any] = {"success": False, "error": ""}
        try:
            resp = await self._client.patch(url, json=payload, headers=self._headers())
            if resp.status_code in (200, 204):
                result["success"] = True
            else:
                result["error"] = f"Backend returned {resp.status_code}"
        except Exception as e:
            result["error"] = f"Mark email failed: {e}"
        return result

    # ── Search Parameters ──

    async def get_search_params(
        self, type: str, keywords: str, search_account_id: str | None = None,
    ) -> list[dict[str, str]]:
        """Look up LinkedIn search parameter codes via the backend proxy.

        Args:
            type: Parameter type (LOCATION, INDUSTRY, JOB_TITLE, DEPARTMENT, etc.)
            keywords: Search keywords to look up codes for.
            search_account_id: Optional override to route through a premium account.

        Returns:
            List of {name, code} dicts.
        """
        url = f"{self.base_url}/api/v1/search/parameters"
        payload: dict[str, Any] = {"type": type, "keywords": keywords}
        if search_account_id:
            payload["search_account_id"] = search_account_id
        resp = await self._retry_request("POST", url, json=payload)
        if resp.status_code == 401:
            raise UnipileAuthError("Backend JWT expired or invalid.")
        _raise_proxy_refusal(resp, _NO_LINKEDIN_MSG)
        resp.raise_for_status()
        return resp.json().get("params", [])

    # ── Voice Memo Proxy ──

    async def generate_voice_memo(
        self,
        text: str,
        voice_config: dict[str, Any],
        voice_signature: dict[str, Any] | None = None,
        humanize: bool = True,
        noise_type: str = "auto",
        noise_volume: str = "subtle",
    ) -> tuple[str, float]:
        """Generate voice memo audio via backend enhanced voice pipeline.

        The backend runs text humanization, multi-utterance splitting,
        Hume TTS, and ambient noise overlay in a single request.

        Returns:
            Tuple of (audio_base64: str, duration_seconds: float).
        """
        url = f"{self.base_url}/api/v1/voice/generate"
        payload: dict[str, Any] = {
            "text": text,
            "voice_config": voice_config,
            "humanize": humanize,
            "noise_type": noise_type,
            "noise_volume": noise_volume,
        }
        if voice_signature:
            payload["voice_signature"] = voice_signature
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url)
        if resp.status_code == 401:
            raise UnipileAuthError("Backend JWT expired or invalid.")
        if resp.status_code >= 400:
            detail = resp.text[:200] if resp.text else f"HTTP {resp.status_code}"
            raise UnipileError(f"Voice memo generation failed: {detail}")
        data = _ensure_dict(resp.json())
        return data.get("audio_base64", ""), data.get("duration_seconds", 0.0)

    # ── LLM Proxy Methods ──

    async def analyze_voice(
        self, profile: dict[str, Any], posts: list[dict],
    ) -> dict[str, Any]:
        """Analyze voice signature via the backend LLM proxy."""
        url = f"{self.base_url}/api/v1/llm/analyze-voice"
        payload = {"profile": profile, "posts": posts}
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url)
        if resp.status_code == 401:
            raise UnipileAuthError("Backend JWT expired or invalid.")
        if resp.status_code == 503:
            raise UnipileError("Backend LLM service unavailable — no GEMINI_API_KEY configured.")
        resp.raise_for_status()
        return resp.json()

    async def generate_icp(
        self, target_description: str, user_context: dict[str, Any],
    ) -> dict[str, Any]:
        """Generate ICP via the backend LLM proxy."""
        url = f"{self.base_url}/api/v1/llm/generate-icp"
        payload = {"target_description": target_description, "user_context": user_context}
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url)
        if resp.status_code == 401:
            raise UnipileAuthError("Backend JWT expired or invalid.")
        if resp.status_code == 503:
            raise UnipileError("Backend LLM service unavailable.")
        resp.raise_for_status()
        data = _ensure_dict(resp.json())
        return data.get("icp", data)

    async def generate_icp_rag(
        self,
        target_description: str,
        company_context: str = "",
        focus_query: str = "",
        user_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Generate ICP via the backend RAG pipeline (ingest → embed → retrieve → summarize → ICP)."""
        url = f"{self.base_url}/api/v1/llm/generate-icp-rag"
        payload = {
            "target_description": target_description,
            "company_context": company_context,
            "focus_query": focus_query,
            "user_context": user_context or {},
        }
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url)
        if resp.status_code == 401:
            raise UnipileAuthError("Backend JWT expired or invalid.")
        if resp.status_code == 503:
            raise UnipileError("Backend LLM service unavailable.")
        resp.raise_for_status()
        data = _ensure_dict(resp.json())
        return data.get("icp", data)

    async def icp_goal_match(
        self,
        goal: str,
        offer: str = "",
        icp: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Ask the backend whether an ICP holds buyers who can deliver a goal.

        Returns the judge payload: verdict / decision_maker_coverage /
        persona_alignment / warnings / suggestions / reason / kb_cards_used.
        """
        url = f"{self.base_url}/api/v1/llm/icp-goal-match"
        payload = {"goal": goal, "offer": offer, "icp": icp or {}}
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url)
        if resp.status_code == 401:
            raise UnipileAuthError("Backend JWT expired or invalid.")
        if resp.status_code == 503:
            raise UnipileError("Backend LLM service unavailable.")
        resp.raise_for_status()
        return _ensure_dict(resp.json())

    async def generate_message(
        self,
        sender: dict[str, Any],
        prospect: dict[str, Any],
        voice: dict[str, Any],
        campaign_context: dict[str, Any],
        prospect_analysis: dict[str, Any] | None = None,
        conversation_history: list[dict] | None = None,
    ) -> str:
        """Generate a personalized message via the backend LLM proxy."""
        url = f"{self.base_url}/api/v1/llm/generate-message"
        payload = {
            "sender": sender,
            "prospect": prospect,
            "voice": voice,
            "campaign_context": campaign_context,
        }
        if prospect_analysis:
            payload["prospect_analysis"] = prospect_analysis
        if conversation_history:
            payload["conversation_history"] = conversation_history
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url)
        if resp.status_code == 401:
            raise UnipileAuthError("Backend JWT expired or invalid.")
        if resp.status_code == 503:
            raise UnipileError("Backend LLM service unavailable.")
        resp.raise_for_status()
        return resp.json().get("message", "")

    async def improve_message(
        self,
        draft: str,
        voice: dict[str, Any],
        message_type: str = "invitation",
        max_chars: int = 200,
        intent: str = "",
        must_keep: str = "",
        role_frame: str = "",
        conversation_history: list[dict[str, Any]] | None = None,
    ) -> str:
        """Improve a message via the backend LLM proxy.

        The optional fields (intent / must_keep / role_frame /
        conversation_history) are ignored by a backend that predates the
        9 Sep reply-context work, so this is safe to ship ahead of it.
        """
        url = f"{self.base_url}/api/v1/llm/improve-message"
        payload: dict[str, Any] = {
            "draft": draft,
            "voice": voice,
            "message_type": message_type,
            "max_chars": max_chars,
        }
        if intent:
            payload["intent"] = intent
        if must_keep:
            payload["must_keep"] = must_keep
        if role_frame:
            payload["role_frame"] = role_frame
        if conversation_history:
            payload["conversation_history"] = conversation_history
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url)
        if resp.status_code == 401:
            raise UnipileAuthError("Backend JWT expired or invalid.")
        if resp.status_code == 503:
            raise UnipileError("Backend LLM service unavailable.")
        resp.raise_for_status()
        return resp.json().get("message", draft)

    async def fix_message(
        self,
        message: str,
        issues: list[str],
        voice: dict[str, Any],
        message_type: str = "invitation",
        max_chars: int = 200,
    ) -> str:
        """Fix validation issues in a message via the backend LLM proxy."""
        url = f"{self.base_url}/api/v1/llm/fix-message"
        payload = {
            "message": message,
            "issues": issues,
            "voice": voice,
            "message_type": message_type,
            "max_chars": max_chars,
        }
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url)
        if resp.status_code == 401:
            raise UnipileAuthError("Backend JWT expired or invalid.")
        if resp.status_code == 503:
            raise UnipileError("Backend LLM service unavailable.")
        resp.raise_for_status()
        return resp.json().get("message", message)

    async def recheck_campaign_fit(
        self,
        prospect: dict[str, Any],
        campaign_context: dict[str, Any],
        icp_data: dict[str, Any] | None = None,
        our_last_message: str = "",
    ) -> dict[str, Any]:
        """Judge campaign fit via the backend LLM proxy."""
        url = f"{self.base_url}/api/v1/llm/recheck-campaign-fit"
        payload = {
            "prospect": prospect,
            "campaign_context": campaign_context,
            "icp_data": icp_data or {},
            "our_last_message": our_last_message or "",
        }
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url)
        if resp.status_code == 401:
            raise UnipileAuthError("Backend JWT expired or invalid.")
        if resp.status_code == 503:
            raise UnipileError("Backend LLM service unavailable.")
        resp.raise_for_status()
        return _ensure_dict(resp.json())

    async def classify_sentiment(self, text: str) -> str:
        """Classify sentiment via the backend LLM proxy."""
        url = f"{self.base_url}/api/v1/llm/classify-sentiment"
        payload = {"text": text}
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url)
        if resp.status_code == 401:
            raise UnipileAuthError("Backend JWT expired or invalid.")
        if resp.status_code == 503:
            raise UnipileError("Backend LLM service unavailable.")
        resp.raise_for_status()
        sentiment = resp.json().get("sentiment")
        return sentiment if sentiment else "neutral"

    # ── Knowledge base (hosted RAG corpus) ──

    @staticmethod
    def _knowledge_detail(resp: Any, fallback: str) -> str:
        """The backend's own refusal text, or a readable fallback.

        The knowledge routes answer 400/404/409/422 with a FastAPI
        ``{"detail": ...}`` body that names the actual limit hit (which cap,
        which kind). Swallowing it leaves the tool printing a bare status code
        at a user who can do nothing with it.

        FastAPI's own 422 is the exception: its ``detail`` is a list of
        ``{loc, msg, type}`` dicts, and str() on that prints Python repr at
        the user. Join the ``msg`` fields instead.
        """
        try:
            data = _ensure_dict(resp.json())
        except (ValueError, AttributeError):
            return fallback
        detail = data.get("detail")
        if not detail:
            return fallback
        if isinstance(detail, list):
            msgs = [
                str(d["msg"]).strip()
                for d in detail
                if isinstance(d, dict) and d.get("msg")
            ]
            if msgs:
                return "; ".join(msgs)[:300]
        return str(detail)[:300]

    def _knowledge_check_available(self, resp: Any) -> None:
        """A 5xx means the corpus is down, not that the caller did anything.

        Left to raise_for_status(), httpx renders two lines that quote the
        backend URL — nothing the user can act on. Say it in one line.
        """
        if getattr(resp, "status_code", 0) >= 500:
            raise UnipileError(_KNOWLEDGE_UNAVAILABLE)

    async def knowledge_add(
        self, title: str, text: str, source_uri: str = "",
    ) -> dict[str, Any]:
        """Upload one document into the workspace knowledge base."""
        url = f"{self.base_url}/api/v1/knowledge/sources"
        payload = {"title": title, "text": text, "source_uri": source_uri}
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url)
        self._check_rate_limit(resp)
        if resp.status_code == 401:
            raise UnipileAuthError("Backend JWT expired or invalid.")
        if resp.status_code in (409, 422):
            raise UnipileError(
                self._knowledge_detail(resp, "Knowledge base refused this upload.")
            )
        self._knowledge_check_available(resp)
        resp.raise_for_status()
        try:
            return _ensure_dict(resp.json())
        except (ValueError, AttributeError):
            # A 201 Created may carry only a Location header. The upload
            # happened; report it rather than failing on the missing body.
            return {
                "id": "",
                "title": title,
                "source_type": "upload",
                "chunk_count": 0,
                "embed_status": "queued",
            }

    async def knowledge_list(self, kind: str = "") -> dict[str, Any]:
        """List knowledge sources, optionally filtered to one kind.

        Returns the whole payload — ``sources`` plus ``totals`` — because the
        totals cover kinds the filtered list does not.
        """
        url = f"{self.base_url}/api/v1/knowledge/sources"
        params = {"kind": kind} if kind else None
        try:
            resp = await self._client.get(
                url, params=params, headers=self._headers()
            )
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url)
        self._check_rate_limit(resp)
        if resp.status_code == 401:
            raise UnipileAuthError("Backend JWT expired or invalid.")
        if resp.status_code == 400:
            raise UnipileError(self._knowledge_detail(resp, f"Unknown kind '{kind}'."))
        self._knowledge_check_available(resp)
        resp.raise_for_status()
        return _ensure_dict(resp.json())

    async def knowledge_remove(self, source_id: str) -> bool:
        """Delete one knowledge source from this workspace."""
        url = (
            f"{self.base_url}/api/v1/knowledge/sources/"
            f"{quote(source_id, safe='')}"
        )
        try:
            resp = await self._client.delete(url, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url)
        self._check_rate_limit(resp)
        if resp.status_code == 401:
            raise UnipileAuthError("Backend JWT expired or invalid.")
        if resp.status_code == 404:
            raise UnipileError(
                self._knowledge_detail(
                    resp, f"Source not found in this workspace: {source_id}"
                )
            )
        self._knowledge_check_available(resp)
        resp.raise_for_status()
        if resp.status_code == 204:
            return True
        try:
            data = _ensure_dict(resp.json())
        except (ValueError, AttributeError):
            # A DELETE is not obliged to answer with a body; an empty or
            # non-JSON 200 still means the source is gone.
            return True
        return bool(data.get("deleted", True))

    async def knowledge_refresh(
        self, scope: str = "all", campaign_id: str = "", sync: bool = False,
    ) -> dict[str, Any]:
        """Re-ingest the derived corpus (website, campaigns, reply exemplars).

        ``sync=True`` crawls inline, so it gets ``_LLM_TIMEOUT``: the route is
        not under /api/v1/llm/, so _post's own heuristic would leave it on the
        30s default and cut every crawl short.
        """
        url = f"{self.base_url}/api/v1/knowledge/refresh"
        payload: dict[str, Any] = {"scope": scope, "sync": sync}
        if campaign_id:
            payload["campaign_id"] = campaign_id
        try:
            resp = await self._post(
                url,
                json=payload,
                headers=self._headers(),
                timeout=_LLM_TIMEOUT if sync else None,
            )
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url)
        self._check_rate_limit(resp)
        if resp.status_code == 401:
            raise UnipileAuthError("Backend JWT expired or invalid.")
        if resp.status_code in (400, 404, 409, 422):
            raise UnipileError(
                self._knowledge_detail(resp, f"Knowledge refresh refused: {scope}")
            )
        self._knowledge_check_available(resp)
        resp.raise_for_status()
        return _ensure_dict(resp.json())

    async def knowledge_search(
        self,
        query: str,
        kinds: list[str] | None = None,
        top_k: int = 6,
        campaign_id: str = "",
    ) -> list[dict[str, Any]]:
        """Retrieve grounded evidence chunks for a query."""
        url = f"{self.base_url}/api/v1/knowledge/search"
        payload: dict[str, Any] = {"query": query, "top_k": top_k}
        if kinds:
            payload["kinds"] = list(kinds)
        if campaign_id:
            payload["campaign_id"] = campaign_id
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url)
        self._check_rate_limit(resp)
        if resp.status_code == 401:
            raise UnipileAuthError("Backend JWT expired or invalid.")
        if resp.status_code == 400:
            raise UnipileError(
                self._knowledge_detail(resp, "Knowledge search refused this request.")
            )
        self._knowledge_check_available(resp)
        resp.raise_for_status()
        data = _ensure_dict(resp.json())
        evidence = data.get("evidence")
        return evidence if isinstance(evidence, list) else []

    async def generate_followup(
        self,
        sender: dict[str, Any],
        prospect: dict[str, Any],
        voice: dict[str, Any],
        campaign_context: dict[str, Any],
        conversation_history: list[dict[str, Any]],
        followup_number: int = 1,
        engagement_history: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Generate a follow-up DM via the backend LLM proxy.

        Carries the backend's ``allowed_entities`` — the companies and
        products the generated text is permitted to name. Nothing on this
        client reads it yet: it is here so the entity guard that will check
        the message against it has evidence to check, and so a future
        "nothing was allowed" is distinguishable from "the client dropped
        the key". Until that guard lands, this value is inert.
        """
        url = f"{self.base_url}/api/v1/llm/generate-followup"
        payload: dict[str, Any] = {
            "sender": sender,
            "prospect": prospect,
            "voice": voice,
            "campaign_context": campaign_context,
            "conversation_history": conversation_history,
            "followup_number": followup_number,
        }
        if engagement_history:
            payload["engagement_history"] = engagement_history
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url)
        if resp.status_code == 401:
            raise UnipileAuthError("Backend JWT expired or invalid.")
        if resp.status_code == 503:
            raise UnipileError("Backend LLM service unavailable.")
        resp.raise_for_status()
        data = _ensure_dict(resp.json())
        return {
            "message": data.get("message", ""),
            "reasoning": data.get("reasoning", {}),
            # Carried for a client-side entity guard that does not exist yet.
            "allowed_entities": data.get("allowed_entities", []),
        }

    async def generate_reply(
        self,
        sender: dict[str, Any],
        prospect: dict[str, Any],
        voice: dict[str, Any],
        campaign_context: dict[str, Any],
        conversation_history: list[dict[str, Any]],
        reply_text: str,
        sentiment: str,
        booking_link: str = "",
        prospect_calendar_url: str = "",
    ) -> dict[str, Any]:
        """Generate a reply via the backend LLM proxy.

        Carries the backend's ``allowed_entities`` — the companies and
        products the generated text is permitted to name. Nothing on this
        client reads it yet: it is here so the entity guard that will check
        the message against it has evidence to check, and so a future
        "nothing was allowed" is distinguishable from "the client dropped
        the key". Until that guard lands, this value is inert.
        """
        url = f"{self.base_url}/api/v1/llm/generate-reply"
        payload = {
            "sender": sender,
            "prospect": prospect,
            "voice": voice,
            "campaign_context": campaign_context,
            "conversation_history": conversation_history,
            "reply_text": reply_text,
            "sentiment": sentiment,
            "booking_link": booking_link,
            "prospect_calendar_url": prospect_calendar_url,
        }
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url)
        if resp.status_code == 401:
            raise UnipileAuthError("Backend JWT expired or invalid.")
        if resp.status_code == 503:
            raise UnipileError("Backend LLM service unavailable.")
        resp.raise_for_status()
        data = _ensure_dict(resp.json())
        return {
            "message": data.get("message", ""),
            "reasoning": data.get("reasoning", {}),
            # Carried for a client-side entity guard that does not exist yet.
            "allowed_entities": data.get("allowed_entities", []),
        }

    async def generate_comment(
        self,
        sender: dict[str, Any],
        prospect: dict[str, Any],
        voice: dict[str, Any],
        post_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Generate a comment via the backend LLM proxy."""
        url = f"{self.base_url}/api/v1/llm/generate-comment"
        payload = {
            "sender": sender,
            "prospect": prospect,
            "voice": voice,
            "post_data": post_data,
        }
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url)
        if resp.status_code == 401:
            raise UnipileAuthError("Backend JWT expired or invalid.")
        if resp.status_code == 503:
            raise UnipileError("Backend LLM service unavailable.")
        resp.raise_for_status()
        data = _ensure_dict(resp.json())
        return {
            "comment": data.get("comment", ""),
            "style": data.get("style", ""),
            "reasoning": data.get("reasoning", {}),
        }

    async def analyze_prospect(
        self,
        prospect: dict[str, Any],
        campaign_context: dict[str, Any],
        icp_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Analyze a prospect via the backend LLM proxy."""
        url = f"{self.base_url}/api/v1/llm/analyze-prospect"
        payload = {
            "prospect": prospect,
            "campaign_context": campaign_context,
            "icp_data": icp_data or {},
        }
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url)
        if resp.status_code == 401:
            raise UnipileAuthError("Backend JWT expired or invalid.")
        if resp.status_code == 503:
            raise UnipileError("Backend LLM service unavailable.")
        resp.raise_for_status()
        return resp.json()

    # ── Experiment Analysis (LLM proxy) ──

    async def analyze_experiments(self, snapshot: str) -> dict[str, Any]:
        """Analyze campaign data via backend LLM for experiment hypotheses."""
        url = f"{self.base_url}/api/v1/llm/analyze-experiments"
        payload = {"snapshot": snapshot}
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url)
        if resp.status_code == 401:
            raise UnipileAuthError("Backend JWT expired or invalid.")
        if resp.status_code == 503:
            raise UnipileError("Backend LLM service unavailable.")
        resp.raise_for_status()
        return resp.json()

    # ── Strategy Engine (LLM proxy) ──

    async def analyze_strategy(self, snapshot: str) -> dict[str, Any]:
        """Analyze cross-campaign data via backend LLM for strategy patterns."""
        url = f"{self.base_url}/api/v1/llm/analyze-strategy"
        payload = {"snapshot": snapshot}
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url)
        if resp.status_code == 401:
            raise UnipileAuthError("Backend JWT expired or invalid.")
        if resp.status_code == 503:
            raise UnipileError("Backend LLM service unavailable.")
        resp.raise_for_status()
        return resp.json()

    # ── Communication Strategist (LLM proxy) ──

    async def plan_daily_actions(self, prompt: str) -> dict[str, Any]:
        """Generate daily action plans for a batch of prospects via backend LLM."""
        url = f"{self.base_url}/api/v1/llm/plan-daily-actions"
        payload = {"prompt": prompt}
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url)
        if resp.status_code == 401:
            raise UnipileAuthError("Backend JWT expired or invalid.")
        if resp.status_code == 503:
            raise UnipileError("Backend LLM service unavailable.")
        resp.raise_for_status()
        return resp.json()

    # ── Brand Strategy (LLM proxy) ──

    async def analyze_brand_strategy(
        self,
        profile: dict[str, Any],
        posts: list[dict],
        ssi_data: dict[str, Any],
        campaign_stats: dict[str, Any],
        icp_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Analyze brand strategy via the backend LLM proxy."""
        url = f"{self.base_url}/api/v1/llm/analyze-brand"
        payload = {
            "profile": profile,
            "posts": posts,
            "ssi_data": ssi_data,
            "campaign_stats": campaign_stats,
        }
        if icp_context:
            payload["icp_context"] = icp_context
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url)
        if resp.status_code == 401:
            raise UnipileAuthError("Backend JWT expired or invalid.")
        if resp.status_code == 503:
            raise UnipileError("Backend LLM service unavailable.")
        resp.raise_for_status()
        data = _ensure_dict(resp.json())
        return data.get("analysis", data)

    async def brand_generate(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.7,
        max_tokens: int = 4000,
    ) -> str:
        """Generic brand LLM call via backend Gemini API.

        Sends a pre-built prompt to the backend, returns raw LLM text.
        Used by generate_brand_plan/action to avoid needing a local API key.
        """
        url = f"{self.base_url}/api/v1/llm/brand-generate"
        payload = {
            "prompt": prompt,
            "system": system,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url)
        if resp.status_code == 401:
            raise UnipileAuthError("Backend JWT expired or invalid.")
        if resp.status_code in (502, 503):
            raise UnipileError(f"Backend LLM error: {resp.text[:200]}")
        resp.raise_for_status()
        return resp.json().get("text", "")

    # ── Profile Editing (proxy to Unipile via backend) ──

    async def update_profile_headline(
        self,
        account_id: str,
        provider_id: str,  # noqa: ARG002 — deprecated, kept for backward compat
        new_headline: str,
    ) -> dict[str, Any]:
        """Update LinkedIn profile headline via backend → Unipile edit endpoint."""
        url = f"{self.base_url}/api/v1/linkedin/edit-profile"
        payload = {"account_id": account_id, "headline": new_headline}
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
            if resp.status_code in (200, 201):
                data = _ensure_dict(resp.json())
                return {"success": data.get("success", True), "error": ""}
            return {"success": False, "error": f"Backend returned {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"success": False, "error": f"Backend request failed: {e}"}

    async def update_profile_summary(
        self,
        account_id: str,
        provider_id: str,  # noqa: ARG002 — deprecated, kept for backward compat
        new_summary: str,
    ) -> dict[str, Any]:
        """Update LinkedIn profile summary via backend → Unipile edit endpoint."""
        url = f"{self.base_url}/api/v1/linkedin/edit-profile"
        payload = {"account_id": account_id, "summary": new_summary}
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
            if resp.status_code in (200, 201):
                data = _ensure_dict(resp.json())
                return {"success": data.get("success", True), "error": ""}
            return {"success": False, "error": f"Backend returned {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"success": False, "error": f"Backend request failed: {e}"}

    async def upload_profile_photo(
        self,
        account_id: str,
        provider_id: str,  # noqa: ARG002 — deprecated, kept for backward compat
        image_bytes: bytes,
        content_type: str = "image/jpeg",
    ) -> dict[str, Any]:
        """Upload profile photo via backend → Unipile edit endpoint."""
        import base64

        url = f"{self.base_url}/api/v1/linkedin/edit-profile"
        payload = {
            "image_base64": base64.b64encode(image_bytes).decode(),
            "image_content_type": content_type,
        }
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
            if resp.status_code in (200, 201):
                data = _ensure_dict(resp.json())
                return {"success": data.get("success", True), "error": ""}
            return {"success": False, "error": f"Backend returned {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"success": False, "error": f"Backend request failed: {e}"}

    async def upload_cover_photo(
        self,
        account_id: str,
        provider_id: str,  # noqa: ARG002
        image_bytes: bytes,
        content_type: str = "image/jpeg",
    ) -> dict[str, Any]:
        """Upload cover/banner photo via backend → Unipile edit endpoint."""
        import base64

        url = f"{self.base_url}/api/v1/linkedin/edit-profile"
        payload = {
            "cover_image_base64": base64.b64encode(image_bytes).decode(),
            "cover_image_content_type": content_type,
        }
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
            if resp.status_code in (200, 201):
                data = _ensure_dict(resp.json())
                return {"success": data.get("success", True), "error": ""}
            return {"success": False, "error": f"Backend returned {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"success": False, "error": f"Backend request failed: {e}"}

    async def update_custom_link(
        self,
        account_id: str,
        provider_id: str,  # noqa: ARG002
        category: str,
        url_value: str,
    ) -> dict[str, Any]:
        """Set a custom profile link via backend → Unipile edit endpoint."""
        url = f"{self.base_url}/api/v1/linkedin/edit-profile"
        payload = {"custom_link": {"category": category, "url": url_value}}
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
            if resp.status_code in (200, 201):
                data = _ensure_dict(resp.json())
                return {"success": data.get("success", True), "error": ""}
            return {"success": False, "error": f"Backend returned {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"success": False, "error": f"Backend request failed: {e}"}

    async def update_location(
        self,
        account_id: str,
        provider_id: str,  # noqa: ARG002
        location_id: str,
    ) -> dict[str, Any]:
        """Update profile location via backend → Unipile edit endpoint."""
        url = f"{self.base_url}/api/v1/linkedin/edit-profile"
        payload = {"location_id": location_id}
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
            if resp.status_code in (200, 201):
                data = _ensure_dict(resp.json())
                return {"success": data.get("success", True), "error": ""}
            return {"success": False, "error": f"Backend returned {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"success": False, "error": f"Backend request failed: {e}"}

    async def update_skills(
        self,
        account_id: str,
        provider_id: str,  # noqa: ARG002
        skills: list[str],
    ) -> dict[str, Any]:
        """Add skills via backend → Unipile edit endpoint."""
        url = f"{self.base_url}/api/v1/linkedin/edit-profile"
        payload = {"skills": skills}
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
            if resp.status_code in (200, 201):
                data = _ensure_dict(resp.json())
                return {"success": data.get("success", True), "error": ""}
            return {"success": False, "error": f"Backend returned {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"success": False, "error": f"Backend request failed: {e}"}

    async def update_skills_follow(
        self,
        account_id: str,
        provider_id: str,  # noqa: ARG002
        skills_follow: bool,
    ) -> dict[str, Any]:
        """Enable/disable skills follow via backend → Unipile edit endpoint."""
        url = f"{self.base_url}/api/v1/linkedin/edit-profile"
        payload = {"skills_follow": skills_follow}
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
            if resp.status_code in (200, 201):
                data = _ensure_dict(resp.json())
                return {"success": data.get("success", True), "error": ""}
            return {"success": False, "error": f"Backend returned {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"success": False, "error": f"Backend request failed: {e}"}

    async def update_experience(
        self,
        account_id: str,
        provider_id: str,  # noqa: ARG002
        experience: dict,
    ) -> dict[str, Any]:
        """Add/edit experience entry via backend → Unipile edit endpoint."""
        url = f"{self.base_url}/api/v1/linkedin/edit-profile"
        payload = {"experience": experience}
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
            if resp.status_code in (200, 201):
                data = _ensure_dict(resp.json())
                return {"success": data.get("success", True), "error": ""}
            return {"success": False, "error": f"Backend returned {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"success": False, "error": f"Backend request failed: {e}"}

    async def update_education(
        self,
        account_id: str,
        provider_id: str,  # noqa: ARG002
        education: dict,
    ) -> dict[str, Any]:
        """Add/edit education entry via backend → Unipile edit endpoint."""
        url = f"{self.base_url}/api/v1/linkedin/edit-profile"
        payload = {"education": education}
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
            if resp.status_code in (200, 201):
                data = _ensure_dict(resp.json())
                return {"success": data.get("success", True), "error": ""}
            return {"success": False, "error": f"Backend returned {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"success": False, "error": f"Backend request failed: {e}"}

    async def update_picture_settings(
        self,
        account_id: str,
        provider_id: str,  # noqa: ARG002
        settings: dict,
    ) -> dict[str, Any]:
        """Update profile picture settings via backend → Unipile edit endpoint."""
        url = f"{self.base_url}/api/v1/linkedin/edit-profile"
        payload = {"picture_settings": settings}
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
            if resp.status_code in (200, 201):
                data = _ensure_dict(resp.json())
                return {"success": data.get("success", True), "error": ""}
            return {"success": False, "error": f"Backend returned {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"success": False, "error": f"Backend request failed: {e}"}

    async def update_cover_picture_settings(
        self,
        account_id: str,
        provider_id: str,  # noqa: ARG002
        settings: dict,
    ) -> dict[str, Any]:
        """Update cover picture settings via backend → Unipile edit endpoint."""
        url = f"{self.base_url}/api/v1/linkedin/edit-profile"
        payload = {"cover_picture_settings": settings}
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
            if resp.status_code in (200, 201):
                data = _ensure_dict(resp.json())
                return {"success": data.get("success", True), "error": ""}
            return {"success": False, "error": f"Backend returned {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"success": False, "error": f"Backend request failed: {e}"}

    async def update_open_to_work(
        self,
        account_id: str,
        provider_id: str,  # noqa: ARG002
        settings: dict,
    ) -> dict[str, Any]:
        """Update Open to Work settings via backend → Unipile edit endpoint."""
        url = f"{self.base_url}/api/v1/linkedin/edit-profile"
        payload = {"open_to_work": settings}
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
            if resp.status_code in (200, 201):
                data = _ensure_dict(resp.json())
                return {"success": data.get("success", True), "error": ""}
            return {"success": False, "error": f"Backend returned {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"success": False, "error": f"Backend request failed: {e}"}

    # ── Filter Candidates (LLM proxy for enrichment) ──

    async def filter_candidates(self, prompt: str) -> str | None:
        """Run a filter_candidates prompt through the backend LLM proxy.

        Used by linkedin_enricher when no local LLM key is available.
        Returns raw LLM response text (JSON) or None on failure.
        """
        url = f"{self.base_url}/api/v1/llm/filter-candidates"
        payload = {"prompt": prompt}
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            logger.debug(f"filter_candidates connection error: {e}")
            return None
        if resp.status_code == 503:
            logger.debug("Backend GEMINI_API_KEY not configured — filter_candidates unavailable")
            return None
        if resp.status_code == 401:
            logger.debug("Backend JWT expired for filter_candidates")
            return None
        if resp.status_code != 200:
            logger.debug(f"filter_candidates failed: HTTP {resp.status_code}")
            return None
        data = _ensure_dict(resp.json())
        return data.get("text") or None

    # ── News Search Proxy ──

    async def search_news(
        self,
        query: str,
        num_results: int = 3,
        time_range: str = "pastMonth",
    ) -> list[dict[str, Any]]:
        """Search for news articles via the backend SERPER proxy.

        Args:
            query: Search query string.
            num_results: Number of results to return (1-10).
            time_range: Time range filter: "pastWeek", "pastMonth", "pastYear".

        Returns:
            List of news items with title, link, snippet, date, source.
            Returns empty list on error (graceful fallback).
        """
        url = f"{self.base_url}/api/v1/news/search"
        payload = {
            "query": query,
            "num_results": num_results,
            "time_range": time_range,
        }
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            logger.warning(f"News search connection error: {e}")
            return []
        if resp.status_code == 503:
            logger.info("Backend SERPER_API_KEY not configured — no news available")
            return []
        if resp.status_code == 401:
            logger.warning("Backend JWT expired for news search")
            return []
        if resp.status_code != 200:
            logger.warning(f"News search failed: HTTP {resp.status_code}")
            return []
        data = _ensure_dict(resp.json())
        return data.get("news", [])

    async def _chat_id_from_attendee(
        self, account_id: str, identifier: str, *, raise_on_error: bool,
    ) -> str | None:
        """The chat with this attendee, whatever its age.

        Twin of the UnipileClient method — see it for why the recent-chat scan
        needs a fallback that is scoped to the person rather than to recency.
        The backend proxies the same endpoint under /api/v1/linkedin.
        """
        url = (
            f"{self.base_url}/api/v1/linkedin/chat_attendees/{identifier}"
            f"/messages?limit=1"
        )
        try:
            resp = await self._client.get(url, headers=self._headers())
        except Exception as e:
            logger.warning("Attendee chat lookup failed for %s: %s", identifier, e)
            if raise_on_error:
                raise ChatLookupUnavailable(f"attendee chat lookup failed: {e}") from e
            return None
        if resp.status_code in (400, 404):
            return None
        if resp.status_code != 200:
            if raise_on_error:
                raise ChatLookupUnavailable(
                    f"attendee chat lookup returned HTTP {resp.status_code}"
                )
            return None
        try:
            data = resp.json()
        except Exception as e:
            if raise_on_error:
                raise ChatLookupUnavailable(
                    f"attendee chat lookup returned an unreadable body: {e}"
                ) from e
            return None
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("items") or data.get("messages") or data.get("data") or []
        else:
            if raise_on_error:
                raise ChatLookupUnavailable(
                    f"attendee chat lookup returned {type(data).__name__}, not messages"
                )
            return None
        for msg in items:
            if not isinstance(msg, dict):
                continue
            chat_id = msg.get("chat_id") or msg.get("conversation_id") or ""
            if chat_id:
                return str(chat_id)
        return None

    async def find_chat_for_user(
        self,
        account_id: str,
        prospect_linkedin_id: str,
        *,
        raise_on_error: bool = False,
        preferred_chat_id: str | None = None,
    ) -> str | None:
        """Find the chat_id for a conversation with a specific prospect.

        Read-only lookup: scan recent chats (up to 3 pages × 50) for a matching
        attendee. There is no create-or-resolve fallback — see the note on
        _chat_fallback_notice_logged. Callers already treat None as "no chat
        yet" and either start one with send_new_message() or skip and retry.

        Args:
            account_id: The user's Unipile account ID.
            prospect_linkedin_id: The prospect's LinkedIn provider_id.
            raise_on_error: Raise ChatLookupUnavailable when the scan could not
                run — transport failure, non-200, unreadable body — instead of
                returning the same None that means "no chat". Off by default so
                the callers that only want to start a chat keep the contract
                they read. See the UnipileClient twin for why the one caller
                that opts in must not be given a permissive answer.
            preferred_chat_id: Previously persisted outreach.chat_id. When set,
                skip the inbox scan.

        Returns:
            chat_id if found, None otherwise. The recent-chat scan is backed by
            an attendee-scoped lookup with no recency horizon, so None means no
            thread at any age rather than none in the pages reached.
        """
        from .chat_lookup_cache import get_cached_chat, store_cached_chat

        stored = (preferred_chat_id or "").strip()
        if stored:
            store_cached_chat(account_id, prospect_linkedin_id, stored)
            return stored
        hit, cached = get_cached_chat(account_id, prospect_linkedin_id)
        if hit:
            return cached
        result = await self._find_chat_for_user_uncached(
            account_id, prospect_linkedin_id, raise_on_error=raise_on_error,
        )
        store_cached_chat(account_id, prospect_linkedin_id, result)
        return result

    async def _find_chat_for_user_uncached(
        self,
        account_id: str,
        prospect_linkedin_id: str,
        *,
        raise_on_error: bool = False,
    ) -> str | None:
        # ── Phase 1: scan recent chats (paginated, up to 3 pages) ──
        url = f"{self.base_url}/api/v1/chats"
        cursor: str | None = None
        MAX_PAGES = 3

        try:
            for _page in range(MAX_PAGES):
                params: dict[str, str] = {"limit": "50"}
                if cursor:
                    params["cursor"] = cursor
                try:
                    resp = await self._client.get(url, params=params, headers=self._headers())
                except (httpx.ConnectError, httpx.TimeoutException) as e:
                    logger.warning(f"find_chat_for_user connection error: {e}")
                    if raise_on_error:
                        raise ChatLookupUnavailable(f"chat scan failed: {e}") from e
                    break
                if resp.status_code != 200:
                    if raise_on_error:
                        raise ChatLookupUnavailable(
                            f"chat scan returned HTTP {resp.status_code}"
                        )
                    break
                data = resp.json()

                if isinstance(data, list):
                    items = data
                elif isinstance(data, dict):
                    items = data.get("items") or data.get("chats") or data.get("data") or []
                else:
                    if raise_on_error:
                        raise ChatLookupUnavailable(
                            f"chat scan returned {type(data).__name__}, not a chat list"
                        )
                    break

                for chat in items:
                    if not isinstance(chat, dict):
                        continue
                    chat_id = chat.get("id") or chat.get("chat_id") or ""
                    if not chat_id:
                        continue

                    # Check attendee_provider_id (flat field) or attendees (list)
                    att_pid = chat.get("attendee_provider_id") or ""
                    if str(att_pid) == str(prospect_linkedin_id):
                        return str(chat_id)
                    attendees = chat.get("attendees") or []
                    for att in attendees:
                        if not isinstance(att, dict):
                            continue
                        for key in ("provider_id", "public_identifier", "id"):
                            att_id = att.get(key, "")
                            if att_id and str(att_id) == str(prospect_linkedin_id):
                                return str(chat_id)

                # Advance cursor for next page
                next_cursor = None
                if isinstance(data, dict):
                    next_cursor = data.get("cursor") or data.get("next_cursor") or data.get("paging", {}).get("cursor")
                if not next_cursor or not items:
                    break
                cursor = next_cursor

        except ChatLookupUnavailable:
            raise
        except Exception as e:
            logger.warning(f"find_chat_for_user scan failed: {e}")
            if raise_on_error:
                raise ChatLookupUnavailable(f"chat scan failed: {e}") from e

        # Not in the recent pages is not the same as not existing. Ask the
        # attendee endpoint before giving up, so an old thread stops reading as
        # a first contact to the pre-send guard.
        from_attendee = await self._chat_id_from_attendee(
            account_id, prospect_linkedin_id, raise_on_error=raise_on_error,
        )
        if from_attendee:
            return from_attendee

        # Both routes came up empty, so this is a real "no thread": the scan
        # covered the recent pages and the attendee endpoint covered every age.
        # Still no POST /chats fallback — it can only 422, because the endpoint
        # requires a 'text' body, i.e. actually sending a DM.
        global _chat_fallback_notice_logged
        if not _chat_fallback_notice_logged:
            _chat_fallback_notice_logged = True
            logger.warning(
                "find_chat_for_user: no chat in the %d most recent pages and none "
                "from the attendee endpoint either, so this reads as no thread. "
                "The POST /api/v1/chats fallback stays disabled (it requires a "
                "'text' body, i.e. sending a DM).",
                MAX_PAGES,
            )
        else:
            logger.debug(
                "find_chat_for_user: no chat found for %s (scan of %d pages, then "
                "the attendee endpoint)",
                str(prospect_linkedin_id)[:40], MAX_PAGES,
            )
        return None

    async def mark_chat_read(self, chat_id: str) -> dict[str, Any]:
        """Mark a chat as read via backend proxy."""
        url = f"{self.base_url}/api/v1/chats/{chat_id}/read"
        try:
            resp = await self._client.patch(url, headers=self._headers())
            if resp.status_code == 429:
                _raise_rate_limit(resp)
            resp.raise_for_status()
            return resp.json() if resp.text else {"ok": True}
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            return {"ok": False, "error": str(_wrap_connection_error(e, self.base_url))}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def archive_chat(self, chat_id: str) -> dict[str, Any]:
        """Archive a chat via backend proxy."""
        url = f"{self.base_url}/api/v1/chats/{chat_id}/archive"
        try:
            resp = await self._client.patch(url, headers=self._headers())
            if resp.status_code == 429:
                _raise_rate_limit(resp)
            resp.raise_for_status()
            return resp.json() if resp.text else {"ok": True}
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            return {"ok": False, "error": str(_wrap_connection_error(e, self.base_url))}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def reconnect_account(self, account_id: str) -> dict[str, Any]:
        """Reconnect a disconnected account via backend proxy."""
        url = f"{self.base_url}/api/v1/accounts/{account_id}/reconnect"
        try:
            resp = await self._post(url, headers=self._headers())
            if resp.status_code == 429:
                _raise_rate_limit(resp)
            resp.raise_for_status()
            return resp.json()
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url) from e
        except UnipileError:
            raise
        except Exception as e:
            raise UnipileError(f"Reconnect failed: {e}") from e

    async def handle_checkpoint(self, account_id: str, code: str = "") -> dict[str, Any]:
        """Handle 2FA/checkpoint challenge via backend proxy."""
        url = f"{self.base_url}/api/v1/accounts/{account_id}/checkpoint"
        body: dict[str, Any] = {}
        if code:
            body["code"] = code
        try:
            resp = await self._post(url, headers=self._headers(), json=body)
            if resp.status_code == 429:
                _raise_rate_limit(resp)
            resp.raise_for_status()
            return resp.json()
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url) from e
        except UnipileError:
            raise
        except Exception as e:
            raise UnipileError(f"Checkpoint failed: {e}") from e

    async def resync_account(self, account_id: str) -> dict[str, Any]:
        """Trigger account data resync via backend proxy."""
        url = f"{self.base_url}/api/v1/accounts/{account_id}/resync"
        try:
            resp = await self._client.get(url, headers=self._headers())
            if resp.status_code == 429:
                _raise_rate_limit(resp)
            resp.raise_for_status()
            return resp.json()
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url) from e
        except UnipileError:
            raise
        except Exception as e:
            raise UnipileError(f"Resync failed: {e}") from e

    async def create_post(
        self,
        account_id: str,
        text: str,
        image: tuple[str, bytes, str] | None = None,
    ) -> dict[str, Any]:
        """Create a LinkedIn post via backend proxy, with an optional image.

        *image* is (filename, bytes, mime) from services.post_media. It goes
        as multipart so the proxy can hand the file straight to Unipile;
        text-only stays JSON, the shape every post has used until now.
        """
        url = f"{self.base_url}/api/v1/posts"
        try:
            if image:
                filename, data, mime = image
                resp = await self._post(
                    url,
                    data={"text": text},
                    files={"attachments": (filename, data, mime)},
                    headers=multipart_headers(self._headers()),
                )
            else:
                resp = await self._post(
                    url, json={"text": text}, headers=self._headers(),
                )
            if resp.status_code == 429:
                _raise_rate_limit(resp)
            resp.raise_for_status()
            data = _ensure_dict(resp.json())
            return {"success": True, "post_id": data.get("id") or data.get("post_id") or "", "error": ""}
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url) from e
        except UnipileError:
            raise
        except Exception as e:
            return {"success": False, "error": str(e), "post_id": ""}

    async def upload_content_photo(
        self, name: str, data: bytes, mime: str,
    ) -> dict[str, Any]:
        """Add one photo to the hosted Content library the drafter consumes."""
        url = f"{self.base_url}/api/v1/content/photos"
        try:
            resp = await self._post(
                url,
                files={"photos": (name, data, mime)},
                headers=multipart_headers(self._headers()),
            )
            if resp.status_code == 429:
                _raise_rate_limit(resp)
            resp.raise_for_status()
            return _ensure_dict(resp.json())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url) from e

    async def get_post_comments(
        self, account_id: str, post_id: str, limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Get comments on a LinkedIn post via backend proxy."""
        # Strip URN prefix — send raw numeric ID in URL path (backend adds URN for Unipile)
        if post_id and post_id.startswith("urn:li:"):
            post_id = post_id.split(":")[-1]
        url = f"{self.base_url}/api/v1/posts/{post_id}/comments?limit={limit}"
        try:
            resp = await self._client.get(url, headers=self._headers())
            if resp.status_code == 429:
                _raise_rate_limit(resp)
            resp.raise_for_status()
            data = resp.json()
            items = data if isinstance(data, list) else data.get("items", [])
            # The backend proxies Unipile's raw Comment objects. Returning them
            # untouched made this method answer with a different shape than
            # UnipileClient's — no comment_id, no author_id — so every caller
            # that read those keys silently got nothing in hosted mode.
            return [
                _normalize_comment(item)
                for item in items
                if isinstance(item, dict)
            ]
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url) from e
        except UnipileError:
            raise
        except Exception as e:
            logger.warning("get_post_comments failed: %s", e)
            return []

    async def get_post_reactions(
        self, account_id: str, post_id: str, limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Get reactions on a LinkedIn post via backend proxy."""
        # Strip URN prefix — send raw numeric ID in URL path (backend adds URN for Unipile)
        if post_id and post_id.startswith("urn:li:"):
            post_id = post_id.split(":")[-1]
        url = f"{self.base_url}/api/v1/posts/{post_id}/reactions?limit={limit}"
        try:
            resp = await self._client.get(url, headers=self._headers())
            if resp.status_code == 429:
                _raise_rate_limit(resp)
            resp.raise_for_status()
            data = resp.json()
            items = data if isinstance(data, list) else data.get("items", [])
            reactions: list[dict[str, Any]] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                author = item.get("author") or {}
                if isinstance(author, str):
                    author = {"name": author}
                reactions.append({
                    "author_id": str(author.get("provider_id") or author.get("id") or item.get("provider_id") or ""),
                    "author_name": author.get("name") or item.get("name") or "",
                    "author_headline": author.get("headline") or "",
                    "reaction_type": item.get("reaction_type") or item.get("type") or "",
                    "timestamp": item.get("timestamp") or item.get("created_at") or "",
                })
            return reactions
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url) from e
        except UnipileError:
            raise
        except Exception as e:
            logger.warning("get_post_reactions failed: %s", e)
            return []

    async def qualify_inbound(
        self,
        profile: dict[str, Any],
        content: str,
        signal_type: str,
        icp_summaries: list[dict[str, Any]],
        our_last_message: str = "",
    ) -> Any:
        """Qualify an inbound signal via the backend LLM proxy."""
        from ..ai.inbound_qualifier import InboundQualification

        url = f"{self.base_url}/api/v1/llm/qualify-inbound"
        payload = {
            "profile": profile,
            "content": content,
            "signal_type": signal_type,
            "icp_summaries": icp_summaries,
            "our_last_message": our_last_message or "",
        }
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url)
        if resp.status_code == 401:
            raise UnipileAuthError("Backend JWT expired or invalid.")
        if resp.status_code == 503:
            raise UnipileError("Backend LLM service unavailable.")
        resp.raise_for_status()
        data = _ensure_dict(resp.json())
        return InboundQualification(
            intent=data.get("intent", "unknown"),
            matched_icp_id=data.get("matched_icp_id"),
            confidence=float(data.get("confidence", 0.3)),
            recommended_action=data.get("recommended_action", "ask_purpose"),
            reasoning=data.get("reasoning", ""),
        )

    async def generate_discovery_dm(
        self,
        profile: dict[str, Any],
        signal_type: str,
        content: str,
        voice: dict[str, Any],
        qualification: dict[str, Any] | None = None,
        sender_headline: str = "",
    ) -> dict[str, str]:
        """Generate a discovery DM via the backend LLM proxy."""
        url = f"{self.base_url}/api/v1/llm/generate-discovery"
        payload = {
            "profile": profile,
            "signal_type": signal_type,
            "content": content,
            "voice": voice,
            "qualification": qualification or {},
            "sender_headline": sender_headline,
        }
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url)
        if resp.status_code == 401:
            raise UnipileAuthError("Backend JWT expired or invalid.")
        if resp.status_code == 503:
            raise UnipileError("Backend LLM service unavailable.")
        resp.raise_for_status()
        data = _ensure_dict(resp.json())
        return {
            "message": data.get("message", ""),
            "reasoning": data.get("reasoning", ""),
        }

    async def generate_counter_pitch(
        self,
        profile: dict[str, Any],
        content: str,
        voice: dict[str, Any],
        campaign_context: dict[str, Any] | None = None,
        sender_headline: str = "",
    ) -> dict[str, str]:
        """Generate a counter-pitch reply via the backend LLM proxy."""
        url = f"{self.base_url}/api/v1/llm/generate-counter-pitch"
        payload = {
            "profile": profile,
            "content": content,
            "voice": voice,
            "campaign_context": campaign_context or {},
            "sender_headline": sender_headline,
        }
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url)
        if resp.status_code == 401:
            raise UnipileAuthError("Backend JWT expired or invalid.")
        if resp.status_code == 503:
            raise UnipileError("Backend LLM service unavailable.")
        resp.raise_for_status()
        data = _ensure_dict(resp.json())
        return {
            "message": data.get("message", ""),
            "reasoning": data.get("reasoning", ""),
        }

    async def classify_signal(
        self,
        content: str,
        author_name: str = "",
        author_headline: str = "",
        author_company: str = "",
        icp_summaries: list[dict[str, Any]] | None = None,
    ) -> Any:
        """Classify a signal's intent via the backend LLM proxy."""
        from ..ai.signal_classifier import SignalClassification

        url = f"{self.base_url}/api/v1/llm/classify-signal"
        payload = {
            "content": content,
            "author_name": author_name,
            "author_headline": author_headline,
            "author_company": author_company,
            "icp_summaries": icp_summaries or [],
        }
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url)
        if resp.status_code == 401:
            raise UnipileAuthError("Backend JWT expired or invalid.")
        if resp.status_code == 503:
            raise UnipileError("Backend LLM service unavailable.")
        resp.raise_for_status()
        data = _ensure_dict(resp.json())
        return SignalClassification(
            intent=data.get("intent", "unknown"),
            confidence=float(data.get("confidence", 0.3)),
            pain_points_detected=data.get("pain_points_detected", []),
            keywords_matched=data.get("keywords_matched", []),
            engagement_hook=data.get("engagement_hook", ""),
            reasoning=data.get("reasoning", ""),
        )

    async def classify_seller(
        self,
        messages: list[str],
        author_name: str = "",
        author_headline: str = "",
    ) -> dict[str, Any]:
        """Detect if a prospect is a LinkedIn seller via the backend LLM proxy."""
        url = f"{self.base_url}/api/v1/llm/classify-seller"
        payload = {
            "messages": messages,
            "author_name": author_name,
            "author_headline": author_headline,
        }
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url)
        if resp.status_code == 401:
            raise UnipileAuthError("Backend JWT expired or invalid.")
        if resp.status_code == 503:
            raise UnipileError("Backend LLM service unavailable.")
        resp.raise_for_status()
        return resp.json()

    async def analyze_post(
        self,
        post_text: str,
        author_info: dict[str, Any] | None = None,
    ) -> Any:
        """Analyze a post's topic/sentiment via the backend LLM proxy."""
        from ..ai.post_analyzer import PostAnalysis

        url = f"{self.base_url}/api/v1/llm/analyze-post"
        payload = {
            "post_text": post_text,
            "author_info": author_info or {},
        }
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url)
        if resp.status_code == 401:
            raise UnipileAuthError("Backend JWT expired or invalid.")
        if resp.status_code == 503:
            raise UnipileError("Backend LLM service unavailable.")
        resp.raise_for_status()
        data = _ensure_dict(resp.json())
        return PostAnalysis(
            topic=data.get("topic", "unknown"),
            subtopics=data.get("subtopics", []),
            sentiment=data.get("sentiment", "neutral"),
            key_themes=data.get("key_themes", []),
            pain_points=data.get("pain_points", []),
            buying_signals=data.get("buying_signals", []),
            engagement_hook=data.get("engagement_hook", ""),
        )

    # ── Comment reply drafts ──

    async def list_comment_drafts(self) -> list[dict[str, Any]]:
        """Reply drafts waiting for approval. Nothing here has been sent."""
        url = f"{self.base_url}/api/v1/comment-drafts"
        try:
            resp = await self._client.get(url, headers=self._headers())
            resp.raise_for_status()
            data = _ensure_dict(resp.json())
            return list(data.get("drafts") or [])
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url) from e
        except Exception as e:
            logger.warning("list_comment_drafts failed: %s", e)
            return []

    async def approve_comment_draft(self, draft_id: str, text: str = "") -> dict[str, Any]:
        """Send one drafted reply. The only call in this feature that posts.

        *text* replaces the draft, so approving an edit is the same action as
        approving what was generated.
        """
        url = f"{self.base_url}/api/v1/comment-drafts/{draft_id}/approve"
        try:
            resp = await self._post(url, json={"text": text} if text else {},
                                    headers=self._headers())
            resp.raise_for_status()
            return _ensure_dict(resp.json())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url) from e

    async def discard_comment_draft(self, draft_id: str) -> dict[str, Any]:
        """Throw a draft away without sending it."""
        url = f"{self.base_url}/api/v1/comment-drafts/{draft_id}/discard"
        try:
            resp = await self._post(url, json={}, headers=self._headers())
            resp.raise_for_status()
            return _ensure_dict(resp.json())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url) from e

    async def backfill_published_posts(self) -> dict[str, Any]:
        """Record posts that predate published_posts so they can be monitored."""
        url = f"{self.base_url}/api/v1/posts/backfill"
        try:
            resp = await self._post(url, json={}, headers=self._headers())
            resp.raise_for_status()
            return _ensure_dict(resp.json())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url) from e

    async def reply_to_comment(
        self,
        account_id: str,
        post_id: str,
        comment_id: str,
        text: str,
        mentions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Reply to a comment on a LinkedIn post via backend proxy.

        Sends Unipile's own field name, `comment_id` — `parent_comment_id` is
        not read, so replies were landing at top level. *mentions* is
        [{name, profile_id}], referenced from the text as "{{0}}".
        """
        url = f"{self.base_url}/api/v1/posts/{post_id}/comments"
        body: dict[str, Any] = {"text": text, "comment_id": comment_id}
        if mentions:
            body["mentions"] = mentions
        try:
            resp = await self._post(url, json=body, headers=self._headers())
            if resp.status_code == 429:
                _raise_rate_limit(resp)
            resp.raise_for_status()
            return {"success": True, "error": ""}
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url) from e
        except UnipileError:
            raise
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── Network Intelligence Layer ──

    async def network_pool_opt_in(self, account_id: str = "") -> dict[str, Any]:
        """Opt the user's LinkedIn account into the network pool."""
        url = f"{self.base_url}/api/v1/network/pool/opt-in"
        try:
            resp = await self._post(
                url, json={"account_id": account_id}, headers=self._headers(),
            )
            resp.raise_for_status()
            return resp.json()
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url) from e

    async def network_pool_opt_out(self) -> dict[str, Any]:
        """Opt the user's LinkedIn account out of the network pool."""
        url = f"{self.base_url}/api/v1/network/pool/opt-out"
        try:
            resp = await self._post(url, json={}, headers=self._headers())
            resp.raise_for_status()
            return resp.json()
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url) from e

    async def network_pool_status(self) -> dict[str, Any]:
        """Get network pool health dashboard."""
        url = f"{self.base_url}/api/v1/network/pool/status"
        try:
            resp = await self._client.get(url, headers=self._headers())
            resp.raise_for_status()
            return resp.json()
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url) from e

    async def network_pool_sync(self) -> dict[str, Any]:
        """Trigger connection graph sync for the user's pool account."""
        url = f"{self.base_url}/api/v1/network/pool/sync"
        try:
            resp = await self._post(url, json={}, headers=self._headers())
            resp.raise_for_status()
            return resp.json()
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url) from e

    async def network_pool_opt_in_all(self) -> dict[str, Any]:
        """Admin: opt in all connected LinkedIn accounts."""
        url = f"{self.base_url}/api/v1/network/pool/opt-in-all"
        try:
            resp = await self._post(url, json={}, headers=self._headers())
            resp.raise_for_status()
            return resp.json()
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url) from e

    async def network_pool_sync_all(self) -> dict[str, Any]:
        """Admin: sync connections for all pool accounts."""
        url = f"{self.base_url}/api/v1/network/pool/sync-all"
        try:
            resp = await self._post(
                url, json={}, headers=self._headers(),
                timeout=httpx.Timeout(300.0),  # long timeout for full sync
            )
            resp.raise_for_status()
            return resp.json()
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url) from e

    # ── Pool consumer routes ──
    #
    # These nine spend other members' seats and connection graphs, so the pool
    # is reciprocal about them: a workspace whose own LinkedIn seat is not an
    # active pool member is refused with 403. Each one runs the response past
    # _raise_if_pool_denied first so tools/network.py can print the remedy
    # instead of a bare HTTPStatusError. The pool/* membership routes above
    # (opt-in, opt-out, status, sync) stay open to everyone — a non-member has
    # to be able to reach them to become a member.

    async def network_enrich(
        self, linkedin_id: str, force_refresh: bool = False,
    ) -> dict[str, Any]:
        """Smart single-profile enrichment via closest-connected pool account.

        Pool members only: raises NetworkPoolNotMemberError when this
        workspace is not an active member of the reciprocal pool.
        """
        url = f"{self.base_url}/api/v1/network/enrich"
        try:
            resp = await self._post(
                url,
                json={"linkedin_id": linkedin_id, "force_refresh": force_refresh},
                headers=self._headers(),
            )
            _raise_if_pool_denied(resp)
            resp.raise_for_status()
            return resp.json()
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url) from e

    async def network_contact_info(self, linkedin_id: str) -> dict[str, Any]:
        """Get email/phone via a 1st-degree connected pool account.

        Pool members only: raises NetworkPoolNotMemberError when this
        workspace is not an active member of the reciprocal pool.
        """
        url = f"{self.base_url}/api/v1/network/contact-info"
        try:
            resp = await self._post(
                url, json={"linkedin_id": linkedin_id}, headers=self._headers(),
            )
            _raise_if_pool_denied(resp)
            resp.raise_for_status()
            return resp.json()
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url) from e

    async def network_parallel_enrich(
        self, linkedin_ids: list[str], max_concurrency: int = 10,
    ) -> dict[str, Any]:
        """Fan-out profile enrichment across pool accounts.

        Pool members only: raises NetworkPoolNotMemberError when this
        workspace is not an active member of the reciprocal pool.
        """
        url = f"{self.base_url}/api/v1/network/parallel-enrich"
        try:
            resp = await self._post(
                url,
                json={"linkedin_ids": linkedin_ids, "max_concurrency": max_concurrency},
                headers=self._headers(),
                timeout=httpx.Timeout(300.0),  # long timeout for 100 profiles
            )
            _raise_if_pool_denied(resp)
            resp.raise_for_status()
            return resp.json()
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url) from e

    async def network_search(
        self,
        keywords: str = "",
        title: str = "",
        max_accounts: int = 5,
        count_per_account: int = 25,
    ) -> dict[str, Any]:
        """Distributed search across pool accounts.

        Pool members only: raises NetworkPoolNotMemberError when this
        workspace is not an active member of the reciprocal pool.
        """
        url = f"{self.base_url}/api/v1/network/search"
        try:
            resp = await self._post(
                url,
                json={
                    "keywords": keywords,
                    "title": title,
                    "max_accounts": max_accounts,
                    "count_per_account": count_per_account,
                },
                headers=self._headers(),
                timeout=httpx.Timeout(120.0),
            )
            _raise_if_pool_denied(resp)
            resp.raise_for_status()
            return resp.json()
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url) from e

    async def network_reach(self, linkedin_id: str) -> dict[str, Any]:
        """Show which pool accounts can reach a prospect.

        Pool members only: raises NetworkPoolNotMemberError when this
        workspace is not an active member of the reciprocal pool.
        """
        url = f"{self.base_url}/api/v1/network/reach/{linkedin_id}"
        try:
            resp = await self._client.get(url, headers=self._headers())
            _raise_if_pool_denied(resp)
            resp.raise_for_status()
            return resp.json()
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url) from e

    async def network_intros(self, linkedin_id: str) -> dict[str, Any]:
        """Find warm introduction paths to a prospect through the pool.

        Pool members only: raises NetworkPoolNotMemberError when this
        workspace is not an active member of the reciprocal pool.
        """
        url = f"{self.base_url}/api/v1/network/intros/{linkedin_id}"
        try:
            resp = await self._client.get(url, headers=self._headers())
            _raise_if_pool_denied(resp)
            resp.raise_for_status()
            return resp.json()
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url) from e

    # ── Network Message Insights ──

    async def network_insights_query(
        self,
        insight_type: str = "",
        segment: str = "",
        min_confidence: float = 0.0,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Query aggregated message insights contributed by pool members.

        Pool members only: raises NetworkPoolNotMemberError when this
        workspace is not an active member of the reciprocal pool.
        """
        url = f"{self.base_url}/api/v1/network/insights/query"
        payload: dict[str, Any] = {"limit": limit}
        if insight_type:
            payload["insight_type"] = insight_type
        if segment:
            payload["segment"] = segment
        if min_confidence > 0:
            payload["min_confidence"] = min_confidence
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
            _raise_if_pool_denied(resp)
            resp.raise_for_status()
            return resp.json()
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url) from e

    async def network_insights_summary(self) -> dict[str, Any]:
        """Get insight dashboard summary.

        Pool members only: raises NetworkPoolNotMemberError when this
        workspace is not an active member of the reciprocal pool.
        """
        url = f"{self.base_url}/api/v1/network/insights/summary"
        try:
            resp = await self._client.get(url, headers=self._headers())
            _raise_if_pool_denied(resp)
            resp.raise_for_status()
            return resp.json()
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url) from e

    async def network_insights_refresh(self) -> dict[str, Any]:
        """Admin: trigger manual insight extraction cycle.

        Pool members only: raises NetworkPoolNotMemberError when this
        workspace is not an active member of the reciprocal pool.
        """
        url = f"{self.base_url}/api/v1/network/insights/refresh"
        try:
            resp = await self._post(
                url, json={}, headers=self._headers(), timeout=300.0,
            )
            _raise_if_pool_denied(resp)
            resp.raise_for_status()
            return resp.json()
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url) from e

    # ── Shared contact directory ──

    async def contribute_directory(self, cards: list[dict]) -> dict:
        """Upload public professional cards to the shared directory.

        A 404 means the API is not deployed yet — skip, do not fail the sync.
        """
        if not self.jwt_token:
            return {"upserted": 0, "skipped": len(cards), "unsupported": True}
        url = f"{self.base_url}/api/v1/directory/contribute"
        resp = await self._post(
            url, headers=self._headers(), json={"cards": cards},
            timeout=httpx.Timeout(300.0),
        )
        if resp.status_code == 404:
            return {"upserted": 0, "skipped": len(cards), "unsupported": True}
        resp.raise_for_status()
        return resp.json()

    async def pull_directory(self, since: int = 0, after_id: str = "", limit: int = 500) -> dict:
        """Pull public professional cards newer than the keyset cursor.

        A 404 means the API is not deployed yet — skip, do not fail the sync.
        """
        if not self.jwt_token:
            return {"cards": [], "next_since": since, "next_after_id": "", "unsupported": True}
        url = f"{self.base_url}/api/v1/directory/pull"
        resp = await self._client.get(
            url, headers=self._headers(),
            params={"since": since, "after_id": after_id, "limit": limit},
        )
        if resp.status_code == 404:
            return {"cards": [], "next_since": since, "next_after_id": "", "unsupported": True}
        resp.raise_for_status()
        return resp.json()

    # ── Google Calendar integration ──

    async def extract_meeting(
        self,
        messages: list[dict[str, Any]],
        current_date: str,
        user_timezone: str = "UTC",
    ) -> dict[str, Any]:
        """Extract meeting details from a conversation via backend LLM."""
        url = f"{self.base_url}/api/v1/llm/extract-meeting"
        payload = {
            "messages": messages,
            "current_date": current_date,
            "user_timezone": user_timezone,
        }
        try:
            resp = await self._post(url, json=payload, headers=self._headers())
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise _wrap_connection_error(e, self.base_url) from e
        if resp.status_code == 401:
            raise UnipileAuthError("Backend JWT expired or invalid.")
        resp.raise_for_status()
        return resp.json()
