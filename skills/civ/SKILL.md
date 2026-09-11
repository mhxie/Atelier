---
name: civ
description: Read-only life dashboard over privately configured resources, civilizations, and terminal values.
---
# Civilization Report

Read-only life dashboard. Single-pass render from vault signals. No new state files.

## Architecture

Three conceptual layers, inspired by Civ 6:

```
Resources → Civilizations → Terminal Values
Configured inputs → Configured conversion engines → Configured outputs
```

**Resources** are what you spend. **Civs** are where you invest them. **Terminal Values** are what you're optimizing for. The dashboard shows all three and whether your resource allocation is actually producing terminal value.

## Private Framework

Load the user's framework from a private source referenced by
`profile/identity.md` or located through bounded local retrieval.
Names, symbols, counts, ordering, layers, mappings, trade rules,
weights, assessment criteria and unresolved questions belong to that
source, not this command. Historical reflections are evidence, not
automatically current configuration. Missing configuration remains
unknown; do not invent defaults or write new state.

## Resources

Resources are spendable inputs. For each configured resource, read its
definition, renewal and compounding properties, measurement rules and
source artifacts from the private framework.

### Stock Sourcing

Ground each stock in a specific dated vault artifact. Report its
measurements, source and date. Flag evidence older than 30 days.
Distinguish measured stocks, qualitative assessments and inferred
allocations. State the method and limitations of counts or estimates;
missing evidence remains unknown.

### Trade Rules

Read permitted exchanges, protected outcomes and irreversibility
constraints from the private framework. Missing rules remain unknown.

## Terminal Values

Load configured outputs, current priorities and assessment criteria.
Display order does not imply ranking. Apply stage-specific weighting
only when supported by current private evidence.

Assess each value from recent reflections and report:
`rising` | `stable` | `declining` | `neglected` | `unknown`.

## Civilizations

Civilizations convert resources into terminal values. Load each
configured civilization's layer, consumed resources, terminal outputs
and dependencies from the private framework.

## Civ 6 Mechanics

### Governors (♛/○)

**Governed** (♛) = has a dedicated plan/system artifact. **Ungoverned** (○) = no infrastructure. The Researcher checks for plan files referenced in directions.md or reflections.

### Ages (per civ)

| Age | Condition | Symbol |
|---|---|---|
| **Golden** | Accelerating + at least 1 wonder in window | `★` |
| **Normal** | Steady, or accelerating without wonder | `·` |
| **Dark** | Coasting/declining/blocked, or declared goal with 0 activity | `◆` |
| **Heroic** | Was Dark last period, now accelerating with wonder | `✦` |

### Era Score

Self-estimated composite percentile vs peer cohort (read the user's peer-cohort definition from `profile/identity.md`, set during `/introspect`). Not competition; a self-check on whether you're living up to your own potential in context.

| Tier | Percentile | Era | Meaning |
|---|---|---|---|
| **Legendary** | Top 1% | Heroic `✦` | Across-the-board excellence; rare and unsustainable long-term |
| **Elite** | Top 5% | Golden `★` | Strong in most areas; firing on most cylinders |
| **Strong** | Top 20% | Normal+ `·` | Solid foundation; clear growth trajectory |
| **Average** | Top 50% | Normal `·` | Holding ground; no major wins or losses |
| **Below** | Under 50% | Dark `◆` | Falling behind your own baseline or peer trajectory |

Use dated benchmarks appropriate to each configured metric and the private
cohort definition. Identify the source and comparison basis. Where no
defensible benchmark exists, label the assessment qualitative or unknown.

**The Researcher proposes a tier** based on: resource stocks, terminal value trends, civ ages, wonders, emergencies, and 知行 alignment. The user confirms or overrides. The system never assigns a tier silently.

**Signals that push tier up:**
- Multiple terminal values rising
- Wonders completed; governors established
- Emergencies resolved before deadline
- Dedication alignment (focus matches activity)

**Signals that push tier down:**
- Terminal values declining or neglected
- Ungoverned Dark civs
- Emergencies missed; stale goals accumulating
- 知行 gap (declared priority, 0 activity)

### Emergencies (⚡)

Deadlines <90 days from today. Scan directions.md and reflections for dates + deadline language. Report: `description (Nd) [state] → affected civs`

Closure is three-valued: `open` (date ahead), `done` (closing evidence found, cite it), `unknown` (date passed with no closing evidence, or two files disagree). An unticked box past its due date is not evidence the work did not happen; render `(Nd, closure unknown)` and name where you looked. When files disagree about one fact, render `(Nd, conflict: A vs B)` and cite both sides rather than the copy that scans first.

### Dedications

Quarterly focus = current Dedication. Aligned activity is a positive era signal. 知行合一 check evaluates adherence.

## Prerequisites

1. `profile/identity.md` missing → "Run `/introspect` first." Stop.
2. `profile/directions.md` missing → "Run `/introspect` first." Stop.
3. `Last built:` >7 days → warn, continue.

## Dispatch

Single pass, two dispatches:
1. **Researcher**: gathers signals, reads stocks, computes era score, assesses terminal values.
2. **Synthesizer**: renders compact tree.

## Data Contract

All reads local. No hardcoded regex; derive terms from profile files at runtime.

### Researcher collects:

1. **Profile context**: era, goals, completed, stale, insights, employer, partner, key people.
2. **Term derivation**: per civ, extract nouns from goal lines, build ephemeral regex.
3. **Resource stocks**: read each configured resource per Stock Sourcing rules. Flag stale.
4. **Per-civ signals**: goal inventory, reflection scan (30d/60d), governor check, age classification.
5. **Terminal value assessment**: assess each configured value from recent reflections.
6. **Emergencies**: deadlines <90d from today, each with a three-valued closure state (open / done / unknown) plus the evidence, or the named absence, that decided it.
7. **Era score**: propose a tier (Legendary/Elite/Strong/Average/Below) per the Era Score percentile table; list signals_up and signals_down from the bullet lists in that section. The user confirms or overrides the proposed tier.
8. **Constraints**: directed dependencies between configured civilizations, including any evidence-backed strategic resources.
9. **Wonders** (60d): 3-5 accomplishments from strikethrough, completed goals, milestone language.
10. **知行合一**: declared focus vs actual activity distribution.

### Handoff Format

```
era: { current, theme, dedication, phase_context }

resources:
  [resource_id]: { display_name, layer, measurements, allocation, source, as_of, stale, limitations }

terminal_values:
  [value_id]: { display_name, status, evidence }

civilizations:
  [civ_id]: { display_name, layer, age, governor, key_evidence, wins, resources_consumed, terminal_output }

strategic_resource:
  [resource_id]: { status, constraints_on, evidence }

emergencies: [{ description, deadline, days_remaining, affected_civs, closure: open|done|unknown, evidence_or_absence, conflict_sources }]

era_score: { proposed_tier, reasoning (2-3 sentences), signals_up: [...], signals_down: [...] }

constraints: [directed edges]
wonders: [...]
alignment: { dedication, top_alignment, top_gap, undeclared }
gaps: [...]
```

## Synthesizer: Compact Tree

One screen, readable without scrolling. Orchestrator handles drill-down from brief in context.

```
## Civ Report (YYYY-MM-DD)
_Era · Phase · Dedication: [focus]_

RESOURCE    UPDATED  STALENESS (|=30d, 1 cell=10d)   STALE  DETAIL
[resource]  MM-DD    ###|#######......  NNNd !  [measurement, CJK allowed here]
[one row per configured resource; filled cells = stale_days/10 capped
 at 16; `|` fixed after cell 3; trailing `!` when past 30d]

VALUES    [value] =   [value] ^   [value] v   [value] X

LAYER      CIV          AGE GOV  NOTE
[layer]    [civ]         D   ~    [<=10 words, CJK allowed]
[AGE: G golden, N normal, D dark, H heroic. GOV: + fresh, ~ stale/partial, - none]

90-DAY DEADLINES                                    today YYYY-MM-DD
WHEN     DATE   ST   CHAIN  ITEM
CLOSED   MM-DD  [x]         [item]
OVERDUE  MM-DD  [!]  [tag]  [item]  CHAIN HEAD, Nd late
MON      MM-DD  [ ]  [tag]  [item]  <- MM-DD
         ===== MM-DD  [irreversible deadline] / IRREVERSIBLE =====

ALIGNMENT  DECLARED           ACTUAL (week NNhNNm)
           [leg]        [x]   [category] ###  NN.N%  <- [note]

Era: [tier] (Top N%). [1-line reasoning]. Confirm? [highest-leverage action]
Next: [configured dependency sequence]

> Expand: civ name, resource, terminal value, "era score", "constraints", "wonders"
```

Render rules. Aligned columns must contain ONLY unambiguous single-width
ASCII. Box-drawing, block, arrow and star glyphs (U+2500 block, ### ###, arrows,
stars, middots) are East Asian Ambiguous: one column in a Latin terminal, two in
a CJK-configured one, so they silently break every column to their right. CJK
text is double-width and belongs only in the trailing free column of a row.
Never hand-count alignment; compute display width with
`unicodedata.east_asian_width` (W and F are 2, A is 2 in a CJK terminal) and
verify a row renders identically under both assumptions before emitting it.
Express dependencies as an explicit `<- MM-DD` predecessor column plus a chain
summary line, not as drawn connector art, so a slipped prerequisite is legible
and greppable.

### Drill-Down Templates

**Civ:**
```
### [Civ] [age][gov] ([layer]) → [terminal output]
Goals: [list]
Evidence: [2-3 sentences citing [[Sources]]]
Wins: [list or "none"]
Governor: [plan name] or "none (signal_up if built)"
Resources consumed: [which tokens]
Terminal output: [which values, rising/stable/declining]
Next: [one action]
```

**Resource:**
```
### [Resource] ([display name])
Stock: [value] (as of MM-DD) [stale flag if applicable]
Trades: [what this resource can be exchanged for]
Spending on: [current civ allocations]
Bottleneck: [what limits this resource]
```

**Terminal Value:**
```
### [Value] ([display name])
Status: [rising/stable/declining/neglected]
Fed by: [which civs produce this]
Evidence: [2-3 sentences from recent reflections]
Risk: [what would cause decline]
```

**Era Score:** Proposed tier with reasoning, signals_up and signals_down lists, and "to reach next tier: [specific actions]"

Other sections (constraints, wonders, full 知行) on request. A drill-down answers the one section asked for and stops.

## Style

- No em dashes. Colons, semicolons, parentheses, or restructure.
- No H1. Start with `##`.
- Citations (`[[Title]]`) in drill-downs only.
- Default English. Chinese for token/value names and natural expressions.
- No vibes-based scores. Stocks are grounded in artifacts; terminal values in reflection evidence.
- Missing evidence renders as unknown, never as a negative. A tracker checkbox is derived state; when it disagrees with the file owning that fact, render the conflict instead of picking a side.

## Frequency

Ad hoc. Identical output on unchanged vault.

## Evolution

Persistent era score ledger and historical snapshots are out of scope. If demand emerges, evolve toward a dedicated state directory. Re-read the private framework when its configuration or unresolved questions change.
