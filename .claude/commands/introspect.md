---
description: Build or refresh profile files from local notes and reading patterns.
---
# Introspect

> Also reachable via `/hi <natural language>` (e.g., `/hi rebuild profile`, `/hi who am I`,
> `/hi refresh profile`). See `harness/intents.toml` `[intents.introspect]` for the row's
> example phrases. Both paths execute this same procedure.

Build or refresh your self-model by examining your local notes, session history, and reading patterns. Produces three profile files that the rest of the system consults.

Unlike `/index` (mechanical extraction), `/introspect` discovers patterns: what you're drawn to, how your taste is shifting, where your curiosity is orbiting before you've named it as a goal.

## Prerequisites

Verify the local `$OV/` vault is present and non-empty: `Bash: test -d "$OV"/daily-notes && ls "$OV"/daily-notes | head -1`. If the directory is missing or empty, tell the user: "Local `$OV/` vault is missing. Check that `$OV` is set and points to a valid directory."

## Output Files

```
profile/
├── identity.md      — who you are + intellectual taste + curiosity vectors
├── directions.md    — era, goals, quarterly focus, active directions
└── expertise.md     — domain knowledge, research taste, known biases
```

`profile/reader_persona.md` is a raw input signal, not a profile output. Built separately by `/build-persona` from Readwise data.

## Pipeline

### Step 1: Gather Raw Material

Run these searches in parallel over the local `$OV/` vault. Local grep is instant, deterministic, and returns full paths you can then `Read` directly.

**Goals & Directions:**
1. `Grep(pattern: "目标", path: "$OV/")` — Chinese goal mentions
2. `Grep(pattern: "goal", path: "$OV/", -i: true)` — English goal mentions
3. `Grep(pattern: "小目标", path: "$OV/")` — Annual goal notes
4. `Grep(pattern: "objective", path: "$OV/", -i: true)` — Additional goal mentions

**Identity & Themes:**
5. Discover tags: `Bash: grep -rohE '#[A-Za-z][A-Za-z0-9_-]*' "$OV"/ | sort -u` (local tag inventory)
6. `Grep(pattern: "career", path: "$OV/", -i: true)` — Career trajectory
7. `Grep(pattern: "职业", path: "$OV/")` — Chinese career notes
8. `Grep(pattern: "learning", path: "$OV/", -i: true)` — Learning interests
9. `Grep(pattern: "学习", path: "$OV/")` — Chinese learning notes

**Recent Context:**
10. `Read <paths.daily_notes>/YYYY/MM/<today>.md` — today
11. `Read <paths.daily_notes>/YYYY/MM/<yesterday>.md` — yesterday
12. Recent planning: `Bash: find "$OV"/daily-notes "$OV"/reflections "$OV"/gtd -type f -name "*.md" -mtime -30 | xargs grep -l -i "plan" 2>/dev/null`

Deduplicate results by file path. Prioritize files with recent mtimes. **Semantic pass:** for conceptual angles grep cannot phrase ("curiosity vectors", "intellectual taste", "what am I drawn to"), run `Bash: uv run scripts/semantic.py query "<concept>" --top 10`. Reframe and retry if thin.

### Step 2: Read Full Content

For the top ~30-40 most relevant notes, `Read` the file paths returned by Step 1 directly. Focus on:
- All goal notes (critical for directions.md)
- Recent daily notes (critical for identity.md — taste and curiosity signals)
- Top career/learning notes

### Step 3: Discover Taste & Curiosity (NEW — not in old /index)

This step goes beyond mechanical extraction. Look for:

**Intellectual taste** (what you engage deeply with vs. skim):
- Topics that recur across daily notes without being declared goals
- Articles/papers you chose to deep-read vs. archive (check triage files in `<paths.cache>/triage-*.md`)
- Discussion tangents that repeatedly surface in reflection sessions
- Aesthetic preferences in how you evaluate ideas (from research-profile patterns)

**Curiosity vectors** (areas orbiting but not yet goals):
- Topics mentioned 3+ times in recent notes that aren't in current goals
- Reading saves that cluster around an unnamed theme
- Questions you keep asking but haven't formalized

**If `profile/reader_persona.md` exists**, read it as a secondary signal — it shows what you save (behavioral), not what you think about (intentional). Note divergences between Readwise patterns and local-note patterns.

### Step 4: Synthesize Profile Files

Using all gathered content, write three files:

**`profile/identity.md`** — who you are + taste + curiosity:
```markdown
# Identity
Last built: YYYY-MM-DD

## Who You Are
[2-3 sentence summary — role, interests, life stage]

## Active Life Areas
[Bullet points for each major area]

## Major Themes
[Recurring topics — what does this person think about most?]

## Intellectual Taste
[What you find interesting, elegant, and worth your time — independent of goals.
Built from session patterns and reading engagement.]

## Curiosity Vectors
[Areas you're starting to orbit but haven't formalized as goals yet.
Detected from session patterns, reading saves, and discussion tangents.]

## Key People
[Important people referenced frequently]

## Recent Focus (last 30 days)
[What you've been writing about recently]
```

**`profile/directions.md`** — era, goals, focus:
```markdown
# Directions
Last built: YYYY-MM-DD

## Era
[Current era, theme, primary/secondary directions, quarterly focus]

## Near-term (next 1-3 months)
[Goals with specific metrics — Source: [[Note Title]]]

## Mid-term (3-12 months)
[Goals from annual planning notes]

## Long-term (1-3 years)
[Career/life direction from strategic planning]

## Stale Goals (>1 year old, may need review)
[Goals from notes not edited recently]
```

**`profile/expertise.md`** — domain knowledge, research taste:
- Only update if new evidence warrants it (new reviews, new domain exposure)
- Follow the update guidelines already in the file
- Don't downgrade expertise without multiple data points

### Step 5: Quality Gate — Profile Validation

Before writing profile files, dispatch **Reviewer** + **Challenger** in parallel:

- **Reviewer** spot-checks 3-5 claims in the draft profile against source notes by `Read`-ing the local file paths:
  - Are cited [[Note Titles]] real and accurately represented?
  - Do "Intellectual Taste" claims have evidence in the notes, or are they fabricated?
  - Are goal statuses (progressing/stale) consistent with note edit dates?
  - Score using Session Review rubric (Citation Accuracy + Honesty dimensions only)
- **Challenger** probes the profile for blind spots:
  - Are curiosity vectors genuinely emerging, or just noise from 1-2 notes?
  - Does the profile overweight recent notes and miss long-term patterns?
  - Are there important life areas absent from the profile entirely?

If Reviewer scores < 7 on either dimension, fix the specific issues before writing. Present the validated profile to the user before saving — the user confirms or corrects.

### Step 6: Check for Divergences

Compare the three profile layers and flag tensions:
- Identity taste says X, but directions don't include it → potential emerging goal?
- Reader persona (Readwise) shows heavy saves in area Y, but no local notes → consuming but not processing?
- Expertise is deep in area Z, but no current goals reference it → dormant strength?

Present divergences to the user — these are the interesting findings.

### Session Log

After writing profile files, emit a session log:
1. `Bash: uv run scripts/session_log.py --type introspect --duration <minutes>`
2. `Edit` the created file to populate sections from session data (agents dispatched, searches, questions, frameworks, anomalies). The canonical fill-in guide lives in `protocols/session-log.md` § "Section Guidance". Leave empty sections with headers only. If the write fails, warn and continue.

### Step 7: Report

Tell the user what you found:
- How many notes were queried
- Key themes identified
- **Taste and curiosity discoveries** (the new part)
- Any divergences between layers
- Suggest running `/hi` to start using the profile
