---
name: prm
description: Personal relationship management audit grounded in local relationship notes.
---
# PRM: Personal Relationship Management

Audit the health and robustness of the user's social support system. Grounded in Dunbar's layer model, House's four-dimension support framework, and the cached Dunbar energy-allocation paper under `<paths.cache>/` when present.

## Prerequisites

1. Check if `profile/identity.md` exists. If not, tell the user: "No profile found. Run `/introspect` first to build your self-model." and stop.
2. Read `profile/identity.md`. Check the `Last built:` date. If older than 7 days, warn: "Your profile is stale (built on [date]). Consider running `/introspect` to refresh. Continuing with current profile."
3. Verify `<paths.people>/` exists and contains DL-tagged files. Run `Grep(pattern: "DL[0-5]", path: "<paths.people>/")` to count coverage. If fewer than 10 DL-tagged files, warn: "Low DL coverage (N files tagged). Results will be incomplete. Consider running `/prm enrich` after the audit."
4. Filter out `#author` files (public figures, book authors) from the audit scope. These are reference entries, not personal contacts.

## Data Sources

- **People files:** `<paths.people>/*.md` (each file has Bio, Notes, and Relationship sections with Dunbar layer tags [[DL0]] through [[DL5]])
- **PRM template:** `<paths.archive>/templates/` PRM template file (layer definitions and scoring rules)
- **Daily notes:** `<paths.daily_notes>/` (for interaction frequency detection)
- **Profile:** `profile/identity.md` (Key People section, Active Life Areas: Social & Emotional)
- **Prior reflections:** `<paths.reflections>/` (relationship findings from prior reflection and PRM sessions)

## Reference Frameworks

### Dunbar Layer Model (from PRM template)
| Layer | Tag | Size Target | Description |
|-------|-----|-------------|-------------|
| DL0 | [[DL0]] | ~5 | Support clique: closest emotional support |
| DL1 | [[DL1]] | ~15 | Sympathy group: close friends, regular interaction |
| DL2 | [[DL2]] | ~50 | Affinity circle: good friends, frequent contacts |
| DL3 | [[DL3]] | ~150 | Casual friends: party-invite level |
| DL4 | [[DL4]] | ~500 | Acquaintances: recognizable faces, weak ties |
| DL5 | [[DL5]] | rest | Any listed contact |

### House Support Type Model
| Type | Code | What it provides |
|------|------|-----------------|
| Emotional | E | Being heard, cared for, emotional resonance |
| Instrumental | I | Practical help, resources, material assistance |
| Informational | Inf | Advice, guidance, knowledge |
| Appraisal | A | Feedback, validation, self-assessment help |

### Health Indicators
| Indicator | Healthy | Warning | Critical |
|-----------|---------|---------|----------|
| DL0 size | 3-7 | 1-2 or 8-10 | 0 or >10 |
| DL1 size | 8-20 | 5-7 or 21-30 | <5 or >30 |
| Support type coverage | 4 types from 3+ sources each | 1-2 types concentrated | All types from 1 person |
| Domain diversity | 3+ distinct domains in DL0-1 | 2 domains | 1 domain |
| New connections (past 90 days) | 2+ new DL2+ | 1 new | 0 new |
| Staleness (no mention in 90 days) | <20% of DL0-1 | 20-50% | >50% |
| Energy balance | Mix of give/receive/mutual | Mostly one direction | All giving or all receiving |

## Execution Flow

### Step 1: Census

Scan all people files (excluding `#author`-tagged entries) and build the current map.

Use `Grep` for the structural census:
- `Grep(pattern: "\\[\\[DL0\\]\\]", path: "<paths.people>/", output_mode: "files_with_matches")` for each layer DL0 through DL5
- For each matched file, `Grep(pattern: "#author", path: "<file>")` to exclude public figures

For each person in DL0 and DL1, extract:
- Name (from filename)
- Dunbar layer
- Categorization tags (if present)
- Company/affiliation (domain proxy)

Present as a table. Flag data gaps (missing categorization, missing company).

### Step 2: Support Type Mapping

Dispatch **Researcher** to scan daily notes for DL0-DL1 people mentions. The Researcher uses `Bash: uv run scripts/semantic.py query "<person name> interaction" --after "<90 days ago, YYYY-MM-DD>" --top 10` as primary search, then `Grep` for exact name matches as structural follow-up.

For each DL0-DL1 person, assess which support types they provide. Sources for inference:
1. **Categorization tags** in the people file (#mentor: Informational + Appraisal, #partner: Emotional + Instrumental, #colleague: Informational, etc.)
2. **Daily note mentions** (from Researcher): infer interaction type from context
3. **Profile Key People section**: role descriptions give support type hints
4. **Ask the user** for any ambiguous cases

Build a support type matrix:

| Person | Layer | E | I | Inf | A | Domain |
|--------|-------|---|---|-----|---|--------|
| Name | DL0 | x | x | | | family |

### Step 3: Health Assessment

Score against the Health Indicators table. For each indicator, report:
- Current value
- Status (healthy / warning / critical)
- One-line explanation

Key analyses:
- **Concentration risk:** How many support types point to the same person? Flag anyone carrying 3+ types.
- **Domain diversity:** Are DL0-1 all from the same context (work, school, family)? Cross-reference company/affiliation fields.
- **Layer capacity:** Is any layer over/under Dunbar targets?
- **Staleness:** Cross-reference DL0-1 names against daily notes from the past 90 days. Flag people with zero mentions.
- **Weak tie health:** Is DL2-3 populated enough to provide information bridges and opportunity flow?
- **New connections:** Has anyone been added or promoted in the past 90 days?

### Step 4: Structural Vulnerabilities

Dispatch **Challenger** to probe the health assessment for blind spots: Are the vulnerability scenarios realistic? Are there hidden dependencies the data doesn't surface? What would the user lose if a key person became unavailable?

Identify specific failure scenarios:
- "If [Person X] were unavailable, which support types would you lose entirely?"
- "Your [type] support comes exclusively from [domain]. A single context shift would remove it."
- "DL1 has N people but they're all from [same context]."

### Step 5: Recommendations

Generate 3-5 specific, actionable suggestions ranked by impact:
- **Rebalance:** "Consider promoting [Name] from DL2 to DL1; they provide [type] support that DL0-1 lacks"
- **Diversify:** "Your DL0-1 is heavily [domain]. One cross-domain relationship would reduce concentration risk"
- **Maintain:** "[Name] hasn't appeared in daily notes for 90+ days. A brief check-in prevents decay"
- **Demote:** "DL0 has [N] people (target: 5). Consider whether all truly belong in your support clique"
- **Enrich data:** "N people in DL0-1 have no categorization tags. Filling these enables better analysis next time"

### Step 6: Data Enrichment (Interactive, Optional)

If the user wants to improve the data:
- Walk through DL0-1 people files one by one
- Ask the user to confirm/update: categorization tags, support types, last meaningful interaction
- Edit the people files with updated information
- Offer to add a `Support types:` field to the Relationship section of each file

Proposed enhanced Relationship section format:
```markdown
### Relationship

- Dunbar Layer at [[DLx]]
- Categorization: #tag1, #tag2
- Support types: E / I / Inf / A (which types this person provides)
- Domain: work / family / school / community / online
- Last meaningful interaction: YYYY-MM-DD
- Shared connections: [[Name1]], [[Name2]]
```

## Output

Write a report to `<paths.reflections>/YYYY-MM-DD-prm-audit.md`:

```markdown
# PRM Audit: YYYY-MM-DD

## Layer Distribution
| Layer | Count | Target | Status |
|-------|-------|--------|--------|

## Support Type Matrix (DL0-1)
| Person | Layer | E | I | Inf | A | Domain |
|--------|-------|---|---|-----|---|--------|

## Health Indicators
| Indicator | Value | Status | Note |
|-----------|-------|--------|------|

## Structural Vulnerabilities
[Specific failure scenarios identified]

## Recommendations
[3-5 ranked, actionable suggestions]

## Data Quality
- People files scanned: N
- Dunbar-tagged: N (N%)
- Categorization coverage in DL0-1: N%
- Fields to enrich: [list]
```

## Quality Gate

Before presenting the final report, dispatch **Reviewer** + **Challenger** in parallel:
- **Reviewer** checks: Are health indicator scores grounded in actual data? Are vulnerability scenarios supported by the census? Are recommendation examples cited correctly?
- **Challenger** checks: What structural risks did the analysis miss? Are there hidden dependencies between people? Is the user's self-report of support types accurate, or should it be questioned?

Fix any issues they surface before writing the output file.

## Session Log

After writing the PRM audit file, emit a session log:
1. `Bash: uv run scripts/session_log.py --type prm --duration <minutes>`
2. `Edit` the created file to populate sections from session data (agents dispatched, searches, questions, frameworks, anomalies). The canonical fill-in guide lives in `protocols/session-log.md` § "Section Guidance". Leave empty sections with headers only. If the write fails, warn and continue.

## Wrap Up

The PRM audit file in `<paths.reflections>/` is the durable session output. Daily notes are user-authored; the system reads them but does not modify them. Tell the user the audit has been saved and where to find it.

## Usage Patterns

- `/prm`: Full audit (Steps 1-5)
- `/prm enrich`: Full audit + interactive data enrichment (Steps 1-6)
- `/prm check [Name]`: Single-person deep dive (all mentions in daily notes, support type analysis, relationship trajectory)

## Frequency

Recommended: monthly full audit. Between audits, the Relationships check-in in `skills/daily-reflection/SKILL.md` → Relevant life context supplies longitudinal evidence when it comes up; the monthly `/prm` audit synthesizes structural patterns.
