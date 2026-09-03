# Decision Ledger

Every verdict a person gives the system is one line in
`$OV/_meta/decisions.jsonl`, written by `scripts/decisions.py`. The line
carries the verdict, the one-sentence reason, and the features of the item,
so a later item of the same kind can be judged by precedent instead of by a
question. Silence is never recorded: an auto-dismissed proposal is not a
decision.

## Writers

| Surface | Class | How the line is written |
|---|---|---|
| `/autoevo-review` apply, skip, defer | `autoevo/<category>` | `scripts/autoevo_pending.py resolve --reason` (mandatory) and `defer --reason` (optional) write it |
| `/autoevo-nightly` precedent defaults | `autoevo/<category>` | `scripts/precedent.py autoevo` calls `set-default`, which records `by = "precedent"` |
| `/hi` clarification | `hi/route` | `scripts/intent_coverage.py intent-log --match-kind clarified --clarified-to` |
| `/triage` intent-coverage lane | `triage/intent-coverage` | `scripts/decisions.py record` with the accepted or rejected proposal |
| anything else | `<surface>/<kind>` | `scripts/decisions.py record --class ... --subject ... --verdict ... --reason ...` |

`by` is `human` for a person's verdict, `precedent` for a default the judge
proposed, `rule` for a fixed-rule default. Only human lines are precedents.
A human line that later contradicts a precedent line on the same subject is
a veto; `scripts/decisions.py stats` turns vetoes into per-class accuracy.

## Precedent judge

`scripts/precedent.py` proposes the default a person would have chosen:

1. Pre-filter (deterministic): human lines of the same class that share the
   item's tier, ranked by token overlap on the proposed action and evidence,
   and recency; at most 20. Precedents share the item's tier; a cross-tier
   decision is not a precedent, because the ledger's verdicts hinge on tier
   while token overlap mostly reflects how formulaic a category's summaries
   are. An item with no resolvable tier ranks without the partition.
2. Judge (model): the ranked precedents and the new item go to the
   `precedent-judge` role (a native subagent; the nightly writes prompts with
   `--bundle-dir` and reads verdicts back with `--judgment-dir`, so nothing
   leaves the machine) or, only by explicit `--model` /
   `ATELIER_PRECEDENT_MODEL`, to a direct-API model. Either answers
   `{verdict, confidence, cited, reason}` and must say `human` when precedents
   are mixed, thin, or hinge on a feature the new item lacks. There is no
   hosted-model fallback.
3. Gate (deterministic): a verdict that names an executable action (`apply`
   or `dismiss`), confidence at least 0.8, at least 3 distinct cited
   precedents that all agree with the verdict and all sit at or above the
   similarity floor (2.0, the tier term), class accuracy at least 0.9 once 5
   defaults have been judged, and fewer than 10 defaults set in this class
   since the user last decided anything (the silent budget; any human
   decision in any class refills it, and a budget of 0 makes the judge a
   sorter that never decides alone). A pass becomes a default with a
   14-day veto window (`protocols/autoevo.md` § Default after a veto
   window); anything else stays human.

The veto surface is `/autoevo-review` today and the digest once it carries
an undo line per default. Each veto is a new human line, so the judge learns
from its own misses.

Accuracy alone cannot revoke autonomy: a default that ages out unchallenged
counts as judged and correct, so `precedent_accuracy` rises while nobody
looks, and the user the automation serves is the one who loosens the gate.
The silent budget is the brake that does engage. It counts defaults set since
the user last decided anything, in any class, and stops the judge at 10.

## What is never inferred

Wiki rewrites, era judgments (time-stale-B), anything sent outside the
vault, and decisions about money or people stay explicit. The ledger records
them; the judge does not act on them.
