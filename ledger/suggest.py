from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml

from ledger import report

CATEGORIES_PATH = Path(__file__).parent / "config" / "categories.yaml"


def budget_enum() -> set[str]:
    return set(yaml.safe_load(CATEGORIES_PATH.read_text(encoding="utf-8"))["budget"])


def emit_batch(con: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """Batch input for the LLM (spec 8): unmatched norm strings + aggregates only."""
    batch = []
    for row in report.queue(con, limit):
        if con.execute(
            "SELECT 1 FROM suggestions WHERE norm_description = ? AND status != 'rejected'",
            (row["norm"],),
        ).fetchone():
            continue
        samples = [
            r["raw_description"]
            for r in con.execute(
                "SELECT DISTINCT raw_description FROM transactions "
                "WHERE norm_description = ? LIMIT 3",
                (row["norm"],),
            )
        ]
        batch.append(
            {
                "norm_description": row["norm"],
                "samples": samples,
                "occurrences": row["n"],
                "total_cents": row["total"],
                "sign": "debit" if row["total"] < 0 else "credit",
                "account_kinds": sorted(row["accounts"].split(",")),
            }
        )
    return batch


def load_batch(con: sqlite3.Connection, path: Path, model: str) -> tuple[int, int]:
    """Validate LLM output against the enum and store as pending (spec 8).

    Returns (accepted, dropped). Malformed entries are dropped, never stored.
    """
    enum = budget_enum()
    items = json.loads(path.read_text(encoding="utf-8"))
    accepted = dropped = 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for it in items:
        ok = (
            isinstance(it, dict)
            and isinstance(it.get("norm_description"), str)
            and it.get("budget_category") in enum
            and (it.get("tax_category") is None)
            and isinstance(it.get("confidence"), (int, float))
            and 0 <= it["confidence"] <= 1
            and it.get("suggested_match_type") in ("exact", "prefix", "substring", "regex")
            and isinstance(it.get("suggested_pattern"), str)
        )
        if not ok:
            dropped += 1
            continue
        con.execute(
            "INSERT OR REPLACE INTO suggestions (norm_description, budget_category, "
            "tax_category, confidence, suggested_pattern, suggested_match_type, model, "
            "created_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
            (it["norm_description"], it["budget_category"], it["tax_category"],
             it["confidence"], it["suggested_pattern"], it["suggested_match_type"],
             model, now),
        )
        accepted += 1
    con.commit()
    return accepted, dropped
