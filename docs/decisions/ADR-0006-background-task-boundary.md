# ADR-0006：后台任务先持久化 Outbox，再进程内执行

## 状态

已接受（Phase 7 MVP）

## 背景

Super AI Agent 的模型和工具执行可能超过 HTTP 请求的生命周期。若在请求线程内直接执行，客户端断开会隐藏任务状态；若先启动线程再写数据库，进程崩溃会丢失任务。

## 决策

- `/api/tasks` 保留同步闭环，便于调试和小任务。
- `/api/tasks/async` 先在独立事务中提交 `TriggerRecord` 与 `OutboxMessage`，再由 `BackgroundAgentRunner` 在线程池消费。
- 两种入口都进入同一个 `Outbox -> Router -> Worker -> Run -> Delivery` 编排，不维护第二套 Agent 逻辑。
- 客户端用 `/api/tasks/status?dedupe_key=...` 查询持久化状态；服务器重启后，未发布的 Outbox 事件仍可被后续消费者接管。
- `super-agent worker` 提供独立的 Outbox 消费进程；消费者 claim 超过 lease 的消息时可回收并继续处理。
- SQLite 开发环境启用 WAL 与 busy timeout；后台执行器在模型调用前提交 Run/lease，避免网络等待持有数据库写锁。

## 后果

- 单进程 MVP 不会因 HTTP 连接断开而丢失已接受的任务。
- API 内置线程池适合单机开发；生产部署可用独立 Outbox worker，并继续补充 Run 级租约和 Watchdog 接管。
- 每个请求仍必须携带或继承稳定的 `dedupe_key`，否则无法可靠查询和去重。
