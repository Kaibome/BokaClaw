# 本地 BokaClaw 技能

面向本机 Docker/Colima 部署的技能。放在这里而不是公开的 `skills/` 目录，
是因为它们描述的是本地基础设施，而不是对外发布的 SDK。Claude Code 会自动
加载本目录下的技能。

| 技能 | 使用场景 |
|---|---|
| [`bokaclaw-docker-stack`](bokaclaw-docker-stack/SKILL.md) | 构建或重建拆分镜像；启动、停止、迁移和健康检查本地的 Postgres + server + dashboard 容器 |
| [`bokaclaw-db-query`](bokaclaw-db-query/SKILL.md) | 列出表、查看表结构、统计行数，或对本地 `mem0_app` / `postgres` 数据库执行只读 SQL；无需在宿主机安装 psql |

脚本（技能文档中也有说明）：

```bash
.claude/skills/bokaclaw-docker-stack/scripts/build-images.sh [all|server|dashboard]
.claude/skills/bokaclaw-docker-stack/scripts/start-stack.sh
.claude/skills/bokaclaw-docker-stack/scripts/stop-stack.sh [--remove]
.claude/skills/bokaclaw-db-query/scripts/db.sh [flags] <tables|databases|schema|count|query>
```

所有脚本都会根据自身位置解析仓库根目录，因此可以从任意工作目录调用。
