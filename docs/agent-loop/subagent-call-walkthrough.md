# 一次子 Agent 调用的完整解剖

> 记录时间：2026-11-06
> 本文以一次**真实调用**为例（非虚构），完整呈现从用户提问到最终答复的每一环输入输出。

---

## 场景

演示任务：探索工作区里的 `AgentSpan采集与离线评测设计/` 目录（13 个 .slide、1 个 .pptx、1 个 .md），弄清它的内容构成与文件关系。

---

## 阶段 0：用户问题进入

用户消息进入我的上下文。此时我持有的信息：

- 完整的对话历史（本会话前面几十轮）
- 系统提示（工具说明、行为规则）
- 工作区文件快照（只知道该目录有 15 个文件，不知道内容）

**决策**：目录有 15 个文件、需要通读多个文件并汇总结构结论，属于"中间过程噪声大、结论小"的任务 → 派 `Explore`（只读、快速探索型），而不是自己逐个读。

---

## 阶段 1：构造 prompt（我实际发送的原文）

子 Agent **看不到任何对话历史**，它只收到这一条消息。所以所有背景必须显式写进去：

````markdown
你是只读探索代理，禁止修改、创建或删除任何文件。

目标目录：/Users/pankaibo/WorkBuddy/PPT-Test/AgentSpan采集与离线评测设计/
彻底度：medium

背景（你需要知道的全部信息，你看不到任何对话历史）：
- 这是一个工作目录，里面据称有 15 个文件（13 个 .slide、1 个 .pptx、1 个 .md）
- 同级目录下还有 agent-span-evaluation.md（设计源稿）和
  agent-span-evaluation-v2.pptx，但它们不在你的探索范围内，只在需要对照时提及
- 我想知道这个目录的内容构成，用于判断它和源稿的对应关系

请回答三个问题：
1. 目录的完整文件清单：每个文件的名称、大小、修改时间
2. .slide 文件是什么格式（打开 1-2 个样本看内容），它们和最终 pptx 是什么关系
3. 目录里的 .md 文件讲了什么（给出 5 句话以内的概要）

返回格式：按上述三个问题分段作答，文件清单用表格，其余用要点。
不要给建议，只报告事实。
````

**prompt 的五个组成部分**：

| 部分 | 作用 | 缺失的后果 |
|------|------|-----------|
| 角色约束 | 声明只读 | 子 Agent 可能顺手改文件 |
| 目标 + 绝对路径 | 给出探索对象 | 无头绪乱扫 |
| 背景（显式声明"你看不到对话历史"） | 补齐它缺失的上下文 | 它不知道同级有源稿可对照 |
| 三个编号问题 | 定义完成标准 | 返回泛泛而谈的摘要 |
| 返回格式约束 | 表格/要点/只报事实 | 拿到自由散文，还得二次提炼 |

---

## 阶段 2：子 Agent 收到的模型输入

它收到的**不是**我的对话，而是两样东西：

1. **它自己的系统提示**：Explore 的角色定义（只读探索代理、彻底度机制说明、每个工具的使用规范）
2. **我上面那条 prompt**（作为它的第一条用户消息）

**分配给它的工具列表**（Explore 类型）：

```
Read, ViewImage, Bash, PowerShell, Glob, Grep,
TaskCreate, TaskGet, TaskUpdate, TaskList,
WebFetch, WebSearch, Skill, ToolSearch, DeferExecuteTool,
SendMessage, MessageColleague, SpeakInChannel, ChannelTasks,
CreateColleague, CreateChannel, ExitPlanMode
```

注意：**没有 Write 和 Edit**——只读是工具级别的硬约束，不是提示词级别的软约束。

## 阶段 3：Agent 工具的调用输入

这是我（主智能体）真正发出的那次函数调用，参数：

```json
{
  "subagent_type": "Explore",
  "description": "探索 AgentSpan 设计目录",
  "prompt": "（阶段 1 的全文）"
}
```

未使用的可选参数：`name`（本次不需要中途通信）、`mode`（Explore 本身只读）、`run_in_background`（需要立即取结果）、`max_turns`（任务有界）、`resume`（新任务非续接）。

## 阶段 4：Agent 工具的输出（真实返回，节选）

````markdown
## 1. 目录完整文件清单
（表格共 19 行，节选）
| 文件 | 大小 (字节) | 修改时间 |
| AgentSpan采集与离线评测设计.pptx | 61,709 | 2026-11-05 14:54 |
| DESIGN.md | 2,472 | 2026-11-05 14:43 |
| slides/01.slide | 2,152 | 2026-11-05 14:44 |
| ... | ... | ... |
13 个 .slide 修改时间从 14:44 递增到 14:54，与 .pptx 最终
修改时间一致，呈现逐页生成的时序。

## 2. .slide 文件格式及其与 .pptx 的关系
- 格式：类 JSX 的 DSL 纯文本，每文件一页，根元素
  <Slide style={{width: 1280, height: 720}}>，组件含
  Box/Text/Table，样式为内联 React style 对象
- 关系：.slidep/slidep.log 显示通过 @tencent/slidep 的
  upsert-dsl 命令逐页写入 pptx，.slide 是源码，
  .pptx 是编译产物

## 3. DESIGN.md 内容概要
- 是 PPT 的设计规范文档（主题、受众、13 页结构、
  版式参考、配色字号约定、anti_pattern 总则）

[Agent ID: agent-176fc565-912a-40a6-aedd-09efe7e81416]
````

返回末尾附 Agent ID——之后可用它续接该子 Agent（`resume`），历史不丢。

## 阶段 5：子 Agent 内部发生了什么（黑盒里的事）

它收到 prompt 后，在自己的上下文里跑独立的"分析→选工具→执行→观察"循环，大致过程：

1. `Bash ls -la` 列根目录 → 发现 slides/、.slidep/、assets/
2. `Bash ls` 列 slides/ → 得到 13 个 .slide 清单
3. `Read slides/01.slide`、`Read slides/06.slide` → 判定 DSL 格式
4. `Read .slidep/slidep.log` → 发现 upsert-dsl 命令记录 → 确立源码/产物关系
5. `Read DESIGN.md` → 提炼概要
6. 组织成约定格式，作为最终消息返回

这些中间步骤全部留在它的上下文里，**只有最终那条消息回到我这里**——这正是上下文隔离的意义：我拿到了结论，没有被 15 次工具输出污染。

## 阶段 6：我如何核验与综合

子 Agent 的返回是"自称完成"，不是"已核实"。我做了两件事：

**① 独立核验**（不信它的表格，抽查事实）：

```bash
ls -la "AgentSpan采集与离线评测设计/"        # → DESIGN.md 2472 字节 ✓
ls -la "AgentSpan采集与离线评测设计/slides/" # → 01.slide 2152 字节 ✓
head -5 "slides/01.slide"                    # → 确为 <Slide style={...}> JSX DSL ✓
```

三处抽查全部与它的报告吻合 → 采信。

**② 综合成答复**，遵循三条原则：

1. **转述关键结论，不粘贴原文**：用户要的是"目录构成 + 文件关系"，我把 19 行表格压缩成结构描述（根目录 2 文件 + slides/13 页 + 隐藏 .slidep/ + 空 assets/）
2. **保留最有价值的发现**：.slide 是 JSX 源码、pptx 是 slidep 编译产物、DESIGN.md 是设计规范——这三条是增量信息，必须出现在最终答复里
3. **标注不确定性**：报告里的判断（如"逐页生成时序"）属于它的推断，若要给用户承诺需区分"观察到的事实"与"推断"

---

## 全链路一图总结

```
用户提问
  │
  ▼
[我的上下文] 判断：多文件通读、结论小 → 派 Explore
  │
  ├─ 构造自包含 prompt（背景/目标/问题/格式四件套）
  │
  ▼
Agent 工具调用（输入：subagent_type + prompt）
  │
  ▼
[子 Agent 独立上下文] 系统提示(角色+工具规范) + 工具列表(无 Write/Edit)
  │   内部循环：ls → Read → Read log → Read md（噪声全部留在它那）
  │
  ▼
Agent 工具返回（输出：最终报告 + Agent ID）→ 只进我的上下文
  │
  ▼
[我的上下文] Bash 抽查核验 → 采信 → 压缩转述 + 保留增量结论
  │
  ▼
最终答复（用户只看到这一条）
```

**成本视角**：这次调用我付出了约 600 字的 prompt 构造成本，换回约 800 字的结构化结论；15 次中间工具输出（估计上万 token）全部被隔离在子 Agent 上下文里，没有进入我的对话。文件越多、中间输出越脏，这个交换越划算；反之，像查价格这种一次闭合的任务，就不值得。
