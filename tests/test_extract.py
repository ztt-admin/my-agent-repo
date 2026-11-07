import unittest
from pathlib import Path

from brain.extract import build_heuristic_note, render_markdown, structure_transcript
from brain.transcript import parse_transcript

ROOT = Path(__file__).resolve().parent.parent


class ExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        text = (ROOT / "samples" / "product-kickoff.txt").read_text(encoding="utf-8")
        cls.transcript = parse_transcript(text, "product-kickoff.txt")
        cls.note = build_heuristic_note(cls.transcript)

    def test_core_fields_are_filled(self):
        self.assertTrue(self.note["title"])
        self.assertTrue(self.note["summary"])
        self.assertGreaterEqual(len(self.note["key_points"]), 3)
        self.assertGreaterEqual(len(self.note["tags"]), 3)
        self.assertGreaterEqual(len(self.note["sections"]), 2)
        self.assertEqual(self.note["engine"], "heuristic")

    def test_decisions_and_tasks_are_extracted(self):
        decisions = "".join(self.note["decisions"])
        self.assertIn("MVP", decisions)
        tasks = [task["text"] for task in self.note["tasks"]]
        self.assertTrue(any("小林负责" in task for task in tasks), tasks)
        owner_task = next(task for task in self.note["tasks"] if "小林负责" in task["text"])
        self.assertEqual(owner_task["owner"], "小林")
        self.assertIsNotNone(owner_task["due"])

    def test_questions_and_quotes(self):
        self.assertTrue(any("向量检索" in question for question in self.note["questions"]))
        self.assertTrue(any("记忆不是仓库" in quote for quote in self.note["quotes"]))

    def test_quote_not_duplicated_in_key_points(self):
        self.assertFalse(
            any("记忆不是仓库" in point for point in self.note["key_points"]),
            self.note["key_points"],
        )

    def test_sections_have_timestamps_and_points(self):
        first = self.note["sections"][0]
        self.assertIsNotNone(first["start_seconds"])
        self.assertTrue(first["heading"])
        self.assertTrue(first["points"])

    def test_markdown_renders_all_major_sections(self):
        markdown = render_markdown(self.note)
        for heading in ("# ", "## 摘要", "## 核心要点", "## 章节", "## 决策", "## 行动项", "## 标签"):
            self.assertIn(heading, markdown)

    def test_short_transcript_still_works(self):
        note = structure_transcript(parse_transcript("大家好，我们决定用 SQLite。", "short.txt"))
        self.assertTrue(note["title"])
        self.assertTrue(note["summary"])


if __name__ == "__main__":
    unittest.main()
