"""SQLite-backed shared memory: structured notes, atomic items, provenance, search."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from .extract import render_markdown, structure_transcript
from .llm import LLMClient
from .transcript import parse_transcript
from .util import (
    clean_sentence,
    collapse_ws,
    content_sha256,
    fingerprint,
    is_meaningful,
    jaccard,
    now_iso,
    stable_id,
)

SCHEMA_VERSION = "1"
ITEM_TYPES = {"summary", "point", "decision", "task", "question", "quote", "fact", "note"}
ITEM_STATUSES = {"active", "open", "done", "archived"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'agent',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'transcript',
    filename TEXT,
    content_sha256 TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_sources_agent_hash ON sources(agent_id, content_sha256);

CREATE TABLE IF NOT EXISTS notes (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    body_markdown TEXT NOT NULL,
    note_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_notes_agent ON notes(agent_id);
CREATE INDEX IF NOT EXISTS ix_notes_created ON notes(created_at);

CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    note_id TEXT,
    source_id TEXT,
    agent_id TEXT NOT NULL,
    type TEXT NOT NULL,
    text TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active',
    confidence REAL NOT NULL DEFAULT 0.6,
    meta TEXT NOT NULL DEFAULT '{}',
    fingerprint TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_items_type ON items(type);
CREATE INDEX IF NOT EXISTS ix_items_note ON items(note_id);
CREATE INDEX IF NOT EXISTS ix_items_updated ON items(updated_at);

CREATE TABLE IF NOT EXISTS contributions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    note_id TEXT,
    source_id TEXT,
    role TEXT NOT NULL DEFAULT 'author',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_contrib_item ON contributions(item_id);
CREATE INDEX IF NOT EXISTS ix_contrib_agent ON contributions(agent_id);

CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    item_id UNINDEXED,
    tokens,
    tags,
    type UNINDEXED,
    agent_id UNINDEXED
);

CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    note_id UNINDEXED,
    tokens
);
"""


def fts_tokens(text: str) -> list[str]:
    """Character-level tokens for CJK + word tokens for ASCII (unicode61 friendly)."""
    tokens: list[str] = []
    for match in re.finditer(r"[A-Za-z0-9_]+|[\u3400-\u9fff]+", text or ""):
        chunk = match.group()
        if chunk[0].isascii():
            tokens.append(chunk.lower())
        else:
            tokens.extend(chunk)
    return tokens


def _indexed(text: str) -> str:
    return " ".join(fts_tokens(text))


def _loads(raw: str, fallback):
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _relevance_ok(query: str, text: str) -> bool:
    """Reject results that only share stray characters with the query."""
    query = query.strip().casefold()
    text = (text or "").casefold()
    if not query:
        return False
    if query in text:
        return True
    ascii_tokens = re.findall(r"[a-z0-9_]{2,}", query)
    if any(token in text for token in ascii_tokens):
        return True
    cjk = re.sub(r"[^\u3400-\u9fff]", "", query)
    if len(cjk) == 1:
        return cjk in text
    if len(cjk) >= 2:
        return any(cjk[index : index + 2] in text for index in range(len(cjk) - 1))
    return False


def _dedupe_terms(tags: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for tag in tags:
        normalized = collapse_ws(str(tag)).strip("#")
        if normalized and normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out


class BrainStore:
    """A single shared memory store that many agents read and write."""

    def __init__(self, db_path: str | Path, *, llm_client: LLMClient | None = None) -> None:
        self.db_path = str(db_path)
        self.llm_client = llm_client
        if self.db_path != ":memory:":
            Path(self.db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self.init()

    # ------------------------------------------------------------------ #
    # Plumbing
    # ------------------------------------------------------------------ #
    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def init(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                (SCHEMA_VERSION,),
            )
            conn.commit()

    def _touch_agent(self, conn: sqlite3.Connection, agent_id: str, kind: str = "agent") -> None:
        now = now_iso()
        conn.execute(
            """INSERT INTO agents(id, kind, first_seen, last_seen) VALUES(?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen""",
            (agent_id, kind, now, now),
        )

    # ------------------------------------------------------------------ #
    # Ingestion
    # ------------------------------------------------------------------ #
    def ingest_text(
        self,
        text: str,
        *,
        agent_id: str,
        filename: str = "",
        title_hint: str | None = None,
        use_llm: bool = True,
    ) -> dict:
        transcript = parse_transcript(text, filename=filename)
        digest = content_sha256(text)

        with closing(self._connect()) as conn:
            existing = conn.execute(
                "SELECT id FROM sources WHERE agent_id=? AND content_sha256=?",
                (agent_id, digest),
            ).fetchone()
            if existing:
                note_row = conn.execute(
                    "SELECT id FROM notes WHERE source_id=?", (existing["id"],)
                ).fetchone()
                if note_row:
                    return {
                        "note": self.get_note(note_row["id"]),
                        "deduplicated": True,
                        "items_created": 0,
                        "items_linked": 0,
                    }

            note = structure_transcript(
                transcript,
                client=self.llm_client,
                use_llm=use_llm,
                hint=title_hint,
            )
            note_id = stable_id("note")
            source_id = stable_id("src")
            created_at = note["created_at"] = now_iso()
            note["schema"] = "shared-brain/note/v1"
            note["id"] = note_id
            note["agent_id"] = agent_id
            note["source"] = {
                "filename": filename or "粘贴文本",
                "sha256": digest,
                "chars": transcript.chars,
            }
            note["source_stats"] = transcript.stats()
            markdown = render_markdown(note)
            note_json = json.dumps(note, ensure_ascii=False, sort_keys=True)

            created = linked = 0
            try:
                with conn:
                    self._touch_agent(conn, agent_id, "agent")
                    conn.execute(
                        """INSERT INTO sources(id, agent_id, kind, filename, content_sha256, content, created_at)
                           VALUES(?, ?, 'transcript', ?, ?, ?, ?)""",
                        (source_id, agent_id, filename, digest, text, created_at),
                    )
                    conn.execute(
                        """INSERT INTO notes(id, source_id, agent_id, title, summary, body_markdown,
                                             note_json, created_at, updated_at)
                           VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            note_id,
                            source_id,
                            agent_id,
                            note["title"],
                            note["summary"],
                            markdown,
                            note_json,
                            created_at,
                            created_at,
                        ),
                    )
                    conn.execute(
                        "INSERT INTO notes_fts(note_id, tokens) VALUES(?, ?)",
                        (note_id, _indexed(note["title"] + " " + note["summary"] + " " + " ".join(note["tags"]))),
                    )
                    for item in self._note_items(note, transcript):
                        _, was_created = self._upsert_item(
                            conn,
                            text=item["text"],
                            item_type=item["type"],
                            agent_id=agent_id,
                            tags=item.get("tags") or note["tags"][:2],
                            note_id=note_id,
                            source_id=source_id,
                            status=item["status"],
                            confidence=item["confidence"],
                            meta=item["meta"],
                        )
                        created += 1 if was_created else 0
                        linked += 0 if was_created else 1
            except sqlite3.IntegrityError:
                # Lost an ingestion race: return the note another process created.
                row = conn.execute(
                    "SELECT id FROM notes WHERE source_id=?",
                    (source_id,),
                ).fetchone()
                if row is None:
                    row = conn.execute(
                        "SELECT n.id FROM notes n JOIN sources s ON s.id=n.source_id "
                        "WHERE s.agent_id=? AND s.content_sha256=?",
                        (agent_id, digest),
                    ).fetchone()
                if row is None:
                    raise
                return {
                    "note": self.get_note(row["id"]),
                    "deduplicated": True,
                    "items_created": 0,
                    "items_linked": 0,
                }

        return {
            "note": self.get_note(note_id),
            "deduplicated": False,
            "items_created": created,
            "items_linked": linked,
        }

    def _note_items(self, note: dict, transcript=None) -> list[dict]:
        items: list[dict] = []
        if note.get("summary"):
            items.append(
                {"type": "summary", "text": note["summary"], "status": "active",
                 "confidence": 0.7, "meta": {}}
            )
        for point in note.get("key_points") or []:
            items.append(
                {"type": "point", "text": point, "status": "active",
                 "confidence": 0.65, "meta": {}}
            )
        for section in note.get("sections") or []:
            for point in section.get("points") or []:
                items.append(
                    {"type": "point", "text": point, "status": "active",
                     "confidence": 0.55,
                     "meta": {"section": section.get("heading")}}
                )
        for decision in note.get("decisions") or []:
            items.append(
                {"type": "decision", "text": decision, "status": "active",
                 "confidence": 0.75, "meta": {}}
            )
        for task in note.get("tasks") or []:
            items.append(
                {
                    "type": "task",
                    "text": task.get("text", ""),
                    "status": "open",
                    "confidence": 0.7,
                    "meta": {"owner": task.get("owner"), "due": task.get("due")},
                }
            )
        for question in note.get("questions") or []:
            items.append(
                {"type": "question", "text": question, "status": "active",
                 "confidence": 0.55, "meta": {}}
            )
        for quote in note.get("quotes") or []:
            items.append(
                {"type": "quote", "text": quote, "status": "active",
                 "confidence": 0.6, "meta": {}}
            )
        if transcript is not None:
            # Keep every source sentence searchable at low confidence so facts that
            # did not make the curated highlights are still recallable.
            for segment in transcript.segments:
                items.append(
                    {"type": "point", "text": segment.text, "status": "active",
                     "confidence": 0.4, "meta": {"granularity": "sentence"}}
                )

        # Prefer high-value memory types when two items share the same content:
        # a decision/task should not be swallowed by a generic sentence point.
        priority = {"decision": 0, "task": 1, "question": 2, "quote": 3, "summary": 4, "point": 5}
        items.sort(key=lambda item: priority.get(item["type"], 9))

        deduped: list[dict] = []
        seen: list[set[str]] = []
        for item in items:
            text = clean_sentence(item["text"])
            if not is_meaningful(text):
                continue
            terms = {text[i : i + 2] for i in range(max(1, len(text) - 1))}
            if any(jaccard(terms, previous) >= 0.85 for previous in seen):
                continue
            item["text"] = text
            seen.append(terms)
            deduped.append(item)
        return deduped

    # ------------------------------------------------------------------ #
    # Atomic items
    # ------------------------------------------------------------------ #
    def _upsert_item(
        self,
        conn: sqlite3.Connection,
        *,
        text: str,
        item_type: str,
        agent_id: str,
        tags: list[str] | None = None,
        note_id: str | None = None,
        source_id: str | None = None,
        status: str = "active",
        confidence: float = 0.6,
        meta: dict | None = None,
        role: str = "author",
    ) -> tuple[str, bool]:
        text = clean_sentence(text)
        if not is_meaningful(text):
            raise ValueError("memory item text is too short")
        if item_type not in ITEM_TYPES:
            raise ValueError(f"unsupported item type: {item_type}")
        if status not in ITEM_STATUSES:
            raise ValueError(f"unsupported status: {status}")
        tags = _dedupe_terms(tags or [])
        meta = meta or {}
        now = now_iso()
        digest = fingerprint(item_type, text)
        new_id = stable_id("item")

        conn.execute(
            """INSERT INTO items(id, note_id, source_id, agent_id, type, text, tags, status,
                                 confidence, meta, fingerprint, created_at, updated_at)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(fingerprint) DO UPDATE SET updated_at=excluded.updated_at""",
            (
                new_id,
                note_id,
                source_id,
                agent_id,
                item_type,
                text,
                json.dumps(tags, ensure_ascii=False),
                status,
                confidence,
                json.dumps(meta, ensure_ascii=False, sort_keys=True),
                digest,
                now,
                now,
            ),
        )
        row = conn.execute("SELECT id, tags FROM items WHERE fingerprint=?", (digest,)).fetchone()
        item_id = row["id"]
        created = item_id == new_id
        if created:
            conn.execute(
                "INSERT INTO items_fts(item_id, tokens, tags, type, agent_id) VALUES(?, ?, ?, ?, ?)",
                (item_id, _indexed(text), _indexed(" ".join(tags)), item_type, agent_id),
            )
        else:
            existing_tags = _loads(row["tags"], [])
            merged = _dedupe_terms(list(existing_tags) + tags)
            if merged != existing_tags:
                conn.execute(
                    "UPDATE items SET tags=? WHERE id=?",
                    (json.dumps(merged, ensure_ascii=False), item_id),
                )
                conn.execute(
                    "UPDATE items_fts SET tags=? WHERE item_id=?",
                    (_indexed(" ".join(merged)), item_id),
                )

        self._add_contribution(conn, item_id, agent_id, note_id, source_id, role)
        self._touch_agent(conn, agent_id, "agent")
        return item_id, created

    def _add_contribution(
        self,
        conn: sqlite3.Connection,
        item_id: str,
        agent_id: str,
        note_id: str | None,
        source_id: str | None,
        role: str,
    ) -> bool:
        if note_id is None:
            existing = conn.execute(
                "SELECT 1 FROM contributions WHERE item_id=? AND agent_id=? AND note_id IS NULL",
                (item_id, agent_id),
            ).fetchone()
        else:
            existing = conn.execute(
                "SELECT 1 FROM contributions WHERE item_id=? AND agent_id=? AND note_id=?",
                (item_id, agent_id, note_id),
            ).fetchone()
        if existing:
            return False
        conn.execute(
            """INSERT INTO contributions(item_id, agent_id, note_id, source_id, role, created_at)
               VALUES(?, ?, ?, ?, ?, ?)""",
            (item_id, agent_id, note_id, source_id, role, now_iso()),
        )
        return True

    def add_memory(
        self,
        text: str,
        *,
        agent_id: str,
        item_type: str = "fact",
        tags: list[str] | None = None,
        meta: dict | None = None,
    ) -> dict:
        with closing(self._connect()) as conn:
            with conn:
                item_id, created = self._upsert_item(
                    conn,
                    text=text,
                    item_type=item_type,
                    agent_id=agent_id,
                    tags=tags or [],
                    status="open" if item_type == "task" else "active",
                    confidence=0.8,
                    meta=meta or {},
                    role="manual",
                )
        item = self.get_item(item_id)
        assert item is not None
        item["created"] = created
        return item

    def set_item_status(self, item_id: str, status: str) -> dict | None:
        if status not in ITEM_STATUSES:
            raise ValueError(f"unsupported status: {status}")
        with closing(self._connect()) as conn:
            with conn:
                cursor = conn.execute(
                    "UPDATE items SET status=?, updated_at=? WHERE id=?",
                    (status, now_iso(), item_id),
                )
                if cursor.rowcount == 0:
                    return None
        return self.get_item(item_id)

    def get_item(self, item_id: str) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                return None
            return self._hydrate_item(conn, row)

    def _hydrate_item(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
        contributors = conn.execute(
            """SELECT agent_id, note_id, source_id, role, created_at
               FROM contributions WHERE item_id=? ORDER BY created_at, id""",
            (row["id"],),
        ).fetchall()
        note_title = None
        if row["note_id"]:
            note_row = conn.execute(
                "SELECT title FROM notes WHERE id=?", (row["note_id"],)
            ).fetchone()
            note_title = note_row["title"] if note_row else None
        return {
            "id": row["id"],
            "type": row["type"],
            "text": row["text"],
            "tags": _loads(row["tags"], []),
            "status": row["status"],
            "confidence": row["confidence"],
            "meta": _loads(row["meta"], {}),
            "agent_id": row["agent_id"],
            "note_id": row["note_id"],
            "note_title": note_title,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "contributors": [
                {
                    "agent_id": contributor["agent_id"],
                    "note_id": contributor["note_id"],
                    "role": contributor["role"],
                    "created_at": contributor["created_at"],
                }
                for contributor in contributors
            ],
        }

    # ------------------------------------------------------------------ #
    # Retrieval
    # ------------------------------------------------------------------ #
    def _fts_search(
        self,
        match: str,
        *,
        limit: int,
        item_type: str | None = None,
        agent_id: str | None = None,
        status: str | None = None,
    ) -> list[tuple[str, float]]:
        clauses = ["items_fts MATCH ?"]
        params: list[object] = [match]
        if item_type:
            clauses.append("items.type = ?")
            params.append(item_type)
        if status:
            clauses.append("items.status = ?")
            params.append(status)
        if agent_id:
            clauses.append(
                "EXISTS (SELECT 1 FROM contributions c WHERE c.item_id=items.id AND c.agent_id=?)"
            )
            params.append(agent_id)
        params.append(max(1, min(limit, 100)))
        sql = (
            "SELECT items.id AS id, bm25(items_fts, 0.0, 1.0, 0.3, 0.0, 0.0) AS rank "
            "FROM items_fts JOIN items ON items.id = items_fts.item_id "
            f"WHERE {' AND '.join(clauses)} ORDER BY rank LIMIT ?"
        )
        try:
            with closing(self._connect()) as conn:
                rows = conn.execute(sql, params).fetchall()
            return [(row["id"], row["rank"]) for row in rows]
        except sqlite3.OperationalError:
            return []

    def _known_tokens(self, tokens: list[str]) -> list[str]:
        """Drop query tokens absent from the store so multi-keyword queries still match."""
        known: list[str] = []
        with closing(self._connect()) as conn:
            for token in dict.fromkeys(tokens):
                if not token:
                    continue
                row = conn.execute(
                    "SELECT 1 FROM items WHERE text LIKE ? OR tags LIKE ? LIMIT 1",
                    (f"%{token}%", f"%{token}%"),
                ).fetchone()
                if row:
                    known.append(token)
        return known

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        item_type: str | None = None,
        agent_id: str | None = None,
        status: str | None = None,
    ) -> list[dict]:
        query = (query or "").strip()
        if not query:
            return []
        limit = max(1, min(limit, 100))
        tokens = fts_tokens(query)
        candidates: list[tuple[str, float]] = []
        if tokens:
            known = self._known_tokens(tokens)
            if known:
                if len(known) == len(tokens) and len(known) <= 12:
                    phrase = '"' + " ".join(known) + '"'
                    candidates = self._fts_search(
                        phrase, limit=limit * 2, item_type=item_type, agent_id=agent_id, status=status
                    )
                if not candidates:
                    candidates = self._fts_search(
                        " ".join(known), limit=limit * 2, item_type=item_type, agent_id=agent_id, status=status
                    )
                if not candidates and len(tokens) > 1:
                    candidates = self._fts_search(
                        " OR ".join(tokens), limit=limit * 2, item_type=item_type, agent_id=agent_id, status=status
                    )

        ids = [item_id for item_id, _ in candidates]
        with closing(self._connect()) as conn:
            if not candidates:
                # LIKE fallback keeps short/odd queries useful.
                clauses = ["text LIKE ?"]
                params: list[object] = [f"%{query}%"]
                if item_type:
                    clauses.append("type = ?")
                    params.append(item_type)
                if status:
                    clauses.append("status = ?")
                    params.append(status)
                params.append(limit)
                rows = conn.execute(
                    f"SELECT * FROM items WHERE {' AND '.join(clauses)} "
                    "ORDER BY updated_at DESC LIMIT ?",
                    params,
                ).fetchall()
                items = [self._hydrate_item(conn, row) for row in rows]
                if agent_id:
                    items = [
                        item for item in items
                        if any(c["agent_id"] == agent_id for c in item["contributors"])
                    ]
                for item in items:
                    item["score"] = 0.5
                return items[:limit]

            rank_by_id = {item_id: rank for item_id, rank in candidates}
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT * FROM items WHERE id IN ({placeholders})", ids
            ).fetchall()
            items = []
            for row in rows:
                item = self._hydrate_item(conn, row)
                rank = rank_by_id.get(item["id"], 0.0)
                score = -rank
                if query.casefold() in item["text"].casefold():
                    score += 2.0
                if item_type and item["type"] != item_type:
                    continue
                if status and item["status"] != status:
                    continue
                if agent_id and not any(c["agent_id"] == agent_id for c in item["contributors"]):
                    continue
                if not (
                    _relevance_ok(query, item["text"])
                    or _relevance_ok(query, item.get("note_title") or "")
                ):
                    continue
                item["score"] = round(score, 4)
                items.append(item)
        items.sort(key=lambda item: item["score"], reverse=True)
        return items[:limit]

    def search_notes(self, query: str, *, limit: int = 10) -> list[dict]:
        tokens = fts_tokens(query)
        if not tokens:
            return []
        rows: list[sqlite3.Row] = []
        with closing(self._connect()) as conn:
            for match in ('"' + " ".join(tokens) + '"', " ".join(tokens)):
                try:
                    rows = conn.execute(
                        "SELECT n.* FROM notes_fts JOIN notes n ON n.id = notes_fts.note_id "
                        "WHERE notes_fts MATCH ? ORDER BY bm25(notes_fts) LIMIT ?",
                        (match, limit),
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = []
                if rows:
                    break
            return [
                {
                    "id": row["id"],
                    "title": row["title"],
                    "summary": row["summary"],
                    "agent_id": row["agent_id"],
                    "created_at": row["created_at"],
                }
                for row in rows
                if _relevance_ok(query, row["title"] + " " + row["summary"])
            ]

    def list_notes(self, limit: int = 20) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT n.id, n.title, n.summary, n.agent_id, n.created_at, n.updated_at,
                          s.filename,
                          (SELECT COUNT(DISTINCT c.item_id) FROM contributions c
                           WHERE c.note_id = n.id) AS item_count
                   FROM notes n JOIN sources s ON s.id = n.source_id
                   ORDER BY n.created_at DESC LIMIT ?""",
                (max(1, min(limit, 100)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_note(self, note_id: str) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT n.*, s.filename, s.content_sha256, s.kind
                   FROM notes n JOIN sources s ON s.id = n.source_id WHERE n.id=?""",
                (note_id,),
            ).fetchone()
            if row is None:
                return None
            note = _loads(row["note_json"], {})
            items = conn.execute(
                """SELECT DISTINCT items.* FROM items
                   LEFT JOIN contributions c ON c.item_id = items.id
                   WHERE c.note_id = ? OR items.note_id = ?
                   ORDER BY items.created_at, items.rowid""",
                (note_id, note_id),
            ).fetchall()
            return {
                "id": row["id"],
                "title": row["title"],
                "markdown": row["body_markdown"],
                "note": note,
                "source": {
                    "filename": row["filename"],
                    "sha256": row["content_sha256"],
                    "kind": row["kind"],
                },
                "items": [self._hydrate_item(conn, item) for item in items],
            }

    def list_items(
        self,
        *,
        item_type: str | None = None,
        agent_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[object] = []
        if item_type:
            clauses.append("type=?")
            params.append(item_type)
        if status:
            clauses.append("status=?")
            params.append(status)
        if agent_id:
            clauses.append(
                "EXISTS (SELECT 1 FROM contributions c WHERE c.item_id=items.id AND c.agent_id=?)"
            )
            params.append(agent_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(limit, 200)))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT * FROM items {where} ORDER BY updated_at DESC LIMIT ?", params
            ).fetchall()
            return [self._hydrate_item(conn, row) for row in rows]

    def recent_items(self, limit: int = 40, *, item_types: list[str] | None = None) -> list[dict]:
        params: list[object] = []
        clause = ""
        if item_types:
            clause = f"WHERE type IN ({','.join('?' for _ in item_types)})"
            params.extend(item_types)
        params.append(max(1, min(limit, 200)))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT * FROM items {clause} ORDER BY updated_at DESC LIMIT ?", params
            ).fetchall()
            items = [self._hydrate_item(conn, row) for row in rows]
        for item in items:
            item["score"] = 0.5
        return items

    def stats(self) -> dict:
        with closing(self._connect()) as conn:
            totals = {
                "notes": conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0],
                "items": conn.execute("SELECT COUNT(*) FROM items").fetchone()[0],
                "agents": conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0],
                "sources": conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
                "contributions": conn.execute("SELECT COUNT(*) FROM contributions").fetchone()[0],
            }
            by_type = {
                row["type"]: row["count"]
                for row in conn.execute(
                    "SELECT type, COUNT(*) AS count FROM items GROUP BY type ORDER BY count DESC"
                ).fetchall()
            }
            by_status = {
                row["status"]: row["count"]
                for row in conn.execute(
                    "SELECT status, COUNT(*) AS count FROM items GROUP BY status"
                ).fetchall()
            }
            agents = [
                dict(row)
                for row in conn.execute(
                    """SELECT a.id, a.kind, a.first_seen, a.last_seen,
                              (SELECT COUNT(DISTINCT c.item_id) FROM contributions c
                               WHERE c.agent_id = a.id) AS item_count
                       FROM agents a ORDER BY a.last_seen DESC"""
                ).fetchall()
            ]
            tag_counter: dict[str, int] = {}
            for row in conn.execute("SELECT note_json FROM notes").fetchall():
                for tag in _loads(row["note_json"], {}).get("tags", []):
                    tag_counter[tag] = tag_counter.get(tag, 0) + 1
            top_tags = sorted(tag_counter.items(), key=lambda pair: (-pair[1], pair[0]))[:10]
            recent_notes = [
                dict(row)
                for row in conn.execute(
                    "SELECT id, title, agent_id, created_at FROM notes ORDER BY created_at DESC LIMIT 5"
                ).fetchall()
            ]
        return {
            "totals": totals,
            "items_by_type": by_type,
            "items_by_status": by_status,
            "agents": agents,
            "top_tags": [{"tag": tag, "count": count} for tag, count in top_tags],
            "recent_notes": recent_notes,
            "db_path": self.db_path,
            "generated_at": now_iso(),
        }

    def agent_summary(self, agent_id: str) -> dict:
        items = self.list_items(agent_id=agent_id, limit=200)
        return {
            "agent_id": agent_id,
            "item_count": len(items),
            "types": {
                item_type: sum(1 for item in items if item["type"] == item_type)
                for item_type in sorted({item["type"] for item in items})
            },
            "latest": items[0]["updated_at"] if items else None,
        }
