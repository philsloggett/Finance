from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "ledger.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  account_id  TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  kind        TEXT NOT NULL CHECK (kind IN ('transaction','savings','credit_card')),
  institution TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_batches (
  batch_id      INTEGER PRIMARY KEY,
  account_id    TEXT NOT NULL REFERENCES accounts(account_id),
  source_path   TEXT NOT NULL,
  source_sha256 TEXT NOT NULL UNIQUE,
  imported_at   TEXT NOT NULL,
  row_count     INTEGER NOT NULL,
  period_start  TEXT,
  period_end    TEXT
);

CREATE TABLE IF NOT EXISTS transactions (
  txn_id           TEXT PRIMARY KEY,
  account_id       TEXT NOT NULL REFERENCES accounts(account_id),
  batch_id         INTEGER NOT NULL REFERENCES import_batches(batch_id) ON DELETE CASCADE,
  date             TEXT NOT NULL,
  amount           INTEGER NOT NULL,
  raw_description  TEXT NOT NULL,
  norm_description TEXT NOT NULL,
  balance          INTEGER,
  fy               TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_txn_norm ON transactions(norm_description);
CREATE INDEX IF NOT EXISTS idx_txn_account_date ON transactions(account_id, date);

CREATE TABLE IF NOT EXISTS rules (
  rule_id         TEXT PRIMARY KEY,
  match_type      TEXT NOT NULL CHECK (match_type IN ('exact','prefix','substring','regex')),
  pattern         TEXT NOT NULL,
  account_scope   TEXT,
  sign            TEXT CHECK (sign IN ('debit','credit')),
  amount_min      INTEGER,
  amount_max      INTEGER,
  budget_category TEXT NOT NULL,
  tax_category    TEXT,
  priority        INTEGER NOT NULL,
  source          TEXT NOT NULL CHECK (source IN ('manual','llm_approved','imported_legacy')),
  active          INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS overrides (
  txn_id          TEXT PRIMARY KEY REFERENCES transactions(txn_id),
  budget_category TEXT,
  tax_category    TEXT,
  note            TEXT NOT NULL,
  created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS classifications (
  txn_id          TEXT PRIMARY KEY REFERENCES transactions(txn_id) ON DELETE CASCADE,
  budget_category TEXT,
  tax_category    TEXT,
  source          TEXT NOT NULL CHECK (source IN ('override','rule','unclassified')),
  rule_id         TEXT,
  needs_review    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS transfer_links (
  txn_a     TEXT NOT NULL REFERENCES transactions(txn_id) ON DELETE CASCADE,
  txn_b     TEXT NOT NULL REFERENCES transactions(txn_id) ON DELETE CASCADE,
  method    TEXT NOT NULL CHECK (method IN ('auto','manual')),
  confirmed INTEGER NOT NULL,
  PRIMARY KEY (txn_a, txn_b)
);

CREATE TABLE IF NOT EXISTS suggestions (
  norm_description     TEXT PRIMARY KEY,
  budget_category      TEXT,
  tax_category         TEXT,
  confidence           REAL NOT NULL,
  suggested_pattern    TEXT,
  suggested_match_type TEXT,
  model                TEXT,
  created_at           TEXT NOT NULL,
  status               TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending','approved','rejected'))
);

-- Legacy sheet categories per imported txn; feeds the rule seeder (spec 4.2).
CREATE TABLE IF NOT EXISTS legacy_labels (
  txn_id   TEXT PRIMARY KEY REFERENCES transactions(txn_id) ON DELETE CASCADE,
  category TEXT NOT NULL
);

-- Conflicting legacy categories awaiting adjudication in review (spec 4.2).
CREATE TABLE IF NOT EXISTS legacy_conflicts (
  norm_description TEXT PRIMARY KEY,
  categories       TEXT NOT NULL,
  txn_count        INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    return con


def get_meta(con: sqlite3.Connection, key: str) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
