# Phase 5 任务：Watchdog、可靠性与性能

## P5-T001 健康快照和规则 Watchdog

- 目标：监控基础设施、Run 进展、租约和错误积累。
- 优先级：P1；依赖：P2-T005、P4-T005。
- 涉及文件：src/watchdog、src/health、tests/integration。
- 输入/输出与接口：输入通过前置模块的版本化契约提供；输出通过领域类型、状态变更或事件返回；错误使用统一错误分类，不得直接依赖具体供应商。
- 实现要求：独立调度、最小权限、心跳和健康快照；先用规则判断停滞和可恢复错误。
- 测试：进程崩溃、租约过期、数据库短暂不可用、Worker 停滞和重复告警。
- 验收：异常有分类、证据和下一步动作；Watchdog 不能修改无关任务。
- 状态：DONE。
- 完成记录：已实现 HealthSnapshot、WatchdogFinding 和规则 Watchdog，检查过期租约、停滞 Run、重复失败和健康状态。
- 验证结果：Watchdog 集成测试通过；全量 73 个 pytest、ruff 和 mypy 通过。

## P5-T002 恢复策略和幂等修复

- 目标：实现重试、补偿、暂停和人工接管。
- 优先级：P1；依赖：P5-T001。
- 涉及文件：src/recovery、src/audit、tests/integration。
- 输入/输出与接口：输入通过前置模块的版本化契约提供；输出通过领域类型、状态变更或事件返回；错误使用统一错误分类，不得直接依赖具体供应商。
- 实现要求：按错误分类选择重试/等待/暂停/失败；修复动作有幂等键、最大次数和回滚记录。
- 测试：模型超时、工具失败、Delivery 失败、重复恢复和人工停止。
- 验收：每次恢复都可追溯；超过阈值不会无限循环。
- 状态：DONE。
- 完成记录：已实现 RecoveryManager、错误类别决策、幂等键缓存、最大尝试、pause 和人工 stop。
- 验证结果：恢复集成测试通过；全量 75 个 pytest、ruff 和 mypy 通过。

## P5-T003 Router 批处理和性能基准

- 目标：量化事件速率、batch 大小、延迟、token 和并发上限。
- 优先级：P1；依赖：P3-T004、P5-T001。
- 涉及文件：tools/benchmarks、tests/performance、docs/article-analysis.md。
- 输入/输出与接口：输入通过前置模块的版本化契约提供；输出通过领域类型、状态变更或事件返回；错误使用统一错误分类，不得直接依赖具体供应商。
- 实现要求：合成事件流可重复；比较单事件、批处理、直通开关；记录 p50/p95、模型 usage 和队列长度。
- 测试：逐步提高 Trigger 速率，确认系统不静默丢失事件。
- 验收：形成基线和扩容阈值；是否需要 Router 分片由数据决定。
- 状态：DONE。
- 完成记录：已实现 benchmark_router、BatchBenchmarkResult、直通计数和 prompt token 估算。
- 验证结果：性能基准测试通过；全量 77 个 pytest、ruff 和 mypy 通过。

## P5-T004 OpenTelemetry、成本和告警

- 目标：把请求、任务、模型、工具、事件队列和 Delivery 连接成可观测链路。
- 优先级：P1；依赖：P0-T002、P2-T004。
- 涉及文件：src/observability、deploy/、tests/contract。
- 输入/输出与接口：输入通过前置模块的版本化契约提供；输出通过领域类型、状态变更或事件返回；错误使用统一错误分类，不得直接依赖具体供应商。
- 实现要求：trace/span 关联实体 ID；指标不包含原始敏感内容；预算超限触发策略和通知。
- 测试：采样、脱敏、预算告警、指标准确性和 exporter 不可用。
- 验收：单个任务可从 Trigger 追踪到 ToolCall 和 Delivery；成本超过预算会停止或请求批准。
- 状态：DONE。
- 完成记录：已实现 Telemetry span/trace 关联、计数器、token/cost accounting 和 approval_required 预算告警。
- 验证结果：观测与预算测试通过；全量 79 个 pytest、ruff 和 mypy 通过。

## P5-T005 Phase 5 可靠性验收

- 目标：通过故障演练验证长期运行能力。
- 优先级：P0；依赖：P5-T001 至 P5-T004。
- 涉及文件：tests/chaos、docs/progress.md。
- 输入/输出与接口：输入通过前置模块的版本化契约提供；输出通过领域类型、状态变更或事件返回；错误使用统一错误分类，不得直接依赖具体供应商。
- 实现要求：演练服务重启、依赖抖动、Worker 死循环、重复事件和超预算。
- 测试：恢复时间、未丢事件、无限循环阻断和审计完整性。
- 验收：满足阶段 SLO 草案；记录残余风险和人工介入点。
- 状态：DONE。
- 完成记录：已添加 Phase 5 chaos E2E，组合租约过期、Watchdog、Recovery、预算、Outbox 去重和设备撤销。
- 验证结果：全量 80 个 pytest、ruff 和 mypy 通过。


