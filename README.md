# Atelier

> **A personal workshop, published.** A reflective-thinking system built for [Codex CLI](https://github.com/openai/codex), [Claude Code](https://docs.anthropic.com/en/docs/claude-code), and a local-first Zettelkasten: daily reflection, decision-making, deep reading, goal tracking, knowledge crystallization. Not a product. The patterns are reusable; the configuration is bespoke. Read the code, fork what's useful, build your own.

The system surrounds an **œuvre**: the accumulating body of notes, decisions, and reflections kept as local Markdown under `$OV/`, outside this public repository. A 15-specialist agent team (le cercle) coordinates session work, a deterministic trust engine (`scripts/trust.py`) scores the wiki layer, and both runtimes share one workflow specification through a runtime-adapter contract. Codex is the shipped default; Claude Code is a supported local choice.

## Who is this for?

1. **Pattern students** (the primary audience). You want to see how native Codex and Claude Code runtimes wire to a personal-knowledge-management substrate end to end: agent contracts in `harness/agents.toml`, command portability in `harness/commands.toml`, trust scoring in `scripts/trust.py`, the five-tier model in `protocols/local-first-architecture.md`, the wiki schema in `protocols/wiki-schema.md`. Take the patterns; leave the configuration.
2. **System forkers.** MIT-licensed, fork away, but expect to rip and replace rather than clone-and-run: a fresh clone has no `$OV/` vault, no `profile/identity.md`, and the vocabulary (le cercle, the Painter, the œuvre) is bespoke. The fastest path to disappointment with a system like this is inheriting someone else's taxonomy wholesale; the value lives in writing your own.
3. **Maintainer.** Daily use, self-improving weekly via `$system-review` / `/system-review` plus `scripts/review.sh`.

## What It Does

**Reflect**: daily check-ins grounded in what you actually wrote; surfaces forgotten connections, challenges assumptions, tracks goals across life chapters.
**Read**: deep-reads articles, notes, and transcripts through four lenses (critical, structural, practical, dialectical), multiple readers in parallel, then discussion.
**Plan**: goal reviews, decision journals, energy audits, backed by 22+ thinking frameworks with cross-validation.
**Act**: compact redundant notes, deep-dive a topic with parallel agents, triage notes, curate the Readwise inbox.
**Learn**: reading recommendations; `/introspect` rebuilds the self-model.
**Crystallize**: promote validated thinking into `$OV/wiki/` entries with structured claims and external anchors; `scripts/trust.py` scores them, `$lint` / `/lint` keeps corpus and harness healthy.

Session output goes to `$OV/reflections/`. Daily notes are user-authored: the system reads them, and the sole write path is the Scribe agent recording user-dictated content verbatim.

## Forking the patterns

If you read one thing in this repo, read these in order:

1. `protocols/local-first-architecture.md`: the five-tier (L1–L5) model. The load-bearing idea: directory = certification level, no tags required.
2. `protocols/wiki-schema.md`: claim markers (`[C1]`, `@anchor`, `@cite`, `@pass`), bi-temporal `valid_at`/`invalid_at`, and how `scripts/trust.py` reads them.
3. `harness/agents.toml`, `harness/commands.toml`, `harness/models.toml`, `harness/capabilities.toml`: provider-neutral registries. The runtimes are adapters, not first-class consumers. The part most worth lifting.
4. `scripts/trust.py`: Personalized PageRank with external anchors as trust seeds. Stdlib-only, deterministic.
5. `scripts/semantic.py`: pluggable embedder + store backends (BGE-M3 + LanceDB by default); the CLI contract is encoder-agnostic.
6. `scripts/lint.py` and `scripts/privacy_check.py`: quality gates with structured JSON output, failing loud on placebo-pass conditions.
7. `.claude/agents/*.md` and `.codex/agents/*.toml`: fifteen shared role specs and their Codex adapters, rendered from the registries by `scripts/render_runtime_edges.py` (`--check` keeps them byte-identical). Edit the registries, never the generated files.

Deliberately not portable: `profile/`, `$OV/personal/` and `$OV/wiki/` content, the impressionist vocabulary register, the bilingual English/Chinese behavior, the Era/Direction taxonomy, and the `civ`, `dine`, `prm` workflows that encode a bespoke life-area model. Strip those before adapting.

## Running it

This is the maintainer's daily-use configuration. Running it identically is supported but starts at a cliff: with no vault and no profile, most session commands will route you to `$introspect` / `/introspect` first. That is working as intended for the maintainer and a wall for everyone else.

### Prerequisites

- [Codex CLI](https://github.com/openai/codex) (default) or [Claude Code](https://docs.anthropic.com/en/docs/claude-code); Codex also serves as the external reviewer leg of `scripts/review.sh` alongside a direct-API leg, with [Gemini CLI](https://github.com/google-gemini/gemini-cli) as an optional fallback
- [uv](https://docs.astral.sh/uv/) (Python 3.11+)
- A `$OV/` directory with at least `daily-notes/`, `wiki/`, `reflections/`

### Install

```bash
git clone https://github.com/mhxie/atelier.git ~/atelier
cd ~/atelier
uv sync
echo 'export OV="$HOME/path/to/your/vault"' >> ~/.zshrc
source ~/.zshrc
```

All personal content under `$OV/` is gitignored; only system configuration (protocols, agents, commands, scripts) is committed.

### Permissions and plugins

The canonical write path is local: the runtime writes files under `$OV/`, and a filesystem sync client (such as Google Drive) handles persistence. When `$OV/` is outside the workspace, add it as a writable root while keeping the sandbox at `workspace-write`:

```bash
codex -C . --add-dir "$OV" --sandbox workspace-write --ask-for-approval on-request '$hi'
```

Write access being technically possible does not bypass domain rules: ordinary `$OV/` writes still require approval, and daily notes remain user-authored except for verbatim Scribe capture. Beyond repo + `$OV` read/write and local shell (`uv`, `rg`, `git`, `jq`), everything is optional: live web search for the research agents (`--search`), outbound shell network for the Readwise CLI.

No plugin is required. Plugins only add access to cloud data not already on disk:

| Integration | Authorization | Supported use |
|---|---|---|
| Gmail plugin | plugin enabled + Google OAuth | mail search/read for user-requested context |
| Google Drive plugin | plugin enabled + Google OAuth | cloud-only Drive files; Drive-writing routines need the connector on their hosting runtime |
| Readwise CLI | `readwise login` or token | Reader search, saved documents, inbox curation, anchor snapshots |
| GitHub plugin | connector auth | remote issues/PRs; local `git` works without it |
| Google Calendar plugin | Google OAuth | fork-added calendar workflows; no core command depends on it |

Plugin readiness has four gates (installed, enabled, OAuth completed, tools loaded in a fresh session), and each runtime manages its own connections: authorizing a service on Claude.ai does not configure the Codex plugin, or vice versa. Unattended local routines use a stricter Codex-only envelope (sanitized environment, non-interactive approvals, per-profile write scopes and command fingerprints). References: [Codex plugins](https://learn.chatgpt.com/docs/plugins.md), [sandbox and approvals](https://learn.chatgpt.com/docs/agent-approvals-security.md), [MCP configuration](https://learn.chatgpt.com/docs/extend/mcp).

### First run

The optional selector launches whichever CLI is configured (`ATELIER_RUNTIME=codex|claude` overrides one process; unattended launchd routines remain Codex-only):

```bash
python3 scripts/atelier_runtime.py status
python3 scripts/atelier_runtime.py use claude   # make Claude the interactive default
python3 scripts/atelier_runtime.py run hi
```

Direct native invocation always works:

```bash
codex -C . --add-dir "$OV" '$hi'               # fresh Codex TUI with vault write access
codex --add-dir "$OV" exec -C . '$lint'        # one-shot, no TUI
claude                                          # Claude Code in the project, then /introspect, /hi
```

Codex reads `AGENTS.md`, discovers skills under `.agents/skills/`, dispatches roles through `.codex/agents/`, and runs hooks from `.codex/hooks.json`; each `$command` skill reads the matching `.claude/commands/*.md` specification directly (`protocols/runtime-adapters.md` defines the boundary). Reflection workflows default to fresh sessions, because reusing a prior session pollutes the new reflection; continuation-friendly commands are marked `resume_friendly = true` in `harness/commands.toml`.

## Sessions

Command names are stable across runtimes: `$name` in Codex is `/name` in Claude Code. `$hi` opens the menu; the main flows:

| Mode | What happens |
|------|-------------|
| Daily Reflection | Reflects on today's notes, asks questions at increasing depth, surfaces a forgotten connection |
| Weekly Review | Energy + attention audit across the week |
| Explore | Finds hidden connections and open threads across your notes |
| Goal Review | Checks progress on goals: progressing, neglected, or shifted |
| Decision Journal | Structured decision-making with framework cross-validation |
| Energy Audit | Four-dimension assessment (physical, mental, emotional, social) |
| Read & Discuss | Multi-lens reading of an article or note, then interactive discussion |
| Deep Dive | Full briefing on a topic: your notes + web research + resources + framework |
| Compact Notes | Find and merge redundant notes |
| Curate Inbox | Goal-aware triage of your Readwise inbox |
| Note Triage | Scan for compaction candidates |
| Process Meeting | Turn a work meeting transcript into structured notes with action items |

Direct commands: `$review`, `$weekly`, `$decision`, `$explore`, `$energy-audit`, `$curate`, `$introspect`, `$lint`, `$promote`, `$dine`, `$prm`, `$civ`, `$system-review`.

Knowledge layer: `$promote` creates an L4 wiki entry from L2 sources (Researcher finds claims + anchors, Curator drafts the schema-compliant entry, the orchestrator writes after approval); `$lint` runs the corpus structural check over `$OV/wiki/` plus harness health (root-file budgets, privacy gate, ingestion hygiene).

## The Team

Fifteen specialist agents (le cercle). The orchestrator dispatches automatically; you can also address them directly:

- *"find notes about X"* sends Researcher (the Observer)
- *"read [[Article]] with critical lens"* sends Reader
- *"challenge my assumption about X"* sends Challenger (the Critic)
- *"compact my notes on Y"* sends Curator (the Collector)
- *"recommend reading on Z"* sends Librarian (the Cataloguer)
- *"what's happening in the world on X"* sends Scout (the Flâneur)

The full archetype map lives in `protocols/atelier.md`.

## How It Works

```
Capture sources                  Local data layer ($OV/)
(Readwise inbox,                 L4  $OV/wiki/        ─ locally certified
 voice notes,                        (trust-scored canon)
 markdown editor)                L3  $OV/papers/ + $OV/preprints/ ─ peer-reviewed
                                 L2  $OV/daily-notes/ + reflections/ +
                                     research/ + agent-findings/ +
                                     wip/ + …
                                 L1  $OV/cache/ + Readwise (cloud, via CLI)

                                         ^
                                         |
                                         v
                            AI runtime (Claude Code or Codex)
                                         |
                     +-----------+-------+-------+-----------+
                     v           v               v           v
                Le Cercle    Sessions     Frameworks    Trust engine
                (15 agents)  (/hi menu)   (22 + xval)   (trust.py,
                     |           |               |        lint.py)
                     v           v               v
                Protocols    $OV/reflections/   Cross-validation
                (protocols/) (session outputs)  & Pattern Library
```

- **Five-tier knowledge model.** Everything under `$OV/` is classified by depth of crystallization, raw capture (L1) through locally-certified wiki (L4), with L5 reserved. Directory = tier. Agents read from disk via semantic search and grep.
- **TrustRank over the wiki.** Trust mass enters the graph only at external anchors and propagates through internal cites: no external anchor, no trust. Deterministic, stdlib-only; the same input always produces the same score.
- **Era-aware and bilingual.** Tracks life chapters with user-configured themes; handles English and Chinese notes and matches your language.
- **Self-improving with a subtraction bias.** The Evolver follows `protocols/evolution.md` (root principles with mechanical backstops: prose budgets, hot-path byte ceilings, generated runtime edges), reviewed weekly by external models via `scripts/review.sh`. Mechanical work stays in deterministic scripts with single writers.
- **Public-repo privacy gate.** `scripts/privacy_check.py` scans public-bound worktree and staged content against private titles and local exact terms; the Steward (privacy-reviewer) catches semantic leaks across two providers. This protects new commits, not copies already in Git history or forks.

## Vocabulary

The narrative register comes from the impressionist atelier: *le cercle* (the agents), *the Painter* (you), *the œuvre* (the body of work). The register lives in conversation and identity only; workflow names, dispatch keys, and `$OV/` paths stay literal and stable. Full glossary: `protocols/atelier.md`.

## License

MIT, for the code. The taste, the vocabulary, and the daily-use configuration are not licensed and not portable. Fork the patterns; build your own atelier.
