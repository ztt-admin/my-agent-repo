# 共享大脑（Shared Brain）产品 Spec

> 版本：v0.1 · 日期：2026-11-07 · 状态：MVP 已实现，待评审  
> 范围：口播 `.txt` → 结构化笔记；多来源 agent 共用一份结构化记忆  
> 对应实现：本仓库 `brain/`，演示 `scripts/demo.sh`，测试 `tests/`

---

## 0. 一页结论

**要做的事**

1. 用户把口播/访谈/课程的 `.txt` 转录稿丢进来，得到一篇固定结构、可读、可检索的笔记：标题、摘要、核心要点、章节、决策、行动项、待解答问题、原话、标签。
2. 笔记不是终点。系统把其中的决策、任务、要点等拆成**原子记忆**，多个来源不同的 agent 往同一个记忆库读写；重复内容自动去重，但保留“谁贡献的、来自哪篇笔记”。

**主流路线对比后的结论**

- **口播结构化**：纯规则可离线但抽象能力弱；单次 LLM 质量好但长稿会截断；分块 Map-Reduce 质量最稳但成本最高；现成会议笔记产品开箱即用但数据被锁定、无法服务多 agent 共享。  
  **选择：规则兜底 + LLM 可选增强的混合方案**。MVP 默认离线可跑，配置 API key 后自动升级；长稿的 Map-Reduce 放到 Beta。
- **共享记忆**：文件式简单但检索/并发弱；向量 RAG 语义强但缺结构和溯源；Mem0/Zep/Letta 等记忆框架功能全但黑盒、锁定；知识图谱表达力强但维护重；平台内置记忆不可跨产品。  
  **选择：SQLite + FTS5 的自建结构化记忆**（原子 item + 贡献溯源 + 去重 + 读时上下文包），用 **MCP / HTTP** 做接入标准；向量检索与图谱关系作为 Phase 2 增量，而不是 MVP 前提。

**MVP 已经交付什么**

- 零依赖 Python 实现（3.10+），`python3 -m brain ...` 开箱即用。
- 结构化笔记 + 原子记忆 + 去重/贡献者 + 中文 FTS5 检索 + token 预算上下文包。
- 三种入口：CLI、本地 HTTP API、MCP stdio server（7 个工具）。
- 规则引擎 + OpenAI 兼容 LLM 引擎（失败自动回退）。
- 30 个自动化测试；两篇真实感样例稿；可一键运行的多 agent 演示。

**MVP 明确不做什么**

- 不做录音、ASR、实时转写；输入只接受 `.txt`。
- 不做向量/语义检索、不做知识图谱、不做自动冲突裁决。
- 不做 Web UI、登录、多租户、权限、云同步。
- 不做任务执行/日程集成；只记录和复用记忆。

---

## 1. 背景与问题

### 1.1 原始需求

> 1、将 `.txt` 口播文本转写成结构化笔记；  
> 2、构建属于共享大脑，多个来源不同的 agent 共用一份结构化记忆，方便工作的产出。  
> 以产品的角度，按照目前主流的方案对比，列出一份可供执行的 spec，并构造出第一个 MVP。

### 1.2 问题拆解

口播稿不是文章：它有大量口头语、重复、跳跃和“说了但没说完”的句子。用户真正需要的不是逐字稿，而是**能拿去做决策和执行的结构化信息**。

四个具体问题：

1. **信息损耗**：用通用摘要模型处理 30 分钟口播稿，容易保留“听起来重要”的观点，却漏掉“谁负责、什么时候完成、到底定了什么”。
2. **重复劳动**：内容、销售、运营 agent 各自读原稿、各自总结，产生多份互相冲突的版本。
3. **不可追溯**：一条结论无法回答“谁在哪个来源、什么时间说的”，出问题无法回滚。
4. **读取成本**：把整个记忆库塞给 agent 会超上下文窗口；需要按问题和 token 预算动态组装。

### 1.3 为什么现在做

- LLM 的 JSON 结构化输出已经足够稳定，可以用固定 schema 产出笔记。
- MCP（Model Context Protocol）正在成为 agent 接工具/数据的通用协议，一份记忆可以被不同 runtime 复用。
- 小团队不需要一上来就上向量库/图数据库：SQLite + FTS5 + 好的数据模型可以覆盖 80% 的“找得到、说得清、可复用”。

---

## 2. 目标与非目标

### 2.1 产品目标

| 编号 | 目标 | 衡量方式 |
| --- | --- | --- |
| G1 | 让一篇 10–30 分钟的口播稿在 1 分钟内变成可读的结构化笔记 | 导入 p95 时延；字段完整率 |
| G2 | 让决策/任务/问题从“说过”变成“可被检索、可被引用” | 决策/任务抽取 P/R；检索 hit@5 |
| G3 | 让多个 agent 共用一份记忆而不产生重复条目 | 去重率；多贡献者条目占比 |
| G4 | 让 agent 在固定 token 预算内拿到最相关的记忆 | 上下文包预算遵守率；引用完整率 |
| G5 | 个人/小团队零运维、数据本地可控 | 无需外部服务即可跑通全部 P0 功能 |

### 2.2 用户目标（JTBD）

- “我听完一场分享，想把里面真正能用的东西留下来，而不是收藏一个逐字稿。”
- “我不想让三个 agent 各写一份会议纪要，最后不知道信哪个。”
- “我让 agent 写方案时，希望它先读到团队已经定过的决策和待办，而不是重新猜。”

### 2.3 非目标（MVP）

- 不做通用笔记/知识管理产品（不是 Notion 替代品）。
- 不做会议机器人、不接入日历、不做实时录音。
- 不做自动执行（发消息、建日程、改代码）。
- 不做企业级权限、审计、多租户、合规认证。

### 2.4 成功指标（上线后 4 周）

- 结构化字段完整率 ≥ 90%（标题/摘要/≥3 要点/≥3 标签）。
- 决策/任务抽取在 50 条人工标注句上 F1 ≥ 0.7（规则+LLM 混合）。
- 检索 hit@5 ≥ 0.8（30 条真实查询）。
- 多 agent 重复写入的去重率 ≥ 95%。
- 上下文包 100% 不超过设定预算（按保守 token 估算）。

---

## 3. 用户与场景

### 3.1 目标用户

| 用户 | 特征 | 核心诉求 |
| --- | --- | --- |
| 个人创作者/研究者 | 有大量访谈、课程、播客转写稿 | 快速得到可读笔记和可复用素材 |
| 3–10 人内容/产品团队 | 有产品会、选题会、客户访谈 | 决策和任务不丢失，多人/多 agent 不重复 |
| Agent 开发者/重度用户 | 同时用 Claude、Cursor、自研 bot | 一份记忆被多个 agent 通过标准协议读写 |

### 3.2 三个核心场景

**场景 A：个人沉淀**

小林把一期 40 分钟播客的逐字稿存成 `podcast.txt`，运行 `brain ingest`。她得到一篇带章节、金句、行动项的笔记；一周后写文章时用 `brain context "创作者 商业化" --budget 600` 取回相关要点。

**场景 B：团队共享决策**

产品 agent、写作 agent 分别导入同一场评审会的不同片段。两个 agent 都写入了“MVP 先不做知识图谱”这条决策；系统只保留一条记忆，贡献者显示两人。销售 agent 在准备客户材料时，能引用这条决策并看到来源。

**场景 C：多 agent 协作**

Claude 通过 MCP 读取团队记忆写方案；Cursor 通过 MCP 把新决策写回；飞书 bot 通过 HTTP 搜索。三个入口共享同一个 SQLite 文件，不需要互相适配。

### 3.3 用户旅程

```text
上传/粘贴 .txt
   │
   ▼
解析时间戳/说话人/句子
   │
   ▼
结构化抽取（规则 → 可选 LLM）
   │
   ├──▶ 笔记视图（Markdown / JSON，供人阅读）
   │
   └──▶ 原子记忆（去重 + 贡献者 + 来源）
              │
              ▼
        检索 / 上下文包（按 query + token 预算）
              │
              ▼
        agent 写方案 / 人做决策 / 任务被认领
```

---

## 4. 需求定义

### 4.1 R1：口播文本 → 结构化笔记

**输入**

- UTF-8 / GB18030 / UTF-16 的 `.txt`，或直接粘贴文本。
- 可选：`[00:12:30]` 时间戳、`主持人：` / `Speaker 1:` 说话人标签。
- 可选：用户指定标题。

**输出（固定字段）**

| 字段 | 必填 | 说明 | MVP 规则引擎能力 |
| --- | --- | --- | --- |
| `title` | 是 | ≤44 字，优先取前部有信息量的陈述句 | 启发式打分 |
| `summary` | 是 | 2–3 句，按原文顺序拼接 | 句子打分 + 去重 |
| `key_points` | 是 | 3–6 条核心要点 | cue 词 + 关键词密度 |
| `sections` | 是 | 1–5 个章节，含标题、起止时间、要点 | 相邻句子词集相似度切分 |
| `decisions` | 可空 | 明确的决定/结论 | “决定/确定/采用/结论”等 cue + 实质内容过滤 |
| `tasks` | 可空 | 可执行行动项，含 `owner` / `due` | 动作 cue + 负责人/时间正则 |
| `questions` | 可空 | 待解答的开放问题 | 问号 + 疑问词 |
| `quotes` | 可空 | 值得保留的原话 | 引号抽取 + 观点句 |
| `tags` | 是 | 3–8 个主题标签 | 中文 n-gram + 覆盖率去重 |
| `engine` | 是 | `heuristic` / `llm` | 记录生成方式 |
| `warnings` | 是 | LLM 失败等降级说明 | 结构化返回 |

**质量要求**

- 不编造原文没有的人名、数字、结论；LLM 输出必须通过 schema 校验和空值兜底。
- 时间戳只在原文存在时保留；无时间戳时章节不显示时间。
- 笔记 Markdown 与 JSON 同时存储；Markdown 用于阅读，JSON 用于程序消费。

**不做**

- 不做逐字校对、不做错别字修正、不做说话人自动聚类（只识别已有标签）。

### 4.2 R2：多 Agent 共享结构化记忆

**写入**

- 任何 agent 可以通过 CLI/HTTP/MCP 写入一篇笔记，或直接写一条原子记忆（`remember` / `add_memory`）。
- 写入必须带 `agent_id`；系统记录来源笔记、原始内容 hash、写入时间。

**去重**

- 原子记忆的指纹 = `hash(type + 规范化文本)`；同类型同文本只保留一条 item。
- 重复写入只追加一条 contribution（agent + note + source + time），不新建 item。
- 同一个 agent 重复导入同一份稿子：返回原笔记，完全幂等。

**读取**

- 关键词检索：中文按字符级 FTS5，英文按词；支持按类型/agent/状态过滤。
- 上下文包：给定 query 与 token 预算，按“决策 > 行动项 > 摘要 > 要点 > 问题 > 原话”的优先级选取，带 agent/笔记引用，硬性裁剪到预算内。

**生命周期**

- 任务初始 `open`，可 `done` / `archived`；其他类型 `active`。
- MVP 不自动删除记忆；冲突内容保留双方，等 Beta 做裁决。

**权限**

- MVP 单库单用户/小团队，无鉴权；通过“本地文件 + 127.0.0.1”降低暴露面。
- 多租户/按 agent 隔离属于平台阶段。

### 4.3 非功能需求

| 类别 | 要求 |
| --- | --- |
| 依赖 | 核心路径零第三方依赖，Python 3.10+ |
| 部署 | 单文件 SQLite，无需服务端；可选 HTTP/MCP 进程 |
| 性能 | 1 万字以内导入 < 2s（规则引擎）；检索 < 100ms（万级 item） |
| 可靠性 | WAL + 事务；重复导入/并发写入不产生重复 item |
| 隐私 | 默认不联网、不写遥测；LLM 仅在用户显式配置后启用 |
| 可迁移 | 数据在单个 `.db` 文件，可直接备份/拷贝 |
| 可扩展 | 记忆 schema 带版本号；MCP/HTTP 接口与存储解耦 |

### 4.4 约束

- 输入不含音频，因此无法利用声学信息（停顿、情绪、重音）；时间戳/说话人质量取决于上游转写工具。
- 规则引擎的中文分词能力有限，标签/章节标题质量有天花板；正式使用建议 LLM。
- SQLite 适合单机低并发写入；高并发服务端需要 Postgres。

---

## 5. 主流方案对比

> 说明：以下为产品与技术路线层面的对比，不构成对任何产品当前版本的精确评测；具体能力以各产品官方文档为准（检索时间 2026-11）。

### 5.1 R1：口播文本 → 结构化笔记

| 路线 | 代表做法/产品 | 结构化质量 | 成本/延迟 | 可控/可解释 | 离线 | 适合 | MVP |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 纯规则/统计 | 关键词、正则、TextRank、模板 | 中低：能抓 cue 句，难归纳 | 极低 / 毫秒级 | 高：每条规则可解释 | 是 | 兜底、合规、无网环境 | ✅ 兜底引擎 |
| 单次 LLM JSON | JSON mode / function calling，一次长上下文调用 | 高：摘要/章节自然 | 中 / 秒级；随长度增长 | 中：schema 可校验，仍可能幻觉 | 否 | 10–30 分钟稿、快速上线 | ✅ 可选增强 |
| 分块 Map-Reduce | 分段抽取 → 合并去重 → 全局归纳 | 最高：长稿不漏段 | 高 / 多次调用 | 中高：可分步校验 | 否 | 1 小时以上、重要会议 | Phase 2 |
| ASR 原生结构化 | 转写时带说话人/时间戳/标点，再用 LLM | 取决于 ASR | 中 / 转写+推理 | 中 | 部分 | 有音频源的团队 | 上游输入 |
| 微调小模型 | 专用抽取模型/分类头 | 稳定、可定制 | 低边际成本 / 快 | 中：需数据与评测 | 可 | 数据积累后的规模化 | Phase 3 |
| 现成 SaaS | Granola、Otter.ai、飞书妙记、Notion AI | 高（产品化模板） | 订阅费 / 网络 | 低：黑盒、格式锁定 | 否 | 个人快速使用 | 不采用，借鉴模板 |

**分析**

- **规则不是敌人**：口播里的决策/任务有很强的语言线索（“决定/采用/负责/下周三之前”）。规则先抽 cue，能保证关键字段不漏，且天然可解释；缺点是归纳和章节标题生硬。
- **LLM 不是万能**：长稿直接一次调用会被截断，且模型倾向写“漂亮的摘要”而不是“可执行的清单”。需要 schema 约束 + 规则校验 + 引用原文。
- **Map-Reduce 是长稿正解，但不是 MVP 必需**：先把单篇 10–30 分钟稿跑通，验证字段价值和用户留存，再为长稿付多次调用成本。
- **现成产品不适合本需求**：它们的产物是“会议纪要”，不是“可被多个 agent 写入/读取的结构化记忆”；数据在对方云上，格式不可控。

**决策：混合方案**

1. 规则引擎默认开启：解析 → cue 抽取 → n-gram 标签/章节 → 模板渲染。
2. 配置 LLM 后，一次调用产出同 schema 的 JSON；失败/超时/非法 JSON 自动回退规则，并在笔记中记录 warning。
3. 无论哪个引擎，都走同一套存储、检索、上下文包，保证下游稳定。
4. Beta 引入分块 Map-Reduce：按章节切块抽取，再合并去重，解决 1 小时以上长稿。

### 5.2 R2：多 Agent 共享结构化记忆

| 路线 | 代表 | 结构化 | 多 agent 共享 | 溯源/去重 | 语义召回 | 运维成本 | MVP |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 文件式 | Markdown/JSON + Git；Obsidian、`AGENTS.md` | 中：靠约定 | 中：Git 冲突 | 强：版本历史 | 弱 | 极低 | 否 |
| 向量 RAG | Chroma、pgvector、Qdrant + embedding | 低：chunk 无字段 | 中：共享索引 | 弱：难精确去重 | 强 | 中 | Phase 2 增量 |
| 记忆框架 | Mem0、Zep/Graphiti、Letta、Cognee | 中高：自动抽取 | 取决于产品 | 中：黑盒更新 | 强 | 中高 | 不直接采用 |
| 知识图谱 | Neo4j、Graphiti | 高：实体/关系/时序 | 中 | 强 | 中 | 高 | Phase 2/3 |
| 自建关系型 + FTS | SQLite/Postgres + FTS/JSON | 高：字段可控 | 高：单库 + 贡献者 | 强：指纹/来源 | 弱（可补） | 低 | ✅ 采用 |
| 平台内置记忆 | OpenAI/Claude/飞书内置 | 低 | 低：锁在平台内 | 弱 | 中 | 极低 | 否 |

**分析**

- **向量检索解决“找得到”，不解决“说得清”**。共享记忆的核心难点是去重、溯源、冲突和 token 预算；向量库对这些几乎没有原生支持。
- **记忆框架很强，但 MVP 不需要黑盒**。Mem0/Zep 等把抽取、更新、遗忘都封装了，接入快；但数据模型、更新策略、调试能力都被产品方控制，不适合作为“团队自己的结构化资产”的第一版。
- **图谱适合关系，不适合冷启动**。实体/关系/时间线很有价值，但需要额外的实体对齐、关系抽取和人工校验；在还没有稳定 item 之前上图，会把错误放大。
- **文件式适合个人，不适合并发**。多人/多 agent 同时写入时，Git 冲突和“谁覆盖谁”会成为主要问题。

**决策：自建结构化记忆 + 预留升级路径**

1. 单库：`items` 是唯一事实来源，`notes` 是阅读视图，`contributions` 记录来源。
2. 去重靠内容指纹，不靠向量相似度；宁可漏合并，不可错合并。
3. 读时组装 context pack，而不是写入时压缩；同一批记忆可以服务不同问题/预算。
4. 预留：`items` 加 embedding 列/外部向量索引做混合检索；加 `relations` 表做决策→任务→来源的图谱；存储层抽象后可迁 Postgres。

### 5.3 代表性产品对照

| 产品 | 定位 | 强项 | 不适合直接用的原因 | 可借鉴 |
| --- | --- | --- | --- | --- |
| [Granola](https://www.granola.ai/) | AI 会议笔记 | 无 bot 记录、笔记与会议上下文结合 | 面向个人会议，记忆不开放给多 agent | 笔记模板、会议上下文 |
| [Otter.ai](https://otter.ai/) | 转写 + 会议助手 | 实时转写、说话人、摘要、行动项 | 云端、格式锁定、无跨 agent 记忆 | 时间轴 + 行动项交互 |
| [飞书妙记](https://www.feishu.cn/product/minutes) | 音视频转文字 + 智能纪要 | 中文转写、纪要、待办、组织内协作 | 绑定飞书生态，数据不出域难 | 纪要模板、待办联动 |
| [Notion AI](https://www.notion.com/product/ai) | 文档 + AI | 块级笔记、总结、问答 | 不是 agent 记忆层，写入/去重不可控 | Markdown/块结构 |
| [Mem0](https://mem0.ai/) | Agent 记忆层 | add/search、自动抽取、图记忆 | 黑盒更新、数据模型固定 | 简洁的 memory API |
| [Zep / Graphiti](https://github.com/getzep/graphiti) | 时序知识图谱记忆 | 时间线、关系、冲突处理 | 图建模/维护重 | 时序溯源、关系 |
| [Letta](https://letta.com/) | 有状态 agent 运行时 | 记忆块、长期状态 | 绑定运行时，不是中立存储 | 记忆块与上下文预算 |
| [Cognee](https://www.cognee.ai/) | 记忆/知识图谱流水线 | 图 + 向量组合 | 复杂度高、冷启动成本 | 图+向量混合 |

### 5.4 最终方案与决策触发器

**采用：自建结构化记忆（SQLite+FTS5）+ 规则/LLM 混合抽取 + MCP/HTTP 接入。**

什么时候需要换/加：

| 触发条件 | 动作 |
| --- | --- |
| 检索 hit@5 < 0.7，且坏例主要是近义表达 | 加 embedding，做 FTS + 向量混合检索 |
| item 数 > 10 万或并发写明显冲突 | 迁 Postgres，保留 schema |
| 用户开始问“这条决策影响了哪些任务/来源” | 加 `relations` 表/图库 |
| 多人同时编辑同一篇笔记 | 引入版本/事件日志，而不是覆盖 |
| 企业客户要求权限/审计 | 多租户 + RBAC + 审计日志，进入平台阶段 |

---

## 6. MVP 产品定义

### 6.1 范围与优先级

| 优先级 | 功能 | 状态 |
| --- | --- | --- |
| P0 | `.txt` 导入；结构化笔记 9 类字段 | ✅ |
| P0 | SQLite 存储；内容指纹去重；贡献者溯源 | ✅ |
| P0 | 中文/英文关键词检索；按类型/agent 过滤 | ✅ |
| P0 | token 预算上下文包（含引用、硬裁剪） | ✅ |
| P0 | CLI + 本地 HTTP API + MCP stdio | ✅ |
| P0 | 规则引擎；OpenAI 兼容 LLM 可选；失败回退 | ✅ |
| P0 | 同一 agent 幂等导入；不同 agent 共享 item | ✅ |
| P0 | 测试、样例、演示脚本 | ✅ |
| P1 | 向量混合检索 | ⏳ |
| P1 | 笔记编辑/删除/合并；冲突展示 | ⏳ |
| P1 | 批量目录导入 + 评测脚本 | ⏳ |
| P1 | 本地 Web UI（阅读/校对） | ⏳ |
| P2 | 多租户/权限/审计 | ⏳ |
| P2 | 图谱关系、定时 consolidation | ⏳ |
| P2 | 飞书/Notion 同步、托管版 | ⏳ |

### 6.2 用户流程

**流程 1：人导入 + 阅读**

1. `brain ingest talk.txt --agent product:me`
2. 系统解析、抽取、存储；打印标题/标签/前三条要点。
3. `brain show <note_id>` 查看 Markdown；`--json` 给程序消费。

**流程 2：agent 写入 + 另一个 agent 读取**

1. 写作 agent 通过 MCP `ingest_transcript` 导入稿件。
2. 产品 agent 通过 MCP `get_context_pack` 取 600 token 上下文。
3. agent 产出文章/方案；新的决策用 `add_memory` 写回。
4. 人在 `brain items` 中看到所有贡献者与待办。

**流程 3：任务闭环**

1. 导入的笔记抽出“小林负责周五前录视频”。
2. 任务写入 `items`，状态 `open`，带 owner/due。
3. 完成后 `brain done <item_id>` 或 MCP `complete_task`。
4. 上下文包默认不再优先展示已完成任务。

### 6.3 结构化笔记 Schema

```json
{
  "schema": "shared-brain/note/v1",
  "id": "note_xxx",
  "title": "……",
  "summary": "……",
  "key_points": ["……"],
  "sections": [
    {
      "heading": "范围",
      "start_seconds": 3,
      "end_seconds": 150,
      "points": ["……"]
    }
  ],
  "decisions": ["……"],
  "tasks": [
    {"text": "……", "owner": "小林", "due": "下周三"}
  ],
  "questions": ["……"],
  "quotes": ["……"],
  "tags": ["MVP", "结构化记忆"],
  "engine": "heuristic",
  "engine_detail": "cue-rules+n-grams",
  "warnings": [],
  "agent_id": "agent:pm",
  "created_at": "2026-11-07T10:00:00Z",
  "source": {"filename": "talk.txt", "sha256": "...", "chars": 951},
  "source_stats": {"segments": 42, "speakers": ["主持人", "嘉宾"], "has_timestamps": true}
}
```

约束：

- `title` / `summary` / `key_points` / `tags` / `sections` 非空（引擎保证兜底）。
- `start_seconds` / `end_seconds` 为整数秒或 `null`。
- 所有字符串去首尾空白；单条 item 通常不超过 240 字（规则引擎按句切分，LLM 输出做长度校验）。

### 6.4 数据模型

```sql
sources(
  id TEXT PK, agent_id TEXT, kind TEXT, filename TEXT,
  content_sha256 TEXT, content TEXT, created_at TEXT,
  UNIQUE(agent_id, content_sha256)
)

notes(
  id TEXT PK, source_id TEXT, agent_id TEXT,
  title TEXT, summary TEXT, body_markdown TEXT, note_json TEXT,
  created_at TEXT, updated_at TEXT
)

items(
  id TEXT PK, note_id TEXT NULL, source_id TEXT NULL, agent_id TEXT,
  type TEXT, text TEXT, tags TEXT(JSON), status TEXT,
  confidence REAL, meta TEXT(JSON),
  fingerprint TEXT UNIQUE, created_at TEXT, updated_at TEXT
)

contributions(
  id INTEGER PK, item_id TEXT, agent_id TEXT,
  note_id TEXT NULL, source_id TEXT NULL, role TEXT, created_at TEXT
)

agents(id TEXT PK, kind TEXT, first_seen TEXT, last_seen TEXT)
```

**不变量**

1. `fingerprint = sha256(type + "\x1f" + 规范化文本)`；同类型同文本唯一。
2. 每个 item 至少有一条 contribution；重复写入只增加 contribution。
3. `sources(agent_id, content_sha256)` 唯一：同一 agent 重复导入幂等。
4. item 的 `note_id` 是“首次来源”，一篇笔记的 item 通过 contributions 关联，而不是只看 `note_id`。
5. 任务初始 `open`，其余 `active`；`done/archived` 只能显式设置。
6. MVP 不物理删除记忆；纠错通过新增记忆或新笔记，保留审计线索。

### 6.5 去重与溯源规则

```text
写入一条记忆：
  1. 规范化文本（去空白、标点、大小写）
  2. fingerprint = sha256(type + 规范化文本)
  3. 若 fingerprint 已存在：
       - 合并标签
       - 追加 contribution(agent, note, source, time)
       - 不新建 item
     否则：
       - 新建 item
       - 写入 FTS 索引
       - 追加 contribution
  4. 更新 agent.last_seen
```

跨 agent 导入同一篇稿子：

- 每个 agent 有独立的 `sources`/`notes`（来源与阅读视图不同）。
- `items` 共享；第二个人写入时 `items_created=0, items_linked=N`。
- 检索结果里的 `contributors` 会列出所有 agent。

### 6.6 检索与上下文包算法

**检索**

1. 中文按单字、英文按词切 token；FTS5 短语优先，其次 AND，再 OR。
2. 查询里不存在于库中的 token 直接丢弃（避免“完全不存在的话题”被常见字命中）。
3. 结果做相关性过滤：查询的连续二元组或 ASCII 词必须出现在文本中。
4. 排序：BM25 + 原文子串命中加权 + 类型/agent/status 过滤。

**上下文包**

```text
输入：query、budget_tokens、可选 agent 过滤
1. 检索候选（最多 40 条），按文本相似度去重
2. 分组：决策 / 行动项 / 摘要 / 要点 / 问题 / 原话
3. 第一轮：每个类型保底 1 条
4. 第二轮：按类型优先级 + 分数填满预算
5. 已完成任务不进入包
6. 渲染 Markdown（含 agent、笔记、负责人/截止引用）
7. 若总 token 超预算，从最低优先级尾部硬裁剪
输出：markdown + sections + used_tokens + truncated + notes
```

token 估算采用保守口径：中文 1 字 ≈ 1 token，ASCII 约 4 字符 ≈ 1 token；宁可提前截断，不让 agent 侧超窗。

### 6.7 接口清单

**CLI**

```text
brain [--db PATH] [--agent ID] [--no-llm] <command>
  init / ingest / notes / show / search / context / remember / items / done
  stats / agents / serve / mcp
```

**HTTP API**（默认 `127.0.0.1:8765`，无鉴权，仅本地）

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/healthz` | 健康检查 |
| POST | `/api/notes` | 导入文本（JSON 或 `text/plain`） |
| GET | `/api/notes` | 笔记列表 |
| GET | `/api/notes/{id}` | 笔记详情（JSON + markdown） |
| GET | `/api/notes/{id}/markdown` | 纯 Markdown |
| GET | `/api/items` | 原子记忆列表（type/agent/status） |
| POST | `/api/items` | 写入一条记忆 |
| GET | `/api/items/{id}` | 单条记忆 |
| POST | `/api/items/{id}/status` | 修改任务状态 |
| GET | `/api/search?q=` | 检索 |
| GET | `/api/context?q=&budget=` | 上下文包 |
| GET | `/api/stats` / `/api/agents` | 统计 |

**MCP 工具**

`ingest_transcript`、`search_memory`、`get_context_pack`、`add_memory`、`list_notes`、`complete_task`、`brain_stats`。

### 6.8 错误与降级

| 场景 | 行为 |
| --- | --- |
| 空文件/无有效句子 | 400 / CLI 报错，不写库 |
| 编码不支持 | UTF-8 → GB18030 → UTF-16 → 替换字符，尽量不失败 |
| LLM 未配置 | 规则引擎，`engine=heuristic` |
| LLM 超时/非 JSON/schema 不合法 | 规则引擎兜底，`warnings` 记录原因 |
| 重复导入 | 返回原笔记，`deduplicated=true` |
| 检索无结果 | 返回空列表/空包，不报错 |
| MCP 未知方法 | JSON-RPC `-32601` |
| MCP 工具参数错误 | `isError=true` + 可读错误文本 |
| 并发写 | WAL + 唯一约束；冲突时返回已有记录 |

### 6.9 验收标准（可执行）

1. `./scripts/check.sh` 全部通过（当前 30 个测试）。
2. `./scripts/demo.sh` 输出：2 篇笔记、3 个 agent、同一条决策显示两个贡献者、上下文包 ≤ 预算。
3. 同一 agent 连续两次 `ingest` 同一文件：第二次 `deduplicated=true`，note id 不变，item 数不增。
4. 两个 agent 导入含相同句子的稿件：共享 item 的 `contributors` 长度为 2，item 总数不翻倍。
5. 搜索 `MVP 先不做知识图谱` 能命中决策；搜索 `完全不存在的话题` 返回空。
6. `context --budget 300/600/1200` 返回的 `used_tokens` 均不超过预算，且优先包含决策/行动项。
7. HTTP：`POST /api/notes` → `GET /api/search` → `GET /api/context` 全链路 200；错误返回统一 JSON。
8. MCP：`initialize` → `tools/list` → `tools/call`（ingest/search/context/add/complete）全链路成功。
9. 无网络、无 API key 环境可完成 1–8（LLM 路径除外）。
10. 笔记 Markdown 包含摘要/要点/章节/决策/行动项/标签等小节（无内容的小节可省略）。

### 6.10 演示

```bash
./scripts/demo.sh
```

演示内容：产品 agent 导入 → 重复导入幂等 → 写作 agent 导入另一来源 → 跨 agent 检索同一条决策（两个贡献者）→ 增长 agent 取 500 token 上下文 → 运营 agent 写任务并完成 → 统计与笔记列表。

---

## 7. 架构与实现

### 7.1 模块图

```text
┌────────────────────────────── CLI (brain/cli.py) ──────────────────────────────┐
│                                                                                │
│   HTTP API (brain/api.py)          MCP stdio (brain/mcp.py)                    │
│          │                                  │                                  │
│          └──────────────┬───────────────────┘                                  │
│                         ▼                                                      │
│              BrainStore (brain/store.py)                                       │
│   ingest / search / context / items / stats / agents                           │
│         │            │              │                                          │
│         ▼            ▼              ▼                                          │
│  transcript.py   extract.py     context.py                                     │
│  解析时间戳/     规则引擎/LLM     token 预算/优先级                              │
│  说话人/句子     抽取 + Markdown   引用/裁剪                                     │
│         │            │                                                         │
│         ▼            ▼                                                         │
│      util.py      llm.py（可选 OpenAI 兼容）                                     │
│                                                                                │
│                         SQLite + FTS5                                          │
└────────────────────────────────────────────────────────────────────────────────┘
```

### 7.2 数据流

```text
.txt
 └─ parse_transcript ──▶ segments（时间/说话人/句子）
      └─ structure_transcript
           ├─ heuristic: cue 规则 + n-gram + 章节切分
           └─ llm: 一次 JSON 调用（失败回退）
                └─ note dict
                     ├─ render_markdown ──▶ notes.body_markdown
                     ├─ note_json        ──▶ notes.note_json
                     └─ _note_items      ──▶ items + contributions + FTS
```

### 7.3 关键实现选择

| 选择 | 原因 | 代价 |
| --- | --- | --- |
| Python 标准库实现 | 零依赖、易审计、任何环境可跑 | 无 Web 框架，UI 需另做 |
| SQLite + WAL | 单文件、事务、FTS5、易备份 | 高并发写弱 |
| 中文按字符 FTS | 无需分词依赖，召回稳定 | 语义弱，靠上下文包类型筛选补偿 |
| 规则 + LLM 双引擎 | 离线可用，联网增强；失败可回退 | 两套代码需保持一致 schema |
| 原子 item + 笔记视图 | 阅读与复用分离 | 关联查询稍复杂 |
| 读时组装上下文 | 同一记忆服务不同问题/预算 | 每次读取有检索成本 |
| MCP + HTTP 双入口 | 兼容 agent 与普通服务 | 需维护两套序列化 |

### 7.4 并发与一致性

- 每个操作独立连接，`busy_timeout=15s`，`journal_mode=WAL`。
- 导入在事务中完成；`sources(agent_id, content_sha256)` 唯一约束兜底重复导入。
- `items.fingerprint` 唯一；重复写入用 `ON CONFLICT ... DO UPDATE` 后追加 contribution。
- 任务状态是 last-write-wins；MVP 不记录状态变更历史（Beta 加事件表）。
- 没有分布式锁；单机多进程（CLI + MCP + HTTP）可安全共存，依赖 SQLite 锁。

### 7.5 安全与隐私

- HTTP 默认只绑定 `127.0.0.1`；远程需 `--allow-remote` 并自行加鉴权。
- 无遥测、无外部请求；唯一外部调用是用户显式配置的 LLM API。
- MCP 的 `ingest_transcript` 支持本地路径读取，属于本地信任模型；不要把 MCP server 暴露给不可信客户端。
- 口播稿可能含客户信息；启用 LLM 前需确认数据出境/合规要求。

### 7.6 代码地图

```text
brain/
  cli.py        命令行入口与输出格式
  api.py        本地 HTTP API
  mcp.py        MCP stdio server + 工具定义
  store.py      SQLite schema、去重、检索、统计
  context.py    token 预算上下文包
  extract.py    规则抽取、LLM schema 校验、Markdown 渲染
  transcript.py 时间戳/说话人/句子解析
  llm.py        OpenAI 兼容客户端
  util.py       文本/时间/哈希/token 估算
samples/        两篇示例口播稿
tests/          30 个单元/集成测试
scripts/        demo.sh / check.sh
docs/           SPEC.md / demo-output.txt
```

---

## 8. 质量与评测

### 8.1 评测集设计

从真实场景收集 20–30 篇口播稿（10–40 分钟），覆盖：

- 单人分享 / 双人访谈 / 多人圆桌；
- 有/无时间戳、有/无说话人；
- 产品决策、课程知识、销售对话、个人复盘。

每篇人工标注：

- 5–10 条决策/结论；
- 5–10 条行动项（含负责人、截止时间）；
- 3–5 个开放问题；
- 3–5 句值得保留的原话；
- 10 条检索 query + 期望命中的 item。

### 8.2 指标定义

| 指标 | 定义 | 目标 |
| --- | --- | --- |
| 字段完整率 | 标题/摘要/≥3 要点/≥3 标签都非空的笔记占比 | ≥90% |
| 决策/任务 F1 | 与人工标注按文本相似度匹配（≥0.8） | ≥0.7 |
| 检索 hit@5 | 期望 item 出现在前 5 的查询占比 | ≥0.8 |
| 上下文效率 | 被引用且与 query 相关的 item / 总 token | 人工评估 ≥70% |
| 去重率 | 重复写入被合并的比例 | ≥95% |
| 多贡献者占比 | contributors ≥2 的 item / 总 item | 观察指标 |
| 导入时延 | 1 万字规则引擎 p95 | <2s |
| 成本 | 每千字 LLM token 成本 | 按模型实测 |

### 8.3 测试策略（当前）

- 单元：文本解析、句子切分、规则抽取、Markdown、token 估算。
- 集成：SQLite 幂等/去重/贡献者/检索/上下文预算。
- 接口：HTTP 全链路、错误契约、纯文本上传。
- 协议：MCP 子进程 `initialize/list/call/ping`。
- LLM：本地 stub server 验证请求格式、成功解析、失败回退；不依赖外网。

### 8.4 LLM 幻觉控制

1. Prompt 明确“只输出 JSON、不编造、没有就 null/空数组”。
2. 输出做 schema 校验：字段类型、长度、任务 owner/due 类型。
3. 失败/空结果回退规则引擎，不把错误静默吞掉。
4. `engine` 与 `warnings` 写进笔记，用户可判断可信度。
5. Beta：关键字段（决策/任务）要求可引用原文 span，人工抽检。

---

## 9. 路线图

### Phase 1：MVP（本次，约 1–2 人周）

- 结构化笔记 + 原子记忆 + 去重/溯源。
- CLI / HTTP / MCP；规则引擎 + LLM 可选。
- 测试、样例、演示、Spec。
- 目标：验证“结构化记忆是否真的被复用”。

### Phase 2：Beta（4–6 人周）

1. **混合检索**：FTS + embedding（sqlite-vec / Chroma），hit@5 ≥0.8。
2. **长稿 Map-Reduce**：按章节分块抽取再合并，支持 1 小时以上。
3. **冲突与版本**：同一 item 的多来源冲突展示；状态变更事件表；人工确认。
4. **笔记编辑/校对**：本地 Web UI（阅读 → 改字段 → 保存为新版本）。
5. **批量导入 + 评测脚本**：目录导入、标注对比、指标报表。
6. **导出**：Markdown 包、飞书文档/Notion（按需）。

### Phase 3：平台（8–12 人周）

- Postgres + 多租户 + RBAC + 审计日志。
- 图谱关系：决策 → 任务 → 来源 → 负责人；冲突/影响分析。
- 定时 consolidation：去重、摘要、过期归档、可信度加权。
- 托管同步：团队共享 brain、跨设备、Webhook。
- 生态：MCP 市场、Claude/Cursor/飞书模板。

> 估算基于 1–2 名全栈工程师、已有 MVP 代码与评测集；不含设计/运营投入。

---

## 10. 风险与开放问题

### 10.1 风险

| 风险 | 概率 | 影响 | 缓解 |
| --- | --- | --- | --- |
| 规则引擎质量不达预期 | 高 | 中 | LLM 默认推荐；评测集驱动规则迭代；明确 `engine` |
| LLM 幻觉/漏字段 | 中 | 高 | schema 校验、关键字段引用、回退、人工抽检 |
| 去重过猛（同文不同义） | 中 | 中 | 指纹含 type；阈值保守；Beta 提供“拆分为新条目” |
| 共享 item 状态冲突 | 中 | 中 | MVP last-write-wins；Beta 事件日志 + 冲突展示 |
| SQLite 并发瓶颈 | 低（MVP） | 中 | WAL；写少读多；Phase 2 迁 Postgres |
| 口播稿隐私/数据出境 | 中 | 高 | 默认本地、LLM opt-in、文档声明、企业版脱敏 |
| MCP 协议演进 | 中 | 低 | 固定 protocolVersion、协议测试、版本适配层 |
| 用户不愿维护记忆 | 中 | 高 | 降低写入摩擦（MCP 自动写）、上下文包即时价值、复用指标 |

### 10.2 开放问题

1. **共享边界**：一个团队一个 DB，还是按项目/客户分库？分库会影响跨项目检索与权限。
2. **冲突裁决**：按来源可信度、时间、还是人工确认？MVP 先全部保留。
3. **记忆保留**：已完成任务多久归档/删除？是否需要“遗忘权”？
4. **权限模型**：agent 级读写权限、敏感字段脱敏、只读 agent 如何接入？
5. **商业模式**：开源核心 + 托管团队版？还是纯本地工具？
6. **LLM 数据边界**：默认禁止出域，还是首次使用时显式授权？
7. **多语言**：英文/中英混合的抽取质量与分词策略。

---

## 11. 附录

### A. 示例输入 / 输出

输入（节选）：

```text
[00:00:40] 嘉宾：我先说结论：第一个 MVP 不要做知识图谱，也不要上来就做向量数据库。
我们决定用 SQLite 加全文检索，先把结构化记忆跑通。
[00:01:20] 嘉宾：为什么？因为多 agent 共享记忆的核心不是向量，而是三件事：
来源可追溯、内容去重、读的时候能按预算组装上下文。
[00:02:30] 嘉宾：至少要有标题、摘要、核心要点、章节、决策、行动项和标签。
比如这一步由小林负责，下周三之前完成。
```

输出（节选）：

```json
{
  "title": "我们决定 MVP 只做三件事：导入 .txt 口播稿、生成结构化笔记、让多个 agent 读写同一份结构化记忆",
  "summary": "我们决定 MVP 只做三件事……",
  "decisions": [
    "我们决定 MVP 先不做知识图谱，也不做向量数据库。",
    "我们决定用 SQLite 加全文检索，先把结构化记忆跑通。"
  ],
  "tasks": [
    {"text": "这一步由小林负责，下周三之前完成。", "owner": "小林", "due": "下周三"}
  ],
  "tags": ["MVP", "结构化", "SQLite", "记忆", "行动项"]
}
```

Markdown 输出见 `brain show <note_id>` 或 `docs/demo-output.txt`。

### B. HTTP API 示例

```bash
# 导入
curl -s -X POST http://127.0.0.1:8765/api/notes \
  -H 'Content-Type: application/json' \
  -d '{"text":"...", "filename":"talk.txt", "agent_id":"agent:writer"}'

# 检索
curl -s 'http://127.0.0.1:8765/api/search?q=%E5%85%B1%E4%BA%AB%E8%AE%B0%E5%BF%86&limit=10'

# 上下文包
curl -s 'http://127.0.0.1:8765/api/context?q=MVP&budget=600'

# 写记忆
curl -s -X POST http://127.0.0.1:8765/api/items \
  -H 'Content-Type: application/json' \
  -d '{"text":"周五前发文档。", "type":"task", "agent_id":"agent:ops"}'
```

### C. MCP 配置

```json
{
  "mcpServers": {
    "shared-brain": {
      "command": "python3",
      "args": ["-m", "brain", "--db", "/absolute/path/brain.db", "mcp"]
    }
  }
}
```

### D. 数据字典（items）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `type` | enum | summary/point/decision/task/question/quote/fact/note |
| `text` | string | 一句自包含的记忆 |
| `tags` | JSON array | 条目级标签（优先取文本命中的标签） |
| `status` | enum | active/open/done/archived |
| `confidence` | float | 来源置信度（规则 0.4–0.75；手工 0.8） |
| `meta` | JSON | 任务 owner/due、章节等 |
| `fingerprint` | string | 类型 + 规范化文本的哈希 |
| `note_id` | string/null | 首次来源笔记 |
| `source_id` | string/null | 首次来源文件 |

### E. 参考资料

- [MCP 官方文档](https://modelcontextprotocol.io/)
- [SQLite FTS5](https://www.sqlite.org/fts5.html)
- [Granola](https://www.granola.ai/) · [Otter.ai](https://otter.ai/) · [飞书妙记](https://www.feishu.cn/product/minutes) · [Notion AI](https://www.notion.com/product/ai)
- [Mem0](https://mem0.ai/) · [Zep / Graphiti](https://github.com/getzep/graphiti) · [Letta](https://letta.com/) · [Cognee](https://www.cognee.ai/)
- [Graphiti 技术雷达](https://www.thoughtworks.com/en-cn/radar/platforms/graphiti)
- [LLM Agent Memory Tools 对比（2026）](https://futureagi.com/blog/best-llm-agent-memory-tools-2026/)

---

## 变更记录

| 日期 | 版本 | 变更 |
| --- | --- | --- |
| 2026-11-07 | v0.1 | 首版：路线对比、MVP 定义、数据模型、验收、路线图；对应代码与 30 个测试 |
