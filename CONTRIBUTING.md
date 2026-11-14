# Contributing

感谢参与 Super AI Agent。请先阅读根目录 Agent.md，再阅读目标任务和相关架构/ADR 文档。

## 本地检查

需要 Python 3.12+ 和 uv：

    uv venv .venv --python 3.12
    uv sync --extra dev --python .venv/Scripts/python.exe
    uv run pytest
    uv run ruff check .
    uv run mypy src

## 变更规则

- 一次提交聚焦一个任务，任务 ID 写入提交说明或 PR 描述。
- 公开接口、状态模型、权限边界、部署拓扑和依赖基线变化必须更新 ADR。
- 新代码必须有针对行为的测试；不能用快照或重复实现替代真实验证。
- 不提交密钥、.env、本地数据库、构建产物或包含敏感数据的日志。
- 完成任务后更新任务状态和 docs/progress.md。

## Pull Request

PR 描述需要说明问题、行为变化、测试命令和已知限制。CI 全部通过并完成安全检查后才能合并。

