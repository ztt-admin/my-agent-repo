"""Parse spoken-word .txt transcripts into timestamped, speaker-attributed segments.

Supported (all optional) shapes::

    主持人：大家好，欢迎来到今天的分享。
    [00:12:30] 嘉宾：我们先说结论。
    [00:05] Speaker 1: The first point is ...

If a file has no timestamps or speaker labels, it is still parsed as plain
paragraphs, which keeps the MVP useful for hand-copied transcripts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .util import format_timestamp, normalize_text, parse_hms, split_sentences

_TS_LINE_RE = re.compile(
    r"^\s*(?:[\[\(【]\s*)?((?:\d{1,2}:)?\d{1,2}:\d{2})(?:[.,]\d+)?\s*(?:[\]\)】])?\s*"
)
_SPEAKER_PREFIX_RE = re.compile(
    r"^(?:【([^】\n]{1,16})】|\[([^\]\n]{1,16})\]\s*[：:]?\s*|([^：:\n]{1,12})\s*[：:]\s*)(.*)$"
)
_ROLE_SPEAKER_RE = re.compile(
    r"^(?:说话人|主持人|嘉宾|采访者|主讲人|旁白|speaker|host|guest|interviewer|s\d+)\s*\d*$",
    re.IGNORECASE,
)
_ROLE_SUFFIX_RE = re.compile(
    r"(?:agent|经理|负责人|工程师|主持人|主讲人|嘉宾|采访者|运营|销售|产品|设计|开发|"
    r"speaker|host|guest|interviewer)$",
    re.IGNORECASE,
)
_NON_SPEAKER = {
    "注意", "问题", "方法", "结论", "建议", "原因", "所以", "但是", "不过", "其实",
    "例如", "比如", "第一", "第二", "第三", "首先", "其次", "最后", "总结", "补充",
    "说明", "重点", "核心", "背景", "目标", "现状", "方案", "结果", "风险", "下一步",
    "待办", "今天", "现在", "目前", "大家", "时间", "地点", "案例", "数据", "观点",
    "这里", "另外", "其中", "总之", "也就是说", "换句话", "简单说", "一句话", "注意：",
    "举个例子", "顺便", "对了", "好", "是的", "回答", "提问", "观众", "读者",
}


@dataclass
class Segment:
    index: int
    start_seconds: int | None
    speaker: str | None
    text: str


@dataclass
class Transcript:
    filename: str
    raw: str
    segments: list[Segment] = field(default_factory=list)
    speakers: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(seg.text for seg in self.segments)

    @property
    def chars(self) -> int:
        return len(self.text)

    @property
    def has_timestamps(self) -> bool:
        return any(seg.start_seconds is not None for seg in self.segments)

    @property
    def duration_seconds(self) -> int | None:
        starts = [seg.start_seconds for seg in self.segments if seg.start_seconds is not None]
        if len(starts) < 2:
            return None
        return max(starts) - min(starts)

    def stats(self) -> dict:
        duration = self.duration_seconds
        return {
            "filename": self.filename,
            "chars": self.chars,
            "segments": len(self.segments),
            "speakers": list(self.speakers),
            "has_timestamps": self.has_timestamps,
            "duration_seconds": duration,
            "duration_label": format_timestamp(duration) if duration else None,
        }


def _prefix_candidates(lines: list[str]) -> tuple[dict[str, int], set[str]]:
    """Find repeated 'Name:' prefixes so we do not mistake plain labels for speakers."""
    counts: dict[str, int] = {}
    bracketed: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        ts = _TS_LINE_RE.match(stripped)
        if ts:
            stripped = stripped[ts.end():].strip()
        if not stripped:
            continue
        match = _SPEAKER_PREFIX_RE.match(stripped)
        if not match:
            continue
        bracket_name = match.group(1) or match.group(2)
        colon_name = match.group(3)
        name = (bracket_name or colon_name or "").strip()
        if not name or len(name) > 12 or re.search(r"[。！？!?，,；;]", name):
            continue
        if bracket_name:
            bracketed.add(name)
        counts[name] = counts.get(name, 0) + 1
    return counts, bracketed


def parse_transcript(text: str, filename: str = "") -> Transcript:
    raw = normalize_text(text)
    if not raw:
        raise ValueError("transcript is empty")

    lines = raw.split("\n")
    counts, bracketed = _prefix_candidates(lines)
    known_speakers = {
        name
        for name, count in counts.items()
        if count >= 2
        or name in bracketed
        or _ROLE_SPEAKER_RE.match(name)
        or (len(name) <= 10 and _ROLE_SUFFIX_RE.search(name))
    }
    known_speakers = {name for name in known_speakers if name not in _NON_SPEAKER}

    segments: list[Segment] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        start: int | None = None
        match = _TS_LINE_RE.match(line)
        if match:
            start = parse_hms(match.group(1))
            line = line[match.end():].strip()

        speaker: str | None = None
        match = _SPEAKER_PREFIX_RE.match(line)
        if match:
            bracket_name = match.group(1) or match.group(2)
            colon_name = match.group(3)
            candidate = (bracket_name or colon_name or "").strip()
            if candidate in known_speakers:
                speaker = candidate
                line = (match.group(4) or "").strip()

        if not line:
            continue
        for sentence_index, sentence in enumerate(split_sentences(line)):
            segments.append(
                Segment(
                    index=len(segments),
                    # Only the first sentence inherits the timestamp so sections
                    # are not split inside one timestamped paragraph.
                    start_seconds=start if start is not None and sentence_index == 0 else None,
                    speaker=speaker,
                    text=sentence,
                )
            )
    if not segments:
        raise ValueError("transcript has no usable sentences")

    speakers: list[str] = []
    for seg in segments:
        if seg.speaker and seg.speaker not in speakers:
            speakers.append(seg.speaker)
    return Transcript(filename=filename, raw=raw, segments=segments, speakers=speakers)
