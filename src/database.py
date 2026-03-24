"""SQLite-хранилище для сырых тендеров и результатов фильтрации."""

from __future__ import annotations

import sqlite3
import json
import logging
from pathlib import Path
from typing import Optional

from src.models import Tender, MatchResult

logger = logging.getLogger(__name__)

SCHEMA_RAW = """
CREATE TABLE IF NOT EXISTS raw_tenders (
    ocid            TEXT,
    release_id      TEXT,
    source_id       TEXT,
    country         TEXT,
    title           TEXT,
    description     TEXT,
    status          TEXT,
    procurement_method TEXT,
    value_amount    REAL,
    value_currency  TEXT,
    buyer_name      TEXT,
    buyer_id        TEXT,
    tender_period_start TEXT,
    tender_period_end   TEXT,
    date_published  TEXT,
    items_text      TEXT,
    raw_json        TEXT,
    collected_at    TEXT,
    PRIMARY KEY (ocid, release_id)
);

CREATE INDEX IF NOT EXISTS idx_raw_source ON raw_tenders(source_id);
CREATE INDEX IF NOT EXISTS idx_raw_date   ON raw_tenders(date_published);
CREATE INDEX IF NOT EXISTS idx_raw_status ON raw_tenders(status);
"""

SCHEMA_MATCHES = """
CREATE TABLE IF NOT EXISTS matched_tenders (
    ocid              TEXT,
    source_id         TEXT,
    country           TEXT,
    title             TEXT,
    description       TEXT,
    buyer_name        TEXT,
    value_amount      REAL,
    value_currency    TEXT,
    tender_period_end TEXT,
    date_published    TEXT,
    matched_product   TEXT,
    matched_keywords  TEXT,
    match_snippet     TEXT,
    raw_json          TEXT,
    PRIMARY KEY (ocid, matched_product)
);
"""

SCHEMA_STATE = """
CREATE TABLE IF NOT EXISTS collection_state (
    source_id       TEXT PRIMARY KEY,
    last_collected  TEXT,
    last_page       INTEGER DEFAULT 0,
    last_release_date TEXT
);
"""


class Database:
    """Thin wrapper around SQLite."""

    def __init__(self, db_path: str = "data/tenders.db"):
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def _create_tables(self):
        cur = self.conn.cursor()
        cur.executescript(SCHEMA_RAW)
        cur.executescript(SCHEMA_MATCHES)
        cur.executescript(SCHEMA_STATE)
        self.conn.commit()

    # --- raw tenders -------------------------------------------------------
    def upsert_tender(self, t: Tender) -> bool:
        """INSERT OR IGNORE. Returns True if new row inserted."""
        sql = """
            INSERT OR IGNORE INTO raw_tenders
            (ocid, release_id, source_id, country,
             title, description, status, procurement_method,
             value_amount, value_currency,
             buyer_name, buyer_id,
             tender_period_start, tender_period_end, date_published,
             items_text, raw_json, collected_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """
        cur = self.conn.execute(sql, (
            t.ocid, t.release_id, t.source_id, t.country,
            t.title, t.description, t.status, t.procurement_method,
            t.value_amount, t.value_currency,
            t.buyer_name, t.buyer_id,
            t.tender_period_start, t.tender_period_end, t.date_published,
            t.items_text, t.raw_json, t.collected_at,
        ))
        return cur.rowcount > 0

    def upsert_tenders_batch(self, tenders: list[Tender]) -> int:
        """Batch upsert. Returns count of new rows."""
        new = 0
        for t in tenders:
            if self.upsert_tender(t):
                new += 1
        self.conn.commit()
        return new

    def iter_raw_tenders(
        self,
        source_id: Optional[str] = None,
        status: Optional[str] = None,
    ):
        """Yield raw_tenders rows as dicts."""
        clauses = []
        params = []
        if source_id:
            clauses.append("source_id = ?")
            params.append(source_id)
        if status:
            clauses.append("status = ?")
            params.append(status)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM raw_tenders {where}"

        for row in self.conn.execute(sql, params):
            yield dict(row)

    def count_raw(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM raw_tenders").fetchone()[0]

    # --- matched tenders ---------------------------------------------------
    def save_match(self, m: MatchResult):
        sql = """
            INSERT OR REPLACE INTO matched_tenders
            (ocid, source_id, country,
             title, description, buyer_name,
             value_amount, value_currency,
             tender_period_end, date_published,
             matched_product, matched_keywords, match_snippet, raw_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """
        self.conn.execute(sql, (
            m.ocid, m.source_id, m.country,
            m.title, m.description, m.buyer_name,
            m.value_amount, m.value_currency,
            m.tender_period_end, m.date_published,
            m.matched_product, m.matched_keywords, m.match_snippet, m.raw_json,
        ))

    def save_matches_batch(self, matches: list[MatchResult]):
        for m in matches:
            self.save_match(m)
        self.conn.commit()

    def clear_matches(self):
        self.conn.execute("DELETE FROM matched_tenders")
        self.conn.commit()

    def count_matches(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM matched_tenders").fetchone()[0]

    def get_all_matches(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM matched_tenders").fetchall()
        return [dict(r) for r in rows]

    # --- collection state --------------------------------------------------
    def get_state(self, source_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM collection_state WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        return dict(row) if row else None

    def save_state(self, source_id: str, last_collected: str,
                   last_page: int = 0, last_release_date: str = ""):
        self.conn.execute("""
            INSERT OR REPLACE INTO collection_state
            (source_id, last_collected, last_page, last_release_date)
            VALUES (?, ?, ?, ?)
        """, (source_id, last_collected, last_page, last_release_date))
        self.conn.commit()

    # --- lifecycle ---------------------------------------------------------
    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()