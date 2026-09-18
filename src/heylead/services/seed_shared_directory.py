"""One-shot read-only seed of the shared contact directory.

Opens a caller-supplied heylead.db with sqlite URI mode=ro only.
Never calls get_db() — that runs migrations on whatever it opens.
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import httpx

from heylead.config import get_backend_config, is_backend_mode
from heylead.linkedin.backend_client import BackendClient
from heylead.services.directory_card import public_directory_card

CONTRIBUTE_CHUNK = 200


def gather_cards(db_path: str | Path) -> list[dict[str, Any]]:
    """Read public people cards from a local heylead.db without writing to it."""
    path = Path(db_path).resolve()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM global_contacts").fetchall()
    finally:
        conn.close()

    cards: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        lid = str(row.get("linkedin_id") or "")
        if lid.isdigit():
            continue
        card = public_directory_card(row)
        if card is None:
            continue
        cards.append(card)
    return cards


async def contribute_cards(cards: list[dict[str, Any]]) -> dict[str, int]:
    """POST public cards in chunks of 200. Hosted JWT required."""
    if not is_backend_mode():
        raise SystemExit("Seed requires a hosted HeyLead profile (backend JWT).")
    url, jwt = get_backend_config()
    client = BackendClient(url, jwt)
    upserted = skipped = 0
    try:
        for i in range(0, len(cards), CONTRIBUTE_CHUNK):
            chunk = cards[i : i + CONTRIBUTE_CHUNK]
            result = None
            for attempt in range(5):
                try:
                    result = await client.contribute_directory(chunk)
                    break
                except httpx.TimeoutException:
                    if attempt == 4:
                        raise
                    await asyncio.sleep(2 * (attempt + 1))
            if result.get("unsupported"):
                raise SystemExit(
                    "Shared directory API is not deployed; seed aborted."
                )
            upserted += int(result["upserted"] or 0)
            skipped += int(result.get("skipped") or 0)
            print(
                f"Chunk {i // CONTRIBUTE_CHUNK + 1}/"
                f"{(len(cards) + CONTRIBUTE_CHUNK - 1) // CONTRIBUTE_CHUNK}: "
                f"+{result.get('upserted') or 0} upserted, "
                f"{result.get('skipped') or 0} skipped",
                flush=True,
            )
    finally:
        await client.close()
    if cards and upserted + skipped == 0:
        raise SystemExit(
            "Shared directory seed contributed nothing; aborting so a missing "
            "API is not treated as success."
        )
    return {"upserted": upserted, "skipped": skipped}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Seed the shared directory from a local heylead.db (read-only).",
    )
    parser.add_argument("--db", required=True, help="Path to heylead.db")
    args = parser.parse_args(argv)

    if not is_backend_mode():
        raise SystemExit("Seed requires a hosted HeyLead profile (backend JWT).")

    cards = gather_cards(args.db)
    result = asyncio.run(contribute_cards(cards))
    print(f"Contributed {result['upserted']} cards ({result['skipped']} skipped).")
    return 0
