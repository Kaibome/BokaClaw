# BokaClaw 0→1 构建问题记录

本文件记录 BokaClaw（尤其是 `bokaclaw-agent`）从 0 到 1 构建过程中**实际遇到并分析过的问题**，目的是后续复用这套经验到其他项目，或直接用于汇报。

## 记录规则

1. **发现问题、分析问题时立即记录**，先写现象、证据、根因分析，状态标为 `未解决`。
2. **问题解决后原地补齐**「解决措施」与「解决后效果」，状态改为 `已解决`；只缓解未根治的标 `部分缓解`。
3. 每条记录必须带**可核对的证据**（run id、耗时、事件数量、报错原文、commit、文件路径），不写没有依据的推测；推测放进「根因假设」。
4. 新条目按编号追加，不覆盖历史结论；结论被推翻时追加「修正」段落并说明原因。

状态取值：`未解决` / `部分缓解` / `已解决`。

---

## P-01 Agent 猜 URL，抓取连续失败后整轮报错

- **状态**：已解决（commit `6493c05`）
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

- **状态**：未解决
- **现象**：即使把 `AGENT_MAX_ITERATIONS` 从 10 提到 16，模型仍然**每一轮都发起新的搜索或抓取**，最后一轮仍在调工具，最终答案始终来自 P-03 的兜底路径，而不是模型自己判断「信息够了」。
- **证据**：

  | 配置 | 耗时 | 轮次 | 工具调用 | 工具失败 | 最后一轮 | 结束方式 |
  |---|---|---|---|---|---|---|
  | 10 轮 | 75.2s | 10 | 20 | 5 | 2×WebFetch | 兜底终答 |
  | 16 轮 | 122.2s | 16 | 30 | 7 | 2×WebFetch | 兜底终答 |

  16 轮版本增加了 6 轮、47 秒，答案从 1413 字增至 2028 字（拿到了 OpenRouter 的真实报价），但**没有改变「依靠兜底收尾」这一事实**。

- **根因假设**：
  1. 系统提示只鼓励「查证」，没有给出「信息足够即停止」的判定条件；
  2. 搜索结果重复率高（多组 query 返回同一批页面），模型不断换关键词试图验证，陷入低效循环；
  3. 对失败抓取（404、客户端渲染空页、超时）没有负反馈，模型会反复尝试相邻 URL。
- **下一步方向**（未实施）：
  - 提示词中加入收敛条件（例如「已获得可直接回答的关键事实后停止调用工具」）；
  - 运行时检测重复 URL / 重复查询，在工具结果里提示模型「该页面已抓取过」；
  - 对连续失败给出汇总提示，引导模型基于已有信息作答；
  - 评估是否需要「预算过半时提醒模型准备收尾」。
- **解决后效果**：待补充。

---

## P-05 目标信息源不可读：客户端渲染、404、超时

- **状态**：部分缓解
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

- **状态**：部分缓解（commit `c3d3334`；单 run 精确 URL 去重已生效，线上效果待复测）
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
  - 线上同一问题的复测尚未进行，去重前后的 WebFetch 次数与耗时对比**待补充**；
  - 需要说明：缓存只消除「完全相同 URL」的重复网络请求。若上下文压缩让模型看不到早前结果，模型仍可能再次调用同一 URL（此时命中缓存、不再发请求）；`.md` / 英文站变体、跨 run 重试仍会重新抓取。
- **后续方向**：
  - 用同一问题复测，统计去重前后 WebFetch 调用数、唯一 URL 数、耗时；
  - 缓存命中时返回更明确的提示（例如「本 run 已抓取过该 URL，请复用早前结果」），或只返回短提示以节省上下文；
  - 对重定向到列表页的 URL（`result.url != 请求 URL`）标记为不可用，避免模型继续换 slug；
  - 结合 P-04 的收敛提示，减少「信息已足够仍继续抓取」的情况。

---

## 变更记录

| 日期 | 变更 |
|---|---|
| 2026-11-04 | 建立文件，录入 P-01 ~ P-05（P-01/P-02/P-03 已解决，P-04 未解决，P-05 部分缓解） |
| 2026-11-04 | 追加 P-06（WebFetch 单 run URL 去重缓存，commit `c3d3334`）；标为部分缓解，线上效果待复测 |
