"""Whether a string can be sent to Unipile as a company id.

A display name ("Top Tier Golf") is not an identifier. A numeric id, a URN
and a public slug are. (#3327)
"""

from __future__ import annotations

import re

_REF = re.compile(r"^[A-Za-z0-9._~-]+$")


def is_company_identifier(value: str) -> bool:
    text = (value or "").strip()
    return bool(text) and bool(_REF.fullmatch(text))
