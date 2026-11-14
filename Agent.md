# Super AI Agent 项目指挥中心

本项目把参考文章中的 Super-agent 思想实现为一个可长期运行、事件驱动、可扩展的开源 Agent 平台。当前规划已完成，后续开发和真实服务验证按任务文档继续。

## 目标与原则

- 让用户通过多个工作信道与同一个逻辑 Agent 协作，同时允许多个任务并行推进。
- 以持久化事件、Router/Worker 双层架构、线程订阅、异步执行和 Watchdog 支撑连续、高效、主动、持久四个特征。
- 先交付可验证的单机 MVP，再按阶段增加多信道、端侧执行、多 Agent、可靠性和生态能力。
- 所有跨模块交互使用版本化接口和显式状态；安全、可观测性、幂等和恢复能力从第一版纳入设计。
- 文章是设计参考，不是必须原样复制的实现规格；事实、推断和待验证问题分别记录。

## 当前技术基线

规划基线为 Python 3.12、FastAPI、Pydantic v2、SQLAlchemy 2、Alembic、PostgreSQL、pytest、OpenTelemetry 和 Docker Compose。模型通过统一的 ModelGateway 接口接入 OpenAI-compatible 或其他供应商；事件总线、任务执行器、渠道适配器和记忆后端都必须可替换。

“当前技术基线”是规划选择，不代表代码已经存在。需要改变基础技术选型时，先更新对应 ADR。

## 文档入口

- 文章事实、推断和批判性分析：docs/article-analysis.md
- 需求、优先级和验收范围：docs/requirements.md
- 模块、接口、数据流和部署拓扑：docs/architecture.md
- 安全、权限、可靠性和威胁边界：docs/security.md
- 开源发布、贡献者和用户体验规范：docs/opensource.md
- 规划技术审查和残余风险：docs/review.md
- 真实服务凭证、模型测试和 Magpie 边界：docs/real-services.md
- 阶段目标和里程碑：docs/roadmap.md
- 技术决策记录：docs/decisions/
- 可执行任务：docs/tasks/
- 当前运行时收敛任务：docs/tasks/phase-7-runtime.md
- 任务索引和阶段入口：docs/tasks/README.md
- 当前状态和阻塞项：docs/progress.md

## AI 编程 Agent 工作流

1. 先读本文件、docs/progress.md 和目标任务文件。
2. 按任务依赖加载相关架构章节、接口契约和 ADR；不要为普通任务读取全部历史材料。
3. 先确认数据形状、状态转换、输入输出和验收标准，再修改代码。
4. 一次只实现一个可验证任务；保持变更小而完整，不跨阶段偷渡功能。
5. 运行任务要求的单元、集成、类型、静态和安全检查；失败时保持任务为 IN_PROGRESS 或 BLOCKED。
6. 完成后更新任务状态、docs/progress.md 以及受影响的架构或 ADR 文档。
7. 若发现规划与现实冲突，先记录影响和候选方案；涉及公开接口、数据模型、权限边界或部署拓扑的变化必须新增或更新 ADR。

任务状态只使用 TODO、IN_PROGRESS、BLOCKED、DONE。每项任务必须有唯一 ID、前置依赖、涉及文件、测试和可观察的验收条件。

## 工程约束

- 领域层不得直接依赖 FastAPI、具体数据库、具体模型供应商或具体渠道。
- 所有外部事件必须有稳定的 event_id、thread_id、source、occurred_at、received_at 和幂等键。
- Worker 不直接向外部渠道发消息；统一通过 Router/Delivery 层输出。
- 工具调用必须经过能力策略、资源限制、超时、审计和可取消机制。
- 模型上下文默认遵循 Inline/Spill；不得把完整历史、凭据或无关事件无界注入提示词。
- 不把“进程仍存活”当作任务健康；Watchdog 必须检查进展、停滞、重试和偏航。
- 默认拒绝高风险操作；密钥通过引用或端侧代理使用，禁止进入模型上下文和普通日志。
- 所有异步操作必须可查询、可重试、可停止、可恢复，并且结果可追溯到触发事件。
- 阶段门禁：规划阶段只能维护规划文档；规划完成后允许按 docs/tasks/ 中的任务实现，不能跳过前置依赖或跨阶段偷渡功能。

## 兼容入口

AGENTS.md 和 CLAUDE.md 只作为工具兼容入口，引用本文件和相关专项文档，不维护第二套规则。

