## Finance analysis

Answers questions about specific securities, earnings, cash flow, capital
expenditure, and investing signals from the Painter's own notes plus fresh
evidence. There is no dedicated ledger; retrieval and sourcing rules do the
work.

### Procedure

1. Scope: name the security, theme, or period. Resolve `<paths.finance>`
   through the paths registry.
2. Local evidence first: `uv run scripts/semantic.py query "<theme>" --path
   <paths.finance>` for the theme, then `rg` under the finance tier for exact
   tickers and titles. Read the source files before quoting them.
3. Freshness: any aggregate under the finance tier declaring
   `freshness: required` is checked against its subject source with
   `uv run scripts/aggregate_freshness.py`; stale aggregates are quoted only
   with their date.
4. External evidence: when the question needs current numbers (a filing, a
   report date, a price), dispatch a Scout. Scout findings stay
   `unverified-scout` until checked against a primary source.
5. Analysis: keep sourced facts separate from inference. Every number
   carries a source; unsupported claims are marked `[unverified]` and
   conclusions that depend on them stay unknown. State what changed since
   the most recent note on the subject.
6. Output: a short brief with a sources list. Write nothing under `$OV`
   without approval; propose a note update when the analysis adds durable
   knowledge.
