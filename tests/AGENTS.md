# 产品测试（`tests/`）

面向 BokaClaw 服务端和 Agent 的跨包测试。它们通过把 `server/` 目录前插到 `sys.path` 来导入
`app.*`，因此可以直接从仓库根目录运行，无需把服务端安装为包。

各包的本地测试与代码放在一起：

| 路径 | 覆盖范围 |
|------|--------|
| `server/tests/` | 记忆后端、工作区语法/索引/服务、评测 worker、golden 用例 |
| `agent-loop/tests/` | `bokaclaw-agent` 运行时、工具、子 Agent、Postgres 存储 |
| `dashboard/` | Biome/TypeScript 检查与 `pnpm build` |

## 命令

```bash
# 从仓库根目录运行
python -m pytest tests
python -m pytest tests/test_agent_runtime.py::test_memory_tools_are_scoped_to_the_user

# 服务端包测试
cd server && python -m pytest tests
```

仅支持 Docker 的包：如果宿主机没有 Python 环境，可挂载仓库，在 server 镜像内运行测试套件（镜像
不包含 pytest）：

```bash
docker run --rm -v "$PWD/server:/app" -v "$PWD/tests:/tests" -w /app \
  bokaclaw-server:test sh -lc 'pip install -q pytest pytest-mock pytest-asyncio && python -m pytest /app/tests /tests'
```

## 约定

- 文件名遵循 `test_<module>.py`；ruff 行宽 **120**。
- 测试不得依赖网络、实时数据库或嵌入模型。用 fake 替换 `app.main.get_memory_instance`（或相关
  服务）。
- 模块级配置（`app.core.auth`）在 import 时被缓存。当测试用不同环境重新加载 `app.main` 时，在同一
  fixture 中刷新这些全局变量。
- Mock 外部 SDK，绝不 mock 被测代码。一个只是把实现原样断言一遍的测试，比没有测试更糟。
- Bug 修复需要附带一个在未修复时会失败的回归测试。先写测试，看它失败。
