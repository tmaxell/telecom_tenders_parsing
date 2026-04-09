#!/usr/bin/env python3
"""
BicoTender Collector v3
=======================
★ Добавлен постпроцессинг: --verify-keywords
★ Режимы проверки: --verify-mode exact|fuzzy|token

python -m src.collector_bico --search "маркетинговая платформа" \\
    --per-page 100 --max-pages 5 --verify-keywords

python -m src.collector_bico --all-keywords --verify-keywords \\
    --verify-mode fuzzy --max-pages 3
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
from src.keyword_verifier import KeywordVerifier

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
        description="BicoTender Keyword Collector v3 — с верификацией",
    )
    parser.add_argument("--search", type=str)
    parser.add_argument("--product", type=str)
    parser.add_argument("--all-keywords", action="store_true")
    parser.add_argument(
        "--lang", default="ru", choices=["ru", "en", "both"],
    )
    parser.add_argument("--max-pages", type=int, default=5)
    parser.add_argument(
        "--per-page", type=int, default=DEFAULT_PER_PAGE,
    )
    parser.add_argument("--details", action="store_true")
    parser.add_argument(
        "--strict", action="store_true", default=True,
        help="Строгий поиск на сайте (keywordsStrict=1)",
    )
    parser.add_argument(
        "--no-strict", dest="strict", action="store_false",
    )

    # ★ Новые флаги для постпроцессинга
    parser.add_argument(
        "--verify-keywords", action="store_true",
        help="Проверять вхождение ключевых слов в текст тендера",
    )
    parser.add_argument(
        "--verify-mode",
        default="fuzzy",
        choices=["exact", "fuzzy", "token", "regex"],
        help="Режим проверки: exact|fuzzy|token|regex (default: fuzzy)",
    )

    parser.add_argument("--test", action="store_true")
    parser.add_argument("--config-dir", default="config")
    args = parser.parse_args()

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

    # ★ Инициализируем верификатор, если включён
    verifier = None
    if args.verify_keywords:
        verifier = KeywordVerifier(
            mode=args.verify_mode,
            case_sensitive=False,
            min_keyword_length=3,
        )
        logger.info(
            "Keyword verification enabled (mode: %s)",
            args.verify_mode,
        )

    # ── Collect ───────────────────────────────────────────────────
    new_count = 0
    verified_count = 0
    rejected_count = 0

    with Database(db_path) as db:
        batch: list[Tender] = []
        batch_size = 100

        for tender in source.collect_by_keywords(
            keywords=keywords,
            max_pages_per_keyword=args.max_pages,
            fetch_details=args.details,
            per_page=args.per_page,
            strict_search=args.strict,
        ):
            # ★ Постпроцессинг: проверяем вхождение ключевых слов
            if verifier:
                # Собираем текст для проверки
                searchable_text = " ".join([
                    tender.title,
                    tender.description,
                    tender.items_text,
                    tender.buyer_name,
                ]).strip()

                if not searchable_text:
                    logger.debug(
                        "Tender %s has no text, skipping verification",
                        tender.ocid,
                    )
                    # Сохраняем всё равно, если текста нет
                else:
                    # Проверяем по всем ключевым словам
                    matched_any, results = verifier.verify_any(
                        searchable_text, keywords,
                    )

                    if not matched_any:
                        # Отклоняем тендер
                        rejected_count += 1
                        logger.debug(
                            "Rejected %s: no keywords found in text",
                            tender.ocid,
                        )
                        continue

                    verified_count += 1

                    # Логируем какие ключевики нашлись
                    matched_kws = [
                        r.keyword for r in results if r.matched
                    ]
                    logger.debug(
                        "Verified %s: matched %s",
                        tender.ocid,
                        matched_kws[:3],
                    )

            # Сохраняем
            batch.append(tender)
            if len(batch) >= batch_size:
                new_count += db.upsert_tenders_batch(batch)
                batch.clear()

        if batch:
            new_count += db.upsert_tenders_batch(batch)

        logger.info(
            "═══ BicoTender done ═══\n"
            "  New tenders saved  : %d\n"
            "  Verified (passed)  : %d\n"
            "  Rejected (no match): %d\n"
            "  DB total           : %d",
            new_count, verified_count, rejected_count, db.count_raw(),
        )

    source.close()


if __name__ == "__main__":
    main()