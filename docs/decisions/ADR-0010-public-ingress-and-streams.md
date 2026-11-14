# ADR-0010：统一 Webhook 与流式观察接口

## 状态

已接受（Phase 7）

## 决策

- Web UI、CLI 和 `/api/webhooks/{source}` 都只负责构造规范化 Trigger，后续统一进入 Outbox/Router/Worker 运行时。
- Webhook 在配置 `SUPER_AGENT_WEBHOOK_SECRET` 后要求 canonical JSON 的 HMAC-SHA256 签名；本地未配置 secret 时允许开发测试。
- `/api/chat/stream` 只负责直接模型文本流；真正的 Agent 任务使用 `/api/tasks/{task_id}/stream` 观察持久化 Worker 事件，避免把流式输出当作状态事实。

## 后果

- 外部渠道可以复用同一 Agent，而不需要复制业务编排逻辑。
- 生产 Webhook 必须配置 secret，并由上游保管签名密钥。
- Agent 的最终状态仍以 Task/Run/Worker 持久化记录为准，客户端断线不会丢失任务。
