#!/usr/bin/env python3
"""
OCDS Tender Collector
=====================
Собирает актуальные тендеры со всех активных OCDS-источников,
нормализует в единую модель и сохраняет в SQLite.

Запуск:
    python -m src.collector                 # все активные источники
    python -m src.collector --source colombia_secop2
    python -m src.collector --max-pages 10  # для отладки
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import yaml

# --- project imports -------------------------------------------------------
from src.ocds_api import OCDSApiClient
from src.database import Database
from src.models import Tender

# --- logging ---------------------------------------------------------------
LOG_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def setup_logging(settings: dict):
    log_cfg = settings.get("logging", {})
    log_file = log_cfg.get("file", "logs/collector.log")
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file, encoding="utf-8"),
    ]
    logging.basicConfig(
        level=getattr(logging, log_cfg.get("level", "INFO")),
        format=LOG_FMT,
        handlers=handlers,
    )


logger = logging.getLogger("collector")


# --- config loaders --------------------------------------------------------
def load_yaml(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_sources(path: str = "config/sources.yaml") -> list[dict]:
    data = load_yaml(path)
    return [s for s in data.get("sources", []) if s.get("active", True)]


def load_settings(path: str = "config/settings.yaml") -> dict:
    return load_yaml(path)


# --- core collector --------------------------------------------------------
class TenderCollector:
    """Orchestrates OCDS data collection from multiple sources."""

    def __init__(
        self,
        db: Database,
        settings: dict,
        sources: list[dict],
    ):
        self.db = db
        self.settings = settings.get("collection", {})
        self.sources = sources
        self.max_pages = self.settings.get("max_pages_per_source", 200)
        self.days_lookback = self.settings.get("days_lookback", 90)
        self.only_active = self.settings.get("only_active_tenders", True)
        self.timeout = self.settings.get("request_timeout_sec", 45)
        self.retries = self.settings.get("max_retries", 3)
        self.delay = self.settings.get("rate_limit_delay_sec", 1.5)

    def _cutoff_date(self) -> str:
        dt = datetime.now(timezone.utc) - timedelta(days=self.days_lookback)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    def collect_source(
        self,
        source: dict,
        max_pages: Optional[int] = None,
    ) -> int:
        """Collect tenders from a single OCDS source. Returns new-tender count."""
        src_id = source["id"]
        country = source.get("country", "??")
        base_url = source["base_url"]
        rel_path = source.get("releases_path", "/releases.json")
        api_type = source.get("api_type", "release_package")

        logger.info(
            "▶ Collecting [%s] %s (%s) …",
            country, source.get("name", src_id), base_url,
        )

        pages = max_pages or self.max_pages
        cutoff = self._cutoff_date()

        client = OCDSApiClient(
            base_url=base_url,
            timeout=self.timeout,
            max_retries=self.retries,
            delay=self.delay,
        )

        new_count = 0
        total_seen = 0
        batch: list[Tender] = []
        batch_size = 500

        try:
            for release in client.iter_releases(
                path=rel_path,
                max_pages=pages,
                tag_filter={"tender", "tenderUpdate", "tenderAmendment"},
            ):
                total_seen += 1

                # фильтр по дате
                pub_date = release.get("date", "")
                if pub_date and pub_date < cutoff:
                    continue

                tender = Tender.from_ocds_release(release, src_id, country)

                # фильтр по статусу
                if self.only_active and tender.status not in (
                    "", "active", "planned", "open",
                ):
                    continue

                batch.append(tender)

                if len(batch) >= batch_size:
                    new_count += self.db.upsert_tenders_batch(batch)
                    batch.clear()

            # flush remaining
            if batch:
                new_count += self.db.upsert_tenders_batch(batch)

        except Exception:
            logger.exception("Error collecting %s", src_id)
        finally:
            client.close()

        # save state
        self.db.save_state(
            source_id=src_id,
            last_collected=datetime.now(timezone.utc).isoformat(),
        )

        logger.info(
            "✔ [%s] done — seen %d releases, stored %d new tenders (total in DB: %d)",
            src_id, total_seen, new_count, self.db.count_raw(),
        )
        return new_count

    def collect_all(self, max_pages: Optional[int] = None) -> int:
        """Collect from all active sources. Returns total new tenders."""
        total = 0
        for source in self.sources:
            total += self.collect_source(source, max_pages)
        return total


# --- discovery (optional): scrape registry ---------------------------------
def discover_sources_from_registry(
    registry_url: str = "https://data.open-contracting.org",
) -> list[dict]:
    """
    Attempt to scrape the OCP Data Registry for OCDS publication metadata.
    Returns list of source dicts compatible with sources.yaml format.

    NOTE: data.open-contracting.org is a catalog of publications, not a
    unified data API.  Each publication links to a publisher's own OCDS
    endpoint.  This function tries to extract those links.
    """
    import requests
    from bs4 import BeautifulSoup

    logger.info("Discovering sources from %s …", registry_url)
    discovered: list[dict] = []

    try:
        resp = requests.get(registry_url, timeout=30, headers={
            "User-Agent": "OCDS-TenderMonitor/1.0"
        })
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.error("Cannot reach registry: %s", exc)
        return discovered

    soup = BeautifulSoup(resp.text, "html.parser")

    # Ищем ссылки на отдельные публикации
    # Типичная структура: <a href="/en/publication/{id}">…</a>
    pub_links = soup.find_all("a", href=True)
    pub_urls = set()
    for a in pub_links:
        href = a["href"]
        if "/publication/" in href:
            full = href if href.startswith("http") else f"{registry_url}{href}"
            pub_urls.add(full)

    logger.info("Found %d publication pages", len(pub_urls))

    for pub_url in list(pub_urls)[:50]:  # ограничиваем для первого прохода
        try:
            r = requests.get(pub_url, timeout=30, headers={
                "User-Agent": "OCDS-TenderMonitor/1.0"
            })
            r.raise_for_status()
            pub_soup = BeautifulSoup(r.text, "html.parser")

            # Пытаемся извлечь URL API / bulk-download
            # Ищем ссылки с ключевыми словами
            for link in pub_soup.find_all("a", href=True):
                href_text = link.get_text(strip=True).lower()
                href_val = link["href"]
                if any(kw in href_text for kw in ("api", "json", "download", "releases")):
                    # Пытаемся определить страну/название из текста страницы
                    title_tag = pub_soup.find("h1")
                    name = title_tag.get_text(strip=True) if title_tag else pub_url
                    discovered.append({
                        "id": f"discovered_{len(discovered)}",
                        "name": name,
                        "country": "??",
                        "base_url": href_val,
                        "releases_path": "",
                        "api_type": "release_package",
                        "active": True,
                        "discovered_from": pub_url,
                    })
                    break

        except Exception as exc:
            logger.debug("Skip %s: %s", pub_url, exc)

    logger.info("Discovered %d potential OCDS API endpoints", len(discovered))
    return discovered


# --- entry point -----------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="OCDS Tender Collector")
    parser.add_argument("--source", type=str, help="Collect only this source_id")
    parser.add_argument("--max-pages", type=int, help="Override max pages per source")
    parser.add_argument("--discover", action="store_true",
                        help="Try to discover sources from data.open-contracting.org")
    parser.add_argument("--config-dir", default="config", help="Config directory")
    args = parser.parse_args()

    settings = load_settings(f"{args.config_dir}/settings.yaml")
    setup_logging(settings)

    sources = load_sources(f"{args.config_dir}/sources.yaml")

    if args.discover:
        extra = discover_sources_from_registry()
        sources.extend(extra)
        logger.info("Total sources after discovery: %d", len(sources))

    if args.source:
        sources = [s for s in sources if s["id"] == args.source]
        if not sources:
            logger.error("Source '%s' not found in config", args.source)
            sys.exit(1)

    db_path = settings.get("database", {}).get("path", "data/tenders.db")

    with Database(db_path) as db:
        collector = TenderCollector(db, settings, sources)
        total_new = collector.collect_all(max_pages=args.max_pages)

    logger.info("═══ Collection complete: %d new tenders ═══", total_new)


if __name__ == "__main__":
    main()