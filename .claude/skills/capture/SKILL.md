---
name: capture
description: Use when the user dictates raw factual content they want recorded verbatim — reporting events, not asking for analysis. Common shapes are explicit "记一下" / "log this" / "save this" phrases, and date-prefixed factual narratives like "5/4 早上去了 X, 中午吃了 Y" (date plus a report of events without an analytical question). This is an entry hint for the Atelier capture intent; it forwards the user's input verbatim into `/hi` so the canonical intent router (`harness/intents.toml`) prints the standard routing announcement and dispatches the Scribe agent for verbatim recording.
---

# Capture (Atelier entry hint)

Forward the user's input verbatim into `/hi <user-text>`. The router in
`.claude/commands/hi.md` selects `intents.capture` and dispatches Scribe for
verbatim recording; `harness/intents.toml` is the dispatch source of truth.

Do not bypass `/hi` or call Scribe directly. Do not paraphrase, summarize, or
editorialize the content. If the input mixes a record-this clause with a
question, a recommendation request, or retrieval, let `/hi` route it and
surface the ambiguity instead of forcing capture.

The entry-hint contract lives in `protocols/runtime-adapters.md` → Runtime
Surfaces. When the capture concept changes in `harness/intents.toml`, update
this description to match; lint checks structure, not trigger phrasing.
