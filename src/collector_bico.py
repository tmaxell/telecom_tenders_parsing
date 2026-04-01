#!/usr/bin/env python3
"""
BicoTender Collector
====================
python -m src.collector_bico
python -m src.collector_bico --product CEIR --max-pages 5
python -m src.collector_bico --search "SMS firewall" --details
python -m src.collector_bico --all-keywords --max-pages 5 --per-page 100
python -m src.collector_bico --test
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import yaml

from src.sources.bicotender import BicoTenderSource, DEFAULT_PER_PAGE
from src.database import Database
from src.models import Tender

LOG_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logger = logging.getLogger("collector_bico")


def load_yaml(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_keywords_for_search(
    keywords_path: str,
    product: str | None = None,
    lang: str = "en",
) -> list[str]:
    """Извлечь ключевые слова для поисковых запросов."""
    data = load_yaml(keywords_path)
    keywords: list[str] = []

    for prod in data.get("products", []):
        if product and prod["name"] != product:
            continue
        if lang in ("ru", "both"):
            keywords.extend(prod.get("keywords_ru", []))
        if lang in ("en", "both"):
            keywords.extend(prod.get("keywords_en", []))

    seen = set()
    result = []
    for kw in keywords:
        kw = kw.strip()
        if len(kw) < 3 or kw.lower() in seen:
            continue
        seen.add(kw.lower())
        result.append(kw)

    return result


def main():
    parser = argparse.ArgumentParser(
        description="BicoTender Keyword Collector",
    )
    parser.add_argument(
        "--search", type=str,
        help="Manual search query (single)",
    )
    parser.add_argument(
        "--product", type=str,
        help="Only keywords from this product (CEIR, CVM, ...)",
    )
    parser.add_argument(
        "--all-keywords", action="store_true",
        help="Search by ALL keywords from keywords.yaml",
    )
    parser.add_argument(
        "--lang", default="ru",
        choices=["ru", "en", "both"],
        help="Language of keywords to use (default: ru)",
    )
    parser.add_argument(
        "--max-pages", type=int, default=5,
        help="Max pages per keyword (default: 5)",
    )
    parser.add_argument(
        "--per-page", type=int, default=DEFAULT_PER_PAGE,
        help="Tenders per page (default: 100)",
    )
    parser.add_argument(
        "--details", action="store_true",
        help="Fetch detail pages (slower, more data)",
    )
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--config-dir", default="config")
    args = parser.parse_args()

    # ── Settings ──────────────────────────────────────────────────
    settings = load_yaml(f"{args.config_dir}/settings.yaml")

    log_cfg = settings.get("logging", {})
    log_file = log_cfg.get("file", "logs/collector_bico.log")
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, log_cfg.get("level", "INFO")),
        format=LOG_FMT,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )

    db_path = settings.get("database", {}).get(
        "path", "data/tenders.db",
    )
    bico_config = f"{args.config_dir}/bicotender.yaml"

    # ── Source ────────────────────────────────────────────────────
    source = BicoTenderSource(bico_config)

    if args.test:
        result = source.test_connection()
        logger.info("Test result: %s", result)
        source.close()
        sys.exit(0 if result.get("ok") else 1)

    # ── Keywords ──────────────────────────────────────────────────
    if args.search:
        keywords = [args.search]
    elif args.all_keywords or args.product:
        keywords = get_keywords_for_search(
            f"{args.config_dir}/keywords.yaml",
            product=args.product,
            lang=args.lang,
        )
        if not keywords:
            logger.error(
                "No keywords found. Check keywords.yaml and --product",
            )
            source.close()
            sys.exit(1)
        logger.info("Loaded %d keywords for search", len(keywords))
    else:
        keywords = get_keywords_for_search(
            f"{args.config_dir}/keywords.yaml",
            lang="ru",
        )
        logger.info("Using ALL %d RU keywords", len(keywords))

    # ── Collect ───────────────────────────────────────────────────
    new_count = 0

    with Database(db_path) as db:
        batch: list[Tender] = []
        batch_size = 100

        for tender in source.collect_by_keywords(
            keywords=keywords,
            max_pages_per_keyword=args.max_pages,
            fetch_details=args.details,
            per_page=args.per_page,
        ):
            batch.append(tender)
            if len(batch) >= batch_size:
                new_count += db.upsert_tenders_batch(batch)
                batch.clear()

        if batch:
            new_count += db.upsert_tenders_batch(batch)

        logger.info(
            "═══ BicoTender done: %d new tenders (DB total: %d) ═══",
            new_count, db.count_raw(),
        )

    source.close()


if __name__ == "__main__":
    main()