---
name: reading
description: "Use when the user's primary intent is to have an article, paper, transcript, or note read and discussed: a supplied URL or wikilink is the main payload, or the user explicitly asks to read or discuss attached content. Incidental links in capture, inbox triage, save-for-later, or cited questions do not trigger this skill. Forwards into the canonical Atelier `/hi` router."
---

# Reading (Atelier entry hint)

Forward the user's input verbatim into `/hi <user-text>`. The router in
`.claude/commands/hi.md` selects the row and follows its procedure; reading
dispatch is defined there, not here.

Trigger only when the URL, wikilink, or "read this" phrase is the request
itself. URLs appear inside many other intents; these stay with the router:

- Capture with a link (`5/4 看了 https://... 觉得有意思`): the URL is content.
- Curate or inbox triage (`triage my inbox`, `curate readwise`), even with URLs.
- Save for later (`save this <url>`): Scribe records the URL verbatim.
- A question that cites a link: the question is the request.
- A meal record with a restaurant link: dine.
- Wikilinks inside structured notes (`see also [[Foo]]`).

Do not bypass `/hi` or call Reader directly; a reading-shaped phrase that is
really about "this week" or feeling "burnt out" belongs to weekly or
energy-audit, and only the router sees the whole catalog.

The entry-hint contract lives in `protocols/runtime-adapters.md` → Runtime
Surfaces. When the reading concept changes in `harness/intents.toml`, update
this description to match; lint checks structure, not trigger phrasing.
