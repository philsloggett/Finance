# Claude Code — Project Instructions

## Project

A local, re-runnable transaction-classification pipeline ("ledger") that turns years of
bank/credit-card exports into accountant-ready totals and a budgeting baseline.
[ledger-spec.md](ledger-spec.md) is the working spec — follow its principles (§1) and build
order (§10) unless overridden here.

**Deviation from spec §11:** a simple web UI (plain HTML/JavaScript, served locally) is in
scope alongside the CLI — primarily for the review loop (§7) and reports. Keep it thin: the
Python layer owns all logic; the UI talks to it over a small local HTTP API. No frontend
build tooling (no npm/bundlers) unless it becomes genuinely necessary.

## Data sources

- Bank exports land in `ledger/raw/` (gitignored — never commit account data, and never
  commit `ledger.db`).
- Prior classification work lives in a Google Sheet
  (id `17U5ddFSZ0UqOdbYsKrHzO-vyYoCMnitamxlznKQhWro`) — the **CREDIT** and **SAVINGS**
  tables are the legacy input for the rule seeder (spec §4.2). Ignore the summary tabs.

## Git

- Do **not** add `Co-Authored-By` trailers to commit messages.
- Prefer shorter, less verbose, descriptions of changes in commit messages.

## Python Conventions

- Use **snake_case** for function names.
- Follow **PEP 8** for all other names (classes `PascalCase`, variables/modules `snake_case`).
- Use **Python 3 type hints / annotations** on functions (params and return). Prefer
  modern builtin-generic syntax (`list[str]`, `tuple[str, ...]`, `str | None`) over
  `typing.List`/`Optional`. Add `from __future__ import annotations` at the top so
  annotations stay lazy.
  **Do not** use `if TYPE_CHECKING:` guards for annotation-only imports — just import
  the type normally at module scope alongside the other imports. Plain imports keep
  the code obvious; the import-cost saving isn't worth the ceremony.
  Since the annotations carry the types, **do not repeat types in docstrings**
  (no `param (str):` / `Returns: list:`) — keep only the descriptions.
- Use **Google-style docstrings** (`Args:` / `Returns:` / `Raises:` sections),
  descriptions only (types come from the annotations).
- **Comments minimal and terse**; prefer inline, and only where the code isn't
  self-explanatory. Do not narrate every decision or restate what the code does.
- **Minimise error handling.** Avoid defensive try/except; validate the code path
  up front and rely on that. Let failures surface within the path rather than
  catching broadly. Catch only when there's a specific, handled recovery.
- Prefer explicit module namespaces on imports:
  ```
  from A.B import C        # call as C.something()
  from A.B import some_very_long_module_name as svlmn  # abbreviation is fine
  ```
