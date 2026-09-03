---
description: Publish local commits after both privacy gates pass over the whole unpushed range.
---

Outcome: every commit that would leave this machine has passed the mechanical
and the semantic privacy gate, then `git push` runs.
Done when: both gates report clean for `<remote>..HEAD` and the push succeeds,
or a finding is reported with `file:line` and nothing is pushed.
Evidence: the two gate outputs and the push result.
Output: one short report; never a leak's content, only its location.

## Step 1: Range

```bash
git fetch -q origin
RANGE="$(git merge-base origin/main HEAD)..HEAD"
git log --oneline "$RANGE"
```

An empty range means nothing to publish; stop.

## Step 2: Mechanical gate over history (blocking)

```bash
uv run scripts/privacy_check.py --range "$RANGE" --json
```

`hits` must be empty. A hit carries `why` (which vault source produced the
term) and the commit it lives in. Fix the offending commit (amend or
`git rebase -x` with the same replacement in every affected tree), never by
adding the term to `scripts/privacy_allowlist.txt` unless it is deliberately
public. `uv run scripts/privacy_index.py why "<term>"` explains an
unexpected hit. Re-run until clean.

## Step 3: Semantic gate over the same range (blocking)

Dispatch `Agent (subagent_type=privacy-reviewer)` with:

> Privacy review the commits in `$RANGE` before they are pushed to a public
> remote. Walk `git diff <base>..HEAD` and `git log -p <base>..HEAD` yourself
> (intermediate commits count: a name that was added and later removed still
> ships in history). Honor `scripts/privacy_allowlist.txt` as deliberate
> public opt-outs. Cross-reference `profile/` for identity, employer, health,
> financial, relationship, and taxonomy leaks the mechanical scanner cannot
> see. Return CLEAN, or findings as `commit:file:line` with a one-line reason
> and a neutral replacement; never quote more than the offending token.

Any finding blocks the push: fix the history as in Step 2, then rerun both
gates. Do not proceed on NEEDS_REVISION.

## Step 4: Push

```bash
git push origin HEAD
```

Report the range, both gate results, and the push output. If
`scripts/hooks/pre-push` is installed (`git config core.hooksPath
scripts/hooks`), it repeats Step 2 as a last line of defense; a hook failure
after a clean Step 2 means the index changed underneath, so rebuild it
(`uv run scripts/privacy_index.py build --force`) and rerun.
