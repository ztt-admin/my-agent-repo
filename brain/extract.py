"""Turn a parsed transcript into a structured note.

Two engines:

* ``heuristic`` - deterministic, offline, zero-dependency. Cue-phrase rules pick
  summaries, decisions, action items and questions; n-gram frequency picks tags
  and section topics. This is the default and the fallback.
* ``llm`` - one call to an OpenAI-compatible JSON mode model when configured.
  Better abstraction and sectioning, but requires a key and network.

Both engines emit the same note dictionary so storage, search, API and MCP layers
do not care where a note came from.
"""

from __future__ import annotations

import re
from collections import Counter

from .llm import LLMClient
from .transcript import Transcript
from .util import (
    clean_sentence,
    collapse_ws,
    format_timestamp,
    is_meaningful,
    jaccard,
    normalize_for_fingerprint,
    truncate,
)

STOP_ASCII = {
    "the", "and", "for", "that", "with", "this", "you", "are", "was", "were",
    "have", "has", "had", "not", "but", "they", "from", "what", "when", "where",
    "which", "who", "how", "why", "can", "will", "would", "could", "should",
    "about", "into", "your", "our", "their", "there", "here", "then", "than",
    "also", "just", "like", "really", "very", "much", "more", "most", "some",
    "any", "all", "one", "two", "three", "its", "it's", "we're", "i'm", "ok",
    "okay", "yeah", "well", "now", "get", "got", "make", "made", "think", "know",
    "want", "need", "going", "gonna", "let", "say", "said", "see", "look",
}

STOP_TERMS = {
    "我们", "你们", "他们", "她们", "咱们", "大家", "自己", "这个", "那个", "这些",
    "那些", "什么", "怎么", "怎样", "这么", "那么", "为什么", "因为", "所以", "但是",
    "不过", "然后", "而且", "如果", "虽然", "还是", "或者", "以及", "并且", "同时",
    "其实", "当然", "真的", "非常", "特别", "比较", "稍微", "有点", "一下", "一点",
    "一些", "很多", "几个", "每个", "各种", "所有", "其他", "另外", "其中", "现在",
    "今天", "明天", "昨天", "时候", "时间", "地方", "东西", "事情", "问题", "情况",
    "部分", "方面", "过程", "结果", "原因", "意思", "感觉", "觉得", "知道", "看到",
    "发现", "开始", "结束", "进行", "出现", "存在", "包括", "比如", "例如", "就是",
    "可以", "需要", "应该", "可能", "已经", "一定", "一般", "主要", "重要", "简单",
    "复杂", "清楚", "明白", "告诉", "来说", "而言", "方面", "内容", "方式", "方法",
    "一样", "这样", "那样", "这里", "那里", "哪里", "之前", "之后", "以后", "以前",
    "具体", "其实", "基本", "整体", "整个", "相对", "绝对", "直接", "真正",
    "第一", "第二", "第三", "首先", "其次", "最后", "总结", "补充", "说明", "重点",
    "核心", "关键", "本质", "背景", "目标", "现状", "方案", "计划", "结果", "风险",
    "下一步", "接下来", "待办", "todo", "action", "item", "follow", "up", "by",
    "thanks", "thank", "hello", "everyone", "today", "going", "kind", "sort",
    "thing", "things", "stuff", "lot", "little", "maybe", "probably", "actually",
    "basically", "literally", "right", "sure", "yes", "no", "okay",
    "大家好", "同一", "负责", "尽量", "最好", "分别", "一份", "这事", "那事",
}

STOP_CHARS = set("的了是在我你他她它吗呢吧啊呀哦嗯就都也还又很太更最和与及或而但这那咱没么什怎样个不越")
_HEADING_BAD_PREFIX = ("越", "这", "那", "就", "很", "更", "又", "还", "也", "但", "而", "且", "或")
_TERM_BAD_START = set("一二三四五六七八九十们这那很更又还也就但而且或把被让给从向地得着过了不没每各其所此该本我你他她它咱您做化再")
_TAG_TIME_RE = re.compile(r"(周[一二三四五六日天]|星期|下周|本周|这周|月底|月\d|\d月|今天|明天|后天|今年|明年|昨天)")
_TERM_CONTAINS_BAD = (
    "负责", "决定", "建议", "需要", "我们", "你们", "他们", "这个", "那个",
    "就是", "然后", "所以", "但是", "如果", "因为", "以及",
)
_TERM_BAD_END = set("的了着过们么呢吧啊呀哦嗯吗")

DECISION_CUES = (
    "决定", "确定", "定了", "拍板", "结论", "统一", "采用", "选择", "放弃", "建议",
    "敲定", "最终", "定下来", "就这么", "decided", "decision", "go with",
    "final", "we'll use", "we will use",
)
TASK_STRONG_CUES = (
    "下一步", "接下来", "待办", "负责", "跟进", "安排", "记得", "落地", "推进",
    "排期", "截止", "交付", "上线", "负责人",
    "todo", "action item", "follow up", "deadline", "assign",
)
TASK_WEAK_CUES = ("需要", "完成", "准备", "整理", "写完", "产出")
TASK_CUES = TASK_STRONG_CUES + TASK_WEAK_CUES
QUESTION_CUES = ("是否", "要不要", "怎么", "如何", "为什么", "能不能", "有没有", "多少", "哪些")
BELIEF_CUES = ("我觉得", "我认为", "我的判断", "说白了", "本质上", "我特别相信", "最关键的", "我相信")
KEY_CUES = (
    ("最重要的是", 1.6), ("核心", 1.2), ("关键", 1.2), ("重点", 1.0),
    ("本质", 1.0), ("结论", 1.0), ("意味着", 0.8), ("总结", 0.8),
    ("记住", 0.6), ("一句话", 0.8), ("因此", 0.6), ("所以", 0.5),
    ("第一", 0.4), ("首先", 0.4), ("最后", 0.3), ("其实", 0.3),
)
GREETING_RE = re.compile(r"^(大家好|各位好|欢迎|谢谢|辛苦|Hello|Hi|OK|好的)[，,。！!]?")
_OWNER_RE = re.compile(
    r"(?:由|让|请|交给|找)\s*([\u3400-\u9fffA-Za-z]{1,6}?)(?:来|去|负责|跟进|牵头|推进|完成|处理)"
)
_OWNER_RE2 = re.compile(r"([\u3400-\u9fff]{2,4})(?:负责|牵头|跟进)")
_DUE_RE = re.compile(
    r"(今天|明天|后天|大后天|本周|这周|下周|下下周|月底|本月底|下月底|"
    r"周[一二三四五六日天]|下周[一二三四五六日天]|\d{1,2}月\d{1,2}[日号]|"
    r"\d{1,2}/\d{1,2}|Q[1-4]|本周内|日内)"
)
_PRONOUN_OWNERS = {"我们", "大家", "你", "我", "他", "她", "团队", "各位", "所有人", "每个", "分别"}
_QUOTED_RE = re.compile(r"[“「『\"]([^”」』\"\n]{4,120})[”」』\"]")

LLM_SYSTEM = """你是一名资深中文内容编辑，擅长把口播/访谈/课程转录稿整理成结构化笔记。
你必须只输出一个合法 JSON 对象，不要输出 markdown 代码块或任何解释。
要求：
1. 忠实于原文，不编造事实、数字、人名；原文没有的信息用 null 或空数组。
2. title 不超过 40 字；summary 2-3 句，概括核心结论。
3. key_points 4-7 条，每条一句完整的话；sections 3-6 组，heading 简短，points 为要点句。
4. decisions 只收明确的决定/结论；tasks 收可执行的行动项，若能识别负责人和截止时间则填入 owner/due，否则 null。
5. questions 收开放问题；quotes 收值得保留的原话（保持原意）；tags 5-8 个中文关键词。
6. start_seconds/end_seconds 只有在原文有时间戳时填写，否则为 null。
JSON 结构：
{"title": "", "summary": "", "key_points": [""],
 "sections": [{"heading": "", "start_seconds": null, "end_seconds": null, "points": [""]}],
 "decisions": [""], "tasks": [{"text": "", "owner": null, "due": null}],
 "questions": [""], "quotes": [""], "tags": [""]}"""


# --------------------------------------------------------------------------- #
# Term extraction
# --------------------------------------------------------------------------- #

def _find_all(text: str, term: str, *, casefold: bool) -> list[int]:
    positions: list[int] = []
    start = 0
    haystack = text.casefold() if casefold else text
    needle = term.casefold() if casefold else term
    while True:
        index = haystack.find(needle, start)
        if index < 0:
            return positions
        positions.append(index)
        start = index + 1


def extract_terms(text: str, limit: int = 8, min_count: int | None = None) -> list[str]:
    """Deterministic keyword extraction for Chinese + English transcripts."""
    text = text or ""
    if not text.strip():
        return []
    if min_count is None:
        min_count = 1 if len(text) < 300 else 2

    counts: Counter[str] = Counter()
    kinds: dict[str, str] = {}
    forms: dict[str, Counter[str]] = {}
    for match in re.finditer(r"[A-Za-z][A-Za-z0-9+#._-]{1,}", text):
        original = match.group()
        term = original.lower().strip("._-")
        if len(term) < 2 or term in STOP_ASCII:
            continue
        counts[term] += 1
        kinds[term] = "ascii"
        forms.setdefault(term, Counter())[original] += 1
    for match in re.finditer(r"[\u3400-\u9fff]{2,}", text):
        run = match.group()
        for size in (2, 3, 4):
            for index in range(len(run) - size + 1):
                term = run[index : index + size]
                if term in STOP_TERMS or any(ch in STOP_CHARS for ch in term):
                    continue
                if term[0] in _TERM_BAD_START or term[-1] in _TERM_BAD_END:
                    continue
                if len(term) >= 3 and _TAG_TIME_RE.search(term):
                    continue
                if any(bad in term for bad in _TERM_CONTAINS_BAD):
                    continue
                counts[term] += 1
                kinds[term] = "cjk"

    scored: list[tuple[float, str]] = []
    for term, count in counts.items():
        if count < min_count:
            continue
        if kinds[term] == "cjk" and len(term) == 4 and count < 2:
            continue
        if kinds[term] == "ascii":
            score = count * (0.7 + 0.12 * min(len(term), 10))
        else:
            score = count * (0.55 + 0.35 * (len(term) - 1))
        scored.append((score, term))
    scored.sort(key=lambda item: (-item[0], -len(item[1]), item[1]))

    selected: list[str] = []
    covered: set[int] = set()
    for _, term in scored[:100]:
        positions = _find_all(text, term, casefold=kinds[term] == "ascii")
        fresh = []
        for pos in positions:
            if not any(index in covered for index in range(pos, pos + len(term))):
                fresh.append(pos)
        if not fresh:
            continue
        display = forms[term].most_common(1)[0][0] if kinds[term] == "ascii" else term
        selected.append(display)
        for pos in fresh:
            covered.update(range(pos, pos + len(term)))
        if len(selected) >= limit:
            break
    return selected


def _cohesion_terms(text: str) -> set[str]:
    terms: set[str] = set()
    for word in re.findall(r"[A-Za-z]{2,}", text.lower()):
        if word not in STOP_ASCII:
            terms.add(word)
    for run in re.findall(r"[\u3400-\u9fff]{2,}", text):
        for index in range(len(run) - 1):
            bigram = run[index : index + 2]
            if bigram in STOP_TERMS or any(ch in STOP_CHARS for ch in bigram):
                continue
            terms.add(bigram)
    return terms


def _sim_terms(text: str) -> set[str]:
    normalized = normalize_for_fingerprint(text)
    if len(normalized) < 2:
        return {normalized} if normalized else set()
    return {normalized[i : i + 2] for i in range(len(normalized) - 1)}


def _dedupe_texts(texts: list[str], threshold: float = 0.8) -> list[str]:
    kept: list[str] = []
    kept_terms: list[set[str]] = []
    for text in texts:
        terms = _sim_terms(text)
        if any(jaccard(terms, existing) >= threshold for existing in kept_terms):
            continue
        kept.append(text)
        kept_terms.append(terms)
    return kept


# --------------------------------------------------------------------------- #
# Heuristic extraction
# --------------------------------------------------------------------------- #

def _score_rows(rows: list[dict], terms: list[str]) -> list[tuple[float, int]]:
    total = max(1, len(rows))
    scored: list[tuple[float, int]] = []
    for row in rows:
        text = row["text"]
        score = 0.0
        for cue, weight in KEY_CUES:
            if cue in text:
                score += weight
        lowered = text.casefold()
        hits = sum(1 for term in terms if term and term.casefold() in lowered)
        score += min(3.0, hits * 0.45)
        score += (1.0 - row["idx"] / total) * 0.6
        if 12 <= len(text) <= 90:
            score += 0.4
        elif len(text) > 170:
            score -= 0.8
        if text.endswith(("？", "?")):
            score -= 0.25
        if GREETING_RE.match(text):
            score -= 2.0
        scored.append((score, row["idx"]))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return scored


def _is_greeting(text: str) -> bool:
    return bool(GREETING_RE.match(text)) and len(text) < 40


def _pick_title(rows: list[dict], tags: list[str], filename: str, hint: str | None) -> str:
    if hint:
        return truncate(collapse_ws(hint), 44)
    best: tuple[float, int, str] | None = None
    for row in rows[:25]:
        text = row["text"]
        if not (6 <= len(text) <= 60) or _is_greeting(text) or text.endswith(("？", "?")):
            continue
        score = sum(0.4 for tag in tags if tag and tag.casefold() in text.casefold())
        score += 1.6 * (1.0 - row["idx"] / max(25, len(rows)))
        if re.search(r"[：:]", text):
            score += 0.2
        if text.startswith(("那", "所以", "但是", "然后", "另外", "最后", "第", "我这边")):
            score -= 1.2
        if any(cue in text for cue in TASK_CUES):
            score -= 0.6
        if best is None or score > best[0]:
            best = (score, row["idx"], text)
    if best:
        return truncate(re.sub(r"[。！!；;，,]+$", "", best[2]), 44)
    if tags:
        return truncate("、".join(tags[:2]) + " · 口播笔记", 44)
    stem = filename.rsplit("/", 1)[-1].rsplit(".", 1)[0] if filename else ""
    return truncate((stem + " · 口播笔记") if stem else "口播笔记", 44)


def _pick_summary(rows: list[dict], terms: list[str]) -> str:
    scored = _score_rows(rows, terms)
    chosen: list[dict] = []
    chosen_terms: list[set[str]] = []
    for _, idx in scored:
        row = rows[idx]
        text = row["text"]
        if len(text) < 10 or _is_greeting(text):
            continue
        sim_terms = _sim_terms(text)
        if any(jaccard(sim_terms, existing) >= 0.55 for existing in chosen_terms):
            continue
        chosen.append(row)
        chosen_terms.append(sim_terms)
        if len(chosen) >= 3:
            break
    if not chosen:
        chosen = rows[:2]
    chosen.sort(key=lambda row: row["idx"])
    joined = "".join(row["text"] for row in chosen)
    if not re.search(r"[。！？!?；;]$", joined):
        joined += "。"
    return truncate(joined, 240)


def _extract_decisions(rows: list[dict]) -> list[str]:
    found: list[str] = []
    for row in rows:
        text = row["text"]
        if len(text) < 6 or len(text) > 180:
            continue
        lowered = text.lower()
        if any(cue in lowered for cue in DECISION_CUES):
            if text.endswith(("？", "?")):
                continue
            core = lowered
            for cue in DECISION_CUES:
                core = core.replace(cue, "")
            if len(re.sub(r"[^\w\u3400-\u9fff]", "", core)) < 6:
                continue
            found.append(text)
    return _dedupe_texts(found)[:5]


def _extract_tasks(rows: list[dict]) -> list[dict]:
    tasks: list[dict] = []
    seen: list[set[str]] = []
    for row in rows:
        text = row["text"]
        lowered = text.lower()
        if len(text) < 6 or len(text) > 180:
            continue
        if text.endswith(("？", "?")) and not any(
            cue in lowered for cue in ("负责", "截止", "之前", "排期", "上线")
        ):
            continue
        if re.search(r"(待办|action item|todo)", lowered) and len(text) < 8:
            continue
        owner = None
        for regex in (_OWNER_RE, _OWNER_RE2):
            match = regex.search(text)
            if match:
                candidate = match.group(1)
                if candidate not in _PRONOUN_OWNERS:
                    owner = candidate
                    break
        due_match = _DUE_RE.search(text)
        has_strong = any(cue in lowered for cue in TASK_STRONG_CUES)
        has_weak = any(cue in lowered for cue in TASK_WEAK_CUES)
        if not has_strong and not (has_weak and (owner or due_match)):
            continue
        text_terms = _sim_terms(text)
        if any(jaccard(text_terms, existing) >= 0.8 for existing in seen):
            continue
        seen.append(text_terms)
        tasks.append({"text": text, "owner": owner, "due": due_match.group(1) if due_match else None})
        if len(tasks) >= 6:
            break
    return tasks


def _extract_questions(rows: list[dict]) -> list[str]:
    found: list[str] = []
    for row in rows:
        text = row["text"]
        if not (6 <= len(text) <= 140):
            continue
        lowered = text.lower()
        is_question = text.endswith(("？", "?")) or any(
            cue in lowered for cue in ("是否", "要不要", "能不能", "有没有", "为什么", "如何", "可不可以")
        )
        if is_question:
            if any(cue in lowered for cue in DECISION_CUES + TASK_CUES) and not text.endswith(("？", "?")):
                continue
            found.append(text)
    return _dedupe_texts(found)[:5]


def _extract_quotes(raw: str, rows: list[dict], blocked: set[str]) -> list[str]:
    quotes: list[str] = []
    for match in _QUOTED_RE.finditer(raw):
        quote = clean_sentence(match.group(1))
        if len(quote) >= 4 and is_meaningful(quote):
            quotes.append(quote)
    if not quotes:
        for row in rows:
            text = row["text"]
            if 8 <= len(text) <= 90 and any(cue in text for cue in BELIEF_CUES):
                if normalize_for_fingerprint(text) in blocked:
                    continue
                quotes.append(text)
    return _dedupe_texts(quotes)[:4]


def _pick_key_points(
    rows: list[dict],
    terms: list[str],
    blocked: set[str],
    limit: int = 6,
) -> list[str]:
    scored = _score_rows(rows, terms)
    chosen: list[dict] = []
    chosen_terms: list[set[str]] = []
    for _, idx in scored:
        row = rows[idx]
        text = row["text"]
        if not (8 <= len(text) <= 130) or _is_greeting(text):
            continue
        if normalize_for_fingerprint(text) in blocked:
            continue
        text_terms = _sim_terms(text)
        if any(jaccard(text_terms, existing) >= 0.75 for existing in chosen_terms):
            continue
        chosen.append(row)
        chosen_terms.append(text_terms)
        if len(chosen) >= limit:
            break
    chosen.sort(key=lambda row: row["idx"])
    return [row["text"] for row in chosen]


def _build_sections(rows: list[dict]) -> list[dict]:
    n = len(rows)
    if n == 0:
        return []
    if n <= 4:
        blocks = [(0, n)]
    else:
        target = min(5, max(2, round(n / 5)))
        strengths: list[tuple[float, int]] = []
        for index in range(2, n - 1):
            before: set[str] = set()
            after: set[str] = set()
            for j in range(max(0, index - 3), index):
                before |= _cohesion_terms(rows[j]["text"])
            for j in range(index, min(n, index + 3)):
                after |= _cohesion_terms(rows[j]["text"])
            strength = 1.0 - jaccard(before, after)
            gap_bonus = 0.0
            current, previous = rows[index]["start"], rows[index - 1]["start"]
            if current is not None and previous is not None and current - previous >= 90:
                gap_bonus = 0.6
            if gap_bonus:
                strength += gap_bonus
            strengths.append((strength, index))
        accepted: list[int] = []
        for _, index in sorted(strengths, reverse=True):
            if len(accepted) >= target - 1:
                break
            if index < 2 or n - index < 2:
                continue
            if any(abs(index - existing) < 2 for existing in accepted):
                continue
            current, previous = rows[index]["start"], rows[index - 1]["start"]
            if current is not None and current == previous:
                continue
            accepted.append(index)
        accepted.sort()
        boundaries = [0] + accepted + [n]
        blocks = [(boundaries[i], boundaries[i + 1]) for i in range(len(boundaries) - 1)]

    scored_map = {idx: score for score, idx in _score_rows(rows, [])}
    sections: list[dict] = []
    previous_heading = ""
    for block_index, (start_idx, end_idx) in enumerate(blocks):
        block_rows = rows[start_idx:end_idx]
        block_text = "".join(row["text"] for row in block_rows)
        block_terms = [
            term
            for term in extract_terms(block_text, limit=6, min_count=2)
            if len(term) >= 3 and not term.startswith(_HEADING_BAD_PREFIX)
        ]
        heading = "、".join(block_terms[:2]) if block_terms else truncate(block_rows[0]["text"], 20)
        if heading == previous_heading:
            heading = heading + "（续）"
        previous_heading = heading

        ranked = sorted(
            block_rows,
            key=lambda row: (-scored_map.get(row["idx"], 0.0), row["idx"]),
        )
        points = [row["text"] for row in ranked[:3]]
        points.sort(key=lambda text: next(row["idx"] for row in block_rows if row["text"] == text))

        starts = [row["start"] for row in block_rows if row["start"] is not None]
        start_seconds = starts[0] if starts else None
        end_seconds: int | None = None
        if block_index + 1 < len(blocks):
            next_start = next(
                (row["start"] for row in rows[blocks[block_index + 1][0] :] if row["start"] is not None),
                None,
            )
            if start_seconds is not None and next_start is not None and next_start > start_seconds:
                end_seconds = next_start
        if end_seconds is None and start_seconds is not None:
            end_seconds = start_seconds + 60

        sections.append(
            {
                "heading": heading.strip() or f"第 {block_index + 1} 部分",
                "start_seconds": start_seconds,
                "end_seconds": end_seconds,
                "points": points,
            }
        )
    return sections


def build_heuristic_note(transcript: Transcript, hint: str | None = None) -> dict:
    rows: list[dict] = []
    for segment in transcript.segments:
        text = clean_sentence(segment.text)
        if not is_meaningful(text):
            continue
        rows.append(
            {
                "idx": len(rows),
                "text": text,
                "speaker": segment.speaker,
                "start": segment.start_seconds,
            }
        )
    if not rows:
        raise ValueError("transcript has no meaningful sentences")

    full_text = "".join(row["text"] for row in rows)
    tags = extract_terms(full_text, limit=8, min_count=1)

    decisions = _extract_decisions(rows)
    tasks = _extract_tasks(rows)
    questions = _extract_questions(rows)
    blocked = {
        normalize_for_fingerprint(text)
        for text in decisions + questions + [task["text"] for task in tasks]
    }
    quotes = _extract_quotes(transcript.raw, rows, blocked)
    key_points = _pick_key_points(rows, tags, blocked)
    key_points = [point for point in key_points if not any(quote in point for quote in quotes)]
    if not key_points:
        key_points = [row["text"] for row in rows[:3]]

    return {
        "title": _pick_title(rows, tags, transcript.filename, hint),
        "summary": _pick_summary(rows, tags),
        "key_points": key_points,
        "sections": _build_sections(rows),
        "decisions": decisions,
        "tasks": tasks,
        "questions": questions,
        "quotes": quotes,
        "tags": tags,
        "engine": "heuristic",
        "engine_detail": "cue-rules+n-grams",
    }


# --------------------------------------------------------------------------- #
# LLM extraction
# --------------------------------------------------------------------------- #

def _str_list(value, limit: int = 12) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, dict):
            item = item.get("text") or item.get("point") or item.get("content") or ""
        if isinstance(item, str):
            text = clean_sentence(item)
            if is_meaningful(text):
                out.append(truncate(text, 240))
        if len(out) >= limit:
            break
    return out


def _task_list(value) -> list[dict]:
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    tasks: list[dict] = []
    for item in value[:10]:
        if isinstance(item, str):
            text, owner, due = item, None, None
        elif isinstance(item, dict):
            text = item.get("text") or item.get("task") or ""
            owner = item.get("owner") or None
            due = item.get("due") or None
        else:
            continue
        text = clean_sentence(str(text))
        if not is_meaningful(text):
            continue
        tasks.append(
            {
                "text": truncate(text, 240),
                "owner": truncate(str(owner), 20) if owner else None,
                "due": truncate(str(due), 30) if due else None,
            }
        )
    return tasks


def _section_list(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    sections: list[dict] = []
    for item in value[:8]:
        if not isinstance(item, dict):
            continue
        heading = clean_sentence(str(item.get("heading") or item.get("title") or ""))
        points = _str_list(item.get("points") or item.get("content") or [], limit=8)
        if not heading and not points:
            continue
        start = item.get("start_seconds")
        end = item.get("end_seconds")
        sections.append(
            {
                "heading": truncate(heading or (points[0] if points else "未命名章节"), 40),
                "start_seconds": int(start) if isinstance(start, (int, float)) and start >= 0 else None,
                "end_seconds": int(end) if isinstance(end, (int, float)) and end >= 0 else None,
                "points": points or [heading],
            }
        )
    return sections


def _transcript_for_prompt(transcript: Transcript, limit: int = 24000) -> str:
    lines: list[str] = []
    for segment in transcript.segments:
        prefix = f"[{format_timestamp(segment.start_seconds)}] " if segment.start_seconds is not None else ""
        speaker = f"{segment.speaker}：" if segment.speaker else ""
        lines.append(f"{prefix}{speaker}{segment.text}")
    body = "\n".join(lines)
    if len(body) <= limit:
        return body
    head = int(limit * 0.8)
    tail = limit - head
    return body[:head] + "\n…（中间省略）…\n" + body[-tail:]


def _coerce_llm_note(data: dict, transcript: Transcript, hint: str | None) -> dict:
    rows = [
        {"idx": index, "text": clean_sentence(segment.text), "speaker": segment.speaker, "start": segment.start_seconds}
        for index, segment in enumerate(transcript.segments)
        if is_meaningful(clean_sentence(segment.text))
    ]
    title = clean_sentence(str(data.get("title") or ""))
    summary = clean_sentence(str(data.get("summary") or ""))
    key_points = _str_list(data.get("key_points"))
    sections = _section_list(data.get("sections"))
    decisions = _str_list(data.get("decisions"), limit=8)
    tasks = _task_list(data.get("tasks"))
    questions = _str_list(data.get("questions"), limit=8)
    quotes = _str_list(data.get("quotes"), limit=6)
    tags = _str_list(data.get("tags"), limit=10)

    if not title:
        title = _pick_title(rows, tags, transcript.filename, hint)
    if not summary:
        summary = _pick_summary(rows, tags)
    if not key_points:
        key_points = [point for section in sections for point in section["points"]][:6]
    if not key_points:
        key_points = [row["text"] for row in rows[:3]]
    if not sections:
        sections = [{"heading": tags[0] if tags else "要点", "start_seconds": None, "end_seconds": None, "points": key_points}]
    if len(tags) < 3:
        tags = extract_terms("".join(row["text"] for row in rows), limit=8, min_count=1)

    if not (title or summary or key_points or sections):
        raise ValueError("LLM note was empty")
    return {
        "title": truncate(title, 44),
        "summary": truncate(summary, 300),
        "key_points": key_points,
        "sections": sections,
        "decisions": decisions,
        "tasks": tasks,
        "questions": questions,
        "quotes": quotes,
        "tags": tags,
        "engine": "llm",
        "engine_detail": None,
    }


def build_llm_note(transcript: Transcript, client: LLMClient, hint: str | None = None) -> dict:
    prompt = (
        "请把下面的口播转录稿整理成结构化笔记 JSON。\n\n"
        f"文件名：{transcript.filename or '未命名'}\n"
        f"字数：{transcript.chars}\n"
        f"时间戳：{'有' if transcript.has_timestamps else '无'}\n"
        f"说话人：{'、'.join(transcript.speakers) if transcript.speakers else '未知'}\n\n"
        "----- 转录稿开始 -----\n"
        f"{_transcript_for_prompt(transcript)}\n"
        "----- 转录稿结束 -----"
    )
    data = client.chat_json(LLM_SYSTEM, prompt)
    note = _coerce_llm_note(data, transcript, hint)
    note["engine_detail"] = client.model
    return note


def structure_transcript(
    transcript: Transcript,
    *,
    client: LLMClient | None = None,
    use_llm: bool = True,
    hint: str | None = None,
) -> dict:
    """Produce a note dict, degrading safely to the heuristic engine."""
    warnings: list[str] = []
    if client is not None and use_llm:
        try:
            note = build_llm_note(transcript, client, hint)
            note["warnings"] = warnings
            return note
        except Exception as exc:  # noqa: BLE001 - fallback is intentional
            warnings.append(f"LLM structuring failed ({type(exc).__name__}: {exc}); used heuristic fallback.")
    note = build_heuristic_note(transcript, hint)
    note["warnings"] = warnings
    return note


# --------------------------------------------------------------------------- #
# Markdown rendering
# --------------------------------------------------------------------------- #

def _section_time(section: dict) -> str:
    start = section.get("start_seconds")
    end = section.get("end_seconds")
    if start is None:
        return ""
    if end is None or end <= start:
        return format_timestamp(start)
    return f"{format_timestamp(start)}–{format_timestamp(end)}"


def render_markdown(note: dict) -> str:
    """Render a stored note dict as a human-readable Markdown document."""
    lines: list[str] = [f"# {note.get('title') or '未命名笔记'}", ""]
    source = note.get("source") or {}
    meta_bits = []
    if source.get("filename"):
        meta_bits.append(f"来源：{source['filename']}")
    if note.get("agent_id"):
        meta_bits.append(f"agent：`{note['agent_id']}`")
    if note.get("created_at"):
        meta_bits.append(note["created_at"][:10])
    if source.get("chars"):
        meta_bits.append(f"{source['chars']} 字")
    if note.get("engine"):
        meta_bits.append(f"引擎：{note['engine']}")
    if meta_bits:
        lines.extend(["> " + " · ".join(meta_bits), ""])

    if note.get("summary"):
        lines.extend(["## 摘要", "", note["summary"], ""])

    if note.get("key_points"):
        lines.extend(["## 核心要点", ""])
        lines.extend(f"- {point}" for point in note["key_points"])
        lines.append("")

    if note.get("sections"):
        lines.extend(["## 章节", ""])
        for section in note["sections"]:
            stamp = _section_time(section)
            heading = section.get("heading") or "未命名章节"
            lines.append(f"### {stamp + ' · ' if stamp else ''}{heading}")
            lines.append("")
            points = section.get("points") or []
            lines.extend(f"- {point}" for point in points)
            if not points:
                lines.append("- （无要点）")
            lines.append("")

    for key, label, icon in (
        ("decisions", "决策", ""),
        ("tasks", "行动项", ""),
        ("questions", "待解答问题", ""),
        ("quotes", "原话摘录", ""),
    ):
        values = note.get(key) or []
        if not values:
            continue
        lines.extend([f"## {label}", ""])
        if key == "tasks":
            for task in values:
                text = task.get("text", "") if isinstance(task, dict) else str(task)
                extras = []
                if isinstance(task, dict):
                    if task.get("owner"):
                        extras.append(f"负责人：{task['owner']}")
                    if task.get("due"):
                        extras.append(f"截止：{task['due']}")
                suffix = f"（{' · '.join(extras)}）" if extras else ""
                lines.append(f"- [ ] {text}{suffix}")
        else:
            lines.extend(f"- {value}" for value in values)
        lines.append("")

    if note.get("tags"):
        lines.extend(["## 标签", "", " ".join(f"`#{tag}`" for tag in note["tags"]), ""])

    if note.get("warnings"):
        lines.extend(["---", ""])
        lines.extend(f"> ⚠️ {warning}" for warning in note["warnings"])
        lines.append("")

    stats = note.get("source_stats") or {}
    if stats.get("speakers"):
        lines.append(f"<!-- speakers: {', '.join(stats['speakers'])} -->")
    return "\n".join(lines).rstrip() + "\n"
