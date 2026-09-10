# Atelier

**A workshop for your thinking, built on a local-first Zettelkasten.**

Atelier is a personal agent harness for [Codex CLI](https://github.com/openai/codex)
and [Claude Code](https://docs.anthropic.com/en/docs/claude-code), built around a
local-first **Zettelkasten**. It connects daily reflection, deep reading,
decision journals, and goal reviews to your **œuvre**: the notes, sources,
decisions, and reflections you accumulate over time. A session can reconnect
a question to its sources, revisit a decision with its original assumptions,
or crystallize working notes into source-backed wiki claims. The lasting
artifact is your knowledge, kept in plain Markdown and independent of any
one model or chat history.

Underneath is an opinionated knowledge architecture. Provider-neutral registries
let both runtimes share the same workflows; bounded retrieval loads task-relevant
context; **le cercle** adds specialist perspectives. Certification tiers keep
provisional working notes separate from locally certified wiki knowledge.
At the wiki layer, `[C1]` claim identifiers, bi-temporal evidence anchors, and a
deterministic PageRank trust engine make support inspectable at claim level.
External evidence seeds the trust graph; repeated agent agreement does not.
The result is a knowledge base designed to be revisited, challenged, and revised.

[What you can do](#what-you-can-do) · [Get started](#get-started) ·
[Design](#design) · [Forking](#forking)

## What you can do

| Practice | What it helps you do | Start with |
|---|---|---|
| Daily reflection | Find patterns and open questions in your recent notes. | `$hi` |
| Decision journals | Make assumptions, trade-offs, and review triggers explicit. | `$decision` |
| Deep reading | Read a paper or article in the context of your questions. | `$read` |
| Goal tracking | Review progress, commitments, and direction. | `$weekly`, `$review` |
| Knowledge crystallization | Develop working notes into evidence-anchored wiki claims. | `$promote` |

Start with a question in your agent's chat:

```text
$hi I've been busy all week. What actually moved forward?
$read <paper URL or local path>
$decision Help me think this through before I say yes.
$hi Turn this meeting transcript into decisions and action items.
```

`$hi` accepts a free-form request or opens the menu when you're not sure where
to begin. In Claude Code, use `/hi`, `/read`, and so on instead of `$`.
The [workflow menu](protocols/hi-menu.md) covers the rest.

## Get started

You need an authenticated Codex CLI or Claude Code, Git, Python 3.11+, `uv`,
`rg`, `jq`, and Node 22+ with npm. Use a system or Homebrew Node installation
for the local adapters.

**1. Install the harness.**

```bash
git clone https://github.com/mhxie/atelier.git ~/atelier
cd ~/atelier
uv sync --locked
npm ci
```

**2. Connect your vault.**

Point `OV` at an existing Markdown vault outside the repository. Your personal
notes are not part of the clone.

```bash
export OV="/absolute/path/to/your/existing-vault"
```

Follow [local search setup](sources/semantic.md#setup-and-hardware) to initialize
models and index your notes. The [path registry](harness/paths.toml) maps the
expected vault layout; [local overrides](harness/paths.local.toml.example)
adapt it to yours.

**3. Build a profile and open a session.**

Profile setup draws on your authored daily notes. From the repository, run:

```bash
codex -C . --add-dir "$OV" '$introspect'
```

Review the proposed profile before saving it, then continue with `$hi`.
For Claude Code, launch `claude` from the repository and use `/introspect`,
then `/hi`. The generated personal context stays in gitignored `profile/`.

## Design

Shared workflows and agent briefs define the work. Provider-neutral registries
and thin adapters make those same specifications available in either runtime.
Each session selects the context it needs and draws on [**le cercle**](protocols/atelier.md),
the circle of specialist agents. You, **the Painter**, set the direction;
substantive note changes are proposed for your approval.

```text
+----------------------------------+
|     Atelier: shared harness      |
|                                  |
| +------------------------------+ |                  +----------------------------+
| |   Workflows + agent briefs   | | specs / results  |                            |
| +------------------------------+ |----------------->|                            |
|                                  |                  |                            |
| +------------------------------+ |                  |       Agent session        |
| |    Registries + adapters     | |                  |  Codex CLI or Claude Code  |
| +------------------------------+ |                  |                            |
|                                  |   tool calls     |                            |
| +------------------------------+ |<-----------------|                            |
| |   Protocols + local tools    | |                  |                            |
| +------------------------------+ |                  +----------------------------+
+----------------------------------+                       ^                |
                                                           | notes          | writes
                                                           |                |
                                                           |                v
                                                      +----------------------------+
                                                      |        Your oeuvre         |
                                                      |       Markdown vault       |
                                                      |           ($OV)            |
                                                      +----------------------------+
```

Logical boundaries for an interactive session, not separate services. Arrows
carry specifications, tool calls, and note data; writes follow the selected
workflow's authorization rules. The vault lives outside this repository.
Storage and retrieval are local-first; model calls may use remote providers.

The architecture rests on four principles:

- **Files are the source of truth.** Notes remain ordinary Markdown; search
  indexes and summaries are views, not a replacement for the source.
- **Context follows the task.** A session loads selected sources and agent
  instructions, keeping the working context bounded.
- **Directory = certification tier (L1–L5).** L1 is raw capture, L2 working
  notes, L3 external receipts, and L4 locally certified wiki knowledge. L5 is
  reserved for foundations. Tiers describe validation depth, not authorship;
  most thinking stays provisional.
- **Claim-level trust.** The wiki uses `[C1]` claim markers, bi-temporal anchors,
  and evidence-seeded PageRank in a deterministic [trust engine](scripts/trust.py).
  The [claim schema](protocols/wiki-schema.md) makes support and validity periods
  explicit; trust scores are not a substitute for checking the evidence.

[Knowledge architecture](protocols/local-first-architecture.md) ·
[Registries](harness/README.md) · [Runtime setup](protocols/runtime-adapters.md) ·
[Local search](sources/semantic.md) · [Workflow contracts](protocols/README.md)

## Forking

MIT, for the code. Expect some rip-and-replace, not a turnkey second brain.
`profile/`, vault content, bilingual English/Chinese defaults, and the
`civ` / `dine` / `prm` life-area workflows are bespoke. The impressionist
vocabulary (*le cercle*, *the Painter*, *the œuvre*) is optional.

The value is in building your own taxonomy, not inheriting someone else's.
Take the patterns, replace what doesn't fit, and build your own atelier.
Beret optional.
