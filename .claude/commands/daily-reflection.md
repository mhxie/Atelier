---
description: Daily reflection procedure — coaching flow grounded in profile, recent notes, and open TODOs. Routed via /hi or invoked directly.
---
# Daily Reflection

Run a reflection session grounded in your notes and goals.

## Prerequisites

1. Check if `profile/identity.md` exists. If not, tell the user: "No profile found. Run `/introspect` first to build your self-model." and stop.
2. Read only the `Last built:` line from `profile/identity.md`. If older than 7 days, warn: "Your profile is stale (built on [date]). Consider running `/introspect` to refresh. Continuing with current profile." The bounded context projection below loads the declared profile content.

**Protocols used in this session:** `protocols/session-continuity.md` (connecting sessions), `protocols/epistemic-hygiene.md` (write-first nudge in warm-up, provenance tagging in write-back).

## Context Loading

1. Determine the **effective date**: if current local time is before 03:00,
   use yesterday's date; otherwise use today's. This is the user's day
   boundary.
2. If `$hi` did not already provide a current `reflection` projection, build
   the selected workflow context:

   ```bash
   uv run scripts/context_bundle.py \
     --intent reflection \
     --component profile \
     --component session \
     --component reflections \
     --component daily \
     --effective-date YYYY-MM-DD \
     --byte-budget 20480 \
     --format json
   ```

   Reuse an existing current projection instead of running the helper twice.
   This is the only preload. It includes the intent-declared profile files,
   bounded reflection excerpts, the latest session's continuity sections, and
   the explicitly requested current-capture component. If an omission is
   relevant, retrieve that source or section deliberately.
3. For recent activity related to the active themes, run
   `Bash: uv run scripts/semantic.py query "<theme>" --after "<7 days ago, YYYY-MM-DD>" --top 5 --context --format json`.
   For structural follow-up such as exact strings, known tags, or dates, use
   `Grep` against the relevant paths.

4. **Load open TODO state (silent, orchestrator working memory):**
   - `Bash: uv run scripts/todos.py list --json` — parse and hold the open list throughout the session. Source files (`source` field) are needed at session end for closure write-back.
   - Don't display this output to the user. It's context for the orchestrator's decisions in Step 0 (digest), mid-conversation (topic matching), and Step 7 (resurface vs generate).
   - **Fallback:** if the command exits non-zero, fails to parse as JSON, or returns `[]`, proceed with empty TODO context for this session and note "TODO context unavailable" under Anomalies in the wrap-up. The TODO Awareness rules below all degrade silently to no-op when the queue is empty.

## Coaching Session

Based on the loaded context, run an interactive reflection.

**Cross-cutting rule: TODO Awareness.** The orchestrator holds the open TODO list loaded in Context Loading step 4 for the entire session.

- **Mid-conversation soft surfacing (max 1 per session):** if the user mentions a topic that strongly matches an open TODO not yet mentioned this session, do **one** soft callback: "顺便,你 [date] 写过 [item] 还 open — 要不要本周 commit?" Confidence threshold is high (phrase or strong-token overlap); do not reach for tenuous matches. If user confirms commitment → mark for closure-pending or promotion at wrap-up. If user declines → do not surface again this session.
- **No proactive list dump.** The TODO list is for *matching*, not narration. Never volunteer "btw here are N open TODOs" outside Step 0 digest or explicit user request.
- **Closure write-back at wrap-up.** When the user explicitly confirms in conversation that a TODO is done or killed (Step 0 closure / stale prompt or mid-session), accumulate the closure into the **pending Scribe operations** list (see "Pre-Output: Raw Capture" below). Do NOT do direct `Edit` from the orchestrator: that bypasses the Scribe cost-partition contract and creates duplicate write paths. Two paths, two source types — both dispatched as Scribe `gtd_entry` operations at wrap-up:
  - **Done path (user completed the item):**
    - GTD-source (`source` under `<paths.gtd>/`): pending op `gtd_entry` with `operation_kind: toggle_done`, `target_file: <source>`, `line_no: <line>`, `expected_text: <bullet text from list --json>`.
    - Reflection-source (`source` under `<paths.reflections>/`): pending op `gtd_entry` with `operation_kind: prefix_line`, `target_file: <source>`, `line_no: <line>`, `expected_text: <bullet text>`, `prefix: "DONE <effective-date>: "`.
  - **Kill path (user abandons the item):**
    - GTD-source: pending op `gtd_entry` with `operation_kind: toggle_killed`. `[~]` is the killed marker per `todos.py` STATE_MAP; preserves the audit distinction from `[x]` (done). Orchestrator passes the marker glyph as a parameter.
    - Reflection-source: pending op `gtd_entry` with `operation_kind: prefix_line`, `prefix: "KILLED <effective-date>: "`. The scanner excludes both `DONE ` and `KILLED ` prefixes from open scans.
  - **Line-drift guard:** the Scribe re-reads the source line at the recorded `line_no` at dispatch time and verifies `expected_text` matches. If it does not match (the user manually edited mid-session), the Scribe aborts that operation with a "line drifted" error; the orchestrator notes the skipped closure under Anomalies in the wrap-up. The orchestrator does NOT do this verification itself.
  - Dispatch all accumulated closures at the Pre-Output stage, not mid-conversation — keeps the dialogue uninterrupted and routes mechanical writes through the cheap tier.

### 0. Continuity Check (if not the first session)

Run `Bash: uv run scripts/todos.py digest` to fetch:
- Last reflection's open Next Actions
- Closure candidates (TODOs mentioned with "已完成 X" / 等 closure language in recent daily notes)
- Top stale items (≥30d, kill-or-promote prompts)

Present **at most one item per category**, woven into the conversation per `protocols/session-continuity.md`. Do not dump the whole digest:

- **Last Next Action callback** (most recent prior session's first item, if from a different day): "Last time, you intended to [action]. How did that go?" Accept any answer without judgment — missed actions are data points, not failures.
- **Closure candidate** (if any): "I noticed you mentioned [X] in [date] — does that mean [TODO Y] is done?" If user confirms → add to closure-pending list (write-back at wrap-up).
- **Stale prompt** (if any): "[Item] has been open ~Nd with no movement. Kill, or promote to GTD with a real deadline?" If user picks "kill" → add to kill-path closures (per the write-back rules above). If "promote" → ask for due date and area, then accumulate a pending Scribe op: `gtd_entry` with `operation_kind: add`, `target_file: <active GTD file>`, `text: <item>`, structured fields `due: <date>`, `area: <#tag>`. The active GTD file is the most recently modified `.md` file in `<paths.gtd>/` (`Bash: ls -t "$OV"/gtd/*.md | head -1`); resolve at accumulation time and pass as `target_file`. Dispatch happens at Pre-Output. Do NOT append directly from the orchestrator.

Skip rules:
- Previous session was **today**: skip the Next Action callback.
- Previous session had no Next Action: skip that part.
- No closure candidates: skip silently.
- No stale items: skip silently.
- First session ever: skip everything; introduce the system briefly.

### 1. Warm-Up: Adaptive Opening
Choose opening style based on what you find in the daily note:

| What you find | Opening style |
|---|---|
| User wrote something specific today | Reflect it back: "I see you wrote about [X]..." |
| User had a big day (many entries) | Acknowledge the energy: "Busy day — what stood out most?" |
| User wrote very little or nothing | Go to yesterday or last session: "Last time we talked about [X]. How has that been sitting?" |
| A contradiction with a past note | Lead with curiosity: "Something interesting — in [[Old Note]] you said X, but today..." |
| A neglected goal is relevant | Gentle nudge: "I notice [[Goal]] hasn't come up recently..." |

Don't ask a question yet in the warm-up — just ground the conversation.

### 2. Reflective Questions (2-3, one at a time)
Use the Challenger's question taxonomy for depth:

| Question | Purpose |
|----------|---------|
| First question | **Mirror/Surface** — clarify what's on their mind |
| Second question | **Structural** — examine an assumption or connect to a goal |
| Third question | **Paradigmatic/Generative** — open new possibility or challenge a belief |

Each question should:
- Reference a specific note or goal by title in [[brackets]]
- Connect current activity to longer-term patterns or goals
- Be open-ended (not yes/no)
- Match the user's language (Chinese for Chinese goals)

### 3. Forgotten Connection (Semantic Discovery)
Use `Bash: uv run scripts/semantic.py query "<concept>" --before "<3 months ago, YYYY-MM-DD>" --top 10 --context --format json` to find a semantically related note the user may have forgotten. Reframe and retry if thin.
- Search with a concept from the conversation, not just keywords
- Go back at least 3 months for genuine surprise
- Present as a provocation, not a summary:
  "This reminds me of something you wrote in [[old note title]] — '[brief quote]'. Do you see a connection?"

### 4. Framework Application (Optional — delegate to Thinker)
If a clear pattern emerged during the conversation, dispatch to the **Thinker** agent:
- The Thinker selects and applies a framework from `frameworks/` using its decision tree
- Present the Thinker's insight as an "orient" perspective: "Looking at this through [framework]..."
- This is the Orient phase — contextualizing raw observations against mental models

### 5. Support System Pulse (brief, every session)
A lightweight check-in on the user's interpersonal interactions and support system health. Not a deep conversation topic; a structured micro-review logged for longitudinal tracking.

**Data gathering:** Scan today's daily note for people mentioned and interaction types. If the user brought up relationships during the session, use that context too.

**Ask one question** (rotate across sessions):
- "今天和谁有过有意义的互动？是哪种类型的？"（mapping interactions）
- "这周有没有在核心圈之外和谁有过连接？"（weak tie check）
- "最近有没有某段关系让你觉得能量被消耗？"（boundary check）

**Observe and log** (silently, for the output file):

| Indicator | What to track |
|-----------|---------------|
| Interactions today | Names, relationship type, Dunbar layer (DL0-DL5, per PRM template) |
| Support type exchanged | Emotional / Instrumental / Informational / Appraisal (House model) |
| Diversity flags | All same domain? Any cross-industry/cross-generational? Any new connections this week? |
| Concentration warning | Multiple support types pointing to same person? |
| Energy direction | Net giver or receiver today? Any draining interactions? |

**Offer one observation or suggestion** based on patterns:
- If interactions are concentrated: note it without judgment, suggest one low-cost diversification action
- If a new connection appeared: acknowledge it
- If no meaningful interactions logged: flag gently as a data point, not a problem
- Compare against prior sessions' logs if available for trend detection

Keep this step under 2 minutes of conversation time. The value is in the longitudinal record, not the daily depth.

### 6. Nutrition Review and Dining Pulse (brief, every session)

#### Daily nutrition review

Review the day's reported food using `profile/diet.md § Daily recap nutrition
review`. Use the daily note and session context first. If meal details are absent,
ask once: "今天三餐、零食和饮料大致吃了什么?" Do not infer missing meals.

Return a brief `on track` / `mixed` / `heavy` verdict, the observed health flags,
the rolling 7-day high-load gathering count when known, and exactly one practical
adjustment for tomorrow. A restaurant meal whose observed flags are all ones that
`profile/diet.md` says do not count toward the high-load limit is not a high-load gathering. Never recommend fasting, skipping meals,
or punitive exercise as compensation.

#### Dining Pulse

Lightweight capture of dining experiences for personal preference learning + future `/dine` recommendations. Skip silently if user has nothing to share.

**Trigger question** (one shot, only if dining didn't already come up in conversation):

> "今天有没有去新餐厅打卡, 或重访旧餐厅? 如有, 体验如何?"

**If user has dining to share, capture quickly** (do not deep-dive):
- 餐厅名 (中/英文均可)
- 评分 1-10 (8+ = top, 6-7 = good, 4-5 = ok, ≤3 = avoid)
- 再去? (Y / N / Maybe) — ask only when `profile/diet.md` ("Capture tiers") still requires it; a settled restaurant takes a dash
- **健康 flags** (per-visit, 依赖所点菜): use the taxonomy enumerated in `profile/diet.md` ("Full health-flag taxonomy" section). Multiple flags joined by `·`, blank = unobserved. Restaurant ordering 是健康管理重要部分, 不能省
- 人数 / 总额 (如可得); 人均由总额 ÷ 人数计算, 不凭同行名单或价位推断金额
- 1-2 句话: 必点菜, 服务/ambiance, 同行
- 推断 from context (else 1-line confirm): City / 类型 / booking platform / payment benefit used

**Route the capture:**
- If the user explicitly associates the visit with a named trip or a same-session explicit `current trip → exact existing trip-note path/title` mapping, route it directly to `/dine` Intent C and its confirmation gate. Do not accumulate a `dining_row` Scribe operation.
- Otherwise, accumulate a pending `dining_row` Scribe operation with `target_file` resolved from `profile/diet.md § Catalog files`, structured row fields (date, restaurant, city, type, score, 再去, health flags, party size, total, per-person, platform, credit), and `raw_content` for the 必点·备注 free-text column. Required capture fields follow the tier table in `profile/diet.md` ("Capture tiers"); dash placeholder only for missing data the user can't recall. Dispatch happens at Pre-Output. The Scribe reads the file's schema header at dispatch time and formats the row to match exactly.

**Cross-doc sync triggers** (silent unless flagged for user):
- If 评分 ≥ 8 AND 再去 = Y AND restaurant NOT in the regional catalog rotation → flag user: "Add to rotation?"
- If Credit maps to a benefit cycle configured in the private profile → also flag: "Update benefits tracker cycle subtotal?"
- If restaurant on the credit-perks catalog → mark ✅ + date in Cycle Tracking

**If user has nothing to share**: respond "记下了, 没新餐厅" and move to Close. Don't push.

**Output to reflection file** (new "Dining" section, see Output template below).

Keep this step under 60s of conversation time. Goal is consistent capture, not depth.

### 7. Close with Concrete Prompt

One specific, actionable next step tied to a goal. **Resurface before generating new** — the open queue is the first place to look:

1. Scan the loaded open-TODO list (Context Loading step 4) for items relevant to what was discussed this session — matching topic, area, or framework.
2. **If 1+ items match**: surface the most relevant as the next action. "Already on your list: [item] ([source]:[line]). Make it this week's commitment?" No need to invent.
3. **If no match AND fewer than 5 active items (P0/P1) in the queue**: generate a new concrete next action. The new action goes in the reflection file's `## Next Action` section as a single bullet line (`- <action>` or `1. <action>`). Plain prose under that header is invisible to `todos.py` and will not be resurfaced next session.
4. **If no match AND queue is bloated (≥5 active P0/P1)**: do not add. Say "Open queue is already at [N] active items. Today doesn't need to add — pick one from the list to commit to this week instead?" Surface 2-3 candidates, let user choose.

Not generic advice — something the user can do today or this week. Match user's language (Chinese for Chinese topics).

## Pre-Output: Raw Capture (Cloud-Native Mode)

Before writing the reflection file, dispatch the Scribe agent for every accumulated capture operation from this session. Under the cloud-native architecture, chat is the user's authoring surface; the orchestrator must record raw input rather than only synthesizing it into the reflection. Do this work via the Scribe, not yourself: transcribing chat input on deep-cognition voices is a known cost antipattern.

The orchestrator accumulates pending Scribe operations during the session (it does NOT write directly). Sources of accumulated ops:

| Source step | Scribe operation | When to accumulate |
|---|---|---|
| Step 0 closure write-back (TODO Awareness rule) | `gtd_entry` (`toggle_done` / `toggle_killed` / `prefix_line`) | User confirmed a TODO done or killed (GTD-source toggle; reflection-source prefix) |
| Step 0 stale prompt → promote | `gtd_entry` (`add`) | User picked "promote" with due date and area |
| Step 6 Dining Pulse, ordinary meal | `dining_row` | User shared a restaurant visit without an explicit trip association |
| Step 6 Dining Pulse, explicitly trip-associated meal | `/dine` Intent C | Route immediately to Intent C's confirmation gate; do not accumulate a Scribe operation |
| Any step where user dictates a daily-note-style narrative for a date | `daily_note` | Narrative covers events for a date whose daily-note file is missing or lacks the new content |
| Any step where a person is mentioned with bio context AND no person note exists | `people_stub` | Verify with `uv run scripts/people.py "<name>"` before adding; only accumulate if no match returned |
| User explicitly says "save this" / "记一下" with no typed slot fit | `generic` | Orchestrator picks a `<paths.wip>/` path and confirms with user before adding to pending list |

**Skip condition (per op):** the corresponding file already captures the content, or the user provided only reflection-mode input (questions, feelings, abstract discussion) for that surface. Do not invent content.

**Dispatch all accumulated Scribe ops at this stage.** For each pending op, call the Scribe with the operation-specific fields documented in `.claude/agents/scribe.md` ("Operations" section). Do NOT pre-rewrite user text before passing it; the Scribe applies verbatim + light-format rules. Trip-associated Dining Pulse captures have already routed to `/dine` Intent C and are not pending Scribe work.

Where pending ops target independent files, dispatch the Scribe calls in parallel (single message, multiple `Agent` tool calls) for latency.

If the Scribe returns a clarification request (missing schema reference, line drift on a closure, ambiguous target file), resolve it: ask the user if needed and re-dispatch, or note the skipped op under Anomalies in the reflection's Session Meta. Do not silently fall back to direct orchestrator writes.

After all Scribes return, proceed to Output.

## Output

After the interactive session, write a reflection file:

**File:** `<paths.reflections>/YYYY-MM-DD-reflection.md`
```markdown
# Reflection — YYYY-MM-DD

## Context
[Brief summary of what was discussed, with note citations]

## Key Insights
[Bullet points of insights from the conversation]

## Connections Made
[Notes or themes that were connected during the session]

## Next Action
[The concrete prompt or action suggested]

## Notes Referenced
[List of all notes cited during this session, as [[Note Title]] links]

## Support System Log
| Person | Dunbar Layer | Support Type | Domain | Direction |
|--------|-------------|-------------|--------|-----------|
| [Name] | DL0-DL5 | emotional/instrumental/informational/appraisal | work/family/friend/community | gave/received/mutual |

- Diversity score: [how many distinct domains represented today]
- Concentration flag: [any person carrying 3+ support types?]
- New connection this week: yes / no
- Observation: [one-line pattern note for longitudinal tracking]

## Nutrition
- Verdict: on track / mixed / heavy
- Observed flags: [flags from profile/diet.md, or unobserved]
- Rolling 7-day high-load gatherings: [N, or unknown]
- Tomorrow's adjustment: [exactly one practical action]

## Dining
| Restaurant | Score (/10) | 再去 | 健康 flags | 人数 | 总额 | 人均 | 必点·备注 |
|---|---|---|---|---:|---:|---:|---|
| [Name] | [1-10] | Y/N/Maybe | [flag(s) per profile/diet.md taxonomy] | [N/—] | [$N/—] | [$N/—] | [必点 + 1 line] |

- Captured to: the meal-history tracker (count of new rows appended)
- Cross-doc updates triggered: [regional rotation add? benefits tracker update? benefit-program ✓?]
- 健康 trend: [if multiple recent entries flag the heavy-load flags from `profile/diet.md` → surface as health observation in Next Action]
- (omit table entirely if no dining captured)

## Session Meta
- User engagement: high / medium / low
- Questions that landed: [which questions got thoughtful responses]
- Surprise factor: yes / no [did we surface something genuinely new?]
```

## Session Log

After writing the reflection file, emit a session log. Two steps:

**Step 1: Create skeleton.**
```
Bash: uv run scripts/session_log.py --type reflection --duration <minutes>
```
The script prints the file path (e.g., `<paths.sessions>/2026-04-11-reflection.md`). It handles the late-sleep date rule and collision auto-increment.

**Step 2: Fill the skeleton.**
Use `Edit` to populate each section of the skeleton from data you accumulated during the session:

- **Agents Dispatched:** One row per agent you dispatched. Include agent name, task summary, success/failure, and approximate turns.
- **Search Log:** Every `semantic.py query` and notable `Grep` you or agents issued. Mark whether results were useful (yes/no).
- **Questions & Engagement:** Each question you asked the user. Note depth level (surface/structural/paradigmatic) and whether it landed (got substantive response).
- **Frameworks Applied:** Any framework the Thinker applied. Include fit score if available.
- **Continuity:** Which previous session you referenced (from step 0), and the seed/next-action from step 5.
- **Decisions & Branches:** Non-obvious routing decisions (e.g., "skipped framework; user in a rush").
- **Anomalies:** empty searches, user course corrections, degraded mode (e.g., TODO context unavailable).
- **Harness Assumptions Exercised:** Any assumption from `protocols/harness-assumptions.md` that was load-bearing (e.g., "Profile stale >7d warning triggered").

If a section has no data, leave the table headers but add no rows. Do not invent data. If the write fails, warn and continue; session logs never block a session.

## Wrap Up

The reflection file in `<paths.reflections>/` is the durable session output. No write-back to daily notes — the user's daily note is their capture stream, read-only from the system's perspective. Tell the user the reflection has been saved and where to find it.
