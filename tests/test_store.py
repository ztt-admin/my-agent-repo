import tempfile
import unittest
from pathlib import Path

from brain.context import build_context_pack
from brain.store import BrainStore

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = (ROOT / "samples" / "product-kickoff.txt").read_text(encoding="utf-8")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tempdir.name) / "brain.db")
        self.store = BrainStore(self.db)

    def tearDown(self):
        self.store = None
        self.tempdir.cleanup()

    def test_ingest_is_idempotent_per_agent(self):
        first = self.store.ingest_text(SAMPLE, agent_id="agent:a", filename="demo.txt")
        second = self.store.ingest_text(SAMPLE, agent_id="agent:a", filename="demo.txt")
        self.assertFalse(first["deduplicated"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(first["note"]["id"], second["note"]["id"])
        self.assertEqual(first["note"]["note"]["schema"], "shared-brain/note/v1")
        self.assertEqual(self.store.stats()["totals"]["items"], self.store.stats()["totals"]["items"])
        self.assertEqual(self.store.stats()["totals"]["notes"], 1)

    def test_two_agents_share_one_item_with_two_contributors(self):
        first = self.store.ingest_text(SAMPLE, agent_id="agent:a", filename="a.txt")
        second = self.store.ingest_text(SAMPLE, agent_id="agent:b", filename="b.txt")
        self.assertEqual(first["items_created"], second["items_created"] + second["items_linked"])
        self.assertGreater(second["items_linked"], 0)
        shared = self.store.search("MVP 先不做知识图谱")
        self.assertTrue(shared)
        top = shared[0]
        agents = {contributor["agent_id"] for contributor in top["contributors"]}
        self.assertEqual(agents, {"agent:a", "agent:b"})
        self.assertEqual(len(second["note"]["items"]), len(first["note"]["items"]))

    def test_manual_memory_and_status(self):
        item = self.store.add_memory(
            "周五之前把 MCP 接入文档发到群里，负责人：小林。",
            agent_id="agent:ops",
            item_type="task",
            tags=["文档", "MCP"],
            meta={"owner": "小林", "due": "周五"},
        )
        self.assertTrue(item["created"])
        self.assertEqual(item["status"], "open")
        updated = self.store.set_item_status(item["id"], "done")
        self.assertEqual(updated["status"], "done")
        duplicate = self.store.add_memory(
            "周五之前把 MCP 接入文档发到群里，负责人：小林。",
            agent_id="agent:ops",
            item_type="task",
        )
        self.assertFalse(duplicate["created"])
        self.assertEqual(len(self.store.list_items(item_type="task")), 1)

    def test_search_supports_chinese_and_ascii_and_filters(self):
        self.store.ingest_text(SAMPLE, agent_id="agent:a", filename="a.txt")
        self.assertTrue(self.store.search("共享记忆"))
        self.assertTrue(self.store.search("SQLite"))
        tasks = self.store.search("负责", item_type="task")
        self.assertTrue(tasks)
        self.assertTrue(all(item["type"] == "task" for item in tasks))
        self.assertEqual(self.store.search("完全不存在"), [])

    def test_context_pack_respects_budget(self):
        self.store.ingest_text(SAMPLE, agent_id="agent:a", filename="a.txt")
        self.store.add_memory("补充一条测试任务，负责人：小林，截止：本周五。", agent_id="agent:b", item_type="task")
        for budget in (180, 400, 800):
            pack = build_context_pack(self.store, "共享记忆 MVP 上下文包", budget_tokens=budget)
            self.assertLessEqual(pack["used_tokens"], budget)
            self.assertTrue(pack["markdown"].startswith("# 共享记忆上下文"))
        sparse = build_context_pack(self.store, "完全不存在的话题", budget_tokens=200)
        self.assertEqual(sparse["item_count"], 0)
        self.assertIn("没有命中", sparse["markdown"])

    def test_stats_lists_agents_and_types(self):
        self.store.ingest_text(SAMPLE, agent_id="agent:a", filename="a.txt")
        stats = self.store.stats()
        self.assertEqual(stats["totals"]["notes"], 1)
        self.assertEqual(stats["totals"]["agents"], 1)
        self.assertIn("decision", stats["items_by_type"])
        self.assertEqual(stats["agents"][0]["id"], "agent:a")


if __name__ == "__main__":
    unittest.main()
