# ADR-0005：采用 Python 模块化单体作为技术基线

- 状态：Accepted
- 日期：2026-11-07

## 背景

项目需要快速验证事件驱动 Agent 的完整闭环，同时保留模型、工具、渠道和存储的替换能力。技术栈应适合 AI 编程 Agent 维护，具备成熟的类型、Web、数据库、测试和观测生态。

## 候选方案

1. Python 3.12、FastAPI、Pydantic v2、SQLAlchemy 2、Alembic、PostgreSQL、pytest、OpenTelemetry。
2. TypeScript、Node.js、NestJS、Prisma 和 PostgreSQL。
3. Go 微服务加独立消息和工作流基础设施。

## 决策

采用方案 1 作为 MVP 基线。Python 便于集成模型 SDK、数据处理和测试；FastAPI/Pydantic 适合定义版本化 API 和契约；SQLAlchemy/Alembic 提供可迁移持久化；OpenTelemetry 与 Docker Compose 支持本地到生产的渐进演进。

## 代价与影响

- Python 异步、进程和 CPU 密集执行需要明确边界；长任务通过持久化 Run 和后台执行器隔离。
- 依赖较多，需要锁定版本、运行漏洞扫描和保持适配器边界。
- 若性能或部署需求证明另一语言更合适，只能通过新的 ADR 迁移单个执行适配器或服务，不能直接重写领域契约。

