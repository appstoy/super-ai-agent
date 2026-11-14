# Phase 0 任务：工程基线与安全地基

状态枚举：TODO、IN_PROGRESS、BLOCKED、DONE。Phase 0 全部任务已完成。

## P0-T001 建立 Python 项目骨架

- 目标：创建可安装的 Python 3.12 包、src 布局、测试布局和基础 CLI/API 入口。
- 优先级：P0；依赖：无。
- 涉及文件：pyproject.toml、src/、tests/、README.md、.gitignore。
- 输入/输出与接口：输入通过前置模块的版本化契约提供；输出通过领域类型、状态变更或事件返回；错误使用统一错误分类，不得直接依赖具体供应商。
- 实现要求：定义包名和版本；提供 help、健康检查和可注入配置对象；领域层不得导入 Web 框架。
- 输入/输出：命令行参数和环境配置输入；CLI 返回稳定退出码，健康端点返回版本和状态。
- 测试：安装测试、CLI 帮助测试、健康端点测试、领域层导入隔离测试。
- 验收：干净环境可安装；help 和健康检查成功；测试可重复运行。
- 状态：DONE。
- 完成记录：已创建项目包骨架、配置对象、CLI、健康 API 和测试；已用 Python 3.12.14 完成安装验收。
- 验证结果：6 个 pytest 测试通过；ruff check 通过；mypy src 通过；CLI health 和 version 命令通过。

## P0-T002 配置、错误和日志规范

- 目标：建立类型化配置、统一错误分类、结构化日志和关联 ID。
- 优先级：P0；依赖：P0-T001。
- 涉及文件：src/config、src/observability、src/errors、tests/。
- 输入/输出与接口：输入通过前置模块的版本化契约提供；输出通过领域类型、状态变更或事件返回；错误使用统一错误分类，不得直接依赖具体供应商。
- 实现要求：区分配置缺失、外部依赖、权限、超时、可重试和不可重试错误；日志支持 request_id、task_id、run_id；敏感字段脱敏。
- 输入/输出：环境变量或配置文件输入；输出不可变配置对象、结构化日志事件。
- 测试：缺失配置、错误映射、日志脱敏和关联字段测试。
- 验收：测试日志中找不到 API key、Authorization 或完整凭据；同一请求的日志可关联。
- 状态：DONE。
- 完成记录：已实现 ErrorCategory/AgentError、CorrelationContext、JSON 日志和敏感字段递归脱敏。
- 验证结果：9 个 pytest 测试通过；ruff check 通过；mypy src 通过。

## P0-T003 数据库迁移和 Outbox 基础

- 目标：建立 PostgreSQL 连接、迁移机制和最小 Outbox 表。
- 优先级：P0；依赖：P0-T001、P0-T002。
- 涉及文件：src/storage、migrations、docker-compose.yml、tests/integration/。
- 输入/输出与接口：输入通过前置模块的版本化契约提供；输出通过领域类型、状态变更或事件返回；错误使用统一错误分类，不得直接依赖具体供应商。
- 实现要求：连接池、健康检查、迁移命令、事务边界、Outbox 状态和重试字段；不得在业务层拼 SQL。
- 输入/输出：数据库 URL 和事件 envelope；输出持久化记录和可领取消息。
- 测试：迁移从空库执行；事务回滚；同一事件不会重复插入；Outbox 领取和重试。
- 验收：Docker Compose 启动数据库后迁移成功；集成测试在 CI 可执行。
- 状态：DONE。
- 完成记录：已实现 SQLAlchemy Outbox 模型、SQLite/PostgreSQL 兼容引擎、session_scope、OutboxRepository 和 Alembic 初始迁移。
- 验证结果：迁移可 upgrade/downgrade；2 个 Outbox 集成测试通过；全量 11 个测试、ruff 和 mypy 通过。

## P0-T004 CI、质量门禁和开发文档

- 目标：建立 lint、format、类型检查、测试、安全扫描和贡献指南。
- 优先级：P0；依赖：P0-T001、P0-T002。
- 涉及文件：.github/workflows/、README.md、CONTRIBUTING.md、SECURITY.md、Makefile 或 task runner 配置。
- 输入/输出与接口：输入通过前置模块的版本化契约提供；输出通过领域类型、状态变更或事件返回；错误使用统一错误分类，不得直接依赖具体供应商。
- 实现要求：CI 使用固定 Python 版本和锁定依赖；失败不得允许合并；README 包含本地启动和测试命令。
- 测试：在本地运行与 CI 相同命令；检查文档命令可执行。
- 验收：Pull Request 能自动运行质量门禁；新贡献者可按 README 完成环境初始化。
- 状态：DONE。
- 完成记录：已创建 GitHub Actions quality workflow、uv.lock、CONTRIBUTING.md、SECURITY.md 和 Makefile。
- 验证结果：uv sync、pytest、ruff、mypy 全部通过；CI 使用相同检查命令。

## P0-T005 Phase 0 集成验收

- 目标：验证工程骨架可安装、可启动、可迁移和可观测。
- 优先级：P0；依赖：P0-T001 至 P0-T004。
- 涉及文件：tests/integration/、docs/progress.md。
- 输入/输出与接口：输入通过前置模块的版本化契约提供；输出通过领域类型、状态变更或事件返回；错误使用统一错误分类，不得直接依赖具体供应商。
- 实现要求：从干净环境启动服务，执行迁移、健康检查和一条失败请求，确认错误与日志可追踪。
- 测试：一条脚本覆盖安装、迁移、启动、健康端点和清理。
- 验收：脚本在开发机和 CI 都通过；更新 progress.md 并把任务标记 DONE。
- 状态：DONE。
- 完成记录：已添加 Phase 0 smoke 测试，覆盖健康 API、404 错误、配置错误分类和 Outbox 闭环。
- 验证结果：Alembic downgrade/upgrade 通过；全量 13 个 pytest 通过；ruff 和 mypy 通过；真实 CLI 进程的 health endpoint 返回 200。


