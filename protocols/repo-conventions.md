## Purpose

GitHub-canonical conventions for `$OV` — the user's markdown vault. The typical setup is a git repo with a private remote, but these rules apply equally when $OV is a plain folder synced via Google Drive / iCloud (the conventions still make GitHub-style rendering work when the user views via web). They optimize for: clean GitHub UI rendering, efficient agent navigation, sustainable folder browsing, and a plain-markdown vault that stays ecosystem-agnostic (no editor-specific carry-over).

Companion: `protocols/semantic-vocabulary.md` (backlink and tag conventions).

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

`$OV/.gitignore` whitelists `**/images/*.{png,jpg,jpeg,gif,svg,webp}`. Place an image under any `images/` subdir and it auto-tracks on next `git add`.

`assets/` (auto-paste collectors, hash-named imports from prior tools) is re-excluded; any `assets/images/<hash>.png` style import stays local. To promote an `assets/` image to GitHub, move it to the right `<tier>/images/` path with a semantic name and update the markdown reference.

### Legacy image refs

Markdown that still points at `assets/images/<hash>.png` will render broken on GitHub. Acceptable for archive content; for active content, promote on next edit by:
1. `cp assets/images/<hash>.png <tier>/images/<semantic-name>.png`
2. Update the `![](path)` reference in the .md.
3. `git add -A`.

### Examples in this file

All filenames, paths, topics, and people referenced in the example blocks above and the table below are placeholders. Replace `<topic>`, `<vendor>`, `<source>`, `<author>`, `<lab>`, `<venue>` with concrete strings when applying the convention; do not commit those concrete strings into protocol or convention files (they belong inside `$OV/`, which is gitignored).

## Folder size — fission rule

**Magic number: 32 entries (files + subdirectories combined).** When a directory's immediate-child count reaches 32, trigger a fission: split into subdirectories along a natural axis. GitHub's tree view truncates long lists and pagination breaks navigation; most file explorers also slow above that range.

The 32 threshold is hard, not "rough". A directory at 32 should be split before the next addition.

### Per-tier split axes

| Tier | Split axis | Result example |
|------|------------|----------------|
| `daily-notes/` | year, then month (two-level grouping keeps every level under 32; filename carries full ISO date) | `daily-notes/YYYY/MM/YYYY-MM-DD.md` |
| `reflections/` | year-month | `reflections/YYYY-MM/YYYY-MM-DD-reflection.md` |
| `agent-findings/` | year-month | `agent-findings/YYYY-MM/<agent>-<slug>.md` |
| `preprints/<class>/` | venue | `preprints/<class>/<venue><yy>/` |
| `wiki/` (and any localized shadow wikis from `[paths.wiki_localized]`) | topic cluster (semantic) | `wiki/<cluster>/<Topic Title>.md` |
| `research/<area>/labs/` | by org type or first-letter | `research/<area>/labs/<X>/<lab>/` |
| `people/` | first-letter bucket: `A/`, …, `0-9/`, `中/` (CJK) | `people/<X>/<Person Name>.md` |
| `archive/<subdir>` | first-letter bucket or topical sub-grouping (case-by-case) | `archive/<subdir>/<X>/<Item>.md` |

### Rebuilding refs after any move (canonical workflow)

File moves break standard markdown links `[X](path.md)` and image embeds `![](path)`. The relink contract makes reorganization non-destructive:

```
1. Move files via any tool         (scripts/fission.py / manual mv / one-off scripts)
2. uv run scripts/relink.py --apply   ← auto-fixes broken refs
3. Commit
```

`scripts/relink.py` builds a global filename → location index across all tracked `.md`/image files, scans every `[text](path)` and `![alt](path)` reference, and rewrites broken paths to the file's current location. Since refs track filename (not path), any reorganization that doesn't rename files is fully recoverable. Use `--dry-run` first to preview changes.

The wikilink converter (`scripts/wikilink_to_md.py`) handles `[[...]]` → standard markdown link conversion when needed. `relink.py` is the steady-state tool for all path moves once links are standard markdown.

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

This rule is enforced by `protocols/antipatterns.md` → #10 Legacy framing in living docs, scanned by the Evolver self-check and Reviewer System modes on Tier 2+ changes.

## Editing discipline

When making edits inside this repo (code, protocols, agents, commands, scripts, or markdown content):

- **Match existing style and conventions.** Mirror surrounding indentation, naming, comment style, prose voice, and structure. Wiki entries are not daily-note voice; daily-note voice is not protocol voice. Reading the few neighboring lines is cheaper than imposing a foreign style.
- **Surgical changes only.** Don't "improve" adjacent code, comments, or formatting that wasn't part of the request. Every changed line should trace to what was asked. If a line changed for any other reason, undo it or surface it in the response.
- **Surface, don't silently fix.** If you notice unrelated dead code, pre-existing bugs, broken links, or style issues, mention them so the user can decide; don't bundle a silent fix with an unrelated edit.
- **Clean up your own orphans, not others'.** When your changes orphan imports, variables, links, or sections, remove those. Don't remove pre-existing dead material unless explicitly asked.

These rules apply to agent edits on the user's behalf. Direct user edits to their own working tree are user discretion; the rules shape what an agent does when delegated to edit.

Reviewer-side detection of violations: `protocols/antipatterns.md` § 8 (Scope creep past the stated criterion).

## Lint enforcement

Atelier-side lint runs via `uv run scripts/harness_lint.py` (registered names, path-literal templating, doc-indirection cycles, etc.) and `uv run scripts/privacy_check.py`. The privacy gate scans public-bound pathnames, content, and divergent staged blobs against the private-entity index that `scripts/privacy_index.py` derives from the vault (directory names and paths, note stems, wikilinks, routine and feature registries, frontmatter, profile proper nouns, each with provenance; rebuilt when a day old), plus a path rule that flags any real content-tier directory named in prose. Both fire in `/lint` and `/system-review`; `/push` runs the mechanical gate over the whole unpushed history (`--range`) and the semantic privacy-reviewer over the same range before anything leaves the machine, and `scripts/hooks/pre-push` repeats the mechanical range scan (`git config core.hooksPath scripts/hooks`). `profile/private_terms.txt` and `profile/private_slugs.txt` stay for what no vault source can derive; the committed allowlist is reserved for deliberately public literals and is honored by both gates. `privacy_check.py --why "<term>"` explains any hit.

Vault-side lint for the conventions in this doc (folder fission, image placement, image naming, ISO date strings) is currently manual. A consolidated `scripts/vault_lint.py` is deferred until two distinct conventions need automated enforcement at once; until then, the fission rule is enforced by `scripts/aggregate_freshness.py` only for self-declaring aggregates, and image / date conventions are honored by hand.

## $OV git push policy

`$OV` is typically a git repo with an optional private remote (private GitHub repo). The atelier does not auto-commit or auto-push; both are user-driven. **Named exception:** `protocols/autoevo.md` defines a nightly autonomous decay sweep that auto-commits each op to `$OV` so `git revert` is the recovery path. It still never auto-pushes; push remains user-driven for all flows.

Conventions:

- **Push cadence**: at the user's discretion. Reasonable triggers include after `/sync` (which produces a parent commit absorbing zettelm digests), after a `/promote` that lands a new wiki entry, or at end-of-session if anything material changed.
- **Scope**: whatever the vault's `.gitignore` permits. `cache/` (L1 ephemera) and `assets/` (auto-paste hash-named imports) are typically excluded. `_meta/` (routine config, drive aliases) is typically pushed because it holds load-bearing user-private config; users with sensitive content in `_meta/<backend>.toml` may prefer to gitignore it and re-derive per device. The atelier does not dictate the vault's gitignore.
- **Threat model**: the private remote is one credential away from disclosing the entire knowledge base. Treat it like a password vault. On suspected compromise: rotate the GitHub token, audit recent pushes, and consider a fresh repo with selectively replayed history (the rewrite path is destructive; document the recipe before needing it).
- **Staleness**: no atelier cue surfaces "remote is N commits behind." Users who want that signal wire it as a local cron or shell prompt indicator. Acceptable trade-off because the local $OV is the authoritative copy and Drive sync provides the device-level redundancy.
