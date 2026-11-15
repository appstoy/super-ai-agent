# Changelog

## Unreleased

- **A long skill no longer disappears.** `appstoy-video` has a 36 000-character
  SKILL.md; the 24 000 cap rejected it, and because discovery swallows parse
  failures the skill simply was not in the list — the user's reasonable
  conclusion was "SAA does not have it". Big bodies now load with
  `on_demand: true`: selecting one inlines the first 24 000 characters plus a
  pointer to `skill_read` for the rest, so it stays usable without drowning the
  prompt.
- **A skill that fails to parse is reported per agent** (`skipped`) instead of
  vanishing, both in `GET /api/skills/agents` and in the panel.
- **Only agents installed on this machine get a pill.** `GET /api/skills/agents`
  used to return every known location, so the filter row offered `Cursor 0`,
  `OpenCode 0`, `VS Code 0` — noise that made the five real agents harder to
  pick out.

- **The skill panel now imports from where skills actually live.** Three tabs
  instead of two, and the two new ones are what makes the feature usable:
  - **其他 agent**: scans the folders every agent tool already uses
    (`~/.claude/skills`, `~/.codex/skills`, `~/.gemini/skills`,
    `~/.agents/skills`, Cursor, OpenCode, VS Code, and Magpie's own library) and
    offers one-click import. On this machine that is **20 skills across 4
    agents**. Sub-filter pills narrow to a single agent; 全部导入 takes the rest.
  - **Drag a folder or .zip/.tar.gz anywhere on the page**, or use 📁 选择文件.
    A wrapped archive installs under the skill's own declared name; a folder
    holding several skills imports them all; anything without `SKILL.md` is
    refused with that reason. One broken skill never blocks the rest.
  - **🔗 链接 / 目录** accepts a local folder path, a Magpie ref, or a git URL.
  - Discovery fingerprints skill content, so the same folder copied under three
    agents shows as one row naming all three rather than three duplicates. A
    folder with no `SKILL.md` is skipped instead of listed.
  - Import copies; the other agent's folder is never modified.
  - `GET /api/skills/agents`, `POST /api/skills/import`,
    `POST /api/skills/upload` (raw archive), `POST /api/skills/upload-files`
    (a folder as files).
  - Archives are unpacked in a temp tree and rejected on `../../` entries or
    contained links — zip slip and symlink escapes are both refused.

- **Dropped folders do not need a client-side zip library.** Reading a folder
  and packing it in the browser would mean shipping JSZip or calling a CDN,
  which breaks the offline story. The page sends the files as base64 and the
  server rebuilds the tree, so both folders and archives end up in the same
  "find SKILL.md, validate, copy" path with no new runtime dependency.

- **Skills finally have a visible entry point.** The only way to pick a skill
  was to type `/` in the composer, which nobody discovers by accident - the
  feature existed and looked like it did not. There is now a **技能** button in
  the composer that opens a panel:
  - *已安装*: every skill on disk with its title, description, tags, and 选用 /
    移除 per row, plus a search box and a selection count on the button.
  - *Magpie 市场*: search the catalogue and install with one click; already
    installed rows say so instead of offering a duplicate install.
  - Installing switches to *已安装* and selects the skill, because landing back
    on the market list with no sign of it reads as "nothing happened".
  - An install failure stays in the panel with its full text (Magpie's index
    drifts, so a ref can point at a folder that no longer exists and the message
    lists what the repo does have). A three-second toast cannot carry that.
  - `DELETE /api/skills/{name}` backs the 移除 button; it still refuses to delete
    a hand-written skill.
  - The `/` menu and the panel close each other rather than overlapping.

- **Install skills straight from Magpie's catalogue.** Magpie keeps a local
  index of skills (`market/skills.json` + `market/about.json`, keyed
  `owner/repo/skill`; 735 entries across 96 repos on this machine). SAA reads
  those files directly — Magpie does not need to be running:
  - `super-agent skills market <query>` searches it (ranking puts a precise name
    match above a more popular prefix match, which sorting by installs alone got
    wrong); `--official` filters to official entries.
  - `super-agent skills install <ref> --from-magpie` clones the repository once
    and copies only the referenced skill out of it.
  - In the page, typing `/` shows local matches first and then the Magpie
    section with install counts; clicking one installs it and pre-selects it.
  - `GET /api/skills/market`, `POST /api/skills/install`.
  - A configured `SUPER_AGENT_MAGPIE_MARKET_DIR` that is wrong reports the
    market as unavailable instead of silently reading another Magpie install.

- **An explicitly configured skill library is now the install target.** The
  roots list always put `<workspace>/skills` first, so a configured
  `SUPER_AGENT_SKILL_PATHS` was read from but never installed into — install and
  listing could point at different folders. Configured paths now win, and the
  workspace default applies only when nothing is configured.

- **Skills can be installed, and a running server picks them up.** Previously a
  skill had to be placed by hand and then required a restart, because the
  library was scanned once at startup:
  - `super-agent skills install <folder|git-url|owner/repo>` copies and validates
    in one step; `skills remove` undoes it; `skills path` says where the library
    lives. `--ref` pins a branch or tag, `--force` overwrites.
  - An install validates first: a skill that fails to parse is reported in
    `skipped` while the rest still install, so one bad folder cannot block a
    library.
  - `remove` only deletes folders recorded in `<library>/.installed.json`; a
    hand-written skill is refused rather than silently deleted.
  - `LiveSkillLibrary` re-checks folder modification stamps before each read, so
    a folder added while the server runs appears on the next page refresh — for
    the picker, the task payload, and the model's `skill.list` / `skill_read`.
    An unchanged library is not re-parsed.
  - `skill.list` / `skill.read` are now always registered rather than only when
    the startup scan found something, which is what made a fresh install
    invisible to the model until a restart.

- **Typing `/` in the composer now opens the skill list.** The chips were the
  only way to pick a skill, which meant scanning a row below the box. A `/`
  token at the start of the current word opens a menu under the cursor with
  live filtering (`/rel`), arrow keys + `Enter` to pick, `Esc` to dismiss.
  Picking removes the `/token` so the message stays exactly what the user meant,
  and the choice appears as a removable tag above the input. A slash anywhere
  else (a URL, `a/b`) is left alone as prose. The menu and the chip row write to
  the same selection set, so they cannot diverge. Verified in real headless
  Chrome: `node tools/verification/check_slash_menu.mjs` (12/12).

- **Skills: user-selectable instruction playbooks.** The runtime had tools but
  no way to teach it *how* to do a class of job, so every task started from the
  same generic system prompt. A skill is now a folder with `SKILL.md`
  (frontmatter + instructions + optional bundled resources), discovered from
  `<workspace_root>/skills` and `SUPER_AGENT_SKILL_PATHS`:
  - The composer has a skill bar; the selection travels in the task payload and
    the Worker inlines those bodies into the system prompt. `--skill` does the
    same on the CLI, `"skills": [...]` on `POST /api/tasks`.
  - The one-line catalogue is in *every* system prompt, and `skill.list` /
    `skill_read` are registered whenever a library exists, so a request that
    matches a skill nobody picked can still be handled. Unselected bodies stay
    out of the prompt, so a large library costs no context.
  - `GET /api/skills` lists what is selectable and what failed to load, with
    the reason. A skill with no `description` is rejected: it would be
    invisible in the catalogue and therefore unselectable.
  - An unknown selected name is reported to the model instead of dropped.
  - Load failures are per-file, matching the tool loader: one bad skill never
    blocks the rest.
  - Webhooks deliberately do *not* gain a `skills` field — it would change the
    canonical body that `X-Super-Agent-Signature` signs and break every
    existing client.
  - Ships two examples (`skills/html-page`, `skills/release-notes`). Measured
    difference on a real task: with `release-notes` selected the model refused
    to invent changes and returned a structure to fill in; without it, it
    fabricated four generic bullet points.


- **A model that only looked around was accepted as success.** Reported symptom:
  the task finished with the whole HTML page pasted into the chat and zero tool
  runs. Root cause: the "you did not write the file" correction only fired when
  the model returned *no* tool calls, so a model that called `file_list` (or
  `file_read`) and then narrated slipped past it — the deliverable was never
  written, yet the turn succeeded. The correction now also fires after
  exploratory-only tool use, keeps the code block the model already produced
  (`_strip_leading_code_block`, so the page is copied instead of regenerated),
  and names `file_write` explicitly. Exhausting the round limit on a file task
  without a successful write now reports "never called file_write successfully"
  instead of a generic round-limit failure.

- **A task now has one user-facing result instead of three disconnected ones.**
  Before this, a finished task existed only as a status row: the model's answer
  sat unread in `deliveries.payload_ref`, files were scattered under the cache
  root with no link back to their task, and a user had to guess a filename to
  see anything. Now:
  - `super_agent/application/result.py` projects one Task into a single object —
    state in plain words, the conclusion (the model's leading sentence, not its
    whole report), the artifacts with sizes and preview URLs, and a deduplicated
    plain-language step list. It is the only translation layer between storage
    records and user language, so the answer cannot drift from the records.
  - `GET /api/tasks/{id}/result` returns one result; `GET /api/results` returns
    the newest tasks already projected. Neither exposes `worker_id`, `run_id`,
    `output_ref` or any other runtime vocabulary.
  - The conclusion falls back in a defined order (assistant context entry →
    delivery text → last `result_reported` event) and refuses to show internal
    pointers such as `task-created:<uuid>`, which previously surfaced verbatim
    as the "answer" of a cancelled task.
  - The feed keeps the user's own message (`ask`) next to the Task title, since
    the title is written by the Router and can read like an internal
    instruction.
- **Two consoles for two audiences.** `/` is now the task page — one input, then
  a feed of results with inline HTML preview. The runtime console (Router,
  Watchdog, Outbox, task matrix) moved unchanged to `/ops`. The old page at `/`
  was correct for an operator and useless for a person who just wanted a page
  built.
- **Fixed: the card's ⋯ button overlapped the timestamp.** The button is
  anchored top-right, which is exactly where the relative time ("40 分钟前")
  wanted to sit, so the two collided and both became unreadable. The meta row
  now reserves right padding for the button, and the button is smaller and
  nudged up to align with the state pill. Touch devices get a visible button
  (no hover to reveal it) via `@media (hover:none)`.
- **Metrics split into "这一次" and "整个对话" instead of one unlabelled strip.**
  Two sets of numbers sitting side by side with no labels was genuinely
  confusing — the previous line read
  `🔄 4 轮 · ↑ 19,996 入 · ⚡ 90% 缓存命中 · Σ 线程累计 29 轮 · ↑ 92,684`,
  which invites "what is the difference?". They now render as two labelled
  rows: the run that produced this answer, and the total across every attempt
  on the thread (with the attempt count when there was more than one). Each
  chip also carries a `title` explaining what it measures, e.g. the cache rate
  says how many input tokens were served from the prompt cache and that only
  the misses are billed at full price.
- **Full Markdown rendering in task answers.** Fenced code with language tags,
  GFM-style pipe tables with scroll wrappers, headings h1-h6, bullet and
  numbered lists, blockquotes, horizontal rules, bold, italic, strikethrough,
  and safe outbound links. All rendered natively without an external CDN, so the
  console remains fully functional offline and from an installed wheel.
- **Card action options: pin, archive, colour, and delete.**
  - Every task card and output file tile now has a subtle `⋯` hover menu.
  - `PATCH /api/tasks/{id}/flags` persists user presentation state (`pinned`,
    `archived`, `color`) in a dedicated `task_flags` table, decoupled from the
    task domain model. Pinned cards float to the top of the feed with a
    `📌 置顶` badge; archived tasks are hidden by default and toggleable with
    a new `📦 显示已归档` filter button; colours (7 palette options) render a
    left accent border.
  - `DELETE /api/files?path=...` allows true disk removal of unwanted artifacts.
- **Feed folding & expand/collapse.**
  - Long lists (e.g. 29 succeeded tasks) are capped at a default threshold
    (`FOLD_LIMIT = 6`), collapsing older items into an interactive button
    (`还有 N 个更早的结果 · 展开全部`).
  - Clicking expands all items, toggling the button to `收起，只看最近的 6 个`.
  - Pinned cards are immune to folding, staying anchored at the top.
- **Fixed: retried tasks under-reported their real cost, sometimes as zero.**
  `TaskResult.metrics` read only the newest Run. On a thread whose newest Run
  was still live (or that had failed twice before succeeding), the result showed
  `0 rounds / 0 tokens` for work that had actually spent 29 rounds and 92,684
  prompt tokens. Metrics now report two numbers: the attempt that produced the
  answer, and the sum across every attempt on the thread
  (`thread_attempts`, `thread_model_rounds`, `thread_prompt_tokens`, …). The card
  shows the thread total only when it differs, so an ordinary single-attempt
  task stays quiet instead of printing the same figure twice.
- **Prompt-cache impact of the refusal filter, measured.** Dropping a
  self-defeating assistant turn from replayed history does change the prompt —
  a controlled experiment shows the filtered prompt is byte-identical to one
  that never had the refusal, so the only cost is a single cache miss on the
  first continuation after such a turn. Every later turn hits normally. Worth
  it: inheriting the excuse cost a whole failed turn, and re-sending context
  already invalidates the cache each turn anyway (measured hit rate rises
  58.8% → 74.5% → 89.2% across four turns).
- **Execution telemetry on every result: tokens, rounds, cache hits.**
  The runtime already received token usage from the provider and threw it away —
  `ModelUsage` had no cache fields, nothing read `usage`, and `RunRecord` had
  nowhere to store it. Now:
  - `ModelUsage` carries `cached_tokens` / `cache_write_tokens` plus a
    `cache_hit_rate`, and the OpenAI-compatible adapter parses
    `prompt_tokens_details` (absent on many endpoints, so it degrades to zero).
  - `RunMetrics` accumulates rounds, tokens, cache hits and tool calls during
    execution, where the usage object actually exists, and `RunRecord` persists
    them so a result can explain its own cost after the fact.
  - `GET /api/tasks/{id}/result` exposes a `metrics` block; the card renders it
    as a compact strip (`🔄 1 轮 · ↑ 1,159 入 · ↓ 51 出 · ⚡ 88% 缓存命中`).
  - Measured on a real two-turn thread: the cold turn reported 0% cache, the
    continued turn 88.35% (1024 of 1159 prompt tokens) — which is the concrete
    reason continuing a thread is cheaper than starting a new task.
- **Fixed: a give-up turn poisoned the thread it was continued from.**
  `_durable_context` replays every prior context entry, including the assistant
  turn where a model narrated its own limits ("我没有写文件的能力，请手动保存").
  Continuing that thread fed the next turn a reason to refuse again. Assistant
  turns matching a refusal pattern are now excluded from replayed history;
  user messages and tool results are never filtered.
- **Fixed: adding a column to a model broke every existing database.**
  `create_schema` only created missing tables — it never altered an existing
  one, so the new telemetry columns would have failed with "no such column" on
  any upgraded checkout. Added an idempotent `_ensure_columns` that adds missing
  columns with a conservative default; Alembic remains authoritative for
  managed deployments.
- **Dedicated Artifacts Shelf & Category Navigation.**
  - Added `GET /api/artifacts` aggregating all workspace-created files with
    direct preview URLs and originating task titles.
  - Results are now categorized by state (`正在进行`, `已经做好`, `执行失败`,
    `已取消`, `全部`) with dynamic badge counters, defaulting to `正在进行`
    so users are not overwhelmed by dozens of historical cards on first load.
  - Added a dedicated "产出文件" shelf below results with instant search/filter
    and 1-click inline HTML modal preview.
- **Deep Process Trace with Real Evidence.**
  - Steps now feature two-layer inspection: clean headline and human summary for
    everyone, plus expandable "底层参数与真实证据" showing raw user prompts, Fast
    Path filtering decisions, tool invocation arguments (formatted JSON),
    raw tool outputs, and Model Gateway request IDs.
- **Context Continuation & Multi-Turn Conversations.**
  - Every card now offers `💬 基于此任务继续`. Clicking activates continuation
    mode with a visible thread banner; submitting a follow-up automatically
    reuses the same persistent `thread_id`, resuming the same Worker sandbox and
    memory context.
- **The task page can tell you when a task finishes.** Browser notification,
  on by request. Two deliberate rules: permission is asked on the first submit
  (a real user gesture — asking on page load is hostile and browsers penalise
  it), and the popup fires **only while the tab is hidden**; when the page is
  visible the result card is already in front of the user, so a system popup
  would be a duplicate of the same news. Clicking the notification focuses the
  tab and scrolls to that result. The header control reflects the three
  permission states and tells the user how to unblock when the browser has
  denied it. Announcement happens in exactly one function guarded by a flag
  carried on the result object — the flag is also preserved across
  `upsert()`, otherwise every poll re-announced the same finished task. A task
  that had already finished before the page loaded is marked announced, so
  opening the page never fires a burst of popups for old history.
  Verified against real Chrome over CDP (`tools/verification/check_browser_notify.js`:
  granted + hidden → exactly one notification; granted + visible → zero).
  Static contract pinned by `tests/e2e/test_task_page_notify.py`.
- **Fixed: the task page rendered its own Chinese copy as `\u4F60\u60F3...`.**
  283 characters were written as literal escape sequences, so the headline read
  "\u4F60\u60F3\u8BA9\u5B83\u505A\u4EC0\u4E48\uFF1F" instead of
  "你想让它做什么？". Restored to real UTF-8, and a test now asserts the page
  contains no literal escapes — the failure mode is invisible in a code review
  and obvious to every user.
- **A submitted request now executes exactly once.** `POST /api/tasks/async`
  ingests a trigger and hands it to the in-process runner, while a durable Outbox
  worker polls the same queue. `submit_via_outbox` fell back to `submit()`
  whenever it failed to claim the event — and `process_dedupe` returns `None`
  both when it processed the message *and* when another consumer holds the claim,
  so "not mine" looked identical to "run it directly". One request therefore
  produced two overlapping Runs, called the model twice, and wrote the same
  artifacts twice; measured on the real database, three of the newest tasks had
  two Runs on one Trigger. The claim is now inspected before it is attempted, and
  an event owned elsewhere is reported as `ignored` instead of being re-executed.
  Covered by `tests/integration/test_exactly_once.py`.
- **A provider failure is now an outcome, not a 500.** `POST /api/tasks` mapped
  only `AgentError` to 502; any other provider/transport exception escaped as an
  opaque 500, so the console could not tell "the task failed" from "the server
  is broken".
- Added `tools/verification/verify_user_result.py` as runnable acceptance
  evidence (29 checks, no network or credentials) and
  `tests/e2e/test_task_result_api.py` (6 tests).

- **Generated artifacts no longer land in the repository root.** Every Task now
  writes into its own folder: `<workspace root>/项目缓存文件/<short task id>-<title
  slug>/`. `file.write` receives that folder through `ExecutionContext.workspace_dir`,
  resolves relative paths inside it, and rejects absolute paths or `..` climbs —
  so one task cannot scatter files across the project or collide with another.
  Reads are deliberately NOT confined: `file.read`/`file.list` still reach the
  workspace (an agent that cannot inspect the project is useless), and
  `file.list` defaults to the task's own folder so the model sees what it just
  produced. The folder name is derived from the Task, so a resumed Worker
  reuses the folder its first attempt created instead of forking a new one.
  New `GET /api/tasks/{task_id}/files` lists one task's artifacts.
  Configurable with `SUPER_AGENT_TASK_WORKSPACES` (default on) and
  `SUPER_AGENT_CACHE_DIR_NAME` (default `项目缓存文件`).

- Added the Router/Worker event-driven foundation, persistent Outbox, context recall, safe tool policies, Watchdog, recovery, plugin SDK, work-channel adapters, and task control API.
- Added automated tests, type checks, lint checks, and security regression coverage.
- Fixed cross-provider tool-call compatibility: standard/legacy/text-embedded/Anthropic-style
  calls, lenient argument parsing, normalized tool names in both directions, and tool errors
  returned to the model for self-correction instead of failing the Worker.
- Added user-loaded tools: functions, Tool classes, factories, manifests, module paths,
  directories and entry points, with failure isolation and capability checked by ToolPolicy.
  New `super-agent tools` command, `GET /api/tools`, SDK exports and `docs/custom-tools.md`.
- Speculative execution now derives capabilities from the registry instead of
  hardcoding `file.read`, so user-loaded tools work on that path too.
- Added `tools/verification/verify_tool_compat.py` as runnable acceptance evidence
  (21 checks, also wired into pytest).

- Streaming SSE tool_calls are now assembled across deltas, and stream requests
  carry normalized tool declarations; Windows `path:attr` tool specs parse correctly.

### Fixed: three runtime defects that made the agent look broken

- **Stalled tasks never reported anything.** The batch drain ran a whole claim
  batch inside one transaction and constructed the model gateway outside any
  per-message guard, so a missing credential rolled back the attempt counter
  and the same queue head was re-picked forever (`attempts` stayed `0`). Each
  event now fails independently, records its `last_error`, backs off
  exponentially, and is parked as `FAILED` after `max_attempts`. A corrupt
  payload can no longer escape as a batch-level exception. `run_forever`
  exposes `last_error` instead of swallowing loop failures, and
  `create_schema` now adds the missing `(status, available_at)` and
  `(status, attempts)` queue indexes idempotently on existing databases.
- **Long tasks were killed mid-generation, and near-miss tool names broke calls.**
  Three defects behind "the task sits at starting/running and no tool is called":
  the Run lease was 60s and was only renewed *between* rounds, so one blocking
  model call (a 70 KB HTML page takes ~135s) let the recovery thread time the Run
  out and the turn died with "run lease expired"; the Worker held its write
  transaction unless `detach_execution` was set, blocking the keepalive thread on
  a second SQLite connection; and a model that invented `write_file`/`list_tools`
  for `file_write`/`file_list` got "tool is not registered", concluded no write
  tool existed, and pasted the document into chat. Fixes: a lease keepalive
  thread renews at lease/3 for the duration of every model call, the write
  transaction is always released first, the default lease is 300s and the model
  timeout 180s, `resolve_tool_name` maps near-miss names by token set (also
  registering bare `read`/`write`/`list`), a model that gives up is corrected
  once with the exact tool name, and `/api/tasks` reports a provider failure as
  502 instead of crashing. Measured on the real gateway: the same prompt that
  previously failed now finishes in 136s and writes a 72 KB file.

- **Tool calls were unreliable against real providers.** Three protocol
  defects: the Worker rebuilt the whole durable context (including its own
  assistant/tool turns) into a single user message on every round, so the model
  saw each tool result twice in a shape no OpenAI-compatible endpoint expects;
  `tool_choice` was never sent, so a model could answer with prose and claim it
  had saved the file; and the previous prose-only nudge looped until
  `max_rounds` because the "did we write yet" check read the wrong context.
  The Worker now sends a standard system/user/assistant/tool conversation,
  forces `tool_choice=required` on the first round of a file request, returns to
  `auto` after a write, and nudges at most once so a stubborn model cannot spin.
  Measured against the real Magpie gateway: 5/5 file requests produced a real
  file, and a read-then-write task used both tools.
- **The agent still would not write, even with a write tool.** Two further
  defects hid behind the tool registration: the Worker system prompt said
  "use registered read-only tools", so models concluded they had no write
  capability; and a response with no tool call was accepted as success, so a
  model could paste a whole HTML document into chat, claim it was saved, and
  leave no file on disk. The prompt now states the real toolset and the
  requirement to call `file_write` for deliverables, and when a file-seeking
  request ("html", "页面", "报告", "save"…) finishes with prose only and no
  write happened, the Worker gets one corrective round before completing.
- **The agent could not create files.** The built-in registry only exposed
  `file.read`, so "build me an HTML page" could never succeed and the write
  tool was never in the model's tool list. Added `file.write` (root-scoped,
  size-capped, `overwrite` required to replace) and `file.list`, plus
  workspace artifact endpoints (`/api/files`, `/api/files/content`,
  `/files/{path}`) and a console preview panel so a generated HTML page
  actually renders. Speculative execution still excludes write capabilities.
- **Live work was a number with no substance.** `/api/runtime/summary` now
  returns `active_run_details` (task title, run status, attempt, heartbeat
  age) and `worker_last_error`; `/api/outbox` returns the queued rows with
  text, attempts, backoff and error. The console gained "正在跑的任务",
  "待处理队列" and "它产出的文件" panels. The model-judged watchdog loop now
  defaults to 60s instead of being off, so a stalled run is actually reclaimed.
