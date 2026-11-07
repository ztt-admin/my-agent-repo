"""Command line interface: the fastest way to try the shared brain."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import unicodedata

from . import __version__
from .api import create_server
from .context import build_context_pack
from .llm import LLMClient
from .mcp import MCPServer
from .store import BrainStore
from .util import dumps, read_text_file, truncate

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def default_db_path() -> str:
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "shared-brain", "brain.db")


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def _table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "（空）"
    widths = [_width(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], _width(str(value)))
    lines = ["  ".join(_pad(header, widths[i]) for i, header in enumerate(headers)).rstrip()]
    lines.append("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        lines.append("  ".join(_pad(str(value), widths[i]) for i, value in enumerate(row)).rstrip())
    return "\n".join(lines)


def _agent_name(raw: str | None) -> str:
    return raw or f"cli:{getpass.getuser()}"


def _read_input(file: str) -> tuple[str, str]:
    if file == "-":
        return sys.stdin.read(), "stdin.txt"
    return read_text_file(file), os.path.basename(file)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="brain",
        description="Shared Brain：把口播 .txt 转成结构化笔记，并让多个 agent 共用一份结构化记忆。",
    )
    parser.add_argument("--db", default=os.environ.get("BRAIN_DB") or default_db_path(),
                        help=f"SQLite 数据库路径（默认 {default_db_path()}，也可用 BRAIN_DB）")
    parser.add_argument("--agent", default=None,
                        help="当前 agent 标识，例如 writer / researcher / cli:me")
    parser.add_argument("--no-llm", action="store_true",
                        help="即使配置了 BRAIN_LLM_API_KEY 也强制使用离线规则引擎")
    parser.add_argument("--version", action="version", version=f"shared-brain {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="初始化数据库")

    ingest = sub.add_parser("ingest", help="把 .txt 口播稿转成结构化笔记并写入共享记忆")
    ingest.add_argument("file", help=".txt 文件路径，'-' 表示从标准输入读取")
    ingest.add_argument("--title", help="覆盖自动生成的标题")
    ingest.add_argument("--filename", help="记录在来源字段里的文件名")
    ingest.add_argument("--llm", action="store_true", help="强制使用 LLM 引擎（需要已配置 API key）")
    ingest.add_argument("--json", action="store_true", help="输出 JSON")

    notes = sub.add_parser("notes", help="列出笔记")
    notes.add_argument("--limit", type=int, default=20)
    notes.add_argument("--json", action="store_true")

    show = sub.add_parser("show", help="查看一篇笔记")
    show.add_argument("note_id")
    show.add_argument("--json", action="store_true", help="输出完整 JSON（含 items）")

    search = sub.add_parser("search", help="检索共享记忆")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--type", dest="item_type", default=None)
    search.add_argument("--agent", dest="filter_agent", default=None)
    search.add_argument("--json", action="store_true")

    context = sub.add_parser("context", help="按 token 预算组装给 agent 用的上下文包")
    context.add_argument("query")
    context.add_argument("--budget", type=int, default=800)
    context.add_argument("--agent", dest="filter_agent", default=None)
    context.add_argument("--json", action="store_true")

    remember = sub.add_parser("remember", help="写入一条原子记忆（不以笔记为来源）")
    remember.add_argument("text")
    remember.add_argument("--type", dest="item_type", default="fact",
                          choices=["fact", "decision", "task", "question", "point", "note"])
    remember.add_argument("--tags", default="", help="逗号分隔，例如 发布,视频")
    remember.add_argument("--owner", default=None, help="任务负责人")
    remember.add_argument("--due", default=None, help="任务截止时间")
    remember.add_argument("--json", action="store_true")

    items = sub.add_parser("items", help="列出原子记忆")
    items.add_argument("--type", dest="item_type", default=None)
    items.add_argument("--status", default=None)
    items.add_argument("--agent", dest="filter_agent", default=None)
    items.add_argument("--limit", type=int, default=50)
    items.add_argument("--json", action="store_true")

    done = sub.add_parser("done", help="把一条任务标记为完成")
    done.add_argument("item_id")

    stats = sub.add_parser("stats", help="查看共享记忆统计")
    stats.add_argument("--json", action="store_true")

    agents = sub.add_parser("agents", help="列出参与共享的 agent")
    agents.add_argument("--json", action="store_true")

    serve = sub.add_parser("serve", help="启动本地 HTTP API")
    serve.add_argument("--host", default=DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--allow-remote", action="store_true", help="允许绑定非 127.0.0.1 地址")
    serve.add_argument("--quiet", action="store_true")

    mcp = sub.add_parser("mcp", help="启动 MCP stdio 服务，供任意 agent 接入")
    mcp.add_argument("--agent", dest="mcp_agent", default=None, help="默认 agent 标识（默认取 MCP client 名称）")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    client = None if args.no_llm else LLMClient.from_env()
    if args.command == "ingest" and args.llm and client is None:
        print("错误：指定了 --llm，但没有配置 BRAIN_LLM_API_KEY。", file=sys.stderr)
        return 2

    try:
        store = BrainStore(args.db, llm_client=client)
    except Exception as exc:  # noqa: BLE001
        print(f"错误：无法打开数据库 {args.db}：{exc}", file=sys.stderr)
        return 1

    if args.command == "init":
        print(f"已初始化：{store.db_path}")
        return 0

    if args.command == "ingest":
        try:
            text, detected_name = _read_input(args.file)
            filename = args.filename or detected_name
            use_llm = not args.no_llm and (args.llm or client is not None)
            result = store.ingest_text(
                text,
                agent_id=_agent_name(args.agent),
                filename=filename,
                title_hint=args.title,
                use_llm=use_llm,
            )
        except (OSError, ValueError) as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 1
        note = result["note"]
        if args.json:
            print(dumps({
                "note_id": note["id"],
                "title": note["title"],
                "engine": note["note"].get("engine"),
                "deduplicated": result["deduplicated"],
                "items_created": result["items_created"],
                "items_linked": result["items_linked"],
                "tags": note["note"].get("tags", []),
                "warnings": note["note"].get("warnings", []),
                "markdown": note["markdown"],
            }, pretty=True))
            return 0
        if result["deduplicated"]:
            print(f"已存在相同内容（同一 agent），返回原笔记 {note['id']}：{note['title']}")
        else:
            print(
                f"已生成笔记 {note['id']}（引擎：{note['note'].get('engine')}，"
                f"新增记忆 {result['items_created']} 条，复用 {result['items_linked']} 条）"
            )
        print(f"标题：{note['title']}")
        tags = note["note"].get("tags") or []
        if tags:
            print(f"标签：{'、'.join(tags)}")
        print(f"摘要：{truncate(note['note'].get('summary', ''), 120)}")
        points = note["note"].get("key_points") or []
        if points:
            print("要点：")
            for point in points[:3]:
                print(f"  - {truncate(point, 90)}")
        for warning in note["note"].get("warnings") or []:
            print(f"⚠️  {warning}", file=sys.stderr)
        print(f"查看全文：brain show {note['id']}")
        return 0

    if args.command == "notes":
        rows = store.list_notes(limit=args.limit)
        if args.json:
            print(dumps({"notes": rows}, pretty=True))
            return 0
        print(_table(
            ["ID", "标题", "Agent", "条目", "来源", "创建时间"],
            [
                [row["id"], truncate(row["title"], 36), row["agent_id"],
                 str(row["item_count"]), row["filename"] or "-", row["created_at"][:16].replace("T", " ")]
                for row in rows
            ],
        ))
        return 0

    if args.command == "show":
        note = store.get_note(args.note_id)
        if note is None:
            print(f"错误：找不到笔记 {args.note_id}", file=sys.stderr)
            return 1
        print(dumps(note, pretty=True) if args.json else note["markdown"])
        return 0

    if args.command == "search":
        items = store.search(
            args.query,
            limit=args.limit,
            item_type=args.item_type,
            agent_id=args.filter_agent,
        )
        notes = store.search_notes(args.query, limit=5)
        if args.json:
            print(dumps({"query": args.query, "items": items, "notes": notes}, pretty=True))
            return 0
        if not items and not notes:
            print("没有命中。可以试试更短的关键词。")
            return 0
        for note in notes:
            print(f"[笔记] {truncate(note['title'], 46)}  {note['id']}")
        if notes and items:
            print()
        for item in items:
            agents = "、".join(sorted({c["agent_id"] for c in item["contributors"]}))
            extra = ""
            if item["type"] == "task":
                meta = item["meta"] or {}
                bits = []
                if meta.get("owner"):
                    bits.append(f"负责人 {meta['owner']}")
                if meta.get("due"):
                    bits.append(f"截止 {meta['due']}")
                if item["status"] == "done":
                    bits.append("已完成")
                if bits:
                    extra = f"  （{'，'.join(bits)}）"
            print(f"[{item['type']}] {truncate(item['text'], 90)}{extra}  · {agents} · {item['id']}")
        return 0

    if args.command == "context":
        pack = build_context_pack(
            store,
            args.query,
            budget_tokens=args.budget,
            agent_id=args.filter_agent,
        )
        if args.json:
            print(dumps(pack, pretty=True))
            return 0
        print(pack["markdown"])
        if pack["truncated"]:
            print(f"（已按 {args.budget} token 预算截断）", file=sys.stderr)
        return 0

    if args.command == "remember":
        tags = [tag.strip() for tag in args.tags.split(",") if tag.strip()]
        meta = {}
        if args.owner:
            meta["owner"] = args.owner
        if args.due:
            meta["due"] = args.due
        item = store.add_memory(
            args.text,
            agent_id=_agent_name(args.agent),
            item_type=args.item_type,
            tags=tags,
            meta=meta,
        )
        if args.json:
            print(dumps(item, pretty=True))
            return 0
        action = "新增" if item.pop("created", False) else "复用"
        print(f"已{action}记忆 {item['id']}：{truncate(item['text'], 80)}")
        return 0

    if args.command == "items":
        items = store.list_items(
            item_type=args.item_type,
            status=args.status,
            agent_id=args.filter_agent,
            limit=args.limit,
        )
        if args.json:
            print(dumps({"items": items}, pretty=True))
            return 0
        print(_table(
            ["ID", "类型", "状态", "内容", "Agent"],
            [
                [item["id"], item["type"], item["status"], truncate(item["text"], 52),
                 "、".join(sorted({c["agent_id"] for c in item["contributors"]}))]
                for item in items
            ],
        ))
        return 0

    if args.command == "done":
        item = store.set_item_status(args.item_id, "done")
        if item is None:
            print(f"错误：找不到记忆 {args.item_id}", file=sys.stderr)
            return 1
        print(f"已完成：{truncate(item['text'], 80)}")
        return 0

    if args.command == "stats":
        data = store.stats()
        if args.json:
            print(dumps(data, pretty=True))
            return 0
        totals = data["totals"]
        print(f"数据库：{data['db_path']}")
        print(
            f"笔记 {totals['notes']} · 记忆 {totals['items']} · agent {totals['agents']} · "
            f"来源 {totals['sources']} · 贡献 {totals['contributions']}"
        )
        if data["items_by_type"]:
            print("类型：" + "  ".join(f"{key} {value}" for key, value in data["items_by_type"].items()))
        if data["top_tags"]:
            print("热门标签：" + "  ".join(f"#{tag['tag']}×{tag['count']}" for tag in data["top_tags"]))
        if data["agents"]:
            print()
            print(_table(
                ["Agent", "记忆数", "最近活跃"],
                [[agent["id"], str(agent["item_count"]), agent["last_seen"][:16].replace("T", " ")]
                 for agent in data["agents"]],
            ))
        return 0

    if args.command == "agents":
        agents = store.stats()["agents"]
        if args.json:
            print(dumps({"agents": agents}, pretty=True))
            return 0
        print(_table(
            ["Agent", "记忆数", "首次出现", "最近活跃"],
            [[agent["id"], str(agent["item_count"]), agent["first_seen"][:16].replace("T", " "),
              agent["last_seen"][:16].replace("T", " ")] for agent in agents],
        ))
        return 0

    if args.command == "serve":
        if args.host not in ("127.0.0.1", "localhost", "::1") and not args.allow_remote:
            print("错误：该 API 没有鉴权，默认只允许绑定 127.0.0.1。如确需远程访问，请加 --allow-remote。", file=sys.stderr)
            return 2
        server = create_server(store, args.host, args.port, quiet=args.quiet)
        host, port = server.server_address[:2]
        print(f"共享大脑 API 已启动：http://{host}:{port}  （数据库：{store.db_path}）")
        print("按 Ctrl+C 停止。")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n已停止。")
        finally:
            server.server_close()
        return 0

    if args.command == "mcp":
        MCPServer(store, agent_id=args.mcp_agent).serve()
        return 0

    parser.error("unknown command")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
