# ADR-0002：PostgreSQL 作为事实源，Outbox 作为 MVP 事件总线

- 状态：Accepted
- 日期：2026-11-07

## 背景

事件至少一次投递、任务恢复和审计需要可靠持久化。MVP 规模不足以证明 Kafka 等独立消息系统的必要性。

## 候选方案

1. PostgreSQL 状态表加 Outbox/Inbox 和轮询消费者。
2. Redis Streams 或 NATS。
3. Kafka 加完整事件流平台。

## 决策

选择 PostgreSQL 作为任务和状态事实源，Outbox 记录待发布事件，EventBus 接口屏蔽实现。未来按吞吐和故障隔离指标评估 NATS 或 Redis Streams。

## 代价与影响

- 减少依赖和本地运维成本，事务内可同时写状态和事件。
- 数据库轮询吞吐有限；必须记录队列延迟并设置迁移阈值。
- 不做全量事件溯源，关键状态变化保留审计记录和可回放输入。

