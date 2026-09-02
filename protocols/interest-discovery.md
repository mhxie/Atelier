## Interest discovery

Outcome: a ledger of what the user actually consumes, strengthened by every
new event, that tells the collection routines what to watch and how hard.
Done when: a consumption event lands in the ledger within a day of being
recorded anywhere the system reads, and the next routine fire reads the
ledger instead of a hand-kept list.
Evidence: `scripts/interests.py list`, the ledger under `<paths.meta>/`, and
the routine outputs that cite ledger entries.

The model is event-driven, never a static list. Watching a series, attending a
concert or a game, reading a book, playing a game: each is an event on an
interest, and the interest's strength is the time-decayed sum of its events.
Strength decides how much collection the interest earns. Nothing is maintained
by hand except corrections.

### Three layers

1. **Event capture.** Sources, in order of automation:
   - AniList library via the tracking cache: `COMPLETED` is a completion,
     progress on `CURRENT` is a watch.
   - The user's own live-events table (concerts, games, conventions,
     theatre), declared as `[interests] experience_log` in the private
     digest config so its name stays out of the repo. A concert, festival,
     show, or convention names its own reason and attributes itself. A game
     does not: "A vs B" says nothing about which side, which player, or
     whether the point was the occasion, and the player the user came for
     may not have played. Game rows attach only to a name the ledger already
     knows; otherwise they wait as pending evidence for a reading.
   - Readwise highlights: a book with highlights in the window is a read.
   - Daily notes and capture dictation: the script never turns prose into
     events. `interests.py evidence` curates candidate lines (a bounded cue
     list, nothing more) and the pending rows; the orchestrator reads them,
     asks the user when the reason is not evident, and records the judgment
     with `interests.py add`, then clears the row with `resolve`. The script
     curates; the model decides.
   - Manual: `scripts/interests.py add`, or `/hi 记一下 ...` which lands in the
     daily note and surfaces as evidence on the next pulse.
   - Future pollers (ticketing, streaming, game platforms) are more
     `ingest` adapters writing the same event shape; nothing downstream
     changes when one is added.
2. **Ledger and strength.** One entry per interest with kind, aliases, and
   its events. Strength is `sum(weight × 0.5^(age_days / 90))`: attended 3,
   completed 2, watched / read / played / listened / started 1, declared
   1.5, accepted proposal 1. Status follows strength: `active` at 0.75 and
   above (one fresh event is enough), `watch` at 0.25 and above, otherwise
   `dormant`; a single watch stays active for about five weeks and on watch
   for about six months. A declared interest
   never falls below `watch`. `declined` is set by the user and is never
   re-proposed. Any new event moves the interest back to `active` on the next
   recompute, which is how reinforcement works without anyone editing a list.
3. **Collection templates by kind.** Routines read `interests.py active
   --json` and search per kind:
   - anime: next work by the same studio, a further season, a film, a disc
     release, a live event by the soundtrack artist.
   - artist: next tour date within reach, a release, a festival lineup that
     includes them.
   - team or player: a fixture within reach, a trade or signing, an injury or
     return.
   - book: the author's next title, a new edition.
   - game: DLC, a sequel, the studio's next title, a release date.
   `active` earns the full template; `watch` earns only dated events in the
   next 30 days; `dormant` earns nothing.

### Discovery modes

Every candidate a routine surfaces carries one label:

- `familiar`: directly tied to an `active` or `watch` interest.
- `adjacent`: one hop from an active interest (same studio, same label, same
  festival, same author), with the hop named in `why_now`.
- `counter-profile`: outside the ledger, included only because the evidence
  is strong and the item is dated. At most one per fire, at most a few per
  month across all routines.

A routine states `no expansion candidate` rather than padding the adjacent or
counter-profile modes. Missing feedback is unknown, not rejection.

### Proposals and feedback

A routine may write `## Proposals`: up to three interests it believes the user
would add, each with mode, evidence URL, and the ledger entry it hangs off.
The digest gives proposals one line. The user accepts with `interests.py add`
(or by dictating a consumption event, which is the stronger signal) and
declines with `interests.py decline <slug>`; declined slugs are read by every
routine and skipped. Weekly review carries an `## Interest Pulse` section
whose answers are events too.

### Privacy

The ledger lives under `<paths.meta>/` and is private. Routine prompts read
it at run time and never carry names; repo files and tests use invented
titles. Proposals cite public sources only.
