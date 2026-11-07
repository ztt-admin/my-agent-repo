import json
import select
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = (ROOT / "samples" / "agent-workflow.txt").read_text(encoding="utf-8")


class McpTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        db = str(Path(self.tempdir.name) / "mcp.db")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "brain", "--db", db, "mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=str(ROOT),
        )

    def tearDown(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc.stdin.close()
        self.proc.stdout.close()
        self.proc.stderr.close()
        self.tempdir.cleanup()

    def send(self, message, expect_response=True):
        self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        if not expect_response:
            return None
        ready, _, _ = select.select([self.proc.stdout], [], [], 10)
        self.assertTrue(ready, "MCP server did not respond in time")
        line = self.proc.stdout.readline()
        self.assertTrue(line)
        return json.loads(line)

    def call(self, request_id, name, arguments):
        response = self.send({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        self.assertIn("result", response)
        return response["result"]

    def test_protocol_and_tools(self):
        init = self.send({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "unit-test", "version": "1"},
            },
        })
        self.assertEqual(init["result"]["serverInfo"]["name"], "shared-brain")
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect_response=False)
        tools = self.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {tool["name"] for tool in tools["result"]["tools"]}
        self.assertIn("ingest_transcript", names)
        self.assertIn("get_context_pack", names)
        self.assertEqual(self.send({"jsonrpc": "2.0", "id": 3, "method": "ping"})["result"], {})

    def test_ingest_search_context_and_task_lifecycle(self):
        self.send({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "unit-test", "version": "1"}},
        })
        ingest = self.call(2, "ingest_transcript", {"text": SAMPLE, "filename": "agent-workflow.txt"})
        self.assertFalse(ingest["isError"])
        payload = json.loads(ingest["content"][0]["text"])
        self.assertTrue(payload["note_id"])
        self.assertEqual(payload["engine"], "heuristic")

        search = self.call(3, "search_memory", {"query": "共享记忆 行动项", "type": "task", "limit": 5})
        items = json.loads(search["content"][0]["text"])["items"]
        self.assertTrue(items)
        task = items[0]

        context = self.call(4, "get_context_pack", {"query": "共享记忆", "budget_tokens": 300})
        self.assertIn("共享记忆上下文", context["content"][0]["text"])

        added = self.call(5, "add_memory", {
            "text": "MCP 接入文档需要覆盖 tools/list 与 tools/call。",
            "type": "fact",
            "tags": ["MCP", "文档"],
        })
        self.assertFalse(added["isError"])
        item = json.loads(added["content"][0]["text"])
        self.assertTrue(item["id"])

        done = self.call(6, "complete_task", {"item_id": task["id"]})
        self.assertFalse(done["isError"])
        self.assertEqual(json.loads(done["content"][0]["text"])["status"], "done")

        stats = self.call(7, "brain_stats", {})
        self.assertFalse(stats["isError"])


if __name__ == "__main__":
    unittest.main()
