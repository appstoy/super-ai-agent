# Super AI Agent

说一句话，它真的把东西做出来给你 —— 事件驱动的持久化 Agent 运行时。跑在你自己机器上，
产物落在你自己的磁盘上。

**能做什么**：输入框里打 `/` 选技能（或直接说），它自己规划、调用工具、写出真实文件，
做完给你一个能直接打开的结果。有缓存命中、轮次、工具调用等运行指标，也有置顶/归档/配色
和产物文件架。

**工具 vs 技能**：工具是"能做什么"（读文件、写文件、列目录、你自己注册的 Python 函数）；
技能是"知道怎么做"（带 `SKILL.md` 的说明手册，只在被选中时才进入提示词）。见
[技能文档](docs/skills.md)。

---

Super AI Agent is an event-driven, persistent Agent platform inspired by the Router/Worker Super-agent architecture described in the project research documents.

The repository now implements the full article architecture (see `docs/article-conformance.md`): a resident Router session with global memory and trigger batching, two-level event filtering, many-to-many subscription fan-out, an asynchronous ReAct loop (`task.start/stop/status` with completion re-injection), channel-aware delivery, chain-of-recall (`recall.read`), human-gated speculative execution, BYOM endpoint daemons, and a model-driven Watchdog control loop — on top of the Phase 0-6 foundation and Phase 7 runtime bridge: Router/Worker orchestration, persistent Outbox and Trigger ingress, asynchronous task acceptance, durable status polling, same-Thread Worker resumption, safe local file reading, Run leases and bounded retries, persistent Watchdog recovery, context recall, plugin contracts, work-channel adapters, and a task control API. Model providers are isolated behind the provider-neutral `ModelGateway`, with deterministic mocks for offline tests and OpenAI-compatible adapters for real services, including Magpie's local hosted-model gateway.

## Requirements

- Python 3.12 or newer

## Development setup

Use a Python 3.12 virtual environment, then install the development extras:

    py -3.12 -m venv .venv
    .\.venv\Scripts\Activate.ps1
    python -m pip install --upgrade pip
    python -m pip install -e ".[dev]"

Copy ".env.example" to ".env" only when you need non-default local settings. Keep secrets out of the repository; production credentials should be supplied through a secret manager.

Run the health API:

    super-agent serve --host 127.0.0.1 --port 8000

There are two surfaces, for two different people.

**`http://127.0.0.1:8000/` — the task page.** You type one sentence and get
results back. No Trigger, Worker, Run or Outbox vocabulary appears here; each
finished task is rendered as a card that answers the only three questions a
user has:

- did it work (`做好了` / `没做成` / `已取消`, plus a duration),
- what came out (the model's leading sentence, not its whole report),
- how it was done (a two-layer step trace: high-level summary for normal users,
  plus expandable real evidence like input parameters, tool outputs, and filter
  decisions for technical auditing),
- where you can see it (files attached to cards, plus a dedicated **产出文件**
  shelf at the bottom with real-time search, file deletion, and 1-click preview),
- user-owned card actions (hover `⋯` on any card or file to pin, archive, pick
  an accent colour, or delete; pinned items float to the top; long lists fold
  older items into a single expandable button),
- full native Markdown rendering (fenced code, tables, headings, blockquotes,
  lists, without external CDN dependencies),
- context continuation (click `💬 基于此任务继续` on any card to continue the
  dialogue within the same persistent thread and Worker memory),
- what it cost (`🔄 1 轮 · ↑ 1,159 入 · ↓ 51 出 · ⚡ 88% 缓存命中`): model
  rounds, prompt/completion tokens, and prompt-cache reuse. The cache rate is
  the concrete reason a continued thread is cheaper than a fresh task — measured
  on a real two-turn thread, the cold turn hit 0% and the continuation 88%.

A task can run for minutes, so the page also raises a **browser notification**
when it finishes. Two rules keep it from being noise: permission is requested on
your first submit (a real user gesture, never on page load), and the popup only
appears when the tab is **not** visible — if you are looking at the feed, the
card is already in front of you and a system popup would be a second copy of the
same news. Clicking the notification focuses the tab and scrolls to that result.
The header control shows the current state and explains the fix when the browser
has blocked notifications. `tools/verification/check_browser_notify.js` proves it
against a real Chrome over CDP.

Implementation notes: `GET /api/results` returns the newest tasks already
projected into that shape and `GET /api/tasks/{id}/result` returns one. Both are
built by `super_agent/application/result.py`, the single translation layer
between storage records and user language — so the console cannot drift from
the records, and the projection is covered by
`tools/verification/verify_user_result.py`.

**`http://127.0.0.1:8000/ops` — the mission-control console** for whoever runs
the system. It surfaces the architecture instead of hiding it behind a chat box:

- **指挥舱 / Cockpit** — a live topology of ingress → Fast Path filter → resident
  Router → Worker slots → Watchdog → BYOM devices, with animated event pulses;
  the dispatch panel (background / synchronous / speculative) and the Router's
  durable global memory transcript side by side.
- **事件流 / Trigger Streams** — the real Trigger feed with two-level filter
  decisions, plus the Thread × subscription fan-out topology.
- **任务矩阵 / Task Matrix** — search, status filters, sorting, saved views, bulk
  selection with batch stop, inline rename, and a detail drawer showing the
  Worker, subscriptions, run/event timeline, async tool runs, and the
  Inline & Spill context entries. Stop, rename, revoke and discard are undoable;
deleting a task (single or bulk) asks for confirmation and refuses live work.
- **运行时实验室 / Runtime Lab** — speculative drafts with approve/discard, BYOM
  device registration and online state, and Watchdog findings.

Six live counters (active Workers, Router memory, subscriptions, Outbox depth,
tool runs, tasks) poll `/api/runtime/summary`. Sound cues are tied to visible
events and can be muted from the header; polling can be paused. The console is
packaged inside the wheel (`super_agent/web/ops.html`), so an installed
package serves the same UI.

The page keeps a local Thread ID. Sending a follow-up on that Thread resumes the
same persisted Worker and context; use `新 Thread` to start an independent task.
Task timelines can also be consumed as Server-Sent Events from
`/api/tasks/{task_id}/stream`.
Persisted Run health is available from `/api/runtime/watchdog` and the
`super-agent watchdog` command.
For direct streaming model output, use `POST /api/chat/stream`; Agent task
execution continues to expose durable progress through the task SSE stream.
External systems can submit signed events through `POST /api/webhooks/{source}`
when `SUPER_AGENT_WEBHOOK_SECRET` is configured.

Reproduce the article §7 experiments offline (synthetic load, no real model):

    python -m tools.benchmarks.ablation --runs 5 --events 300 --out docs/experiments/ablation.json
    python -m tools.benchmarks.router_load --rates 1,10,20,40 --out docs/experiments/router-batching.json

Results and interpretation live in `docs/experiments/README.md`; the full
article-by-article conformance matrix is `docs/article-conformance.md`.

Runtime knobs: `SUPER_AGENT_ROUTER_BATCH_LIMIT` merges backlogged cold-start
triggers into one Router call while draining the Outbox;
`SUPER_AGENT_SPECULATIVE_ENABLED=1` silently pre-computes drafts for
signal-like events (delivered only after promotion);
`SUPER_AGENT_WATCHDOG_INTERVAL_SECONDS=60` runs the model/rule Watchdog
control loop inside the API process;
`SUPER_AGENT_WORKER_MAX_ROUNDS=12` bounds model calls per Worker turn (raise it
for tasks that read many files). `GET /api/runtime/router` exposes
batching and transcript metrics.

Built-in tools are `file.read`, `file.list`, `file.write` and `recall.read`; all
of them stay inside `SUPER_AGENT_WORKSPACE_ROOT`. A queued event that keeps
failing records its attempt count, backs off exponentially, and is parked as
`FAILED` after 5 attempts so one bad event cannot block the queue head forever.
`GET /api/runtime/summary` returns `active_run_details` (which tasks are
actually running, with heartbeat age) and `worker_last_error`;
`GET /api/outbox` returns the queued rows themselves. Artifacts the agent
produces are browsable at `/api/files`, readable at `/api/files/content`, and
an HTML artifact renders directly at `/files/<path>`.
Run `python tools/verification/verify_runtime_regressions.py` for offline
acceptance evidence covering all three behaviours.

For a separate process that can consume pending events after an API restart, run:

    super-agent worker

The API also starts a lightweight in-process recovery consumer, so the default
single-process deployment can recover pending Outbox work automatically.

The worker also renews Run leases, handles cooperative cancellation checkpoints,
and marks abandoned leases as timed out instead of treating a live process as
proof of task health.

Inspect persisted Run health or trigger bounded recovery with:

    super-agent watchdog

Add `--model` to let a model judge Worker progress (INTERVENE/ESCALATE) on top of
the rule layer, and `--loop N` to run the external control loop every N seconds.

Bring your own machine (BYOM): register this device once, keep the printed secret
in an environment variable, then run the stateless endpoint daemon:

    super-agent device register --name my-laptop --capability device.read
    super-agent daemon --server-url http://127.0.0.1:8000 --device-id <id> --workspace . --once

The daemon only executes HMAC-signed, capability-checked commands inside its
workspace; secrets never enter model context. Speculative background work is
available via `POST /api/speculations` and is delivered only after
`POST /api/speculations/{id}/promote`.

Check it:

    super-agent health
    curl http://127.0.0.1:8000/health

Submit a task without opening the web panel:

    super-agent submit "读取 Agent.md 并总结项目目标"

Run checks:

    pytest
    ruff check .
    mypy src

## 自定义工具 / Custom tools

Load your own Python functions, Tool classes, manifest files, directories or
installed entry points as model-callable tools. All of them still pass through
the capability-checked `ToolRegistry` and `ToolPolicy`.

    SUPER_AGENT_TOOL_PATHS=examples/tools super-agent tools
    SUPER_AGENT_TOOL_PATHS=examples/tools super-agent submit "echo hello three times"

See [自定义工具文档](docs/custom-tools.md) for the manifest format, safety
boundaries and the `GET /api/tools` load report.

Runnable acceptance evidence (21 checks, no credentials required):

    python tools/verification/verify_tool_compat.py

## 技能 / Skills

工具是"能做什么"，技能是"知道怎么做"。技能是一个带 `SKILL.md` 的文件夹，
里面写清某类任务的做事方法；它的正文只在你选中时才进入提示词，所以装三十个
技能也不会占用日常上下文。

    skills/release-notes/SKILL.md     # 项目自带示例
    skills/html-page/SKILL.md

选技能：

    # 网页：点输入框里的「✦ 技能」按钮，三个标签页：
    #      已安装        本机 SAA 库里的，能搜 / 选用 / 移除
    #      其他 agent    扫 Claude Code / Codex / Gemini / .agents 等目录，一键导入
    #      Skill 市场    搜 Magpie 的 735+ 技能
    #      也能把文件夹或 .zip/.tar.gz 直接拖到页面上，自动识别 SKILL.md。
    #      输入框打 / 也能弹列表，↑↓ 选、Enter 确认。
    # CLI：
    super-agent submit "写一段 v2.1 更新说明" --skill release-notes
    # API：
    POST /api/tasks/async  {"message": "...", "skills": ["release-notes"]}

装技能：

    super-agent skills market "code review"       # 搜 Magpie 技能市场（本机 735 个）
    super-agent skills install anthropics/skills/webapp-testing --from-magpie
    super-agent skills install ./my-skills        # 从文件夹
    super-agent skills install someone/skills     # 从 GitHub 仓库
    super-agent skills list                       # 看装了什么
    super-agent skills remove <名字>               # 卸载
    super-agent skills path                       # 库在哪个目录
    GET  /api/skills                              # 同上，含加载失败原因

**装完不用重启**：服务每次读取前会检查目录是否有变化，刷新页面就能用——
选择器、任务 payload、模型的 `skill_list` / `skill_read` 都会立即看到。

没选中的技能只以一行简介出现在提示词里，模型觉得对得上可以自己用
`skill_read` 打开 —— 所以"用户没选但正好相关"的情况也能处理。

See [技能文档](docs/skills.md) for the SKILL.md format, bundled resources and
the load-failure contract.

## Real model integration test

The project supports any OpenAI-compatible chat completions endpoint. Copy the example environment file to a local environment file and set the base URL, API key, and model name. The application loads the local environment file automatically for development.

Examples include OpenAI, DeepSeek, OpenRouter, LM Studio, Ollama, and Magpie. Magpie provides a local gateway that unifies hosted subscriptions, provider keys, local models, and routing groups.

Run the configured model check with:

    uv run super-agent model-check

For Magpie, inspect the local gateway and its hosted model catalog with:

    uv run super-agent magpie-check

The task page at `http://127.0.0.1:8000/` provides model selection and a single
input box backed by Magpie. Behind that box a task still enters Trigger ->
Outbox -> Router -> Worker -> Model -> Delivery, rather than calling a chat
endpoint directly — the pipeline is unchanged, only what the user is asked to
look at. Magpie does not currently support runtime
registration of third-party Agent rows; appearing in its Agents page requires an
upstream Magpie adapter and release.

Run the optional real integration test with:

    uv run pytest tests/integration/test_real_model.py -rs

See Agent.md for the development workflow and docs/progress.md for completed and pending work.

