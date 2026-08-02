# Finance

Local transaction-classification pipeline ("ledger") — turns bank/credit-card
exports into accountant-ready totals and a budgeting baseline. Spec in
[ledger-spec.md](ledger-spec.md); conventions in [CLAUDE.md](CLAUDE.md).

## Usage

```
python -m ledger.cli import <csv> --account <id> [--legacy]
python -m ledger.cli coverage          # gaps + balance-chain check
python -m ledger.cli seed-legacy       # legacy labels -> rules.yaml
python -m ledger.cli match-transfers   # pair cross-account movements
python -m ledger.cli reclassify        # rebuild classifications
python -m ledger.cli stats             # coverage by rows and $ volume
python -m ledger.cli review            # browser review UI (localhost:8765)
python -m ledger.cli suggest --emit    # LLM batch input for unmatched strings
python -m ledger.cli suggest --load <json> --model <name>
python -m ledger.cli report [--fy 2023-24] [--tax|--budget]
```

The review UI is keyboard-driven: `a` accept suggestion as a rule, `e` edit
(pick category / pattern), `s` skip, `t` internal transfer, `x` one-off
override (no rule), `r` reject suggestion, `u` unskip, `g` refetch queue.

`rules.yaml` is the artifact — human decisions accumulate there and re-run
identically forever. `ledger/raw/` and `ledger.db` hold account data and stay
out of git.
