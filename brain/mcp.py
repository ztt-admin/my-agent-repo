"""MCP (Model Context Protocol) stdio server exposing the shared brain as tools.

Any MCP-capable agent (Claude Desktop/Code, Cursor, Dify, custom runtimes) can
attach to the same on-disk memory and call: ingest_transcript, search_memory,
get_context_pack, add_memory, list_notes, complete_task, brain_stats.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

from . import __version__
from .context import build_context_pack
from .store import BrainStore
from .util import dumps, read_text_file, truncate

PROTOCOL_VERSION = "2024-11-05"

TOOLS: list[dict[str, Any]] = [
    {
        "name": "ingest_transcript",
        "description": (
            "Turn a spoken-word transcript (.txt content or local path) into a structured note: "
            "title, summary, key points, sections, decisions, tasks, questions, quotes and tags. "
            "The note and its atomic memory items are stored in the shared brain."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Transcript text. Provide this or `path`."},
                "path": {"type": "string", "description": "Local .txt file path. Provide this or `text`."},
                "filename": {"type": "string", "description": "Display filename for provenance."},
                "title": {"type": "string", "description": "Optional title override."},
                "agent_id": {"type": "string", "description": "Contributing agent id; defaults to this MCP client."},
                "use_llm": {"type": "boolean", "description": "Use the configured LLM engine when available."},
            },
        },
    },
    {
        "name": "search_memory",
        "description": "Search the shared structured memory (decisions, tasks, points, questions, quotes).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keywords or a question."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                "type": {
                    "type": "string",
                    "enum": ["summary", "point", "decision", "task", "question", "quote", "fact", "note"],
                },
                "agent_id": {"type": "string", "description": "Only items contributed by this agent."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_context_pack",
        "description": (
            "Assemble a token-budgeted context pack for a topic: top decisions, tasks and points "
            "with agent/provenance citations and a Markdown rendering. Use this before writing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Topic or question."},
                "budget_tokens": {"type": "integer", "minimum": 120, "maximum": 20000, "default": 800},
                "agent_id": {"type": "string", "description": "Only memory contributed by this agent."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "add_memory",
        "description": "Write one atomic memory item (fact/decision/task/idea) into the shared brain.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The memory content, one self-contained sentence."},
                "type": {
                    "type": "string",
                    "enum": ["fact", "decision", "task", "question", "point", "note"],
                    "default": "fact",
                },
                "tags": {"type": "array", "items": {"type": "string"}},
                "agent_id": {"type": "string"},
                "owner": {"type": "string", "description": "Task owner, if any."},
                "due": {"type": "string", "description": "Task due date, if any."},
            },
            "required": ["text"],
        },
    },
    {
        "name": "list_notes",
        "description": "List recent structured notes in the shared brain.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}},
        },
    },
    {
        "name": "complete_task",
        "description": "Mark a task item done/open/archived.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "item_id": {"type": "string"},
                "status": {"type": "string", "enum": ["done", "open", "archived"], "default": "done"},
            },
            "required": ["item_id"],
        },
    },
    {
        "name": "brain_stats",
        "description": "Show shared brain totals: notes, items, agents, types and top tags.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _text_result(text: str, *, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


class MCPServer:
    def __init__(self, store: BrainStore, agent_id: str | None = None) -> None:
        self.store = store
        self.agent_id = agent_id or "mcp:agent"

    # ------------------------------------------------------------------ #
    # Protocol
    # ------------------------------------------------------------------ #
    def handle(self, message: dict) -> dict | None:
        is_notification = "id" not in message
        message_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}

        try:
            if method == "initialize":
                client_name = ""
                client_info = params.get("clientInfo") or {}
                if isinstance(client_info, dict):
                    client_name = str(client_info.get("name") or "")
                if client_name:
                    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", client_name).strip("-")[:32] or "agent"
                    self.agent_id = f"mcp:{safe}"
                result = {
                    "protocolVersion": str(params.get("protocolVersion") or PROTOCOL_VERSION),
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "shared-brain", "version": __version__},
                    "instructions": (
                        "Shared structured memory. Ingest transcripts, search decisions/tasks, "
                        "and pull a context pack before producing work."
                    ),
                }
                return None if is_notification else self._ok(message_id, result)

            if method == "notifications/initialized":
                return None
            if method == "ping":
                return None if is_notification else self._ok(message_id, {})
            if method == "tools/list":
                return None if is_notification else self._ok(message_id, {"tools": TOOLS})
            if method == "tools/call":
                name = params.get("name")
                arguments = params.get("arguments") or {}
                if not isinstance(arguments, dict):
                    return self._error(message_id, -32602, "arguments must be an object")
                if not isinstance(name, str) or not any(tool["name"] == name for tool in TOOLS):
                    return self._error(message_id, -32602, f"unknown tool: {name}")
                return None if is_notification else self._ok(
                    message_id, self._call_tool(name, arguments)
                )
            if is_notification:
                return None
            return self._error(message_id, -32601, f"method not found: {method}")
        except Exception as exc:  # noqa: BLE001 - tool failures are reported, not crashed
            if is_notification:
                return None
            return self._error(message_id, -32603, f"{type(exc).__name__}: {exc}")

    def _call_tool(self, name: str, arguments: dict) -> dict:
        try:
            if name == "ingest_transcript":
                text = arguments.get("text")
                if not isinstance(text, str) or not text.strip():
                    path = arguments.get("path")
                    if not isinstance(path, str) or not path:
                        return _text_result("Provide either `text` or `path`.", is_error=True)
                    text = read_text_file(path)
                    filename = str(arguments.get("filename") or path)
                else:
                    filename = str(arguments.get("filename") or "mcp-粘贴文本")
                use_llm = bool(arguments.get("use_llm", self.store.llm_client is not None))
                agent_id = str(arguments.get("agent_id") or self.agent_id)
                result = self.store.ingest_text(
                    text,
                    agent_id=agent_id,
                    filename=filename,
                    title_hint=arguments.get("title"),
                    use_llm=use_llm,
                )
                note = result["note"]
                payload = {
                    "note_id": note["id"],
                    "title": note["title"],
                    "engine": note["note"].get("engine"),
                    "deduplicated": result["deduplicated"],
                    "items_created": result["items_created"],
                    "items_linked": result["items_linked"],
                    "tags": note["note"].get("tags", []),
                    "warnings": note["note"].get("warnings", []),
                    "markdown": note["markdown"],
                }
                return _text_result(dumps(payload, pretty=True))

            if name == "search_memory":
                query = str(arguments.get("query") or "").strip()
                if not query:
                    return _text_result("`query` is required.", is_error=True)
                items = self.store.search(
                    query,
                    limit=int(arguments.get("limit", 10)),
                    item_type=arguments.get("type") or None,
                    agent_id=arguments.get("agent_id") or None,
                )
                notes = self.store.search_notes(query, limit=5)
                return _text_result(dumps({"query": query, "items": items, "notes": notes}, pretty=True))

            if name == "get_context_pack":
                query = str(arguments.get("query") or "").strip()
                if not query:
                    return _text_result("`query` is required.", is_error=True)
                pack = build_context_pack(
                    self.store,
                    query,
                    budget_tokens=int(arguments.get("budget_tokens", 800)),
                    agent_id=arguments.get("agent_id") or None,
                )
                return _text_result(pack["markdown"])

            if name == "add_memory":
                text = str(arguments.get("text") or "").strip()
                if not text:
                    return _text_result("`text` is required.", is_error=True)
                tags = arguments.get("tags") or []
                if isinstance(tags, str):
                    tags = [tag.strip() for tag in tags.split(",") if tag.strip()]
                meta = {}
                if arguments.get("owner"):
                    meta["owner"] = str(arguments["owner"])
                if arguments.get("due"):
                    meta["due"] = str(arguments["due"])
                item = self.store.add_memory(
                    text,
                    agent_id=str(arguments.get("agent_id") or self.agent_id),
                    item_type=str(arguments.get("type") or "fact"),
                    tags=[str(tag) for tag in tags],
                    meta=meta,
                )
                return _text_result(dumps(item, pretty=True))

            if name == "list_notes":
                notes = self.store.list_notes(limit=int(arguments.get("limit", 10)))
                return _text_result(dumps({"notes": notes}, pretty=True))

            if name == "complete_task":
                item_id = str(arguments.get("item_id") or "").strip()
                if not item_id:
                    return _text_result("`item_id` is required.", is_error=True)
                item = self.store.set_item_status(item_id, str(arguments.get("status") or "done"))
                if item is None:
                    return _text_result(f"Task not found: {item_id}", is_error=True)
                return _text_result(dumps(item, pretty=True))

            if name == "brain_stats":
                return _text_result(dumps(self.store.stats(), pretty=True))

            return _text_result(f"Unknown tool: {name}", is_error=True)
        except ValueError as exc:
            return _text_result(str(exc), is_error=True)

    def _ok(self, message_id, result) -> dict:
        return {"jsonrpc": "2.0", "id": message_id, "result": result}

    def _error(self, message_id, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": truncate(message, 500)}}

    # ------------------------------------------------------------------ #
    # Transport
    # ------------------------------------------------------------------ #
    def serve(self, stdin=None, stdout=None) -> None:
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for raw_line in stdin:
            line = raw_line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                stdout.write(dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}) + "\n")
                stdout.flush()
                continue
            if not isinstance(message, dict):
                continue
            response = self.handle(message)
            if response is not None:
                stdout.write(dumps(response) + "\n")
                stdout.flush()
