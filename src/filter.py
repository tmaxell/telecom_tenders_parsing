#!/usr/bin/env python3
"""
Keyword-based Tender Filter (Post-processor)
=============================================
Загружает собранные тендеры из SQLite, прогоняет по набору ключевых слов
из keywords.yaml и сохраняет совпадения обратно в БД + экспорт в CSV/JSON.

Запуск:
    python -m src.filter
    python -m src.filter --product CEIR
    python -m src.filter --export json
    python -m src.filter --stats
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from src.database import Database
from src.models import MatchResult

LOG_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logger = logging.getLogger("filter")


# ---------------------------------------------------------------------------
# Keyword loading
# ---------------------------------------------------------------------------
class KeywordIndex:
    """
    Загружает keywords.yaml и строит индекс для быстрого поиска.

    Поддерживает три режима совпадения:
      - substring   : keyword ⊂ text
      - word_boundary : keyword как целое слово/фраза (\\b…\\b)
      - regex       : keyword — готовое регулярное выражение
    """

    def __init__(self, config_path: str = "config/keywords.yaml",
                 match_mode: str = "substring",
                 case_sensitive: bool = False,
                 min_kw_length: int = 3):
        self.match_mode = match_mode
        self.case_sensitive = case_sensitive
        self.min_kw_length = min_kw_length
        self.products: dict[str, list[re.Pattern]] = {}
        self._raw: dict[str, list[str]] = {}
        self._load(config_path)

    def _load(self, path: str):
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        flags = 0 if self.case_sensitive else re.IGNORECASE

        for product in data.get("products", []):
            name = product["name"]
            all_kw: list[str] = []
            all_kw.extend(product.get("keywords_ru", []))
            all_kw.extend(product.get("keywords_en", []))

            # де-дупликация и нормализация
            seen = set()
            patterns: list[re.Pattern] = []
            raw_list: list[str] = []

            for kw in all_kw:
                kw = kw.strip()
                if not kw or len(kw) < self.min_kw_length:
                    continue
                key = kw.lower()
                if key in seen:
                    continue
                seen.add(key)
                raw_list.append(kw)

                if self.match_mode == "regex":
                    patterns.append(re.compile(kw, flags))
                elif self.match_mode == "word_boundary":
                    escaped = re.escape(kw)
                    patterns.append(re.compile(rf"\b{escaped}\b", flags))
                else:  # substring
                    escaped = re.escape(kw)
                    patterns.append(re.compile(escaped, flags))

            self.products[name] = patterns
            self._raw[name] = raw_list
            logger.info(
                "Product %-18s: %d keyword patterns loaded",
                name, len(patterns),
            )

    def match(self, text: str) -> list[tuple[str, list[str], str]]:
        """
        Ищет совпадения во входном тексте.

        Returns:
            list of (product_name, [matched_keywords], snippet)
        """
        results: list[tuple[str, list[str], str]] = []

        for product_name, patterns in self.products.items():
            matched_kws: list[str] = []
            snippet = ""

            for pattern, raw_kw in zip(patterns, self._raw[product_name]):
                m = pattern.search(text)
                if m:
                    matched_kws.append(raw_kw)
                    if not snippet:
                        # Вырезаем фрагмент вокруг совпадения (±80 символов)
                        start = max(0, m.start() - 80)
                        end = min(len(text), m.end() + 80)
                        snippet = "…" + text[start:end].strip() + "…"

            if matched_kws:
                results.append((product_name, matched_kws, snippet))

        return results

    @property
    def product_names(self) -> list[str]:
        return list(self.products.keys())


# ---------------------------------------------------------------------------
# Filter engine
# ---------------------------------------------------------------------------
class TenderFilter:
    """Применяет KeywordIndex ко всем тендерам в БД."""

    def __init__(self, db: Database, keyword_index: KeywordIndex):
        self.db = db
        self.kw = keyword_index

    def run(
        self,
        product_filter: Optional[str] = None,
        source_filter: Optional[str] = None,
    ) -> int:
        """Фильтрует тендеры, сохраняет совпадения в matched_tenders.
        Returns: количество совпадений."""

        self.db.clear_matches()
        match_count = 0
        checked = 0
        batch: list[MatchResult] = []

        for row in self.db.iter_raw_tenders(source_id=source_filter):
            checked += 1

            # Собираем текст для поиска
            search_parts = [
                row.get("title", ""),
                row.get("description", ""),
                row.get("items_text", ""),
                row.get("buyer_name", ""),
            ]
            search_text = " ".join(p for p in search_parts if p)

            if not search_text.strip():
                continue

            hits = self.kw.match(search_text)
            if not hits:
                continue

            for product_name, matched_kws, snippet in hits:
                if product_filter and product_name != product_filter:
                    continue

                mr = MatchResult(
                    ocid=row["ocid"],
                    source_id=row["source_id"],
                    country=row["country"],
                    title=row["title"] or "",
                    description=(row["description"] or "")[:500],
                    buyer_name=row["buyer_name"] or "",
                    value_amount=row["value_amount"],
                    value_currency=row["value_currency"] or "",
                    tender_period_end=row["tender_period_end"],
                    date_published=row["date_published"],
                    matched_product=product_name,
                    matched_keywords=", ".join(matched_kws),
                    match_snippet=snippet[:300],
                    raw_json=row.get("raw_json", ""),
                )
                batch.append(mr)
                match_count += 1

                if len(batch) >= 200:
                    self.db.save_matches_batch(batch)
                    batch.clear()

            if checked % 5000 == 0:
                logger.info("Checked %d tenders, %d matches so far …", checked, match_count)

        if batch:
            self.db.save_matches_batch(batch)

        logger.info(
            "Filtering done: checked %d tenders, found %d matches",
            checked, match_count,
        )
        return match_count


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def export_matches(db: Database, fmt: str = "csv",
                   out_dir: str = "data/exports"):
    """Export matched_tenders to file."""
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    matches = db.get_all_matches()

    if not matches:
        logger.warning("No matches to export.")
        return

    # exclude raw_json from exports (too large)
    export_fields = [k for k in matches[0].keys() if k != "raw_json"]

    if fmt in ("csv", "both"):
        csv_path = Path(out_dir) / f"matched_tenders_{timestamp}.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=export_fields, extrasaction="ignore")
            writer.writeheader()
            for row in matches:
                writer.writerow({k: row[k] for k in export_fields})
        logger.info("Exported %d matches → %s", len(matches), csv_path)

    if fmt in ("json", "both"):
        json_path = Path(out_dir) / f"matched_tenders_{timestamp}.json"
        export_data = [{k: row[k] for k in export_fields} for row in matches]
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(export_data, f, ensure_ascii=False, indent=2, default=str)
        logger.info("Exported %d matches → %s", len(matches), json_path)


def print_stats(db: Database, kw: KeywordIndex):
    """Print summary statistics."""
    matches = db.get_all_matches()
    print("\n" + "=" * 70)
    print(f" Total raw tenders in DB  : {db.count_raw()}")
    print(f" Total matched tenders    : {len(matches)}")
    print("-" * 70)

    # по продуктам
    by_product: dict[str, int] = {}
    by_country: dict[str, int] = {}
    for m in matches:
        p = m["matched_product"]
        c = m["country"]
        by_product[p] = by_product.get(p, 0) + 1
        by_country[c] = by_country.get(c, 0) + 1

    print(" Matches by product:")
    for p in sorted(by_product, key=by_product.get, reverse=True):
        print(f"   {p:25s} : {by_product[p]}")

    print(" Matches by country:")
    for c in sorted(by_country, key=by_country.get, reverse=True):
        print(f"   {c:25s} : {by_country[c]}")

    print("=" * 70 + "\n")

    # Топ-10 совпадений (для визуальной проверки)
    if matches:
        print(" Top-10 matches (preview):")
        print("-" * 70)
        for m in matches[:10]:
            print(f"  [{m['country']}] {m['matched_product']:15s} | "
                  f"{(m['title'] or 'N/A')[:60]}")
            print(f"    Keywords: {m['matched_keywords'][:80]}")
            print(f"    Buyer   : {m['buyer_name'][:50]}")
            print(f"    Value   : {m['value_amount']} {m['value_currency']}")
            print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Tender Keyword Filter")
    parser.add_argument("--product", type=str,
                        help="Filter only this product (e.g. CEIR, CVM)")
    parser.add_argument("--source", type=str,
                        help="Filter only this source_id")
    parser.add_argument("--export", type=str, default="csv",
                        choices=["csv", "json", "both"],
                        help="Export format")
    parser.add_argument("--match-mode", type=str, default="substring",
                        choices=["substring", "word_boundary", "regex"])
    parser.add_argument("--stats", action="store_true",
                        help="Print statistics after filtering")
    parser.add_argument("--config-dir", default="config")
    args = parser.parse_args()

    # settings
    settings_path = f"{args.config_dir}/settings.yaml"
    with open(settings_path, "r", encoding="utf-8") as f:
        settings = yaml.safe_load(f) or {}

    # logging
    log_cfg = settings.get("logging", {})
    log_file = log_cfg.get("file", "logs/filter.log")
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, log_cfg.get("level", "INFO")),
        format=LOG_FMT,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )

    filter_cfg = settings.get("filtering", {})
    db_path = settings.get("database", {}).get("path", "data/tenders.db")
    export_dir = filter_cfg.get("export_dir", "data/exports")

    # keyword index
    kw_index = KeywordIndex(
        config_path=f"{args.config_dir}/keywords.yaml",
        match_mode=args.match_mode or filter_cfg.get("match_mode", "substring"),
        case_sensitive=filter_cfg.get("case_sensitive", False),
        min_kw_length=filter_cfg.get("min_keyword_length", 3),
    )

    with Database(db_path) as db:
        if db.count_raw() == 0:
            logger.error("No tenders in DB. Run collector first: python -m src.collector")
            sys.exit(1)

        logger.info("DB contains %d raw tenders", db.count_raw())

        # filter
        fltr = TenderFilter(db, kw_index)
        count = fltr.run(
            product_filter=args.product,
            source_filter=args.source,
        )

        if count == 0:
            logger.warning("No matches found.")
        else:
            # export
            export_matches(db, fmt=args.export, out_dir=export_dir)

        # stats
        if args.stats or count > 0:
            print_stats(db, kw_index)


if __name__ == "__main__":
    main()