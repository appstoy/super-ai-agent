# 自定义工具（User-loaded Tools）

Super AI Agent 自带以下内置工具，不需要任何配置：

| 工具 | 能力 | 作用 |
| --- | --- | --- |
| `file.read` | `file.read` | 读取工作区内的 UTF-8 文件 |
| `file.list` | `file.read` | 列出工作区目录，用来发现文件 |
| `file.write` | `file.write` | 在工作区内创建/覆盖文件（HTML、代码、报告） |
| `recall.read` | `recall.read` | 链式召回历史上下文 |

`file.write` 的几个约束：路径必须在 `SUPER_AGENT_WORKSPACE_ROOT` 之内（相对路径按该目录解析，`..` 越界会被拒绝）；默认不覆盖已有文件，需要显式传 `overwrite=true`；单文件内容有大小上限。工作区内的写入属于产品能力，因此 MEDIUM 风险被预授权；`command.run` 这类 HIGH 风险工具仍然需要人工审批。推测执行（speculation）会**排除所有写入类工具**，因为它承诺不产生副作用。

写出来的文件可以用 `GET /api/files?path=...` 浏览、`GET /api/files/content?path=...` 读文本、`GET /files/<path>` 直接渲染 HTML；控制台的「它产出的文件」面板就是调这几个接口。

除内置工具外，你可以加载自己的工具，模型会在 ReAct 循环里像内置工具一样调用它们。所有自定义工具**仍然经过 `ToolRegistry` 和 `ToolPolicy`**：能力检查、审批门槛、输出截断和审计都不会被绕过。

## 四种加载方式

优先级与顺序：内置工具 → 清单文件 → 环境变量 → 已安装的 entry point。

1. **清单文件**：设置 `SUPER_AGENT_TOOL_MANIFEST=/path/to/tools.json`；不设置时若当前工作目录存在 `tools.json` 也会自动读取。
2. **模块**：`SUPER_AGENT_TOOL_MODULES=my_pkg.tools:make_tool,other_pkg:tool`（逗号分隔）。
3. **目录 / 文件**：`SUPER_AGENT_TOOL_PATHS=./my_tools`（os.pathsep 分隔；目录会扫描顶层 `*.py`，跳过 `__init__.py`）。

   Windows 路径支持 `C:\tools\my.py:make_tool` 这种带 `:attr` 的写法（不会被盘符冒号切开）。
4. **entry point**：第三方包在自己的 `pyproject.toml` 里声明 `[project.entry-points."super_agent.tools"]`，安装后自动发现。

单个工具加载失败只会被记录，**不会阻断启动**；用 `super-agent tools` 或 `GET /api/tools` 查看 `loaded` / `failed`。

## 工具可以长什么样

- 普通 Python 函数 → 自动包成 `FunctionTool`，从类型标注和默认值生成参数 schema。
- 实现了 `describe()` / `execute()` 的 Tool 实例或类。
- 工厂函数（`make_tool` / `create_tool` / `build_tool`，或模块级 `tool` / `TOOL` / `tools` / `TOOLS`）。
- 一个 `.py` 文件可以导出多个工具（用元组 `tools = (a, b)`）。

最小例子见 [`examples/tools/example_tool.py`](../examples/tools/example_tool.py)。

## 清单格式

```json
{
  "tools": [
    {
      "path": "./my_tools/echo.py",
      "capabilities": ["custom.echo"],
      "enabled": true
    },
    {
      "module": "my_pkg.tools",
      "attr": "make_tool",
      "kind": "factory",
      "name": "echo.custom",
      "description": "Echo text",
      "capabilities": ["custom.echo"],
      "args": {"prefix": "> "}
    }
  ]
}
```

字段说明：

| 字段 | 含义 |
| --- | --- |
| `path` | `.py` 文件路径；可加 `:attr` 指定导出 |
| `module` / `attr` | `module:attr` 形式的导入路径 |
| `kind` | `auto`（默认）/ `tool` / `class` / `factory` / `function` |
| `name` / `description` | 覆盖普通函数的注册名与描述 |
| `capabilities` | 自定义能力集合，会自动并入 Worker 的 `granted_capabilities` |
| `args` | 传给工厂或类构造函数的 kwargs |
| `enabled` | `false` 时跳过 |

安全边界：

- `task.start` / `task.stop` / `task.status` 及其归一化形式（`task_start` 等）是保留控制名，不能占用。
- `capabilities` 不允许 `"*"` 或空字符串。
- 高风险工具（`risk=high` / `requires_approval=True`）仍由 `ToolPolicy` 要求显式审批；加载器不会放行。
- 工具抛出的异常会被转成 `ValidationError` 并以 tool result 回传给模型，不会让 Worker 崩溃。

## 端到端试一下

```bash
# 看加载报告（不连数据库、不发网络请求）
SUPER_AGENT_TOOL_PATHS=examples/tools super-agent tools

# 提交一个会用自定义工具的任务
SUPER_AGENT_TOOL_PATHS=examples/tools super-agent submit "用 echo.custom 重复 hello 三次"

# HTTP 方式查看
curl http://127.0.0.1:8000/api/tools
```

## 验收证据

仓库里有一份可直接运行、不需要任何凭据或网络的验收脚本，覆盖多厂商 tool_calls
形状、参数容错、工具名一致性、自定义工具发现/执行、失败隔离、未知工具自愈，
以及一次真实 HTTP 往返（假服务端到端调用自定义工具 + 内置工具）：

```bash
python tools/verification/verify_tool_compat.py
```

它会输出一份 JSON 清单（`passed` + 每条 `checks`，当前 21 项），覆盖流式 SSE 分片
tool_calls 拼装、Windows `path:attr` 解析、以及真实 HTTP 端到端；全绿即视为两项能力都成立。
同一脚本也接入了 pytest：`tests/integration/test_tool_compat_acceptance.py`。

## 用 SDK 自己写

```python
from super_agent.sdk import FunctionTool, load_user_tools

def weather(city: str) -> str:
    """Return a fake weather report."""
    return f"{city}: sunny"

tool = FunctionTool(weather, capabilities=frozenset({"custom.weather"}))
report = load_user_tools([tool])
registry = report.registry
```

`ToolLoadReport.to_dict()` 的输出即 `/api/tools` 的响应体，不含任何凭据。
