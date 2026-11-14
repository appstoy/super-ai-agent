# Changelog

## Unreleased

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
