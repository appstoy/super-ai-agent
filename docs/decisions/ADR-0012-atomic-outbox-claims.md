# ADR-0012：原子 Outbox claim

## 状态

已接受（Phase 7）

## 决策

- Outbox 消费者先选候选事件，再用 `id + attempts + 可领取状态` 条件原子更新为 `CLAIMED`。
- 条件更新影响行数不是 1 时，消费者放弃本次 claim，不执行事件。
- PostgreSQL 使用行锁，SQLite 使用 WAL/busy timeout 与条件更新共同防止重复领取。

## 后果

- API 内置恢复线程、独立 worker 和多个部署实例可以安全并行消费同一 Outbox。
- 事件仍采用至少一次语义；业务处理必须依靠 Trigger/Run/Delivery 幂等保证最终一致。
