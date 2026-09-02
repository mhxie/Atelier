# Voice Dispatch

Split from `orchestrator.md` (2026-08-24) so call sites load ~4 KB, not 44.
Canonical for per-role voice legs and the multi-leg call-site list.

## Voice Dispatch

Every role declares a `voices` keyed inline table in `harness/agents.toml` mapping leg name to model identity. Three leg types:

- **`native`** - project-agent leg in the selected interactive runtime. Claude Code dispatches the role's `.claude/agents/<role>.md`; Codex dispatches `.codex/agents/<role>.toml`. Claude uses role frontmatter for its model. Codex inherits the selected session model and maps the native voice identity's `reasoning_tier` from `harness/models.toml` to `model_reasoning_effort`.
- **`direct`** - direct-api leg dispatched via `uv run scripts/chat_completion.py --model <identity> --max-tokens 0 --prompt -` with the prompt on stdin.
- **`codex`** - explicit external Codex CLI reviewer leg dispatched via `codex exec`, independently of the selected interactive runtime. Today it is used only by `external-reviewer` through `scripts/review.sh`.

Schema split:
- **What identities and reasoning tiers exist** → `harness/models.toml` (committed)
- **How identities map to providers** → `profile/models.toml` (gitignored; bindings)
- **Which voices each role binds** → `harness/agents.toml` (`voices` per agent)

Protocol prose does NOT enumerate specific model identities per role. That info lives in `harness/agents.toml`; restating it here creates drift on every rebind.

### Dispatch shape per role

`voices` declares each role's INTENDED leg set. Whether all declared legs fire on a given dispatch is the **call site's** decision, not a universal contract. This is intentional: the schema is forward-ready (every role pre-declares its bound pair), but enabling the second leg per dispatch site is a per-call-site decision based on tool needs, write safety, and runtime cost.

Pattern at a multi-leg call site (the orchestrator fires one tool call per leg in the same assistant message):

| Voices declared | Multi-leg dispatch shape |
|---|---|
| `{native = "X", direct = "Y"}` | Selected runtime's native project agent + `chat_completion.py --model Y --max-tokens 0` |
| `{native = "X"}` only | Selected runtime's native project agent only; single-leg by design |
| `{direct = "Y", codex = "Z"}` | `chat_completion.py --model Y` + `codex exec -m Z`; script-driven only (see `scripts/review.sh`) |

The dispatched legs share the same user-facing prompt. Agents whose work depends on tool calls (vault reads, file writes) cannot be perfectly mirrored — the direct-api leg sees the prompt only. Treat the second leg as a cross-check on verdict / framing, not on the tool-driven output.

### Single-leg roles (write-capable carve-out)

Roles that write files or handle verbatim user-authored content declare only the `native` leg. Today: **Scribe** (`voices = {native = "haiku"}`). Rationale: a parallel direct-api leg would either (a) produce duplicate writes or (b) leak verbatim user content to an external API. The selected runtime performs the one write. The single-leg declaration is explicit; lint accepts any non-empty voices table. Add a single-leg carve-out only when the role meets one of these conditions and document why in the agent's description.

### Currently-enabled multi-leg call sites

The multi-leg dispatch shape is enabled at these specific sites today:

- **`/system-review` Step 1c** — privacy-reviewer's `native` + `direct` legs both fire (worked example in that command file).
- **`scripts/review.sh`** — external-reviewer's `direct` + `codex` legs both fire.
- **`/decision` (conditional):** ordinary decisions stay single-leg. For a costly, irreversible, or high-uncertainty choice where independent framing could materially change the outcome, dispatch Thinker's `native` + `direct` legs and surface disagreement before presenting the result.
- **Quantitative-claim outputs (synthesis-time check, all roles, all sites):** whenever an agent's returned output contains quantitative or factual claims covered by CLAUDE.md's always-on invariants, the orchestrator inspects the dispatch shape at synthesis time. If the dispatch was single-leg `native`, the orchestrator either (a) redispatches the role's `direct` leg in parallel as a disagreement detector on the same prompt and diffs numeric / factual claims at synthesis, OR (b) marks every affected claim as `unverified` before write-back. **Option (a) is a disagreement detector, not a verification gate**: two-leg agreement does NOT promote `unverified` claims to verified, because both legs see the same prompt and can co-hallucinate. Verification still requires a primary-source citation. Option (a)'s value is catching disagreement between legs and forcing the claim to `unverified` regardless. **Option (a) requires the role to have a `direct` voice declared; for the Scribe role (single-leg-only by carve-out), option (b) is the mandatory fallback.** This is a post-dispatch gate, not a per-call-site predeclaration; it applies uniformly without requiring every command file to opt in.

Every other dispatch site (the Reading hub in `/hi` for non-fact-bearing reads, ad-hoc Reader / Scout / Curator calls that produce only narrative or capture, single-source restatements) fires only the role's `native` leg. The `direct` leg is declared in `voices` as a forward binding for when that call site opts in. To enable a second leg at any new call site, follow the worked example in `/system-review` Step 1c. Lint does NOT enforce dual dispatch at every call site; the schema is intent, the call site is policy.

**Operational visibility:** when a dispatch on this list runs single-leg because the direct leg soft-skipped (api_env unset, network failure, exit 2), the orchestrator surfaces a one-line warning in its user-facing output: `Cross-provider check downgraded: <role> ran native-only (<reason>).` The user can then decide whether to re-run with the second leg or accept the single-leg result. Silent degradation on these specific sites is a bug.

### Soft-skip on missing api_env

When a `direct`-leg's `api_env` is unset (key not provided in the runtime environment), `chat_completion.py` exits 2 cleanly. Callers MUST handle exit-2 as soft-skip-and-degrade: the dispatch collapses to a single-leg result with a warning surfaced in the synthesis, never a hard failure. Same handling for codex-leg unavailability (codex CLI not installed → exit 127; treated as soft-skip).

### Tier scales cardinality, not voice composition

A "Tier N" gathering = N parallel copies of role-units. The `/system-review` review-ladder (Tier 1-4) governs how many reviewer-units run on a change. This is the only "tier" semantics in this repo. Tier never alters a role's bound voice composition; it only adds more role-units to the gathering. The role's per-agent voice composition lives in `harness/agents.toml`.
