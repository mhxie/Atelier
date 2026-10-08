# Wiki Schema

The structural format for a note that lives under `<paths.wiki>/`. Location is the certification: a note is a wiki entry by virtue of being in `<paths.wiki>/`, not by carrying any tag. Wiki entries are parseable by `scripts/trust.py` and have claim-level granularity in the trust graph. Notes outside `<paths.wiki>/` are alloy by default (see `epistemic-hygiene.md`).

Design rationale for location-based certification and claim-level trust lives in the architecture review ledger under `$OV/research/`, not here; this file carries only the operative schema. The `#solo-flight` tag lives orthogonally to the schema and marks unstructured pure-human capture, which is location-independent (see `epistemic-hygiene.md`).

## Session-Visible Markers

Session output identifies wiki entries by path (`<paths.wiki>/<title>.md`); other notes use `[[Note Title]]`. Inside notes, use title wikilinks so Reflect preserves backlinks and renames. Reflect's L1–L4 labels come from the path registry projection described in `protocols/local-first-architecture.md`; L4 identifies the layer, while review status belongs to individual claims. Never combine path prefixes with wikilink titles (`[[<paths.wiki>/foo]]`).

Wiki entries read as articles. Each explicitly bounded claim has its own evidence and trust score; headings, paragraphs, and display numbers do not define identity. Note-level aggregation is a derived view.

## Note Structure

A wiki entry opens with its readable H1 title. Localized notes use `lang` metadata and may keep a distinct canonical frontmatter `title` for link identity; the H1 omits the language suffix. Otherwise frontmatter `title` and H1 agree. Introduce the subject, then group explanations and qualifications under natural H2/H3 sections. Omit Summary/Claims scaffolding, repeated assertion headings, and C-number reading instructions. Put evidence under `## References` (`## Evidence` also works) and history under `## Revision Log`.

````markdown
# Example concept

A concise introduction.

## How it works

The explanation connects <!-- claim:c2 -->one assertion and its qualifications. [ref][study]<!-- /claim:c2 --> with <!-- claim:c1 -->another assertion. [[Sample Wiki Entry#^c1|ref]]<!-- {"metadata":{"citation":{"valid_at":"2026-04-06"}}} --><!-- /claim:c1 -->

## References

```anchors c2
@anchor: url:https://example.org/study | valid_at: 2026-04-06
```

```anchors c1
```

[study]: https://example.org/study "Study, p. 12"

## Revision Log

- 2026-04-06: Initial draft; review pending.
````

`<!-- claim:cN -->` and `<!-- /claim:cN -->` delimit one continuous range. IDs are canonical `c` plus a positive integer, unique per note, independent of order, and never reused or renumbered. Multiple claims can share a paragraph; a range can span paragraphs. Require one opening and one later closing marker, nonempty prose, and no nesting, crossing, duplicate pairs, or disjoint repetitions. Unannotated text never inherits nearby evidence. Keep essential qualifications and supporting citation occurrences within the range.

Endpoints must be prose positions, outside code, headings, links, entities, escapes, and citation-plus-metadata units. Markers must preserve ordinary Markdown text and formatting. At an existing paragraph boundary, put a line-leading marker on its own line followed by a blank line; mid-paragraph markers remain inline. A marker cannot open a list item or quote line, and a range touching a table stays within one cell. Never introduce a paragraph break to express ownership. Incomplete or malformed ranges fail visibly; the parser never infers a closing boundary. Ranges cannot intersect evidence fences or References, Evidence, and Revision Log sections.

One `anchors cN` fence under the references section owns that range's structured records. Duplicate fences and owners without a range fail validation. An unanchored claim may omit its fence or use an empty one. Preserve every evidence field, duplicate record, date, and invalidation during conversion. The parser reports article `range_utf8` as half-open UTF-8 byte offsets into the unchanged source, excluding the marker comments; internal Python offsets count code points.

Legacy `## Claims` with `### [Cn] <claim>` headings and local `anchors` fences remains readable. Its IDs also remain stable across reordering and may have gaps. A note may contain both representations with distinct, non-overlapping ownership; never define one ID twice. Legacy `@cite` records remain readable, while new writing keeps citation links beside their supported prose.

Revision Log entries are latest first and outside claim scope. The trust graph ignores log prose. Topic tags do not affect certification or scores.

## Index Hierarchy

Inside the wiki, folders carry navigation; elsewhere links, not folders, organize notes. A folder's `index.md` (exact lowercase name) is its index; every other note is an article, whatever its claims. A note's home is the nearest existing `index.md` in its folder or an enclosing one, within its own language folder; folders without one are skipped and the root `index.md` has none. Reflect derives the breadcrumb and Indexes tree from these paths, so write no parent metadata and no standalone "All topics" or "back to index" links. Reading routes in an index may link any note; body links never create parentage. Localized breadcrumbs skip a missing localized index rather than fall back to the source language, so localize each index with its folder.

## Citing a Claim

A claim is addressed by its stable `cN` ID. Ordinary navigation uses `[[Note Title#^cN]]`; evidence uses the citation-reference form below. The suffix resolves the exact range or legacy claim heading. Session output keeps the path form from "Session-Visible Markers": `<paths.wiki>/Note Title.md [C1]`.

Do not add bare `^cN` block identifiers. Reflect generates all visible reference numbers; authors maintain source keys and stable identities, never display numbers.

## The Marker Vocabulary

`@anchor` and `@pass` markers live in `anchors cN` fences, one marker per line, with pipe-separated fields. Legacy claims use their local `anchors` fences. Unknown records fail validation and remain visible; never drop them during editing or rendering.

Internal citations are inline wikilinks with attached metadata, outside fences so backlinks and renames work. Legacy `@cite` records remain parseable inside or outside fences; new writing uses citation references.

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

### Citation references

A citation to another wiki entry or claim propagates trust; it never seeds trust. Place it beside the supported prose inside the owning range:

```markdown
Supported statement. [[Note Title#^c3|ref]]<!-- {"metadata":{"citation":{"valid_at":"2026-04-06"}}} -->
```

`ref` is a reserved display alias. Reflect derives numbers by first occurrence, displays adjacent citation groups in ascending order, and preserves each occurrence's locator and dates. Keep each citation run adjacent, with punctuation outside the run. Identity remains the resolved note plus optional claim suffix; whole-note and specific-claim targets are distinct. Ordinary topical links, including numeric aliases, do not create evidence edges.

Keep the JSON comment immediately adjacent on the same line. `metadata.citation` accepts only string `valid_at` and optional `invalid_at`, with the same date rules as anchors. Malformed metadata fails structural integrity; never invent a date. Keep the wikilink outside the comment. Code examples, escaped links, and comments do not create edges. References alone do not satisfy the body-prose requirement.

Targets resolve to existing wiki or ordinary notes; missing, ambiguous, or nonexistent claim targets fail integrity. Ordinary-note citations provide provenance without changing their layer, creating graph nodes, or adding trust. An ordinary-note claim target must expose a valid range or legacy claim. Citations outside claim ranges provide note-level provenance only. A missing claim never widens to the whole note. Existing `@cite: [[Note Title#^c3]] | valid_at: ...` records remain valid. Conversions preserve target, date window, and history.

External references use `[ref][source-key]` with `[source-key]: URL "Author or study, locator"` definitions at the bottom. Preserve any API name, subject or assertion carried by the original link label as ordinary prose beside the compact citation. These links do not seed trust themselves; the owning range's `@anchor` records do. Definitions with the same URL can retain different locators. Numbering is presentation, independent of anchor URL matching or review status; unknown records remain available. Copies carry their definitions; rename conflicting keys and occurrences together, never rebind them silently.

### `@pass`

A record of an agent pass or an editor's review-needed event. **`@pass` markers never accumulate trust.** They serve two purposes:

1. **Audit trail.** They show what scrutiny the claim has survived.
2. **Floor trust eligibility.** A wiki entry that has at least one `@pass: reviewer | status: verified` and passes structural integrity becomes eligible for the claim-level floor trust of 0.1 on its unanchored claims.

```
@pass: <agent> | status: <verified|flagged|inconclusive> | at: <YYYY-MM-DD> [| ref: <session-id-or-note>]
@pass: editor | status: pending | at: <YYYY-MM-DD>
```

`<agent>` is one of: `reviewer`, `challenger`, `thinker`, `scout`, `curator`. The optional `ref` field points at the session reflection or another note where the pass was recorded, for audit.

The editor appends `editor/pending` for substantive text edits. This constrained pair requires an ISO `at` date: editor cannot claim another status, and an agent cannot claim pending. It flags review work without inventing a reviewer pass, renewing evidence, or revoking the existing note-level verified floor. Preserve earlier records.

## Bi-temporal Anchors

`valid_at` records when evidence was added; display it as “Evidence recorded,” not a whole-note review date. Add `invalid_at` to invalidate an anchor or citation; preserve the original target and addition date. Reviewer records use `at`. This retains the evidence available at a past time.

Example evolution:

```
@anchor: arxiv:2501.13956 | valid_at: 2026-04-06
```

Later, after the paper is retracted:

```
@anchor: arxiv:2501.13956 | valid_at: 2026-04-06 | invalid_at: 2026-04-12
```

The `Revision Log` records the change with an ordinary wikilink to its explanation. It is outside the scored claim body. An editorial rewrite preserves claim meaning or explicitly disposes of affected evidence and review records; a pending label alone cannot cancel the note's verified floor. `--as-of` filters evidence windows against current prose, not a historical text snapshot.

`scripts/trust.py` filters markers by `invalid_at` when computing current trust: a marker with `invalid_at <= today` is excluded from the graph. The original record is preserved on disk forever. This is the Graphiti-style append-only-but-mutable contract.

The trust engine treats all valid markers as equal weight regardless of age. Temporal decay (β = 0.9 per month from Temporal PageRank, Rozenshtein & Gionis 2016) is tracked under § Open v2 Items.

## The Trust Propagation Rule

This is the rule that makes the design work. State it bluntly so it never drifts.

> **External anchors are the only seeds of trust. Citations to wiki claims propagate trust; ordinary notes provide provenance. Internal `@pass` markers never accumulate trust; verified reviewer passes enable the floor.**

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

The note-level score is a derived view shown in the trust report and used for ranking in search results. Citation references to a whole note (no `#^cn` suffix) read this aggregate as the upstream signal; references to a specific claim read its claim-level score directly.

## Structural Integrity Check

A note **passes structural integrity** if all of the following hold. `scripts/trust.py` enforces a minimum subset of these; the full check is the responsibility of `/lint`.

**Required (enforced by `scripts/trust.py`):**

1. The note's file path is under `<paths.wiki>/`.
2. The note has an H1 and at least one valid range or legacy claim.
3. IDs are unique positive integers; source ownership is unambiguous, non-overlapping, and independent of order.
4. Every claim has substantive prose; metadata and references alone are insufficient.
5. Every evidence fence and citation reference parses. Fences contain only blank/comment lines or valid markers; article fences have a unique existing owner under References or Evidence. Legacy `@cite` records also parse outside fences, excluding literal code/comment examples. Citation and metadata units cannot cross a claim boundary.
6. Every `@anchor` has a recognized type and a `valid_at`.
7. Every internal citation resolves to a note and, when supplied, its `#^cN` claim; only wiki targets propagate trust.
8. Every `@pass` has a recognized agent and status.
9. `valid_at` is a valid ISO date <= today.
10. If `invalid_at` is present, it is a valid ISO date > the corresponding `valid_at`.

**Required for floor eligibility (in addition to the above):**

11. At least one claim in the note has a `@pass: reviewer | status: verified` marker.

**Authoring checks:**

12. The introduction, narrative order, examples, and qualifications make the article useful without claim IDs.
13. Revision Log records substantive changes; evidence and audit details stay outside the article's prose.
14. Claims have retrievable support, and source locators refer to the correct occurrence. `/lint` owns its deterministic checks; it does not establish scientific validity or poll every URL.

## Open v2 Items

Documented here so they do not get lost between sessions.

- **Temporal decay.** β = 0.9 per month from Temporal PageRank. Older anchors carry less weight. Requires per-marker age computation.
- **Signed edges (`contradicts`).** A claim that contradicts another claim is not a positive edge. The literature recommends a separate post-processing penalty rather than a signed PageRank, since signed PageRank breaks the stochastic matrix assumption. Defer until contradictions are common enough to matter.
- **Anchor weight from S2 / OpenAlex.** Use `influentialCitationCount` or FWCI as the seed weight for paper anchors. v1 treats all weights as 1.0. The schema field `weight` already exists for forward compatibility.
- **Note-level aggregation alternatives.** Weighted mean by claim length, anchor count, or claim age. v1 is unweighted mean.
- **Claim retirement.** Markers can be invalidated; claim retirement has no dedicated syntax. Preserve the ID and evidence history, disposition its support, and record the change in Revision Log. Never recycle an old ID for a different assertion.

## Localized Shadow Wikis

Every wiki entry in `<paths.wiki>/` may have one or more **localized shadow copies** in a sibling directory (e.g., a `wiki-cn` directory for Chinese, `wiki-ja` for Japanese — naming is user-private). Shadow paths are configured in `harness/paths.local.toml` under `[paths.wiki_localized]`; the canonical `harness/paths.toml` ships with no shadows defined, so OSS users opt in by language.

Shadows are generated by `/promote` (Phase 4) when a localized target is configured, and can be regenerated on demand.

Translation rules:
- Translate article prose, headings, and Revision Log entries into the target language; keep the `## References`, `## Evidence` and `## Revision Log` headings in English, since the parser recognizes only those
- DO NOT translate: technical terms, code identifiers, URLs, paths, claim IDs, evidence fields, or citation targets/aliases/JSON
- Keep the source filename for native language switching. Preserve a distinct canonical frontmatter `title` for backlinks, set BCP 47 `lang`, and use a simple H1 without a language suffix. Reflect derives its display title from the H1; `display_title` is only for an explicit override. Display names never become citation keys.
- Omit in-body language-switch links and translation boilerplate. Reflect supplies the language buttons and title superscript from metadata.

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
