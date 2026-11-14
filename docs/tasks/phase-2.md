# Phase 2 任务：并发、订阅与异步执行

## P2-T001 Worker Runtime 和隔离上下文

- 目标：实现 Worker 生命周期、上下文隔离和状态汇报。
- 优先级：P0；依赖：P1-T005。
- 涉及文件：src/application/worker、src/context、tests/unit/worker。
- 输入/输出与接口：输入通过前置模块的版本化契约提供；输出通过领域类型、状态变更或事件返回；错误使用统一错误分类，不得直接依赖具体供应商。
- 实现要求：Worker 不直接发送 Delivery；每个 Worker 有能力集合和任务关联；状态事件可重放。
- 测试：并发 Worker、隔离上下文、非法状态转换和结果汇报。
- 验收：两个任务同时运行不会互相注入历史或覆盖状态。
- 状态：DONE。
- 完成记录：已实现 WorkerContext copy-on-write 快照、WorkerRuntime 生命周期、状态事件序号和内部结果汇报。
- 验证结果：Worker 隔离、事件重放、非法终态转换测试通过；全量 39 个 pytest、ruff 和 mypy 通过。

## P2-T002 Subscription 直通和幂等消费

- 目标：实现 Thread-Worker 多对多订阅、直通和取消/续订。
- 优先级：P0；依赖：P2-T001、P1-T002。
- 涉及文件：src/application/subscriptions、src/eventing、tests/integration。
- 输入/输出与接口：输入通过前置模块的版本化契约提供；输出通过领域类型、状态变更或事件返回；错误使用统一错误分类，不得直接依赖具体供应商。
- 实现要求：匹配已订阅 Thread 时绕过 Router 决策；Router 保留全局观察事件；消费按 event_id 幂等。
- 测试：多 Worker、多订阅、重复投递、过期订阅、Worker 交回 Router。
- 验收：同一 Thread 后续事件只触发符合订阅的 Worker，且不重复执行。
- 状态：DONE。
- 完成记录：已实现多对多 Subscription、有效期、取消/续订、Thread 直通和 per-worker event_id 幂等消费。
- 验证结果：3 个订阅集成测试通过；全量 42 个 pytest、ruff 和 mypy 通过。

## P2-T003 异步 Tool Executor

- 目标：实现 start、stop、完成事件、超时、取消和重试。
- 优先级：P0；依赖：P2-T001、P0-T002、P1-T006。
- 涉及文件：src/tools、src/application/runs、tests/integration。
- 输入/输出与接口：输入通过前置模块的版本化契约提供；输出通过领域类型、状态变更或事件返回；错误使用统一错误分类，不得直接依赖具体供应商。
- 实现要求：Run 和 ToolCall 状态机；后台执行立即返回 Started；完成结果写入 Outbox；重试按错误分类。
- 测试：慢工具期间新事件、取消竞态、超时、进程重启恢复和重复完成事件。
- 验收：前台请求不等待慢工具；结果最终可查询；不可取消工具能明确报告限制。
- 状态：DONE。
- 完成记录：已实现 AsyncToolExecutor、RunEvent、后台 start/wait、stop、超时、可重试错误和终态幂等。
- 验证结果：3 个异步执行集成测试通过；全量检查中的 ruff 和 mypy 通过。

## P2-T004 Scheduler 租约和并发策略

- 目标：控制并发、优先级、租约、心跳和恢复。
- 优先级：P0；依赖：P2-T003。
- 涉及文件：src/scheduler、migrations、tests/unit/scheduler。
- 输入/输出与接口：输入通过前置模块的版本化契约提供；输出通过领域类型、状态变更或事件返回；错误使用统一错误分类，不得直接依赖具体供应商。
- 实现要求：租约过期可回收；同一 Task 的互斥策略可配置；全局和用户级预算可见。
- 测试：租约丢失、抢占、并发上限、优先级和恢复。
- 验收：重复 Worker 不会同时持有同一互斥执行权；重启后能决定重试或失败。
- 状态：DONE。
- 完成记录：已实现优先级队列、全局容量、Task mutex、租约 heartbeat、release 和过期回收。
- 验证结果：3 个 Scheduler 单元测试通过；全量 48 个 pytest、ruff 和 mypy 通过。

## P2-T005 Phase 2 集成验收

- 目标：验证多任务全双工闭环。
- 优先级：P0；依赖：P2-T001 至 P2-T004。
- 涉及文件：tests/e2e、docs/progress.md。
- 输入/输出与接口：输入通过前置模块的版本化契约提供；输出通过领域类型、状态变更或事件返回；错误使用统一错误分类，不得直接依赖具体供应商。
- 实现要求：回放一个包含独立任务、Thread 跟进、慢工具和重复事件的消息流。
- 测试：测量事件延迟、重复执行次数、Run 恢复结果和 Router 决策数。
- 验收：需求场景 2、3 通过；记录基线并更新 article-analysis 中的验证状态。
- 状态：DONE。
- 完成记录：已添加 Phase 2 E2E，组合 WorkerRuntime、SubscriptionRouter、AsyncToolExecutor 和 Scheduler。
- 验证结果：全量 49 个 pytest、ruff 和 mypy 通过；多任务、Thread 直通、重复事件和异步完成均通过。


