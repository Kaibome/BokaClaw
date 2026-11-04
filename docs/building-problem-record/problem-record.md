# BokaClaw 0→1 构建问题记录

本文件记录 BokaClaw（尤其是 `bokaclaw-agent`）从 0 到 1 构建过程中**实际遇到并分析过的问题**，目的是后续复用这套经验到其他项目，或直接用于汇报。

## 记录规则

1. **发现问题、分析问题时立即记录**，先写现象、证据、根因分析，并标注**发现时间**（`YYYY-MM-DD`），状态标为 `未解决`。
2. **问题解决后原地补齐**「解决措施」与「解决后效果」，补上**解决时间**，状态改为 `已解决`；只缓解未根治的标 `部分缓解`。
3. 每条记录的状态下方固定保留「发现时间 / 解决时间」两个字段；尚未解决的条目，解决时间为空或写 `—`，不得预先填写。
4. 每条记录必须带**可核对的证据**（run id、耗时、事件数量、报错原文、commit、文件路径），不写没有依据的推测；推测放进「根因假设」。
5. 新条目按编号追加，不覆盖历史结论；结论被推翻时追加「修正」段落并说明原因。

状态取值：`未解决` / `部分缓解` / `已解决`。

---

## P-01 Agent 猜 URL，抓取连续失败后整轮报错

- **状态**：已解决（commit `6493c05`）
- **发现时间**：2026-11-04
- **解决时间**：2026-11-04
- **场景**：用户提问「qwen 系列最适合个人开发者的模型」，期望联网查证价格与编码能力。
- **现象**：Agent 没有搜索，直接拼出看似合理的 URL 调 `WebFetch`；多次抓取失败后耗尽步数，接口返回 `agent_upstream_error`，消息为 `Agent exceeded the maximum number of steps.`，用户拿不到任何答案。
- **证据**：上一轮失败 run `b64c8499-…`；复测前后行为对比见 P-03 的 run id。
- **根因**：
  1. 工具集里只有「读已知 URL」的 `WebFetch`，没有「发现页面」的工具，模型只能靠记忆猜域名和路径；
  2. 系统提示没有禁止猜 URL，也没有要求先检索；
  3. 步数耗尽只有失败路径，没有收尾机制（见 P-03）。
- **解决措施**：
  - 新增 `WebSearch` 工具（`server/app/agent/dev_tools.py`），返回 `title/url/snippet`，让模型先搜再抓；
  - 系统提示改为「用 WebSearch 找页面、用 WebFetch 读内容，能搜到就不要猜 URL」；
  - 步数上限兜底（P-03）。
- **解决后效果**：
  - 复测首轮即调用 `WebSearch`（`{"query": "Qwen3-Coder 价格 编码能力 对比 个人开发者 2025", "max_results": 6}`），不再出现凭记忆构造 URL；
  - 后续 20 次工具调用全部来自 WebSearch / WebFetch 组合；
  - 整轮不再以 500 结束。
- **可复用结论**：只给「读取」类工具而不给「发现」类工具，模型一定会猜 URL；检索工具与抓取工具必须成对提供，并在提示词里明确分工。

---

## P-02 搜索后端在当前网络不可达（DuckDuckGo → Bing RSS）

- **状态**：已解决
- **发现时间**：2026-11-04
- **解决时间**：2026-11-04
- **场景**：为 `WebSearch` 选默认搜索后端。
- **现象**：最初接入 DuckDuckGo 的 html / lite 端点，实测**12 秒超时**，一个结果都拿不到。
- **根因**：当前网络环境下 DuckDuckGo 端点不可达或被限流（不是代码解析问题）。
- **解决措施**：
  - 探测多个候选后端：`cn.bing.com` 返回 200、约 0.5s；
  - 默认改用 Bing 的 RSS 视图 `https://www.bing.com/search?format=rss`，用 `ElementTree` 解析 `item/title/link/description`；
  - 删除 DuckDuckGo 解析器与相关测试，避免保留不可用路径；
  - 保留 SearXNG JSON 作为可配置后端（`AGENT_WEB_SEARCH_URL`），便于换网络环境时切换。
- **解决后效果**：单次搜索 0.6–1.1s，稳定返回 5–10 条带中文摘要的结果。
- **可复用结论**：搜索后端要按**实际网络**选型并做可达性探测，不能按文档默认；RSS/JSON 接口比 HTML 页面更稳定，解析成本也低。

---

## P-03 步数上限直接抛异常，用户拿不到「尽力而为」的答案

- **状态**：已解决（commit `6493c05`）
- **发现时间**：2026-11-04
- **解决时间**：2026-11-04
- **场景**：长任务需要多轮「搜索 → 抓取 → 再搜索」，`max_iterations` 很容易用尽。
- **现象**：`for ... else` 分支只有失败路径：持久化 `failed`、发 `RUN_END`、抛 `MaxIterationsExceeded`，即使前 9 轮已经收集到大量可用信息也全部作废。
- **根因**：运行时把「步数用尽」等同于「任务失败」，缺少降级收尾。
- **解决措施**：
  - SDK 新增 `AgentConfig.final_answer_on_max_iterations`（默认 `True`）；
  - server 新增 `AGENT_FINAL_ANSWER_ON_MAX_ITERATIONS`（默认 `true`）；
  - 步数耗尽时追加一条「不要再调用工具，用已有信息作答，无法核实的要说明」的提示（仅存在于模型视图，不落库），用 `tools=None, tool_choice=None` 发起一次最终请求；
  - 有答案 → 记为 `completed`，`stop_reason="max_iterations"`；无答案 → 保持原失败路径；配置为 `false` 可恢复硬失败。
- **解决后效果**：
  - 10 轮复测（run `eec31c48-5436-408d-8d2e-09ea78445c22`）：75.2s、10 轮、20 次工具调用、5 次工具失败，最终 `completed`，1413 字答案，含「未能核实的部分」；
  - 16 轮复测：122.2s、16 轮、30 次工具调用、7 次工具失败，最终 `completed`，2028 字答案；
  - 两次都**没有**再出现 `exceeded the maximum number of steps`。
- **可复用结论**：Agent 循环的「预算耗尽」应该是降级路径而不是异常路径；把最后一次无工具调用作为兜底，能显著提升任务可用性，成本只多一次模型调用。

---

## P-04 模型在 ReAct 循环中不会主动收敛（当前主要瓶颈）

- **状态**：已解决（2026-11-04 实现 S1+S3+S5，同一问题两次复测均自然收敛；改动尚未提交）
- **发现时间**：2026-11-04
- **解决时间**：2026-11-04
- **现象**：即使把 `AGENT_MAX_ITERATIONS` 从 10 提到 16，模型仍然**每一轮都发起新的搜索或抓取**，最后一轮仍在调工具，最终答案始终来自 P-03 的兜底路径，而不是模型自己判断「信息够了」。
- **证据**：

  | 配置 | 耗时 | 轮次 | 工具调用 | 工具失败 | 最后一轮 | 结束方式 |
  |---|---|---|---|---|---|---|
  | 10 轮 | 75.2s | 10 | 20 | 5 | 2×WebFetch | 兜底终答 |
  | 16 轮 | 122.2s | 16 | 30 | 7 | 2×WebFetch | 兜底终答 |

  16 轮版本增加了 6 轮、47 秒，答案从 1413 字增至 2028 字（拿到了 OpenRouter 的真实报价），但**没有改变「依靠兜底收尾」这一事实**。

- **根因（事实与高置信推断分开标注）**：
  1. **终止是隐式的**（事实）：循环只在模型「不返回任何 tool_calls」时自然结束。工具充足时，模型的默认倾向是「再查一次」，而不是主动宣布完成；我们没有显式的收尾动作或完成信号。
  2. **事实被上下文压缩丢掉**（高置信估算，未取到日志证据）：16 轮 run 的工具结果合计 **127,199 字符 ≈ 50,880 tokens**（按仓库估算公式 `ascii*0.4 + 非ascii*1`），超过 `AGENT_CONTEXT_MAX_TOKENS=32000`，压缩器会把早期搜索结果和抓取正文折叠进摘要。模型在后续轮次看不到早前拿到的价格/型号，只能重新搜索、重新抓取。该 run 未开 `AGENT_CONTEXT_DEBUG`，所以没有 `context_compressed` 日志；估算值与 P-06 的观察一致。
  3. **没有「无进展」检测**（事实）：7 次抓取失败（404 / 空页 / 超时）、多组查询返回同一批页面、重定向到列表页的 URL，都没有被识别为「没有新信息」，模型继续换关键词或换 slug。
  4. **工具反馈不可行动**（事实）：`WebFetch` 404 只回 `request failed with HTTP 404`；被重定向到列表页的 URL 未标记；`WebSearch` 不标注「该结果前面已经抓过 / 结果与上次重复」。
  5. **任务没有可判定的完成标准**（事实）：开放式研究问题（「哪个最适合」）不存在唯一权威答案，模型缺少「证据足够」的判定依据，倾向于无限逼近。

- **外部调研（2026-11，避免闭门造车）**：

  | 系统 / 来源 | 做法 | 关键细节 |
  |---|---|---|
  | LangChain `AgentExecutor` | `max_iterations` + `early_stopping_method="generate"` | 达到上限后追加一次「I now need to return a final answer…」的模型调用；`"force"` 只返回固定文案。与我们的 P-03 等价。已知缺陷：新版 RunnableAgent 不支持 `generate`（#16263），生成结果可能是 `Action:` 字符串（#16374） |
  | OpenAI Agents SDK | `max_turns` + `error_handlers={"max_turns": ...}` | 正常终止 = 模型输出无 tool call 的 final output；`max_turns` 超限默认抛 `MaxTurnsExceeded`，可配置为返回受控终答；`reset_tool_choice` 默认在首次工具调用后清空强制选择，避免「被迫一直调工具」 |
  | AutoGen AgentChat | 可组合终止条件 | `MaxMessageTermination`、`TextMentionTermination`、`TimeoutTermination`、`TokenUsageTermination` 用 `|` / `&` 组合，语义终止与硬上限并存 |
  | LangChain `open_deep_research` | `ResearchComplete` 工具 + 硬上限 | `max_researcher_iterations=6`、`max_react_tool_calls=10`；满足「超迭代 / 无 tool call / 调用 ResearchComplete」任一即结束 |
  | Jina `node-DeepResearch` | `answer` 动作 + 评估器 + Beast Mode | `answer` 需通过评估（`numEvalsRequired`）；预算耗尽仍无合格答案时进入 Beast Mode：禁用除 `answer` 外所有动作，强制生成 |
  | OpenHands `StuckDetector` | 4 类卡死模式 + nudge | 同一 action-observation 重复 4 次、同一 action-error 重复 3 次、agent 独白 3 次、A/B 交替 6 轮即判定；较新版本先 nudge 警告再硬停 |
  | PraisonAI `loop_detection` / `loopdetect`(Go) | 哈希指纹 + 分级 | 工具参数 canonical JSON → SHA-256；检测 `generic_repeat`、`no_progress`（参数+结果都相同）、`ping_pong`、全局熔断、压缩后守卫 |
  | 论文《Judged Useless, Queried Anyway》(arXiv 2610.06191) | 连续无用结果后强制整合 | 智能体在 97–100% 的情况下能判断结果「无用」，却仍继续查询；**连续 5 次无用结果后强制执行一次整合/作答**，可跨模型修复该问题 |
  | 论文《When Agents Do Not Stop》(arXiv 2607.01641) | 68 个真实无限循环案例 | 38.2% 属于「终止由模型控制」；结论：**上限必须覆盖真正重复的那条路径**，内层上限盖不住外层循环 |
  | 论文 CaRT (arXiv 2510.08517) / ACL 2026 证据感知终止 | 训练/提示「何时信息足够」 | 仅靠提示词只能改变「何时停」，效果有限；显式的充分性/证据判据才是主杠杆 |
  | Anthropic《Building effective agents》 | 显式停止条件 + 迭代上限 + 人工检查点 | 原话是「任务不会可靠地自行终止」，所以必须显式设限；提示词是 nudge，不是保证 |

- **候选方案（按「成本/收益/风险」排序，均未实施）**：

  | ID | 方案 | 成本 | 预期效果 | 风险 | 依据 |
  |---|---|---|---|---|---|
  | S1 | **证据账本（Fact Ledger）**：run 内维护一个紧凑的「已确认事实 + 来源 URL + 抓取状态」块（限 2–3k tokens），每轮随消息注入，压缩只压缩原始 tool 消息、不压缩账本 | 中 | 高：直接消除「压缩导致事实丢失 → 重查」 | 需要定义事实提取规则；账本质量决定上限 | 《When Agents Do Not Stop》建议的 Execution State 重注入；CaRT |
  | S2 | **无进展/循环守卫**：对 `(tool, canonical args)` 与结果做哈希；精确重复直接返回缓存/提示；连续失败或无新信息 3 次 → nudge，5 次 → 强制终答；A/B 交替 6 轮 → 终止 | 中 | 高：命中本次 7 次失败与重复抓取的主要症状 | 阈值误伤合法轮询；变体参数（改写 query、换 slug）能绕过哈希 | OpenHands 阈值；PraisonAI / loopdetect；arXiv 2610.06191 的「5 次」 |
  | S3 | **预算感知 + 渐进收尾**：每轮在模型视图注入 `Step X / N`；剩余 2 轮起提示「停止检索、开始综合、可标注缺口」；保留现有终答兜底 | 低 | 中：让模型在事实还在视图里时提前整合 | 输出前缀每轮变化，可能降低 prompt 缓存命中 | OpenAI Agents SDK 的 turn 机制；OpenRouter `strictFinalResponse`；sinan-agentic-core 的 `reminder_at` |
  | S4 | **显式 `submit_answer` 工具**：模型调用该工具即结束循环，内容作为最终答案；可加「答案评估 → 不合格则退回重试」 | 中 | 中：显式动作比「什么都不返回」更容易被工具型模型触发 | 可能过早调用；社区有反向经验（sttp-ai 从 finish 工具改回「无 tool call 即终止」） | LangChain open_deep_research 的 `ResearchComplete`；Jina 的 `answer` + evaluator |
  | S5 | **工具反馈可行动化**：404 提示「该 URL 不存在，改用 WebSearch」；重定向到列表页的 URL 标记为不可用；WebSearch 去重并标注「前面已抓取 / 与上次结果重复」 | 低 | 中：减少盲目换 slug/换关键词 | 只是引导，不能保证模型照做 | Anthropic「fix tool ergonomics」；P-06 的重定向观察 |
  | S6 | **充分性评估器**：在预算过半或无进展时，用一次廉价模型调用判断「现有证据是否足以回答，缺什么」，足够则强制终答，不足则把缺口列表注入下一轮 | 中高 | 高：最接近「知道何时够了」 | 额外调用成本；评估器本身可能误判；需评测集验证 | CaRT；ACL 2026 证据感知终止；Jina evaluator |

  **明确不建议**：
  - 单纯提高 `max_iterations`：10 → 16 已实测，只是多跑 6 轮、多 47 秒，仍靠兜底收尾；
  - 长期开启 `tool_choice="required"`：模型会被迫每轮调用工具，永远无法自然结束（OpenAI 社区已报告该问题）；
  - 只改提示词：研究一致认为提示是 nudge 而非保证。

- **推荐实施顺序**：
  1. **S1 + S3 + S5**（一次提交）：证据账本解决事实丢失，预算感知让模型提前收尾，工具反馈减少无效尝试。三者都不改循环结构，风险最低。
  2. **S2**：加入哈希指纹与分级处置（nudge → 强制终答），阈值先按 OpenHands 的 4/3/6 与论文的「连续 5 次无用结果」设置。
  3. **S6**（可选）：如果前两步仍不能把「兜底终答率」降下来，再加充分性评估器。
  4. **S4**（A/B）：作为对照组验证「显式 finish 工具」是否比隐式终止更稳。

- **验收指标**（复测同一问题 + 3–5 个研究型问题）：
  - `stop_reason != "max_iterations"` 的比例（目标：>80%）；
  - 平均轮次、工具调用数、唯一 URL 数、重复调用数；
  - 压缩触发次数与「压缩后重新抓取同一 URL」次数；
  - 答案中「未能核实」比例与人工可接受度。

- **解决措施（2026-11-04，实现第一批 S1 + S3 + S5）**：
  - **S1 证据账本**：新增 `agent-loop/src/bokaclaw_agent/agent/ledger.py`。`ToolLedger` 按 `(工具名, canonical JSON 参数)` 去重，记录每次调用的参数、成功/失败、结果摘要（搜索前 3 条标题+URL、抓取字符数与正文片段、错误信息）和重复次数；上限 24 条 / 2,500 字符，超出时先丢最旧条目并标注「N earlier calls omitted」。账本在**上下文压缩之后**拼进模型视图，因此早前事实不会被摘要吞掉；重复调用只加计数，不展开。
  - **S3 预算感知**：每轮在模型视图追加 `Step X of N`；剩余 1 轮提示「不要开启新研究，现在就答，并说明缺口；若缺一条关键事实，只允许最后一次工具调用」，剩余 0 轮提示「不要再调工具」。提示随账本合并为一条尾部 `user` 消息，前缀 `[Runtime context, not a new user request]`，**只存在于请求视图，不落库**；步数耗尽时的强制终答请求也带上账本。
  - **S5 工具反馈可行动化**（`server/app/agent/dev_tools.py`）：
    - `WebFetch` 的 HTTP ≥400 错误改为「页面不存在或不可用，请用 WebSearch 找当前 URL，不要猜」；
    - 重定向改变路径时返回 `requested_url` + `note`（「可能已被重定向到列表页，详情页不存在，用 WebSearch 找」）；
    - `WebSearch` 按 run 缓存完整查询（忽略大小写/空白），重复查询直接复用并标记 `cached: true` + 「不要重复运行」；
    - 搜索结果若已被本轮其他查询返回过标 `previously_seen`，若已被抓取过标 `already_fetched`；全部结果都见过时给顶层提示「不要再次搜索，直接抓取或作答」。
  - **配置**：SDK 默认关闭（`fact_ledger=False`、`step_budget_notice=False`）；server 默认开启：`AGENT_FACT_LEDGER=true`、`AGENT_FACT_LEDGER_MAX_CHARS=2500`、`AGENT_STEP_BUDGET_NOTICE=true`（`false`/`off` 可关闭）。文档同步 `agent-loop/README.md`、`server/README.md`、`server/.env.example`。
  - **测试**：新增 `agent-loop/tests/test_ledger.py`（11 条）与运行时/压缩交互用例，`agent-loop` 全量 141 passed / 1 skipped；server 侧新增 WebSearch 去重、重定向、HTTP 错误、环境变量与 `build_runtime` 透传用例，`tests/test_agent_{runtime,router,runs,dev_tools}.py` 共 124 passed。

- **解决后效果（同一问题、同一模型 `deepseek-flash`、同一镜像，仅代码不同）**：

  | 版本 | run_id | 配置 | 耗时 | 工具调用 | 结束方式 | `finish_reason` |
  |---|---|---|---|---|---|---|
  | 改前 | `eec31c48` | 10 轮 | 75.2s | 20 | 兜底终答 | `max_iterations` |
  | 改前 | `9736ad44` | 16 轮 | 122.2s | 30 | 兜底终答 | `max_iterations` |
  | 改后 | `ff4c7f5c` | 10 轮 | 29s | 10 | 模型自然收尾 | `final` |
  | 改后 | `e7e8f61b` | 16 轮 | 25s | 10 | 模型自然收尾 | `final` |
  | 改后 | `0c475da4` | 16 轮 | 78s | 23 | 第 16 轮（最后一步提示后）自然收尾 | `final` |

  - 两次「25–29 秒 / 10 次工具调用」的复测中，全部查询与 URL 无重复，模型在预算内主动作答；`0c475da4` 是更保守的一次采样：模型一直查到第 16 轮，但读到「最后一步」提示后停止调工具，仍未落入兜底路径。
  - 两次短复测都没有触发上下文压缩（`context_compressed` 无日志），因此账本「防摘要丢事实」的作用由单元测试验证（压缩后账本仍在、且带 `(x2)` 计数），端到端样本尚未覆盖；`0c475da4` 的压缩日志显示压缩确实会发生（第 10–16 轮 before 32k→61k tokens，after 2k–19k）。
  - 答案质量：10 轮版本给出 `qwen3.7-plus` 为主力、`qwen3.8-flash` 备选的结论与价格表；16 轮版本给出 `qwen3.8-flash` 为主力。两次结论不同，说明开放式问题的「正确答案」仍有模型采样差异，但都基于本轮抓到的可读来源，并标注了未核实项。
- **仍未实施**：S4（显式 `submit_answer` 工具）、S6（充分性评估器）。`0c475da4` 说明模型仍可能把预算用满才发现该收尾，S2 已作为下一批护栏实现（见下）。

- **解决措施（2026-11-04，第二批 S2 + 三处工具反馈修复）**：
  - **S2 无进展/循环守卫**：新增 `agent-loop/src/bokaclaw_agent/agent/progress.py`。
    - 证据指纹按**结果内容**而非调用参数计算：搜索取排序后的 URL 集合；抓取/读取取正文 SHA-256（不同 URL 返回同一正文＝无进展）；命令取 stdout；错误、空结果、空对象一律不计为证据。参数改写、换 slug 但结果相同的循环能被抓住，而读取同一文件的不同区间仍算进展。
    - 连续无进展达到 `progress_nudge_after`（默认 3）时，在模型视图追加「换一个来源或现在作答」提示；达到 `progress_stop_after`（默认 5）时结束循环，发一次禁用工具、带「不要调工具、说明未核实项」提示的请求。提示只进请求视图、不落库。
    - 停止发生在**一轮工具调用全部执行完之后**，保证 assistant `tool_calls` 与 `tool` 消息始终成对，不产生非法会话。
    - 成功终答标记 `stop_reason="no_progress"`；若强制请求仍无文本，落库为 `failed` 并抛新增的 `NoProgressError`，server 映射为错误码 `agent_no_progress`（不是 `agent_upstream_error`）。
    - 配置：SDK 默认关闭（`progress_guard=False`、`progress_nudge_after=3`、`progress_stop_after=5`，后者不得小于前者）；server 默认开启 `AGENT_PROGRESS_GUARD=true`、`AGENT_PROGRESS_NUDGE_AFTER=3`、`AGENT_PROGRESS_STOP_AFTER=5`，server 侧把 stop 夹到不小于 nudge，避免非法配置让整轮失败。
  - **HTML 表格单元格分隔**（清单 2）：`_html_to_text` 在 `td`/`th` 开始标签处插入 ` | `，行内保留 `Region | Price`，不再把价格表拍平成 `Qwen3.8-Flash0.82.7`。
  - **缓存命中的抓取结果改为摘要**（清单 5）：重复 `WebFetch` 命中缓存时，正文超过 1,200 字符只返回前 1,200 字符 + 截断标记，并提示可用 `full=true` 取全文（`digest: true`）；短页面原样返回。避免「为避免重复请求而重发 16k 正文」。
  - **搜索缓存 key 纳入 `max_results`**（清单 6）：key 从 query 改为 `(归一化 query, 截断后的 limit)`，同一 query 换 limit 会真正重新搜索；同 query 同 limit 仍直接复用。
  - **测试**：新增 `agent-loop/tests/test_progress.py`（11 条：证据指纹、排序无关、同正文不同 URL、缓存命中、错误/空结果、阈值夹取、nudge 只发一次）；`tests/test_runtime.py` 新增 5 条（nudge 出现时机、停止后强制终答、证据更新后重置、默认关闭、空终答抛 `NoProgressError`）；server 侧新增表格分隔、缓存摘要、搜索 key、环境变量与 `build_runtime` 透传、`agent_no_progress` 映射用例。agent-loop 全量 156 passed / 1 skipped；server agent 相关 131 passed。

- **解决后效果（2026-11-04 第二批，同一问题、`deepseek-flash`、16 轮上限、仅代码不同）**：

  | run_id | 耗时 | 轮次 | 工具调用 | 压缩 | 结束方式 |
  |---|---|---|---|---|---|
  | `eed19c6a` | 58.7s | 10 | 17（9 搜索 / 8 抓取） | 3 次（第 8–10 轮） | `final`，模型自然收尾 |
  | `f46c9c49` | 29.1s | 6 | 12（5 搜索 / 7 抓取） | 0 次 | `final`，模型自然收尾 |

  - `eed19c6a` 第 5 次调用用带 `#text-generation` 的同一 URL 重抓，命中缓存并返回 1,289 字符摘要（首次 4,027 字符，`digest: true`），没有再次发请求或重发全文；第 6–8 次搜索的结果全部与早前重复并带「Every result was already returned…」提示，按 3/5 阈值在第 8 次触发一次 nudge，模型随后转向抓取新的中文价格页。
  - `f46c9c49` 无重复 URL、无压缩，6 轮内自然收尾；两次都没有落入步数兜底，也没有触发「强制终答」。
  - 两次采样均未触发 S2 的 `progress_stop_after`，强制终答与 `NoProgressError` 路径目前只由单元测试覆盖；单次样本方差大（第一批同类样本为 10 次调用 25–29s 与 23 次调用 78s），S2 的边际收益需要清单第 9 项的多问题自动评测来量化。

- **后续优化清单（2026-11-04 分析；第 2、5、6 项已在第二批实施）**：
  1. **压缩过于频繁（已有日志证据）**：`0c475da4` 第 10–16 轮**每轮**触发一次压缩，其中 3 次调用模型生成摘要。原因是每轮工具结果增长 8k–16k tokens，压缩后视图又很快回到预算上方。可选：降低 `AGENT_TOOL_RESULT_MAX_CHARS`、压缩后只保留最近 1–2 条原始 tool 消息（细节交给账本）、或提高 `SUMMARY_MIN_FOLD_TOKENS` 并设置「距上次压缩新增不足 N tokens 时不重复摘要」。
  2. **HTML 表格被拍平（代码证据）**：`_html_to_text` 只在 `tr` 处换行，`td`/`th` 之间没有分隔符，价格表可能变成 `Qwen3.8-Flash0.82.7`。建议在单元格边界插入 ` | ` 或制表符。价格类页面几乎全是表格，收益直接。
  3. **WebFetch 截断诱发变体重抓**：正文到 16k 字符被截断后，模型会去抓 `.md` / `en/` 版本补全。建议提供「按关键词/章节提取」参数，或返回标题大纲，而不是让模型换 URL 重抓。
  4. **URL 变体归一化**：`model-pricing` / `model-pricing.md` / `en/model-studio/model-pricing` 视为同页；同域连续 3 次 404 或列表页兜底后做域名级熔断。
  5. **缓存命中返回全文**：`WebFetch` 缓存命中仍重发最多 16k 字符正文，浪费上下文；可返回 1–2k 摘要 + 「全文已在早前工具消息中」，细节由账本兜底。
  6. **搜索缓存 key 忽略 `max_results`**：同一 query 先 `max_results=5` 再 `max_results=10` 会拿到 5 条。把 limit 纳入 key，或命中条数不足时重新搜索。
  7. **可观测性**：`MODEL_REQUEST` 视图不落库，排查「为什么还在查」只能看服务器日志。建议把每轮 `iteration`、压缩事件数、账本字符数写进 run metadata，并在 UI 上区分「自然收尾 / 步数兜底」。
  8. **可复用研究结论**：同一问题已被重复问 4 次，每次从零研究。可在成功运行后把「结论 + 来源 + 抓取时间」写入 mem0 记忆，下一轮先检索再决定是否重查。
  9. **评测**：用 3–5 个固定研究问题 × 多次重复，自动统计 `finish_reason`、轮次、工具调用、重复 URL、压缩次数，替代单次人工复测。

---

## P-05 目标信息源不可读：客户端渲染、404、超时

- **状态**：部分缓解
- **发现时间**：2026-11-04
- **解决时间**：—
- **现象**：官方与第三方页面大量不可用，导致 Agent 拿不到权威定价：
  - `https://qwen.ai/home`：HTTP 200，但正文由客户端渲染，无可读文本，`WebFetch` 判定失败；
  - `https://huggingface.co/Qwen`：30 秒超时；
  - `https://www.qwencloud.com/pricing`、`https://www.qianwenai.com/pricing`：404；
  - `qianwenai.com/models/qwen3.8-flash-next`、`qwen3.5-coder`：404。
- **根因**：SPA 客户端渲染、网络超时、站点路由变更/不存在。
- **已采取措施**：
  - `WebFetch` 检测「200 但无可用正文」并返回明确错误，而不是把空壳当成功；
  - 下载上限 1MB、输出上限 20 万字符、30 秒超时；
  - 同一 run 内按 URL 缓存抓取结果（commit `c3d3334`），避免重复请求同一页面。
- **解决后效果**：失败会以结构化错误返回给模型，模型在终答中明确列出「未能核实的部分」；但官方价格页仍然拿不到，答案依赖 OpenRouter 等可读的第三方数据。
- **后续方向**：接入渲染服务或官方标价 API；优先选取可读的镜像/聚合页；对同一域名连续 404 做熔断。

---

## P-06 同一问题反复抓取相同网页（WebFetch 无去重）

- **状态**：已解决（commit `c3d3334` 精确 URL 去重 + 2026-11-04 的查询级去重与结果标注）
- **发现时间**：2026-11-04
- **解决时间**：2026-11-04
- **场景**：用户连续 4 次提交同一问题「qwen系列模型最适合用于个人开放者的模型是哪个？综合考虑价格和编码能力」，涉及会话 `74a72c07-880c-4360-8b52-11a4c008d081` 与 `01211c7e-a140-4760-877d-37f8311131a7`。
- **现象**：Agent 在单轮 ReAct 循环内、以及多次重试之间，反复调用 `WebFetch` 抓取相同页面；部分页面还以 HTML / `.md` / 英文站等不同形式重复读取。
- **证据**（`agent_run_events` 按 `tool_call_id` 配对统计）：

  | run_id | 时间 (UTC) | 结束状态 | WebFetch 次数 | 唯一 URL | 精确重复 |
  |---|---|---|---|---|---|
  | `a9b1bf99-f753-434b-83d2-ce06b6eae688` | 06:30:16 | failed (`agent_upstream_error`) | 19 | 14 | **5 个页面各抓 2 次** |
  | `f0332c95-192c-402a-9ea6-e21fc09277aa` | 06:33:16 | interrupted | 11 | 11 | 0 |
  | `b64c8499-185e-4b29-b694-41c965a6c3f3` | 07:16:47 | failed (`agent_upstream_error`) | 16（另有 2 次 Bash `curl`/`python` 抓同一批价格页） | 15 | `qwen-coder` ×2，另有大量同页 `.md` / 英文站变体 |
  | `375a6a87-3e83-49d2-866c-3aa2fac6ade8` | 07:18:59 | cancelled（21.7s） | 11 | 11 | 0 |

  `a9b1bf99` 的精确重复（轮次为 `iteration`）：

  | URL | 抓取轮次 |
  |---|---|
  | `.../model-studio/coding-plan` | 3、6 |
  | `.../model-studio/qwen3-8-flash` | 4、8 |
  | `.../model-studio/qwen3-coder-flash` | 6、9 |
  | `.../model-studio/qwen3-7-plus` | 5、10 |
  | `.../model-studio/qwen3-coder-plus` | 7、10 |

  `b64c8499` 的同页不同形式：`model-pricing` → `model-pricing.md` → `en/model-studio/model-pricing.md`；`models` → `models.md`；`text-generation` → `text-generation-model.md`；`qwen-coder` 在第 7、23 次调用各抓一次，之后又抓 `qwen-coder.md`。

  跨 4 次运行重复次数：`models` 4 次、`qwen-coder` 4 次、`coding-plan` 4 次、`qwen3-8-flash` 3 次、`qwen3-7-plus` 3 次。

- **根因（事实）**：
  1. `WebFetch` 底层 `_fetch_url`（`server/app/agent/dev_tools.py`）没有任何 URL 缓存或去重，每次调用都真实发起 HTTP 请求；
  2. 部分页面返回后被截断（`models-price`、`qwen-coder`、`token-plan` 等均达到 15,803 字符上限，`truncated=true`），模型为拿全信息改用 `.md` / 英文站等变体重抓；
  3. `qwen3-coder-plus`、`qwen-coder-plus` 两个 slug 被站点重定向回 `models` 列表页：返回内容长度 3,214 字符，与 `models` 页完全一致，`result.url` 也变成 `.../models`；模型以为没拿到详情，继续换 slug 尝试。
- **根因假设**：
  1. 单轮重复集中在第 6–10 轮，而相同页面在第 3–5 轮已经抓过；`AGENT_CONTEXT_MAX_TOKENS=32000`，该 run 抓回的正文累计约 15 万字符，压缩器很可能把早期 tool result 折叠进摘要，模型在模型视图里看不到原文，只能重新抓取；
  2. 系统提示没有「已抓过的页面直接复用」的约束，也没有收敛判定（见 P-04）。
- **解决措施**（commit `c3d3334`）：
  - `build_dev_tools` 内为每次 run 创建独立的 `fetch_cache`，`WebFetch` 闭包共享；
  - `_normalize_fetch_url` 生成缓存 key：忽略 fragment，scheme/host 小写；path、参数、query 保留原样；
  - 命中缓存直接返回原结果并标记 `"cached": true`，不再发请求；
  - 失败抓取在写入缓存前抛异常，**错误不缓存**，后续重试仍可真正访问网络；
  - 缓存作用域随 `build_runtime` / `build_dev_tools` 的每次调用重建，因此不跨 run 共享；
  - 补充 3 个回归测试（单 run 复用、跨 run 隔离、失败不缓存），并更新 `server/README.md`。
- **解决后效果**：
  - 单元测试确认：同一 URL 重复调用只产生 1 次网络请求，第二次返回 `cached: true`；两个 `build_dev_tools()` 实例各请求 1 次；连续两次 404 仍请求 2 次；隔离 checkout 中 `tests/test_agent_dev_tools.py` 37 passed；
  - 线上同一问题的复测（2026-11-04，S1/S3/S5 上线后）：`ff4c7f5c`（10 轮上限，29s，10 次调用）与 `e7e8f61b`（16 轮上限，25s，10 次调用）中 5 次 WebFetch 的 URL 全部唯一，0 次重复；更保守的 `0c475da4`（78s，23 次调用）中也没有精确重复的 URL；
  - `WebSearch` 现在同样按 run 去重：重复查询直接返回 `cached: true`；已被本轮其他查询返回过的结果标 `previously_seen`，已抓取过的标 `already_fetched`，全部见过时给顶层提示；
  - 需要说明：缓存只消除「完全相同 URL」的重复网络请求。若上下文压缩让模型看不到早前结果，模型仍可能再次调用同一 URL（此时命中缓存、不再发请求）；`.md` / 英文站变体、跨 run 重试仍会重新抓取。
  - 2026-11-04 第二批修复后：缓存命中不再重发全文，超过 1,200 字符只返回摘要并提示 `full=true`（避免重复调用再次占用 16k 上下文）；搜索缓存 key 纳入 `max_results`，换条数会真正重新搜索；无进展守卫（S2）在连续 5 次没有新证据时强制终答，覆盖「换 slug / 换关键词但结果相同」的变体重抓。
- **后续方向**：
  - `.md` / 英文站变体的归一化（同源同路径去扩展名）尚未实现；
  - 对重定向到列表页的 URL 现在会返回 `note` 提示，但未做跨 slug 熔断。

---

## P-07 跨会话重复研究同一问题（结论不沉淀）

- **状态**：部分缓解（2026-11-04 已实现并完成跨会话复测；注入链路已验证，工具调用未观察到减少）
- **发现时间**：2026-11-04
- **解决时间**：—
- **场景**：同一用户反复问「qwen 系列最适合个人开发者的模型」；P-06 的 4 次 run 分布在多个会话中。
- **现象**：每次提问都从零开始 `WebSearch` → `WebFetch`，前一晚已经得到的结论、来源和抓取时间完全没有被下一次运行使用；同一批 URL 被跨 run 反复抓取（P-06 统计：`models` / `qwen-coder` / `coding-plan` 各 4 次）。
- **根因**：
  1. 对话历史只按 `conversation_id` 读取，跨会话不可见；
  2. mem0 里只有模型显式调用 `save_memory` 才会写入的「用户事实」，没有「本轮研究结论 + 来源 + 抓取时间」的结构化记录；
  3. 新会话没有「先查已有结论、再决定是否重查」的提示或工具调用约束。
- **解决措施**：
  - 新增 `server/app/agent/research_memory.py`，把「问题 + 终答 + 去重后的来源 URL + 抓取/保存时间」写成一条 user 级 mem0 记忆：
    - 仅当 run 状态为 `completed`、终答非空、工具轨迹里至少有一个成功的 `WebSearch` / `WebFetch` 时写入；
    - `kind="research_conclusion"`、`infer=False`，结论和来源按原文保存，最多 8 个来源、终答最多 4,000 字符；
    - `question_key` 为规范化问题的哈希：同一问题再次研究时用 `memory.update()` 覆盖旧结论，不产生重复记忆；不同问法仍作为新记忆保存，由向量检索召回。
  - 在 `stream_local_agent` 构建运行时之前，检索该用户的 `research_conclusion`（先精确、后语义）：
    - 先用规范化的 `question_key` 调 `get_all` 精确命中「同一问题」，相同问题确定能召回，不再受向量分数影响；
    - 再用当前 `message` 做向量检索召回「不同问法」，只保留相似度不低于 `AGENT_RESEARCH_MEMORY_MIN_SCORE`（默认 0.5）的记忆；
    - 两条路径都只保留 `captured_at` 在 `AGENT_RESEARCH_MEMORY_TTL_DAYS`（默认 30 天）内的记忆，按 id 去重；
    - 渲染成带「保存日期 / 距今多久 / 可能过期 / 可用 WebSearch/WebFetch 复核」说明的 system prompt 块，最多 3 条、总长约 3,000 字符；该块只进模型视图，不落库为消息；
    - 检索失败、mem0 未初始化或结果异常时静默回退，不阻断 turn。
  - `runner._finalize()` 在 `done` 事件发出后异步写入记忆，embedding 慢或失败都不影响用户收到终答；恢复路径不重复写入。
  - 新增 `AGENT_RESEARCH_MEMORY`（默认 `true`，总开关）、`AGENT_RESEARCH_MEMORY_TTL_DAYS`、`AGENT_RESEARCH_MEMORY_MIN_SCORE`；远程 `AGENT_LOOP_URL` 执行器不接这层，文档已注明。
- **解决后效果**：
  - 新增 20 条单测（`tests/test_agent_research_memory.py` 14 条，`tests/test_agent_runtime.py` 5 条，`tests/test_agent_runs.py` 1 条），覆盖来源提取、错误/非 Web 工具过滤、同问题 update、无关记忆不覆盖、无来源/无用户/关闭开关不写、mem0 异常不抛、TTL/相似度/kind 过滤、注入块长度上限、system prompt 注入、无 `user_id` 不检索、检索异常不影响 run、`_finalize` 完成态才写入；
  - 本次改动相关三个测试文件 `85 passed`（含并行 P-08 新增用例）；server agent 全量 `180 passed`；
  - 跨会话真实复测（2026-11-04，同一问题三个独立会话）：
    - A（无记忆）：6 次工具调用（WebSearch 3 / WebFetch 2 / search_memories 1），21s；B（记忆已存在但未注入）：3 次调用，14s；C（注入生效）：9 次调用（WebSearch 5 / WebFetch 4），57s。
    - 首轮暴露召回缺陷：同一问题的向量相似度只有 **0.3558**，低于默认阈值 0.5，B 完全没有收到注入块；而无关问题「如何用 Python 读取 CSV 文件」得分 **0.4007**——长结论文本 + 短问题的绝对相似度阈值既漏召回又不可靠。
    - 修复：检索改为「`question_key` 精确命中 + 向量召回不同问法」双路径（精确路径仍受 TTL 约束，按 id 去重），新增 3 条单测；相关三文件 `88 passed`。
    - 修复后 C 的日志出现 `research memory injected user=... chunk_chars=1520`，注入块包含「saved 2026-11-04, today」、上次结论和来源；模型按提示复核后采用了新结论（Qwen3.8-27B，与上次的 Qwen3-8B 不同）。
    - 效果：**注入链路已验证**，但本次样本的工具调用没有减少（6 → 9），且模型运行间策略波动很大（无注入的 B 只有 3 次），因此「减少重复研究」还不能下结论；需要多次 A/B 对照，并考虑按记忆年龄给出更强的复用指引（如 7 天内同题默认直接回答）。
- **复测中发现的新问题（2026-11-04，P-08 复测时）**：与 Qwen 完全无关的问题「DeepSeek 官方 API 价格」也命中了旧的 Qwen 结论并注入（`chunk_chars=1520`），说明中文短问句的向量相似度可能普遍高于 0.5，向量路径存在**误注入**；精确路径（`question_key`）不会误命中。后续需要提高阈值、加入实体/关键词过滤，或让模型在注入块里看到相似度分数。
- **可复用结论**：跨会话复用研究结论，必须把「结论 + 来源 + 抓取时间」一起持久化，并在注入时明确标注年龄和「可复核」；只存一段摘要会让模型无法判断时效与出处。写入用 `infer=False` + 稳定 `question_key` 才能保证原文不被二次改写、同问题不重复堆积。

---

## P-08 跨 run 工具轨迹未回灌模型（每次 run 从零开始）

- **状态**：已解决（2026-11-04 已实现并完成跨 run 真实复测）
- **发现时间**：2026-11-04
- **解决时间**：2026-11-04
- **现象**：同一会话内第二次提问时，模型只能看到上一轮的 user/assistant 文本，看不到上一轮 `WebFetch` / `WebSearch` / `Read` / `Bash` 的调用与结果。P-06 的跨 run 重复抓取（`models` / `qwen-coder` / `coding-plan` 各 4 次）与 P-07 的「结论不沉淀」都受此影响。
- **根因（事实）**：
  1. 工具调用与结果已经持久化：run 期间写入 `agent_run_events`（`tool.call` / `tool.result`），终态 assistant 消息的 `metadata.tool_trace` 保存有界快照；
  2. 但 `store.build_history()` 只查询 `role in ('user','assistant')` 且 `status='completed'`、`content != ''` 的消息，只返回 `{role, content}`；
  3. `runtime.build_messages()` 又只接受 `user/assistant/system`，`role='tool'` 与 `tool_calls` 在进入 SDK 前被丢弃。SDK `ContextManager` 本身支持完整的 OpenAI wire format，因此瓶颈在 server 的历史重建。
- **解决措施**：
  - `store.build_history()` 查询 `status='completed'` 的 user/assistant 消息，以及带 `metadata.tool_trace` 的 assistant 消息（包括失败/取消的 run，但只回灌工具记录，不伪造终答）；
  - 每条 assistant 记录重建为 `assistant(tool_calls)` + 每个调用一条 `tool` 消息 + 有终答时再追加一条 assistant 文本，保持 `tool_call_id` 配对；
  - 复用 `arguments` / `raw_arguments`，非法 JSON 参数回退为 `{}`；工具结果与参数分别按 `AGENT_LOOP_HISTORY_TOOL_RESULT_MAX_CHARS`（默认 2,000）与 `AGENT_LOOP_HISTORY_TOOL_ARGUMENT_MAX_CHARS`（默认 1,000）截断；
  - 历史字符预算按整个 tool block 计费，并保证保留 assistant 时同时保留其前一条 user 问题；预算不足时整块丢弃，不产生孤儿 `tool` 消息；
  - `runtime.build_messages()` 按 OpenAI wire format 透传 `tool_calls` / `role='tool'`，并丢弃孤儿或格式错误的工具记录，避免上游校验失败；
  - 新增 `AGENT_LOOP_HISTORY_TOOL_TRACE`（默认 `true`，设为 `false` 退化为纯文本历史）。
- **解决后效果**：
  - `tests/test_agent_history.py` 与 `tests/test_agent_runtime.py` 新增 11 条用例，覆盖重建、参数/结果截断、失败 run 轨迹、开关、空消息、预算成对丢弃、孤儿 tool 结果过滤；
  - server agent 相关 `103 passed`；
  - 跨 run 真实复测（2026-11-04，`deepseek-flash`，临时关闭 P-07 注入以隔离变量）：
    - A（新会话，研究轮）：2 次工具调用（`WebFetch` 定价页 + `WebSearch`），5.97s，`completed`；assistant 消息 `metadata.tool_trace` 落库 2 条（`["WebFetch","WebSearch"]`）。
    - B（同会话追问，要求不使用工具、原样列出上一轮搜索结果）：0 次工具调用，6.2s，逐条列出 5 个搜索结果的标题与 URL，以及抓取请求 URL、重定向后的最终 URL、`cached: false` 与重定向提示——这些只存在于上一轮的 `tool` 消息里，证明历史已按 `assistant.tool_calls` + `tool` 的 wire format 回灌。
    - 对照（新会话问同一问题）：0 次工具调用，1.3s，仅回答「上下文里没有原始记录」，没有编造。
  - 复测暴露回灌上限：B 看到的抓取正文在 2,000 字符处被 `AGENT_LOOP_HISTORY_TOOL_RESULT_MAX_CHARS` 截断，价格表恰好落在截断之后，模型主动声明「上一轮价格表在当前上下文中没有原始依据」。默认 2,000 字符对表格/数据页偏小，需要时可调大。
  - 第一次复测时 P-07 注入仍开着，结论记忆直接污染了对照（模型声称抓取过 `example.com`），正式对照因此关闭了 `AGENT_RESEARCH_MEMORY`；两项特性的评估必须分开做。
- **可复用结论**：持久化工具调用只是第一步；下一轮必须在历史里以**配对的 `assistant.tool_calls` + `tool` 消息**回灌，模型才能真正复用结果。只存事件表而不重建 wire format，等于没有上下文。

---

## P-09 Dashboard Memory Detail 内容超长时被裁切、无法滚动

- **状态**：已解决（2026-11-04）
- **发现时间**：2026-11-04
- **解决时间**：2026-11-04
- **场景**：Dashboard「Memories」页点击任一行，右侧 Sheet 展示 Memory Detail（`dashboard/src/app/(root)/dashboard/memories/page.tsx`）。
- **现象**：记忆内容较长时，抽屉只显示首屏可见部分，下方 ID / 元数据 / Delete memory 按钮被裁掉；鼠标滚轮与触控板都无法滚动，内容无法完整查看。
- **根因（事实）**：
  1. `SheetContent`（`dashboard/src/components/ui/sheet.tsx`）的右侧变体样式为 `fixed inset-y-0 right-0 h-full p-6`，没有任何 `overflow` 设置；
  2. Memory Detail 的内容直接作为 `SheetContent` 子节点平铺，没有可滚动容器，超出 `h-full` 的部分被浏览器裁切；
  3. 超长无空格字符串还会横向溢出（缺少 `break-words`）。
- **解决措施**：
  - `SheetContent` 改为 `flex flex-col sm:max-w-md`，`SheetHeader` 加 `shrink-0`；
  - 详情内容容器改为 `mt-6 min-h-0 flex-1 space-y-4 overflow-y-auto pr-1`：头部固定、正文独立纵向滚动，写法与 `dashboard/src/app/(root)/dashboard/agent/page.tsx:145` 的移动端抽屉一致；
  - Content 文案加 `break-words whitespace-pre-wrap`，避免长 token 横向溢出并保留原文换行。
- **解决后效果**：
  - `tsc --noEmit` 通过；`next build` 通过（`/dashboard/memories` 6.74 kB）；
  - 内容超出视口时正文出现独立滚动条，标题与关闭按钮保持可见，长内容可完整查看。
- **可复用结论**：`fixed inset-y-0 h-full` 的抽屉只保证占满视口，**不会自动滚动**；内容区必须显式设置 `flex-1 min-h-0 overflow-y-auto`（flex 子项默认 `min-height: auto`，漏掉 `min-h-0` 仍会被内容撑破）。

---

## 变更记录

| 日期 | 变更 |
|---|---|
| 2026-11-04 | 建立文件，录入 P-01 ~ P-05（P-01/P-02/P-03 已解决，P-04 未解决，P-05 部分缓解） |
| 2026-11-04 | 追加 P-06（WebFetch 单 run URL 去重缓存，commit `c3d3334`）；标为部分缓解，线上效果待复测 |
| 2026-11-04 | P-04 补充量化证据（16 轮工具结果 ≈50,880 tokens > 32,000 预算）、外部调研（LangChain / OpenAI Agents SDK / AutoGen / OpenHands / PraisonAI / 4 篇论文）与 6 个候选方案及推荐顺序 |
| 2026-11-04 | 实现 P-04 的 S1（事实账本）+ S3（步数感知收尾）+ S5（可行动的工具反馈）；三次同题复测 `finish_reason=final`（10 轮 29s / 16 轮 25s，均 10 次工具调用），P-04 改为已解决，P-06 效果补测 |
| 2026-11-04 | 实现 P-04 第二批：S2（无进展守卫，nudge 3 / 强制终答 5、`stop_reason=no_progress`、`NoProgressError` / `agent_no_progress`）；修复 HTML 表格单元格分隔、缓存抓取返回摘要（`full=true` 取全文）、搜索缓存 key 纳入 `max_results`；agent-loop 156 passed / server agent 131 passed；两次同题复测 `final`（58.7s/17 调用、29.1s/12 调用） |
| 2026-11-04 | 追加 P-07：实现跨会话「可复用研究结论」——成功 run 后把结论 + 来源 + 抓取时间写入 mem0（`infer=False`、`kind=research_conclusion`、同问题 update），下一轮按相似度 + TTL 检索并注入 system prompt；新增 20 条用例、三文件 85 passed，跨会话复测待执行 |
| 2026-11-04 | P-07 跨会话复测：同一问题向量相似度 0.356 < 0.5 导致零注入（无关问题反而 0.4007）；检索改为 `question_key` 精确命中 + 向量召回，容器内验证 `research memory injected ... chunk_chars=1520`；三会话工具调用 6/3/9，减少重复研究的效果待多样本 |
| 2026-11-04 | 追加 P-08：server 历史重建回灌每次 run 的 `metadata.tool_trace`，按 `assistant.tool_calls` + `tool` 消息传入 SDK；结果/参数分别截断，预算不足整块丢弃；新增 `AGENT_LOOP_HISTORY_TOOL_TRACE` 等配置；相关 103 tests passed，跨 run 复测待执行 |
| 2026-11-04 | 追加 P-09：Dashboard Memory Detail 抽屉补 `flex flex-col` + 正文 `flex-1 min-h-0 overflow-y-auto`，长内容可滚动；文案加 `break-words whitespace-pre-wrap`；`tsc` 与 `next build` 通过 |
| 2026-11-04 | 记录规则新增「发现时间 / 解决时间」；P-01 ~ P-09 的发现时间均记为 2026-11-04，已解决条目解决时间记为 2026-11-04，部分缓解的 P-05 / P-07 解决时间留空 |
| 2026-11-04 | P-08 跨 run 真实复测：同会话追问 0 次工具调用即可原样复述上一轮搜索标题/URL 与抓取重定向信息，新会话对照只答「上下文里没有原始记录」；同时发现回灌结果 2,000 字符上限会截掉表格，以及 P-07 对无关中文问题误注入 |
