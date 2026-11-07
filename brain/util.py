"""Small shared helpers: text cleanup, time formatting, hashing, token estimates."""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import time
from datetime import datetime, timezone

CJK_RANGE = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
_CJK_RE = re.compile(f"[{CJK_RANGE}]")
_WS_RE = re.compile(r"[\s\u3000]+")
_FILLER_PREFIX_RE = re.compile(
    r"^(?:嗯+|啊+|呃+|额+|哦+|噢+|那个|然后呢|然后|就是|其实|这个|所以说|那么|好的|好|对|是的)[，,、\s]+"
)
_EMPTY_CHARS_RE = re.compile(r"[\s\u3000，,。.！!？?、；;：:\-—~～]+")


def is_cjk(ch: str) -> bool:
    return bool(_CJK_RE.match(ch))


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def stable_id(prefix: str) -> str:
    """Short, human-scannable, collision-resistant id."""
    return f"{prefix}_{int(time.time() * 1000):x}{secrets.token_hex(4)}"


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u3000", " ")
    text = text.replace("\ufeff", "")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def collapse_ws(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def clean_sentence(text: str) -> str:
    """Trim whitespace and spoken filler at the start of a sentence."""
    text = collapse_ws(text)
    for _ in range(4):
        new = _FILLER_PREFIX_RE.sub("", text, count=1)
        if new == text or not new:
            break
        text = new.strip()
    return text.strip()


def is_meaningful(text: str) -> bool:
    return len(_EMPTY_CHARS_RE.sub("", text)) >= 4


def split_sentences(text: str, max_chars: int = 110) -> list[str]:
    """Split Chinese/English transcript text into sentence-like units.

    Spoken Chinese often has very few full stops, so long comma chains are
    further split into digestible chunks (<= max_chars) at punctuation marks.
    """
    text = normalize_text(text)
    if not text:
        return []
    pieces: list[str] = []
    for part in re.split(r"(?<=[。！？!?；;])|\n+", text):
        part = part.strip()
        if not part:
            continue
        for sub in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\u4e00-\u9fff])", part):
            sub = sub.strip()
            if sub:
                pieces.append(sub)

    result: list[str] = []
    for piece in pieces:
        if len(piece) <= max_chars or not re.search(r"[，,、]", piece):
            result.append(piece)
            continue
        buf = ""
        for chunk in re.split(r"(?<=[，,、])", piece):
            if buf and len(buf) + len(chunk) > max_chars:
                result.append(buf.strip())
                buf = chunk
            else:
                buf += chunk
        if buf.strip():
            result.append(buf.strip())

    merged: list[str] = []
    for sentence in result:
        # A full stop inside “...” can split the closing quote onto the next
        # fragment; glue it back so quotes stay intact.
        if merged and sentence[:1] in ("”", "」", "』"):
            merged[-1] += sentence
        else:
            merged.append(sentence)
    return [sentence for sentence in merged if is_meaningful(sentence)]


def parse_hms(raw: str) -> int:
    """Parse 'mm:ss', 'hh:mm:ss', '12:34' into seconds."""
    parts = [int(p) for p in raw.split(":")]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    raise ValueError(f"unsupported timestamp: {raw}")


def format_timestamp(seconds: int | float | None) -> str:
    if seconds is None:
        return "—"
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def estimate_tokens(text: str) -> int:
    """Conservative token estimate: 1 per CJK char, ~4 per ASCII char."""
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return max(1, cjk + math.ceil(other / 4))


def normalize_for_fingerprint(text: str) -> str:
    text = text.lower()
    text = _WS_RE.sub("", text)
    return re.sub(r"[^\w\u3400-\u9fff]", "", text, flags=re.UNICODE)


def fingerprint(kind: str, text: str) -> str:
    raw = f"{kind}\x1f{normalize_for_fingerprint(text)}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def content_sha256(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: max(1, limit - len(suffix))].rstrip() + suffix


def read_text_file(path: str) -> str:
    with open(path, "rb") as fh:
        data = fh.read()
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16")
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def dumps(value, *, pretty: bool = False) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2 if pretty else None, sort_keys=pretty)
