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
| `/curate`, `/read` reading episodes | `reading/item` | `scripts/decisions.py reading-record --input <event.json>`; typed evidence below |
| Curation policy capture | `reading/policy` | `scripts/decisions.py reading-policy`; immutable snapshot stored once |
| anything else | `<surface>/<kind>` | `scripts/decisions.py record --class ... --subject ... --verdict ... --reason ...` |

`by` is `human` for a person's verdict, `precedent` for a default the judge
proposed, `rule` for a fixed-rule default. Only human lines are precedents.
A human line that later contradicts a precedent line on the same subject is
a veto; `scripts/decisions.py stats` turns vetoes into per-class accuracy.
Reading events additionally use `agent` and `observed`. Both reading classes
are excluded from precedent statistics and never refill autoevo's silent budget.

## Reading feedback loop

Curate, Read, Introspect, and explicit policy evaluation share `reading/item` episodes in this
ledger. An episode joins one candidate, its selection policy, exposure, and
subsequent feedback. It is operational evidence, written by the orchestrator
from observed events or the user's actual words. It does not update profile
files, factual trust, or external applications. Missing feedback stays unknown.

Capture the policy before curation with `decisions.py reading-policy --file
.claude/commands/curate.md --model <actual-model> --context <projection.json>`.
Include additional selection instructions with repeated `--file` and all
loaded preference context with repeated `--context`. The helper stores the
exact source/context/model snapshot once as `reading/policy` in the private
ledger and returns a compact receipt with its `id`. Proposals reference that ID;
do not copy the snapshot into each event or model response. Assign an
episode ID for this curation run. Item IDs are durable source identifiers such
as `readwise:DOC_ID` or a canonical URL, never a title or scratch filename.

`reading-record --input <events.json>` accepts one object or a batch array:

```json
{
  "episode_id": "curate-session-id",
  "item_id": "readwise:example",
  "policy_id": "<id from reading-policy>",
  "event_id": "<stable ID for this observed event>",
  "event": "proposed",
  "by": "agent",
  "reason": "Concrete relevance or selection reason",
  "evidence_ref": "<session/turn or source locator>",
  "action": "deep-read",
  "item": {"title": "Example", "summary": "Bounded source summary", "category": "article"}
}
```

Batch candidates and later observed events separately. The helper validates
the complete batch before appending, deduplicates identical event IDs, and
returns counts rather than event bodies. The proposal requires `action`
(`deep-read`, `digest`, or `archive`) and item
metadata (title, available summary/category/url/author/reading_time/word_count/
tags; retain the actual inputs used to select). Later events copy the same
episode/item/policy IDs and omit proposal fields. Preserve `event_id` across
retries; reuse it only for the identical event. Source locators
and reasons must describe actual evidence, never an inferred user statement.

The accepted event/provenance combinations are enforced by
`scripts/reading_feedback.py ACTORS`. `proposed` and `shown` record the agent's
selection and actual presentation. Explicit `approved`, `declined`, and
`deferred` describe the user's decision about that proposed action. `consumed`
requires the user's statement or a reliable observation; completing an agent
analysis does not establish consumption. Only explicit `useful` or `not-useful`
feedback supplies a usefulness label. Batch permission to tag/archive records
operation approval only. Preserve the user's reason or bare feedback verbatim
without inventing an explanation; short feedback such as “有用” is valid.

`reading-evidence [--item <id>] [--limit 50]` separates explicit feedback from weaker
consumption evidence and excludes proposals/exposure. Later explicit ratings
replace earlier ratings in the view while preserving history. Feedback without
a matching proposal can inform taste but cannot receive policy outcome credit;
conflicting attribution is reported and excluded. Old triage caches contain
agent proposals and must not be backfilled as human choices.
`reading-episodes --item <id>` is the separate bounded attribution lookup;
proposal-only episodes remain discoverable without becoming taste evidence.

Ledger failures warn and leave the current user task usable. Reading writes
load the shared ledger strictly: any malformed line, reading-class or not,
blocks new reading evidence until the ledger is repaired. Profile changes
and harness changes retain their existing approval and review boundaries.

## Offline reading evaluation

Load this section only when evaluating selection-policy changes, not for
ordinary curation, reading, or feedback recording.

`reading-outcomes` reports rated, unknown, and useful counts per policy and
proposed action with feedback coverage. An archived item later found useful
is visible under archive, not counted as a successful reading recommendation.
These are descriptive results across possibly different
candidate pools. For an offline comparison:

1. Save `reading-cases --view cases` and `--view labels` outputs as a frozen
   pair; do not regenerate either during the experiment. Keep them private.
2. Run the baseline and candidate curation policies against the same cases and
   frozen context. The selecting agents see only cases, never labels, prior
   selection reasons, or the current policy outcome report. Keep model/context
   fixed unless that is the explicitly named experimental variable. Exclude
   feedback on scored items from evaluation context. If those labels informed
   the candidate's design, treat the result as development evidence and use
   fresh, untouched episodes for validation.
3. Each returns `{"case_set_id":"...","policy_id":"...","policy":{...},"predictions":
   [{"id":"...","selected":true}]}` covering every case exactly once.
   `selected` means the policy would recommend close reading. Stamp each
   policy using `reading-policy`; export its full snapshot with the read-only
   `reading-policy --id <policy-id>` and include it in `policy`.
4. Run `reading-evaluate --labels <labels.json> --baseline <baseline.json>
   --candidate <candidate.json>`. It reports useful selections, unwanted
   selections, and useful items missed. Empty labels cannot establish success.
5. Persist cases, labels, policy snapshots, predictions, and the comparison
   under one private experiment directory. Record a keep/revise/reject
   decision with evidence, then check subsequent explicit feedback. Offline agreement alone does not authorize
   promotion or establish long-term benefit. Reserve fresh episodes for that
   check; repeatedly tuning on the same cases is not independent validation.

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
   since the user last made a non-reading decision (the silent budget; a human
   decision outside `reading/item` refills it, and a budget of 0 makes the judge a
   sorter that never decides alone). A pass becomes a default with a
   14-day veto window (`protocols/autoevo.md` § Default after a veto
   window); anything else stays human.

The veto surface is `/autoevo-review` today and the digest once it carries
an undo line per default. Each veto is a new human line, so the judge learns
from its own misses.

Unobserved defaults remain unconfirmed; silence contributes no correctness
label. The silent budget additionally bounds defaults between non-reading
human decisions, even when no accuracy observation is available.

## What is never inferred

Wiki rewrites, era judgments (time-stale-B), anything sent outside the
vault, and decisions about money or people stay explicit. The ledger records
them; the judge does not act on them.
