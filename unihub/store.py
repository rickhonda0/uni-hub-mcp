"""SQLite document store with full-text search, plus markdown files under corpus/."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone

from .classify import slug
from .config import CORPUS, DB_PATH, ROOT, STATE

SCHEMA = """
CREATE TABLE IF NOT EXISTS units(
    unit TEXT PRIMARY KEY, aliases TEXT, name TEXT, term TEXT,
    canvas_id INTEGER, ed_id INTEGER, canvas_synced TEXT, ed_synced TEXT);
CREATE TABLE IF NOT EXISTS docs(
    doc_id TEXT PRIMARY KEY, unit TEXT, source TEXT, kind TEXT, week INTEGER, title TEXT,
    module TEXT, module_pos INTEGER, item_pos INTEGER, tags TEXT, url TEXT, md_path TEXT,
    raw_path TEXT, parent_id TEXT, fingerprint TEXT, updated_at TEXT, chars INTEGER, synced_at TEXT);
CREATE INDEX IF NOT EXISTS docs_unit ON docs(unit, week);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
    doc_id UNINDEXED, unit UNINDEXED, kind UNINDEXED, week UNINDEXED, title, heading, body,
    tokenize = 'porter unicode61');
"""

DOC_FIELDS = (
    "doc_id", "unit", "source", "kind", "week", "title", "module", "module_pos", "item_pos", "tags",
    "url", "md_path", "raw_path", "parent_id", "fingerprint", "updated_at", "chars", "synced_at",
)
META_FIELDS = ("kind", "week", "title", "module", "module_pos", "item_pos", "tags", "url", "parent_id")
CHUNK_CHARS = 1500


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def chunk(body: str, title: str):
    """Yield (heading, text) pieces: split on '## ' headings, then to at most CHUNK_CHARS."""
    sections, heading, buffer = [], "", []
    for line in body.splitlines():
        if line.startswith("## "):
            if buffer:
                sections.append((heading, "\n".join(buffer)))
            heading, buffer = line[3:].strip(), []
        else:
            buffer.append(line)
    if buffer:
        sections.append((heading, "\n".join(buffer)))
    produced = False
    for heading, text in sections:
        text = text.strip()
        while len(text) > CHUNK_CHARS:
            cut = text.rfind("\n\n", 0, CHUNK_CHARS)
            if cut < CHUNK_CHARS // 3:
                cut = text.rfind("\n", 0, CHUNK_CHARS)
            if cut < CHUNK_CHARS // 3:
                cut = CHUNK_CHARS
            yield heading, text[:cut]
            produced = True
            text = text[cut:].strip()
        if text:
            yield heading, text
            produced = True
    if not produced:
        yield "", title


class Store:
    def __init__(self) -> None:
        STATE.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(DB_PATH)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # ---- units -------------------------------------------------------------
    def put_unit(self, unit: str, aliases: list[str], name: str, term: str, canvas_id: int | None) -> None:
        self.db.execute(
            "INSERT INTO units(unit, aliases, name, term, canvas_id) VALUES(?,?,?,?,?) "
            "ON CONFLICT(unit) DO UPDATE SET aliases=excluded.aliases, name=excluded.name, "
            "term=excluded.term, canvas_id=excluded.canvas_id",
            (unit, ",".join(aliases), name, term, canvas_id),
        )
        self.db.commit()

    def mark_synced(self, unit: str, source: str, ed_id: int | None = None) -> None:
        column = "canvas_synced" if source == "canvas" else "ed_synced"
        self.db.execute(f"UPDATE units SET {column}=? WHERE unit=?", (now_iso(), unit))
        if ed_id is not None:
            self.db.execute("UPDATE units SET ed_id=? WHERE unit=?", (ed_id, unit))
        self.db.commit()

    def units(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM units ORDER BY term DESC, unit").fetchall()

    def resolve_unit(self, name: str) -> sqlite3.Row | None:
        """Match a unit key or any alias, e.g. DATA2902 or data2002."""
        wanted = name.strip().upper()
        for row in self.units():
            if wanted == row["unit"] or wanted in (row["aliases"] or "").split(","):
                return row
        return None

    # ---- docs --------------------------------------------------------------
    def is_fresh(self, doc_id: str, fingerprint: str | None) -> bool:
        row = self.db.execute("SELECT fingerprint, md_path FROM docs WHERE doc_id=?", (doc_id,)).fetchone()
        return bool(
            row and fingerprint and row["fingerprint"] == fingerprint
            and row["md_path"] and (ROOT / row["md_path"]).exists()
        )

    def touch(self, meta: dict) -> None:
        """Refresh placement metadata (module, week, kind) without rewriting content."""
        sets = ", ".join(f"{field}=?" for field in META_FIELDS)
        self.db.execute(f"UPDATE docs SET {sets} WHERE doc_id=?", [meta.get(f) for f in META_FIELDS] + [meta["doc_id"]])
        self.db.execute("UPDATE chunks SET kind=?, week=? WHERE doc_id=?", (meta.get("kind"), meta.get("week"), meta["doc_id"]))
        self.db.commit()

    def put(self, meta: dict, body: str) -> None:
        doc_id = meta["doc_id"]
        suffix = re.sub(r"\W", "", doc_id.split(":")[-1])[:16]
        rel = f"corpus/{meta['unit']}/{meta['source']}/{slug(meta['title'])}_{suffix}.md"
        path = ROOT / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._render(meta, body), encoding="utf-8")

        old = self.db.execute("SELECT md_path FROM docs WHERE doc_id=?", (doc_id,)).fetchone()
        if old and old["md_path"] and old["md_path"] != rel:
            (ROOT / old["md_path"]).unlink(missing_ok=True)

        row = {field: meta.get(field) for field in DOC_FIELDS}
        row.update(md_path=rel, chars=len(body), synced_at=now_iso())
        self.db.execute(
            f"INSERT OR REPLACE INTO docs({', '.join(DOC_FIELDS)}) VALUES({', '.join('?' * len(DOC_FIELDS))})",
            [row[field] for field in DOC_FIELDS],
        )
        self.db.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
        self.db.executemany(
            "INSERT INTO chunks(doc_id, unit, kind, week, title, heading, body) VALUES(?,?,?,?,?,?,?)",
            [(doc_id, meta["unit"], meta["kind"], meta.get("week"), meta["title"], heading, text)
             for heading, text in chunk(body, meta["title"])],
        )
        self.db.commit()

    @staticmethod
    def _render(meta: dict, body: str) -> str:
        front = {k: meta.get(k) for k in ("doc_id", "title", "unit", "source", "kind", "week", "module", "tags", "url", "raw_path", "updated_at")}
        lines = ["---"] + [f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in front.items() if v not in (None, "")]
        return "\n".join(lines + ["---", "", f"# {meta['title']}", "", body.strip(), ""])

    def doc(self, doc_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM docs WHERE doc_id=?", (doc_id,)).fetchone()

    def outline(self, unit: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT doc_id, source, kind, week, title, tags, module, chars FROM docs WHERE unit=? "
            "ORDER BY week IS NULL, week, module_pos, item_pos, title",
            (unit,),
        ).fetchall()

    def counts(self, unit: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT kind, COUNT(*) AS n FROM docs WHERE unit=? GROUP BY kind ORDER BY n DESC", (unit,)
        ).fetchall()

    def search(self, query: str, units: list[str] | None = None, kinds: list[str] | None = None,
               week: int | None = None, limit: int = 8, per_doc: int = 2) -> list[sqlite3.Row]:
        terms = re.findall(r"\w+", query)
        if not terms:
            return []
        filters, params = [], []
        if units:
            filters.append(f"unit IN ({','.join('?' * len(units))})")
            params += units
        if kinds:
            filters.append(f"kind IN ({','.join('?' * len(kinds))})")
            params += kinds
        if week is not None:
            filters.append("week = ?")
            params.append(week)
        where = "".join(f" AND {f}" for f in filters)
        sql = (
            "SELECT doc_id, unit, kind, week, title, heading, "
            "snippet(chunks, 6, '**', '**', ' … ', 40) AS snip, "
            "bm25(chunks, 0, 0, 0, 0, 6.0, 3.0, 1.0) AS score "
            f"FROM chunks WHERE chunks MATCH ?{where} ORDER BY score LIMIT ?"
        )
        for joiner in (" AND ", " OR "):  # strict first, then loose
            match = joiner.join(f'"{t}"' for t in terms)
            rows = self.db.execute(sql, [match, *params, limit * 5]).fetchall()
            if rows:
                kept, per = [], {}
                for row in rows:
                    per[row["doc_id"]] = per.get(row["doc_id"], 0) + 1
                    if per[row["doc_id"]] <= per_doc:
                        kept.append(row)
                return kept[:limit]
        return []
