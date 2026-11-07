import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from brain.llm import LLMClient, extract_json_object
from brain.store import BrainStore

NOTES_JSON = {
    "title": "LLM 整理的产品会笔记",
    "summary": "团队决定先做结构化记忆，再做语义检索。",
    "key_points": ["MVP 采用本地 SQLite", "MCP 作为统一接入方式", "先保证来源可追溯"],
    "sections": [{"heading": "范围", "points": ["只做 .txt 导入"], "start_seconds": 0, "end_seconds": 60}],
    "decisions": ["先做结构化记忆，再做向量检索。"],
    "tasks": [{"text": "小林周五前录好演示视频。", "owner": "小林", "due": "周五"}],
    "questions": ["权限模型怎么设计？"],
    "quotes": ["记忆不是仓库，而是工作台。"],
    "tags": ["MVP", "结构化记忆", "MCP", "SQLite"],
}


class _StubHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        self.server.requests.append(body)
        content = self.server.content
        payload = {"choices": [{"message": {"content": content}}]}
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args):
        pass


class _StubServer(ThreadingHTTPServer):
    def __init__(self, *args, content="{}", **kwargs):
        super().__init__(*args, **kwargs)
        self.content = content
        self.requests = []


class LlmTests(unittest.TestCase):
    def start_server(self, content):
        server = _StubServer(("127.0.0.1", 0), _StubHandler, content=content)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def test_extract_json_object(self):
        self.assertEqual(extract_json_object('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(extract_json_object('prefix {"b": 2} suffix'), {"b": 2})

    def test_llm_engine_is_used_when_configured(self):
        server = self.start_server(json.dumps(NOTES_JSON, ensure_ascii=False))
        client = LLMClient("test-key", base_url=f"http://127.0.0.1:{server.server_address[1]}/v1", model="stub-model")
        with tempfile.TemporaryDirectory() as tempdir:
            store = BrainStore(str(Path(tempdir) / "llm.db"), llm_client=client)
            result = store.ingest_text("测试口播稿。", agent_id="agent:llm", filename="llm.txt")
        note = result["note"]["note"]
        self.assertEqual(note["engine"], "llm")
        self.assertEqual(note["title"], NOTES_JSON["title"])
        self.assertEqual(note["tasks"][0]["owner"], "小林")
        self.assertTrue(server.requests)

    def test_llm_failure_falls_back_to_heuristic(self):
        server = self.start_server("this is not json")
        client = LLMClient("test-key", base_url=f"http://127.0.0.1:{server.server_address[1]}/v1", model="stub-model")
        with tempfile.TemporaryDirectory() as tempdir:
            store = BrainStore(str(Path(tempdir) / "fallback.db"), llm_client=client)
            result = store.ingest_text(
                "我们决定用 SQLite 做共享记忆。小林负责周五之前完成演示。",
                agent_id="agent:llm",
                filename="fallback.txt",
            )
        note = result["note"]["note"]
        self.assertEqual(note["engine"], "heuristic")
        self.assertTrue(note["warnings"])

    def test_from_env(self):
        self.assertIsNone(LLMClient.from_env({}))
        client = LLMClient.from_env({
            "BRAIN_LLM_API_KEY": "k",
            "BRAIN_LLM_BASE_URL": "http://example.test/v1",
            "BRAIN_LLM_MODEL": "m",
            "BRAIN_LLM_TIMEOUT": "5",
        })
        self.assertEqual(client.model, "m")
        self.assertEqual(client.base_url, "http://example.test/v1")


if __name__ == "__main__":
    unittest.main()
