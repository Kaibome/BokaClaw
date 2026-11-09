---
title: "MCP 接入"
description: "远程 MCP server 的工具发现、调用、审批与安全边界"
---

# MCP 接入

> 状态：v1（仅远程 Streamable HTTP）
> 适用版本：`server/` 内进程 Agent 运行时
> 相关文件：`server/app/agent/mcp/`、`server/app/agent/runtime.py`

## 目标与非目标

BokaClaw 通过 MCP 把外部系统的工具接入 Agent，而不为每个外部系统重复实现工具。

v1 支持：

- 远程 Streamable HTTP MCP server；
- env / JSON 配置文件声明 server；
- 按 server 静态启用，配合 `allow_tools` / `deny_tools`；
- 按 MCP 注解分级审批；
- 进程级工具目录缓存与失败隔离。

v1 不支持：stdio、旧 SSE 传输、OAuth、Dashboard 管理、工具检索/按需加载、`resources/*`、`prompts/*`、子 Agent 和远程 executor 中的 MCP。

## 架构

```text
POST /agent
  → stream_local_agent()
  → load_mcp_config()          # AGENT_MCP_ENABLED=false 时直接返回 None
  → prepare_mcp_tools()        # 发现/缓存 → 生成 Tool 代理
  → build_runtime(extra_tools=...)
  → bokaclaw-agent ReAct 循环
  → tool.call / tool.result → SSE、spans、tool_trace
```

新增包 `server/app/agent/mcp/`：

| 文件 | 职责 |
|---|---|
| `config.py` | 解析配置、校验、`${ENV}` 插值、fingerprint |
| `security.py` | URL/SSRF 校验、密钥脱敏、描述清洗、不可信标记 |
| `client.py` | 唯一导入 `mcp` SDK 的模块；Streamable HTTP 适配器 |
| `catalog.py` | 目录缓存、TTL、stale、退避 |
| `manager.py` | 发现与调用编排、失败隔离、单例 |
| `tools.py` | MCP 工具 → `Tool` 代理、命名空间、注解映射 |
| `approval.py` | 审批策略与 `ask_user` 包装 |
| `results.py` | 结果转换、截断、不可信包裹 |

依赖：`server/requirements.txt` 固定 `mcp==1.12.0`。升级 MCP SDK 时要同步检查 pydantic 版本下限。

## 配置

```text
AGENT_MCP_ENABLED=false
# AGENT_MCP_CONFIG_FILE=/app/config/mcp.json
# AGENT_MCP_SERVERS_JSON=[{"name":"github","url":"https://mcp.example.com/mcp", ...}]
AGENT_MCP_ALLOW_INSECURE_HTTP=false
AGENT_MCP_ALLOW_PRIVATE=false
AGENT_MCP_DISCOVERY_TIMEOUT_SECONDS=10
AGENT_MCP_CATALOG_TTL_SECONDS=300
AGENT_MCP_TIMEOUT_SECONDS=30
AGENT_MCP_MAX_SERVERS=10
AGENT_MCP_MAX_TOOLS_PER_SERVER=100
```

JSON 文件优先于内联 JSON：

```json
{
  "servers": [
    {
      "name": "github",
      "url": "https://mcp.example.com/mcp",
      "enabled": true,
      "headers": {
        "Authorization": "Bearer ${GITHUB_MCP_TOKEN}"
      },
      "allow_tools": ["search_*", "get_*"],
      "deny_tools": ["delete_*", "admin_*"],
      "approval": "auto",
      "timeout_seconds": 30,
      "catalog_ttl_seconds": 300,
      "max_result_chars": 16000,
      "allowed_hosts": ["mcp.example.com"]
    }
  ]
}
```

字段说明：

- `name`：`[a-z0-9][a-z0-9_-]{0,15}`，同一配置内唯一。
- `url`：默认只允许 `https`；禁止内嵌凭证。
- `headers`：值支持 `${ENV_NAME}`；缺失时只跳过该 server，日志不输出值。
- `allow_tools` / `deny_tools`：`fnmatch` 匹配原始 MCP 工具名，deny 优先。
- `approval`：`auto`（默认）/ `always` / `never`。
- `allowed_hosts`：默认取 URL host，v1 不做通配符。

## 工具映射

- 名称：`mcp__<server>__<tool>`，非法字符替换为 `_`，总长不超过 64 字符。
- Schema：原样透传 MCP `inputSchema`，不经过 Pydantic 改写。
- 描述：清洗控制字符、截断 2048 字符，并加来源说明。
- 只读：`readOnlyHint=true` 且无 `destructiveHint` → `effect="read"`、可并发。
- 其余：`effect="write"`、串行执行。
- 注解只是提示；敏感 server 建议 `approval="always"`。

## 审批

```text
auto    → 只读工具直接执行；破坏性/未标注工具弹出确认
always  → 所有工具都确认
never   → 都不确认（仅建议用于完全可信的只读 server）
```

- 审批复用 `RunInteraction.ask()`，事件类型仍是 `user.question` / `user.answer`。
- 没有交互通道（前台 run、子 Agent、run 已结束）时 **fail closed**，不调用 server。
- 拒绝、超时、取消、已有待答问题都视为拒绝，且不重试。
- 审批工具串行执行，避免与「一个 run 只能有一个待答问题」冲突。

## 安全

- 默认 https；`http://` 需要 `AGENT_MCP_ALLOW_INSECURE_HTTP=true`。
- 连接前做 DNS 解析，拒绝 private/loopback/link-local/reserved/multicast/unspecified；需要内网时显式设置 `AGENT_MCP_ALLOW_PRIVATE=true`。
- `allowed_hosts` 严格匹配；禁止 URL 内嵌凭证；HTTP 客户端关闭重定向。
- header 密钥只在内存中解析；日志、错误、span 不输出值。
- 工具描述是 server 控制的 prompt 输入：截断、清洗、加来源前缀；用 deny 列表隐藏高风险工具。
- 所有结果包裹为 `<<<BEGIN UNTRUSTED MCP CONTENT>>>`，并转义伪造的边界标记。
- MCP 输出不自动写入记忆；如果未来接入 capture，`trust` 必须是 `tool`。
- MCP 只在 server 进程运行；executor 容器不接收 MCP 配置或密钥。

## 可观测性

- 复用 `tool.call` / `tool.result`、`agent_run_events` 和 `agent_run_spans`，不新增事件/span 类型。
- 通过 `mcp__` 前缀识别 MCP 工具。
- 日志字段：server、工具名、状态、耗时、字符数；不记录 URL、header、完整参数。
- 结果最终仍受 `AGENT_TOOL_RESULT_MAX_CHARS` 限制。

## 生命周期

- 每个进程一个 `MCPManager`，目录缓存按 server 保存。
- 每次发现/调用使用短连接会话；P1 再考虑持久会话池。
- 目录 TTL 默认 300s；刷新失败保留旧目录最多 24h，并按 server 指数退避（最长 300s）。
- 单个 server 失败只记录 warning，不影响整轮 run。
- `tools/list_changed` v1 不处理；需要每轮刷新时设 `catalog_ttl_seconds=0`。

## 故障排查

| 现象 | 检查 |
|---|---|
| 没有 MCP 工具出现 | `AGENT_MCP_ENABLED` 是否为 true；配置是否解析成功（启动日志会有 warning） |
| 工具被过滤 | `allow_tools` / `deny_tools` 是否匹配原始工具名 |
| `requires user approval` | 当前 run 是否有交互通道；是否设置了 `approval="always"` |
| 连接失败 | URL 是否 https、host 是否在 `allowed_hosts`、DNS/私网限制 |
| 工具名被截断 | `mcp__server__tool` 超过 64 字符时会追加哈希后缀 |

## 验证

```bash
cd server && ruff check app tests
make server-test
```

新增测试：`tests/test_agent_mcp_config.py`、`test_agent_mcp_tools.py`、`test_agent_mcp_results.py`、`test_agent_mcp_approval.py`、`test_agent_mcp_manager.py`，全部不依赖网络或真实 MCP server。
