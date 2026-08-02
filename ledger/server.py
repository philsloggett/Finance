from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ledger import classify, db, report, rules, suggest

UI_DIR = Path(__file__).parent / "ui"


def progress(con: sqlite3.Connection) -> dict:
    accounts = []
    for acct in con.execute("SELECT account_id FROM accounts ORDER BY account_id"):
        row = con.execute(
            f"""SELECT COUNT(*) AS n, COALESCE(SUM(ABS(t.amount)), 0) AS vol,
                COALESCE(SUM(c.source != 'unclassified'), 0) AS cn,
                COALESCE(SUM(CASE WHEN c.source != 'unclassified'
                             THEN ABS(t.amount) ELSE 0 END), 0) AS cvol
                FROM transactions t JOIN classifications c USING (txn_id)
                WHERE t.account_id = ? AND {report.NOT_TRANSFER}""",
            (acct["account_id"],),
        ).fetchone()
        accounts.append(
            {"id": acct["account_id"], "rows": row["n"], "rows_c": row["cn"],
             "vol": row["vol"], "vol_c": row["cvol"]}
        )
    remaining = con.execute(
        f"""SELECT COUNT(DISTINCT t.norm_description) AS strings, COUNT(*) AS rows
            FROM transactions t JOIN classifications c USING (txn_id)
            WHERE c.source = 'unclassified' AND {report.NOT_TRANSFER}"""
    ).fetchone()
    return {
        "accounts": accounts,
        "remaining_strings": remaining["strings"],
        "remaining_rows": remaining["rows"],
    }


def queue_items(con: sqlite3.Connection, limit: int = 200) -> list[dict]:
    items = []
    for row in report.queue(con, limit):
        samples = [
            r["raw_description"]
            for r in con.execute(
                "SELECT DISTINCT raw_description FROM transactions "
                "WHERE norm_description = ? LIMIT 3",
                (row["norm"],),
            )
        ]
        sug = con.execute(
            "SELECT budget_category, confidence, suggested_pattern, suggested_match_type, "
            "model FROM suggestions WHERE norm_description = ? AND status = 'pending'",
            (row["norm"],),
        ).fetchone()
        conflict = con.execute(
            "SELECT categories FROM legacy_conflicts WHERE norm_description = ?",
            (row["norm"],),
        ).fetchone()
        items.append(
            {
                "norm": row["norm"], "n": row["n"], "total": row["total"],
                "accounts": row["accounts"], "first": row["first"], "last": row["last"],
                "samples": samples,
                "suggestion": dict(sug) if sug else None,
                "conflict": json.loads(conflict["categories"]) if conflict else None,
            }
        )
    return items


def accept(con: sqlite3.Connection, body: dict) -> dict:
    """Write a rule (spec 7: accepting writes a rule, not labels), then reclassify."""
    norm = body["norm"]
    sug = con.execute(
        "SELECT * FROM suggestions WHERE norm_description = ? AND status = 'pending'",
        (norm,),
    ).fetchone()
    existing = next(
        (r for r in rules.load_rules()
         if r["match"] == body["match"] and r["pattern"] == body["pattern"]),
        None,
    )
    if existing:
        if existing["budget"] != body["budget"]:
            raise ValueError(
                f"rule {existing['id']!r} already maps this pattern to "
                f"{existing['budget']!r} — change it in the decisions panel instead"
            )
        # same decision made twice (stale queue) — just tidy up
        if sug:
            con.execute(
                "UPDATE suggestions SET status = 'approved' WHERE norm_description = ?",
                (norm,),
            )
        con.execute("DELETE FROM legacy_conflicts WHERE norm_description = ?", (norm,))
        con.commit()
        return {"rule_id": existing["id"]}
    followed = (
        sug is not None
        and body["budget"] == sug["budget_category"]
        and body["pattern"] == sug["suggested_pattern"]
        and body["match"] == sug["suggested_match_type"]
    )
    rule_id = rules.append_rule(
        body["match"], body["pattern"], body["budget"],
        "llm_approved" if followed else "manual",
    )
    try:
        classify.reclassify(con)
    except SystemExit as e:  # priority tie — undo the rule, restore a sane db
        rules.remove_rule(rule_id)
        classify.reclassify(con)
        raise ValueError(str(e)) from e
    if sug:
        con.execute(
            "UPDATE suggestions SET status = 'approved' WHERE norm_description = ?", (norm,)
        )
    con.execute("DELETE FROM legacy_conflicts WHERE norm_description = ?", (norm,))
    con.commit()
    return {"rule_id": rule_id}


def override(con: sqlite3.Connection, body: dict) -> dict:
    """One-off: label the specific txns, no rule (spec 7 'x')."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    txns = con.execute(
        "SELECT txn_id FROM transactions t JOIN classifications c USING (txn_id) "
        "WHERE t.norm_description = ? AND c.source = 'unclassified'",
        (body["norm"],),
    ).fetchall()
    for t in txns:
        con.execute(
            "INSERT OR REPLACE INTO overrides (txn_id, budget_category, tax_category, "
            "note, created_at) VALUES (?, ?, NULL, ?, ?)",
            (t["txn_id"], body["budget"], body.get("note", "one-off via review UI"), now),
        )
    con.commit()
    classify.reclassify(con)
    return {"overridden": len(txns)}


def rules_list(con: sqlite3.Connection) -> list[dict]:
    counts = {
        row["rule_id"]: row
        for row in con.execute(
            "SELECT rule_id, COUNT(*) AS n, SUM(t.amount) AS total "
            "FROM classifications c JOIN transactions t USING (txn_id) "
            "WHERE c.rule_id IS NOT NULL GROUP BY rule_id"
        )
    }
    items = []
    for r in rules.load_rules():
        c = counts.get(r["id"])
        items.append(
            {"id": r["id"], "match": r["match"], "pattern": r["pattern"],
             "budget": r["budget"], "source": r.get("source", "manual"),
             "active": bool(r.get("active", True)), "added": r.get("added"),
             "flag": r.get("flag"),
             "n": c["n"] if c else 0, "total": c["total"] if c else 0}
        )
    items.sort(key=lambda x: (x["added"] or "", x["n"]), reverse=True)
    return items


def update_rule(con: sqlite3.Connection, body: dict) -> dict:
    kwargs = {"flag": body["flag"]} if "flag" in body else {}
    rules.update_rule(body["id"], body.get("budget"), body.get("active"), **kwargs)
    if body.get("budget") is not None or body.get("active") is not None:
        classify.reclassify(con)  # flag-only edits don't affect classification
    return {}


def override_txn(con: sqlite3.Connection, body: dict) -> dict:
    """Reclassify one specific transaction, overriding whatever rule matched it."""
    con.execute(
        "INSERT OR REPLACE INTO overrides (txn_id, budget_category, tax_category, note, "
        "created_at) VALUES (?, ?, NULL, ?, ?)",
        (body["txn_id"], body["budget"], body.get("note", "via context view"),
         datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )
    con.commit()
    classify.reclassify(con)
    return {}


def occurrences(con: sqlite3.Connection, norm: str) -> list[dict]:
    return [
        dict(r)
        for r in con.execute(
            "SELECT txn_id, date, account_id, amount FROM transactions "
            "WHERE norm_description = ? ORDER BY date",
            (norm,),
        )
    ]


def context(con: sqlite3.Connection, date_s: str, days: int = 7) -> list[dict]:
    return [
        dict(r)
        for r in con.execute(
            "SELECT t.txn_id, t.date, t.account_id, t.amount, t.raw_description, "
            "t.norm_description, c.budget_category, c.source AS class_source "
            "FROM transactions t JOIN classifications c USING (txn_id) "
            "WHERE t.date BETWEEN date(?, ?) AND date(?, ?) "
            "ORDER BY t.date, t.account_id, t.txn_id",
            (date_s, f"-{days} days", date_s, f"+{days} days"),
        )
    ]


def unusual(con: sqlite3.Connection) -> list[dict]:
    """Flag payments worth a second look.

    Implausibly large (data errors), spikes vs the merchant's own average, and
    large round-number movements with no matched transfer (spec 6: usually a
    transfer whose counterpart account isn't imported).
    """
    txns = con.execute(
        "SELECT t.txn_id, t.date, t.account_id, t.amount, t.norm_description, "
        "t.raw_description, c.budget_category, c.source AS class_source "
        "FROM transactions t JOIN classifications c USING (txn_id)"
    ).fetchall()
    linked = {
        r[0]
        for r in con.execute(
            "SELECT txn_a FROM transfer_links UNION SELECT txn_b FROM transfer_links"
        )
    }
    flags: dict[str, dict] = {}

    def flag(t: sqlite3.Row, reason: str) -> None:
        flags.setdefault(t["txn_id"], {**dict(t), "reasons": []})["reasons"].append(reason)

    by_norm: dict[str, list[sqlite3.Row]] = {}
    for t in txns:
        by_norm.setdefault(t["norm_description"], []).append(t)
        if abs(t["amount"]) >= 100_000_00:
            flag(t, "very large — confirm it's real (could be a data error)")
    for ts in by_norm.values():
        if len(ts) < 4:
            continue
        for t in ts:
            others = [abs(x["amount"]) for x in ts if x["txn_id"] != t["txn_id"]]
            avg = sum(others) / len(others)
            if avg > 0 and abs(t["amount"]) >= 5 * avg and abs(t["amount"]) >= 500_00:
                flag(t, f"{abs(t['amount']) / avg:.0f}x this merchant's average")
    for t in txns:
        if (
            t["txn_id"] not in linked
            and t["amount"] % 1000_00 == 0
            and abs(t["amount"]) >= 1000_00
            and (t["class_source"] == "unclassified" or t["budget_category"] == "Internal")
        ):
            flag(t, "large round amount, no matched transfer")
    return sorted(flags.values(), key=lambda x: -abs(x["amount"]))[:100]


def charts(con: sqlite3.Connection) -> dict:
    """Monthly spend/income per category for the charts view.

    Excludes transfer-linked txns, Internal, and single txns >= $100k (data
    errors and property settlements would destroy the scale); the excluded
    list ships alongside so the UI can disclose them.
    """
    rows = [
        dict(r)
        for r in con.execute(
            f"""SELECT strftime('%Y-%m', t.date) AS month,
                COALESCE(c.budget_category, '(unclassified)') AS cat,
                SUM(CASE WHEN t.amount < 0 THEN -t.amount ELSE 0 END) AS spend,
                SUM(CASE WHEN t.amount > 0 THEN t.amount ELSE 0 END) AS income
                FROM transactions t JOIN classifications c USING (txn_id)
                WHERE {report.NOT_TRANSFER}
                  AND COALESCE(c.budget_category, '') != 'Internal'
                  AND ABS(t.amount) < 100000_00
                GROUP BY month, cat ORDER BY month"""
        )
    ]
    excluded = [
        dict(r)
        for r in con.execute(
            f"""SELECT t.date, t.amount, t.raw_description FROM transactions t
                JOIN classifications c USING (txn_id)
                WHERE {report.NOT_TRANSFER} AND ABS(t.amount) >= 100000_00
                ORDER BY t.date"""
        )
    ]
    return {"rows": rows, "excluded": excluded}


def flag_txn(con: sqlite3.Connection, body: dict) -> dict:
    con.execute(
        "INSERT OR REPLACE INTO flags (txn_id, note, created_at, resolved_at) "
        "VALUES (?, ?, ?, NULL)",
        (body["txn_id"], body.get("note", ""),
         datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )
    con.commit()
    return {}


def resolve_flag(con: sqlite3.Connection, body: dict) -> dict:
    con.execute(
        "UPDATE flags SET resolved_at = ? WHERE txn_id = ?",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), body["txn_id"]),
    )
    con.commit()
    return {}


def followups(con: sqlite3.Connection) -> list[dict]:
    return [
        dict(r)
        for r in con.execute(
            "SELECT f.txn_id, f.note, f.created_at, t.date, t.account_id, t.amount, "
            "t.raw_description, c.budget_category "
            "FROM flags f JOIN transactions t USING (txn_id) "
            "JOIN classifications c USING (txn_id) "
            "WHERE f.resolved_at IS NULL ORDER BY t.date DESC"
        )
    ]


def reject(con: sqlite3.Connection, body: dict) -> dict:
    con.execute(
        "UPDATE suggestions SET status = 'rejected' WHERE norm_description = ?",
        (body["norm"],),
    )
    con.commit()
    return {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def _send(self, code: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            data = (UI_DIR / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        routes = {
            "/api/state": lambda con: {
                "progress": progress(con),
                "queue": queue_items(con),
                "categories": sorted(suggest.budget_enum()),
            },
            "/api/rules": lambda con: {"rules": rules_list(con)},
            "/api/unusual": lambda con: {
                "unusual": unusual(con),
                "followups": followups(con),
                "rule_followups": [
                    {"id": r["id"], "pattern": r["pattern"], "match": r["match"],
                     "budget": r["budget"], "flag": r["flag"]}
                    for r in rules.load_rules() if r.get("flag")
                ],
            },
            "/api/charts": charts,
            "/api/occurrences": lambda con: {"occurrences": occurrences(con, q["norm"])},
            "/api/context": lambda con: {
                "context": context(con, q["date"], int(q.get("days", 7)))
            },
        }
        fn = routes.get(url.path)
        if fn is None:
            self._send(404, {"error": "not found"})
            return
        con = db.connect()
        try:
            self._send(200, fn(con))
        finally:
            con.close()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        actions = {
            "/api/accept": accept,
            "/api/override": override,
            "/api/override_txn": override_txn,
            "/api/reject": reject,
            "/api/rules/update": update_rule,
            "/api/flag": flag_txn,
            "/api/flag_resolve": resolve_flag,
        }
        fn = actions.get(self.path)
        if fn is None:
            self._send(404, {"error": "not found"})
            return
        con = db.connect()
        try:
            result = fn(con, body)
            result["progress"] = progress(con)
            self._send(200, result)
        except ValueError as e:
            self._send(409, {"error": str(e)})
        finally:
            con.close()


def serve(port: int = 8765, open_browser: bool = True) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"review UI at {url}  (Ctrl+C to stop)")
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    server.serve_forever()
