"""Deep links from chat replies to the matching web dashboard page.

Status replies end with a link to the dashboard page that shows the same
state, so users who start in chat discover the dashboard and can check what
really happened. Kept out of formatter.py, which must stay import-light.

URLs follow the dashboard router (basename ``/dashboard``) on the configured
backend host. They never carry an org id: the dashboard resolves the
workspace from the signed-in session.

Links use the configured backend host, so the dashboard must be served from
that same host.
"""

from __future__ import annotations

from urllib.parse import quote

from . import config

# Page kind → path under /dashboard. "overview" is the index route.
PAGES: dict[str, str] = {
    "overview": "",
    "campaigns": "campaigns",
    "actions": "actions",
    "signals": "signals",
    "scheduler": "scheduler",
    "accounts": "settings/accounts",
}

SNAPSHOT_HINT = "🖼 Snapshot attached — open the link above for the live view."


def backend_base_url() -> str:
    """The configured backend host without a trailing slash."""
    return config.get_backend_config()[0].rstrip("/")


def dashboard_url(path: str = "") -> str:
    """Absolute dashboard URL; the overview keeps its trailing slash."""
    path = (path or "").strip("/")
    if not path:
        return f"{backend_base_url()}/dashboard/"
    return f"{backend_base_url()}/dashboard/{path}"


def campaign_url(campaign_id: str, outreach_id: str = "") -> str:
    """Campaign detail page, optionally focused on one outreach."""
    url = dashboard_url(f"campaigns/{quote(str(campaign_id), safe='')}")
    if outreach_id:
        url += f"?outreach={quote(str(outreach_id), safe='')}"
    return url


def page_url(kind: str) -> str:
    """URL for a named page kind from ``PAGES`` (KeyError when unknown)."""
    return dashboard_url(PAGES[kind])


def dashboard_footer(url: str, *, label: str = "Open in the dashboard") -> str:
    """The footer line that closes a status reply."""
    return f"🔗 {label}: {url}"
