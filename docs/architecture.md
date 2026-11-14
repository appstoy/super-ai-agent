# 系统架构

## 架构目标

系统以事件为主线，以 PostgreSQL 作为持久化事实源，以 Router/Worker 双层职责实现全局连续性和并发执行。MVP 采用模块化单体：模块边界、事件契约和状态机先稳定，进程拆分延后到有性能证据时进行。

## 逻辑组件

| 组件 | 职责 | 不负责 |
| --- | --- | --- |
| Channel Adapter | 接收外部消息、验证签名、转换为 Trigger；发送 Delivery | 任务决策、业务状态 |
| Ingress | 鉴权、大小限制、幂等、规范化和 Outbox 写入 | LLM 语义判断 |
| Event Bus | 投递 Trigger、Run 完成事件和内部命令 | 持久化业务真相 |
| Router | 全局上下文、过滤候选、创建/分派/停止 Worker、汇总对外回复 | 执行具体工具 |
| Subscription Manager | 管理 Thread-Worker 多对多订阅和直通 | 语义规划 |
| Worker Runtime | 隔离上下文中规划和执行单项任务，汇报状态 | 直接对外发送 |
| Task Scheduler | 并发限制、优先级、租约、重试和恢复 | 决定业务意图 |
| Model Gateway | 统一模型请求、工具调用、流式输出、预算和重试 | 保存业务状态 |
| Tool Registry/Executor | 注册能力、策略检查、超时、沙箱和审计 | 自行绕过授权 |
| Context Service | Inline/Spill、摘要、引用图和按需召回 | 修改任务状态 |
| State Store | Task、Run、Trigger、Worker、Thread、Subscription、审计持久化 | 调度执行 |
| Delivery Service | 渠道格式化、审批门槛、重试和发送 | 改写领域事实 |
| Watchdog | 周期健康检查、停滞识别、恢复建议和受限修复 | 获得全能管理员权限 |
| Endpoint Daemon（后续） | 在用户环境执行白名单能力，返回结果，不保存会话 | 接收任意云端命令 |

## Skills 与 Plugins 扩展

文章没有把 Skills 或 Plugins 作为独立模块展开，因此本项目把它们定义为工程扩展，而不是隐含在提示词里的任意能力：

- Skill 是版本化的领域工作流包，包含触发条件、提示词片段、输入/输出 schema、所需工具和验收器。Skill 不能绕过 Tool Policy。
- Plugin 是实现一个或多个端口的可加载适配器，例如 ModelGateway、ChannelAdapter、Tool 或 EventBus。插件声明版本、能力和依赖。
- 注册、启用、停用和升级都由配置和审计控制；插件加载失败必须隔离，不得阻塞核心任务。
- 插件 SDK 只暴露稳定端口，不暴露数据库会话、内部密钥或任意管理员接口；详细公开 SDK 放在 Phase 6。

## 数据模型

- Trigger：id、schema_version、source、tenant_id、thread_id、actor、payload_ref、occurred_at、received_at、dedupe_key、filter_decision。
- Thread：渠道中的连续事件流；保存来源标识、权限边界、状态和最后活动时间。
- Task：用户可理解的工作目标；关联一个或多个 Thread、Worker、Run 和 Artifact。
- Worker：执行上下文和能力集合；状态为 STARTING、RUNNING、WAITING、SUCCEEDED、FAILED、STOPPED。
- Subscription：thread_id、worker_id、过滤规则、状态、有效期和版本。
- Run：一次可恢复的执行尝试；包含触发事件、租约、预算、状态和错误分类。
- ToolCall：一次工具执行；包含参数摘要、能力、执行环境、超时、批准和结果引用。
- Artifact：日志、补丁、报告、草稿等大对象；正文存对象存储或文件系统，数据库保存元数据和校验值。
- Delivery：对外输出；包含目标渠道、幂等键、审批状态、格式版本和发送结果。
- TaskResult：**面向用户的投影，不是新的持久实体。** 它把一次 Task 的
  Task/Worker/Run/Event/ToolRun/Delivery/Artifact 折叠成一个对象：状态（人话）、
  结论（模型回答的首句，不是整篇报告）、产物（带大小和预览 URL）、步骤（去重后的
  时间线）。由 `super_agent/application/result.py` 单一实现，`GET
  /api/tasks/{id}/result` 和 `GET /api/results` 都走它。放在这里的原则：**运行时
  词汇（Worker/Run/Trigger/output_ref）只服务运维，不进入用户界面**，所以投影必须
  有唯一实现，否则界面会和存储事实漂移。
- AuditRecord：不可变的安全与状态审计记录。

所有实体使用 UUID/ULID；时间统一 UTC；外部输入和公共事件契约带 schema_version。删除和保留策略按租户/用户配置，默认不把密钥写入任何实体。

## 关键接口契约

### ModelGateway

输入为系统消息、消息列表、工具描述、模型选择和预算；输出为文本、结构化决定、工具调用、usage、provider_request_id 和错误分类。实现不得让上层依赖供应商 SDK 类型。

### ChannelAdapter

ingest(raw_request) -> NormalizedTrigger；deliver(DeliveryRequest) -> DeliveryResult。适配器必须实现签名验证、来源身份映射、能力声明、最大消息大小和幂等键提取。

### Tool

describe() -> ToolSpec；start(ToolInput, ExecutionContext) -> RunHandle；stop(run_id) -> StopResult；resume(run_id) -> RunResult。工具必须声明副作用、所需能力、资源上限和可取消性。

### EventBus

publish(envelope)、subscribe(topic, consumer_group)、ack(delivery_id)。MVP 可由数据库 Outbox 和轮询实现，但上层只依赖该接口。

## 状态与数据流

\`\`\`mermaid
flowchart LR
  C[CLI / Webhook / Channel] --> I[Ingress]
  I --> O[(Outbox + State Store)]
  O --> B[Event Bus]
  B --> F[Fast Filter]
  F --> R[Router]
  R --> S[Subscription Manager]
  S --> W[Worker Runtime]
  W --> M[Model Gateway]
  W --> T[Tool Executor]
  T --> E[Execution Environment]
  W --> O
  R --> D[Delivery Service]
  D --> C
  H[Watchdog] --> O
  H --> R
\`\`\`

### 新任务流程

\`\`\`mermaid
sequenceDiagram
  participant U as User/Channel
  participant I as Ingress
  participant R as Router
  participant S as Subscription
  participant W as Worker
  participant T as Tool
  participant D as Delivery
  U->>I: external event
  I->>I: auth, dedupe, normalize
  I->>R: Trigger
  R->>R: fast filter / semantic decision
  R->>W: create Task + Worker
  R->>S: subscribe Thread -> Worker
  W->>T: start async tool
  T-->>W: Started
  W-->>R: progress event
  T-->>W: completion event
  W-->>R: result / approval request
  R->>D: Delivery
  D-->>U: channel-specific response
\`\`\`

### 后续 Thread 事件

\`\`\`mermaid
flowchart TD
  E[Incoming Trigger] --> D{Active subscription?}
  D -- yes --> W[Direct Worker delivery]
  D -- no --> R[Router decision]
  W --> Q[Worker idempotency check]
  Q --> X[Resume task]
  W --> R2[Global observation event]
\`\`\`

## 上下文策略

Context Service 按预算构建窗口：先放当前 Trigger、Task/Thread/Worker ID 和短摘要，再按局部性加入相邻事件和高相关 Artifact；超过预算的内容只保留引用、截断摘要和读取工具。每次模型调用记录 context manifest，包含对象 ID、来源、估计 token、spill 原因和召回链路。模型不得直接读取未授权对象。

## 并发、幂等与恢复

- Trigger 入库使用 dedupe_key 唯一约束；消费使用至少一次语义。
- Router 对同一全局租约串行处理决定，但可批量合并等待事件；Worker 之间默认并行。
- Run 使用租约和心跳；租约过期由 Scheduler 收回并按错误分类重试或标记失败。
- 工具完成事件、Delivery 和 Watchdog 修复动作都使用幂等键。
- 状态写入采用乐观版本号；冲突时重新读取并重放领域命令，禁止静默覆盖。

## 安全边界

所有外部内容都视为不可信数据。系统分离指令、事件正文和工具结果；提示词注入不能改变能力策略。Tool Policy 在执行前检查身份、租户、资源、路径、命令、网络和审批。端侧 daemon 只接受短期签名命令和明确能力，不接受任意 shell 字符串。审计记录不可由 Agent 删除。

## 部署演进

Phase 1 使用 API、Worker、Scheduler 和 Watchdog 的模块化单体进程配合 PostgreSQL；当前 MVP 已具备持久化 Outbox、Run lease/heartbeat、独立 `super-agent worker` 消费进程和持久化 Watchdog。达到并发或故障隔离门槛后，再把 Event Bus、Worker Runtime 和 Endpoint Daemon 拆成独立服务。服务拆分必须保留相同领域接口和事件契约。

