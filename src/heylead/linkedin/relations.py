"""Paginated relations-API result.

A last page that is still full-size with no cursor is how Unipile and the
backend end a truncated walk — indistinguishable from a natural last page
unless the caller keeps that distinction.
"""

from __future__ import annotations

from typing import Any


class RelationsPage(list):
    """list[dict] plus whether pagination finished on a short page."""

    complete: bool
    cursor: str | None

    def __init__(
        self,
        items: list[dict[str, Any]] | None = None,
        *,
        complete: bool = True,
        cursor: str | None = None,
    ):
        super().__init__(items or [])
        self.complete = bool(complete)
        self.cursor = cursor or None
