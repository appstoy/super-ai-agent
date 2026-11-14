# Phase 1 任务：持久化事件与最小 Agent

## P1-T001 定义领域实体和状态机

- 目标：实现 Trigger、Thread、Task、Worker、Run、Delivery 的类型和合法状态转换。
- 优先级：P0；依赖：P0-T005。
- 涉及文件：src/domain、tests/unit/domain、docs/architecture.md。
- 输入/输出与接口：输入通过前置模块的版本化契约提供；输出通过领域类型、状态变更或事件返回；错误使用统一错误分类，不得直接依赖具体供应商。
- 实现要求：版本化输入；状态转换拒绝非法跳转；所有实体带稳定 ID 和时间字段。
- 测试：合法/非法转换、序列化、幂等键和版本冲突测试。
- 验收：状态机不依赖数据库或 Web 框架；契约测试固定公共字段。
- 状态：DONE。
- 完成记录：已实现 Trigger、Thread、Task、Worker、Run、Delivery 及状态枚举、合法转换、版本检查和序列化。
- 验证结果：领域契约测试通过；全量 24 个 pytest、ruff 和 mypy 通过。

## P1-T002 实现 State Store 和 Trigger Ingress

- 目标：把规范化事件安全写入数据库并完成去重。
- 优先级：P0；依赖：P1-T001、P0-T003。
- 涉及文件：src/application/ingress、src/adapters/storage、migrations、tests/integration。
- 输入/输出与接口：输入通过前置模块的版本化契约提供；输出通过领域类型、状态变更或事件返回；错误使用统一错误分类，不得直接依赖具体供应商。
- 实现要求：鉴权、大小限制、dedupe_key 唯一约束、Outbox 事务写入；原始 payload 只保存受控引用。
- 测试：重复事件、并发插入、非法 schema、超大 payload、事务回滚。
- 验收：同一外部事件最多产生一个逻辑 Trigger；可查询过滤结果和接收时间。
- 状态：DONE。
- 完成记录：已实现 TriggerInput schema、时区和 payload 限制、TriggerRecord、幂等去重和同事务 Outbox 写入。
- 验证结果：Alembic 两个迁移可升级/回滚；全量 26 个 pytest、ruff 和 mypy 通过。

## P1-T003 ModelGateway 与 Mock 模型

- 目标：提供供应商无关的模型端口和可回放 Mock。
- 优先级：P0；依赖：P1-T001。
- 涉及文件：src/ports/model.py、src/adapters/models、tests/contract。
- 输入/输出与接口：输入通过前置模块的版本化契约提供；输出通过领域类型、状态变更或事件返回；错误使用统一错误分类，不得直接依赖具体供应商。
- 实现要求：统一文本、结构化决定、工具调用、usage 和错误分类；记录 provider_request_id；Mock 支持固定脚本。
- 测试：契约测试、超时、限流、供应商错误映射、固定脚本回放。
- 验收：Router/Worker 只依赖端口；无网络时可完成核心测试。
- 状态：DONE。
- 完成记录：已实现 ModelGateway、消息/工具/usage/响应契约、MockModelGateway 和错误重放。
- 验证结果：3 个模型契约测试通过；全量检查中的 ruff 和 mypy 通过。

## P1-T004 Router 最小决策循环

- 目标：把 Trigger 交给 Router，创建 Task/Worker 并产出统一 Delivery。
- 优先级：P0；依赖：P1-T002、P1-T003。
- 涉及文件：src/application/router、src/application/delivery、tests/integration。
- 输入/输出与接口：输入通过前置模块的版本化契约提供；输出通过领域类型、状态变更或事件返回；错误使用统一错误分类，不得直接依赖具体供应商。
- 实现要求：先规则过滤，再调用 ModelGateway；模型决定只能产生领域命令；Router 不直接执行工具。
- 测试：忽略、创建任务、请求澄清、模型失败和重复 Trigger。
- 验收：CLI 或 HTTP 输入可得到可查询 Task 和一条 Delivery；领域状态由代码提交。
- 状态：DONE。
- 完成记录：已实现 Fast Path 过滤、ModelGateway 决策、Task/Worker/Delivery 创建和统一输出；Router 不执行工具。
- 验证结果：Router 集成测试通过；全量检查中的 ruff 和 mypy 通过。

## P1-T005 Phase 1 集成验收

- 目标：验证从输入到持久化任务和响应的最小闭环。
- 优先级：P0；依赖：P1-T001 至 P1-T004、P1-T006。
- 涉及文件：tests/e2e、docs/progress.md。
- 输入/输出与接口：输入通过前置模块的版本化契约提供；输出通过领域类型、状态变更或事件返回；错误使用统一错误分类，不得直接依赖具体供应商。
- 实现要求：使用 Mock 模型运行“提交任务、查询任务、重启服务、读取结果”流程。
- 测试：E2E 脚本和数据库重启测试。
- 验收：需求文档中的 MVP 场景 1、4、6 通过；更新路线状态。
- 状态：DONE。
- 完成记录：已新增 TaskRecord、WorkerRecord、DeliveryRecord、RouterStateStore 和 Phase 1 E2E。
- 验证结果：36 个 pytest、ruff、mypy 通过；Alembic 全量回滚/升级通过；服务重建后可查询 Task/Worker/Delivery。

## P1-T006 最小 Tool Registry 和只读工具契约

- 目标：在不开放真实 Shell 的前提下验证工具描述、能力检查和结果引用。
- 优先级：P0；依赖：P1-T003、P0-T002。
- 涉及文件：src/tools、src/ports/tool.py、tests/unit/tools、tests/security。
- 输入/输出与接口：输入为 ToolSpec、结构化参数和 ExecutionContext；输出为可审计的 ToolResult 或拒绝原因。
- 实现要求：提供 Mock 工具和受控只读输入工具；未知能力默认拒绝；结果有大小限制和引用 ID。
- 测试：能力拒绝、参数 schema、输出截断、重复调用和敏感字段脱敏。
- 验收：Router/Worker 可通过统一 Tool 端口完成离线任务；不执行任意 Shell 或写文件。
- 状态：DONE。
- 完成记录：已实现 ToolSpec、ExecutionContext、ToolResult、ToolRegistry、MockTool 和 ReadOnlyInputTool。
- 验证结果：能力拒绝、缺参、结果截断和只读执行测试通过；ruff 和 mypy 通过。


