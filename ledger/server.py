from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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
    return {"accounts": accounts}


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
        if self.path.startswith("/api/state"):
            con = db.connect()
            self._send(
                200,
                {
                    "progress": progress(con),
                    "queue": queue_items(con),
                    "categories": sorted(suggest.budget_enum()),
                },
            )
            con.close()
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        actions = {"/api/accept": accept, "/api/override": override, "/api/reject": reject}
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
