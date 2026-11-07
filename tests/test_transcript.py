import unittest

from brain.transcript import parse_transcript
from brain.util import normalize_text, split_sentences


class TranscriptTests(unittest.TestCase):
    def test_timestamps_and_speakers(self):
        transcript = parse_transcript(
            "[00:00:05] 主持人：大家好，欢迎来到今天的分享。\n"
            "[00:01:12] 嘉宾：我们先说结论。第一个 MVP 必须能跑。\n"
            "[01:02:03] 嘉宾：最后留一个开放问题。",
            "demo.txt",
        )
        self.assertTrue(transcript.has_timestamps)
        self.assertEqual(transcript.speakers, ["主持人", "嘉宾"])
        self.assertEqual(transcript.segments[0].start_seconds, 5)
        self.assertEqual(transcript.segments[0].speaker, "主持人")
        self.assertGreaterEqual(len(transcript.segments), 4)
        self.assertEqual(transcript.duration_seconds, 3723 - 5)
        self.assertFalse(transcript.segments[2].start_seconds)  # only the first sentence inherits the timestamp

    def test_plain_text_without_metadata(self):
        transcript = parse_transcript("第一句话。第二句话。\n\n第三句话。", "plain.txt")
        self.assertFalse(transcript.has_timestamps)
        self.assertEqual(transcript.speakers, [])
        self.assertEqual(len(transcript.segments), 3)

    def test_role_suffix_speaker_labels(self):
        transcript = parse_transcript(
            "[00:00] 内容 agent：我的用法是先导入口播稿。\n"
            "[00:10] 销售 agent：我需要按行业取上下文包。",
            "roles.txt",
        )
        self.assertEqual(transcript.speakers, ["内容 agent", "销售 agent"])
        self.assertEqual(transcript.segments[0].text, "我的用法是先导入口播稿。")

    def test_quote_is_not_split_across_segments(self):
        sentences = split_sentences("最后送大家一句话：“越用越厚，而不是越用越乱。”共享大脑的价值在复用。")
        self.assertEqual(len(sentences), 1)
        self.assertIn("“越用越厚，而不是越用越乱。”", sentences[0])
        self.assertIn("共享大脑的价值在复用", sentences[0])

    def test_normalize_text(self):
        self.assertEqual(normalize_text("a\r\n\r\n\r\nb"), "a\n\nb")


if __name__ == "__main__":
    unittest.main()
