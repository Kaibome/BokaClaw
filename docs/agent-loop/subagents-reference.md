# 子智能体（Subagents）清单

> 记录时间：2026-11-06
> 最后更新：2026-11-12（新增「五、输入 schema 与调用行规」「六、注册描述原文」）
> 适用范围：当前 WorkBuddy 会话已注册的 agent 类型
> 说明：受显示上限限制，另有少量已注册 agent 未在会话内列出，本文档只收录可见的 8 个。

---

## 概览

| # | 名称 | 类型 | 权限 | 一句话定位 |
|---|------|------|------|-----------|
| 1 | `general-purpose` | 通用执行 | 全工具（`*`） | 复杂多步任务、跨文件研究、需要迭代验证 |
| 2 | `Explore` | 研究（只读） | 只读 | 快速定位代码、回答"在哪实现/怎么串联" |
| 3 | `Plan` | 规划（只读） | 只读 | 出实施方案，识别关键文件与架构取舍 |
| 4 | `statusline-setup` | 配置 | 读写 | 调整 CLI 状态栏显示 |
| 5 | `doc-writer` | 文档 Stage 1 | 写作类 | 匹配领域写作 Skill，产出 Markdown 中间稿 |
| 6 | `doc-formatter` | 文档 Stage 2 | 排版类 | 把内容生成/仿写为 HTML 版式，恒产 html |
| 7 | `doc-converter` | 文档 Stage 3 | 转换类 | HTML 高保真转 .docx 并强制打开预览 |
| 8 | `sheet-agent` | 表格 | 表格读写 | 读写单元格、搜索、跑命令、算公式 |

---

## 一、通用型

### `general-purpose`

**职责**：处理多步骤研究、代码库探索与执行。

**工具权限**：全部工具。

**适用场景**
- 需求需要反复迭代、边查边改的任务
- 跨多个目录/文件的信息整合
- 需要较长自主执行链路的工作

**不适用**：单步查询、纯信息检索（直接搜比派 agent 便宜）。

---

## 二、研究与规划型（只读，不修改文件）

### `Explore`

**职责**：快速探索代码库，按模式找文件、按关键词搜内容、回答关于代码结构的提问。

**彻底度三档**
| 档位 | 含义 |
|------|------|
| `quick` | 基础搜索，快速给出位置 |
| `medium` | 中等探索，覆盖常见命名约定 |
| `very thorough` | 全量分析，跨多个位置与多种命名方式 |

**工具权限**：Read、ViewImage、Bash、PowerShell、Glob、Grep、Task*、WebFetch、WebSearch、Skill、SendMessage 等（无写入/编辑）。

**典型提问**
- "登录逻辑在哪个文件？"
- "API 端点之间怎么串起来的？"
- "这个仓库的构建流程是怎样的？"

### `Plan`

**职责**：软件架构师角色，设计实现方案并返回分步计划。

**特点**
- **只读**：只设计，不改文件，不退出 plan 模式
- 输出包含：步骤拆解、关键文件清单、架构取舍说明
- 适合"动手前先想清楚"的场景，方案交由调用方审阅后再实施

### `statusline-setup`

**职责**：配置 CodeBuddy Code 状态栏设置。

**工具权限**：读写 + 消息/团队类工具。

---

## 三、文档生产流水线（`workbuddy-docx` 三阶段）

这三个 agent 是**串行流水线**，职责边界严格，不互相越界。

```
Stage 1          Stage 2           Stage 3
doc-writer  →  doc-formatter  →  doc-converter
(md 中间稿)     (html 版式)       (docx 成品 + 预览)
```

### `doc-writer`（Stage 1 · 创作）

- 接收创作意图，加载匹配的 L2 领域 Expert Skill，或用 L1 通用写作兜底
- 输出 **Markdown**，是流水线中间产物，**不是最终交付物**，禁止直接给用户
- 只做「Skill 匹配 + 加载驱动」，不碰排版与导出

### `doc-formatter`（Stage 2 · 美化）

- 把具体内容生成/美化为新的 **HTML 版式**，恒产 html
- 两条路由：
  - 有可读参考文档 → `html-imitate` 提取风格仿写
  - 无参考文档 → `html-template` 选内置模板
- 内容来源可为 Stage 1 的 md、用户直接输入、或上传文档解析结果
- **不做注入/编辑**：把材料填进模板骨架、保留原格式产出 docx 属于编辑性质，走 `workbuddy-office-routing`，不进本 agent

### `doc-converter`（Stage 3 · 转换）

- 单一职责：把 HTML 高保真转换为 `.docx` 并**强制打开预览**
- 执行链：预检 → create → open → insert → save → verify（由 `convert_doc.py` 自包含完成）
- 失败时如实报错（JSON `error` 标注失败阶段），降级给出源 HTML 或 markdown_fallback，**不用本地代码兜底生成 docx**
- 转换完成即链路终点，后续编辑属独立 skill 职责

---

## 四、表格

### `sheet-agent`

**职责**：编辑、查询、分析、清洗、透视、排序、筛选、格式化既有 Excel/xlsx/xls/csv。

**调用要求**：必须传入完整 `local_path`，或 `file_id` / `sheet_id`。

**可做**
- 读写单元格区域、批量填公式、计算单条/整表公式
- 搜索数据、读取表结构
- 执行命令、增删行列、合并单元格、生成图表

**路由注意**
- 编辑/分析**既有**表格 → `sheet-agent`
- 从零生成**新**工作簿 → `workbuddy-sheet-generation`

---

## 五、输入 schema 与调用行规

### 前提：没有"每个 agent 各自的 schema"

所有子智能体都经由**同一个 Agent 工具**调用，入参结构完全一致。差异只在三处：
1. `subagent_type` 选谁
2. 该类型被分配的工具权限（写死在类型上，不由入参控制）
3. prompt 里必须交代的行规（不同职责要求不同，见下文对照表）

### 统一输入 schema

```jsonc
{
  "type": "object",
  "required": ["prompt"],          // 只有 prompt 是必填
  "properties": {
    "prompt":            "string  · 任务全文。子 agent 看不到任何对话历史，背景/路径/完成标准必须写全",
    "subagent_type":     "string  · default 'general-purpose'；可选 8 种",
    "description":       "string  · 3-5 词任务标签，缺省取 name 值",
    "name":              "string  · 唯一名，命名后可用 SendMessage 定向通信；'team-lead' 为保留名",
    "model":             "string  · 'default'(继承父级) | 'lite'(快省) | 'reasoning'(强推理) | 具体模型 ID",
    "mode":              "enum    · acceptEdits | bypassPermissions | default | plan(需方案审批) | dontAsk | auto",
    "max_turns":         "integer · >0，限制 agentic 轮数上限",
    "resume":            "string  · 传上次返回的 agent ID，从原上下文续接",
    "run_in_background": "boolean · 挂后台跑，用 TaskOutput 取结果",
    "team_name":         "string  · 已废弃，传了也会被忽略"
  }
}
```

**最小可用调用**：`{ subagent_type, description, prompt }`，其余全为可选。

**输出**：`{ agent ID, result }`
- `result` 只回传给主智能体，**不直连用户**——主智能体须自行核验后再转述
- 末尾的 Agent ID 可喂给 `resume` 续接同一上下文

### 各类型工具权限与 prompt 行规

| subagent_type | 工具权限 | prompt 必带的行规 |
|---|---|---|
| `general-purpose` | `*` 全工具 | 交代清楚边界即可；权力最大，也最容易跑偏 |
| `Explore` | 只读：Read/ViewImage/Bash/PowerShell/Glob/Grep/Task*/WebFetch/WebSearch/Skill，**无 Write·Edit** | 必须声明彻底度 `quick`/`medium`/`very thorough`；注明"只读" |
| `Plan` | 只读 + TeamCreate/TeamDelete，**不会退出 plan 模式** | 给目标与约束，要求返回步骤拆解 + 关键文件 + 架构取舍 |
| `doc-writer` | 会话注册信息未列具体工具（由 skill 指令驱动） | 只要内容意图；明令"输出 md 中间稿，不直接给用户" |
| `doc-formatter` | 同上 | 给内容 + 可选参考文档路径（决定仿写风格还是套内置模板） |
| `doc-converter` | 同上 | 只给 `html_output_path` 与 `output_docx_path` |
| `sheet-agent` | Read/ViewImage/AskUserQuestion/WebSearch/WebFetch + 8 个 sheetagent MCP 工具 | **必须**传完整 `local_path` 或 `file_id`/`sheet_id`，缺失即失败 |
| `statusline-setup` | Read/ViewImage/Write/Edit/ToolSearch/DeferExecuteTool + 通信类 | 说清要显示什么内容、什么格式 |

### 典型调用示例

**Explore（只读探索）**
```jsonc
{
  "subagent_type": "Explore",
  "description": "探索设计目录",
  "prompt": "你是只读探索代理，禁止修改/创建/删除任何文件。\n目标目录：<绝对路径>\n彻底度：medium\n背景（你看不到任何对话历史）：...\n请回答：1) 文件清单 2) .slide 格式 3) md 概要\n返回格式：分段作答，清单用表格，只报事实不给建议。"
}
```

**Plan（先出方案）**
```jsonc
{
  "subagent_type": "Plan",
  "description": "设计导出链路方案",
  "prompt": "目标：为现有 HTML 报告增加 PDF 导出。约束：不引入新依赖、不改现有模板结构。\n请返回：分步实施计划、涉及文件清单、备选方案的取舍说明。不要修改任何文件。"
}
```

**sheet-agent（表操作）**
```jsonc
{
  "subagent_type": "sheet-agent",
  "description": "汇总月度数据",
  "prompt": "文件 local_path：/abs/path/report.xlsx，工作表：Sheet1。\n任务：按 B 列月份分组汇总 E 列金额，结果写入 G1 起的新区域并加粗表头。\n不要改动原始区域。"
}
```

### 五个容易踩的点

1. **`description` 不是 agent 的自我介绍**——它只是本次调用的 3-5 词标签；真正的"描述信息"是本文档各章节里的职责定义。
2. **只读是工具级硬约束**，不是提示词软约束。Explore/Plan 没有 Write/Edit，让它改文件是做不到的，别指望靠 prompt 越权。
3. **prompt 必须自包含**。子 agent 拿到的只有：它自己的系统提示 + 这一条 prompt。对话历史、我刚读过的文件内容，它一概不知道。
4. **`mode: "plan"` 会要求方案审批**，适合"先看方案再放行"的场景。
5. **三个 doc-* agent 工具列表为空**，能力实际由各自 skill 指令 + 流水线脚本提供，不能按"没工具"理解成"不干活"。

---

## 六、注册描述原文

> 以下为 agent 注册信息中**逐字照录**的描述文本，未经改写、压缩或翻译。用于对照上文的人工提炼版本，判断提炼是否有偏差。
>
> 已剥离的部分：每条描述后附带的 `(Tools: …)` 工具清单、`sheet-agent` 的 `(workbuddy-sheetagent@workbuddy-builtin)` 来源后缀。工具清单见第五章表格。
>
> 注意：三个 doc-* agent 的 `(Tools: )` 在注册信息里为空括号。

### `general-purpose`

> handles multi-step research, codebase exploration, and execution. Use when the task needs iterative investigation or substantial autonomous work.

### `statusline-setup`

> Use this agent to configure the user's Codebuddy Code status line setting.

### `Explore`

> Fast agent specialized for exploring codebases. Use this when you need to quickly find files by patterns (eg. "src/components/**/*.tsx"), search code for keywords (eg. "API endpoints"), or answer questions about the codebase (eg. "how do API endpoints work?"). When calling this agent, specify the desired thoroughness level: "quick" for basic searches, "medium" for moderate exploration, "very thorough" for comprehensive analysis across multiple locations and naming conventions.

### `Plan`

> Software architect agent for designing implementation plans. Use this when you need to plan the implementation strategy for a multi-step task before writing code. Returns step-by-step plans, identifies critical files, considers architectural trade-offs. Read-only: it designs but does not modify files or exit plan mode; the caller reviews the plan and implements it.

### `sheet-agent`

> 表格子代理，调用时文件路径必须传入完整 local_path 或 file_id/sheet_id。

### `doc-writer`（Stage 1）

> 创作子 Agent（Stage 1）。接收 Orchestrator 派发的创作意图，加载匹配的 L2 领域 Expert Skill 或 L1 通用写作兜底。
>
> 输出的 Markdown 是流水线中间产物（供 Stage 2 排版消费），不是最终交付物——禁止直接输出给用户。
>
> 职责边界：只做 Expert Skill 匹配 + 加载驱动，不碰排版/输出/导出。

### `doc-formatter`（Stage 2）

> 文档美化子 Agent（Stage 2）。职责单一：把「具体内容」生成/美化为一份新的 HTML 版式，**恒产 html**。按「有无参考版式文档」两路路由：有可读 ref_doc（作**风格来源**）→ html-imitate 提风格仿写；无 ref_doc → html-template（选内置模板）。内容来源可为 Stage1 的 md、用户直接输入、或用户上传文档解析出的内容。**不做任何注入/编辑**——把材料填进模板骨架、保留模板原格式产 docx 的「对齐注入」属于编辑性质，**由编排层识别后直接转给独立 skill `workbuddy-office-routing` 处理，不进本 skill、不进本 Agent**。

### `doc-converter`（Stage 3）

> 格式转换子 Agent（Stage 3，本地通道）。职责单一：把 doc-formatter 产出的 HTML 高保真转换为 .docx 并**强制**打开预览。
>
> - 触发：Orchestrator 传入 html_output_path（.html 文件）
> - 执行：一键转换脚本 `convert_doc.py`（内部完成：预检 → create → open → insert → save 直接写交付路径 → verify，全流程自包含）→ 脚本成功后 `present_files` 打开成品预览；脚本失败时如实报错（JSON `error` 标注失败阶段）
> - 产出：由 `pipeline-state.yaml` 的 `output_docx_path` 决定（用户指定路径或 `stage3/<文件名>.docx`）
>
> 职责边界：只做「HTML→DOCX 格式转换 + 强制打开预览」，不做编辑、不做注入、不做创作。
>
> 转换失败时给出降级信息（源 HTML 或 markdown_fallback），不用本地代码兜底生成 .docx。
>
> 转换完成后本 Agent 即结束（本 skill 调用随之结束）；本 Agent 与本 skill 均不承担任何后续编辑（S3 即链路终点，编辑属独立 skill `workbuddy-office-routing` 的职责，由编排层在识别到 edit_only 时直接转交）。

---

## 使用机制说明

### 并行

相互独立的任务可在同一条消息里同时启动多个子智能体，减少总耗时。

### 后台执行

不需要立刻取结果时用后台模式（`run_in_background`），完成后再读输出，不必轮询等待。

### 组队协作

- 给每个子智能体起**唯一名字**，即可通过 `SendMessage` 定向通信
- 可共享任务列表，形成多角色协作
- `team-lead` 是保留名，不可占用
- 可用 `mode`（如 `plan` 需方案审批）与 `max_turns` 控制行为

### 结果归属

子智能体的返回值**只回传给主智能体，不直接呈现给用户**。因此主智能体必须：
1. 自行汇总与核对
2. 核验实际文件变更后才可报告"已完成"

---

## 选型速查

| 你的需求 | 选谁 |
|----------|------|
| 查一段代码在哪、怎么工作 | `Explore` |
| 动大手术前先要方案 | `Plan` |
| 多步骤、要边查边改地干活 | `general-purpose` |
| 从主题写到成稿 Word | `doc-writer` → `doc-formatter` → `doc-converter` |
| 改一份已有 Excel | `sheet-agent` |
| 从零做一份新表 | `workbuddy-sheet-generation`（skill，非 agent） |
| 查价格、查事实这类单点信息 | 不用子智能体，直接搜更快更省 |

---

## 相关文档

- [`subagent-call-walkthrough.md`](./subagent-call-walkthrough.md) — 一次真实调用的完整解剖：从决策、prompt 构造、工具分配、内部循环到结果核验的全链路，含真实输入输出。
  - 本文档讲**有哪些 agent、入参长什么样**；那份文档讲**一次调用实际怎么跑**，建议对照阅读。
