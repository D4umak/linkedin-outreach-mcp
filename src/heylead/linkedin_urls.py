"""The link a person's name carries in a tool result (heylead-api#1898).

The twin of heylead-api's app/services/linkedin_urls.py. Stored contacts can
carry the URL a LinkedIn search card hands over,
``/in/<slug>?miniProfileUrn=urn%3Ali%3Afs_miniProfile%3A…``: up to 182
characters of percent-escapes that open the same page as ``/in/<slug>``. The
stored URL keeps its query (send_followup and generate_send read the member
id out of it); only the link written for a reader is shortened.
"""

from __future__ import annotations

from urllib.parse import urlsplit


def profile_link_url(url: str | None) -> str:
    """A LinkedIn profile URL cut to ``/in/<slug>`` on its own host; anything else unchanged."""
    text = str(url or "").strip()
    try:
        parts = urlsplit(text)
    except ValueError:
        return text
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not (host == "linkedin.com" or host.endswith(".linkedin.com")):
        return text
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 2 or segments[0] != "in":
        return text
    if not (parts.query or parts.fragment or len(segments) > 2):
        return text
    return f"{parts.scheme}://{parts.netloc}/in/{segments[1]}"
