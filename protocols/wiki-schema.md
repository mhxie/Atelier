# Wiki Schema

The structural format for a note that lives under `<paths.wiki>/`. Location is the certification: a note is a wiki entry by virtue of being in `<paths.wiki>/`, not by carrying any tag. Wiki entries are parseable by `scripts/trust.py` and have claim-level granularity in the trust graph. Notes outside `<paths.wiki>/` are alloy by default (see `epistemic-hygiene.md`).

Design rationale for location-based certification and claim-level trust lives in the architecture review ledger under `$OV/research/`, not here; this file carries only the operative schema. The `#solo-flight` tag lives orthogonally to the schema and marks unstructured pure-human capture, which is location-independent (see `epistemic-hygiene.md`).

## Session-Visible Markers

Because wiki entries live under `<paths.wiki>/` and nothing outside that directory participates in the trust graph, a reader scanning a session (the orchestrator, a subagent, or the user skimming chat) has no visible cue that a referenced file is wiki-grade. The file-path prefix `<paths.wiki>/` is the cue. When agents cite a wiki entry in session output, they cite by path (`<paths.wiki>/<title>.md`), not by bare note title, so the certification is legible inline. Notes outside `<paths.wiki>/` continue to be cited as `[[Note Title]]`. A `[[Note Title]]` reference in any session output is alloy by default; a `<paths.wiki>/...` path reference is wiki-grade. Mixing the two forms in one citation (e.g., `[[<paths.wiki>/foo]]`) is a schema violation the Reviewer flags.

Wiki entries are structured around claims, not paragraphs: each claim has its own anchor set and trust score, and note-level aggregation is a derived view.

## Note Structure

A wiki entry has three required sections and lives in `<paths.wiki>/`. The full layer model is documented in `protocols/local-first-architecture.md`.

A wiki entry MUST begin with an `# <Title>` H1 line matching the filename. `scripts/trust.py` derives the note title from the first `# ` heading and reports `missing H1 title` (a structural-integrity failure that blocks the trust floor) when it is absent. This is the one documented exception to the global no-H1 writing rule in `CLAUDE.md`: that rule governs non-wiki markdown (daily notes, research, reflections), but wiki entries require the H1 so the trust engine can identify and cross-cite them. After the H1, the body may carry an optional one-line `>` blockquote primer, then opens with `## Summary`. (The skeleton example below elides the H1 line for brevity; in a real entry it is required and is line 1.)

```markdown
## Summary

One- to three-paragraph synthesis. Prose. No anchors here — the synthesis is alloy on top of the claims and is not separately scored.

## Claims

### [C1] One-sentence claim text

Optional body paragraph(s) elaborating the claim. Verbatim quotes from anchors should appear here, attributed. ^c1

```anchors
@anchor: s2:gyongyi-vldb-2004 | valid_at: 2026-04-06
@pass: reviewer | status: verified | at: 2026-04-06
```

@cite: [[PageRank fundamentals]] | valid_at: 2026-04-06

### [C2] One-sentence claim text

Body. ^c2

```anchors
@anchor: arxiv:2501.13956 | valid_at: 2026-04-06
@anchor: url:https://github.com/getzep/graphiti | valid_at: 2026-04-06
```

## Revision Log

- 2026-04-12: [C2] anchor `arxiv:2501.13956` invalidated — paper retracted. See @cite [[Graphiti retraction note]].
- 2026-04-06: Initial draft. Claims [C1], [C2] anchored from scout brief sources.
```

**Revision log ordering: latest entry first.** New rows go at the top of the list, not the bottom. The most recent change is almost always the one the reader needs; paging to the bottom of a long log to find it wastes attention. This is a human convention, not a parser-enforced rule — `scripts/trust.py` ignores the Revision Log entirely.

The `## Summary`, `## Claims`, and `## Revision Log` headings are required, below the `# <Title>` H1 mandated above. Topic tags (regular Obsidian-style hashtags) are allowed but not required and play no role in the trust engine.

## Claim Block Identifiers (`^cn`)

Wiki entries need two independent addressing mechanisms for each claim: one the trust parser reads, and one a wikilink resolver reads. They live in the same claim block and do not interfere.

**Why `[[note#Cn]]` does not work.** Heading-based wikilink resolution matches the **literal heading text**, not a prefix or token inside it. A claim heading `### [C1] The concept has structural property X...` has the literal text `[C1] The concept has structural property X...`, not `C1`. A link written as `[[Sample Wiki Entry#C1]]` looks for a heading literally named "C1", finds nothing, and silently fails — no error, no navigation, no warning. This is a real recurring footgun; every agent writing backlinks from alloy notes to wiki claims has tripped it at least once.

**The convention.** Each claim carries a block identifier `^cn` (lowercase `c`, matching the claim number) placed inline at the end of the last body paragraph of the claim, immediately before the fenced `anchors` block. Block identifiers may consist only of Latin letters, numbers, and dashes, so human-readable `^c1`, `^c2`, ... are valid and deliberately mirror the `[Cn]` heading numbering.

```markdown
### [C1] The concept has structural property X under condition Y

The mechanism description goes here: what the concept is, how it behaves,
why the property holds. Prose runs for one or more paragraphs. ^c1

```anchors
@anchor: url:https://example.com/source/ | valid_at: 2026-04-08
@pass: reviewer | status: verified | at: 2026-04-08
```
```

The `^c1` marker is a **sibling of the claim text**, not a sibling of the fenced anchors block. Place it at the end of the last prose paragraph. A wikilink resolver then matches `[[Sample Wiki Entry#^c1]]` to that exact location and scrolls the reader to it.

**Citing a claim from an alloy note.** Use the block-ID form: `[[Note Title#^c1]]`. This is how learning packs, daily notes, session reflections, and any other alloy content under `$OV/` should point at a specific wiki claim when they want fine-grained navigation.

**Citing a claim from session output (orchestrator or subagent chat).** Keep using the path form described in "Session-Visible Markers" above: `<paths.wiki>/Note Title.md [C1]`. The session-visible path citation is about legibility of the certification tier in chat, not about click-through navigation, so block IDs are not involved.

**Citing a claim from another wiki entry's `@cite`.** Use the same `#^cn` block-ID form: `@cite: [[Note Title#^c1]] | valid_at: ...`. The trust parser extracts the note title and claim number from this syntax, and a wikilink-aware viewer renders it as a clickable link that navigates to the claim. This is the unified notation: one syntax works for both the trust graph and viewer navigation.

**Parser impact: none.** `scripts/trust.py` walks claim headings structurally and ignores trailing `^cn` tokens in body text. The structural-integrity check treats a claim body containing `^c1` at the end exactly the same as one without. The schema contract is unchanged — adding `^cn` markers is purely additive for human navigation.

**When `^cn` is recommended.** Any wiki entry that is (or is expected to be) cited at claim granularity from alloy notes should carry `^cn` markers on every claim. In practice: add them by default when authoring a new wiki entry. The cost is one line per claim; the benefit is that future backlinks work without retrofitting. **Absence is a `/lint` WARN, not an ERROR** — a wiki entry without `^cn` markers is still valid, still parses, still scores. The lint warning exists to nudge authors toward the convention so cross-note navigation keeps working, not to reject entries at ingestion time. Pre-existing entries without markers may be retrofitted opportunistically and are not schema violations.

**`^cn` is the only block-ID family allowed in wiki entries.** No `^summary`, `^fig1`, `^table2`, `^revlog-2026-04-08`, or any other block-ID shape. The claim is the atomic unit of trust in the schema; everything else (Summary, Revision Log, anchors fence, any future structural element) is metadata around claims and does not get its own navigation handle. Rationale:

- **Sub-claim granularity is a smell, not a feature.** If you find yourself wanting `^c1-part2` or `^c1a`, the claim is actually two claims and should be split. Splitting preserves the trust-graph granularity: each half gets its own anchors, its own Reviewer pass, its own score. A sub-block ID would let authors evade that pressure and produce "one big claim with five unrelated anchors" notes — precisely the pattern claim-level granularity exists to prevent.
- **Summary doesn't need one.** The bare `[[wiki-entry]]` link already opens the note at the top. A `^summary` block ID would be redundant navigation to the same destination.
- **Revision Log doesn't need one.** Revision log entries are historical metadata, not things anyone cites from elsewhere. If a specific revision matters enough to cite, promote its substance to a claim or a standalone note.
- **Lint stays trivial.** A single regex (`\^c[0-9]+$`) validates every block ID in `<paths.wiki>/`. Mixing id families would force `/lint` to maintain a taxonomy of which shapes are allowed where.

A block ID outside the `^cn` family in a wiki entry is a schema violation that `/lint` flags as ERROR (distinct from the WARN for merely missing `^cn` markers on claims).

## The Marker Vocabulary

`@anchor` and `@pass` markers live inside fenced code blocks with the language label `anchors`, one marker per line, pipe-separated key-value pairs. The fenced format for these two marker types is non-negotiable because they contain URLs and structured data where code formatting is appropriate, and `scripts/trust.py` parses them by fence label.

`@cite` markers live **outside** the fenced block, as regular Markdown lines immediately after the closing ` ``` `. This lets wikilink-aware viewers render the `[[wikilink]]` targets as live backlinks in the graph view and backlinks panel. The parser accepts `@cite` lines both inside and outside fences for backward compatibility, but new entries must place `@cite` outside the fence.

### `@anchor`

An external source. This is a **seed** in the trust graph: only `@anchor` markers contribute initial trust mass to the personalized PageRank.

```
@anchor: <type>:<id> | valid_at: <YYYY-MM-DD> [| invalid_at: <YYYY-MM-DD>] [| weight: <float>] [| readwise: <document_id>]
```

Anchor types and id formats:

| Type | ID format | Example |
|---|---|---|
| `s2` | Semantic Scholar paper ID | `@anchor: s2:649def34f8be52c8b66281af98ae884c09aef38b` |
| `arxiv` | arXiv ID | `@anchor: arxiv:2501.13956` |
| `doi` | DOI | `@anchor: doi:10.14778/3402707.3402711` |
| `isbn` | ISBN-13 | `@anchor: isbn:9780262035613` |
| `url` | full URL | `@anchor: url:https://maggieappleton.com/ai-dark-forest` |
| `gist` | GitHub gist URL | `@anchor: gist:https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f` |

**URL escaping rule.** Marker fields are pipe-separated, so URLs (in `url` and `gist` anchors and in `ref:` fields) must not contain literal pipe characters. If a URL contains `|`, encode it as `%7C` before storing it in the marker. The parser will not try to be clever about pipe placement; it splits on the first occurrence of ` | ` (space-pipe-space) per line, then on `:` for each field's key. Multi-line values are not supported; each marker is exactly one line.

`weight` is optional and defaults to `1.0`. For papers, weight may be set to `s2.influentialCitationCount`-derived values or OpenAlex FWCI when the user wants to bias trust toward higher-quality anchors. The trust engine treats all weights as `1.0` unless explicitly set.

### Anchor Evidence Resolution

Every `@anchor` marker claims "this external source existed and supported this claim on the `valid_at` date." The evidence backing that claim must be durable (L3), not ephemeral (L1). The resolution chain defines where to find the source content for each anchor type:

| Anchor type | Durable evidence (L3) | Ephemeral cache (L1) | Last resort |
|---|---|---|---|
| `url:` | **Readwise** (by `readwise:` document ID) | `<paths.cache>/web-*.md` (current session only) | WebFetch |
| `gist:` | **Readwise** (save the gist URL) | `<paths.cache>/web-*.md` | WebFetch |
| `s2:` / `arxiv:` / `doi:` | `<paths.papers>/` (local PDF + review notes) | — | `sources/cite.py` |
| `isbn:` | (no local evidence expected) | — | Manual verification |

**The `readwise:` field.** Optional on all anchor types, recommended on `url:` and `gist:` anchors. Contains the Readwise document ID (e.g., `01kk0zpka139am1v9jftnae9dw`). When present, the full source content can be retrieved via `readwise reader-get-document-details --document-id <id>` regardless of whether the URL is still live. Readwise snapshots web content at save time and stores it permanently.

**Authoring workflow.** When creating a wiki entry with `url:` anchors:
1. Check if the URL is already in Readwise: `readwise reader-search-documents --query "<url>"`
2. If not, save it: `readwise reader-create-document --url "<url>" --tags anchor-evidence`
3. Add the document ID to the anchor marker: `| readwise: <id>`
4. Optionally snapshot to `<paths.cache>/web-<slug>.md` for current-session agent use (ephemeral; will be cleaned up)

Tag convention: Readwise saves that back wiki anchors carry the `anchor-evidence` tag. This makes them discoverable via `readwise reader-list-documents --tag anchor-evidence`.

**`/lint` behavior.** A `url:` or `gist:` anchor without a `readwise:` field is a WARN, not an ERROR. The anchor is still valid; the evidence is just harder to retrieve if the URL goes down. Pre-existing anchors without `readwise:` fields may be retrofitted opportunistically.

### `@cite`

An internal pointer to another wiki entry. This is an **edge** in the trust graph: it propagates trust from the cited note's claims to this claim. `@cite` markers do not contribute initial mass.

**Placement:** `@cite` markers are placed **outside** the fenced `anchors` block, as regular Markdown lines immediately after the closing ` ``` `. This lets wikilink-aware viewers render the `[[wikilink]]` targets as live backlinks in the graph view and backlinks panel. The parser also accepts `@cite` inside fences for backward compatibility, but new entries must place `@cite` outside.

**Unified block-level citation:** Claim-level references use the block reference syntax `#^cn` inside the wikilink brackets:

```
@cite: [[Note Title#^c3]] | valid_at: <YYYY-MM-DD> [| invalid_at: <YYYY-MM-DD>]
@cite: [[Note Title]] | valid_at: <YYYY-MM-DD>
```

The `#^cn` suffix points at a specific claim via its block ID. A wikilink-aware viewer renders this as a single clickable link that navigates directly to the claim. `scripts/trust.py` parses the note title and claim number from the same syntax. Without the suffix, the citation points at the note as a whole and uses the note-level aggregate score as the upstream signal.

`@cite` markers must resolve. A `@cite` to a note that does not exist in `<paths.wiki>/`, or a `@cite` with a `#^cn` suffix to a non-existent claim, is a **dangling internal cite** — caught by structural-integrity check, fails the floor.

### `@pass`

A record of an internal agent pass: Reviewer, Challenger, Thinker, or other team agents. **`@pass` markers never accumulate trust.** They serve two purposes:

1. **Audit trail.** They show what scrutiny the claim has survived.
2. **Floor trust eligibility.** A wiki entry that has at least one `@pass: reviewer | status: verified` and passes structural integrity becomes eligible for the claim-level floor trust of 0.1 on its unanchored claims.

```
@pass: <agent> | status: <verified|flagged|inconclusive> | at: <YYYY-MM-DD> [| ref: <session-id-or-note>]
```

`<agent>` is one of: `reviewer`, `challenger`, `thinker`, `scout`, `curator`. The optional `ref` field points at the session reflection or another note where the pass was recorded, for audit.

## Bi-temporal Anchors

Every marker carries `valid_at`, the date the marker was added. Markers can later be invalidated by adding `invalid_at`. The original line is **never deleted**; the invalidation is an additive change. This preserves the answer to "what did the system believe at time T?"

Example evolution:

```
@anchor: arxiv:2501.13956 | valid_at: 2026-04-06
```

Later, after the paper is retracted:

```
@anchor: arxiv:2501.13956 | valid_at: 2026-04-06 | invalid_at: 2026-04-12
```

The `Revision Log` section at the bottom of the note records the change in human-readable form, with a `@cite` to the note that explains the invalidation if there is one.

`scripts/trust.py` filters markers by `invalid_at` when computing current trust: a marker with `invalid_at <= today` is excluded from the graph. The original record is preserved on disk forever. This is the Graphiti-style append-only-but-mutable contract.

The trust engine treats all valid markers as equal weight regardless of age. Temporal decay (β = 0.9 per month from Temporal PageRank, Rozenshtein & Gionis 2016) is tracked under § Open v2 Items.

## The Trust Propagation Rule

This is the rule that makes the design work. State it bluntly so it never drifts.

> **External anchors are the only seeds of trust. Internal `@cite` edges propagate trust. Internal `@pass` markers never accumulate trust — only floor it.**

In TrustRank terms: `personalization` is the dict of anchor-bearing claim nodes. Non-anchored claims get `0` initial mass. Personalized PageRank then propagates that mass through `@cite` edges. The damping factor (typically `0.85`) handles cycles natively.

`@pass` markers do not become nodes in the graph. They are metadata on existing claim nodes. Their only effect is gating the structural-integrity check that gates the claim-level floor.

This rule is the structural answer to Karpathy's failure mode (`epistemic-hygiene.md` → "Karpathy's failure mode"). Internal agent re-review, no matter how thorough, can never make a claim more trusted than its anchors warrant. It can only hold the line.

## Claim-Level Floor Trust

Once the personalized PageRank has run, apply the floor. **For this check, "passes structural integrity" means items 1-10 of the structural-integrity check below pass.** Item 11 (the `@pass: reviewer | status: verified` requirement) is the second condition in the pseudocode and is not folded into "structural integrity" itself.

```
for each claim Ci in note N:
    if N lives under <paths.wiki>/
       and N passes structural integrity (items 1-10)
       and N has at least one @pass: reviewer | status: verified
    then:
        Ci.score = max(Ci.score, 0.1)
```

The floor is **claim-level**, not note-level. Every claim in a structurally-valid, reviewer-passed wiki entry gets a baseline 0.1 even if it has zero anchors and zero internal cites. This biases the system to trust well-formed wiki entries more strongly than alloy notes — the structural-integrity work is its own kind of friction, and the floor recognizes it.

A claim with anchors above 0.1 is unaffected by the floor. A claim with anchors below 0.1 (rare but possible after dampening) is raised to 0.1. A claim with no anchors gets exactly 0.1.

If the note loses its structural integrity (a marker becomes unparseable, a `@cite` goes dangling), the floor is removed and unanchored claims drop back to 0.

## Note-Level Aggregation

For v1:

```
N.score = mean(Ci.score for Ci in N.claims)
```

Mean across claims. Weighted aggregation (by claim length, anchor count, or claim age) is tracked under § Open v2 Items.

The note-level score is a derived view shown in the trust report and used for ranking in search results. Internal `@cite` references that point at a whole note (no `#^cn` suffix) read this aggregate as the upstream signal. Internal `@cite` references that point at a specific claim (`[[Note Title#^c2]]`) read the claim-level score directly.

## Structural Integrity Check

A note **passes structural integrity** if all of the following hold. `scripts/trust.py` enforces a minimum subset of these; the full check is the responsibility of `/lint`.

**Required (enforced by `scripts/trust.py`):**

1. The note's file path is under `<paths.wiki>/`.
2. The note has a `## Claims` section.
3. Every claim heading matches `### [Cn] <text>` with `n` sequential starting from 1.
4. Every claim has at least one paragraph of body text.
5. Every fenced `anchors` block parses: every line is either blank, a comment, or matches `@anchor:` / `@pass:` (and optionally `@cite:` for backward compatibility) with valid pipe-separated fields. Bare `@cite:` lines outside fences parse as markers when they appear in the `## Claims` section after a claim heading.
6. Every `@anchor` has a recognized type and a `valid_at`.
7. Every `@cite` resolves: the target note exists in `<paths.wiki>/`. If a `#^cn` suffix is given, the target claim exists in the target note.
8. Every `@pass` has a recognized agent and status.
9. `valid_at` is a valid ISO date <= today.
10. If `invalid_at` is present, it is a valid ISO date > the corresponding `valid_at`.

**Required for floor eligibility (in addition to the above):**

11. At least one claim in the note has a `@pass: reviewer | status: verified` marker.

**Recommended (enforced by `/lint` Phase 1):**

12. The `## Summary` section exists and is non-empty.
13. The `## Revision Log` section exists.
14. No claim is orphaned: every claim is referenced from `## Summary` or has at least one `@anchor` or `@cite`.
15. URLs in `@anchor` markers reach a 200 (cached / periodic check, not real-time).

## Open v2 Items

Documented here so they do not get lost between sessions.

- **Temporal decay.** β = 0.9 per month from Temporal PageRank. Older anchors carry less weight. Requires per-marker age computation.
- **Signed edges (`contradicts`).** A claim that contradicts another claim is not a positive edge. The literature recommends a separate post-processing penalty rather than a signed PageRank, since signed PageRank breaks the stochastic matrix assumption. Defer until contradictions are common enough to matter.
- **Anchor weight from S2 / OpenAlex.** Use `influentialCitationCount` or FWCI as the seed weight for paper anchors. v1 treats all weights as 1.0. The schema field `weight` already exists for forward compatibility.
- **Note-level aggregation alternatives.** Weighted mean by claim length, anchor count, or claim age. v1 is unweighted mean.
- **Claim invalidation.** Currently a marker can be invalidated. A whole claim cannot — there is no `[Cn]` invalidation syntax. If a claim becomes wrong, the v1 workflow is to invalidate all its markers and add a Revision Log entry. v2 may add `### [Cn] ~~Claim text~~` strikethrough as a structural signal.

## Localized Shadow Wikis

Every wiki entry in `<paths.wiki>/` may have one or more **localized shadow copies** in a sibling directory (e.g., a `wiki-cn` directory for Chinese, `wiki-ja` for Japanese — naming is user-private). Shadow paths are configured in `harness/paths.local.toml` under `[paths.wiki_localized]`; the canonical `harness/paths.toml` ships with no shadows defined, so OSS users opt in by language.

Shadows are generated by `/promote` (Phase 4) when a localized target is configured, and can be regenerated on demand. They share the filename of the source English entry, by convention.

Translation rules:
- Translate all prose (Summary, claim text, body paragraphs, Revision Log) into the target language
- DO NOT translate: technical terms, code identifiers, URLs, file paths, `@anchor`/`@pass`/`@cite` markers, block IDs
- Shadow filename matches the English source filename exactly; the shadow keeps the English `# <Title>` H1 (see `/promote` Phase 4)
- Prepend a localized backreference, e.g. for Chinese: `> 本文为 [[English Title]] 的中文版本。核心技术术语保留英文原文。`

Localized shadows are not part of the trust graph: `scripts/trust.py` only scans `<paths.wiki>/`. The shadow copy is for reading convenience. It does not need its own anchors or reviewer passes.

**Sync requirement:** When the English source is updated, the localized shadow must be regenerated. `scripts/lint.py` enforces this with two checks, run for every shadow directory in `[paths.wiki_localized]`:
- `shadow-missing` (WARN): English entry exists but no localized shadow
- `shadow-stale` (WARN): localized shadow is older than the English source

Edit the English source first, then regenerate the localized shadow in the same working session. Do not commit an English edit without its shadow update.

## Cross-References

- Tag taxonomy and the validation-depth principle: `epistemic-hygiene.md`
- Where wiki entries live: `local-first-architecture.md`
- Trust engine implementation: `scripts/trust.py`
- Lint integration: `skills/lint/SKILL.md`
