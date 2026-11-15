# 文章符合性矩阵

对照 `super-agent-article-fulltext.md`（《Super-agent 设计探索》）逐节核对实现。

> 该文为 DeepSeek 产品部于 2026-10-31 发布的第三方文章，著作权归原作者及原发布方；原文完整收录于仓库根目录作对照用，本文档只记录本项目自身的实现核对结论。
验证基线：`236 passed, 1 skipped`（真实模型集成测试需凭证），`ruff check src tests tools`、
`mypy src tools`、Alembic `0006` upgrade/downgrade/upgrade 均通过；BYOM 链路已做真实进程冒烟；
§7 两个实验已可复现运行，结果见 `docs/experiments/`。

| 文章章节 | 设计要点 | 实现位置 | 验证 |
|---|---|---|---|
| §2 双层架构 | Router 只分派不执行；Worker 隔离上下文、挂完整工具 | `application/router.py`、`application/orchestrator.py`、`application/worker.py` | `tests/e2e/test_phase1.py`、`test_router_fanout.py` |
| §2.1 Router 常驻会话 | 跨任务全局记忆；transcript 持久化；Inline & Spill 窗口预算 | `application/router_session.py`（`router_messages` 表） | `tests/unit/test_router_session.py` |
| §2.1 批处理 | 积压 Trigger 合并为一次 LLM 调用，暴露 batch 指标 | `router_session.route_batch` + `RouterBatchMetrics` | 同上（5 条积压 1 次调用） |
| §2.2 Worker 层 | Worker 不直接对外回复，结论经 Router/投递层统一输出 | `orchestrator._execute_worker` + `delivery/service.py` | `tests/unit/test_delivery_channels.py` |
| §3.1 两级过滤 | Fast Path 程序策略先筛噪，Slow Path 语义层判断 | `policies/events.py` 接入 `orchestrator.process`；IGNORE 零 LLM 调用 | `test_router_fanout.py`、`test_policies.py` |
| §3.2 订阅与直通 | Thread↔Worker 多对多；后续事件直通全部订阅者；失效订阅不阻塞其它 | `orchestrator._active_subscriptions/_fan_out_subscribed`（`subscriptions.seq` 保序） | `test_router_fanout.py`（9 例） |
| §3.3 Inline & Spill | 上下文预算、截断续读 | `context/service.py` | `tests/unit/test_context.py` |
| §3.3 链式召回 | 对象引用图、逐跳按需召回、租户边界 | `context/recall.py` + `tools/recall.py`（`recall.read` 模型工具，API/CLI 已注册） | `test_recall_tool.py` |
| §3.4 投机执行 | 无副作用、预算/TTL 约束、后台静默、人工 promote 才投递 | `policies/speculative.py` + `speculative_runner.py`；`POST /api/speculations`、`/promote` | `test_speculative_runner.py`、`test_speculative_api.py` |
| §4.1 异步 ReAct | `start()` 立即返回 Started；完成结果作为新事件回注同 Worker；`stop()` 取消 | `application/async_tools.py`（`tool_runs` 表 + `tool.completed` Outbox 事件）；模型工具 `task.start/stop/status` | `test_async_tools.py`、`test_full_duplex.py` |
| §4.2 多信道交互 | IM ≤100 词；GitHub/邮件结构化 markdown；按来源信道投递、缺适配器回退 CLI | `delivery/service.py`（`channel_for_source`）、`ports/channel.py` | `test_delivery_channels.py` |
| §5.1 计算与状态分离 | 无状态计算 + 持久化会话状态；重启续跑 | `storage/`、`application/background.py`（DurableOutboxWorker）、Run lease | `test_durable_outbox_worker.py`、`test_run_leases.py` |
| §5.2 计算与环境分离 / BYOM | 端侧轻量 daemon 只执行命令；HMAC 签名、防重放、能力白名单、路径穿越拒绝；secret 不进模型上下文 | `daemon/registry.py`、`daemon/service.py`、`daemon/agent.py`、`tools/device.py`；`/api/devices/*`；CLI `daemon` / `device register` | `test_device_registry.py`、`test_byom_daemon.py` + 真实进程冒烟 |
| §6 Watchdog | 外部控制回路；规则层 + 模型判断进展/偏离；INTERVENE 有界恢复、ESCALATE 人工审查；自然语言问询 | `watchdog/runtime.py` + `watchdog/session.py`；CLI `watchdog --model --loop` | `test_watchdog_session.py`、`tests/chaos/` |
| §7.1 消融实验 | 四方案对比：双层+直通 vs 去直通 vs 两种单会话提示 | `tools/benchmarks/ablation.py`（可复现，`--runs 5 --events 300`） | `docs/experiments/ablation.json`：Super-agent p95 最优，去直通 Router 开销 +22%，中性单会话长尾显著 |
| §7.2 Router 批处理 | 到达速率提升时积压自动合并、单决定推理时间下降 | `tools/benchmarks/router_load.py`（延迟模拟模型 + Mock Worker） | `docs/experiments/router-batching.json`：40/min 时 batch 1.89、单决定 2.5s→1.64s |
| §2.1 批处理运行时 | 积压事件在 Outbox 排空时合并为一次 Router 调用 | `background.DurableOutboxWorker.process_batch` + `orchestrator.process_batch`；`SUPER_AGENT_ROUTER_BATCH_LIMIT` | `tests/e2e/test_batch_runtime.py` |
| §3.4 自动投机 | 信号类事件自动触发静默后台预计算 | `DurableOutboxWorker._maybe_speculate` + `SUPER_AGENT_SPECULATIVE_ENABLED` | `test_batch_runtime.py`（关键词触发、噪音不触发） |
| §6 服务端控制回路 | API 进程内独立 Watchdog 线程定时 tick | `api.watchdog_loop` + `SUPER_AGENT_WATCHDOG_INTERVAL_SECONDS`；`GET /api/runtime/router` 暴露批处理指标 | `test_batch_runtime.py`（指标端点） |

## 已知边界（与文章 §8 开放问题一致）

- Router 水平切分、跨实例 A2A、长程跨月记忆：未实现，属文章明确的开放问题。
- 设备命令队列不持久化：重启后待执行命令丢弃，设备重连重新领取（docstring 已声明取舍）。
- 投机结果存进程内存：多实例部署需共享存储，当前单进程控制台场景够用。
- `max_rounds` 语义为单轮 Worker 的模型调用上限；末轮仍请求工具时确定性失败，防止无限续跑。