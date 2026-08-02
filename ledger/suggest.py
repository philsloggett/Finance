from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml

from ledger import report


def covers(match_type: str, pattern: str, norm: str) -> bool:
    """Would a rule with this pattern actually classify this string?"""
    match match_type:
        case "exact":
            return norm == pattern
        case "prefix":
            return norm.startswith(pattern)
        case "substring":
            return pattern in norm
        case "regex":
            try:
                return re.search(pattern, norm) is not None
            except re.error:
                return False
    return False

CATEGORIES_PATH = Path(__file__).parent / "config" / "categories.yaml"


def budget_enum() -> set[str]:
    return set(yaml.safe_load(CATEGORIES_PATH.read_text(encoding="utf-8"))["budget"])


def emit_batch(con: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """Batch input for the LLM (spec 8): unmatched norm strings + aggregates only.

    Scans deep into the queue so strings already carrying a pending suggestion
    don't block the ones below them.
    """
    batch = []
    for row in report.queue(con, 100000):
        if len(batch) >= limit:
            break
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
    """Validate an LLM output file and store as pending (spec 8)."""
    return store_items(con, json.loads(path.read_text(encoding="utf-8")), model)


def store_items(con: sqlite3.Connection, items: list, model: str) -> tuple[int, int]:
    """Validate LLM output against the enum and store as pending (spec 8).

    Returns (accepted, dropped). Malformed entries are dropped, never stored.
    """
    enum = budget_enum()
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
        # a pattern that doesn't cover its own string produces a dead rule on
        # accept — fall back to the always-correct exact match
        if not covers(it["suggested_match_type"], it["suggested_pattern"], it["norm_description"]):
            it["suggested_match_type"] = "exact"
            it["suggested_pattern"] = it["norm_description"]
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


PROMPT = """You are classifying bank-transaction merchant strings for a personal ledger.
For each item in the JSON array below, propose a budget category.

Rules:
- "budget_category" MUST be one of: {categories}
- "tax_category" is always null.
- "confidence": 0..1. If you are unsure, return a value below 0.5 rather than guessing.
- "suggested_match_type": one of "exact", "prefix", "substring", "regex".
  Prefer "exact" (pattern = the norm_description itself); use "prefix" with a stable
  head when location/branch suffixes vary.
- Household context: Sydney inner-west family, kids; a rented home (agent-paid rent);
  restaurants/cafes/pubs/bottle shops are categorised as "Fast Food" by convention.
- Transfers to the family's OWN accounts are "Internal"; payments to other people are
  usually "Other" unless the note says otherwise.

Respond with ONLY a strict JSON array, no prose, no code fences. One object per input
item: {{"norm_description": ..., "budget_category": ..., "tax_category": null,
"confidence": ..., "suggested_match_type": ..., "suggested_pattern": ...}}

Items:
{items}
"""


def run_llm(
    con: sqlite3.Connection, limit: int = 200, batch_size: int = 50, model: str | None = None
) -> dict:
    """Run the suggestion pass through the local `claude` CLI (spec 8).

    Batches of `batch_size` strings; output is validated by store_items — the
    model never writes to the database directly.
    """
    import shutil
    import subprocess

    exe = shutil.which("claude")
    if not exe:
        raise SystemExit("`claude` CLI not found on PATH — install Claude Code or use --emit/--load")
    cats = ", ".join(sorted(budget_enum()))
    totals = {"sent": 0, "accepted": 0, "dropped": 0, "batches": 0}
    while totals["sent"] < limit:
        batch = emit_batch(con, min(batch_size, limit - totals["sent"]))
        if not batch:
            break
        prompt = PROMPT.format(categories=cats, items=json.dumps(batch, indent=1))
        cmd = [exe, "-p", "--output-format", "text"]
        if model:
            cmd += ["--model", model]
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", timeout=600
        )
        if proc.returncode != 0:
            raise SystemExit(f"claude CLI failed: {proc.stderr.strip()[:500]}")
        text = proc.stdout.strip()
        if text.startswith("```"):
            text = text.split("```")[1].removeprefix("json").strip()
        try:
            items = json.loads(text)
        except json.JSONDecodeError:
            print(f"batch of {len(batch)}: unparseable response, skipped")
            totals["sent"] += len(batch)
            continue
        accepted, dropped = store_items(con, items, model or "claude-cli")
        totals["sent"] += len(batch)
        totals["accepted"] += accepted
        totals["dropped"] += dropped
        totals["batches"] += 1
        print(f"batch {totals['batches']}: {accepted} accepted, {dropped} dropped")
    return totals
