# Harness Assumptions Protocol

Identifies and tracks rules in the atelier system that depend on model capabilities, API limits, or temporal context. These assumptions go stale across model upgrades and API changes. This protocol is the registry and audit checklist; the assumptions themselves stay in their original files.

## Why This Exists

Rules like "use the high tier for creative tasks, the cheap tier for mechanical tasks" or "load last 3 reflections" encode a snapshot of model capabilities at a point in time. When capabilities change (new model release, context window expansion, cost reduction, new API features), these rules may become wrong, suboptimal, or unnecessary. Without a registry, they are invisible until they cause a problem.

Inspired by the general lesson: a behavioral workaround for an older model in a tier becomes dead weight when a stronger model ships in that tier. The harness encoded a stale assumption about the model.

## Assumption Classification

| Class | Definition | Example | Staleness Signal |
|-------|-----------|---------|-----------------|
| **Voice Assignment** | Which voice band each role binds to | Researcher = deep band | New model release, benchmark shift, cost change |
| **Token/Context Budget** | Context window sizing, loading limits | "last 3 reflections" | Context window expansion |
| **Temporal Threshold** | Time-based triggers and warnings | "7 days stale" for profile | User behavior data |
| **Turn Budget** | maxTurns per agent | Evolver=25 | Model efficiency changes |
| **Search Strategy** | Query patterns tuned to current capability | semantic.py stub fallback | semantic.py mode change |

## Assumption Registry

### Voice Assignments

`harness/agents.toml` is the canonical source of truth for the per-agent `voices` keyed inline table (`{native = "X", direct = "Y"}` or single-leg variants). `harness/models.toml` declares model identities and runtime-neutral reasoning tiers. Provider/model **bindings** (model id, endpoint URL, env var, request extras) live in the gitignored `profile/models.toml`; loaders merge schema + bindings at runtime. For native shadow telemetry, `scripts/shadow.py native-model` resolves the selected runtime so Codex results use `codex_native` instead of a Claude identity. `harness_lint.py` validates voice references and runtime-aware call sites. The table below is the audit-trigger registry only: it does not restate the per-agent voices (read agents.toml for those), it lists what staleness signal would force a re-evaluation per role family.

Voice band vocabulary used in this file:
- **deep** — flagship pair (e.g., highest-cognition Anthropic + highest-cognition direct-api)
- **mid** — mid-tier pair (cross-provider, cheaper than deep but still substantive)
- **cheap** — minimal pair (mechanical I/O, verbatim preservation)
- **external** — cross-provider audit pair (no Anthropic leg by design)

| Agent | Current voice band | Re-test When |
|-------|----|-------------|
| Researcher | deep | A cheaper tier matches the primary on reading comprehension benchmarks |
| Synthesizer | deep | A cheaper tier matches the primary on synthesis quality |
| Challenger | deep | A cheaper tier improves on open-ended question quality |
| Thinker | deep | A cheaper tier matches the primary on framework reasoning |
| Evolver | deep | A cheaper tier matches the primary on multi-file coherence |
| Scholar | deep | A cheaper tier matches the primary on dense-paper reading quality |
| Reader | mid | A cheaper tier matches the primary on routine-article reading quality |
| Reviewer | mid | Cross-provider agreement rate drops on rubric scoring; cost shifts in either binding |
| Curator | mid | Cross-provider agreement rate drops on note preservation |
| Scout | mid | Cross-provider agreement rate drops on web triage |
| Meeting | mid | Cross-provider agreement rate drops on transcript extraction |
| Librarian | mid | Cross-provider agreement rate drops on bilingual recommendations |
| Privacy Reviewer | mid | Cross-provider agreement rate drops on semantic privacy scan; either binding deprecates |
| Scribe | cheap (single-leg, native only) | A cheaper-still verbatim model becomes available; the current native voice fails verbatim-preservation tests |
| External-Reviewer | external | A new external provider becomes worth adding; one of the current legs deprecates |

### Token/Context Budgets

| Rule | Location | Current Value | Re-test When |
|------|----------|--------------|-------------|
| Route context | context_bundle.py, session-continuity.md, intents.toml | 32 KB when profile files are preloaded, 8 KB otherwise; declared sections land whole (per-file cap 16 KB) | A declared profile or continuity section is truncated on a normal route |
| Selected workflow context | context_bundle.py, session-continuity.md | At most 64 KB before targeted source reads | Workflow evidence needs exceed the cap repeatedly |
| Reflection continuity | context_bundle.py | Headings plus at most two high-signal closing sections from up to 3 files | Reflection structure changes |
| Daily-note preload | context_bundle.py, session-continuity.md | Explicit component only | More routes demonstrably require current capture |
| Profile preload | context_bundle.py, intents.toml | Only selected row's `profile_reads` | Intent ownership changes |
| Session-log preload | context_bundle.py | Latest `Continuity` and `Anomalies` sections only | Session schema changes |

### Temporal Thresholds

| Rule | Location | Current Value | Re-test When |
|------|----------|--------------|-------------|
| Profile staleness warning | CLAUDE.md, context_bundle.py, review.md | 7 days | User data shows profiles change faster/slower |
| Semantic search recency window | daily-reflection.md, challenger.md, researcher.md | 3+ months for forgotten-context probes | Embedding index makes recency less important |
| L2 staleness thresholds | staleness.py | dormant=45d, stale=90d, promote=180d+2refs | First real corpus ages past 90 days; tune with actual archival decisions |
| Meta-reflection trigger | evolver.md (principle 8 pruning trigger) | Every 5 sessions | Session volume data |

### Turn Budgets

| Agent | Location | Current maxTurns | Re-test When |
|-------|----------|-----------------|-------------|
| Evolver | evolver.md | 25 | Model efficiency improves |
| Researcher | researcher.md | 15 | Search strategy changes |
| Synthesizer | synthesizer.md | 15 | Model gets faster at synthesis |
| Reviewer | reviewer.md | 100 | Checklist execution speed |
| Privacy Reviewer | privacy-reviewer.md | 100 | Semantic-leak coverage needs |
| Curator | curator.md | 15 | Note operation complexity |
| Scout | scout.md | 15 | Web search patterns change |
| Librarian | librarian.md | 15 | Recommendation patterns |
| Challenger | challenger.md | 10 | Question generation needs |
| Thinker | thinker.md | 15 | Framework application depth |
| Meeting | meeting.md | 10 | Transcript complexity |
| Reader | reader.md | 15 | Reading depth needs |
| Scholar | scholar.md | 15 | Dense-text reading depth needs (matches Reader; voices differ) |
| Scribe | scribe.md | 10 | Mechanical capture pace |
| Midpoint-checkpoint trigger | agent-handoff.md (Escalation Protocol) | turn 10 of a 15+ turn budget | Model gets faster at synthesis; longer chains become routine; or session-log data shows drift seldom occurs by turn 10 |

### Search Strategy

| Rule | Location | Current Value | Re-test When |
|------|----------|--------------|-------------|
| semantic.py is primary for content queries | CLAUDE.md | Real embedding mode | Index is machine-local at `~/.cache/atelier/lance/`; inspect with `uv run scripts/semantic.py status`; owner-gated launchd maintenance runs incremental `index --if-stale`; full rebuild stays manual |
| Grep for structural queries only | CLAUDE.md | Always | semantic.py covers structural queries too |
| Retry with synonyms on empty results | error-handling.md | Manual retry | semantic.py handles synonyms natively |

### Cloud Backend Assumptions

These track behavioral assumptions about external services the atelier depends on. Cloud APIs are the highest-churn dependency category; backend taxonomy in `protocols/backend-taxonomy.md`.

| Assumption | Backend | Where it bites | Re-test When |
|---|---|---|---|
| Drive MCP `create_file` writes within minutes; absence after one cron cycle = silent failure | Google Drive MCP | `remote-routines.md` § Policy, Debugging row | Anthropic changes MCP latency SLA; user observes routine output gap |
| Drive sync (local client) catches up within minutes of cloud write | Google Drive (substrate) | `scripts/cues.py check_routine_outputs` may report "no new files" while file exists in cloud | Drive desktop client changes sync cadence; user reports phantom missing files |
| Readwise CLI is callable and authenticated on every session | Readwise | `/curate` blocks if not; routes through standard Level-2 degradation | Readwise changes CLI auth; user rotates token |
| claude.ai cron fires within tolerance of declared cron expression | claude.ai routines | Output staleness; cue never fires for routine that silently stopped running | Anthropic changes scheduling SLA; user observes missing cron fires |
| Google Drive MCP API surface (`create_file`, `get_file_metadata`, etc.) stays stable | Google Drive MCP | All routine prompts that call these tools; ingestion flows | Anthropic deprecates or renames MCP tools |
| Gmail MCP draft creation does not auto-send | Gmail MCP | Privacy: a routine creating a draft trusts that the user reviews before sending | MCP changes default behavior |

### Known Runtime Caps

| Assumption | Where it bites | Re-test When |
|---|---|---|
| `maxTurns` frontmatter is the sole turn budget for `Agent`-tool dispatches | Empirically, system reviews (reviewer + evolver) truncate around 25-32 tool uses despite `maxTurns: 100` in their .md frontmatter. The script-driven external-reviewer (chat_completion.py + codex CLI) IS uncapped. The `maxTurns: 100` setting is intent; actual runtime applies an additional ceiling we don't control from agent definitions. | Claude Code releases that change subagent dispatch turn budgets, OR a workaround that routes system reviews through script-driven dispatch (chat_completion.py with the reviewer prompt) instead of `Agent` tool. |

## Audit Checklist

Run this checklist when any of these events occur:

- [ ] New model release in any tier (any provider used in `profile/models.toml` bindings)
- [ ] Context window size change
- [ ] semantic.py mode change (stub to real)
- [ ] Cost structure change (model pricing)
- [ ] Quarterly system review

**Audit procedure:**

1. Identify which registry sections the event affects (use the "Re-test When" column)
2. For each triggered assumption, test whether the current value is still optimal
3. If stale, propose a change via the Evolver's OODA cycle
4. Log the audit result in the next session log under "Harness Assumptions Exercised"
5. Update this registry with new values and rationale after changes land

## Integration

- **Session logs** record which assumptions were load-bearing each session (the "Harness Assumptions Exercised" section in `protocols/session-log.md`)
- **Evolver** checks this registry during its Observe phase for triggered re-test conditions
- **Evolver** aggregates assumption-exercise data across session logs to spot assumptions that are never tested or always active
