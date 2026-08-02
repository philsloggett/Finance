from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ledger import classify, db, importer, report, rules, suggest, transfers


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="ledger")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("import", help="import a CSV export")
    sp.add_argument("path", type=Path)
    sp.add_argument("--account", required=True)
    sp.add_argument("--legacy", action="store_true", help="also capture legacy categories")
    sp.add_argument("--institution", help="override the account's column mapping")
    sp.add_argument("--from", dest="from_date", metavar="ISO_DATE",
                    help="skip rows before this date (overlap with another source)")

    sub.add_parser("coverage", help="gap + balance-chain report")
    sub.add_parser("renormalise", help="recompute norm_description after a config bump")
    sub.add_parser("seed-legacy", help="convert unanimous legacy labels into rules")
    sub.add_parser("match-transfers", help="pair cross-account movements")
    sub.add_parser("reclassify", help="rebuild classifications from rules + overrides")
    sub.add_parser("stats", help="classification coverage")

    sp = sub.add_parser("report", help="totals per category")
    sp.add_argument("--fy")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--tax", action="store_true")
    g.add_argument("--budget", action="store_true")

    sp = sub.add_parser("review", help="launch the review UI")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--no-browser", action="store_true")

    sp = sub.add_parser("suggest", help="LLM batch over unmatched strings")
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--emit", action="store_true", help="print batch input JSON")
    sp.add_argument("--load", type=Path, help="validate + store LLM output JSON")
    sp.add_argument("--run", action="store_true", help="run the pass via the claude CLI")
    sp.add_argument("--model", default=None)

    args = p.parse_args(argv)
    con = db.connect()

    match args.cmd:
        case "import":
            batch_id, n = importer.import_file(
                con, args.path, args.account, args.legacy, args.institution,
                args.from_date,
            )
            print(f"batch {batch_id}: {n} rows" if n else "already imported (no-op)")
        case "coverage":
            report.coverage(con)
        case "renormalise":
            importer.renormalise(con)
            counts = classify.reclassify(con)
            print(f"renormalised; reclassified: {counts}")
        case "seed-legacy":
            emitted, conflicts = rules.seed_from_legacy(con)
            print(f"emitted {emitted} rules, {conflicts} conflicts -> review queue")
        case "match-transfers":
            print(transfers.match_transfers(con))
        case "reclassify":
            print(classify.reclassify(con))
        case "stats":
            report.stats(con)
        case "report":
            report.report(con, args.fy, "tax" if args.tax else "budget")
        case "review":
            from ledger import server

            server.serve(args.port, not args.no_browser)
        case "suggest":
            if args.load:
                accepted, dropped = suggest.load_batch(con, args.load, args.model or "unknown")
                print(f"accepted {accepted}, dropped {dropped}")
            elif args.run:
                print(suggest.run_llm(con, args.limit, model=args.model))
            else:
                json.dump(suggest.emit_batch(con, args.limit), sys.stdout, indent=1)


if __name__ == "__main__":
    main()
