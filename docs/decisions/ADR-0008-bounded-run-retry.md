# ADR-0008：基于原 Trigger 的有界 Run 重试

## 状态

已接受（Phase 7）

## 决策

- 重试不复制 Trigger，也不修改原 dedupe key；只把原 `trigger.received` Outbox 消息重新置为 `PENDING`。
- `AgentOrchestrator.submit` 只把 `failed`、`timed_out` 和 `cancelled` 的旧 Run 视为可再次执行，其余状态仍按幂等重复事件返回既有结果。
- 新执行会创建同一 Task/Trigger 下递增 `attempt` 的 Run；`RunRecoveryService` 默认最多三次尝试。
- 超过上限保持失败/超时终态，交给 Watchdog 或人工处理，不进行无限自主循环。

## 后果

- 暂时的模型/工具故障可以通过同一事件链路自动恢复，且结果仍可追溯到原 Trigger。
- 具有外部副作用的工具必须自行保证幂等；本阶段不会无条件重放高风险副作用。
