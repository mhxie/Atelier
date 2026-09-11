## Hi menu

Use this only when `/hi` or `$hi` has no context. Session-start cues are
already injected by hooks; offer a relevant fired cue first, otherwise show
the first menu.

### Choose mode

1. Reflect: daily reflection, weekly review, explore
2. Plan: goal review, decision journal, energy audit, PRM audit
3. Act: curate, compact, deep dive, note triage, process meeting
4. Read: structured reading and discussion
5. Learn: recommendations or introspection
6. Analyze: company financial and market signals

After the user chooses, offer the actions below and load only the selected
procedure.

### Reflect

- Daily Reflection: `skills/daily-reflection/SKILL.md`
- Weekly Review: `skills/weekly/SKILL.md`
- Explore: `skills/daily-reflection/SKILL.md`, intent `explore`, Exploration branch only

### Plan

- Goal Review: `skills/review/SKILL.md`
- Decision Journal: `skills/decision/SKILL.md`
- Energy Audit: `skills/daily-reflection/SKILL.md`, intent `energy-audit`, Energy branch only
- PRM Audit: `skills/prm/SKILL.md`

### Analyze

- Finance Analysis: `protocols/finance-analysis.md`

### Act

- Curate Inbox: `skills/curate/SKILL.md`
- Process Meeting: `protocols/intent-meeting.md`
- Compact Notes: ask for the topic; Researcher finds related notes;
  orchestrator snapshots sources under `<paths.cache>/`; Curator drafts only
  from snapshots; the user approves each output before the orchestrator writes.
- Deep Dive: ask for the topic; dispatch Researcher, Scout, Librarian, and
  Thinker in parallel; Synthesizer combines results. Before any write-back,
  dispatch Reviewer and Challenger in parallel to check citations and claims.
- Note Triage: ask for 3 to 5 areas or use identity themes; Researcher finds
  overlap; present a prioritized plan; Curator drafts only user-approved merges.

### Read

Load `skills/read/SKILL.md` for its reading modes and Reader or Scholar
selection.

### Learn

- Recommend Resources: ask for a topic and dispatch Librarian. Use existing
  notes for context and present reading-intensive results in Chinese.
- Introspect: `skills/introspect/SKILL.md`
