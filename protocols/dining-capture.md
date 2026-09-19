# Dining Capture Protocol

Owns Intent C of `/dine`: logging a meal the user just ate. Two entry points
reach it, so the behavior lives here rather than inside either one.

- `/dine` Step 0 routes here when the message reports a past meal.
- `/hi` Dining Pulse routes here when the user explicitly associates the meal
  with a named or current trip; every other Dining Pulse capture accumulates a
  `dining_row` Scribe operation instead. `protocols/intent-capture.md` owns that
  split.

The confirmation gate below is the write boundary for both paths.

Append a row to the user's meal log file under `<paths.travel>/` (filename specified in `profile/diet.md` § Catalog files; gitignored config). Co-equal capture path with `/hi` Dining Pulse.

### C.1 Resolve source material

Three input shapes:
- **Image receipt** (`.heic` / `.jpg` / `.jpeg` / `.png`): if HEIC or file size > 256KB, convert first via `sips -s format jpeg -Z 900 <src> --out /tmp/<basename>.jpg`, then `Read` the JPEG. `sips` is macOS-native; do not assume ImageMagick.
- **PDF receipt** (outside any `catering/` folder): `Read` directly.
- **Free text only**: parse the text for restaurant name, party size, total, and any other slots the user volunteered.

For images / PDFs, extract: restaurant name, items + spicy markers, subtotal / tax / tip / total, payment method (Apple Pay / Visa last-4 / gift card / cash), date + time, party size if shown.

### C.2 Cross-check catalogs (parallel reads)

Read `profile/diet.md` § Catalog files first to resolve the five roles below; if `profile/diet.md` is missing or the section is empty, use structural discovery and note that in the closing line.

- `Grep` the city catalog file under `<paths.travel>/` for the restaurant → derive `类型`, `City`, `⭐` if listed. Match its `门店索引` by exact `(餐厅, 分店)` or receipt address; do not collapse branches.
- `Grep` the meal-history file under `<paths.travel>/` for the restaurant → first-time-or-not flag (used in 必点·备注 line if first time).
- Read the credit-perks catalog only for restaurant eligibility; never treat it as live cycle state.
- Read the benefits tracker for current availability, completed visits, and confirmed/reconciliation claim state.
- `Grep` the prepaid-balance file under `<paths.finance>/` for the restaurant → if listed, expect the profile-defined prepaid payment label unless the receipt says otherwise.
- If any catalog file is missing, skip silently and note in the closing line.

### C.2a Resolve explicit trip context

Consider a trip only when the user explicitly says the meal belongs to a named trip or to their current trip. Do not infer a trip from city, restaurant location, receipt address, date, or any combination of those facts.

- **Named trip:** resolve only an exact, unique existing trip-note title or filename under `<paths.travel>/`. If no note or more than one note matches, ask one compact question for the intended trip note; do not offer the trip-log side effect until it is resolved.
- **Current trip:** resolve only when the same session contains an explicit `current trip → exact existing trip-note path/title` mapping. Aliases, partial titles, implicit associations, and a stale, incidental, or merely present trip mention are insufficient. Otherwise ask one compact question for the intended trip note.
- **Compatible location:** read the resolved trip note and use only an existing, clearly labelled log or status section that already contains date-prefixed list entries in its local convention. If no such section exists or the match is uncertain, state that the trip-log reference is unavailable and continue with the meal capture; do not create a heading or invent a trip-note schema.

For a compatible location, record its exact section heading, section-content SHA-256, insertion anchor, before/after position, and date-prefixed list shape. Read the meal-history tracker and resolve its existing document title under the user's local title convention; do not substitute a fixed label. Compute the relative local Markdown path from the resolved trip note's directory to that tracker, then prepare only this reference:

`- YYYY-MM-DD: [<resolved-meal-history-title>](<relative-meal-log-path>)`

Before offering the side effect, search the resolved compatible section for the exact date plus resolved relative meal-history link in its local list shape. If it already exists, do not offer or write another reference. The trip note must not repeat the restaurant, rating, cost, dishes, or any other meal-row content.

### C.3 Auto-derive what you can

Before prompting, apply any `Capture defaults` declared in `profile/diet.md` to missing fields. Explicit per-visit user input wins; never ask for a slot covered by a private default.

| Slot | Derivation |
|---|---|
| **Date** | Default today; respect AGENTS.md late-sleep rule (before 03:00 → previous calendar day). User free text override wins. |
| **Restaurant** | Use the catalog's canonical name. When the chain has multiple registered branches, store `<餐厅>（<分店>）` so ratings remain branch-specific. |
| **门店地址** | Exact receipt address → use; else exact branch match in `门店索引`; else ask only when a first/new physical branch must be registered. |
| **生命周期** | Existing exact branch value → preserve; explicit user statement wins; first observed branch defaults to `active`. Never infer closure or movement from silence. |
| **City** | Catalog match → use; else infer from restaurant address on receipt; else ask. |
| **类型** | Catalog match → use; else infer from restaurant name (湘菜/川菜/etc.); else ask. |
| **⭐** | Catalog match only; else blank. |
| **人数** | Explicit user report → private capture default → receipt party-size field; else `—`. |
| **总额** | Receipt final total or explicit user report, including tip when the source says so; else `—`. |
| **人均** | If 人数 and 总额 are known, compute `总额 ÷ 人数` to cents. If only a sourced per-person amount exists, preserve it and leave 总额 blank. |
| **Platform** | Infer dine-in, pickup, delivery, or a visible booking source; preserve the source label when present; else ask. |
| **Credit** | Map the visible payment method through the private benefit profile. If no mapping exists, record the method without inferring a card or rewards program. |
| **健康 flags** | Apply the taxonomy and dish mappings in `profile/diet.md`. If the profile is absent, use only generic visible attributes and label them as inferred. Always show the derivation in the confirm prompt so the user can correct it. |

Required slots that cannot be derived: ask the user in **ONE compact prompt** (not a 6-question waterfall). Required = the capture fields its tier demands in `profile/diet.md` ("Capture tiers"), plus any of {City / 类型 / Platform} that the auto-derive could not fill. 人数 and 总额 are optional but ask in the same prompt when neither receipt nor text provides them. Optional 1-line note at the end.

Example compact prompt:
> `City? · 评分 1-10? · 再去 Y/N/Maybe? · 人数/总额? · Platform? · 1 句备注?`

Drop the `再去` slot whenever `profile/diet.md` does not require it: a `日常饮品` stop, or a restaurant already logged twice. Check the visit count before composing the prompt so a settled favourite is never asked again.

### C.4 Side-effect plan

Before writing, plan side effects. Each is opt-in via the confirm prompt (see C.5):

| Side effect | Trigger | Action |
|---|---|---|
| Meal log append | Always | Append row to the meal log file (under `<paths.travel>/`, filename per `profile/diet.md`); bump `Last updated:` to today. |
| Establishment registry upsert | First/new physical branch, or explicit address/lifecycle change | Insert or update the regional catalog's `门店索引` row with exact branch, address, lifecycle, verification date, and source. Ratings stay in the meal log. |
| Gift card update | Receipt shows gift-card balance line OR user volunteers balance | Update existing row in the gift-card catalog file (under `<paths.finance>/`, filename per `profile/diet.md`): Balance + Last updated + Source; or insert new row if first time. |
| Benefits-tracker nudge | Credit slot maps to a tracked benefit cycle in the private profile | Suggest an update and cite the affected row without copying program policy into this command. Do NOT auto-write; surface as a one-liner for the user to apply manually. |
| Catalog promotion flag | 评分 ≥ 8 AND 再去 = Y AND restaurant not currently in the relevant city catalog file (per `profile/diet.md`) | One-line suggestion at the end: `→ 考虑 promote 到 <city catalog name> (评分 N + 再去 Y, 还没在 catalog)`. Do NOT write. |
| Trip-log reference | User explicitly associated this meal with a named/current trip, one compatible trip-note location was resolved, and the exact date plus relative meal-history link is not already present | After a successful, audited meal-log append, append the date-only resolved meal-history-title link from C.2a to the trip note. Do not copy meal-row details. |
| Daily note | (never) | Daily notes are user-authored. Do NOT auto-create even if today's note is missing. |

### C.5 Confirm gate (non-negotiable)

Show only applicable side effects, numbered consecutively, and retain a number-to-action map for the selected writes. The trip-log reference is dependent on the meal-log append.

Show the user in this exact shape:

```
Draft row (meal log):
| <Date> | <Restaurant> | <City> | <类型> | <⭐> | <评分> | <再去> | <健康> | <人数> | <总额> | <人均> | <Platform> | <Credit> | <必点·备注> |

Side effects:
  1. Append row to meal log + bump Last updated
  <next number>. <establishment registry upsert if applicable>
  <next number>. <gift card update if applicable>
  <next number>. <benefits-tracker nudge if applicable>
  <next number>. <catalog promotion flag if applicable>
  <next number>. <trip-log reference if resolved>

OK to apply listed effects? (yes / partial: "1,2" / no / edit: tell me what to change)
```

User says `yes` → apply all. Partial → apply only the listed numbers. A partial selection containing the trip-log reference but not the meal-log append is invalid: explain that the reference depends on the meal row, ask the user to include the meal append or remove the reference, then re-present the plan without writing. `no` → do nothing. `edit` → patch and re-confirm. **Never silent-append.**

### C.6 Write

For the meal log: use `Edit` to insert the new row in ascending event-date order. Insert before the next-newer-date row, or append after the last row when it is newest. Never append a backfill at the end merely because it was captured today. Bump `Last updated:` line. For an establishment upsert, edit only the exact `(餐厅, 分店)` row in `门店索引`, or append a new row when no exact identity exists; bump the regional catalog's `Last updated:` line. For the gift-card catalog: same `Edit` pattern.

After writing the meal row, run `python3 scripts/dining_audit.py --json` when available. If the audit fails, repair only the row or invariant introduced by this capture before reporting success. If the audit cannot pass, or the repair removes or rolls back the new meal row, do not write the trip reference.

For a selected trip-log reference: write it only after the meal row was successfully written and the dining audit passed with that row intact. Do not use `Edit` directly. Invoke the helper with the already-resolved values; it canonicalizes the trip-note path and derives its trusted cache lock path internally:

```bash
python3 scripts/trip_reference.py \
  --trip-note "<resolved-trip-note-path>" \
  --section-heading "<exact-section-heading>" \
  --section-sha256 "<captured-section-sha256>" \
  --anchor "<exact-insertion-anchor>" \
  --position "<before-or-after>" \
  --reference "<fully-rendered-date-only-relative-meal-history-link>"
```

The helper holds an exclusive advisory lock for the entire final read, validation, insertion, durable write, and release sequence.

Interpret its JSON status exactly: `inserted` means report the reference added; `already_present` means do not duplicate it; `drift` or `anchor_missing` means skip safely; `error` means skip safely with no fallback direct `Edit`. In every non-`inserted` case, leave the successfully audited meal row intact and report the reference as deferred/skipped.

For the benefits tracker: do NOT write; surface the one-liner only.

### C.7 Report

One line:
> `Logged: <Restaurant> <Date> 评 <N>/10. <one optional flag, e.g., "prepaid balance updated", "trip reference added", "trip reference skipped after meal log", "promote candidate", or "benefits tracker update to apply manually">.`

If the meal row was not written successfully, or was removed or rolled back because its audit could not pass, report: `Not logged: <Restaurant> <Date>. Neither the meal row nor trip reference was written.` If a successfully audited meal row remains but the helper returns a non-`inserted` status, report: `Logged: <Restaurant> <Date>. Trip reference skipped: <reason>.`

## Rules

- **Confirmation gate is non-negotiable**: never silent-append. Always show the draft row + side-effect plan and wait for user `yes` / partial / no / edit.
- **One compact prompt for missing slots**: group required-and-underivable slots into a single line.
- **HEIC + large image handling**: if the input image is HEIC or > 256KB, convert via `sips -s format jpeg -Z 900 <src> --out /tmp/<basename>.jpg` first, then `Read` the JPEG. Do not assume ImageMagick.
- **Read-only on daily notes**: do NOT auto-create today's daily note even if it's missing. Daily notes are user-authored.
- **Read-only on the benefits tracker**: surface the cycle-credit nudge as a one-liner; never auto-write to the tracker.
- **Match user language**: Chinese-dominant for Chinese cuisine; English otherwise.
- **No web search**: restaurant data comes from local catalogs and the user-provided receipt only.
- **Tight output**: draft row + side-effect list + one-line confirm prompt. No preamble.
