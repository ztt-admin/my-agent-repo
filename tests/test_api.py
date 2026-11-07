import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from brain.api import create_server
from brain.store import BrainStore

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = (ROOT / "samples" / "product-kickoff.txt").read_text(encoding="utf-8")


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.store = BrainStore(str(Path(cls.tempdir.name) / "api.db"))
        cls.server = create_server(cls.store, port=0, quiet=True)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tempdir.cleanup()

    def request(self, method, path, payload=None, *, raw=False, headers=None):
        data = None
        request_headers = dict(headers or {})
        if payload is not None:
            if raw:
                data = payload.encode("utf-8")
            else:
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                request_headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            method=method,
            headers=request_headers,
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, response.read().decode("utf-8"), response.headers
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8"), error.headers

    def test_health_and_index(self):
        status, body, _ = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        status, body, _ = self.request("GET", "/api")
        self.assertEqual(status, 200)
        self.assertIn("/api/context", body)

    def test_ingest_note_and_markdown(self):
        status, body, _ = self.request(
            "POST",
            "/api/notes",
            {"text": SAMPLE, "filename": "product-kickoff.txt", "agent_id": "api:test"},
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        note_id = payload["note_id"]
        self.assertTrue(payload["title"])
        self.assertFalse(payload["deduplicated"])
        status, body, _ = self.request("GET", f"/api/notes/{note_id}")
        self.assertEqual(status, 200)
        self.assertIn("markdown", json.loads(body))
        status, body, headers = self.request("GET", f"/api/notes/{note_id}/markdown")
        self.assertEqual(status, 200)
        self.assertIn("text/markdown", headers.get("Content-Type"))
        self.assertIn("## 决策", body)

    def test_search_context_and_stats(self):
        self.request("POST", "/api/notes", {"text": SAMPLE, "agent_id": "api:search"})
        status, body, _ = self.request("GET", "/api/search?q=%E5%85%B1%E4%BA%AB%E8%AE%B0%E5%BF%86&limit=5")
        self.assertEqual(status, 200)
        self.assertGreater(json.loads(body)["count"], 0)
        status, body, _ = self.request("GET", "/api/context?q=MVP&budget=300")
        self.assertEqual(status, 200)
        pack = json.loads(body)
        self.assertLessEqual(pack["used_tokens"], 300)
        self.assertIn("markdown", pack)
        status, body, _ = self.request("GET", "/api/stats")
        self.assertEqual(status, 200)
        self.assertGreater(json.loads(body)["totals"]["notes"], 0)

    def test_items_and_status(self):
        status, body, _ = self.request(
            "POST",
            "/api/items",
            {"text": "API 测试任务，负责人：小林，截止：周五。", "type": "task", "agent_id": "api:test"},
        )
        self.assertEqual(status, 200)
        item_id = json.loads(body)["id"]
        status, body, _ = self.request("GET", f"/api/items/{item_id}")
        self.assertEqual(status, 200)
        status, body, _ = self.request("POST", f"/api/items/{item_id}/status", {"status": "done"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "done")

    def test_raw_text_and_agent_header(self):
        status, body, _ = self.request(
            "POST",
            "/api/notes",
            SAMPLE,
            raw=True,
            headers={"Content-Type": "text/plain", "X-Agent-Id": "api:raw"},
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["note_id"])

    def test_error_contract(self):
        status, body, _ = self.request("GET", "/api/notes/nope")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"]["code"], "not_found")
        status, body, _ = self.request("POST", "/api/notes", {})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "bad_request")
        status, body, _ = self.request("GET", "/api/search")
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
