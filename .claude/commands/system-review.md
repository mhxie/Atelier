---
description: Review system-evolution changes with internal and external reviewers.
---
# System Review

Review a system-evolution bundle (protocols, agents, commands, CLAUDE.md, handoff docs) before committing. Runs internal reviewer + external reviewers (codex + direct-api / DeepSeek Pro) in parallel.

## When to use

- After any change to `protocols/`, `.claude/agents/`, `.claude/commands/`, `CLAUDE.md`, `frameworks/`, or `scripts/`.
- When the user says "review the system changes", "run the reviewers", "review before commit".

Do **not** use for session output (the inline Reviewer gate handles that) or note operations (Gate 4 handles that).

## Flow

### 1. Preflight

```bash
git status --short
git diff --stat
```

If the working tree is clean, stop and tell the user there is nothing to review.

### 1b. Privacy gate (blocking)

The scan covers the working tree and staged blobs; before pushing, `/push`
repeats it over the whole unpushed history so a name that lived in an
intermediate commit cannot ship.

```bash
uv run scripts/privacy_check.py --json
```

Parse stdout as JSON and route on its `action` field (`exit 1` is the normal "hits found" signal, not a script error):

- `"proceed"` — continue to Step 1c. Carry any `coverage_warnings` into the synthesis; do not describe the mechanical gate as complete while one is present.
- `"soft_skip"` — continue to Step 2, noting "privacy gate skipped (<reason>)" in the synthesis.
- `"abort"` — abort with `NEEDS_REVISION`. Present each `hits` entry verbatim (`file:line` + `private_title`). Do not dispatch reviewers; fix leaks, re-run `/system-review`.
- JSON missing/unparseable or exit ≥ 2 with no JSON — real script error: surface stderr, soft-skip, note "privacy gate skipped (script error)".

The script scans public-bound pathnames and content for tracked plus untracked-but-not-ignored files, both staged and working copies, plus exact terms from gitignored `profile/private_terms.txt` when present. Leaks are a hard veto regardless of score; catching them before the expensive external reviewers mirrors `/lint` Phase 0c.

### 1c. Semantic privacy double-guard (blocking, cross-provider)

The mechanical script in 1b matches discovered private titles and locally declared exact terms. It still misses **semantic** leaks: contextual identity clues, previously undeclared restaurants, amount-plus-deadline combinations, demographic phrases, and mirrored personal taxonomies. Step 1c closes that gap with the privacy-reviewer's two voices (declared in `harness/agents.toml`). Cross-provider disagreement carries more diagnostic weight than two samples of the same model.

**Run only after 1b returns `hit_count: 0`** (or soft-skips). If 1b aborted with hits, do not dispatch 1c — fix 1b first.

**Before dispatch — shadow group setup (best-effort):** Run a single Bash call to create the witness file. Parse the UUID from the output line `export ATELIER_SHADOW_GROUP="<uuid>"` and **remember it** for the direct-API leg and for cleanup after dispatch. Best-effort: if the call fails, proceed without correlation.

```bash
python3 scripts/shadow.py group-start --task privacy-review --agent privacy-reviewer
```

`--agent` derives both expected legs from `harness/agents.toml` voices plus the runtime-aware native identity, and also prints `export ATELIER_DIRECT_MODEL="<direct identity>"` for the direct-leg dispatch.

**Do NOT use `eval` + `trap EXIT` here.** Claude Code and Codex run each workflow shell call in an isolated subprocess; an EXIT trap would destroy the witness immediately when that call returns, before the native project-agent dispatch fires. The witness file must stay open on disk until explicit `group-close` after both legs complete.

**Native-leg logging is in-band.** After the selected runtime's project agent returns, log its response via `shadow.py log` with `--prompt-text` and `--response-text`. The runtime-aware `native-model` helper prevents Codex results from being labeled with a Claude model identity.

Dispatch **both legs in parallel in one message: one native project-agent call and one shell call**:

**Leg A - native project agent (`subagent_type: privacy-reviewer`):**

> Privacy review the uncommitted bundle. Walk `git status --short` and `git diff HEAD --` yourself; for untracked-but-not-ignored files, `Read` them in full. Read `scripts/privacy_allowlist.txt` and honor its exact case-insensitive entries as deliberate public opt-outs; the opt-out covers only the literal, not separately sensitive surrounding context. Cross-reference `profile/` files (canonical config home; gitignored but on disk) to detect taxonomy mirroring and value coincidences with what's about to be committed. Apply all leak categories from your agent definition. Return verdict per the format in your spec. You are instance `A` (native leg); do not coordinate with the direct-api leg.

**Leg B - direct-api side (single shell call):**

Resolve the direct-leg model identity from the canonical voices binding (so this dispatch tracks `harness/agents.toml` automatically and never drifts from the schema):

```bash
DIRECT_MODEL=$(python3 -c "import tomllib; print(tomllib.loads(open('harness/agents.toml','rb').read().decode()).get('agents',{}).get('privacy-reviewer',{}).get('voices',{}).get('direct',''))")
{
  echo 'Privacy review the uncommitted bundle. Identify semantic leaks: real names, restaurants, $-amount + deadline pairs, demographic phrases, personal taxonomies, employer slugs that the mechanical filename-stem scanner misses. Honor exact case-insensitive entries in the supplied privacy allowlist as deliberate public opt-outs; each opt-out covers only the literal, not separately sensitive surrounding context. You are instance B (direct-api leg); do not coordinate with the native leg. Output one of: CLEAN | NEEDS_REVISION (with SHOULD-FIX list) | BLOCKER (with leak descriptions and file:line pointers).'
  echo
  echo '--- PRIVACY ALLOWLIST ---'
  cat scripts/privacy_allowlist.txt
  echo
  echo '--- DIFF ---'
  git diff HEAD --
  echo
  echo '--- UNTRACKED FILES ---'
  while IFS= read -r -d '' f; do
    echo "=== $f ==="
    cat "./$f"
  done < <(git ls-files --others --exclude-standard -z)
} | uv run scripts/chat_completion.py --model "$DIRECT_MODEL" --max-tokens 0 --shadow-group "<SHADOW_UUID>" --task-type privacy-review --prompt -
```

Replace `<SHADOW_UUID>` with the UUID captured from `group-start` output. If group-start failed or was skipped, omit `--shadow-group` and `--task-type`.

Both legs return verdicts (CLEAN / NEEDS_REVISION / BLOCKER). The direct-api leg returns `message.content`; treat it as the verdict.

**After both legs return — log native leg and close the shadow group (best-effort):**

Resolve the native model identity, then log the project agent's response inline and close the witness:

```bash
NATIVE_MODEL=$(python3 scripts/shadow.py native-model --agent privacy-reviewer)
python3 scripts/shadow.py log \
  --group "<SHADOW_UUID>" --task privacy-review --model "$NATIVE_MODEL" --leg native \
  --prompt-text "<agent prompt summary>" \
  --response-text "<full agent response text>"
python3 scripts/shadow.py group-close --group "<SHADOW_UUID>" --mark-closed
```

Replace `<agent prompt summary>` with a short summary of what was sent to the project agent, and `<full agent response text>` with its actual response (the verdict text). If group-start was skipped, skip this step too.

**Verdict aggregation** (most-paranoid wins):

| Leg A (native) | Leg B (direct-api) | Action |
|---|---|---|
| CLEAN | CLEAN | Proceed to Step 2 |
| CLEAN | NEEDS_REVISION | Surface SHOULD-FIX list as concerns; proceed to Step 2 (do not block) |
| NEEDS_REVISION | NEEDS_REVISION | Surface union of SHOULD-FIX as concerns; proceed to Step 2 |
| Either | BLOCKER | **Abort** with `NEEDS_REVISION`. Present union of BLOCKERs verbatim. Do not dispatch Step 2 reviewers. Fix and re-run `/system-review`. |

If the direct-api leg soft-skips (exit 2 - api_env unset), note "direct-api leg unavailable; cross-provider check downgraded to single native leg" in the synthesis and continue with Leg A's verdict only. Do not block on direct-api availability.

Cross-provider rationale: two instances of the same model from the same provider have correlated failure modes (training lineage, tokenizer, corpus). The privacy-reviewer's two voices share none of those. Disagreement here is more likely to surface a real leak than two same-model samples would.

### 1d. Eval snapshot (non-blocking)

When the bundle touches `.claude/agents/`, `protocols/`, or
`harness/intents.toml`, record an eval snapshot before dispatching reviewers
so the synthesis can compare against the previous one:

```bash
uv run scripts/eval_run.py --no-semantic
```

Carry the routing score (and any misses) into the synthesis. A drop is a
finding for the reviewers, not an automatic abort; the `eval_regression`
session cue independently escalates drops that reach the vault.

### 2. Dispatch in parallel (one message, multiple tool calls)

Send a **single** assistant message containing both tool calls:

- **Internal reviewer** - native project agent `reviewer`. Prompt: "Run System Diff Review + System Holistic Review on the uncommitted bundle. Walk `git diff` and `git status` yourself. Include the Phase scope brief (what moved, what was deferred). Return: (a) 4-dim score card (Contract integrity, Wiring correctness, Bug absence, Claim fidelity, each 0-10); (b) antipattern scan walking every entry in `protocols/antipatterns.md` (count entries from the file at scan time) with FLAG or N/A-with-reason for each; (c) concern list with severity (BLOCKER / SHOULD-FIX / NICE-TO-HAVE) and `file:line` pointers, minimum 3 or a 'Hunted but not found' section; (d) pre-mortem one-liner; (e) scope clarifier block. Overall verdict per reviewer.md Scoring: APPROVED / NEEDS_REVISION / REJECTED (no APPROVED_WITH_NOTES for system reviews; any dim <6 or missing artifact forces NEEDS_REVISION)."

- **External reviewers (codex + direct-api / DeepSeek Pro)** — one `Bash` call, `timeout: 600000`:
  ```bash
  bash scripts/review.sh
  ```
  (Use `bash scripts/review.sh codex` or `bash scripts/review.sh direct` for one leg only; `gemini` is a legacy mode kept for users with the gemini CLI.) Reports land in `<paths.cache>/review-<timestamp>-{codex,direct}.md`. The script runs both reviewers in parallel, blocks on `wait`, includes untracked files in the diff sent to each, and treats a missing CLI / unset api_env as a soft-skip.

### 3. Synchronous wait (invoker contract)

**The orchestrator MUST block until BOTH tool calls have returned before doing anything else.** No streaming, no partial presentation, no interleaving with user chat.

- If the user sends a message while reviewers are running, acknowledge it in one line and say "reviewers still running — will synthesize once they return."
- Do not start drafting the synthesis until the internal reviewer has produced its handoff AND `bash scripts/review.sh` has exited.
- If the Bash call exits non-zero, read the stderr and the report files anyway (the script writes partial output even on failure) before deciding whether to retry or degrade.

This is a contract at the *invoker* level, not enforced by the script. The script just runs the CLIs in parallel and exits when they're all done; the orchestrator is responsible for not presenting anything until both its own tool calls have finished.

### 4. Synthesize

Only after both dispatches have returned. Read the two report files under `<paths.cache>/review-<timestamp>-{codex,direct}.md`, combine with the internal reviewer's handoff, and present.

**External verdict mapping for system reviews:** External reviewers (codex, direct-api) may emit `APPROVED_WITH_NOTES`. System reviews do not admit a notes-only verdict; treat external `APPROVED_WITH_NOTES` as `NEEDS_REVISION` for the merge ladder. The "notes" themselves still surface as concerns in the synthesis output. This applies only when synthesizing system reviews; session reviews preserve the original verdict.

Synthesis output:

```
## System Review — Phase <N>

### Verdict
<worst verdict wins: REJECTED > NEEDS_REVISION > APPROVED>

Floor check: any internal dim <6 OR any required artifact missing -> NEEDS_REVISION regardless of overall score.

### Required artifacts (internal reviewer)
- Antipattern scan: [complete N/N for catalog entries | missing entries: ...]
- Concern floor: [N surfaced | hunted-but-not-found rationale]
- Pre-mortem: "If this fails within 30 days, most likely cause is: ..."
- Scope clarifier: "What this change does NOT do: ..."

### Blockers
- [source] file:line - issue

### Convergent findings
- issue (flagged by: internal, codex, direct-api)

### Divergent findings
- issue (internal: yes, codex: no, direct-api: no) - resolution: ...

### Scores
- Internal reviewer: X/10 avg across 4 dims; dim floor [passed | BREACHED on <dim>]
- Codex: <one-line summary>
- Direct-api (DeepSeek Pro): <one-line summary>
```

Then ask the user: "Address the blockers and re-review, or proceed to commit?"

## Tiers

The Tier 1-4 ladder is canonical in `protocols/collaboration-matrix.md` § "Review Tiers". This file does not restate the table. If the **Evolver** agent specified a tier in its handoff, honor it; otherwise default to Tier 3 (or Tier 4 for architecture-level bundles).

## Cross-references

- `scripts/review.sh` — the actual invocation (prompts, flags, parallelism)
- `protocols/collaboration-matrix.md` § Review Tiers
- `protocols/agent-handoff.md` → `system-review-request` contract
- `.claude/agents/reviewer.md` → internal reviewer definition
- `.claude/agents/privacy-reviewer.md` → semantic privacy guard (intrinsically dual; both voices fire in Step 1c)
