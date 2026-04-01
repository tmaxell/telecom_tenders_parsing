"""SQLite-хранилище с трекингом экспортов."""

from __future__ import annotations

import sqlite3
import logging
from datetime import datetime, timezone
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
    exported_at     TEXT DEFAULT NULL,
    PRIMARY KEY (ocid, release_id)
);

CREATE INDEX IF NOT EXISTS idx_raw_source ON raw_tenders(source_id);
CREATE INDEX IF NOT EXISTS idx_raw_date   ON raw_tenders(date_published);
CREATE INDEX IF NOT EXISTS idx_raw_status ON raw_tenders(status);
CREATE INDEX IF NOT EXISTS idx_raw_exported ON raw_tenders(exported_at);
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
    exported_at       TEXT DEFAULT NULL,
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

# Миграция: добавить exported_at если отсутствует
MIGRATIONS = [
    """
    ALTER TABLE raw_tenders
    ADD COLUMN exported_at TEXT DEFAULT NULL;
    """,
    """
    ALTER TABLE matched_tenders
    ADD COLUMN exported_at TEXT DEFAULT NULL;
    """,
]


class Database:

    def __init__(self, db_path: str = "data/tenders.db"):
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()
        self._run_migrations()

    def _create_tables(self):
        """Создаёт таблицы. Если таблица уже есть — не падает."""
        cur = self.conn.cursor()

        # Сначала создаём таблицы БЕЗ новых колонок (совместимо со старой БД)
        cur.executescript("""
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
        """)

        cur.executescript("""
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
        """)

        cur.executescript("""
            CREATE TABLE IF NOT EXISTS collection_state (
                source_id       TEXT PRIMARY KEY,
                last_collected  TEXT,
                last_page       INTEGER DEFAULT 0,
                last_release_date TEXT
            );
        """)

        self.conn.commit()

    def _run_migrations(self):
        """Безопасно добавить новые колонки."""
        for sql in MIGRATIONS:
            try:
                self.conn.execute(sql)
                self.conn.commit()
            except sqlite3.OperationalError:
                # Колонка уже существует — нормально
                pass

    # ── raw tenders ───────────────────────────────────────────────

    def upsert_tender(self, t: Tender) -> bool:
        sql = """
            INSERT OR IGNORE INTO raw_tenders
            (ocid, release_id, source_id, country,
             title, description, status, procurement_method,
             value_amount, value_currency,
             buyer_name, buyer_id,
             tender_period_start, tender_period_end, date_published,
             items_text, raw_json, collected_at, exported_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, NULL)
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
        return self.conn.execute(
            "SELECT COUNT(*) FROM raw_tenders"
        ).fetchone()[0]

    # ── ★ Экспорт-трекинг для raw_tenders ────────────────────────

    def get_unexported_raw(
        self,
        source_id: Optional[str] = None,
    ) -> list[dict]:
        """Вернуть только те тендеры, что ещё не экспортировались."""
        clauses = ["exported_at IS NULL"]
        params: list = []
        if source_id:
            clauses.append("source_id = ?")
            params.append(source_id)

        where = f"WHERE {' AND '.join(clauses)}"
        sql = f"""
            SELECT ocid, source_id, country, title, description,
                   buyer_name, value_amount, value_currency,
                   status, procurement_method,
                   date_published, tender_period_end,
                   items_text, collected_at
            FROM raw_tenders {where}
            ORDER BY collected_at DESC
        """
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def count_unexported_raw(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM raw_tenders WHERE exported_at IS NULL"
        ).fetchone()[0]

    def mark_raw_exported(self, ocids: list[str]):
        """Пометить тендеры как экспортированные."""
        if not ocids:
            return
        now = datetime.now(timezone.utc).isoformat()
        # Батчами по 500
        for i in range(0, len(ocids), 500):
            chunk = ocids[i:i + 500]
            placeholders = ",".join("?" * len(chunk))
            self.conn.execute(
                f"UPDATE raw_tenders SET exported_at = ? "
                f"WHERE ocid IN ({placeholders})",
                [now] + chunk,
            )
        self.conn.commit()

    def reset_export_flags(self):
        """Сбросить все флаги экспорта (для повторного экспорта)."""
        self.conn.execute(
            "UPDATE raw_tenders SET exported_at = NULL"
        )
        self.conn.execute(
            "UPDATE matched_tenders SET exported_at = NULL"
        )
        self.conn.commit()

    # ── matched tenders ───────────────────────────────────────────

    def save_match(self, m: MatchResult):
        sql = """
            INSERT OR REPLACE INTO matched_tenders
            (ocid, source_id, country,
             title, description, buyer_name,
             value_amount, value_currency,
             tender_period_end, date_published,
             matched_product, matched_keywords, match_snippet,
             raw_json, exported_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, NULL)
        """
        self.conn.execute(sql, (
            m.ocid, m.source_id, m.country,
            m.title, m.description, m.buyer_name,
            m.value_amount, m.value_currency,
            m.tender_period_end, m.date_published,
            m.matched_product, m.matched_keywords,
            m.match_snippet, m.raw_json,
        ))

    def save_matches_batch(self, matches: list[MatchResult]):
        for m in matches:
            self.save_match(m)
        self.conn.commit()

    def clear_matches(self):
        self.conn.execute("DELETE FROM matched_tenders")
        self.conn.commit()

    def count_matches(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM matched_tenders"
        ).fetchone()[0]

    def get_all_matches(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM matched_tenders"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_unexported_matches(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM matched_tenders WHERE exported_at IS NULL"
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_matches_exported(self, ocids: list[str]):
        if not ocids:
            return
        now = datetime.now(timezone.utc).isoformat()
        for i in range(0, len(ocids), 500):
            chunk = ocids[i:i + 500]
            placeholders = ",".join("?" * len(chunk))
            self.conn.execute(
                f"UPDATE matched_tenders SET exported_at = ? "
                f"WHERE ocid IN ({placeholders})",
                [now] + chunk,
            )
        self.conn.commit()

    # ── Для viewer: все данные (включая экспортированные) ──────────

    def get_all_raw(self, source: str = None) -> list[dict]:
        if source:
            sql = """
                SELECT ocid, source_id, country, title, description,
                       buyer_name, value_amount, value_currency,
                       status, procurement_method,
                       date_published, tender_period_end,
                       items_text, collected_at, exported_at
                FROM raw_tenders WHERE source_id = ?
                ORDER BY collected_at DESC
            """
            return [
                dict(r) for r in
                self.conn.execute(sql, (source,)).fetchall()
            ]
        else:
            sql = """
                SELECT ocid, source_id, country, title, description,
                       buyer_name, value_amount, value_currency,
                       status, procurement_method,
                       date_published, tender_period_end,
                       items_text, collected_at, exported_at
                FROM raw_tenders ORDER BY collected_at DESC
            """
            return [dict(r) for r in self.conn.execute(sql).fetchall()]

    # ── collection state ──────────────────────────────────────────

    def get_state(self, source_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM collection_state WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        return dict(row) if row else None

    def save_state(
        self,
        source_id: str,
        last_collected: str,
        last_page: int = 0,
        last_release_date: str = "",
    ):
        self.conn.execute("""
            INSERT OR REPLACE INTO collection_state
            (source_id, last_collected, last_page, last_release_date)
            VALUES (?, ?, ?, ?)
        """, (source_id, last_collected, last_page, last_release_date))
        self.conn.commit()

    # ── stats ─────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        stats = {
            "total_raw": self.count_raw(),
            "total_matched": self.count_matches(),
            "unexported_raw": self.count_unexported_raw(),
        }

        stats["by_source"] = [dict(r) for r in self.conn.execute("""
            SELECT source_id, country, COUNT(*) as cnt,
                   SUM(CASE WHEN exported_at IS NULL
                       THEN 1 ELSE 0 END) as not_exported,
                   MIN(date_published) as earliest,
                   MAX(date_published) as latest
            FROM raw_tenders GROUP BY source_id
            ORDER BY cnt DESC
        """).fetchall()]

        stats["by_status"] = [dict(r) for r in self.conn.execute("""
            SELECT status, COUNT(*) as cnt
            FROM raw_tenders GROUP BY status ORDER BY cnt DESC
        """).fetchall()]

        stats["by_country"] = [dict(r) for r in self.conn.execute("""
            SELECT country, COUNT(*) as cnt
            FROM raw_tenders GROUP BY country ORDER BY cnt DESC
        """).fetchall()]

        stats["by_product"] = [dict(r) for r in self.conn.execute("""
            SELECT matched_product, COUNT(*) as cnt
            FROM matched_tenders GROUP BY matched_product
            ORDER BY cnt DESC
        """).fetchall()]

        stats["top_buyers"] = [dict(r) for r in self.conn.execute("""
            SELECT buyer_name, COUNT(*) as cnt
            FROM raw_tenders WHERE buyer_name != ''
            GROUP BY buyer_name ORDER BY cnt DESC LIMIT 15
        """).fetchall()]

        stats["collection_state"] = [dict(r) for r in self.conn.execute(
            "SELECT * FROM collection_state ORDER BY last_collected DESC"
        ).fetchall()]

        return stats

    # ── lifecycle ─────────────────────────────────────────────────

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()