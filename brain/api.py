"""Local HTTP API for the shared brain (JSON, no auth, loopback by default)."""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__
from .context import build_context_pack
from .store import BrainStore
from .util import dumps

MAX_BODY_BYTES = 8 * 1024 * 1024


class ApiError(Exception):
    def __init__(self, status: int, message: str, code: str = "bad_request") -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


class BrainHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, store: BrainStore, *, quiet: bool = False) -> None:
        self.store = store
        self.quiet = quiet
        super().__init__(address, BrainRequestHandler)


class BrainRequestHandler(BaseHTTPRequestHandler):
    server_version = f"shared-brain/{__version__}"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------ #
    # HTTP plumbing
    # ------------------------------------------------------------------ #
    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_OPTIONS(self) -> None:
        self._send_raw(204, "", "text/plain")

    def log_message(self, format: str, *args) -> None:
        if not getattr(self.server, "quiet", False):
            super().log_message(format, *args)

    def _dispatch(self, method: str) -> None:
        try:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
            status, payload, content_type = self._route(method, path, query)
            self._send(status, payload, content_type)
        except ApiError as exc:
            self._send_json(exc.status, {"error": {"code": exc.code, "message": exc.message}})
        except ValueError as exc:
            self._send_json(400, {"error": {"code": "bad_request", "message": str(exc)}})
        except Exception as exc:  # noqa: BLE001 - return a safe 500
            self._send_json(500, {"error": {"code": "internal_error", "message": str(exc)}})

    def _send(self, status: int, payload, content_type: str) -> None:
        if content_type == "application/json":
            body = dumps(payload).encode("utf-8")
        else:
            body = str(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Agent-Id")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, payload) -> None:
        self._send(status, payload, "application/json")

    def _send_raw(self, status: int, payload: str, content_type: str) -> None:
        self._send(status, payload, content_type)

    # ------------------------------------------------------------------ #
    # Request helpers
    # ------------------------------------------------------------------ #
    def _query_int(self, query: dict, key: str, default: int, *, low: int, high: int) -> int:
        raw = query.get(key)
        if raw is None or raw == "":
            return default
        try:
            value = int(raw)
        except ValueError as exc:
            raise ApiError(400, f"`{key}` must be an integer") from exc
        return max(low, min(value, high))

    def _agent(self, query: dict | None = None, body: dict | None = None) -> str:
        for source in (body or {}, query or {}):
            value = source.get("agent_id")
            if isinstance(value, str) and value.strip():
                return value.strip()
        header = (self.headers.get("X-Agent-Id") or "").strip()
        return header or "api:anonymous"

    def _read_body(self) -> tuple[dict, str | None]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ApiError(400, "request body is required")
        if length > MAX_BODY_BYTES:
            raise ApiError(413, "request body is too large")
        raw = self.rfile.read(length)
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type == "text/plain":
            for encoding in ("utf-8", "gb18030"):
                try:
                    return {"text": raw.decode(encoding)}, content_type
                except UnicodeDecodeError:
                    continue
            raise ApiError(400, "text body must be UTF-8 or GB18030")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(400, "request body must be valid JSON") from exc
        if not isinstance(data, dict):
            raise ApiError(400, "request body must be a JSON object")
        return data, content_type

    # ------------------------------------------------------------------ #
    # Routing
    # ------------------------------------------------------------------ #
    def _route(self, method: str, path: str, query: dict):
        if path in ("/", "/api"):
            if method != "GET":
                raise ApiError(405, "method not allowed")
            return 200, {
                "name": "shared-brain",
                "version": __version__,
                "endpoints": [
                    "GET  /healthz",
                    "POST /api/notes",
                    "GET  /api/notes",
                    "GET  /api/notes/{id}",
                    "GET  /api/notes/{id}/markdown",
                    "GET  /api/items",
                    "POST /api/items",
                    "GET  /api/items/{id}",
                    "POST /api/items/{id}/status",
                    "GET  /api/search?q=",
                    "GET  /api/context?q=&budget=800",
                    "GET  /api/agents",
                    "GET  /api/stats",
                ],
            }, "application/json"

        if path == "/healthz":
            if method != "GET":
                raise ApiError(405, "method not allowed")
            return 200, {
                "ok": True,
                "version": __version__,
                "db": self.server.store.db_path,
                "llm_configured": self.server.store.llm_client is not None,
            }, "application/json"

        if path == "/api/stats":
            if method != "GET":
                raise ApiError(405, "method not allowed")
            return 200, self.server.store.stats(), "application/json"

        if path == "/api/agents":
            if method != "GET":
                raise ApiError(405, "method not allowed")
            return 200, {"agents": self.server.store.stats()["agents"]}, "application/json"

        if path == "/api/notes":
            if method == "POST":
                return self._ingest()
            if method != "GET":
                raise ApiError(405, "method not allowed")
            limit = self._query_int(query, "limit", 20, low=1, high=100)
            return 200, {"notes": self.server.store.list_notes(limit=limit)}, "application/json"

        match = re.fullmatch(r"/api/notes/([A-Za-z0-9_]+)", path)
        if match:
            if method != "GET":
                raise ApiError(405, "method not allowed")
            note = self.server.store.get_note(match.group(1))
            if note is None:
                raise ApiError(404, "note not found", "not_found")
            return 200, note, "application/json"

        match = re.fullmatch(r"/api/notes/([A-Za-z0-9_]+)/markdown", path)
        if match:
            if method != "GET":
                raise ApiError(405, "method not allowed")
            note = self.server.store.get_note(match.group(1))
            if note is None:
                raise ApiError(404, "note not found", "not_found")
            return 200, note["markdown"], "text/markdown"

        if path == "/api/items":
            if method == "POST":
                return self._add_item()
            if method != "GET":
                raise ApiError(405, "method not allowed")
            items = self.server.store.list_items(
                item_type=query.get("type") or None,
                agent_id=query.get("agent") or None,
                status=query.get("status") or None,
                limit=self._query_int(query, "limit", 50, low=1, high=200),
            )
            return 200, {"items": items, "count": len(items)}, "application/json"

        match = re.fullmatch(r"/api/items/([A-Za-z0-9_]+)", path)
        if match:
            if method != "GET":
                raise ApiError(405, "method not allowed")
            item = self.server.store.get_item(match.group(1))
            if item is None:
                raise ApiError(404, "memory item not found", "not_found")
            return 200, item, "application/json"

        match = re.fullmatch(r"/api/items/([A-Za-z0-9_]+)/status", path)
        if match:
            if method != "POST":
                raise ApiError(405, "method not allowed")
            body, _ = self._read_body()
            status = body.get("status")
            if status not in ("open", "done", "archived", "active"):
                raise ApiError(400, "status must be one of open, done, archived, active")
            item = self.server.store.set_item_status(match.group(1), status)
            if item is None:
                raise ApiError(404, "memory item not found", "not_found")
            return 200, item, "application/json"

        if path == "/api/search":
            if method != "GET":
                raise ApiError(405, "method not allowed")
            q = (query.get("q") or "").strip()
            if not q:
                raise ApiError(400, "`q` is required")
            limit = self._query_int(query, "limit", 20, low=1, high=100)
            items = self.server.store.search(
                q,
                limit=limit,
                item_type=query.get("type") or None,
                agent_id=query.get("agent") or None,
            )
            notes = self.server.store.search_notes(q, limit=10)
            return 200, {"query": q, "items": items, "notes": notes, "count": len(items)}, "application/json"

        if path == "/api/context":
            if method != "GET":
                raise ApiError(405, "method not allowed")
            q = (query.get("q") or "").strip()
            budget = self._query_int(query, "budget", 800, low=120, high=20000)
            limit = self._query_int(query, "limit", 40, low=1, high=100)
            pack = build_context_pack(
                self.server.store,
                q,
                budget_tokens=budget,
                limit=limit,
                agent_id=query.get("agent") or None,
            )
            return 200, pack, "application/json"

        raise ApiError(404, "unknown endpoint", "not_found")

    # ------------------------------------------------------------------ #
    # Handlers
    # ------------------------------------------------------------------ #
    def _ingest(self):
        body, _ = self._read_body()
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ApiError(400, "`text` is required")
        use_llm = bool(body.get("use_llm", True))
        result = self.server.store.ingest_text(
            text,
            agent_id=self._agent(body=body),
            filename=str(body.get("filename") or ""),
            title_hint=body.get("title"),
            use_llm=use_llm,
        )
        note = result["note"]
        return 200, {
            "note_id": note["id"],
            "title": note["title"],
            "engine": note["note"].get("engine"),
            "deduplicated": result["deduplicated"],
            "items_created": result["items_created"],
            "items_linked": result["items_linked"],
            "tags": note["note"].get("tags", []),
            "key_points": note["note"].get("key_points", [])[:3],
            "warnings": note["note"].get("warnings", []),
            "markdown": note["markdown"],
        }, "application/json"

    def _add_item(self):
        body, _ = self._read_body()
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ApiError(400, "`text` is required")
        tags = body.get("tags") or []
        if isinstance(tags, str):
            tags = [tag.strip() for tag in tags.split(",") if tag.strip()]
        if not isinstance(tags, list):
            raise ApiError(400, "`tags` must be a list or comma separated string")
        item = self.server.store.add_memory(
            text,
            agent_id=self._agent(body=body),
            item_type=str(body.get("type") or "fact"),
            tags=[str(tag) for tag in tags],
        )
        return 200, item, "application/json"


def create_server(
    store: BrainStore,
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    quiet: bool = False,
) -> BrainHTTPServer:
    return BrainHTTPServer((host, port), store, quiet=quiet)
