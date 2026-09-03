# Protocols — Quick Reference

Entry point for all agents. When you need to know how to behave, start here.

## By Situation

- **Session start.** `orchestrator.md` (role as hub; voice legs in `voice-dispatch.md`, per-role actions in `orchestrator-actions.md`, chains and Review Tiers in `collaboration-matrix.md`) → `session-continuity.md` (cross-session) → `coaching-progressions.md` (depth). No-context `/hi` uses `hi-menu.md`; routed capture, meeting, and forgetting use their `intent-*.md` procedures.
- **During session.** `quality-gates.md` (checkpoints) → `agent-handoff.md` (envelope format) → `error-handling.md` (escalation). For configured earnings and market-signal work, use `analysis-signals.md` before retrieval.
- **Producing output.** `session-scoring.md` (rubric) → `pattern-library.md` (recurring patterns).
- **After session.** `meta-reflection.md` (system health) → `session-log.md` (process log).
- **System evolution.** `evolution.md` (root principles every harness editor passes) → `harness-assumptions.md` (stale model-era assumptions) → `antipatterns.md` (named failure modes for Tier 2+ review) → `repo-conventions.md` ($OV layout, tooling, push policy).
- **Cross-runtime / narrative.** `runtime-adapters.md` (Claude Code + Codex parity) → `atelier.md` (vocabulary register + cercle archetype map) → `semantic-vocabulary.md` (agent-meaningful tags).

## By Agent

| Agent | Must-read protocols |
|-------|-------------------|
| **Orchestrator** | orchestrator, quality-gates, error-handling, session-continuity, session-log |
| **Researcher** | agent-handoff, error-handling |
| **Synthesizer** | agent-handoff, quality-gates, pattern-library, session-scoring |
| **Reviewer** | quality-gates, agent-handoff |
| **Challenger** | coaching-progressions, error-handling |
| **Thinker** | pattern-library |
| **Curator** | error-handling, agent-handoff, epistemic-hygiene |
| **Scout** | error-handling |
| **Reader / Scholar** | agent-handoff |
| **Evolver** | meta-reflection, session-scoring, harness-assumptions, antipatterns |
| **Scribe** | agent-handoff, local-first-architecture |
| **Forgetter** | agent-handoff, autoevo |
| **Privacy-Reviewer** | agent-handoff, shadow-log |

Dormant agents (Meeting, Librarian): dispatched only on explicit intent match. Contracts live in `agent-handoff.md`.

## Protocol Dependency Graph

```
orchestrator.md
  ├── agent-handoff.md (communication contracts)
  ├── quality-gates.md (checkpoints) → session-scoring.md (rubric)
  ├── error-handling.md (escalation + emotional)
  └── session-continuity.md (cross-session memory)

coaching-progressions.md (depth adaptation)
pattern-library.md (Moments, trade routes, recurring patterns)
atelier.md (narrative vocabulary register + cercle archetype map)
semantic-vocabulary.md (agent-meaningful tag registry)

epistemic-hygiene.md (validation-depth taxonomy) → wiki-schema.md (L4 format)
local-first-architecture.md (five-tier vault model)
  └── autoevo.md (nightly autonomous decay sweep + auto-commit; pending queue surface)
repo-conventions.md (GitHub-canonical $OV conventions; tooling layout, push policy)
backend-taxonomy.md (external systems + SOT carve-outs; per-backend contracts)
  └── remote-routines.md (cron-style remote agents + cue layer; $OV/_meta/ contract)
  └── drive-zk-ingestion.md (raw landing → $OV ingestion) → raw-indexing.md (wikilink indexes over raw archives)
  └── intent-coverage.md (/hi route ledger; coverage feedback into the harness/intents.toml catalog)
  └── decision-ledger.md (every human verdict with its reason; precedent judge that turns them into defaults)
  └── shadow-log.md (multi-leg dispatch correlation; cost + verdict-agreement reporting)
analysis-signals.md (relevance-gated preflight) → analysis-signal-cache.md (fact-ledger reference) → local-first-architecture.md

session-log.md (process recording) → session-replay.md (private native transcript archive) → meta-reflection.md (system health)
harness-assumptions.md (model-era assumption registry)
antipatterns.md (named failure-mode catalog for Tier 2+ review)
runtime-adapters.md (provider-neutral runtime contracts: Claude Code + Codex)
```

Deferred specs (not currently load-bearing): `protocols/specs/`.
