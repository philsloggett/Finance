from __future__ import annotations

import csv
import hashlib
import sqlite3
from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import yaml

from ledger import db
from ledger.normalise import Normaliser

INSTITUTIONS_PATH = Path(__file__).parent / "config" / "institutions.yaml"


def fy_of(d: date) -> str:
    """AU financial year, e.g. 2023-07-01 -> '2023-24'."""
    y = d.year if d.month >= 7 else d.year - 1
    return f"{y}-{str(y + 1)[2:]}"


def to_cents(s: str) -> int:
    return int((Decimal(s.replace(",", "").replace("+", "")) * 100).to_integral_value())


def txn_hash(account_id: str, date_iso: str, amount: int, raw: str, ordinal: int) -> str:
    key = f"{account_id}|{date_iso}|{amount}|{raw}|{ordinal}"
    return hashlib.sha256(key.encode()).hexdigest()


def load_config() -> dict:
    return yaml.safe_load(INSTITUTIONS_PATH.read_text(encoding="utf-8"))


def ensure_account(con: sqlite3.Connection, account_id: str, cfg: dict) -> dict:
    acct = cfg["accounts"][account_id]
    con.execute(
        "INSERT OR IGNORE INTO accounts (account_id, name, kind, institution) VALUES (?, ?, ?, ?)",
        (account_id, acct["name"], acct["kind"], acct["institution"]),
    )
    return cfg["institutions"][acct["institution"]]


def parse_date(s: str, formats: list[str]) -> date:
    for fmt in formats:
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unparseable date: {s!r}")


def import_file(
    con: sqlite3.Connection,
    path: Path,
    account_id: str,
    legacy: bool = False,
    institution: str | None = None,
) -> tuple[int, int]:
    """Import one CSV export. Returns (batch_id, rows). Re-import is a no-op.

    `institution` overrides the account's default column mapping — needed when
    one account has files in more than one export format (legacy sheet vs
    live bank CSV).
    """
    cfg = load_config()
    inst = ensure_account(con, account_id, cfg)
    if institution:
        inst = cfg["institutions"][institution]
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    existing = con.execute(
        "SELECT batch_id FROM import_batches WHERE source_sha256 = ?", (sha,)
    ).fetchone()
    if existing:
        return existing["batch_id"], 0

    norm = Normaliser()
    stored = db.get_meta(con, "normalise_version")
    if stored is not None and int(stored) != norm.version:
        raise SystemExit(
            f"normalise.yaml is v{norm.version} but db was built with v{stored}; "
            "run `ledger renormalise` first"
        )

    rows = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f) if inst.get("header") is False else csv.DictReader(f)
        for rec in reader:
            raw = (rec[inst["description_column"]] or "").strip()
            date_s = (rec[inst["date_column"]] or "").strip()
            if not raw and not date_s:
                continue
            d = parse_date(date_s, inst["date_formats"])
            if "amount_column" in inst:  # single signed column, debits negative
                amount = to_cents(rec[inst["amount_column"]])
            else:
                amount = to_cents(rec[inst["credit_column"]] or "0") - to_cents(
                    rec[inst["debit_column"]] or "0"
                )
            balance = None
            if inst.get("balance_column") is not None:
                bal_s = (rec[inst["balance_column"]] or "").strip()
                balance = to_cents(bal_s) if bal_s else None
            category = ""
            if inst.get("category_column"):
                category = (rec[inst["category_column"]] or "").strip()
            rows.append((d.isoformat(), amount, raw, balance, category))

    cur = con.execute(
        "INSERT INTO import_batches (account_id, source_path, source_sha256, imported_at, "
        "row_count, period_start, period_end) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            account_id,
            str(path),
            sha,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            len(rows),
            min(r[0] for r in rows),
            max(r[0] for r in rows),
        ),
    )
    batch_id = cur.lastrowid

    ordinals: Counter[tuple] = Counter()
    for date_iso, amount, raw, balance, category in rows:
        key = (date_iso, amount, raw)
        ordinal = ordinals[key]
        ordinals[key] += 1
        txn_id = txn_hash(account_id, date_iso, amount, raw, ordinal)
        con.execute(
            "INSERT INTO transactions (txn_id, account_id, batch_id, date, amount, "
            "raw_description, norm_description, balance, fy) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (txn_id, account_id, batch_id, date_iso, amount, raw, norm.apply(raw),
             balance, fy_of(date.fromisoformat(date_iso))),
        )
        if legacy and category and category != "Uncategorized":
            con.execute(
                "INSERT INTO legacy_labels (txn_id, category) VALUES (?, ?)",
                (txn_id, category),
            )

    db.set_meta(con, "normalise_version", str(norm.version))
    con.commit()
    return batch_id, len(rows)


def renormalise(con: sqlite3.Connection) -> None:
    """Recompute norm_description for every txn after a normalise.yaml bump (spec 5)."""
    norm = Normaliser()
    for row in con.execute("SELECT txn_id, raw_description FROM transactions"):
        con.execute(
            "UPDATE transactions SET norm_description = ? WHERE txn_id = ?",
            (norm.apply(row["raw_description"]), row["txn_id"]),
        )
    db.set_meta(con, "normalise_version", str(norm.version))
    con.commit()
