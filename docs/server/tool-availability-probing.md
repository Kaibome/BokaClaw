# 工具可用性探测

## 目标

每次 run 组装工具列表前，先检查工具依赖的本地资源、内部服务和外部服务。不可用的工具不进
模型上下文，避免把会失败的 schema 发给模型、浪费 token，也避免模型调用一个必然失败的工具。

探测结果按工具名缓存在进程级单例中，30 秒内的所有 run 复用同一份结果。缓存过期后重新探测。

## 状态机

| 状态 | 条件 | 行为 | 是否写缓存 |
|---|---|---|---|
| B | 缓存有效（< 30s） | 直接返回缓存的 True/False，不探测 | — |
| C | 缓存过期 / 无缓存 | 执行探针 `fn()` | — |
| C1 | 探测 = True | `last_good = now`，返回 True | 写 True，30s |
| C2 | 探测 = False（含异常/超时），且 `now - last_good < 60s` | 对外返回 True（视为抖动） | 不写缓存 |
| C3 | 探测 = False，且超过 60s 没有成功过（或从未成功） | 对外返回 False（承认故障） | 写 False，30s |

实现位置：`server/app/agent/tool_availability.py` 的 `ToolAvailabilityTracker.check()`。

C2 不写缓存，因此抖动期间每次检查都会重新探测，直到成功或超过 60s 宽限期。

## 探针覆盖

没有探针的工具默认可用：它们只有在开关和依赖都成立时才会被创建（例如 `Skill`、`ask_user`、
子 Agent 工具、`ToolSearch`）。

| 工具 | 探针 | 超时 |
|---|---|---|
| `search_memories` / `get_memory` / `save_memory` / `update_memory` / `delete_memory` | 记忆服务已初始化 | 2s |
| `Read` / `Glob` / `Grep` | workspace 可读 | 2s |
| `Write` / `Edit` / `NotebookEdit` | workspace 可写 | 2s |
| `Bash` / `PowerShell` / `Monitor` | 配置了 executor 时请求 `/healthz`，否则检查本地 shell | 5s（executor）/ 2s（本地） |
| `WebFetch` | 真实抓取 `https://example.com` 并要求 HTTP 200 | 5s |
| `WebSearch` | 用配置的搜索端点（或 Bing RSS）执行一次最小查询 | 5s |
| `PptxBuild` / `PptxUpdateSlide` / `PptxAddSlide` / `PptxDeleteSlide` | `python-pptx` 可用且 workspace 可写 | 2s |
| `SheetInspect` / `SheetQuery` | `pandas`/`openpyxl` 可用且 workspace 可读 | 2s |
| `SheetCreate` / `SheetTransform` / `SheetPivot` | `pandas`/`openpyxl` 可用且 workspace 可写 | 2s |
| `mcp__<server>__*` | 通过 MCP manager 重新获取 catalog（受 MCP 自身缓存约束） | 5s |

同一个探针对象被多个工具共享时（workspace、Office、每个 MCP server），一批检查只执行一次。

## 执行策略

- 所有待探测工具并发执行。
- 单个探针：本地资源 2s，需要调用外部服务 5s。
- 整批硬上限 10s；超过上限仍未完成的探针按失败处理，不会阻塞 run。
- 探针抛异常、超时、被取消，都按 `False` 处理。

## 接入点

1. `server/app/agent/runtime.py` 的 `build_runtime()`：工具列表组装完成后、`collect_deferred()`
   之前调用 `build_tool_probes()` + `filter_tools()`。不可用工具不会进入 registry，也不会进入
   `<deferred-tools>` 目录。
2. `ToolSearch` 执行前：`filter_tool_mapping()` 再检查一遍延迟工具，避免 30s 内状态变化后仍返回
   已不可用的工具。
3. `stream_local_agent()` 通过 `asyncio.to_thread(build_runtime, ...)` 执行组装，避免探针等待
   阻塞 FastAPI 事件循环。

## 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `AGENT_TOOL_PROBES` | 开 | 设为 `0`/`false`/`no`/`off` 时跳过全部探测。产品测试通过 `tests/conftest.py` 默认关闭，避免依赖网络。 |

## 测试

- `server/tests/test_tool_availability.py`：缓存命中、过期、C1/C2/C3、异常/超时、硬上限、探针去重、
  工具过滤、MCP 名称映射。
- `tests/test_agent_runtime.py::test_build_runtime_excludes_tools_whose_probe_fails`：`build_runtime`
  端到端过滤。
