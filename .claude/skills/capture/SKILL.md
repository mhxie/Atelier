---
name: capture
description: Use when the user dictates raw factual content they want recorded verbatim — reporting events, not asking for analysis. Common shapes are explicit "记一下" / "log this" / "save this" phrases, and date-prefixed factual narratives like "5/4 早上去了 X, 中午吃了 Y" (date plus a report of events without an analytical question). This is an entry hint for the Atelier capture intent; it forwards the user's input verbatim into `/hi` so the canonical intent router (`harness/intents.toml`) prints the standard routing announcement and dispatches the Scribe agent for verbatim recording.
---

# Capture (Atelier entry hint)

Non-authoritative entry hint for the Atelier capture intent. The dispatch source of truth is `harness/intents.toml` → `intents.capture`; this skill exists only to remove the friction of typing `/hi` first when the user's input is unambiguously dictation-shaped.

## What the skill does

Forward the user's input verbatim into `/hi <user-text>`. The intent router in `.claude/commands/hi.md` prints `Routing as intents.capture → scribe`, then dispatches the Scribe agent to record the content verbatim under `$OV/`.

Do not bypass `/hi`. Do not call Scribe directly. Do not paraphrase, summarize, or editorialize the user's content; verbatim preservation is Scribe's trust property.

If the user's input is *not* a clean capture shape (mixes a record-this clause with an analytical question, asks for a recommendation, requests retrieval), prefer to let `/hi` handle it through the full intent router — surface the ambiguity to the user rather than forcing capture.

## Why entry hint, not authoritative dispatch

Skill auto-trigger is semantic — Claude Code matches this skill's frontmatter description against the user's phrasing. Routing inside `/hi` is also model judgment, but against the whole catalog at once: every `harness/intents.toml` row's `description` competes, so a request that mixes capture with a question lands where its primary intent lies. The skill widens the natural-language entry surface; the router stays the single decision point for what runs.

This division is documented in `protocols/runtime-adapters.md` § Runtime
Surfaces. Codex does not read `.claude/skills/`; Codex reaches the same flow
through `$hi <user-text>`.

## Maintenance

The row's `description` in `harness/intents.toml` defines what counts as capture. If you change the *concept* of capture there (e.g., narrow it to dining only, broaden it to include inbox-clip pasting), update this skill's frontmatter description so the two prose surfaces stay aligned. `scripts/harness_lint.py` checks structural invariants (description present, `/hi` mentioned, intent row exists) but does not — and should not — substring-check trigger phrases; both surfaces are LLM-judged prose.
