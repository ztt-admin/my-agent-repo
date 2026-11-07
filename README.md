# 共享大脑 · Shared Brain

把口播 `.txt` 转成**结构化笔记**，并让多个来源不同的 agent 共用一份**可追溯、可去重、可按预算读取**的结构化记忆。

零第三方依赖、本地优先、可离线运行；配置一个 OpenAI 兼容的 API key 后，可自动升级到 LLM 结构化引擎。

- 产品方案与主流路线对比：[`docs/SPEC.md`](docs/SPEC.md)
- 多 agent 演示：[`scripts/demo.sh`](scripts/demo.sh)
- 示例输出：[`docs/demo-output.txt`](docs/demo-output.txt)

## 解决什么问题

1. **口播稿信息密度低**：一场 30 分钟的分享，真正能沉淀的决策、任务可能只有十来条，直接丢给模型做摘要容易漏掉关键结论。
2. **多个 agent 各存各的记忆**：内容 agent、销售 agent、运营 agent 重复读同一份原稿、重复总结、版本互相冲突。
3. **记忆不可追溯**：一条结论是谁、在哪个来源、什么时间写进来的，出了问题无法定位。

本项目的做法：

- 把口播稿变成固定 schema 的**结构化笔记**：标题、摘要、核心要点、章节、决策、行动项、问题、原话、标签。
- 把笔记拆成**原子记忆 item**，用内容指纹去重；同一条记忆被多个 agent 写入时，只追加贡献者，不产生重复条目。
- 写入时记录**来源与贡献者**；读取时按查询和 token 预算组装**上下文包**，直接给 agent 使用。
- 一套存储，三种入口：**CLI**（人）、**HTTP API**（服务）、**MCP**（agent）。

## 60 秒上手

要求 Python 3.10+，不需要安装任何依赖：

```bash
# 1. 导入口播稿，生成结构化笔记并写入共享记忆
python3 -m brain --db ./brain.db --agent product:me ingest samples/product-kickoff.txt

# 2. 查看完整笔记
python3 -m brain --db ./brain.db show <note_id>

# 3. 检索共享记忆
python3 -m brain --db ./brain.db search "共享记忆 去重"

# 4. 给另一个 agent 组装 500 token 的上下文包
python3 -m brain --db ./brain.db --agent agent:writer context "MVP 范围" --budget 500

# 5. 写入一条手工记忆 / 标记任务完成
python3 -m brain --db ./brain.db --agent agent:ops remember "周五前把接入文档发群里，负责人：小林。" \
  --type task --owner 小林 --due 本周五
python3 -m brain --db ./brain.db done <item_id>
```

也可以安装成命令（可选）：

```bash
python3 -m pip install -e .
brain --db ./brain.db ingest samples/product-kickoff.txt
```

一次跑完两个 agent 的协作演示：

```bash
./scripts/demo.sh
```

## 结构化笔记长什么样

```markdown
# 我们决定 MVP 只做三件事：导入 .txt 口播稿、生成结构化笔记、让多个 agent 读写同一份结构化记忆

> 来源：product-kickoff.txt · agent：`agent:pm` · 2026-11-07 · 888 字 · 引擎：heuristic

## 摘要
...

## 核心要点
- ...

## 章节
### 00:03–02:30 · 范围
- ...

## 决策
- 我们决定 MVP 先不做知识图谱，也不做向量数据库。

## 行动项
- [ ] 小林负责周五之前把 MVP 的演示视频录好……（负责人：小林 · 截止：下周三）

## 待解答问题
## 原话摘录
## 标签
```

完整 JSON schema 见 [`docs/SPEC.md`](docs/SPEC.md#结构化笔记-schema)。

## 多个 agent 怎么共用一份记忆

```text
                 ┌──────────────────────────────┐
  产品 agent ───▶│                              │
  写作 agent ───▶│  SQLite: notes / items /     │───▶ 搜索 / 上下文包
  销售 agent ───▶│  contributions / FTS5        │
任意 MCP 客户端 ─▶│                              │
                 └──────────────────────────────┘
```

- 每条 item 有稳定指纹：同一句“我们决定先做结构化记忆”被三个 agent 写入，只会有一条 item，三个贡献者。
- 同一个 agent 重复导入同一份稿子：幂等，返回原笔记，不新增记忆。
- 不同 agent 导入同一份稿子：各自有一篇笔记视图，但共享同一批原子记忆和贡献记录。

### 方式一：CLI

见上面的命令；`--agent` 用来标识来源。

### 方式二：HTTP API

```bash
python3 -m brain --db ./brain.db serve --port 8765

curl -s http://127.0.0.1:8765/healthz
curl -s -X POST http://127.0.0.1:8765/api/notes \
  -H 'Content-Type: application/json' \
  -d '{"text":"...", "filename":"talk.txt", "agent_id":"agent:writer"}'
curl -s 'http://127.0.0.1:8765/api/search?q=%E5%85%B1%E4%BA%AB%E8%AE%B0%E5%BF%86'
curl -s 'http://127.0.0.1:8765/api/context?q=MVP&budget=600'
```

API 默认只绑定 `127.0.0.1` 且没有鉴权，适合个人/内网；远程暴露需要显式 `--allow-remote`，并自行加反向代理鉴权。

### 方式三：MCP

任何支持 MCP 的客户端（Claude Desktop/Code、Cursor、Dify、自研 runtime）都可以直接接入：

```json
{
  "mcpServers": {
    "shared-brain": {
      "command": "python3",
      "args": ["-m", "brain", "--db", "/absolute/path/to/brain.db", "mcp"]
    }
  }
}
```

暴露的工具：

| 工具 | 作用 |
| --- | --- |
| `ingest_transcript` | 口播 `.txt`（文本或路径）→ 结构化笔记 + 原子记忆 |
| `search_memory` | 跨笔记检索决策/任务/要点/问题/原话 |
| `get_context_pack` | 按 token 预算组装带引用的上下文包 |
| `add_memory` | 写入一条原子记忆 |
| `list_notes` | 列出最近的笔记 |
| `complete_task` | 标记任务完成/重新打开/归档 |
| `brain_stats` | 查看共享记忆总览 |

MCP 的默认 agent 标识取客户端的 `clientInfo.name`（例如 `mcp:claude-code`），也可以在工具参数里覆盖。

## 可选：LLM 增强

不配置也能用，规则引擎完全离线。配置后，导入时自动使用 LLM 生成更自然的摘要与章节；失败会回退到规则引擎并在笔记里记录 warning。

```bash
export BRAIN_LLM_API_KEY=sk-...
export BRAIN_LLM_BASE_URL=https://api.openai.com/v1   # 任意 OpenAI 兼容网关
export BRAIN_LLM_MODEL=gpt-4o-mini
# 可选：export BRAIN_LLM_TIMEOUT=60

python3 -m brain --db ./brain.db ingest samples/product-kickoff.txt
```

强制离线：全局加 `--no-llm`。强制 LLM：`ingest --llm`（未配置 key 会报错）。

## 常用命令

```text
brain [--db PATH] [--agent ID] [--no-llm] <command>

  init                    初始化数据库
  ingest FILE             口播 .txt → 结构化笔记（`-` 读 stdin）
  notes                   列出笔记
  show NOTE_ID            查看笔记（Markdown / --json 完整 JSON）
  search QUERY            检索共享记忆
  context QUERY           组装上下文包（--budget 800）
  remember TEXT           写入一条原子记忆
  items                   列出原子记忆
  done ITEM_ID            任务标记完成
  stats / agents          统计与 agent 清单
  serve                   启动 HTTP API
  mcp                     启动 MCP stdio 服务
```

环境变量：`BRAIN_DB`（默认 `~/.local/share/shared-brain/brain.db`）、`BRAIN_LLM_*`。

## 数据模型（MVP）

| 表 | 作用 |
| --- | --- |
| `sources` | 原始口播稿、内容 hash、来源 agent；同一 agent 同一内容唯一 |
| `notes` | 结构化笔记的 Markdown 与 JSON 快照 |
| `items` | 原子记忆：`summary/point/decision/task/question/quote/fact`，含指纹、状态、置信度 |
| `contributions` | 谁通过哪篇笔记贡献了哪条记忆；去重后仍保留所有来源 |
| `agents` | 参与共享的 agent 及活跃时间 |
| `items_fts` / `notes_fts` | SQLite FTS5 全文索引（中文按字符切分） |

数据不变量与算法细节见 [`docs/SPEC.md`](docs/SPEC.md)。

## 开发与测试

```bash
./scripts/check.sh          # compileall + 全量 unittest
python3 -m unittest discover -s tests -v
```

当前覆盖：文本解析、结构抽取、SQLite 去重/溯源/检索、上下文预算、HTTP API、MCP 协议、LLM 成功与回退路径。

## 已知限制（MVP 诚实声明）

- 规则引擎是**兜底**：中文关键词与章节标题质量有限，正式使用建议配置 LLM；LLM 结果也需人工抽检。
- 不做 ASR：输入必须是 `.txt`；有时间戳/说话人标签时效果更好，没有也能用。
- 语义检索未实现：当前是结构化 + 字符级 FTS5；近义表达召回弱，Phase 2 加向量混合检索。
- 冲突不自动裁决：两个 agent 写入矛盾内容时都保留，上下文包同时展示；Phase 2 做可信度/时间加权。
- SQLite 单文件、无鉴权、无多租户：适合个人/小团队本地使用；服务端多租户需要 Postgres + 鉴权 + 审计。
- MCP 实现覆盖 `initialize / tools/list / tools/call / ping`，未实现 resources/prompts。

## 文档

- [产品 Spec：方案对比、MVP 定义、数据模型、验收与路线图](docs/SPEC.md)
- [演示脚本](scripts/demo.sh)
- [演示输出](docs/demo-output.txt)
