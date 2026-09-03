# Raw-Indexing Protocol

## Purpose

After Drive → `$OV` ingestion (see [[drive-zk-ingestion.md]]), `$OV/<domain>/raw/` accumulates categorical files. This protocol covers the next layer: **cross-cutting markdown indexes** that make raw lookups one-click via wikilinks.

A raw-index is the navigational complement to the raw archive: raw is the source of truth, the index is the table of contents.

## When to build an index

Build when one of these holds:

- A category has 5+ raw files of related but distinguishable types (ID variants, year-by-year tax docs, vehicle paperwork by registration cycle).
- Files have non-trivial **state** worth tracking (active / expired / superseded / pending).
- Lookup by attribute (holder, jurisdiction, year, vehicle, residence) is more useful than lookup by filename.
- Same logical document has multiple file copies in different raw subtrees (e.g., a passport scan referenced from `<visa-category>/`, `visa/`, and an onboarding folder).

Skip when:

- Files are self-evidently named and never need cross-reference.
- A single existing digest (`timeline.md`, `address-history.md`) already serves the lookup need.
- Only one or two files in the category — direct path is fine.

## Location

| Index scope | Location pattern |
|---|---|
| Single domain (one-domain scope) | `$OV/<domain>/<category>-index.md` |
| Cross-domain identity-level | `<paths.personal>/<category>-index.md` |
| Per-holder (when household has multiple subjects) | `<paths.personal>/<holder>/<category>-index.md` |

Index lives in working tier (`$OV/<domain>/` or `<paths.personal>/`), never under `raw/`. Raw stays unedited per ingestion protocol.

## Required structure

```markdown
---
last_verified: YYYY-MM-DD
canonical: true
scope: <one-line: 索引覆盖什么, 不覆盖什么>
sources:
  - $OV/<domain>/raw/ (relevant raw subtrees)
  - <other authoritative timeline/digest if any>
  - user input <date> (when manual confirmations were folded in)
---

## 用途

<1–2 sentences: who reads this, what question it answers>

## Status legend

| 标 | 含义 |
|---|---|
| ✅ | active |
| ⚠️ | expiring soon |
| 🔴 | expired / superseded |
| ⛔ | missing — 应该有但 $OV/ 找不到 |
| ❓ | status / 日期未确认 |

## <Holder or grouping> — <Name>

| Type | <attr 1> | Status | <date col 1> | <date col 2> | Scan(s) |
|---|---|---|---|---|---|
| <doc kind> | <attr value> | ✅ | <date or TODO> | <date or TODO> | [[vault-relative/path/file.ext]] |
| ... |

(One row per logical document. If the same logical doc has multiple file copies, list each as a separate `[[wikilink]]` in the same cell, separated by ` · ` (middot).)

## ⛔ Missing 优先补

<numbered list, frequent-ask order; or note "none" if complete>

## TODO

<known gaps: dates user must read from scans manually, status syncs deferred, structural cleanup>

## Out of scope (其他类别)

<table or list pointing to where related-but-out-of-scope material lives>

## Update protocol

每次 <category> 类文件事件 (renewal / 新办 / loss / transfer):

1. 表格加新行,旧行 status 改 🔴 superseded / expired
2. raw/ 对应子目录加新文件
3. 触发 cross-domain (e.g., DMV / payroll / health portal / abroad timeline)
4. `last_verified` frontmatter 更新

## Cross-references

- <related digests / timelines / domain READMEs>
```

## Wikilink rules

- **Use vault-relative paths.** Wikilink resolvers expect paths from the vault root, not from the index file's directory. Write `[[abroad/raw/<sub>/<file>.pdf]]`, not `[[../abroad/...]]`.
- **Include extension for non-md files.** PDFs, JPEGs, PNGs need the suffix to resolve: `[[abroad/raw/<sub>/<file>.pdf]]`. Markdown files omit `.md`: `[[abroad/timeline]]`.
- **Multi-file cells separator: ` · `** (U+00B7 middle dot, not comma). Reads cleanly when the cell has 3+ links.
- **Spaces in paths are fine** inside `[[]]` — no escaping needed. Folder names with parentheses (`Scans (old)`) work; avoid square brackets in paths since they collide with link syntax.
- **For directory-level references** (no specific file), use plain backticks not wikilinks: `` `<domain>/raw/<sub>/` ``. Wikilinks don't address folders.
- **Retrofitting plain date cells**: `scripts/log_backlinks.py` converts bare `YYYY-MM-DD` values in markdown-table date columns into `[[YYYY-MM-DD]]` daily-note wikilinks across an index file. Use it when adopting this convention on a pre-existing index.

## Multi-copy / duplicate handling

When the same logical document exists as multiple files (Drive-era duplicates, intentional copies for HR onboarding, etc.):

- **One row per logical doc**, not one row per file copy.
- **List all copies** in the Scan(s) cell, separated by ` · `.
- **Note md5 prefix** if it helps disambiguate "are these actually the same file?": `passport.pdf (md5 61eca2..., 5 处 dup)`.
- **Don't rm duplicates** during indexing — `mv` is the only mutation; dup removal is a separate user-approved pass.

## What NOT to include in an index

- **ID numbers / 证号 / SSN digits / passport numbers / account numbers.** Index records the *location* of scans, never the *contents*. Sensitive values stay in the raw scan (binary) or in encrypted vaults.
- **Dates extracted from PDFs by Claude.** Per [[drive-zk-ingestion.md]] privacy rule, Claude does not open personal-data PDFs just to digest. Date columns get `TODO` and the user fills them manually.
- **Backup of file contents.** The index is metadata only.
- **Decisions / interpretations.** Indexes are descriptive lookup tables, not analysis. Decisions go in dated reflection notes; indexes link to those.

## Example: credentials index

The canonical implementation lives under `<paths.personal>/` (gitignored) as an `<identity>-index.md` covering identification documents. Concrete holder names, jurisdictions, document types, and copy locations stay in the private vault and are out of scope for this committed protocol.

Generic structural elements (use as template):

- Holder × jurisdiction × type axes
- Status legend in use
- Multi-copy handling (single document with multiple raw-subtree copies)
- Out-of-scope routing (other identity-adjacent records deferred to their own indexes)

Use the structure as a template; do not copy private content from the vault implementation into committed protocols.

## Cross-references

- [[drive-zk-ingestion.md]] — how raw files arrive in `$OV/<domain>/raw/` (the prerequisite)
- [[local-first-architecture.md]] — tier model context (indexes live in working tier)
- [[epistemic-hygiene.md]] — validation-depth applies: index entries are alloy unless user-verified (last_verified frontmatter)
