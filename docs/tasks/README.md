# 任务索引

任务按路线阶段拆分，每个任务适合一次聚焦的 AI 编程会话。先完成前置任务，再执行当前阶段的集成验收。

| 文件 | 阶段 | 重点 |
| --- | --- | --- |
| phase-0.md | 工程基线 | 包结构、配置、数据库、CI |
| phase-1.md | 最小 Agent | 领域状态、事件入口、模型端口、最小工具 |
| phase-2.md | 并发执行 | Router/Worker、订阅直通、异步 Run |
| phase-3.md | 上下文与渠道 | Inline/Spill、链式召回、Delivery、回放 |
| phase-4.md | 安全与端侧 | 能力策略、受限执行、daemon、主动过滤 |
| phase-5.md | 可靠性 | Watchdog、恢复、批处理、观测和成本 |
| phase-6.md | 开源生态 | SDK、外部渠道、Web UI、发布基础设施 |
| phase-7-runtime.md | 统一运行时 | AgentOrchestrator、持久化运行、异步恢复和真实工具闭环 |

任务状态只使用 TODO、IN_PROGRESS、BLOCKED、DONE。完成任务时必须更新 docs/progress.md；改变接口、数据模型、权限或部署方式时必须更新 ADR。

