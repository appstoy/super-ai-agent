# 当前进度

## 状态

- 规划阶段：DONE
- 正式功能开发：Phase 6 已开始
- 当前阶段：Phase 8 完成——文章核心机制全部接通（常驻 Router、两级过滤、多对多直通、异步 ReAct、多信道、链式召回、投机执行、BYOM daemon、模型 Watchdog）
- 当前阻塞：无

## 已完成

- 已阅读参考文章和用户提供的规划要求。
- 已建立 Agent.md、AGENTS.md、CLAUDE.md。
- 已完成文章分析、需求拆解、系统架构、ADR、路线图和阶段任务。
- 已完成安全与可靠性专项规范、开源工程规范和规划一致性检查。
- 已完成独立技术审查，并记录残余风险和下一项可执行任务。
- 已修订 MVP 工具边界：早期只做 Mock/只读工具契约，真实 Shell 和端侧执行延后到 Phase 4。
- 已创建 P0-T001 的 Python 包骨架、配置、CLI、健康 API、README、依赖配置和测试文件。
- 已补充 .env.example 和本地配置说明；未引入密钥或真实外部服务配置。
- P0-T001 已完成：Python 3.12.14 环境下安装、pytest、ruff、mypy、CLI health 和 version 验证通过。
- P0-T002 已完成：统一错误分类、关联上下文、结构化 JSON 日志和敏感字段脱敏测试通过。
- P0-T003 已完成：Alembic SQLite 迁移 upgrade/downgrade、Outbox 写入/领取/重试/去重测试通过。
- 已创建 Phase 0 质量门禁文件：GitHub Actions、CONTRIBUTING.md、SECURITY.md 和 Makefile。
- Phase 0 已完成：P0-T001 至 P0-T005 全部 DONE；迁移、测试、lint、类型检查和真实健康 API 进程验收通过。
- P1-T001 已完成：六类领域实体、状态机、版本冲突和序列化契约通过 24 个测试、ruff 和 mypy。
- P1-T002 已完成：Trigger schema、时区/payload 限制、幂等去重和 Outbox 同事务写入通过全量 26 个测试。
- P1-T003 已完成：ModelGateway 契约、Mock 响应脚本、工具调用、usage 和错误重放测试通过。
- P1-T004 已完成：Router Fast Path、Mock 模型决策和 Task/Worker/Delivery 结果测试通过。
- P1-T006 已完成：Tool Registry、能力拒绝、参数校验、输出截断和 Mock/只读工具测试通过。
- Phase 1 已完成：P1-T001 至 P1-T006 全部 DONE；36 个测试、ruff、mypy、迁移回滚/升级和 Router E2E 通过。
- P2-T001 已完成：WorkerContext 隔离快照、WorkerRuntime 生命周期和可重放状态事件通过全量 39 个测试。
- P2-T002 已完成：Thread-Worker 多对多订阅、直通、过期/取消/续订和 event_id 幂等通过全量 42 个测试。
- P2-T003 已完成：AsyncToolExecutor 的 Started、完成、stop、超时、重试和终态幂等通过专项测试。
- P2-T004 已完成：Scheduler 优先级、容量、mutex、heartbeat 和过期回收通过全量 48 个测试。
- Phase 2 已完成：P2-T001 至 P2-T005 全部 DONE；49 个测试、ruff、mypy 和 Phase 2 E2E 通过。
- P3-T001 已完成：Inline/Spill Context Service、预算、当前 Trigger 优先、权限排除和 manifest 通过全量 52 个测试。
- P3-T002 已完成：ReferenceGraph 链式召回、租户/权限边界、深度/节点预算和循环审计通过全量 55 个测试。
- P3-T003 已完成：CLI/Webhook Delivery 格式、审批门槛、失败重试和幂等通过全量 58 个测试。
- P3-T004 已完成：ReplayRunner、Context manifest、token 指标和确定性 JSON 输出通过全量 59 个测试。
- Phase 3 已完成：P3-T001 至 P3-T005 全部 DONE；60 个测试、ruff、mypy 和 Phase 3 E2E 通过。
- P4-T001 已完成：ToolPolicy 风险/能力/审批边界和审计记录通过全量 63 个测试。
- P4-T002 已完成：ReadFileTool/AllowlistedCommandTool 的路径、白名单、审批、超时和审计通过全量 65 个测试。
- P4-T003 已完成：DeviceRegistry 设备注册、HMAC 签名、租约、撤销、过期和重放防护通过全量 68 个测试。
- P4-T004 已完成：EventFilter Fast Path、规则版本和 SpeculativePolicy 预算/TTL/无副作用通过全量 70 个测试。
- Phase 4 已完成：P4-T001 至 P4-T005 全部 DONE；71 个测试、ruff、mypy 和安全 E2E 通过。
- P5-T001 已完成：规则 Watchdog 健康、过期租约、停滞 Run 和重复失败检查通过全量 73 个测试。
- P5-T002 已完成：RecoveryManager 错误分类、幂等、限次、pause 和人工 stop 通过全量 75 个测试。
- P5-T003 已完成：Router batch 基准、直通计数、决定延迟和 prompt token 估算通过全量 77 个测试。
- P5-T004 已完成：Telemetry span/trace、计数器、成本预算和超预算 approval_required 通过全量 79 个测试。
- Phase 5 已完成：P5-T001 至 P5-T005 全部 DONE；80 个测试、ruff、mypy 和 Phase 5 chaos E2E 通过。
- P6-T002 已完成：GitHub/IM/邮件模拟适配器、签名、Thread/身份映射、限流和 feature flag 通过全量 86 个测试。
- P6-T003 已完成：任务查询、停止/恢复 API、安全控制台和数据库配置注入通过全量 87 个测试。
- 已加入 OpenAI-compatible 真实模型 Gateway、model-check 命令、可选真实集成测试和 .env 自动加载。
- 已加入 Magpie 一等文本 ModelGateway：支持 OpenAI Chat Completions、`/api/hello` 身份检查、`/v1/models` 托管模型目录，以及订阅模型、provider key、本地 Ollama/LM Studio 模型和 routing group。
- 已记录真实服务配置与 Magpie 接口：docs/real-services.md；密钥只通过临时环境变量注入，未写入仓库。
- Magpie 真实验收已通过：`magpie-check` 发现 19 个可用模型；通过 Magpie 托管模型完成 `model-check`，返回 `model-ok`；`tests/integration/test_real_model.py` 在凭证存在时通过。
- 已将首页升级为可用控制台：模型选择、Magpie 连接状态、文本聊天、token 用量和任务列表。新增 `/api/config`、`/api/models`、`/api/chat` 与 `/api/magpie/integration`。
- 已新增真正的端到端 `AgentOrchestrator`：Web 任务入口经过 TriggerIngress、Router、Task/Worker 持久化、Worker 上下文、ModelGateway 和 Worker Event/Run 记录；页面任务按钮现在调用 `/api/tasks`，不再只是直接聊天。
- 已新增 `runs`、`worker_events`、`subscriptions` 迁移与 ORM 模型，增加全链路编排集成测试；真实 Magpie 模型已验证 `TASK` 路由并完成 Worker 任务。
- 已增加任务详情 API `GET /api/tasks/{task_id}`，返回 Task、Worker、Run、Worker Event 和 Worker Context 时间线，页面可展开查看执行轨迹。
- Phase 7 已开始：统一运行时编排已经可用；同步、后台、CLI、Webhook 和独立消费者都经过 Outbox -> Router -> Worker 闭环。Run lease、Watchdog、SSE 和有界恢复已接入，但模型强制中断、告警运营和多实例协调仍未完成，不能宣称 Super-agent 全部完成。
- 已新增 `OutboxProcessor` 和 `AgentOrchestrator.from_outbox_payload`，开始把 Outbox 事件重新接入统一运行时；工具调用、上下文回注、Run/Worker Event 持久化和任务详情查询已可验证。
- 已把最终结果接入可选 `DeliveryService`；编排集成测试验证 CLI Delivery 发送和幂等流程。
- 新增 `submit_via_outbox`：任务可以先写入 Outbox，再由处理器 claim、分发并进入同一 AgentOrchestrator，降低直接同步调用与事件链路之间的差异。
- 新增 `/api/tasks/async` 和 `/api/tasks/status`：Trigger/Outbox 先提交后由 `BackgroundAgentRunner` 在线程池执行，页面可轮询持久化状态；已用确定性 Fake Gateway 完成控制台 E2E 验证。
- 控制台现在默认注册受 `SUPER_AGENT_WORKSPACE_ROOT` 限制的只读 `file.read` 工具，模型工具调用会回写 Worker Context；写文件、Shell 和网络能力仍默认关闭。
- 修复 Outbox 入口按目标 dedupe key claim，避免历史待处理事件抢占当前 HTTP 请求；Router 对非契约自然语言输出回退为任务，避免普通模型把可执行请求提前结束。
- 同一 Thread 的活动订阅现在从持久化 Context 恢复并直达原 Worker；模型工具名会转换为 OpenAI 允许的安全标识符，真实 Magpie 文件读取与总结任务已跑通。
- 新增 `DurableOutboxWorker` 和 `super-agent worker` 独立消费者；Outbox 支持过期 claim lease 回收，进程重启后可继续处理已提交但未完成的 Trigger。
- 当前验证基线：`114 passed, 1 skipped`；`ruff check .`、`mypy src tools`、Alembic downgrade/upgrade 均通过。真实 Magpie 已验证同步任务、后台任务、同 Thread 续聊和受限文件读取。
- P7-T003 已进入 IN_PROGRESS：Run 增加 lease/heartbeat/取消请求/过期恢复，Worker 在每轮模型调用前协作检查；停止 API 会向活动 Run 写入 `cancel_requested`。
- 最新真实验收：`/api/tasks/async` 经 Magpie 返回 `succeeded`，状态接口可见 `heartbeat_at` 且完成后 lease 清空；`super-agent worker --once` 在无待处理事件时正常返回 `processed: false`。
- CLI 新增 `super-agent submit`，与 Web 入口共享同一 Trigger/Outbox/Router/Worker 编排；离线 Mock 命令测试已通过。
- SQLite 本地运行启用 WAL、30 秒 busy timeout，并让后台执行在模型调用前提交运行状态；Web 服务和 CLI/worker 可并行访问默认数据库。
- `RunRecoveryService` 已接入独立 Outbox worker：失败/超时 Run 在 attempt 未达上限时自动重入原 Trigger，超过上限保持终态。
- 新增 `PersistentRunWatchdog` 与 `super-agent watchdog`，可检查持久化 Run 停滞/重复失败并执行有界恢复；本地健康检查已返回 `healthy`。
- 任务详情新增 `/api/tasks/{task_id}/stream` SSE 进度流，控制台会在运行任务时实时接收 Worker 事件并自动关闭终态连接。
- 控制台连接卡片新增 `/api/runtime/watchdog` 健康状态，运行停滞或重复失败会显示为 `attention`。
- OpenAI-compatible/Magpie Gateway 新增 SSE 文本流接口 `/api/chat/stream`，支持 token delta、done 和 error 事件。
- 最新真实验收：Magpie `/api/chat/stream` 返回 token、done 事件，服务端保持 `http://127.0.0.1:8000/` 在线。
- 新增 `/api/webhooks/{source}`，可将外部 HTTP 事件签名验证后送入同一 Trigger/Outbox/Router/Worker 路径。
- 服务启动时自动运行进程内 `DurableOutboxWorker`，重启后的待处理事件无需手动触发；恢复线程对凭证/供应商错误保持重试。
- Outbox claim 改为带 attempts/status 条件的原子更新，并增加并发消费者回归测试，避免多实例重复领取同一事件。
- 已确认 Magpie 不提供第三方 Agent 的运行时注册 API；若要在 Magpie Agents 页面出现 Super AI Agent，需要向 Magpie 的 `internal/agent` 添加适配器并等待上游发布。本项目当前采用官方支持的 OpenAI-compatible Gateway 集成方式。
- P6-T004 已完成：许可证、CHANGELOG、Docker/Compose、GitHub 模板、Dependabot 和发布清单通过结构检查与全量测试。
- P6-T005 已完成：wheel/源码包构建、Alembic 回滚/升级、CLI/API、控制台、文档和发布文件验收通过；Docker daemon 未运行，镜像构建留作发布前人工检查。

- Phase 8 已完成：Router 常驻会话（router_messages 持久化 transcript + 批处理 + 窗口 Spill）、EventFilter 接入主链路（IGNORE 零 LLM）、订阅多对多 fan-out（seq 保序、失败隔离）。
- Phase 8 已完成：异步 ReAct Loop——task.start/stop/status 模型工具，tool_runs 持久化，tool.completed 经 Outbox 回注同 Worker 续跑；续跑事件不再二次入箱；恢复服务只重排最新失败 attempt。
- Phase 8 已完成：多信道投递（IM ≤100 词、GitHub/EMAIL 结构化 markdown、按 source 映射、缺适配器回退 CLI）；recall.read 链式召回工具在 API/CLI 注册。
- Phase 8 已完成：BYOM——devices 表持久化注册（DB 只存 secret hash）、HMAC 签名命令、端侧 daemon（urllib、白名单、路径穿越拒绝）、/api/devices/* 路由、CLI daemon/device register；真实进程冒烟通过（注册→入队→daemon 领取执行→回传读取内容）。
- Phase 8 已完成：WatchdogSession 模型控制回路（INTERVENE 有界恢复 / ESCALATE 人工审查 / 模型失败降级规则层 / ask 自然语言问询），CLI watchdog --model --loop。
- Phase 8 已完成：投机执行 SpeculativeRunner + /api/speculations 与 promote 端点（无副作用、TTL、人工批准才投递），E2E 通过。
- 迁移 0006 幂等化（可从中断的部分升级恢复），upgrade/downgrade/upgrade 验证通过。
- Phase 8 续：§7 实验可复现——tools/benchmarks/ablation.py（四方案消融，5 轮×300 事件）与 router_load.py（1→40/min 批处理压测），结果固化在 docs/experiments/（含 README 解读）。定性结论与文章一致：Super-agent p95 最优、去直通 Router 开销 +22%、中性单会话长尾；40/min 时 batch 1.89、单决定推理 2.5s→1.64s。
- Phase 8 续：批处理接入运行时——DurableOutboxWorker.process_batch 排空积压并合并冷启动 Router 调用（SUPER_AGENT_ROUTER_BATCH_LIMIT）；orchestrator.process_batch 支持批量分派；GET /api/runtime/router 暴露批处理/transcript 指标。
- Phase 8 续：自动投机钩子（SUPER_AGENT_SPECULATIVE_ENABLED）——信号关键词事件自动静默预计算，不阻塞主事件路径；服务端 Watchdog 控制回路线程（SUPER_AGENT_WATCHDOG_INTERVAL_SECONDS）。
- 包补齐 py.typed 标记，mypy src tools 全量通过。
- 当前验证基线：236 passed, 1 skipped；ruff check src tests tools、mypy src tools 全绿；迁移 0006 回滚/升级通过。
- 新增 docs/article-conformance.md：文章逐节符合性矩阵与已知边界。

- 前端重做完成：新增 Super-agent 指挥舱控制台（src/super_agent/web/index.html，单一来源并打包进 wheel），四个分区对应文章架构——指挥舱（实时双层拓扑 + 全双工指令台 + Router 全局记忆）、事件流（两级过滤决策 + Thread 订阅 fan-out）、任务矩阵（搜索/筛选/排序/保存视图/批量停止/内联重命名/详情抽屉，破坏性操作可撤销）、运行时实验室（投机批准丢弃 + BYOM 设备 + Watchdog 巡检）。
- 新增可观测性端点：/api/runtime/summary、/api/events、/api/threads、/api/router/transcript、/api/outbox、PATCH /api/tasks/{id}/title、POST /api/speculations/{id}/discard；/tasks 与任务详情补充信道、thread_ids、订阅与 tool_runs。
- 控制台已通过真实浏览器验收：真实模型后台任务全链路（受理→Router→Worker→file.read 工具→上下文回写→完成提示）、投机批准/丢弃/撤销、设备注册、批量停止与撤销、重命名与撤销、保存视图应用/删除/撤销、移动端 390px 无溢出、桌面与移动零运行时错误。
- 控制台生命周期修正：stop 只作用于有活跃 Run 的任务，已完成任务保持终态并返回 changed=false；resume 恢复任务真实结果而非一律置 running；前端批量停止只乐观取消运行中任务，撤销恢复各自原状态。修正了此前被错误复活的历史任务数据。
- Worker 轮次上限从 4 提升为可配置 SUPER_AGENT_WORKER_MAX_ROUNDS（默认 12），并贯通 API/CLI/后台/Outbox 全部构造路径；此前因上限过紧失败的多轮文件分析任务现已真实跑通。
- 修复共享状态缺陷：投机 runner 由模块级全局改为 app.state 作用域（避免跨实例泄漏）；设备注册测试内存库改用 StaticPool；控制台加载对空文件做兜底。
- 控制台补齐完整增删改：任务删除（DELETE /api/tasks/{id}，级联清理 Run/WorkerEvent/WorkerContext/ToolRun/Delivery/Subscription/Worker，运行中任务拒绝删除）、批量删除、设备撤销与恢复（POST /api/devices/{id}/revoke|restore，可撤销）。
- 新增 tests/e2e/test_console_api.py（8 例，含生命周期、删除级联、设备撤销回归）；当前基线 244 passed, 1 skipped；ruff、mypy src tools 全绿；wheel 已验证包含控制台与 py.typed。
- 浏览器实测通过：任务删除 6→5、设备撤销后 revoked 标记与恢复、批量选择/停止/撤销、多轮文件分析任务成功、桌面与 390px 移动端零运行时错误。
- 控制台中文化改版（面向中文用户的可读性）：拓扑图节点改为「消息/代码/网页 → ① 先筛掉噪音 → ② 调度中心 → ③ 执行分身 → 健康巡检」，加 5 条图例说明每个节点的作用；指标带改为「正在执行/调度记忆/对话绑定/待处理事件/后台工具/任务总数」并附一句人话解释；三种执行方式改为可点选的卡片（后台执行/同步执行/投机预演），选中后下方实时说明「是做什么的、什么时候用、结果在哪」；状态、过滤判定、来源、事件类型、上下文角色、巡检发现全部中文化；Router 记忆卡把 `source=... payload_ref=...` 原始串转为「【网页】消息内容」「做成任务：xxx」的可读形式；区块标题与导航改为纯中文（主控台/收件箱/任务/进阶），中文字号加大、取消强制大写。
- 修复真实缺陷：`recall.read` 工具在共享 registry 上重复注册导致同步执行接口 500（改为幂等注册 + 回归测试）。

## 下一步

1. 发布前在 Docker daemon 可用环境执行镜像构建和扫描。
2. 接入真实 PostgreSQL、模型供应商和 GitHub/IM/邮件凭据前，按安全文档完成部署评审。
3. 后续功能开发从新的任务文件开始，不回退已完成阶段状态。

## 规划审查记录

- 已将文章事实、工程推断和待验证问题分开。
- 已选择模块化单体作为 MVP，避免在没有负载证据时拆分微服务。
- 已把幂等、权限、审计、恢复和上下文预算列为核心约束，而不是最后补丁。
- 待在实现前由维护者确认模型供应商、许可证和首个目标渠道。

