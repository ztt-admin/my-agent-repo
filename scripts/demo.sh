#!/usr/bin/env bash
# End-to-end demo: two different agents feed the same shared memory, then a third
# agent reads a token-budgeted context pack and a human-facing task is completed.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PYTHON="${PYTHON:-python3}"
WORKDIR="$(mktemp -d)"
DB="$WORKDIR/demo.db"
trap 'rm -rf "$WORKDIR"' EXIT

run() {
  "$PYTHON" -m brain --db "$DB" --no-llm "$@"
}

section() {
  if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
    printf '\n\033[1m== %s ==\033[0m\n' "$1"
  else
    printf '\n== %s ==\n' "$1"
  fi
}

section "1. 产品 agent 导入口播稿，生成结构化笔记"
run --agent agent:pm ingest samples/product-kickoff.txt

section "2. 重复导入相同内容：幂等，不产生重复笔记"
run --agent agent:pm ingest samples/product-kickoff.txt

section "3. 另一个来源的 agent（写作）导入另一份口播稿"
run --agent agent:writer ingest samples/agent-workflow.txt

section "4. 跨 agent 检索：同一条决策由两个 agent 共同贡献"
run search "MVP 先不做知识图谱"

section "5. 增长 agent 按 500 token 预算取上下文包"
run --agent agent:growth context "共享记忆 去重 上下文包" --budget 500

section "6. 运营 agent 写入一条任务，然后标记完成"
ITEM_ID="$(run --agent agent:ops remember "周五之前把 MCP 接入文档发到群里，负责人：小林。" \
  --type task --owner 小林 --due 本周五 --json | "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
echo "任务 id: $ITEM_ID"
run done "$ITEM_ID"

section "7. 共享记忆总览"
run stats

section "8. 笔记清单"
run notes

printf '\n演示结束（临时数据库已清理）。\n'
