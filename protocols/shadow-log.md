# Shadow-Log System

Cross-provider correlation, cost tracking, and verdict-agreement reporting for multi-leg LLM dispatches. Companion to `protocols/backend-taxonomy.md` § Shadow logs.

## Scope

This system instruments the multi-leg verification workloads the atelier already runs today:

- `/system-review` Step 1c (privacy 2-leg: selected runtime native + direct DeepSeek)
- `scripts/review.sh` (external-reviewer 2-leg: direct DeepSeek + codex)
- `/decision` when a high-stakes choice is escalated to Thinker 2-leg

Deferred: the quantitative-claim fact-check gate is not yet instrumented; `harness/shadow_tasks.toml` reserves a `fact-check` task type for when it is.

**Out of scope (M2 workstream):** routing decisions for single-leg generative workloads (Researcher, Synthesizer, Reader, Scout, Curator). These are 80-90% of LLM cost; their quality cannot be machine-judged on the unstructured prose they emit. The procedure for answering "should I move Researcher from Opus to DeepSeek?" lives in § M2 below.

## Mechanism

| Component | Where | What it does |
|---|---|---|
| Cost catalog | `harness/model_costs.toml` (committed) + `profile/model_costs.toml` (gitignored override) | Per-model USD prices with `last_verified` date. Used by report at compute time; `scripts/shadow.py report` fails closed when any aggregated model is >90d stale, unless `--accept-stale-costs`. |
| Log schema | `scripts/chat_completion.py` (auto-logs direct-API calls) | Adds `shadow_group_id`, `task_type`, `task_dispatch_kind` fields. Reads env `ATELIER_SHADOW_GROUP` / `ATELIER_TASK_TYPE`; explicit `--shadow-group` / `--task-type` flags override. Writes to `~/.cache/atelier/llm_calls/<date>.jsonl` (full) + `$OV/_meta/shadow_logs/<date>.jsonl` (skeleton). |
| Group manager | `scripts/shadow.py group-start` | Issues a UUID, writes witness file at `~/.cache/atelier/shadow_groups/<uuid>.json` declaring expected dispatches `[{model, leg}]`. Prints shell-eval-able env exports. |
| Native-leg identity | `scripts/shadow.py native-model --agent <role>` | Resolves the active runtime before correlation. Claude returns the role's `voices.native` identity; Codex returns the dynamic `codex_native` slot. This prevents Codex output from inheriting false Anthropic pricing. |
| Native-leg logger | `scripts/shadow.py log --leg native` | Called in-band by the orchestrator after each native project-agent dispatch at a multi-leg call site. Accepts `--prompt-text` / `--response-text` for inline content (no temp files needed). Writes synthetic JSONL with `usage_estimate.method = "char_approx"`. |
| Hook replay helper | `scripts/shadow.py log-from-hook` | Accepts PostToolUse-style payloads for fixtures, replay, and possible future hook support. It is not wired in production; native legs are logged in-band via `shadow.py log`. |
| Session cleanup | `scripts/shadow.py gc` (Claude `SessionEnd`; Codex `Stop`) | Removes orphaned witnesses older than `--witness-min` (default 30, mirroring the recency window) and rotates `~/.cache/atelier/llm_calls/` files older than `--retention-days` (default 90). The `$OV/_meta/shadow_logs/` mirror skeleton is not rotated; that is the durable record. Silent best-effort. Claude runs it at session close; Codex runs it at turn stop because Codex has no `SessionEnd` event. Defends against orphaned witnesses left by crashed sessions and bounds primary-log growth. |
| Verdict-token config | `harness/shadow_tasks.toml` | Per-task regex (word-boundary, case-aware, last-match-wins) for extracting structured verdicts from leg responses. |
| Report | `scripts/shadow.py report` | Aggregates logs, dedups, groups by UUID, extracts verdicts, computes cost retroactively from current catalog, emits per-task-type-per-leg-pair agreement + cost ratio + latency. Output prefixed with permanent SCOPE banner naming the 10-20% coverage caveat. |
| Lint guard | `scripts/harness_lint.py check_shadow_group_start` | Greps known multi-leg call sites for `shadow.py group-start` invocation; fails ERROR if missing. Catches the outer-discipline regression that would silently empty the report. |

## Per-call-site recipe

Two patterns depending on whether shell state persists across dispatch steps:

### Pattern A: persistent shell (review.sh, any long-running script)

```bash
NATIVE_MODEL=$(python3 scripts/shadow.py native-model --agent privacy-reviewer)
eval "$(python3 scripts/shadow.py group-start \
  --task system-review \
  --expected '[{"model":"'"$NATIVE_MODEL"'","leg":"native"},{"model":"deepseek_pro_max","leg":"direct"}]')"
trap 'python3 scripts/shadow.py group-close --group "$ATELIER_SHADOW_GROUP" 2>/dev/null || true' EXIT

# Bash legs auto-inherit env (ATELIER_SHADOW_GROUP / ATELIER_TASK_TYPE):
uv run scripts/chat_completion.py --model deepseek_pro_max --prompt-file <path> ...
```

The task name above is illustrative: the live Pattern A site, `scripts/review.sh`, dispatches `--task external-review`; `system-review` is a reserved task type in `harness/shadow_tasks.toml` with no current producer.

### Pattern B: project command markdown (isolated shell calls)

Claude Code and Codex run each shell tool call in a separate subprocess. Env vars and EXIT traps do not persist between calls. Using `eval` + `trap EXIT` in a single call destroys the witness immediately when that call returns, before the native project-agent dispatch fires. Instead, split into three explicit steps:

**Step 1 (before dispatch):** Run `group-start` in one Bash call. Parse the UUID from stdout.
```bash
python3 scripts/shadow.py group-start --task privacy-review --agent privacy-reviewer
# --agent derives expected legs from harness/agents.toml voices + the
# runtime-aware native identity, and prints ATELIER_DIRECT_MODEL for leg B.
# Hand-built --expected stays available for call sites without a registered
# dual-voice agent.
```
The output contains `export ATELIER_SHADOW_GROUP="<uuid>"`. The orchestrator remembers this UUID.

**Step 2 (dispatch):** Send the native project-agent and direct-API shell legs in parallel. Pass the UUID explicitly to the direct leg:
```bash
uv run scripts/chat_completion.py --model deepseek_pro_max --shadow-group "<uuid>" --task-type privacy-review --prompt -
```

**Step 3 (after dispatch):** Log the native leg in-band, then close the witness:
```bash
python3 scripts/shadow.py log \
  --group "<uuid>" --task privacy-review --model "$NATIVE_MODEL" --leg native \
  --prompt-text "<agent prompt summary>" \
  --response-text "<full agent response>"
python3 scripts/shadow.py group-close --group "<uuid>"
```

Native legs must be logged in-band by the orchestrator after the project agent returns. The identity helper prefers the selector's `ATELIER_ACTIVE_RUNTIME`, then runtime-specific session signals, then the local or committed runtime selection. Callers can pass `--runtime claude` or `--runtime codex` when an explicit edge is safer than auto-detection.

### Common to both patterns

- `shadow.py log --leg codex` is still in-band for explicit external Codex CLI dispatches.
- The lint check fires ERROR if a known site is missing the `group-start` invocation. Per-leg correctness is detection-only via the witness file; the report surfaces missing legs in WARNINGS.

## Report output

```
SCOPE: shadow logs cover multi-leg verification workloads (~10-20% of LLM spend).
       ...

task=system-review  groups=23 (since 2026-05-01)
  opus[native] vs deepseek_pro_max[direct]
    verdict agreement: 21/23 = 91.3%
    avg cost: left $0.0823, right $0.0015
    avg latency: left 28.1s, right 14.2s
  ...

WARNINGS:
- opus cost computed via char_approx (±25% true cost); ...
- 3 groups missing expected legs (witness expected 2, got 1): ...
- 5 logged groups have no witness file; treated as single-leg ...
```

When verdict agreement is high (≥90%) AND cost ratio is meaningful (e.g., 50×), the user has evidence to swap the expensive leg at the call site.

## Privacy

`$OV/_meta/shadow_logs/` contains correlation skeletons only: model, leg, usage, latency, verdict, response preview (first 200 chars + SHA-256). Full prompts/responses stay machine-local at `~/.cache/atelier/llm_calls/`. The skeleton still carries some signal (task type, verdict tokens, response preview); users who push `$OV` to a private GitHub remote SHOULD add `_meta/shadow_logs/` to `$OV/.gitignore`. The atelier cannot enforce vault gitignore; this is a documented recommendation.

## M2 — manual A/B for single-leg generative routing

R1 instrumentation does NOT cover single-leg workloads (Researcher, Synthesizer, etc.) because: (a) runtime-native project agents do not expose consistent token usage to parents, (b) the response is unstructured prose with no verdict token, (c) machine-judging quality requires either an LLM judge (defeats cost minimization) or human eyeball.

The 30-minute manual procedure for answering "should I move Researcher from the native runtime to the direct-api voice?":

1. Pick 3-5 representative recent Researcher prompts from `~/.cache/atelier/llm_calls/`.
2. For each, run via direct API on the role's native identity and direct identity using the same system prompt and user prompt. If Codex inherits a session model with no direct binding, bind a comparable model in gitignored `profile/models.toml` before the comparison.
3. Open both responses side-by-side in a markdown table or split view.
4. Score each leg on 3 axes (1-5 each):
   - **Faithfulness**: does the response cite the right notes, avoid hallucinated claims?
   - **Depth**: does it reach the depth a serious user query deserves?
   - **Usability**: is it directly useful for the user's next action?
5. Aggregate: if cheap-leg averages within 0.5 of expensive-leg on all 3 axes across the 5 prompts, the swap is justified. Otherwise keep the expensive leg or run more samples.

Cost: ~$0.10-$0.50 total in API calls + 30 minutes of human judgment. No infrastructure. Answers the routing question for one specific role; repeat per role.

The R1 shadow-log infrastructure scaffolds M2 by providing the prompt corpus (`~/.cache/atelier/llm_calls/`) and the cost catalog. M2 itself is a procedure, not a tool.

## Deferred: PostToolUse(Agent) hook for native-leg capture

**Status: deferred and not currently wired.** Claude Code's `PostToolUse` hook does not fire for `Agent` tool calls in the runtime version supported by this atelier. The design below specifies a possible hook path if a runtime supplies compatible Agent events. Native-leg logging is in-band via `shadow.py log` (Pattern B step 3); `.claude/settings.json` registers no `PostToolUse` hook with matcher `Agent`.

### Designed mechanism (not wired)

```
PostToolUse hook (matcher: "Agent")
  ↓
scripts/shadow.py log-from-hook
  ├─ read PostToolUse stdin JSON: tool_name, tool_input, tool_output, session_id
  ├─ filter to tool_name == "Agent" (defensive; matcher already does this)
  ├─ extract subagent_type + prompt + response text
  │  (field-name resilience: accepts subagent_type|agent_type, prompt|instructions)
  │  (response handles both string and content-block-list shapes)
  ├─ scan ~/.cache/atelier/shadow_groups/*.json for an OPEN witness
  │  (open = started_at within 30 min AND no closed_at AND file present)
  ├─ no open witness → silent exit 0 (not a multi-leg call)
  ├─ resolve subagent_type → voices.native model via harness/agents.toml
  ├─ match against witness.expected_dispatches:
  │    e.model == model AND e.leg == "native" AND
  │    (e.subagent_type absent OR e.subagent_type == subagent_type)
  ├─ match → append JSONL to ~/.cache/atelier/llm_calls/ + $OV mirror
  │           (logged_by: "post_tool_use_hook")
  └─ no match → silent exit 0
```

### Witness lifecycle (designed)

```
flow entry:           shadow.py group-start  → writes ~/.cache/atelier/shadow_groups/<uuid>.json
during dispatch:      hook reads the witness; never mutates
flow exit:            shadow.py group-close --group <uuid>
                       → removes the witness (default) OR writes closed_at (--mark-closed)
```

For Pattern A (persistent shell), the `flow exit` step is wired via an `EXIT trap` in the call site. For Pattern B (Claude Code command markdown), traps do not persist across isolated Bash subprocesses; the call site invokes `group-close` as an explicit final Bash step. The orchestrator at every multi-leg site MUST invoke `group-close`. Without it, a witness lingers for the full 30-min recency window and the hook (if wired) can mis-correlate a later, unrelated agent dispatch into the stale group. The `subagent_type` filter (Design decision 3) is the secondary defense.

### Design decisions

1. **Session-correlation via on-disk witness, not env vars.** `ATELIER_SHADOW_GROUP` does not propagate into hook subshells. Two layers of disambiguation: explicit `group-close` from the call site (primary lifecycle — `EXIT trap` in Pattern A, explicit final Bash step in Pattern B), plus the 30-min recency window (staleness guard for crash / abort cases).

2. **Agent → leg mapping via `harness/agents.toml` voices.** The hook reads the canonical voice binding (`agents.<name>.voices.native`). Couples the hook to that registry's shape; lints in `scripts/harness_lint.py` already enforce voice schema integrity.

3. **`subagent_type` in `expected_dispatches` defeats cross-task contamination.** Multiple agents share native model identities (e.g., `thinker`, `evolver`, `scholar` all → `opus`). When the orchestrator declares `expected_dispatches[].subagent_type` at `group-start`, the hook requires the dispatch's `subagent_type` to match exactly. Entries without `subagent_type` fall back to model+leg matching (backward compat).

4. **Multi-leg overlap tie-break: most-recently-started witness wins.** Concurrent multi-leg sites in the same session are rare; when they happen, the orchestrator typically opens the inner witness immediately before the dispatch we're trying to capture, so the recency rule is the right heuristic.

5. **`group-start` stays in-band.** One Bash call per multi-leg site (not per leg) is bounded cost (~150 tokens) — the per-leg `log` calls were the 4-6× multiplier worth eliminating. The lint guard (`harness_lint check_shadow_group_start`) still detects missing `group-start` at known sites.

### Path comparison

| Path | Status | Tokens per multi-leg call site | Reliability |
|---|---|---|---|
| In-band manual (`shadow.py log --leg native` per dispatch) | **production today** | ~1.5-2.5K per /system-review | Orchestrator-supervised; failures visible inline |
| Out-of-band hook (`PostToolUse(Agent)` → `log-from-hook`) | **deferred (not wired)** | ~0 tokens for native-leg logging; ~150 for `group-start` | Silent failure if witness scan fails — drop detected at report time via missing-leg warning |

If the hook were shipped it would become the production default; until then, the in-band manual path is the production path. Both honor the same schema and write to the same JSONL files; entries are distinguished by `logged_by: "post_tool_use_hook"` (if/when the hook ships) vs `logged_by: "orchestrator_in_band"` (today).

### Phase 2.5 (deferred) — codex leg via `PreToolUse(Bash)`

`scripts/review.sh` still calls `shadow.py log --leg codex` in-band after each `codex exec` dispatch. The same hook pattern could apply via `PreToolUse(Bash)` with a command-pattern filter (`"matcher": "codex exec"`) — but codex legs fire less frequently than native legs and the in-band cost is already bounded. Defer until measurable.

## Cross-references

- `protocols/backend-taxonomy.md` — backend role + SOT + failure mode + identifier-leakage contract
- `protocols/voice-dispatch.md` — multi-leg call-site enumeration
- `protocols/intent-coverage.md` § Producer side — UserPromptSubmit hook precedent for out-of-band logging
- `scripts/chat_completion.py` docstring — log event schema
- `scripts/shadow.py --help` — subcommand reference
- `harness/model_costs.toml` — cost catalog (refresh quarterly)
- `harness/shadow_tasks.toml` — verdict-token extraction rules
