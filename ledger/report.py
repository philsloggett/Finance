from __future__ import annotations

import sqlite3
from datetime import date

# Txns in any transfer link are excluded from all aggregates (spec 6).
NOT_TRANSFER = (
    "t.txn_id NOT IN (SELECT txn_a FROM transfer_links "
    "UNION SELECT txn_b FROM transfer_links)"
)


def dollars(cents: int) -> str:
    return f"{'-' if cents < 0 else ''}${abs(cents) / 100:,.2f}"


def coverage(con: sqlite3.Connection) -> None:
    """Date span, gaps > 3 days, and balance-chain check per account (spec 4.3)."""
    for acct in con.execute("SELECT account_id FROM accounts ORDER BY account_id"):
        aid = acct["account_id"]
        rows = con.execute(
            "SELECT date, amount, balance FROM transactions WHERE account_id = ? "
            "ORDER BY date, txn_id",
            (aid,),
        ).fetchall()
        if not rows:
            print(f"{aid}: no data")
            continue
        print(f"{aid}: {rows[0]['date']} -> {rows[-1]['date']}  ({len(rows)} txns)")
        prev = None
        for r in rows:
            d = date.fromisoformat(r["date"])
            if prev and (d - prev).days > 3:
                print(f"  gap: {prev} -> {d}  ({(d - prev).days} days)")
            prev = d
        if all(r["balance"] is None for r in rows):
            print("  balance chain: no balances in source")
        else:
            breaks = 0
            for a, b in zip(rows, rows[1:]):
                if a["balance"] is not None and b["balance"] is not None:
                    if a["balance"] + b["amount"] != b["balance"]:
                        breaks += 1
            print(f"  balance chain: {'OK' if not breaks else f'{breaks} breaks'}")


def stats(con: sqlite3.Connection) -> None:
    """Classification coverage by row count and dollar volume."""
    for acct in con.execute("SELECT account_id FROM accounts ORDER BY account_id"):
        aid = acct["account_id"]
        row = con.execute(
            f"""SELECT COUNT(*) AS n, SUM(ABS(t.amount)) AS vol,
                SUM(c.source != 'unclassified') AS cn,
                SUM(CASE WHEN c.source != 'unclassified' THEN ABS(t.amount) ELSE 0 END) AS cvol
                FROM transactions t JOIN classifications c USING (txn_id)
                WHERE t.account_id = ? AND {NOT_TRANSFER}""",
            (aid,),
        ).fetchone()
        if not row["n"]:
            print(f"{aid}: no data")
            continue
        print(
            f"{aid}: {row['cn']}/{row['n']} rows classified ({100 * row['cn'] / row['n']:.0f}%), "
            f"{dollars(row['cvol'])} of {dollars(row['vol'])} volume "
            f"({100 * row['cvol'] / row['vol']:.0f}%)"
        )
    n_links = con.execute("SELECT COUNT(*) AS n FROM transfer_links").fetchone()["n"]
    print(f"transfer links: {n_links} pairs excluded from aggregates")


def report(con: sqlite3.Connection, fy: str | None, taxonomy: str = "budget") -> None:
    col = "budget_category" if taxonomy == "budget" else "tax_category"
    where = f"WHERE {NOT_TRANSFER}"
    params: tuple = ()
    if fy:
        where += " AND t.fy = ?"
        params = (fy,)
    rows = con.execute(
        f"""SELECT t.fy, COALESCE(c.{col}, '(unclassified)') AS cat,
            COUNT(*) AS n, SUM(t.amount) AS total
            FROM transactions t JOIN classifications c USING (txn_id)
            {where} GROUP BY t.fy, cat ORDER BY t.fy, total""",
        params,
    ).fetchall()
    cur_fy = None
    for r in rows:
        if r["fy"] != cur_fy:
            cur_fy = r["fy"]
            print(f"\nFY {cur_fy}")
        print(f"  {r['cat']:38s} {r['n']:5d}  {dollars(r['total']):>14s}")


def queue(con: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    """Unclassified norm strings ranked by occurrences x abs(total) (spec 7)."""
    return con.execute(
        f"""SELECT t.norm_description AS norm, COUNT(*) AS n,
            SUM(t.amount) AS total, SUM(ABS(t.amount)) AS vol,
            GROUP_CONCAT(DISTINCT t.account_id) AS accounts,
            MIN(t.date) AS first, MAX(t.date) AS last
            FROM transactions t JOIN classifications c USING (txn_id)
            WHERE c.source = 'unclassified' AND {NOT_TRANSFER}
            GROUP BY t.norm_description
            ORDER BY COUNT(*) * SUM(ABS(t.amount)) DESC LIMIT ?""",
        (limit,),
    ).fetchall()
