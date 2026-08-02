from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import yaml

RULES_PATH = Path(__file__).parent / "rules.yaml"
DEFAULT_PRIORITY = {"exact": 300, "prefix": 200, "substring": 100, "regex": 100}


def load_rules() -> list[dict]:
    if not RULES_PATH.exists():
        return []
    doc = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8")) or {}
    return doc.get("rules", [])


def save_rules(rules: list[dict]) -> None:
    rules.sort(key=lambda r: (r["budget"], r["id"]))
    RULES_PATH.write_text(
        yaml.safe_dump({"rules": rules}, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )


def materialise(con: sqlite3.Connection) -> int:
    """Replace the rules table with the contents of rules.yaml (spec 3)."""
    rules = load_rules()
    seen: set[str] = set()
    for r in rules:
        if r["id"] in seen:
            raise SystemExit(f"duplicate rule id in rules.yaml: {r['id']}")
        seen.add(r["id"])
    con.execute("DELETE FROM rules")
    for r in rules:
        con.execute(
            "INSERT INTO rules (rule_id, match_type, pattern, account_scope, sign, "
            "amount_min, amount_max, budget_category, tax_category, priority, source, active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                r["id"], r["match"], r["pattern"], r.get("account"), r.get("sign"),
                r.get("amount_min"), r.get("amount_max"), r["budget"], r.get("tax"),
                r.get("priority", DEFAULT_PRIORITY[r["match"]]),
                r.get("source", "manual"), int(r.get("active", True)),
            ),
        )
    con.commit()
    return len(rules)


def slugify(s: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]", "-", s.lower())).strip("-") or "rule"


def seed_from_legacy(con: sqlite3.Connection) -> tuple[int, int]:
    """Convert unanimous legacy labels into exact rules (spec 4.2).

    Rebuilds all imported_legacy rules from legacy_labels; manual and
    llm_approved rules are preserved. Conflicting strings go to
    legacy_conflicts for the review queue. Returns (emitted, conflicts).
    """
    groups = con.execute(
        "SELECT t.norm_description AS norm, "
        "       json_group_array(DISTINCT l.category) AS cats, COUNT(*) AS n "
        "FROM legacy_labels l JOIN transactions t USING (txn_id) "
        "WHERE t.norm_description != '' "
        "GROUP BY t.norm_description"
    ).fetchall()

    kept = [r for r in load_rules() if r.get("source") != "imported_legacy"]
    covered = {(r["match"], r["pattern"]) for r in kept}
    used_ids = {r["id"] for r in kept}
    emitted: list[dict] = []
    con.execute("DELETE FROM legacy_conflicts")
    conflicts = 0
    for g in groups:
        cats = json.loads(g["cats"])
        if len(cats) > 1:
            con.execute(
                "INSERT INTO legacy_conflicts (norm_description, categories, txn_count) "
                "VALUES (?, ?, ?)",
                (g["norm"], json.dumps(sorted(cats)), g["n"]),
            )
            conflicts += 1
            continue
        if ("exact", g["norm"]) in covered:
            continue
        rule_id = slugify(g["norm"])
        while rule_id in used_ids:
            rule_id += "-x"
        used_ids.add(rule_id)
        emitted.append(
            {"id": rule_id, "match": "exact", "pattern": g["norm"],
             "budget": cats[0], "tax": None, "source": "imported_legacy"}
        )
    save_rules(kept + emitted)
    con.commit()
    return len(emitted), conflicts
