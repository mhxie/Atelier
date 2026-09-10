# Session Log Protocol

Records continuity and operational outcomes, with detailed process telemetry
for workflows that still require it. Result notes hold the user's conclusions;
logs are bounded system records, never a substitute for an approved note.

## When to Write

At session end, immediately before (or alongside) the reflection file write. The orchestrator emits the session log. Subagents do not write session logs directly; their handoff data feeds into the orchestrator's log.

## Storage

- **File:** `<paths.sessions>/YYYY-MM-DD-<type>.md`
- **Types:** reflection, review, weekly, decision, exploration, energy-audit, reading, curate, introspect, meeting, deep-dive, prm
- **Collisions:** If multiple sessions of the same type run on the same day, append a sequence number: `YYYY-MM-DD-reflection-2.md`
- **Tier:** L2, same as `<paths.reflections>/` and `<paths.daily_notes>/`
- **Write method:** Local `Write` only. No user approval needed (system-facing artifact). If the write fails, warn and continue; do not block the session.

## Reading checkpoint

Reading has a distinct abandonment risk: the first grounded analysis can finish
before the user begins discussion or approves a reflection. For every reading
flow, write the complete session log immediately after that first analysis and
before entering discussion. Create its header, standard sections, and filled
Reading Capsule in one file operation; never persist an empty skeleton first.
This is an orchestrator-owned, system-facing write and needs no approval.

Add this bounded section at that checkpoint:

    ## Reading Capsule
    checkpoint: initial-analysis
    source: title, canonical URL, and Readwise document ID when present
    source_locator: durable source identifier; a cache path is diagnostic only
    mode: read-and-discuss, focused, or multi-lens
    claims:
    - [source] up to three attributed source claims with locations
    - [analysis] up to two clearly labelled initial analytical findings
    status: discussion-open

Keep the capsule below 1.5 KB. It contains no diary material, user facts,
goals, action commitments, or unsourced financial claims. If the reading later
completes, append final gates and continuity to the same session log. A
user-approved reflection remains a separate, optional artifact.

## Format

Daily, weekly, and goal reviews, decisions, and reflection's energy/exploration
branches use compact logs, selected automatically by `scripts/session_log.py`.
Keep the common header below and only these sections:

- **Continuity:** the previous thread, checked callback, and next action or open
  question. Preserve this section even when a result note is not saved.
- **Anomalies:** missing evidence, failed checks, refusals, or degraded behavior;
  empty when none occurred. Never infer success from missing data.
- **Operations:** necessary capture, write, approval, and check outcomes with
  affected paths. Distinguish proposed, approved, completed, refused, and failed.
  No duplicate reflection, user narrative, per-search table, engagement score,
  or mandatory agent/framework telemetry.

Fill these compact sections from observed events only. A declined result write
does not authorize storing its substance in the log. Other types retain the
full format below; reading retains its complete initial-analysis checkpoint.
Historical full logs remain readable. Missing optional telemetry in a compact
log is not noncompliance or an empty result; compare only present sections.

```markdown
---session-log---
session_id: YYYY-MM-DD-<type>[-N]
date: YYYY-MM-DD
type: <type>
duration_estimate: <minutes, rough>
model: <orchestrator model used>
---end-session-log-header---

## Agents Dispatched
| Agent | Task | Result | Turns Used |
|-------|------|--------|------------|

## Search Log
| Query | Tool | Hits | Top Result | Useful |
|-------|------|------|------------|--------|

## Gate Results
| Gate | Score/Pass | Notes |
|------|-----------|-------|

## Questions & Engagement
| Question | Depth | Landed | User Response |
|----------|-------|--------|---------------|

## Frameworks Applied
| Framework | Applied By | Fit Score | Cross-validated |
|-----------|-----------|-----------|-----------------|

## Continuity
- Previous session referenced: <session_id or "none">
- Seed planted: <next action / open question from this session>
- Callbacks checked: <list of previous seeds checked>

## Decisions & Branches
- <Timestamped prose of key routing decisions the orchestrator made>

## Anomalies
- <Anything unexpected: empty searches, user course corrections, degraded mode activations, filesystem errors>

## Harness Assumptions Exercised
- <List any model-era assumptions that were load-bearing this session>
```

### Section Guidance

**Agents Dispatched:** One row per dispatch. For parallel dispatches, list on consecutive rows and note "(parallel)" in the Task column. Include agents that were dispatched but returned errors.

**Search Log:** Every `uv run scripts/semantic.py query` call and notable `Grep` search the orchestrator or agents issued. "Useful" is a boolean: did the results contribute to the session output?

**Gate Results:** From Reviewer handoffs (`review-check` type). Include gate name, score, and pass/fail. Include revision loops if triggered.

**Questions & Engagement:** From Challenger handoffs (`challenge-set` type) and orchestrator's own reflective questions. Depth follows the Challenger taxonomy: surface, structural, paradigmatic, generative. "Landed" means the user gave a substantive response.

**Frameworks Applied:** From Thinker handoffs (`perspective` type). Fit score is the Thinker's self-assessed applicability (0-10).

**Decisions & Branches:** Free-form prose. Captures non-obvious routing decisions: "User requested Scout mid-session", "Reviewer scored 5.8, triggered revision loop round 1", "Skipped framework application (user in a rush)".

**Anomalies:** Empty search results after retries, user course corrections ("actually, let's talk about X instead"), retrieval/model readiness failures, filesystem write failures.

**Harness Assumptions Exercised:** Cite the owning rule that was load-bearing
(for example `harness/agents.toml` for a voice binding or
`scripts/context_bundle.py` for an observed context token ceiling), not a copied registry value.
For a retest, record the trigger, actual model/runtime, task, instruction/context
profile, observed result, and next retest condition. Untested assumptions remain
unknown.

## Queryability

Session logs are plain markdown with structured headings. No special tooling required.

| Intent | Query |
|--------|-------|
| Sessions where a gate failed | `Grep "NEEDS_REVISION\|REJECTED" "$OV"/sessions/` |
| Sessions that dispatched Scout | `Grep "Scout" "$OV"/sessions/` |
| Sessions with zero-hit searches | `Grep "0.*\|" "$OV"/sessions/` in Search Log tables |
| Sessions with anomalies | `Grep "## Anomalies" -A 5 "$OV"/sessions/` |
| Sessions by type | `Bash: ls "$OV"/sessions/ \| grep -oP '(?<=\d{4}-\d{2}-\d{2}-)[a-z-]+' \| sort \| uniq -c` |
| Harness assumptions used | `Grep "## Harness Assumptions Exercised" -A 5 "$OV"/sessions/` |

## Relationship to Other Artifacts

| Artifact | Purpose | Audience |
|----------|---------|----------|
| `<paths.reflections>/YYYY-MM-DD-*.md` | Session conclusions, insights, next actions | User (human-readable) |
| `<paths.sessions>/YYYY-MM-DD-*.md` | Continuity and operational outcomes; full process telemetry where selected | Orchestrator (workflow review, continuity) |

Session logs do not replace reflection files. They are a parallel, system-facing record.
