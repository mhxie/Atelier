## Purpose

`$OV` is a plain-Markdown vault that Reflect edits and syncs through Git; these conventions target Reflect.

## Note titles

Reflect titles are flat, so each must be unambiguous without its folder. Every note but a daily note opens with an H1, which Reflect shows as its title; Reflect reads frontmatter `title:` first, so an existing one must match the H1. A generic filename (README, Index, Taxonomy, ...) gets a scoped H1 such as `# <Scope>: <Topic>`; a colon separates scope from topic, never ` · ` or a dash. `scripts/zk_audit.py` flags duplicate titles and missing or conflicting opening H1s.

## Image policy

### Placement

Images live in a sibling `images/` subdir of the markdown that references them:

```
wiki/<Topic>.md
wiki/images/<topic>-overview.png

wip/<slug>.md
wip/images/<slug>-architecture.png

agent-findings/<agent>-<topic>-YYYY-MM-DD.md
agent-findings/images/<topic>-architecture.png
```

For nested tiers, the `images/` is sibling to the .md file at any depth:

```
research/<area>/<Topic>.md
research/<area>/images/<topic>-flow.png
```

Reference syntax (relative path from the .md file's directory):

```markdown
![alt text](images/<topic>-overview.png)
```

### Naming

- **Lowercase kebab-case**: `<topic>-overview.png`, not `Topic_Overview.png` or `TopicOverview.png`.
- **Semantic**: the name describes what the image *shows*, not when it was captured. `<vendor>-cart-checkout.png`, not `Pasted image 20260406173547.png`.
- **Date-leading only when the image is time-bound** (a snapshot of a UI, chart, or receipt at a specific moment): `YYYY-MM-DD-<source>-<artifact>.png`. For evergreen diagrams, omit the date.
- **Avoid hashes, raw timestamps, generic names** (`IMG_7913.PNG`, `image1.png`, `screenshot.png`).
- **Topic prefix when many images share a theme**: `<topic>-overview.png`, `<topic>-flow.png`, `<topic>-internals.png`. Readable at a glance in the directory listing.

### Tracking

`$OV/.gitignore` is a whitelist: Markdown and Reflect's attachment types except video sync from any folder but `raw/`, `secure/`, the archive, nested `assets/`, and top-level paper PDFs. Reflect commits them on its next sync, except files above its `backupMaxFileMiB` guard.

Root `assets/` (Reflect's pasted images) is tracked; Reflect resolves its vault-root links (`assets/x.png`) from any folder. Nested `assets/` imports stay excluded; to publish one, move it to `<tier>/images/` with a semantic name and update the reference. Archive notes may keep broken links.

### Examples in this file

All filenames, paths, topics, and people referenced in the example blocks above and the table below are placeholders. Replace `<topic>`, `<vendor>`, `<source>`, `<author>`, `<lab>`, `<venue>` with concrete strings when applying the convention; do not commit those concrete strings into protocol or convention files (they belong inside `$OV/`).

## Folder size — fission rule

**Magic number: 32 entries (files + subdirectories combined).** When a directory's immediate-child count reaches 32, trigger a fission: split into subdirectories along a natural axis. Most file explorers slow above that range.

The 32 threshold is hard, not "rough". A directory at 32 should be split before the next addition.

### Per-tier split axes

| Tier | Split axis | Result example |
|------|------------|----------------|
| `reflections/` | year-month | `reflections/YYYY-MM/YYYY-MM-DD-reflection.md` |
| `agent-findings/` | year-month | `agent-findings/YYYY-MM/<agent>-<slug>.md` |
| `preprints/<class>/` | venue | `preprints/<class>/<venue><yy>/` |
| `wiki/` (and any localized shadow wikis from `[paths.wiki_localized]`) | topic cluster (semantic) | `wiki/<cluster>/<Topic Title>.md` |
| `research/<area>/labs/` | by org type or first-letter | `research/<area>/labs/<X>/<lab>/` |
| `people/` | first-letter bucket: `A/`, …, `0-9/`, `中/` (CJK) | `people/<X>/<Person Name>.md` |
| `archive/<subdir>` | first-letter bucket or topical sub-grouping (case-by-case) | `archive/<subdir>/<X>/<Item>.md` |

`daily/` is exempt and flat (`daily/YYYY-MM-DD.md`): Reflect reads dailies only there. Its captures land in root `assets/`; older attachments stay in `daily/raw/YYYY/MM/`, off Git.

### Rebuilding refs after any move (canonical workflow)

`[[Title]]` links survive moves; Reflect resolves them by title. Moves break relative image and attachment links `![](path)` and legacy `[X](path.md)` links. The relink contract makes reorganization non-destructive:

```
1. Move files via any tool         (scripts/fission.py / manual mv / one-off scripts)
2. uv run scripts/relink.py --to-reflect --apply   ← fixes broken refs; note links become [[Title]]
3. Reflect commits on its next sync
```

`scripts/relink.py` builds a global filename → location index across all tracked `.md`/image files, scans every `[text](path)` and `![alt](path)` reference, and rewrites broken paths to the file's current location, %-encoded because Reflect cannot open `<...>` destinations. Since refs track filename (not path), any reorganization that doesn't rename files is fully recoverable. Use `--dry-run` first to preview changes. `--to-reflect` converts legacy note links to `[[Title]]`.

### Tier-specific semantic restructures

For one-off semantic reorgs of a single tier, copy `scripts/fission.py` as a starting point and adjust the bucket map. One-off scripts that hardcode private vault content (TOPIC_MAPs, lab lists, wiki entry titles) live in `scripts/oneoff/` (gitignored) and cannot be referenced from committed protocols. The generic reusable tools stay at `scripts/fission.py` and `scripts/relink.py`.

### Cascading splits

A subdir created during fission can itself reach 32 over time and require its own fission. The rule applies recursively. Calendar splits (year-month, year-quarter) self-bound: a month never exceeds 31 entries.

## Forward-going naming (general)

- **Filenames**: lowercase kebab-case unless a proper noun or canonical title (e.g., wiki entries can use `Title Case With Spaces.md` because they're canonical names).
- **Dates**: ISO `YYYY-MM-DD` only. No `MM/DD/YYYY`, no `Thu, August 8th, 2024`.
- **Tags**: `#kebab-case-tag`, `#中文标签`. No pure-digit tags.

## Documentation hygiene (present-tense protocols)

Protocols, agent specs, and shared docs describe how the system works **now**. Git is the archive for past states; restating "earlier versions used X, now retired" inside live docs creates lint debt and confuses new readers.

- Write rules in present tense. State what the system does, not what it stopped doing.
- When superseding behavior, delete the old description and its justification rather than narrating the change. The diff plus commit message is the audit trail.
- Genuinely-deferred work belongs in a single named roadmap subsection (the pattern: `wiki-schema.md` → Open v2 Items), not as scattered "v1 only" / "Phase B" parentheticals.
- Operational pointers to runtime artifacts the system still encounters (e.g., "`#ai-reflection` may appear on historical notes; treat as alloy") are fine because they describe runtime conditions, not system biography.

This rule is enforced by `check_legacy_framing` in `scripts/harness_lint.py`, which scans the routed prose surface.

## Editing discipline

Agent edits mirror neighboring style and requested scope. Clean up your own orphans; surface unrelated bugs or cleanup for the user. Direct user edits remain their discretion.

For harness changes:

- Before edits, record owner, acceptance checklist and `python3 scripts/harness_lint.py --footprint` in the task plan. Use one whole-feature baseline across patches; exclude unrelated worktree changes.
- Fix reproduced bugs in their owner; prefer configuration, reuse and verified redundancy removal. New services/dependencies/entrypoints/persistent state/retry/fallback paths need a separately approved rationale and plan.
- Implementation growth above the report's `growth_review_lines` needs plan reapproval. Lint and `--footprint` enforce implementation/config total and per-file ceilings. Limits live in `scripts/harness_lint.py`: lower after verified cuts; increases need user approval. Budget is not scope authority.
- Report tests, prose and private code/config/prompts separately: the public footprint excludes private content. Don't hide growth by dropping necessary tests, compressing code, or relocating logic; reuse test setup.
- Finish smoke against the agreed checklist; do not narrow it for success. Escalate genuine blockers and report upstream gaps. New requirements or replacement systems need separate approval.
- Deliver whole-feature deltas (`implementation | tests | prose | entrypoints/dependencies/persistent state`), verification and gaps. Include governance/private changes; label unmeasured scope.

## Lint enforcement

Atelier-side lint runs via `uv run scripts/harness_lint.py` (registered names, path-literal templating, doc-indirection cycles, etc.) and `uv run scripts/privacy_check.py`. The privacy gate scans public-bound pathnames, content, and divergent staged blobs against the private-entity index that `scripts/privacy_index.py` derives from the vault (directory names and paths, note stems, wikilinks, routine and feature registries, frontmatter, profile proper nouns, each with provenance; rebuilt when a day old), plus a path rule that flags any real content-tier directory named in prose. Both fire in `/lint`; `/push` runs the mechanical gate over the whole unpushed history (`--range`) and the semantic privacy-reviewer over the same range before anything leaves the machine, and `scripts/hooks/pre-push` repeats the mechanical range scan (`git config core.hooksPath scripts/hooks`). `profile/private_terms.txt` and `profile/private_slugs.txt` stay for what no vault source can derive; the committed allowlist is reserved for deliberately public literals and is honored by both gates. `privacy_check.py --why "<term>"` explains any hit.

Vault-side lint for the conventions in this doc (folder fission, image placement, image naming, ISO date strings) is currently manual. A consolidated `scripts/vault_lint.py` is deferred until two distinct conventions need automated enforcement at once; until then, the fission rule is enforced by `scripts/aggregate_freshness.py` only for self-declaring aggregates, and image / date conventions are honored by hand.

## $OV git push policy

`$OV` is typically a git repo with an optional private remote (private GitHub repo). The atelier never auto-commits or auto-pushes it; the user or a sync client such as Reflect does. `protocols/autoevo.md` writes plain files that the sync client commits; rollback is `git restore`.

Conventions:

- **Push cadence**: at the user's discretion. Reasonable triggers include after a `/promote` that lands a new wiki entry, or at end-of-session if anything material changed.
- **Scope**: whatever the vault's `.gitignore` permits. `cache/` (L1 ephemera) and nested `assets/` (auto-paste hash-named imports) are typically excluded. `_meta/` (routine config, drive aliases) is typically pushed because it holds load-bearing user-private config; users with sensitive content in `_meta/<backend>.toml` may prefer to gitignore it and re-derive per device. The atelier does not dictate the vault's gitignore.
- **Threat model**: the private remote is one credential away from disclosing the entire knowledge base. Treat it like a password vault. On suspected compromise: rotate the GitHub token, audit recent pushes, and consider a fresh repo with selectively replayed history (the rewrite path is destructive; document the recipe before needing it).
- **Staleness**: no atelier cue surfaces "remote is N commits behind." Users who want that signal wire it as a local cron or shell prompt indicator. Acceptable trade-off because the local $OV is the authoritative copy and Drive sync provides the device-level redundancy.
