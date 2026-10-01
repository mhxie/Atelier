---
description: Prefect-only nightly decay proposal; trusted parent owns live publication and verification.
---
# Autoevo routine adapter

Produce one structured proposal for the trusted Prefect parent. Follow
`protocols/autoevo.md` for authority, trust bands and recovery. Do not launch
this routine interactively: without the adapter-provided
`AUTOEVO_WORKSPACE/plan.json`, refuse and direct the caller to the reviewed
Prefect entrypoint. Never create a replacement plan or start nested Codex.

Live vault notes/state are not model inputs. Use only the retained plan and
workspace snapshots. You author `$AUTOEVO_WORKSPACE/proposal.json`; preview may
write its explicit workspace scratch directory. Do not edit plan, snapshots,
Git, queue, ledger, quarantine, semantic index, or live reports/notes.
Do not publish, commit, run lint, or mutate live state. No installs, downloads,
index rebuilds, push, or automatic rollback.

## 1. Read the trusted inputs

Read `plan.json`, this protocol's trust bands, and the relevant handoff
contracts. `scripts/autoevo_verify.py::proposal_schema(plan)` is the schema
owner; use the adapter's `schema.json` when supplied. Keep the cycle and
every `dispatches[].scope` exactly; do not infer dates or resolve another
private configuration.

The plan supplies ordered dispatches and caps, `protected_paths`, and
`source_files` mapping original vault-relative paths to clean snapshots.
Use those snapshots as note-operation inputs. Never propose an operation on
a protected or unlisted source. Search results do not expand authorization.

Use existing QMD through the supplied environment: its derived database is
staged; cached models stay at their original read-only location. Bounded
queries do not update the live index. Preserve the retained plan's
`retrieval_mode` exactly: `qmd` cannot be relabelled `real` to reach an
automatic band, and its scores never authorize merging. Read candidate/peer
content before reporting overlap. A retrieval failure is a gap, not negative
evidence.
When a direct query is needed, invoke
`"$ATELIER_PYTHON" "$ATELIER_ROOT/scripts/semantic.py" query ...`; the
scratch working directory has no relative `scripts/` tree, and `uv run` could
attempt cache or environment writes outside the authorized workspace.

## 2. Gather bounded sweeps

Run Forgetter sequentially for each planned scope, preserving its snapshots,
`max_candidates`, `time_budget_s`, protected paths and original path identities.
For every role below, use native dispatch. If unavailable or explicitly rejected
before a child starts, follow AGENTS.md: read its canonical brief and handoff
contract, then perform that role inline with the same limits and evidence.
Disclose emulation and the dispatch error in notes. Never emulate a started or
ambiguously started child; retain its missing/failed envelope instead.

Load `protocols/agent-handoff.md` common metadata and Forgetter contract.
Require `---forgetter-result---` / `---end-result---` and validate the
envelope before accepting findings:

- Full + complete: `outcome: envelope_returned`, `mode: full`,
  `completion_status: complete`, no remaining work.
- Partial + partial: also `envelope_returned`; retain the cap reason,
  remaining work and gaps. Put `forgetter_partial: ...` in sweep Notes.
  A returned partial envelope is not itself an Error. Use only findings
  independently supported despite its gaps.
- Missing envelope: `outcome: forgetter_no_envelope`, `mode: absent`,
  `completion_status: aborted`, empty findings, explicit failure reason.
  Continue to the next scope without retrying this dispatch.
- Invalid/aborted envelope: use the same aborted, no-accepted-envelope row
  with empty findings; add the invalid-return reason to `errors`. Never
  repair it by inventing successful coverage.

Create one sweep row per planned scope, including empty results; the plan owns
quarantine filtering.

## 3. Attach operation and contradiction proposals

Normalize supported findings to the schema's categories and required fields:
`category`, `candidate`, `confidence`, `evidence`, `proposed_action`.
Keep applicable `peers`, `scores`, retrieval `mode`, `conditions_met`,
`claim`, `contradicting_peer`, and `contradiction_signal`. Keep concrete
source observations in `evidence`; do not invent missing measurements.
Confidence omitted by an older return becomes medium, never automatic
authority.

For potentially eligible operations under the protocol's bands, run
Curator sequentially with the original source identities, their trusted
snapshot paths, `mode: auto-apply`, matching band and full finding evidence.
Attach its complete parsed envelope as `curator`, including a refusal.
Curator drafts only; it cannot write live content.

Automatic eligibility requires explicit `auto_apply_safe: true`,
`completion_status: complete`, no remaining work and all preservation
evidence. A merge's `target_path` is the oldest snapshot source identity;
`proposed_content`, media inventories and content-integrity fields must
preserve the complete source material. Use original vault-relative target
paths. Do not hide a refusal, missing check or required split. The parent
will independently recheck the band and contents.

For contradictions, run Challenger with the claim,
contradicting peer and signal using its contradiction-probe contract.
Attach the parsed envelope as `probe`. Only a complete, gap-free rhetorical
verdict can dismiss the finding; genuine or unproven contradictions remain
for human review. Never rewrite a wiki claim.

## 4. Preview policy and obtain precedent judgments

Write a schema-valid draft proposal with all sweeps and an initially empty
`judgments` object. The only Autoevo CLI you may invoke is:

```bash
"$ATELIER_PYTHON" "$ATELIER_ROOT/scripts/autoevo_run.py" preview \
  --proposal "$AUTOEVO_WORKSPACE/proposal.json" \
  --plan "$AUTOEVO_WORKSPACE/plan.json" \
  --directory "$AUTOEVO_WORKSPACE/preview"
```

The preview returns trusted-policy routing, pending entries, notes and
`bundles`. It mutates only its scratch state, never live state. Its output
is advisory; the parent later recomputes it against the retained plan.

For each bundle, run precedent-judge with the exact supplied
`prompt` and request its JSON response inline, without writing another
artifact. Write `judgments[entry_id] = {bundle_sha256, judgment}`, using the
returned hash unchanged. Do not invent a verdict, edit a bundle, substitute
another entry or call a hosted model directly. No bundles means no judgments,
not failure. After changing findings or Curator/probe content, refresh preview
in a fresh scratch directory and obtain judgments for the final bundles;
do not carry stale responses forward.

Do not run `precedent.py` setters or queue/ledger commands yourself. Missing,
incomplete or unsupported judgments leave entries human-only.

## 5. Return the proposal

The top-level object has `schema_version`, `cycle_id`, `sweeps`,
`judgments`, `notes`, `errors`. Each sweep has `scope`, `outcome`,
`mode`, `completion_status`, `remaining_work`, `gaps`, `findings`,
`notes`. Follow the generated schema; do not add ad-hoc fields or old
sidecars. Preserve honest partial/failure evidence if interrupted.

After writing the candidate, return only the generic transport-acknowledgment
JSON required by the supplied output schema: name `proposal.json` as
`output_file`, summarize coverage, and use `outcome: delivered` only to mean
the candidate file is ready for the parent. It is not live delivery. The
trusted parent validates, writes per operation, updates state, lints, derives
human reports, and verifies its receipt after you return. A write failure is
not permission to replay the model or roll back the vault.
