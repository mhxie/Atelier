---
name: reading
description: Use when the user's primary intent is to have an article, paper, transcript, or note read and discussed — they paste a URL or wikilink as the main payload, or explicitly say "read this", "read with me", "let's read", or "discuss this" with content attached. Do NOT trigger when a URL or wikilink appears incidentally inside a longer message whose intent is something else (capturing a day's events that happens to contain a link, asking to triage / curate the inbox, saving a link for later, asking a question about a topic that includes a citation, dictation under the capture intent). The signal is that the URL or "read this" phrasing IS the request, not a fragment of a different request. This is an entry hint for the Atelier reading intent; it forwards into `/hi` so the canonical intent router (`harness/intents.toml`) prints the standard routing announcement and dispatches Reader plus Researcher in parallel for the read-and-discuss flow.
---

# Reading (Atelier entry hint)

Non-authoritative entry hint for the Atelier reading intent. The dispatch source of truth is `harness/intents.toml` → `intents.reading`; this skill exists only to remove the friction of typing `/hi` first when the user's input is unambiguously a "read this with me" request.

## When NOT to trigger (the discriminating cases)

URL collision is the main risk for this skill. URLs and wikilinks appear in many other intents; semantic triggering must distinguish primary-intent reading from incidental URL presence:

- **Capture with a link** (`5/4 看了 https://... 觉得有意思`): user is recording a day's events; the URL is content, not the request. Routes to capture.
- **Curate / inbox triage** (`triage my inbox`, `curate readwise` — even when followed by URLs): routes to curate.
- **Save for later** (`save this <url>`, `just save this`): routes to capture (Scribe records the URL verbatim).
- **Question with citation** (`what does X think about <url>?`): the question is the request; the URL is supporting context. Routes to the default reflection / decision intent.
- **Meal-history tracker with restaurant link**: routes to dine.
- **Wikilinks inside structured notes** (`see also [[Foo]]`): not a read request unless the user explicitly says so.

Trigger only when the URL, wikilink, or "read this" phrase IS the request payload — the user wants the system to read the linked content and discuss it, full stop.

## What the skill does

Forward the user's input verbatim into `/hi <user-text>`. The intent router in `.claude/commands/hi.md` prints `Routing as intents.reading → reader, researcher`, then dispatches Reader (lens-based read of the content) plus Researcher (related notes from `$OV/`) in parallel.

Do not bypass `/hi`. Do not call Reader directly. The router weighs every catalog row's description against the whole message, so a reading-shaped phrase that is really about "this week" or feeling "burnt out" still lands on weekly or energy-audit; bypassing it would lose that judgment.

## Why entry hint, not authoritative dispatch

Skill auto-trigger is semantic — Claude Code matches this skill's frontmatter description against the user's phrasing. Routing inside `/hi` is also model judgment, but against the whole catalog at once: every `harness/intents.toml` row's `description` competes. The skill widens the natural-language entry surface; the router stays the single decision point for what runs.

This division is documented in `protocols/runtime-adapters.md` § Runtime
Surfaces. Codex does not read `.claude/skills/`; Codex reaches the same flow
through `$hi <user-text>`.

## Maintenance

If the *concept* of the reading intent changes in `harness/intents.toml` (e.g., separating long-form papers from short articles, adding a new lens), update this skill's frontmatter description so semantic triggering stays aligned. `scripts/harness_lint.py` checks structural invariants (description present, `/hi` mentioned, `intents.reading` row exists) but does not substring-check trigger phrases.
