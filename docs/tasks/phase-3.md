# Phase 3 任务：上下文、记忆与渠道输出

## P3-T001 Context Service 与 Inline/Spill

- 目标：按 token/字符预算构建可解释的模型上下文。
- 优先级：P1；依赖：P2-T005。
- 涉及文件：src/context、src/ports/context.py、tests/unit/context。
- 输入/输出与接口：输入通过前置模块的版本化契约提供；输出通过领域类型、状态变更或事件返回；错误使用统一错误分类，不得直接依赖具体供应商。
- 实现要求：当前 Trigger 优先；按局部性加入 Thread/Task/Artifact 摘要；超限内容保留引用和读取入口；输出 context manifest。
- 测试：预算边界、截断、优先级、未授权对象和 manifest 稳定性。
- 验收：任何模型请求都有预算和对象来源；上下文超限时不会静默丢掉继续读取入口。
- 状态：DONE。
- 完成记录：已实现 ContextCandidate、InlineSpillContextService、token 预算、当前 Trigger 优先、授权排除和 spill read_ref manifest。
- 验证结果：Context 专项测试通过；全量 52 个 pytest、ruff 和 mypy 通过。

## P3-T002 引用图和链式召回

- 目标：让 Router/Worker 根据对象 ID 逐跳恢复相关上下文。
- 优先级：P1；依赖：P3-T001、P1-T002。
- 涉及文件：src/context/recall、src/storage、tests/integration。
- 输入/输出与接口：输入通过前置模块的版本化契约提供；输出通过领域类型、状态变更或事件返回；错误使用统一错误分类，不得直接依赖具体供应商。
- 实现要求：Trigger、Thread、Task、Worker、Artifact 之间保存引用；每跳受权限和深度预算限制；记录召回链。
- 测试：跨 Thread 召回、循环引用、深度上限、删除对象和权限隔离。
- 验收：从当前事件可恢复关联任务和最近摘要；跨权限对象不可被召回。
- 状态：DONE。
- 完成记录：已实现 ReferenceGraph、租户/权限过滤、深度/节点预算、循环检测和 RecallStep 审计链。
- 验证结果：3 个召回集成测试通过；全量 55 个 pytest、ruff 和 mypy 通过。

## P3-T003 Delivery Policy 和第二渠道适配器

- 目标：根据渠道带宽、风险和事件类型生成合适输出。
- 优先级：P1；依赖：P1-T004、P2-T005。
- 涉及文件：src/delivery、src/adapters/channels、tests/contract、tests/integration。
- 输入/输出与接口：输入通过前置模块的版本化契约提供；输出通过领域类型、状态变更或事件返回；错误使用统一错误分类，不得直接依赖具体供应商。
- 实现要求：CLI 与 webhook 至少两个适配器；Worker 只生成内部结果；Delivery 负责格式、审批、重试和幂等。
- 测试：短摘要、结构化长文、发送失败重试、重复发送和审批阻断。
- 验收：同一结果在两个渠道内容密度不同但事实一致；发送失败不会丢任务结果。
- 状态：DONE。
- 完成记录：已实现 DeliveryRequest、DeliveryPolicy、DeliveryService、CLI/Webhook 内存适配器、审批、重试和幂等缓存。
- 验证结果：3 个 Delivery 集成测试通过；全量 58 个 pytest、ruff 和 mypy 通过。

## P3-T004 事件回放和上下文评估工具

- 目标：支持离线重放事件流并比较路由、召回和 token 指标。
- 优先级：P1；依赖：P3-T001、P3-T003。
- 涉及文件：tools/replay、tests/e2e、docs/article-analysis.md。
- 输入/输出与接口：输入通过前置模块的版本化契约提供；输出通过领域类型、状态变更或事件返回；错误使用统一错误分类，不得直接依赖具体供应商。
- 实现要求：固定输入事件、Mock 模型和策略版本；输出决定、延迟、token、召回链和 Delivery。
- 测试：相同输入输出稳定；策略版本变化可比较；敏感内容脱敏。
- 验收：能重放至少一个多 Thread 场景，并生成可保存的基准结果。
- 状态：DONE。
- 完成记录：已实现 ReplayRunner、固定 Mock 决策、Context manifest、token 指标和稳定 JSON dump。
- 验证结果：回放 E2E 通过；全量 59 个 pytest、ruff 和 mypy 通过。

## P3-T005 Phase 3 集成验收

- 目标：验证跨渠道连续交互和按需召回。
- 优先级：P1；依赖：P3-T001 至 P3-T004。
- 涉及文件：tests/e2e、docs/progress.md。
- 输入/输出与接口：输入通过前置模块的版本化契约提供；输出通过领域类型、状态变更或事件返回；错误使用统一错误分类，不得直接依赖具体供应商。
- 实现要求：事件 A 创建任务，事件 B 从另一个渠道补充约束，Worker 通过召回继续处理。
- 测试：上下文预算、权限隔离、两个渠道投递和回放一致性。
- 验收：需求场景中的跨渠道连续性通过；记录 token 和延迟基线。
- 状态：DONE。
- 完成记录：已添加 Phase 3 E2E，组合 Router、ReferenceGraph、ContextService 和 CLI/Webhook Delivery。
- 验证结果：全量 60 个 pytest、ruff 和 mypy 通过；跨渠道连续性、召回和事实一致性通过。


