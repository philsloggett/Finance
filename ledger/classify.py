from __future__ import annotations

import re
import sqlite3

from ledger import db, rules
from ledger.normalise import Normaliser


def _matches(rule: sqlite3.Row, txn: sqlite3.Row) -> bool:
    if rule["account_scope"] and rule["account_scope"] != txn["account_id"]:
        return False
    if rule["sign"] == "debit" and txn["amount"] >= 0:
        return False
    if rule["sign"] == "credit" and txn["amount"] < 0:
        return False
    amt = abs(txn["amount"])
    if rule["amount_min"] is not None and amt < rule["amount_min"]:
        return False
    if rule["amount_max"] is not None and amt > rule["amount_max"]:
        return False
    norm, pat = txn["norm_description"], rule["pattern"]
    match rule["match_type"]:
        case "exact":
            return norm == pat
        case "prefix":
            return norm.startswith(pat)
        case "substring":
            return pat in norm
        case "regex":
            return re.search(pat, norm) is not None
    return False


def reclassify(con: sqlite3.Connection) -> dict:
    """Drop and rebuild classifications from rules + overrides (spec 3).

    Precedence: overrides > highest-priority matching active rule > unclassified.
    Priority ties between distinct matching rules fail loudly.
    """
    stored = db.get_meta(con, "normalise_version")
    current = Normaliser().version
    if stored is not None and int(stored) != current:
        raise SystemExit(
            f"normalise.yaml is v{current} but db was built with v{stored}; "
            "run `ledger renormalise` first"
        )

    n_rules = rules.materialise(con)
    all_rules = con.execute(
        "SELECT * FROM rules WHERE active = 1 ORDER BY priority DESC"
    ).fetchall()
    exact: dict[str, list[sqlite3.Row]] = {}
    scan: list[sqlite3.Row] = []
    for r in all_rules:
        if r["match_type"] == "exact":
            exact.setdefault(r["pattern"], []).append(r)
        else:
            scan.append(r)

    overrides = {
        o["txn_id"]: o for o in con.execute("SELECT * FROM overrides")
    }

    con.execute("DELETE FROM classifications")
    ties: list[tuple[str, str, str]] = []
    counts = {"override": 0, "rule": 0, "unclassified": 0}
    for txn in con.execute("SELECT * FROM transactions"):
        ov = overrides.get(txn["txn_id"])
        if ov:
            con.execute(
                "INSERT INTO classifications (txn_id, budget_category, tax_category, "
                "source, rule_id, needs_review) VALUES (?, ?, ?, 'override', NULL, 0)",
                (txn["txn_id"], ov["budget_category"], ov["tax_category"]),
            )
            counts["override"] += 1
            continue
        candidates = [
            r for r in exact.get(txn["norm_description"], []) if _matches(r, txn)
        ] + [r for r in scan if _matches(r, txn)]
        if not candidates:
            con.execute(
                "INSERT INTO classifications (txn_id, source) VALUES (?, 'unclassified')",
                (txn["txn_id"],),
            )
            counts["unclassified"] += 1
            continue
        top = max(r["priority"] for r in candidates)
        best = [r for r in candidates if r["priority"] == top]
        if len(best) > 1:
            ties.append((txn["norm_description"], best[0]["rule_id"], best[1]["rule_id"]))
            continue
        r = best[0]
        needs_review = int(r["tax_category"] is not None)
        con.execute(
            "INSERT INTO classifications (txn_id, budget_category, tax_category, source, "
            "rule_id, needs_review) VALUES (?, ?, ?, 'rule', ?, ?)",
            (txn["txn_id"], r["budget_category"], r["tax_category"], r["rule_id"], needs_review),
        )
        counts["rule"] += 1

    if ties:
        con.rollback()
        lines = "\n".join(f"  {n!r}: {a} vs {b}" for n, a, b in ties[:20])
        raise SystemExit(f"priority ties — fix the ruleset:\n{lines}")
    con.commit()
    counts["rules_loaded"] = n_rules
    return counts
