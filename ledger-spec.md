# Transaction Classifier — Build Spec

A local, re-runnable pipeline that turns years of bank/credit-card exports into (a) a
defensible set of totals for an accountant and (b) an honest "actuals" baseline for
budgeting.

Hand this file to Claude Code as the working spec. Build it in the order in §10.

---

## 1. Principles (do not violate these)

1. **Raw is immutable.** Exported files are never edited. Everything downstream is derived.
2. **The artifact is the ruleset, not the labels.** A classified CSV is a dead end. A rules
   file re-runs identically forever, and a rule added in year 5 retroactively fixes years 1–4.
3. **Classification is derived and rebuildable.** `reclassify` must be safe to run at any
   time and must never destroy human decisions.
4. **Provenance on every classification.** You must always be able to answer "why is this row
   in this category" with a rule ID, an override, or a model suggestion you approved.
5. **The LLM never writes to the database.** It proposes rules. A human approves them.
6. **The LLM never sees the ledger.** It sees deduplicated merchant strings and aggregates —
   no balances, no account numbers, no per-transaction dates.
7. **Two taxonomies, not one.** Tax correctness matters and needs verification. Budget
   correctness is approximate and doesn't. Never let one contaminate the other.

---

## 2. Layout

```
ledger/
  raw/                    # untouched exports, one dir per account
    savings/*.csv
    visa/*.csv
    legacy/*.csv          # prior classification work exported from Sheets
  config/
    institutions.yaml     # per-bank column mappings
    normalise.yaml        # normalisation transforms (versioned — see §5)
    categories.yaml       # both taxonomies
  ledger.db               # SQLite
  rules.yaml              # THE artifact. Human-readable, diffable, in git.
  cli.py
```

`rules.yaml`, `config/`, and the code go in git. `raw/` and `ledger.db` do not
(`.gitignore` them — they hold account data).

---

## 3. Schema (SQLite)

### `accounts`
| col | type | notes |
|---|---|---|
| `account_id` | TEXT PK | slug, e.g. `savings`, `visa` |
| `name` | TEXT | |
| `kind` | TEXT | `transaction` \| `savings` \| `credit_card` |
| `institution` | TEXT | key into `institutions.yaml` |

### `import_batches`
| col | type | notes |
|---|---|---|
| `batch_id` | INTEGER PK | |
| `account_id` | TEXT FK | |
| `source_path` | TEXT | |
| `source_sha256` | TEXT UNIQUE | re-importing the same file is a no-op |
| `imported_at` | TEXT | |
| `row_count` | INTEGER | |
| `period_start`, `period_end` | TEXT | for coverage checks (§4.3) |

### `transactions` — immutable
| col | type | notes |
|---|---|---|
| `txn_id` | TEXT PK | `sha256(account_id \| date \| amount \| raw_description \| ordinal)` |
| `account_id` | TEXT FK | |
| `batch_id` | INTEGER FK | |
| `date` | TEXT | ISO |
| `amount` | INTEGER | **cents**, signed. Never floats. |
| `raw_description` | TEXT | verbatim |
| `norm_description` | TEXT | see §5 |
| `balance` | INTEGER NULL | if the export has it — used for §4.3 |
| `fy` | TEXT | derived, AU: `2023-24` = 2023-07-01 → 2024-06-30 |

`ordinal` in the hash is the 0-based index of that row among rows in the same file sharing
identical date+amount+description. This handles genuine same-day duplicate purchases without
letting a re-import create duplicates.

### `rules` (materialised from `rules.yaml` on load)
| col | type | notes |
|---|---|---|
| `rule_id` | TEXT PK | stable slug, e.g. `woolworths` |
| `match_type` | TEXT | `exact` \| `prefix` \| `substring` \| `regex` |
| `pattern` | TEXT | matched against `norm_description` |
| `account_scope` | TEXT NULL | restrict to one account |
| `sign` | TEXT NULL | `debit` \| `credit` — for merchants that do both (refunds) |
| `amount_min`, `amount_max` | INTEGER NULL | cents |
| `budget_category` | TEXT | |
| `tax_category` | TEXT NULL | **null is the correct default** |
| `priority` | INTEGER | higher wins; default by specificity (exact 300, prefix 200, substring 100, regex 100) |
| `source` | TEXT | `manual` \| `llm_approved` \| `imported_legacy` |
| `active` | INTEGER | soft-delete |

### `overrides` — human decisions, survive everything
| col | type | notes |
|---|---|---|
| `txn_id` | TEXT PK FK | |
| `budget_category` | TEXT NULL | |
| `tax_category` | TEXT NULL | |
| `note` | TEXT | why — future-you will want this at audit time |
| `created_at` | TEXT | |

### `classifications` — derived, dropped and rebuilt by `reclassify`
| col | type | notes |
|---|---|---|
| `txn_id` | TEXT PK FK | |
| `budget_category` | TEXT NULL | |
| `tax_category` | TEXT NULL | |
| `source` | TEXT | `override` \| `rule` \| `unclassified` |
| `rule_id` | TEXT NULL | |
| `needs_review` | INTEGER | see §7 |

**Precedence:** `overrides` > highest-priority matching active rule > unclassified.
Ties on priority are an error, not a coin flip — `reclassify` must fail loudly and name both
rules so you fix the ruleset.

### `transfer_links`
| col | type | notes |
|---|---|---|
| `txn_a`, `txn_b` | TEXT FK | ordered by date then txn_id |
| `method` | TEXT | `auto` \| `manual` |
| `confirmed` | INTEGER | |

### `suggestions` — LLM output awaiting approval
| col | type | notes |
|---|---|---|
| `norm_description` | TEXT PK | |
| `budget_category`, `tax_category` | TEXT | |
| `confidence` | REAL | |
| `suggested_pattern`, `suggested_match_type` | TEXT | |
| `model`, `created_at` | TEXT | |
| `status` | TEXT | `pending` \| `approved` \| `rejected` |

---

## 4. Import

### 4.1 Bank exports
`institutions.yaml` maps each bank's CSV columns to the canonical fields, including whether
debits are negative or in a separate column, and the date format. Parse amounts to cents via
`Decimal`, never `float`.

### 4.2 Legacy work (your existing spreadsheet classification)
This is a **rule seeder**, not a label importer. Import the sheet, normalise its description
column, then group by `norm_description`:

- Unanimous category across all rows → emit a rule with `source: imported_legacy`,
  `match_type: exact`.
- Conflicting categories → do **not** emit a rule; write it to the review queue with the
  competing categories shown so you adjudicate once.

This converts your prior effort into permanent leverage instead of a one-off labelling of rows
you'd have to re-label after any re-import.

### 4.3 Coverage check
Before any analysis, `ledger coverage` must report, per account: date span covered, any gap
longer than 3 days between consecutive transactions, and — where balances exist — whether
`balance[n-1] + amount[n] == balance[n]` holds throughout. A broken balance chain means a
missing statement. **Find this before you classify, not after.**

---

## 5. Normalisation

Applied to `raw_description` to produce `norm_description`. Order matters; keep it in
`normalise.yaml` with a `version` field.

1. Uppercase, collapse whitespace.
2. Strip card-scheme prefixes: `SQ *`, `SP *`, `PAYPAL *`, `EFTPOS`, `VISA PURCHASE`,
   `DIRECT DEBIT`, `OSKO PAYMENT`, `PAYID`.
3. Strip trailing location/state tokens (`SYDNEY NSW AU`, `NSW AUS`), 4+ digit numbers, dates,
   card-number fragments (`XX1234`), receipt/reference IDs.
4. Strip `AUS`/`AU` country suffix and trailing punctuation.
5. Truncate to the first 40 chars.

Bump `version` whenever you change these; `reclassify` refuses to run if the stored
normalisation version differs from the config until you run `renormalise`, which recomputes
`norm_description` for every transaction and then re-tests every rule. Log any rule that
matched before and doesn't now.

---

## 6. Transfers (do this before any totals)

Credit-card payments out of savings are not spending — the card purchases already are.
Miss this and every number you produce is wrong.

Candidate pair: opposite signs, `abs(amount)` equal, different `account_id`, dates within
±3 days.

- Exactly one candidate → auto-link, `confirmed = 1`.
- Multiple candidates → link the nearest in date, mark `confirmed = 0`, send to review.
- Near-misses (amount within 1%, e.g. fees) → review only, never auto.

Anything in `transfer_links` is excluded from all spend/income aggregates. Report unmatched
large round-number movements separately — those are usually a transfer whose counterpart
account you haven't imported.

---

## 7. Review queue

The queue is **unique `norm_description` strings**, not transactions. Rank by
`occurrences × abs(total_amount)` so the highest-leverage decisions come first — expect the
first ~50 entries to cover the majority of volume.

An item enters the queue when:
- no rule matches it, or
- it has a conflicting legacy category (§4.2), or
- a matched rule has `tax_category` non-null and the transaction hasn't been human-verified
  (tax-relevant items always get eyes on them), or
- an approved suggestion had `confidence < 0.8`.

### Review loop contract
For each item, display:

```
[ 47 txns | -$3,241.18 | savings, visa | 2021-03 → 2026-01 ]
NORM:  WOOLWORTHS
RAW:   WOOLWORTHS 1234 LILYFIELD NSW AU
       EFTPOS WOOLWORTHS METRO SYDNEY
SUGGEST: groceries / (no tax)   conf 0.94   [llm]

[a]ccept  [e]dit  [s]kip  [t]ransfer  [x] one-off (override only)  [q]uit
```

Single keypress. Accepting writes a **rule**, not labels. `x` writes an `override` for the
specific transaction instead — used for genuine one-offs where the merchant string isn't a
reliable signal.

Never require the terminal to render more than one item at a time. Progress (`n of m`,
% of dollar volume classified) prints on every screen — it's what keeps you going.

---

## 8. LLM batch contract

Only unmatched `norm_description` strings are sent. Batch 50.

**Input** (per item):
```json
{ "norm_description": "WOOLWORTHS", "samples": ["WOOLWORTHS 1234 LILYFIELD NSW AU"],
  "occurrences": 47, "total_cents": -324118, "sign": "debit",
  "account_kinds": ["savings", "credit_card"] }
```

**Output** — strict JSON array, no prose, no code fences:
```json
[{ "norm_description": "WOOLWORTHS", "budget_category": "groceries",
   "tax_category": null, "confidence": 0.94,
   "suggested_match_type": "prefix", "suggested_pattern": "WOOLWORTHS" }]
```

System prompt must state: categories are restricted to the enum in `categories.yaml`;
`tax_category` is null unless the merchant is unambiguously work- or investment-related;
if unsure, return confidence below 0.5 rather than guessing. Validate the response against the
enum and drop anything malformed — never let unvalidated output near `rules.yaml`.

---

## 9. CLI

```
ledger import <path> --account <id>     # incl. --legacy for prior spreadsheet work
ledger coverage                          # gap + balance-chain report (§4.3)
ledger renormalise                       # after a normalise.yaml version bump
ledger match-transfers
ledger suggest [--limit 50]              # LLM pass over unmatched strings
ledger review                            # the loop (§7)
ledger reclassify                        # rebuild classifications from rules + overrides
ledger report --fy 2021-22 [--tax|--budget]
ledger export --fy 2021-22 --format csv  # accountant hand-off
```

Every command is idempotent. `reclassify` after every rule change; make it automatic.

---

## 10. Build order

Ship each step working before starting the next.

1. Schema + `import` + `coverage`. Load one account, one year. Confirm the data is intact.
2. Normalisation + `renormalise`. Eyeball 50 random norm results; tune the transforms.
3. `rules.yaml` loader + `reclassify` + precedence and tie-detection.
4. Legacy import as rule seeder (§4.2). Measure classified % — this should jump hard.
5. `match-transfers`.
6. `review` loop, no LLM. Manually clear the top 30 queue items. Measure again.
7. `suggest` (LLM) for the remaining tail.
8. `report` / `export`.

**Pilot scope: the oldest financial year, end to end.** Get one return lodgeable before
touching the rest. Everything after that is re-running the same pipeline over more files.

---

## 11. Deferred (resist these)

Web UI. Charts. Receipt matching/OCR. Multi-currency. Forecasting. Anything that isn't
getting one FY's totals into your accountant's hands.

---

## 12. Before you build

Ask the accountant which buckets they actually want and in what format. Their answer defines
`categories.yaml`'s tax taxonomy, and it may be far coarser than what you'd design unprompted —
which is time saved. Also worth asking what the ATO's prefill already covers on the income
side.
