"""Tool 1: setup_profile — Analyze your LinkedIn profile and generate a voice signature.

This is the #1 killer feature. Voice matching was the strongest positive
reaction in 7 of 12 customer discovery interviews. This is where users
think "OK, this AI actually gets me."

Supports two modes:

**Direct mode** (Unipile credentials in config):
  Call 1: setup_profile(llm_api_key="...") → auth link → user connects LinkedIn
  Call 2: setup_profile() → polls for account → profile fetch → voice analysis

**Backend mode** (backend_url + backend_jwt in config):
  Call 1: setup_profile(llm_api_key="...", backend_url="...", backend_jwt="...")
          → saves config → connects LinkedIn via backend proxy → profile + voice

If the account is already connected (re-run), skips straight to profile fetch.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx

from ..ai.voice_analyzer import analyze_voice
from ..config import (
    get_backend_config,
    get_unipile_config,
    is_backend_mode,
    load_config,
    save_config,
    set_active_org_id,
    set_backend_config,
)
from ..constants import DEFAULT_BACKEND_URL, GEMINI_KEY_URL, LOGIN_URL_PATH
from ..db.queries import get_setting, save_setting
from ..formatter import format_voice_signature
from ..linkedin import (
    UnipileAuthError,
    UnipileError,
    get_account_id,
    get_linkedin_client,
    set_account_id,
)
from ..db.async_bridge import run_db

logger = logging.getLogger(__name__)

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

# Test seam: None means the real network transport.
_TOKEN_CHECK_TRANSPORT: httpx.AsyncBaseTransport | None = None

# Where a setup message comes from. Both refusals end on this sentence.
WHERE_TO_COPY = (
    f"copy a fresh setup message from {DEFAULT_BACKEND_URL}{LOGIN_URL_PATH} "
    "(or Settings → Integrations → Chat client on the dashboard) and paste it here."
)

# First-time setup with an unreachable backend stores the token and carries on
# with no workspace selected — every later call is then implicit and
# the backend answers from users.default_org_id, which is the reported incident
# reached through a network blip. Say so, or the user cannot connect the two.
UNCONFIRMED_WORKSPACE_NOTICE = (
    "⚠️ Couldn't reach HeyLead to confirm which workspace this token belongs to, "
    "so HeyLead will use your default one. If that is the wrong workspace, run "
    "organization(action='list') and switch."
)


async def _check_backend_token(backend_url: str, jwt: str) -> tuple[str | None, dict]:
    """Ask the backend whether ``jwt`` is a HeyLead token, and whose workspace.

    Returns ``(problem, me)``:

    - ``(None, me)`` — the backend accepted it; ``me`` is its /api/v1/auth/me
      body (account bindings and the org the token resolves to).
    - ``("rejected", {})`` — the backend refused the token: 401 or 403 only.
    - ``("unreachable", {})`` — no answer about the token came back: a
      transport error, a 5xx, a 429, a redirect, any other status, or a 200
      that is not the /auth/me body. None of those says the token is bad.

    A body without an ``org_id`` is not the /auth/me body: the real route
    builds it from ctx.org_id, which resolve_org_context never leaves empty
    (it returns an existing org, the default org, or a freshly ensured
    personal one). A captive portal, a misrouted gateway or a future API
    answering ``{}`` or ``{"detail": ...}`` with HTTP 200 would otherwise
    count as an answer, and the caller would clear the user's workspace —
    leaving every later call implicit, which is the original bug.

    Only the Authorization header is sent: a stored X-Org-Id belongs to the
    old token's workspace and could get a perfectly good new token refused.
    """
    try:
        async with httpx.AsyncClient(
            timeout=15.0, transport=_TOKEN_CHECK_TRANSPORT,
        ) as client:
            resp = await client.get(
                f"{backend_url.rstrip('/')}/api/v1/auth/me",
                headers={"Authorization": f"Bearer {jwt}", "Accept": "application/json"},
            )
    except httpx.HTTPError as e:
        logger.warning("Could not check a backend token: %r", e)
        return "unreachable", {}
    if resp.status_code in (401, 403):
        logger.info("Backend refused a token (HTTP %s)", resp.status_code)
        return "rejected", {}
    if resp.status_code != 200:
        logger.warning(
            "Could not check a backend token (HTTP %s)", resp.status_code,
        )
        return "unreachable", {}
    try:
        me = resp.json()
    except ValueError:
        me = None
    if not isinstance(me, dict) or not str(me.get("org_id") or "").strip():
        logger.warning("Token check got HTTP 200 without the /auth/me body")
        return "unreachable", {}
    return None, me


async def run_setup_profile(
    llm_api_key: str = "",
    llm_provider: str = "",
    backend_url: str = "",
    backend_jwt: str = "",
) -> str:
    """Set up the user's profile by analyzing their LinkedIn presence.

    Args:
        llm_api_key: API key for the LLM provider.
        llm_provider: Which LLM to use ("gemini", "claude", "openai").
        backend_url: HeyLead Backend API URL (enables backend mode).
        backend_jwt: JWT from Google OAuth (required for backend mode).

    Returns:
        Formatted string showing the voice analysis results or next-step instructions.
    """

    # ── Step 0: Save any provided credentials ──
    cfg = load_config()
    # One line prefixed to whatever setup returns: which workspace this token
    # landed in, or that we could not find out.
    notice = ""

    if llm_api_key:
        provider = llm_provider or "gemini"
        cfg.setdefault("api_keys", {})[provider] = llm_api_key
        save_config(cfg)

    if backend_url or backend_jwt:
        current_url, current_jwt = get_backend_config()
        current_jwt = current_jwt.strip()
        new_jwt = backend_jwt.strip()
        target_url = backend_url.strip() or current_url
        if backend_url:
            parsed = urlparse(backend_url.strip())
            if parsed.scheme != "https" and parsed.hostname not in _LOCAL_HOSTS:
                return (
                    "❌ backend_url must use https.\n\n"
                    "The stored token authenticates your LinkedIn session — "
                    "it is never sent over plain http."
                )
            # A new host does not inherit the old host's token: carrying it
            # over would hand the credential to whoever supplied the URL. That
            # holds whether the token is left out or passed again explicitly.
            new_host = parsed.hostname != urlparse(current_url).hostname
            if new_host and (not new_jwt or (current_jwt and new_jwt == current_jwt)):
                return (
                    f"❌ Pointing HeyLead at {parsed.hostname} needs a token for "
                    "that backend.\n\n"
                    "  setup_profile(backend_url='...', backend_jwt='YOUR_TOKEN')"
                )
        # Ask the backend about every new (url, token) pair, first-time setup
        # included, for two reasons.
        #
        # (1) A token the backend refuses — a Supabase or Auth0 JWT pasted
        #     while debugging, say — used to be stored, fail account
        #     verification and clear the bound account; a self-hosted install
        #     was switched into backend mode the same way.
        # (2) /auth/me is the only thing that names a workspace for the token
        #     at all. Without asking, the client sends no X-Org-Id and the
        #     backend falls back to users.default_org_id — so a user who
        #     pressed "Set up in chat" inside a second workspace silently got
        #     their personal one, and its LinkedIn seat.
        #
        # The check used to run only when there was a setup to protect, which
        # is precisely the case a new user is not in.
        token_changed = bool(new_jwt) and new_jwt != current_jwt
        pair_changed = bool(new_jwt) and (
            token_changed or target_url.rstrip("/") != current_url.rstrip("/")
        )
        unipile_url, unipile_key = get_unipile_config()
        has_setup = bool(current_jwt) or bool(unipile_url and unipile_key)
        me: dict = {}
        named_a_workspace = False
        if pair_changed:
            problem, me = await _check_backend_token(target_url, new_jwt)
            named_a_workspace = problem is None
            if problem == "rejected":
                if has_setup:
                    return (
                        "❌ That token didn't work with HeyLead — your existing setup "
                        f"is unchanged.\n\nTo switch HeyLead accounts, {WHERE_TO_COPY}"
                    )
                return (
                    "❌ That token didn't work with HeyLead, so it wasn't saved.\n\n"
                    "It may have expired, or be a token for something else. To set "
                    f"HeyLead up, {WHERE_TO_COPY}"
                )
            # No answer, and a setup to protect: refusing costs nothing, what
            # works keeps working. With nothing to protect the same refusal
            # would lock a new user out over a blip, so that half stores the
            # token and carries on below — with the workspace left unset
            # rather than guessed, and the user told so.
            if problem == "unreachable":
                if has_setup:
                    return (
                        "❌ Couldn't reach HeyLead to check that token — your existing "
                        "setup is unchanged.\n\nTry again in a minute."
                    )
                notice = UNCONFIRMED_WORKSPACE_NOTICE
        set_backend_config(target_url, new_jwt or current_jwt)
        # /auth/me names one org for this token. Selecting it puts X-Org-Id on
        # every later call, so the backend never falls back to a default — and
        # it also retires any workspace left over from the old token, which
        # would otherwise have made the backend answer "Organization not found"
        # to everything, setup's own lookup included.
        #
        # Against an API that does not yet carry an org claim on the token,
        # that org is the backend's existing default rather than the workspace
        # the setup message was copied in — i.e. today's behaviour, now stated
        # explicitly instead of implied by silence. The workspace the message
        # names arrives with the API half (the `org` claim and `org_name`).
        if named_a_workspace:
            org_id = str(me["org_id"]).strip()
            set_active_org_id(org_id)
            workspace = str(me.get("org_name") or "").strip() or org_id
            notice = f"✅ Set up in workspace **{workspace}**."
            logger.info(
                "Setup selected the workspace the token names: %r (%r)",
                org_id, me.get("org_name") or "",
            )

    # ── Step 1: Check if we have any connection mode available ──
    # If neither backend JWT nor direct Unipile credentials exist, guide the user
    if not is_backend_mode():
        api_url, api_key = get_unipile_config()
        if not api_url or not api_key:
            login_url = f"{DEFAULT_BACKEND_URL}{LOGIN_URL_PATH}"
            return (
                "👋 Welcome to HeyLead! Let's get you set up (takes about 2 minutes).\n\n"
                "**Step 1** — Sign in and connect LinkedIn:\n"
                f"  👉 {login_url}\n"
                "  Sign in with Google, click 'Connect' on the LinkedIn row, then press "
                "'Copy' under 'Get Started' to copy your setup message.\n"
                "  Already signed in on the dashboard? Find it at "
                "Settings → Integrations → Chat client → 'Copy setup message'.\n\n"
                "**Step 2** — Paste that message back here. It carries your token, "
                "and setup finishes with:\n"
                "  setup_profile(backend_jwt='YOUR_TOKEN')\n\n"
                "That's it! No API keys needed — the hosted backend handles LinkedIn and AI."
            )

    # ── Step 2: Check LLM key ──
    # In backend mode, LLM calls are proxied through the backend — no local key needed.
    # Only require a local key for direct mode (self-hosted Unipile).
    if not is_backend_mode():
        cfg = load_config()  # Reload in case we just saved
        has_llm_key = any(v for v in cfg.get("api_keys", {}).values() if v)
        if not has_llm_key:
            return (
                "❌ No LLM API key configured.\n\n"
                "Please provide an API key:\n"
                "  setup_profile(llm_api_key='YOUR_KEY', llm_provider='gemini')\n\n"
                f"Get a free Gemini key at: {GEMINI_KEY_URL}"
            )

    # ── Step 3: Route to appropriate mode ──
    if is_backend_mode():
        body = await _setup_backend_mode()
    else:
        body = await _setup_direct_mode()
    return f"{notice}\n\n{body}" if notice else body


async def _setup_backend_mode() -> str:
    """Setup flow via HeyLead Backend API proxy."""
    client = get_linkedin_client()
    try:
        stored_account_id = await run_db(get_account_id)

        if stored_account_id:
            connected, status_msg = await client.verify_account(stored_account_id)
            if connected:
                return await _fetch_and_analyze(client, stored_account_id)
            else:
                logger.info(f"Stored account no longer connected: {status_msg}")
                await run_db(set_account_id, None)

        # ── Reconcile with backend ──
        # The webhook may have already bound an account on the backend
        # that we don't know about locally. Check before polling/auth-link.
        backend_account_id, _msg = await client.find_linkedin_account()
        if backend_account_id:
            connected, _status = await client.verify_account(backend_account_id)
            if connected:
                _prev = await run_db(get_setting, "unipile_account_id", "")
                await run_db(set_account_id, backend_account_id)
                await run_db(save_setting, "auth_link_pending", False)
                await run_db(save_setting, "known_account_ids_before_auth", [])
                return await _fetch_and_analyze(
                    client, backend_account_id, previous_account_id=_prev,
                )

        # Check if we're in "waiting for OAuth" state
        auth_link_pending = await run_db(get_setting, "auth_link_pending", False)

        if auth_link_pending:
            known_ids = set( await run_db(get_setting, "known_account_ids_before_auth", []))
            account_id, poll_msg = await client.poll_for_new_account(known_ids)

            if account_id:
                await run_db(set_account_id, account_id)
                await run_db(save_setting, "auth_link_pending", False)
                await run_db(save_setting, "known_account_ids_before_auth", [])
                return await _fetch_and_analyze(client, account_id)
            else:
                return (
                    f"⏳ {poll_msg}\n\n"
                    "Please make sure you:\n"
                    "1. Opened the auth link in your browser\n"
                    "2. Completed the LinkedIn login\n"
                    "3. Saw a success confirmation\n\n"
                    "Then run setup_profile() again to retry."
                )

        # Snapshot existing accounts, then create auth link via backend
        known_ids = await client.get_existing_account_ids()

        # Redirect user back to HeyLead after LinkedIn connects
        redirect_url = f"{DEFAULT_BACKEND_URL}/auth/linkedin-connected"

        try:
            auth_url = await client.create_hosted_auth_link(
                success_redirect_url=redirect_url,
            )
        except UnipileError as e:
            return f"❌ Failed to create LinkedIn auth link: {e}"

        await run_db(save_setting, "auth_link_pending", True)
        await run_db(save_setting, "known_account_ids_before_auth", list(known_ids))

        return (
            "🔗 Almost there! Open this link in your browser to connect LinkedIn:\n\n"
            f"  {auth_url}\n\n"
            "Steps:\n"
            "1. Click the link above (or copy-paste into your browser)\n"
            "2. Log in to LinkedIn when prompted\n"
            "3. Authorize the connection\n"
            "4. Come back here and run setup_profile() again\n\n"
            "⏱️ The link expires in 24 hours.\n"
            "🔒 Your credentials are handled securely by the HeyLead backend."
        )

    except UnipileError as e:
        return f"❌ Backend error: {e}"
    except Exception as e:
        logger.error(f"setup_profile (backend mode) failed: {e}", exc_info=True)
        return f"❌ Setup failed: {e}\n\nCheck ~/.heylead/logs/heylead.log for details."
    finally:
        await client.close()


async def _setup_direct_mode() -> str:
    """Setup flow with direct Unipile credentials (original behavior)."""
    from ..config import get_unipile_config
    from ..linkedin.unipile import UnipileClient

    api_url, api_key = get_unipile_config()

    if not api_url or not api_key:
        login_url = f"{DEFAULT_BACKEND_URL}{LOGIN_URL_PATH}"
        return (
            "No Unipile credentials found for direct mode.\n\n"
            "Recommended: Use backend mode instead (easier setup):\n"
            f"  1. Sign in at: {login_url}\n"
            "  2. Click 'Connect' on the LinkedIn row, then 'Copy' under 'Get Started'\n"
            "  3. Run: setup_profile(backend_jwt='YOUR_TOKEN')\n\n"
            "Advanced: For self-hosted Unipile, add to ~/.heylead/config.json:\n"
            '  "unipile_api_url": "https://apiXX.unipile.com:XXXXX"\n'
            '  "unipile_api_key": "YOUR_UNIPILE_API_KEY"'
        )

    client = UnipileClient(api_url, api_key)
    try:
        stored_account_id = await run_db(get_account_id)
        # A cleared dead account means the next link re-authenticates an
        # existing LinkedIn session, not a first-time connect.
        session_expired = False

        if stored_account_id:
            connected, status_msg = await client.verify_account(stored_account_id)
            if connected:
                return await _fetch_and_analyze(client, stored_account_id)
            else:
                logger.info(f"Stored account no longer connected: {status_msg}")
                await run_db(set_account_id, None)
                session_expired = True

        # Check if we're in "waiting for OAuth" state
        auth_link_pending = await run_db(get_setting, "auth_link_pending", False)

        if auth_link_pending:
            known_ids = set( await run_db(get_setting, "known_account_ids_before_auth", []))
            account_id, poll_msg = await client.poll_for_new_account(known_ids)

            if account_id:
                await run_db(set_account_id, account_id)
                await run_db(save_setting, "auth_link_pending", False)
                await run_db(save_setting, "known_account_ids_before_auth", [])
                return await _fetch_and_analyze(client, account_id)
            else:
                return (
                    f"⏳ {poll_msg}\n\n"
                    "Please make sure you:\n"
                    "1. Opened the auth link in your browser\n"
                    "2. Completed the LinkedIn login\n"
                    "3. Saw a success confirmation\n\n"
                    "Then run setup_profile() again to retry."
                )

        # Snapshot existing accounts, then create auth link
        known_ids = await client.get_existing_account_ids()

        # Redirect user back to HeyLead after LinkedIn connects
        redirect_url = f"{DEFAULT_BACKEND_URL}/auth/linkedin-connected"

        try:
            auth_url = await client.create_hosted_auth_link(
                success_redirect_url=redirect_url,
                is_reconnect=session_expired,
            )
        except UnipileError as e:
            return f"❌ Failed to create LinkedIn auth link: {e}"

        await run_db(save_setting, "auth_link_pending", True)
        await run_db(save_setting, "known_account_ids_before_auth", list(known_ids))

        return (
            "🔗 Almost there! Open this link in your browser to connect LinkedIn:\n\n"
            f"  {auth_url}\n\n"
            "Steps:\n"
            "1. Click the link above (or copy-paste into your browser)\n"
            "2. Log in to LinkedIn when prompted\n"
            "3. Authorize the connection\n"
            "4. Come back here and run setup_profile() again\n\n"
            "⏱️ The link expires in 24 hours.\n"
            "🔒 Your credentials are handled by Unipile — nothing stored locally."
        )

    except UnipileError as e:
        return f"❌ Unipile error: {e}"
    except Exception as e:
        logger.error(f"setup_profile failed: {e}", exc_info=True)
        return f"❌ Setup failed: {e}\n\nCheck ~/.heylead/logs/heylead.log for details."
    finally:
        await client.close()


async def _fetch_and_analyze(
    client, account_id: str, previous_account_id: str = "",
) -> str:
    """Fetch profile, analyze voice, store results, return formatted output.

    Works with both UnipileClient and BackendClient.

    ``previous_account_id`` is restored on every failure path when this is a
    SWITCH rather than a first-time setup. The callers commit the new account
    id before calling this, so without the restore a failure here left the id
    pointing at the new account while `profile`, `voice_signature` and
    `expertise_map` still described the old one — and `setup_complete` stays
    True, which is the only pre-check generate_send makes. The next scheduler
    tick then sent from the new account under the previous person's name and
    voice. switch_account was made atomic for exactly this; this is its twin,
    and the one its error messages used to send people to.

    Empty means first-time setup: there is nothing to go back to, so the
    existing behaviour stands.
    """
    async def _restore() -> None:
        if previous_account_id:
            await run_db(set_account_id, previous_account_id)

    # ── Fetch LinkedIn profile ──
    try:
        profile = await client.get_own_profile(account_id)
    except UnipileAuthError:
        await run_db(set_account_id, None)
        return (
            "❌ LinkedIn account disconnected.\n\n"
            "Run setup_profile() again to reconnect."
        )
    except Exception as e:
        logger.error(f"Failed to fetch LinkedIn profile: {e}")
        await _restore()
        return (
            f"❌ Failed to fetch your LinkedIn profile: {e}\n\n"
            "This might be a temporary issue. Try again in a minute."
        )

    if not profile.get("name"):
        await _restore()
        return (
            "❌ Could not read your LinkedIn profile.\n"
            "The connection might need to be refreshed. Run setup_profile() again."
        )

    # ── Fetch posts ──
    try:
        provider_id = profile.get("provider_id", "")
        posts = await client.get_posts(account_id, provider_id=provider_id)
        profile["posts"] = posts
    except Exception as e:
        logger.warning(f"Failed to fetch posts (non-fatal): {e}")
        profile["posts"] = []

    # ── Analyze voice with LLM ──
    try:
        analysis = await analyze_voice(profile)
    except Exception as e:
        logger.error(f"Voice analysis failed: {e}")
        await _restore()
        return (
            f"❌ Voice analysis failed: {e}\n\n"
            "Check your LLM API key in ~/.heylead/config.json\n"
            "Get a free Gemini key at: https://aistudio.google.com/apikey"
        )

    voice = analysis.get("voice", {})
    expertise = analysis.get("expertise", {})

    # ── Store in database ──
    await run_db(save_setting, "profile", profile)
    await run_db(save_setting, "voice_signature", voice)
    await run_db(save_setting, "expertise_map", expertise)

    # ── Generate Hume AI voice config from voice signature ──
    try:
        from ..ai.hume_voice import create_voice_config
        hume_voice_config = create_voice_config(voice)
        await run_db(save_setting, "hume_voice_config", hume_voice_config)
        logger.info("Hume voice config generated and saved")
    except Exception as e:
        logger.warning("Hume voice config generation failed (non-fatal): %s", e)

    await run_db(save_setting, "setup_complete", True)

    # ── Auto-create company watchlist for user's own company page ──
    try:
        own_company_name = profile.get("company", "")
        own_company_url = ""
        experience = profile.get("experience", [])
        if experience and isinstance(experience, list):
            current = experience[0] if experience else {}
            own_company_url = current.get("company_url", "") or current.get("company_linkedin_url", "")
            if not own_company_name:
                own_company_name = current.get("company", "")

        if own_company_name:
            from ..db.signal_queries import list_watchlists, save_watchlist
            import re as _re
            company_identifier = ""
            if own_company_url:
                _match = _re.search(r"linkedin\.com/company/([^/?#]+)", own_company_url)
                company_identifier = _match.group(1) if _match else ""
            if not company_identifier:
                company_identifier = own_company_name

            existing_company_wl = [
                w for w in await run_db(list_watchlists, is_active=True)
                if w.get("watch_type") == "company"
                and company_identifier in (w.get("keywords_list") or [])
            ]
            if not existing_company_wl:
                await run_db(save_watchlist, name=f"{own_company_name} (Your Company)",
                    watch_type="company",
                    keywords=[company_identifier],)
                logger.info("Auto-created company watchlist for %s", own_company_name)
    except Exception as e:
        logger.debug("Auto company watchlist creation failed (non-fatal): %s", e)

    # ── Auto-create keyword watchlists from user expertise ──
    try:
        from ..db.signal_queries import list_watchlists as _list_wl, save_watchlist as _save_wl

        existing_kws: set[str] = set()
        for wl in await run_db(_list_wl, is_active=True):
            for kw in wl.get("keywords_list", []):
                existing_kws.add(kw.lower().strip())

        # Core expertise → keyword watchlist
        core_raw = expertise.get("core", "")
        if core_raw:
            core_topics = [t.strip() for t in core_raw.split(",") if t.strip()]
            new_core = [t for t in core_topics if t.lower() not in existing_kws]
            if new_core:
                await run_db(
                    _save_wl,
                    name="My Expertise — Core Topics",
                    watch_type="keyword",
                    keywords=new_core[:10],
                )
                existing_kws.update(t.lower() for t in new_core)
                logger.info("Auto-created core expertise watchlist: %d keywords", len(new_core))

        # Credible topics → keyword watchlist
        credible_raw = expertise.get("credible_topics", "")
        if credible_raw:
            credible_topics = [t.strip() for t in credible_raw.split(",") if t.strip()]
            new_credible = [t for t in credible_topics if t.lower() not in existing_kws]
            if new_credible:
                await run_db(
                    _save_wl,
                    name="My Expertise — Industry Topics",
                    watch_type="keyword",
                    keywords=new_credible[:10],
                )
                logger.info("Auto-created industry topics watchlist: %d keywords", len(new_credible))
    except Exception as e:
        logger.debug("Auto expertise watchlist creation failed (non-fatal): %s", e)

    # ── Auto-analyze brand & generate improvement plan ──
    brand_summary = ""
    try:
        from .brand_strategy import _handle_analyze, _handle_plan

        account_id_for_brand = await run_db(get_setting, "unipile_account_id", "")
        if account_id_for_brand:
            await _handle_analyze(account_id_for_brand, "")
            await _handle_plan(account_id_for_brand, "")

            from ..services.brand_service import load_brand_analysis

            ba = await run_db(load_brand_analysis)
            score = ba.get("overall_score", 0) if ba else 0
            brand_summary = (
                f"\n\n Brand Audit: {score}/100\n"
                "A 4-week improvement plan has been auto-generated and will execute automatically.\n"
                "Run brand_strategy(action='progress') anytime to check progress."
            )
            logger.info("Auto brand analysis complete: score=%d", score)
    except Exception as e:
        logger.warning("Auto brand analysis failed (non-fatal): %s", e)

    # ── Detect Sales Navigator license ──
    # Only a delivered verdict is persisted. detect_sales_navigator raises
    # when it cannot decide (expired session, throttle, transport), and a
    # first-run setup is exactly when a fresh account is most likely to be
    # rate-limited — writing False here would downgrade a paying user for a
    # week. An absent key already reads as free tier everywhere.
    has_sales_nav = False
    try:
        has_sales_nav = await client.detect_sales_navigator(account_id)
        await run_db(save_setting, "has_sales_navigator", has_sales_nav)
        if has_sales_nav:
            logger.info("Sales Navigator detected on this account")
    except Exception as e:
        logger.warning("Sales Nav detection gave no verdict — not persisted: %s", e)

    # ── Sync email_account_id from Unipile / backend ──
    email_status = ""
    try:
        from ..services.unipile_email import resolve_email_account_id
        email_acct = await resolve_email_account_id(client)
        if email_acct:
            email_status = "\n📧 Email account connected — email fallback enabled."
            logger.info("Synced email_account_id: %s", email_acct[:8])
    except Exception as e:
        logger.debug("Email account sync skipped: %s", e)

    if is_backend_mode():
        try:
            from ..services.cloud_sync import sync_directory_from_cloud
            await sync_directory_from_cloud()
        except Exception:
            logger.debug("Shared directory pull during setup failed", exc_info=True)
        try:
            from ..services.cloud_sync import ensure_hosted_sending_default
            await ensure_hosted_sending_default()
        except Exception:
            logger.debug("Hosted sending default during setup failed", exc_info=True)

    # ── Format and return ──
    output = format_voice_signature(voice, expertise)

    workspace_line = ""
    seat_line = ""
    if is_backend_mode():
        me = {}
        try:
            if not me:
                response = await client._client.get(
                    f"{client.base_url.rstrip('/')}/api/v1/auth/me",
                    headers=client._headers(),
                )
                response.raise_for_status()
                me = response.json()
            workspace = str(me.get("org_name") or me.get("org_id") or "").strip()
            role = str(me.get("role") or "").strip()
            if workspace:
                workspace_line = (
                    f"✅ HeyLead is set up in workspace **{workspace}**"
                    + (f" ({role})" if role else "")
                    + "\n"
                )
        except Exception:
            logger.warning("Could not name the active workspace in setup output", exc_info=True)

        from ..services.cloud_sync import local_scheduler_engine_enabled
        if local_scheduler_engine_enabled():
            seat_line = (
                f"🔗 Sending from your LinkedIn seat ({profile['name']}) — "
                "this Mac sends from your seat\n"
            )
        else:
            seat_line = (
                f"🔗 Sending from your LinkedIn seat ({profile['name']}) — "
                "the cloud sends for you, nothing goes out until you launch a campaign\n"
            )

    header = (
        workspace_line
        + seat_line
        + f"👤 {profile['name']}"
        + (f" — {profile['title']}" if profile.get("title") else "")
        + (f" at {profile['company']}" if profile.get("company") else "")
        + "\n"
        + (f"📍 {profile['location']}\n" if profile.get("location") else "")
        + (f"🔗 {profile['profile_url']}\n" if profile.get("profile_url") else "")
        + f"📝 {len(profile.get('posts', []))} recent posts analyzed\n"
        + "\n"
    )

    serper_hint = (
        "\n💡 Optional: Add a SERPER API key for news-enhanced messages:\n"
        "   Add \"serper\" to api_keys in ~/.heylead/config.json\n"
        "   Get a free key at: https://serper.dev"
    )

    website_hint = (
        "\n💡 **Tip:** Set up website tracking with `signals(action='website_setup')` "
        "to detect when target companies visit your site."
    )

    from .organization import next_step_hint
    next_hint = "\n\n" + next_step_hint()

    if is_backend_mode():
        serper_hint = ""
        website_hint = ""

    return header + output + email_status + brand_summary + serper_hint + website_hint + next_hint
