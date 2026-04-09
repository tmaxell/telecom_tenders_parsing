#!/usr/bin/env python3
"""
BicoTender.ru Scraper v3.3
==========================
★ Строгий поиск: keywordsStrict=1 (вся фраза целиком)
★ items_text заполняется всегда
★ per_page по умолчанию 100

Запуск:
    python -m src.sources.bicotender --search "маркетинговая платформа" --max-pages 5
    python -m src.sources.bicotender --test
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Generator, Optional
from urllib.parse import urljoin, urlencode, quote_plus

import requests
from bs4 import BeautifulSoup, Tag

import yaml

from src.sources.base import BaseTenderSource
from src.models import Tender

logger = logging.getLogger("bicotender")

# ══════════════════════════════════════════════════════════════════
#  Constants
# ══════════════════════════════════════════════════════════════════

BASE_URL = "https://www.bicotender.ru"
SEARCH_PATH = "/tender/search/"
CATALOG_PATH = "/catalog/"
TENDER_LINK_RE = re.compile(r"tender(\d+)\.html")
TENDER_NUMBER_RE = re.compile(r"(?:№\s*|#\s*|Тендер\s*№?\s*)(\d{6,})")
PRICE_RE = re.compile(r"([\d\s]+[.,]\d{2})")
DATE_RE = re.compile(r"(\d{2})[./](\d{2})[./](\d{4})")
DEFAULT_PER_PAGE = 100

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "keep-alive",
    "Referer": "https://www.bicotender.ru/",
}


# ══════════════════════════════════════════════════════════════════
#  Утилиты
# ══════════════════════════════════════════════════════════════════

def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_price(text: str) -> Optional[float]:
    if not text:
        return None
    text = text.replace("\xa0", " ").replace("\u00a0", " ")
    m = PRICE_RE.search(text)
    if not m:
        m2 = re.search(r"([\d\s]{4,})", text)
        if m2:
            num = m2.group(1).replace(" ", "")
            try:
                v = float(num)
                return v if v > 0 else None
            except ValueError:
                return None
        return None
    num = m.group(1).replace(" ", "").replace(",", ".")
    try:
        v = float(num)
        return v if v > 0 else None
    except ValueError:
        return None


def parse_date(text: str) -> Optional[str]:
    if not text:
        return None
    m = DATE_RE.search(text)
    if m:
        try:
            dt = datetime.strptime(m.group(0), "%d.%m.%Y")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def detect_currency(text: str) -> str:
    t = text.upper()
    if "BYN" in t:
        return "BYN"
    if "USD" in t or "$" in text:
        return "USD"
    if "EUR" in t or "€" in text:
        return "EUR"
    if "KZT" in t or "тенге" in text.lower():
        return "KZT"
    if "UZS" in t or "сум" in text.lower():
        return "UZS"
    return "RUB"


def detect_law(text: str) -> str:
    t = text.lower()
    if "44-фз" in t:
        return "44-ФЗ"
    if "223-фз" in t:
        return "223-ФЗ"
    if "коммерч" in t:
        return "Коммерческая"
    if "запрос предложений" in t:
        return "Запрос предложений"
    if "аукцион" in t:
        return "Аукцион"
    if "конкурс" in t:
        return "Конкурс"
    return ""


# ══════════════════════════════════════════════════════════════════
#  Листинг
# ══════════════════════════════════════════════════════════════════

@dataclass
class ListingItem:
    tender_id: str = ""
    url: str = ""
    title: str = ""
    number: str = ""
    tender_type: str = ""
    price: Optional[float] = None
    price_text: str = ""
    currency: str = "RUB"
    date_text: str = ""
    pub_date: Optional[str] = None
    deadline: Optional[str] = None
    full_card_text: str = ""


def parse_listing_page(soup: BeautifulSoup) -> list[ListingItem]:
    items: list[ListingItem] = []
    seen_ids: set[str] = set()

    tender_anchors = []
    for a in soup.find_all("a", href=True):
        m = TENDER_LINK_RE.search(a["href"])
        if m:
            tender_anchors.append((a, m.group(1)))

    if not tender_anchors:
        logger.warning("No tender links found on listing page")
        return items

    logger.debug("Found %d tender links", len(tender_anchors))

    for a_tag, tender_id in tender_anchors:
        if tender_id in seen_ids:
            continue
        seen_ids.add(tender_id)

        href = a_tag["href"]
        full_url = (
            href if href.startswith("http") else urljoin(BASE_URL, href)
        )
        link_text = clean(a_tag.get_text())

        card_el = _find_card_container(a_tag)
        card_text = clean(card_el.get_text()) if card_el else link_text

        if _is_junk(card_text):
            continue

        item = ListingItem(
            tender_id=tender_id,
            url=full_url,
            title=link_text,
            full_card_text=card_text,
        )

        _extract_listing_fields(card_text, item)

        if not item.number:
            item.number = tender_id

        items.append(item)

    logger.info("Parsed %d tenders from listing page", len(items))
    return items


def _find_card_container(a_tag: Tag) -> Optional[Tag]:
    skip_tags = {"a", "span", "em", "strong", "b", "i", "small", "font"}
    card_tags = {"div", "li", "tr", "td", "article", "section", "p"}

    node = a_tag.parent
    best = None

    for _ in range(8):
        if node is None:
            break
        tag_name = getattr(node, "name", None)
        if tag_name in (
            "body", "html", "main", "table", "tbody",
            "header", "footer", "nav",
        ):
            break
        if tag_name in skip_tags:
            node = node.parent
            continue
        if tag_name in card_tags:
            inner = [
                a for a in node.find_all("a", href=True)
                if TENDER_LINK_RE.search(a["href"])
            ]
            if len(inner) <= 2:
                best = node
            else:
                break
        node = node.parent

    return best


def _is_junk(text: str) -> bool:
    junk_markers = [
        "Найдено тендеров",
        "И еще в Архиве найдено",
        "Как работать в системе",
        "Подключить тест",
        "Задать вопрос эксперту",
        "Подобрать тариф",
    ]
    return any(m in text for m in junk_markers)


def _extract_listing_fields(text: str, item: ListingItem):
    m = TENDER_NUMBER_RE.search(text)
    if m:
        item.number = m.group(1)

    item.tender_type = detect_law(text)

    price_patterns = [
        re.compile(
            r"([\d\s]+[.,]\d{2})\s*(руб|RUB|₽|BYN|USD|EUR|KZT)",
            re.IGNORECASE,
        ),
        re.compile(r"Цена[:\s]*([\d\s]+[.,]?\d*)", re.IGNORECASE),
    ]
    for pp in price_patterns:
        pm = pp.search(text)
        if pm:
            item.price = parse_price(pm.group(1))
            item.price_text = pm.group(0)
            break

    item.currency = detect_currency(text)

    dates = DATE_RE.findall(text)
    for d, m_val, y in dates:
        try:
            dt = datetime.strptime(f"{d}.{m_val}.{y}", "%d.%m.%Y")
            iso = dt.strftime("%Y-%m-%d")
            if not item.pub_date:
                item.pub_date = iso
            elif not item.deadline and iso != item.pub_date:
                item.deadline = iso
        except ValueError:
            pass

    item.date_text = text


# ══════════════════════════════════════════════════════════════════
#  Детальная страница
# ══════════════════════════════════════════════════════════════════

@dataclass
class DetailData:
    title: str = ""
    description: str = ""
    number: str = ""
    customer: str = ""
    price: Optional[float] = None
    price_text: str = ""
    currency: str = "RUB"
    region: str = ""
    delivery_region: str = ""
    platform: str = ""
    pub_date: Optional[str] = None
    deadline: Optional[str] = None
    law: str = ""
    status: str = ""
    okpd2: str = ""
    inn: str = ""
    contact_name: str = ""
    contact_phone: str = ""
    contact_email: str = ""
    lots: list = field(default_factory=list)
    documents: list = field(default_factory=list)
    full_text: str = ""


def parse_detail_page(soup: BeautifulSoup) -> DetailData:
    data = DetailData()

    h1 = soup.find("h1")
    if h1:
        data.title = clean(h1.get_text())
    else:
        content = soup.select_one("div.content")
        if content:
            for tag in content.find_all(["h1", "h2", "h3"]):
                t = clean(tag.get_text())
                if "тендер" in t.lower() and len(t) > 10:
                    data.title = t
                    break

    if data.title:
        m = TENDER_NUMBER_RE.search(data.title)
        if m:
            data.number = m.group(1)

    tab1 = soup.select_one("div.tabs-content-item.tabs-1, div.tabs-1")
    if tab1:
        _parse_description_tab(tab1, data)

    tab2 = soup.select_one("div.tabs-content-item.tabs-2, div.tabs-2")
    if tab2:
        _parse_customer_tab(tab2, data)

    tab3 = soup.select_one("div.tabs-content-item.tabs-3, div.tabs-3")
    if tab3:
        _parse_lots_tab(tab3, data)

    tender_card = soup.select_one("div.tend-card, div.tender-desc")
    if tender_card:
        _parse_table_rows(tender_card, data)

    if not data.region and not data.platform:
        _parse_table_rows(soup, data)

    if not data.contact_email:
        for a in soup.find_all("a", href=True):
            if a["href"].startswith("mailto:"):
                data.contact_email = (
                    a["href"].replace("mailto:", "").strip()
                )
                break

    if not data.contact_phone:
        for a in soup.find_all("a", href=True):
            if a["href"].startswith("tel:"):
                data.contact_phone = (
                    a["href"].replace("tel:", "").strip()
                )
                break

    if not data.law:
        data.law = detect_law(soup.get_text())

    data.full_text = soup.get_text(separator=" ", strip=True)[:5000]

    return data


def _parse_description_tab(tab: Tag, data: DetailData):
    text = clean(tab.get_text())
    if text.startswith("Описание тендера:"):
        text = text[len("Описание тендера:"):].strip()

    desc_parts = []
    for child in tab.children:
        if isinstance(child, Tag):
            if child.name == "table":
                break
            t = clean(child.get_text())
            if t and len(t) > 5 and not _is_junk(t):
                desc_parts.append(t)
        elif isinstance(child, str):
            t = child.strip()
            if t:
                desc_parts.append(t)

    if desc_parts:
        data.description = " ".join(desc_parts)

    _parse_table_rows(tab, data)


def _parse_customer_tab(tab: Tag, data: DetailData):
    text = clean(tab.get_text())
    if "зарегистрируйтесь" in text.lower():
        logger.debug("Customer tab is behind paywall")
        return
    _parse_table_rows(tab, data)


def _parse_lots_tab(tab: Tag, data: DetailData):
    text = clean(tab.get_text())
    lots = []
    lot_blocks = re.split(r"Лот\s*\d+", text)

    for block in lot_blocks:
        block = block.strip()
        if not block:
            continue

        lot = {}

        m = re.search(
            r"Предмет\s*(?:контракта|закупки)\s*:\s*(.+?)"
            r"(?:Цена|Количество|ОКПД|$)",
            block, re.IGNORECASE,
        )
        if m:
            lot["subject"] = clean(m.group(1))

        m = re.search(
            r"Цена\s*(?:контракта|лота)?\s*:\s*([\d\s,.]+\s*\w+)",
            block, re.IGNORECASE,
        )
        if m:
            lot["price_text"] = clean(m.group(1))
            price_val = parse_price(m.group(1))
            if price_val and price_val > 0:
                if data.price is None:
                    data.price = price_val
                    data.price_text = lot["price_text"]
                    data.currency = detect_currency(m.group(1))

        m = re.search(r"ОКПД[\s2-]*:\s*([\d.]+)", block)
        if m:
            lot["okpd2"] = m.group(1)
            if not data.okpd2:
                data.okpd2 = m.group(1)

        if lot:
            lots.append(lot)

    data.lots = lots


def _parse_table_rows(parent: Tag, data: DetailData):
    for table in parent.find_all("table"):
        for tr in table.find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) >= 2:
                label = clean(cells[0].get_text()).rstrip(":").lower()
                value = clean(cells[1].get_text())
                if not value or len(value) < 1:
                    continue
                _assign_field(label, value, data)
            elif len(cells) == 1:
                text = clean(cells[0].get_text())
                m = re.match(r"^(.{3,30}):\s*(.+)$", text)
                if m:
                    _assign_field(m.group(1).lower(), m.group(2), data)

    for dl in parent.find_all("dl"):
        for dt in dl.find_all("dt"):
            dd = dt.find_next_sibling("dd")
            if dd:
                label = clean(dt.get_text()).rstrip(":").lower()
                value = clean(dd.get_text())
                _assign_field(label, value, data)


def _assign_field(label: str, value: str, data: DetailData):
    label = label.strip().lower()

    if any(m in label for m in [
        "регион", "место поставки", "место выполнения", "субъект",
    ]):
        if "поставк" in label:
            data.delivery_region = value[:100]
        elif not data.region:
            data.region = value[:100]
    elif any(m in label for m in [
        "площадк", "торговая площадка", "источник", "платформа",
    ]):
        data.platform = value[:100]
    elif any(m in label for m in [
        "заказчик", "организация", "покупатель", "учреждение",
    ]):
        if not data.customer:
            data.customer = value[:200]
    elif any(m in label for m in [
        "цена", "нмц", "нмцк", "стоимость", "начальная",
    ]):
        if data.price is None:
            data.price = parse_price(value)
            data.price_text = value
    elif any(m in label for m in [
        "реестровый номер", "номер закупки",
        "номер извещения", "номер тендера",
    ]):
        m_num = re.search(r"\d{6,}", value)
        data.number = m_num.group(0) if m_num else value[:30]
    elif any(m in label for m in [
        "дата публикации", "размещено",
        "опубликовано", "дата размещения",
    ]):
        data.pub_date = parse_date(value)
    elif any(m in label for m in [
        "окончание подачи", "дата окончания",
        "срок подачи", "подача заявок",
    ]):
        data.deadline = parse_date(value)
    elif "статус" in label:
        data.status = value[:50]
    elif any(m in label for m in ["закон", "тип закупки", "способ"]):
        data.law = value[:50]
    elif "окпд" in label:
        data.okpd2 = value[:100]
    elif "инн" in label:
        m_inn = re.search(r"\d{10,12}", value)
        if m_inn:
            data.inn = m_inn.group(0)
    elif any(m in label for m in ["контакт", "ответственн"]):
        data.contact_name = value[:100]
    elif any(m in label for m in ["телефон", "тел"]):
        data.contact_phone = value[:30]
    elif any(m in label for m in ["email", "почта", "e-mail"]):
        data.contact_email = value[:60]


# ══════════════════════════════════════════════════════════════════
#  Пагинация
# ══════════════════════════════════════════════════════════════════

def detect_pagination(soup: BeautifulSoup) -> dict:
    info = {"has_next": False, "max_page": 1, "current": 1}
    page_numbers = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)

        m = re.search(r"[?&]page=(\d+)", href)
        if m:
            page_numbers.append(int(m.group(1)))

        if text.isdigit():
            page_numbers.append(int(text))

        if any(x in text.lower() for x in ("след", "next", "»", "›")):
            info["has_next"] = True

    if page_numbers:
        info["max_page"] = max(page_numbers)
        info["has_next"] = True

    active = soup.select_one(
        ".pagination .active, .pager .active, .pages .active, "
        "span.current, b.current"
    )
    if active:
        t = active.get_text(strip=True)
        if t.isdigit():
            info["current"] = int(t)

    return info


# ══════════════════════════════════════════════════════════════════
#  Основной класс
# ══════════════════════════════════════════════════════════════════

class BicoTenderSource(BaseTenderSource):

    source_id = "bicotender"
    source_name = "BicoTender.ru"
    country = "RU"

    def __init__(
        self,
        config_path: str = "config/bicotender.yaml",
        delay: float = 2.0,
        page_delay: float = 3.5,
        max_retries: int = 3,
        timeout: int = 30,
    ):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                self.config = yaml.safe_load(f) or {}
        except FileNotFoundError:
            self.config = {}

        req = self.config.get("request", {})
        self.delay = req.get("delay_between_requests", delay)
        self.page_delay = req.get("delay_between_pages", page_delay)
        self.max_retries = req.get("max_retries", max_retries)
        self.timeout = req.get("timeout", timeout)
        self.max_pages_default = req.get("max_pages", 50)

        self.session = requests.Session()
        self.session.headers.update(
            self.config.get("headers", HEADERS)
        )

        self._stats = {
            "pages": 0, "cards": 0, "details": 0, "errors": 0,
        }

    def _get_soup(self, url: str) -> Optional[BeautifulSoup]:
        for attempt in range(1, self.max_retries + 1):
            try:
                logger.debug("GET %s (attempt %d)", url[:120], attempt)
                resp = self.session.get(url, timeout=self.timeout)

                if resp.status_code == 403:
                    logger.error("403 Forbidden")
                    return None
                if resp.status_code == 429:
                    wait = 30 * attempt
                    logger.warning("429 Rate limit. Sleep %ds", wait)
                    time.sleep(wait)
                    continue
                if resp.status_code >= 500:
                    wait = 5 * attempt
                    logger.warning(
                        "Server %d. Retry %ds", resp.status_code, wait,
                    )
                    time.sleep(wait)
                    continue
                if resp.status_code == 404:
                    logger.warning("404: %s", url[:80])
                    return None

                resp.raise_for_status()
                resp.encoding = resp.apparent_encoding or "utf-8"
                soup = BeautifulSoup(resp.text, "lxml")

                title_text = (
                    soup.title.get_text(strip=True).lower()
                    if soup.title else ""
                )
                if any(
                    x in title_text
                    for x in ("captcha", "капча", "blocked")
                ):
                    logger.error("CAPTCHA detected")
                    return None

                time.sleep(self.delay)
                return soup

            except requests.RequestException as exc:
                wait = 5 * attempt
                logger.warning("Error (attempt %d): %s", attempt, exc)
                time.sleep(wait)

        logger.error(
            "Failed after %d retries: %s",
            self.max_retries, url[:80],
        )
        self._stats["errors"] += 1
        return None

    # ══════════════════════════════════════════════════════════════
    #  ★ ГЛАВНОЕ ИЗМЕНЕНИЕ: URL поиска с keywordsStrict=1
    # ══════════════════════════════════════════════════════════════

    @staticmethod
    def _search_url(
        query: str,
        page: int = 1,
        per_page: int = DEFAULT_PER_PAGE,
        strict: bool = True,
    ) -> str:
        """
        Построить URL поиска с СТРОГИМ соответствием фразы.

        Реальный URL:
        /tender/search/?keywords=маркетинговая+платформа
            &no_search_by_positions=0
            &keywordsStrict=1              ← строгий поиск
            &nokeywords=
            &no_exclude_by_positions=0
            &multifields=0
            &company[name]=
            &company[excludeName]=0
            &company[keywordsStrict]=0
            &company[inn]=
            &costRub[from]=
            &costRub[to]=
            &costRub[withZero]=0
            &prepaymentPercent[from]=
            &prepaymentPercent[to]=
            &loadTime[from]=
            &loadTime[to]=
            &finishDate[from]=
            &finishDate[to]=
            &sourceUrl=
            &excludeSourceUrl=0
            &tender_id=
            &srcNoticeNumber=
            &show_expiration=0
            &on_page=100
            &order=bcHitCountUniq+DESC
            &searchInFound=0
            &searchInFoundKey=
            &submit=Искать
            &page=2
        """
        params = {
            "keywords": query,
            "no_search_by_positions": "0",
            "keywordsStrict": "1" if strict else "0",
            "nokeywords": "",
            "no_exclude_by_positions": "0",
            "multifields": "0",
            "company[name]": "",
            "company[excludeName]": "0",
            "company[keywordsStrict]": "0",
            "company[inn]": "",
            "costRub[from]": "",
            "costRub[to]": "",
            "costRub[withZero]": "0",
            "prepaymentPercent[from]": "",
            "prepaymentPercent[to]": "",
            "loadTime[from]": "",
            "loadTime[to]": "",
            "finishDate[from]": "",
            "finishDate[to]": "",
            "sourceUrl": "",
            "excludeSourceUrl": "0",
            "tender_id": "",
            "srcNoticeNumber": "",
            "show_expiration": "0",
            "on_page": str(per_page),
            "order": "bcHitCountUniq DESC",
            "searchInFound": "0",
            "searchInFoundKey": "",
            "submit": "Искать",
        }
        if page > 1:
            params["page"] = str(page)

        return f"{BASE_URL}{SEARCH_PATH}?{urlencode(params)}"

    @staticmethod
    def _catalog_url(page: int = 1) -> str:
        if page > 1:
            return f"{BASE_URL}{CATALOG_PATH}?page={page}"
        return f"{BASE_URL}{CATALOG_PATH}"

    # ── Collect ───────────────────────────────────────────────────

    def collect(
        self,
        search_query: str = "",
        max_pages: Optional[int] = None,
        fetch_details: bool = False,
        per_page: int = DEFAULT_PER_PAGE,
        strict_search: bool = True,
        **kwargs,
    ) -> Generator[Tender, None, None]:
        """
        Основной генератор.

        Args:
            search_query: поисковый запрос
            max_pages: макс. страниц
            fetch_details: загружать детальные страницы
            per_page: тендеров на странице
            strict_search: строгий поиск (keywordsStrict=1)
        """
        pages_limit = max_pages or self.max_pages_default
        self._stats = {
            "pages": 0, "cards": 0, "details": 0, "errors": 0,
        }

        logger.info(
            "▶ BicoTender: query='%s' max_pages=%d details=%s "
            "per_page=%d strict=%s",
            search_query, pages_limit, fetch_details,
            per_page, strict_search,
        )

        for page_num in range(1, pages_limit + 1):
            if search_query:
                url = self._search_url(
                    search_query, page_num, per_page, strict_search,
                )
            else:
                url = self._catalog_url(page_num)

            logger.info("  📄 Page %d: %s", page_num, url[:150])

            soup = self._get_soup(url)
            if not soup:
                break

            items = parse_listing_page(soup)
            if not items:
                logger.info(
                    "  Page %d: 0 tenders — stopping", page_num,
                )
                break

            self._stats["pages"] += 1
            logger.info("  Page %d: %d tenders", page_num, len(items))

            for item in items:
                self._stats["cards"] += 1

                detail = None
                if fetch_details and item.url:
                    detail = self._fetch_detail(item.url)

                tender = self._to_tender(item, detail)
                yield tender

            pag = detect_pagination(soup)
            if not pag["has_next"] or page_num >= pag["max_page"]:
                logger.info("  Last page (max=%d)", pag["max_page"])
                break

            time.sleep(self.page_delay)

        logger.info(
            "✔ BicoTender: %d pages, %d cards, %d details, %d errors",
            self._stats["pages"], self._stats["cards"],
            self._stats["details"], self._stats["errors"],
        )

    def _fetch_detail(self, url: str) -> Optional[DetailData]:
        soup = self._get_soup(url)
        if not soup:
            return None
        self._stats["details"] += 1
        return parse_detail_page(soup)

    # ── Convert to Tender ─────────────────────────────────────────

    def _to_tender(
        self,
        item: ListingItem,
        detail: Optional[DetailData] = None,
    ) -> Tender:
        ocid = f"bicotender-{item.tender_id}"

        title = (
            (detail.title if detail and detail.title else item.title)
            or ""
        )
        description = (detail.description if detail else "") or ""
        customer = (detail.customer if detail else "") or ""
        price = (
            detail.price
            if detail and detail.price is not None
            else item.price
        )
        currency = (
            detail.currency
            if detail and detail.price is not None
            else item.currency
        ) or "RUB"
        region = (detail.region if detail else "") or ""
        platform = (detail.platform if detail else "") or ""
        law = (
            detail.law if detail and detail.law else item.tender_type
        ) or ""
        pub_date = (
            detail.pub_date
            if detail and detail.pub_date
            else item.pub_date
        )
        deadline = (
            detail.deadline
            if detail and detail.deadline
            else item.deadline
        )
        number = (
            detail.number
            if detail and detail.number
            else item.number
        ) or item.tender_id
        okpd2 = (detail.okpd2 if detail else "") or ""
        inn = (detail.inn if detail else "") or ""
        status = (
            detail.status if detail and detail.status else "active"
        )

        # ── items_text — всегда заполняем ────────────────────────
        items_parts: list[str] = []

        if title:
            subject = re.sub(
                r"^(?:Тендер\s*[-–—]\s*)?", "", title,
            ).strip()
            subject = re.sub(r"\s*№\d+\s*$", "", subject).strip()
            if subject:
                items_parts.append(f"Предмет: {subject}")

        if number:
            items_parts.append(f"№ {number}")

        if law:
            items_parts.append(f"Тип: {law}")

        if item.url:
            items_parts.append(f"URL: {item.url}")

        if okpd2:
            items_parts.append(f"ОКПД2: {okpd2}")
        if region:
            items_parts.append(f"Регион: {region}")
        if platform:
            items_parts.append(f"Площадка: {platform}")

        if detail and detail.lots:
            for i, lot in enumerate(detail.lots, 1):
                subj = lot.get("subject", "")
                if subj:
                    items_parts.append(f"Лот {i}: {subj}")

        raw = {
            "tender_id": item.tender_id,
            "number": number,
            "url": item.url,
            "price_text": (
                detail.price_text if detail else item.price_text
            ),
            "region": region,
            "delivery_region": (
                detail.delivery_region if detail else ""
            ),
            "platform": platform,
            "okpd2": okpd2,
            "inn": inn,
            "contact_name": detail.contact_name if detail else "",
            "contact_phone": detail.contact_phone if detail else "",
            "contact_email": detail.contact_email if detail else "",
            "lots": detail.lots if detail else [],
        }

        return Tender(
            ocid=ocid,
            release_id=(
                f"{ocid}-{datetime.utcnow().strftime('%Y%m%d')}"
            ),
            source_id=self.source_id,
            country=self.country,
            title=title,
            description=description,
            status=status,
            procurement_method=law,
            value_amount=price,
            value_currency=currency,
            buyer_name=customer,
            buyer_id=inn,
            tender_period_start=pub_date,
            tender_period_end=deadline,
            date_published=pub_date,
            items_text=" | ".join(items_parts),
            raw_json=json.dumps(raw, ensure_ascii=False, default=str),
        )

    # ── Multi-keyword ─────────────────────────────────────────────

    def collect_by_keywords(
        self,
        keywords: list[str],
        max_pages_per_keyword: int = 5,
        fetch_details: bool = False,
        per_page: int = DEFAULT_PER_PAGE,
        strict_search: bool = True,
    ) -> Generator[Tender, None, None]:
        """Поиск по нескольким ключевым словам с дедупликацией."""
        seen: set[str] = set()
        total = 0

        for i, kw in enumerate(keywords, 1):
            logger.info(
                "━━━ Keyword %d/%d: '%s' ━━━",
                i, len(keywords), kw,
            )
            for tender in self.collect(
                search_query=kw,
                max_pages=max_pages_per_keyword,
                fetch_details=fetch_details,
                per_page=per_page,
                strict_search=strict_search,
            ):
                if tender.ocid not in seen:
                    seen.add(tender.ocid)
                    total += 1
                    yield tender

            time.sleep(self.page_delay * 2)

        logger.info("═══ Keywords done: %d unique tenders ═══", total)

    def test_connection(self) -> dict:
        result = {
            "ok": False,
            "catalog_tenders": 0,
            "search_tenders": 0,
            "detail_parsed": False,
            "error": "",
        }
        try:
            soup = self._get_soup(f"{BASE_URL}{CATALOG_PATH}")
            if soup:
                items = parse_listing_page(soup)
                result["catalog_tenders"] = len(items)

            search_soup = self._get_soup(
                self._search_url("тендер", per_page=10)
            )
            if search_soup:
                search_items = parse_listing_page(search_soup)
                result["search_tenders"] = len(search_items)

            if soup:
                items = parse_listing_page(soup)
                if items and items[0].url:
                    detail_soup = self._get_soup(items[0].url)
                    if detail_soup:
                        detail = parse_detail_page(detail_soup)
                        result["detail_parsed"] = bool(
                            detail.title or detail.description
                        )

            result["ok"] = (
                result["catalog_tenders"] > 0
                or result["search_tenders"] > 0
            )
        except Exception as exc:
            result["error"] = str(exc)
        return result

    def close(self):
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="BicoTender.ru v3.3 — строгий поиск",
    )
    parser.add_argument("--search", "-s", type=str, default="")
    parser.add_argument("--max-pages", "-p", type=int, default=5)
    parser.add_argument(
        "--per-page", type=int, default=DEFAULT_PER_PAGE,
    )
    parser.add_argument("--details", "-d", action="store_true")
    parser.add_argument("--test", "-t", action="store_true")
    parser.add_argument("--dump-html", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--no-strict", action="store_true",
        help="Отключить строгий поиск (keywordsStrict=0)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    source = BicoTenderSource()

    if args.test:
        res = source.test_connection()
        print(json.dumps(res, indent=2, ensure_ascii=False))
        source.close()
        sys.exit(0 if res["ok"] else 1)

    if args.dump_html:
        if args.search:
            url = source._search_url(
                args.search,
                per_page=args.per_page,
                strict=not args.no_strict,
            )
        else:
            url = source._catalog_url()
        soup = source._get_soup(url)
        if soup:
            items = parse_listing_page(soup)
            print(f"\nURL: {url}\nFound: {len(items)} tenders")
            for i, item in enumerate(items[:20]):
                print(
                    f"\n  [{i+1}] {item.tender_id}: "
                    f"{item.title[:70]}"
                )
                print(
                    f"       Price: {item.price} {item.currency}"
                )
        source.close()
        sys.exit(0)

    count = 0
    for tender in source.collect(
        search_query=args.search,
        max_pages=args.max_pages,
        fetch_details=args.details,
        per_page=args.per_page,
        strict_search=not args.no_strict,
    ):
        count += 1
        print(f"\n{'─'*60}")
        print(f"  #{count} [{tender.ocid}]")
        print(f"  Title    : {tender.title[:80]}")
        print(f"  Customer : {tender.buyer_name[:60]}")
        print(
            f"  Price    : {tender.value_amount} "
            f"{tender.value_currency}"
        )
        print(f"  Items    : {tender.items_text[:120]}")

    print(f"\n{'═'*60}\nTotal: {count} tenders")
    source.close()