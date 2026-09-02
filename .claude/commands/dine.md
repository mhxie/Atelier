---
description: Restaurant recommendation flow using local context and credit-burn signals.
---
## Purpose

Four intents (auto-detected from args):
- **A. Restaurant Recommendation** (default): pick 3 restaurant candidates based on user-supplied context, historical preferences, and credit-burn opportunities. Read-only on catalog docs under this intent.
- **B. Workplace Catering Tracker**: parse a weekly catering PDF from the folder mapped to a workplace slug in `profile/diet.md`, choose health-aware picks for the user's attendance days, and surface a confirmed table for the user to record themselves (the system does not write to daily notes).
- **C. Meal Log Capture** (ad-hoc): log a meal the user just ate. Parses receipt images (HEIC/JPG/PNG/PDF) when provided, cross-references catalogs for missing slots, asks ONE compact question for what cannot be derived, shows a draft row + side-effect plan, and appends to the private meal-history tracker on confirm. An explicitly trip-associated capture may also add a date-only meal-history reference to that trip note. Co-equal capture path with `/hi` Dining Pulse.
- **D. Establishment Update**: update one physical branch's address or lifecycle without creating a meal-history row.

## Quick start

Intent A examples:
- `/dine` → ask all context
- `/dine 工作日午餐` → use as scene hint, ask remaining
- `/dine 朋友 4 人 川菜 dinner` → use as filters, ask remaining
- `/dine <city> burn credit` → location hint + flag credit-burn priority

Intent B examples (first arg = workplace slug mapped in gitignored `profile/diet.md`):
- `/dine <slug>` → find the latest PDF in the mapped catering folder covering this week, pick per `profile/diet.md` attendance pattern
- `/dine <slug> <pdf-path>` → explicit PDF
- `/dine <slug> all` → all 5 weekdays (override)
- `/dine <slug> <day-codes>` → custom attendance set (override; any day-code combination works)

Intent C examples (logging a meal you just ate):
- `/dine log 今天午饭吃的 <restaurant> 两人 $<amount>` → free-text meal report
- `/dine 今天晚饭吃的是 /path/to/receipt.heic` → receipt image (HEIC auto-converted before Read)
- `/dine 昨天 <restaurant> dinner $<amount>` → dated free text (respects late-sleep rule)
- `/dine log /path/to/receipt.pdf` → receipt PDF outside any catering folder

Intent D examples (maintaining a physical branch):
- `/dine status <restaurant> <branch> closed`
- `/dine status <restaurant> <branch> moved <new-address>`

If args present, parse them as initial filters; only ask for slots not derivable.

## Step 0: Intent detection

Parse args. Precedence: **D → B → C → A** (most specific match wins; ambiguous → ask user one line before routing).

Read `profile/diet.md` once, if present, to resolve workplace slugs, catering folders, and `## Catalog files`. Do not assume the private mappings from examples in this command.

Route to **Intent D** when the leading subcommand is `status`.

Else route to **Intent B** if any of:
- First arg matches a workplace `Slug` declared in `profile/diet.md`
- Any arg is a `.pdf` path under a catering `Folder` declared in `profile/diet.md`
- Args contain the literal token `catering`

Else route to **Intent C** if any of:
- Leading subcommand `log` (e.g., `/dine log <freetext>`)
- Args contain past-tense / reporting markers: `记录` / `log` / `吃了` / `吃的` / `刚吃完` / "今天X吃的" / "昨天Y" / "just had"
- An image path (`.heic` / `.jpg` / `.jpeg` / `.png`) is provided
- A `.pdf` path is provided **outside** any `$OV/*/catering/` folder
- Free text mentions a specific restaurant + amount/party (e.g., `<name> 两人 $<amount>`) without a forward-looking verb

Do NOT route to C if the message is forward-looking: contains `推荐` / `去哪吃` / `想吃` / `what should I eat` / `where to eat` / `今晚吃什么` → fall through to A.

Otherwise route to **Intent A** (continue to Step 1 below).

For Intent B, jump to the "Intent B: Workplace Catering Tracker" section near the bottom and skip Steps 1-5.
For Intent C, jump to the "Intent C: Meal Log Capture" section near the bottom and skip Steps 1-5.
For Intent D, jump to the "Intent D: Establishment Update" section near the bottom and skip Steps 1-5.

## Step 1: Gather context

For missing slots, ask via `AskUserQuestion` or sequential 1-line prompts (whichever fits faster). Required slots first; optional slots only if useful.

| Slot | Options | Required |
|---|---|---|
| **Location** | Home region / nearby city / travel destination / other | Y |
| **Party** | Solo / Partner / Family (N) / Friends (N) / Mixed work | Y |
| **Meal** | Lunch / Dinner / Brunch / Late night | Y |
| **Time budget** | Quick (<30min) / Standard (1-2h) / Leisurely (2h+) | Y |
| Mood / cuisine | Free text or options declared in `profile/diet.md` | N (default any) |
| **Health filter** | options enumerated in `profile/diet.md` ("Health filter input options" section) plus `no preference` | N (default no preference) |
| Budget cap | User-supplied cap / no cap | N |
| Avoid recent | Last 30 / 60 / 90 days | N (default 30) |

## Step 2: Load data (parallel)

Resolve the following roles through `profile/diet.md § Catalog files`. Paths are relative to `$OV` unless absolute. If the profile or a mapping is absent, use structural discovery under `<paths.travel>/` and `<paths.finance>/` as a fallback and disclose the gap.

- Regional dining catalog (rotation + Michelin wishlist + 场景索引 + `门店索引`), under `<paths.travel>/`
- Meal-history tracker (history with 评分 + 再去 + recency), under `<paths.travel>/`
- Credit-perks dining catalog (eligibility + city catalogs), under `<paths.travel>/`
- Benefits tracker (current cycle credit status, for burn signal), under `<paths.finance>/`
- Prepaid-balance tracker (balances per restaurant, for a soft "use it" signal), under `<paths.finance>/`
- For travel destinations: use the matching city section plus the corresponding local guide under `<paths.archive>/practical/travel/`

**Missing-file fallback:** if any of these is absent, skip it silently and note the gap in the closing line ("scored without [missing source]"). The recommendation still produces; the user can decide whether to recreate the catalog.

**Log-side aggregates (deterministic):** run
`uv run scripts/dine_rank.py --avoid-days <window>` first; it returns
per-restaurant visit counts, last-visit recency, `评分` averages, `再去`
state, the log-derived score component, and the avoid-window exclusions.
Do not re-read the meal-history tracker row by row; combine the returned
`log_score` with the catalog-side factors below.

**Integrity preflight:** when running from the Atelier repo, execute `python3 scripts/dining_audit.py --json`. Use its `establishments` rows as the address/lifecycle source. If it reports errors, exclude the affected rows or source from scoring and disclose the degraded input. Warnings such as missing legacy values do not block recommendations.

## Step 3: Filter + score

**Hard filters** (eliminate non-matches):
- Location matches user's region
- Cuisine NOT in avoid list
- Estimated price ≤ budget cap (allow 20% margin)
- Drive time fits time budget (heuristic: 🚗 count × 15min one-way)
- For Quick lunch: ⌛ ≤ 1
- For "Special occasion": Michelin OR Exclusive Tables only
- Skip restaurants visited within `avoid recent` window (from the meal-history tracker)
- Skip physical branches whose lifecycle is `closed` or `moved`; allow `unknown` only with an explicit lifecycle warning

Treat `(餐厅, 分店)` as establishment identity. Join visit ratings only to an exact branch-specific restaurant name; never assign a chain-wide or legacy ambiguous score to a branch by inference.

**Soft scoring** (rank candidates):
| Factor | Score |
|---|---|
| Log-side component | `dine_rank.py` `log_score`, verbatim (owns 评分 averages, 再去, rusty recency; components itemized in its `log_score_parts`) |
| Catalog 评 (legacy field) = 3 | +3 |
| Catalog 评 = 2 | +2 |
| Catalog 评 = 1 | +1 |
| 场景索引 match (regional catalog) | +3 |
| Never visited + mood = "Surprise" or "探索" | +2 |
| **Credit-burn priority** (Exclusive Tables restaurant + relevant cycle has unused credit AND ≤ 60d to deadline) | **+5** |
| Michelin star match + mood = "Special occasion" | +4 |
| Old-favorite revisit (rotation 评 ≥ 2 + `days_since` > 60 from dine_rank) | +2 |
| **Health filter active** | apply scoring rules from `profile/diet.md` "Health-filter scoring rules" section (recent-visit penalties, clean-style bonuses, cumulative-load adjustments) |

## Step 4: Output

Top 3 candidates as a table:

```markdown
| # | 餐厅 | 类型 | $ | 距离·等待 | Why | Credit signal |
|---|---|---|---|---|---|---|
| 1 | <restaurant-A> | <cuisine> | <$range> | <distance·wait> | <reason from catalog/log: 评 N + scene fit + recency> | n/a |
| 2 | <restaurant-B> | <cuisine> ⭐ | <$range> | <distance·wait> | <reason: Michelin tier + last log rating + want-revisit>; **<credit-card> <perk-program> <half> deadline <MM/DD> ($<amount>)** | 🔥 burn |
| 3 | <restaurant-C> | <cuisine> ⭐ | <$range> | <distance·wait> | <reason: novelty + Michelin tier + perk-eligible>; <perk-program> 候选 | <credit-card> <half> ✓ available |
```

Brief reasoning paragraph (2-3 lines) below the table:
- Mention the top filter constraints applied
- Flag any credit-burn 紧迫性 in plain text
- If filter returned <3 candidates, note relaxation taken (e.g., "loosened distance to 🚗🚗")

## Step 5: Close

End with one line:
> "选哪个? (回 1/2/3) 我帮你在常用 booking platforms 查时段, 或者 /dine + 新约束 重排"

Do NOT auto-book; just surface candidates.

## Intent B: Workplace Catering Tracker

### B.1 Resolve PDF

- If an arg is a `.pdf` path → use it directly.
- Else: resolve the slug's `Folder` from `profile/diet.md`, list `<Folder>/*.pdf`, and pick the one whose filename date range covers the current calendar week. Typical filename pattern: `<Workplace> Catering_<Mon> <DD>-<Mon> <DD>.pdf`. If multiple match (e.g., manual override), prefer the most recent `mtime`.
- Optional date arg `YYYY-MM-DD` shifts the target week (Mon of that week).
- 0 matches: report `本周菜单还没传到 <configured folder>` and exit cleanly.

### B.2 Parse menu

Read the PDF (`Read` tool). Extract per-day sections (Mon/Tue/Wed/Thu/Fri). Each day has a theme + items + dietary tags (`v` / `vg` / `mwgci`).

### B.3 Determine attendance days

Read attendance pattern from `profile/diet.md` (the section matching the resolved `<slug>`, key: `Attendance days`). Override via the second CLI arg:
- `all` → all 5 weekdays present in the PDF
- `<day-codes>` → custom set (case-insensitive day codes; any combination)

If `profile/diet.md` is absent or has no entry for `<slug>` → ask the user once, do not assume a default. Map each chosen day code to an absolute date based on the resolved week.

### B.4 Pick per day (reuse Step 3 health-filter logic)

Read **dietary picking priorities** and **flag taxonomy** from `profile/diet.md` (the `<slug>` section). Apply the policy verbatim — do not bake personal preferences into this committed file.

Generic fallback when `profile/diet.md` is absent: choose ONE protein + 1-2 veg sides per day, no specific oil/protein bias, and ask the user to confirm the picks before presenting.

The skill itself enforces only the structural shape (one row per attendance day, columns: protein + veg + sauce-note + flag). The semantic content is policy from the private file.

### B.5 Preview

Show table (one row per attendance day; values fill from B.4):

```markdown
| Date | Day | Theme | Pick | Flag |
|---|---|---|---|---|
| YYYY-MM-DD | <day> | <menu theme> | <protein> + <veg sides> + <sauce/dressing note> | <flag from profile/diet.md taxonomy> |
```

Add a 1-2 line cross-day note if `profile/diet.md` defines cross-day rules (e.g., protein rotation, 油脂 balance). Otherwise omit.

### B.6 Present

Show the user the per-day picks as ready-to-paste lines so they can record them in their daily notes themselves. The system does not write to daily notes.

For each attendance day, output one line in the format:
`<Slug> <YYYY-MM-DD> — <Day> <theme> (<pick>, <flag>)` where `<Slug>` is the user-provided slug capitalized (first letter only).

### B.7 Report

```
/dine <slug> summary (<week-range>)
  picks:  N   (date list)
```

## Intent C: Meal Log Capture

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
| **Date** | Default today; respect CLAUDE.md late-sleep rule (before 03:00 → previous calendar day). User free text override wins. |
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

## Intent D: Establishment Update

Use this path for address or lifecycle maintenance without inventing a visit.

1. Resolve the regional catalog from `profile/diet.md`, then find an exact `(餐厅, 分店)` row in `门店索引`.
2. Accept only lifecycle values `active`, `closed`, `moved`, or `unknown`. Require a complete street address for a new row. Preserve an existing address unless the user explicitly changes it.
3. Show the current row and proposed row, then ask one confirmation: `Apply establishment update? (yes / no / edit)`.
4. On `yes`, update or append exactly one registry row, set `核验日` to the local effective date, record the source without copying private prose, and bump the catalog's `Last updated:` line.
5. Run `python3 scripts/dining_audit.py --json`. Repair only the introduced row if validation fails; otherwise report one line: `Updated: <restaurant> (<branch>) → <status>.`

Do not add a meal-history row, change ratings, or infer that another branch shares this lifecycle.

## Rules

Intent A:
- **Read-only on catalog docs (under Intent A)**: do NOT modify the regional dining catalog or the credit-perks catalog when handling a recommendation request. The meal-history tracker is also read-only under Intent A; appends route through Intent C (or `/hi` Dining Pulse).
- **0 candidates after hard filter**: relax most-restrictive constraint by 1 step, retry; surface 1-2 closest matches with flag "relaxed: <constraint>"
- **Always show credit-burn opportunity** if relevant under the live private benefits tracker. Even if the eligible restaurant does not match the exact mood, surface a fourth line using only the currently relevant benefit details.
- **Match user language**: Chinese-dominant if cuisine is Chinese; English if Western
- **Keep output under 30 lines** (table + 2-3 line reasoning + 1 close line)
- **No web search**: cuisine + restaurant data comes from local catalog files only

Intent B:
- **Read-only on the PDF**: never modify the catering PDF
- **Read-only on daily notes**: daily notes are user-authored; the system surfaces picks for the user to record themselves and never writes to `<paths.daily_notes>/`
- **Does not touch the meal-history tracker**: workplace catering is excluded by design (low signal density per memory)
- **Per-day skip on parse failure**: if any one day's section fails to parse, skip that day with a logged warning; do not abort the whole batch
- **No web search**: menu data comes from the PDF only

Intent C:
- **Confirmation gate is non-negotiable**: never silent-append. Always show the draft row + side-effect plan and wait for user `yes` / partial / no / edit.
- **One compact prompt for missing slots**: do NOT waterfall 6 questions. Group required-and-underivable slots into a single line.
- **HEIC + large image handling**: if the input image is HEIC or > 256KB, convert via `sips -s format jpeg -Z 900 <src> --out /tmp/<basename>.jpg` first, then `Read` the JPEG. Do not assume ImageMagick.
- **Read-only on daily notes**: do NOT auto-create today's daily note even if it's missing. Daily notes are user-authored.
- **Read-only on the benefits tracker**: surface the cycle-credit nudge as a one-liner; never auto-write to the tracker.
- **Match user language**: Chinese-dominant for Chinese cuisine; English otherwise.
- **No web search**: restaurant data comes from local catalogs and the user-provided receipt only.
- **Tight output**: draft row + side-effect list + one-line confirm prompt. No preamble.
