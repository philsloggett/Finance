from __future__ import annotations

import sqlite3
from datetime import date


def match_transfers(con: sqlite3.Connection) -> dict:
    """Pair equal-and-opposite movements across accounts (spec 6).

    Exactly one candidate -> auto-link confirmed; multiple -> nearest date,
    unconfirmed. Existing manual links are preserved; auto links are rebuilt.
    """
    con.execute("DELETE FROM transfer_links WHERE method = 'auto'")
    linked: set[str] = {
        t
        for row in con.execute("SELECT txn_a, txn_b FROM transfer_links")
        for t in (row["txn_a"], row["txn_b"])
    }
    txns = con.execute(
        "SELECT txn_id, account_id, date, amount FROM transactions ORDER BY date, txn_id"
    ).fetchall()
    by_amount: dict[int, list[sqlite3.Row]] = {}
    for t in txns:
        by_amount.setdefault(abs(t["amount"]), []).append(t)

    counts = {"auto_confirmed": 0, "needs_review": 0}
    for t in txns:
        if t["txn_id"] in linked or t["amount"] >= 0:
            continue
        d = date.fromisoformat(t["date"])
        candidates = [
            c
            for c in by_amount.get(abs(t["amount"]), [])
            if c["txn_id"] not in linked
            and c["account_id"] != t["account_id"]
            and c["amount"] == -t["amount"]
            and abs((date.fromisoformat(c["date"]) - d).days) <= 3
        ]
        if not candidates:
            continue
        c = min(candidates, key=lambda x: abs((date.fromisoformat(x["date"]) - d).days))
        confirmed = int(len(candidates) == 1)
        a, b = sorted((t, c), key=lambda x: (x["date"], x["txn_id"]))
        con.execute(
            "INSERT INTO transfer_links (txn_a, txn_b, method, confirmed) VALUES (?, ?, 'auto', ?)",
            (a["txn_id"], b["txn_id"], confirmed),
        )
        linked.update((t["txn_id"], c["txn_id"]))
        counts["auto_confirmed" if confirmed else "needs_review"] += 1
    con.commit()
    return counts
