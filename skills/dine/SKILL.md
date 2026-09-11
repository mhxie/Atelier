---
name: dine
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
For Intent C, read `protocols/dining-capture.md` and follow it; skip Steps 1-5.
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

**Soft ranking** (order the survivors). `dine_rank.py` owns the log-side
score (`log_score`, itemized in `log_score_parts`: 评分 averages, 再去, rusty
recency); take it verbatim. Then apply the catalog-side priorities, strongest
first:

1. **Credit-burn priority:** an Exclusive Tables restaurant whose relevant cycle
   has unused credit and ≤ 60 days to deadline outranks everything else.
2. Michelin star match when the mood is "Special occasion".
3. Catalog 评 (higher first) and a 场景索引 match in the regional catalog.
4. Novelty: never visited when the mood is "Surprise" or "探索"; old-favorite
   revisit when rotation 评 ≥ 2 and `days_since` > 60 from dine_rank.
5. **Health filter active:** apply the "Health-filter scoring rules" section of
   `profile/diet.md` (recent-visit penalties, clean-style bonuses,
   cumulative-load adjustments) as exclusions and tie-breakers.

Exact weights, if wanted, belong in `dine_rank.py` beside `log_score`.

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

`protocols/dining-capture.md` owns this path: source resolution, catalog
cross-checks, explicit trip context, auto-derivation, the side-effect plan,
the confirmation gate, and the write and report steps. Read it and follow it.
It is shared with `/hi` Dining Pulse, which is why it is not inline here.

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
- **Shape**: the candidate table, a short reasoning paragraph, one close line. Nothing else.
- **No web search**: cuisine + restaurant data comes from local catalog files only

Intent B:
- **Read-only on the PDF**: never modify the catering PDF
- **Read-only on daily notes**: daily notes are user-authored; the system surfaces picks for the user to record themselves and never writes to `<paths.daily_notes>/`
- **Does not touch the meal-history tracker**: workplace catering is excluded by design (low signal density per memory)
- **Per-day skip on parse failure**: if any one day's section fails to parse, skip that day with a logged warning; do not abort the whole batch
- **No web search**: menu data comes from the PDF only
