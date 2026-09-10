# Atelier — Vocabulary Register

The system's narrative register draws from the impressionist atelier (master + specialist circle) and from publishing/library tradition. **This is narrative only.** Operational keys — slash command names, agent dispatch keys (`subagent_type: "researcher"`), file paths under `$OV/`, JSON keys emitted by scripts, directory names, frontmatter `name:` fields — remain at their current values. The atelier vocabulary lives in conversation, identity, and prose.

## Glossary

| concept | atelier term | operational key (unchanged) |
|---|---|---|
| system / repo | **the Atelier** | `atelier/` |
| user | **the Painter** | (you) |
| vault root | **the œuvre** | `$OV/` |
| agents collectively | **le cercle** | `.claude/agents/` |
| daily reflection | **impression** | `<paths.daily_notes>/YYYY/MM/YYYY-MM-DD.md` |
| weekly review | **étude** | `<paths.reflections>/*-weekly.md` |
| wiki entry (L4) | **tableau** | `<paths.wiki>/<Title>.md` |
| L2 working note | **sketch** | `<paths.wip>/`, `<paths.research>/`, etc. |
| decision journal | **commission** | `<paths.gtd>/decisions/` |
| long-running theme | **série** | (e.g., Monet's haystacks across years) |
| archive | **carnets** | `<paths.archive>/` |
| Readwise inbox | **rough drawer** | cloud-only; `readwise` CLI |
| session | **sitting** | (a `/hi` invocation) |
| session log | **bench notes** | `<paths.sessions>/*.md` |
| promote (L2 → L4) | **exhibit** / **mount** on the cimaise | `/promote` |

## Le cercle — 15 specialist operators

Each agent maps to an impressionist-period archetype. **Dispatch keys are unchanged** (`researcher`, `synthesizer`, etc.); archetype names are for narration only.

| dispatch key | archetype | original |
|---|---|---|
| `researcher` | The Observer | Pissarro — patient eye for motifs |
| `synthesizer` | The Colorist | Monet — composes light into form |
| `reviewer` | The Arbiter | Manet — judges what hangs |
| `challenger` | The Critic | Degas — sharpest tongue |
| `thinker` | The Structuralist | Cézanne — sees underlying geometry |
| `curator` | The Collector | Caillebotte — patron-organizer |
| `scout` | The Flâneur | wanders the boulevard, returns with observations |
| `reader` | The Reader | Borges: "I am proudest of having been a reader" |
| `scholar` | The Scholar | the deep close-reader for foundational texts |
| `librarian` | The Cataloguer | maintains the source register |
| `meeting` | The Stenographer | takes the bench notes verbatim |
| `privacy-reviewer` | The Steward | keeps confidences from the public exhibition |
| `forgetter` | The Conservator | preserves the œuvre by removing decay, not by hoarding |
| `scribe` | The Typewriter | records what is dictated, no editorial hand |

## Mode

The Painter works *en plein œuvre* — within the accumulating body of work. New impressions enter the rough drawer or land directly as field notes; sittings convene le cercle to walk a série, mount an étude, or exhibit a tableau. The œuvre belongs to the Painter; the atelier is the *how*.

## Why this register

Each layer of the metaphor maps cleanly to system function:

- **The œuvre is in dialogue with itself** — Monet's late water lilies are in conversation with his 1870s plein-air sketches. The system's wikilinks, semantic search, and série tracking surface that conversation explicitly.
- **Le cercle is plural specialists, equal in standing** — impressionists were a circle of friends, not a hierarchy. Agents have distinct portfolios but no precedence.
- **The Painter holds the cartoon** — the underlying intent / identity / direction lives with the user (`profile/identity.md`, `profile/directions.md`). Le cercle works against that cartoon.
- **Plein œuvre vs plein air** — the user's working mode is *indoors*, working from accumulated material, not capturing reality outdoors. The "plein" prefix carries the immediacy register; the noun is now œuvre, not air.
