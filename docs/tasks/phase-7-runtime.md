# Phase 7 任务：统一 Super-agent Runtime

本阶段把已有 Router、Worker、Trigger、Context、Tool、Run 和 Delivery 组件连接为真实可运行的任务闭环。

## P7-T001 AgentOrchestrator 端到端编排

- 目标：让 HTTP 任务入口经过 Ingress、Router、Task/Worker、Context、ModelGateway 和 Worker 状态事件。
- 状态：IN_PROGRESS。
- 已完成：新增 `AgentOrchestrator`、`POST /api/tasks`、Magpie 真实任务验证、Run/WorkerEvent/WorkerContext 持久化。
- 已验证：模型可返回工具调用；ToolRegistry 执行工具；工具结果回到 Worker Context；最终结果和事件时间线持久化。
- 已完成：控制台默认挂载受 `SUPER_AGENT_WORKSPACE_ROOT` 限制的 `file.read` 只读工具；Router 对普通模型的非契约输出有任务回退策略。
- 验收：真实任务返回 Task/Worker ID，Task 和 Worker 进入 `succeeded`，Run 与 Worker 时间线可查询。
- 已完成：新增 `/api/tasks/async`、持久化入队和 `/api/tasks/status`，页面提供后台执行按钮；后台任务仍复用同一 Outbox -> Router -> Worker 闭环。
- 已完成：`POST /api/webhooks/{source}` 支持可选 HMAC 验签，并复用同一后台 Agent 入口。
- 当前限制：API 内置线程池仍是单进程；独立消费者、Run lease 和规则 Watchdog 已可接管持久化状态，模型流式中断和告警通知仍待完成。

## P7-T002 持久化订阅与事件消费者

- 状态：IN_PROGRESS。
- 已完成：新增 `OutboxProcessor`，支持 claim、JSON 校验、handler 分发、失败回退和 PUBLISHED 状态；新增从 `trigger.received` payload 重新进入 `AgentOrchestrator` 的接口。
- 已完成：`AgentOrchestrator.submit_via_outbox` 将任务入口显式经过 Outbox claim 和 handler，再进入同一 Router/Worker 编排。
- 已完成：`BackgroundAgentRunner` 在提交前提交 Trigger/Outbox，后台线程只消费已持久化事件；进程内失败不会丢失待处理消息。
- 已完成：同一 Thread 的 active Subscription 会从数据库恢复 Worker Context，并把后续事件直达同一 Task/Worker；助手回复也会写回上下文。
- 已完成：`DurableOutboxWorker` 与 `super-agent worker` 可在独立进程轮询待处理事件；Outbox 会回收过期 claim lease。
- 已完成：`super-agent serve` 启动进程内恢复消费者；服务重启后会自动处理持久化 Outbox/Run，独立 worker 仍可用于多进程部署。
- 已完成：Run lease/heartbeat、过期恢复和跨进程 Worker 状态接管；独立消费者会同步 Task/Worker 健康状态。
- 待完成：模型/工具调用的强制中断和跨实例协调策略。

## P7-T003 Worker 异步恢复

- 状态：IN_PROGRESS。
- 目标：把工具和模型循环放入可恢复 Run，支持停止、超时、重试和 Watchdog 接管。
- 已完成：Run 持久化 heartbeat、lease owner、lease expiry 和 finished_at；Worker 在模型/工具轮次前续租，并在取消请求或租约失效时协作停止。
- 已完成：`RunLeaseService` 支持 claim、heartbeat、cancel request 和 expired-run recovery；独立 Outbox worker 会回收过期 Run 并同步 Task/Worker 健康状态。
- 已完成：`RunRecoveryService` 按 `attempt` 和 `max_attempts` 将已发布的失败/超时 Trigger 重新放回 Outbox；超过上限不再重试。
- 已完成：`PersistentRunWatchdog` 与 `super-agent watchdog` 可检查 stalled/repeated failure，并触发受限 lease recovery/requeue。
- 待完成：模型流式中断、告警通道和更细粒度的自动修复策略。

## P7-T004 真实工具循环与 Delivery

- 状态：IN_PROGRESS。
- 已完成：模型工具调用 -> ToolRegistry -> Worker Context -> 后续模型轮次；任务详情 API 可查询执行事件和上下文。
- 已完成：任务详情提供 `/api/tasks/{task_id}/stream` SSE 进度流，前端可实时观察 Worker 事件直到终态。
- 已完成：OpenAI-compatible Gateway 支持 `/api/chat/stream` token delta；任务级执行仍以 Worker 事件流为事实源。
- 已完成：成功任务将 DeliveryRecord 标记为 `sent` 并保存最终文本；异常任务标记为 `failed` 并保存错误。
- 已完成：可注入 `DeliveryService`，成功结果通过 CLI 渠道发送，并保留幂等键；审批与外部渠道重试沿用现有 DeliveryPolicy/DeliveryService。
