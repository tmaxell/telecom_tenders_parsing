#!/usr/bin/env python3
"""
Диагностика структуры bicotender.ru
====================================
Скачивает страницу каталога и страницу тендера,
выводит реальные CSS-классы, ссылки, структуру.

Запуск:
    python -m src.sources.bicotender_diag
    python -m src.sources.bicotender_diag --url "https://www.bicotender.ru/catalog/?search=SMS"
"""

import argparse
import re
import sys
from collections import Counter
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
}

CATALOG_URL = "https://www.bicotender.ru/catalog/"
DETAIL_URL = (
    "https://www.bicotender.ru/it-kompiutery-sviaz/"
    "uslugi-seo-prodvizeniia-sait-seo-prodvizenie-saita-"
    "respublika-baskortostan-tender326929033.html"
)


def fetch(url: str) -> BeautifulSoup:
    print(f"\n{'='*70}")
    print(f"  FETCHING: {url}")
    print(f"{'='*70}\n")
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.encoding = resp.apparent_encoding or "utf-8"
    print(f"  Status: {resp.status_code}")
    print(f"  Content-Type: {resp.headers.get('content-type','?')}")
    print(f"  Content length: {len(resp.text)} chars")
    return BeautifulSoup(resp.text, "lxml")


def analyze_page(soup: BeautifulSoup, label: str):
    print(f"\n{'─'*70}")
    print(f"  ANALYSIS: {label}")
    print(f"{'─'*70}")

    # 1. Title
    title = soup.title.get_text(strip=True) if soup.title else "N/A"
    print(f"\n  <title>: {title}")

    # 2. All CSS classes — top 40
    class_counter = Counter()
    for tag in soup.find_all(True, class_=True):
        for cls in tag.get("class", []):
            class_counter[cls] += 1

    print(f"\n  TOP-40 CSS classes:")
    for cls, cnt in class_counter.most_common(40):
        print(f"    {cnt:5d}x  .{cls}")

    # 3. All IDs
    ids = [(tag.name, tag.get("id")) for tag in soup.find_all(True, id=True)]
    print(f"\n  Elements with id= ({len(ids)}):")
    for tag_name, id_val in ids[:30]:
        print(f"    <{tag_name} id=\"{id_val}\">")

    # 4. Links with "tender" in href
    tender_links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if re.search(r"tender\d+", href):
            tender_links.append({
                "href": href,
                "text": a.get_text(strip=True)[:80],
                "classes": a.get("class", []),
                "parent_tag": a.parent.name if a.parent else "?",
                "parent_classes": a.parent.get("class", []) if a.parent else [],
            })

    print(f"\n  Links with 'tender\\d+' in href: {len(tender_links)}")
    for i, link in enumerate(tender_links[:15]):
        print(f"    [{i}] href={link['href'][:90]}")
        print(f"        text={link['text'][:70]}")
        print(f"        <a class=\"{' '.join(link['classes'])}\">")
        print(f"        parent: <{link['parent_tag']} "
              f"class=\"{' '.join(link['parent_classes'])}\">")

    # 5. Walk up DOM from first tender link to find card structure
    if tender_links:
        print(f"\n  DOM ancestry of FIRST tender link:")
        first_a = None
        for a in soup.find_all("a", href=True):
            if re.search(r"tender\d+", a["href"]):
                first_a = a
                break
        if first_a:
            node = first_a
            depth = 0
            while node and depth < 10:
                tag_name = getattr(node, "name", "?")
                classes = node.get("class", []) if hasattr(node, "get") else []
                id_val = node.get("id", "") if hasattr(node, "get") else ""
                # Count direct children
                children = list(node.children) if hasattr(node, "children") else []
                child_tags = [c.name for c in children if isinstance(c, Tag)]

                indent = "  " * depth
                desc = f"<{tag_name}"
                if id_val:
                    desc += f' id="{id_val}"'
                if classes:
                    desc += f' class="{" ".join(classes)}"'
                desc += f"> children: {len(child_tags)} {child_tags[:5]}"

                print(f"        {indent}{desc}")
                node = node.parent
                depth += 1

    # 6. Forms (search forms)
    forms = soup.find_all("form")
    print(f"\n  Forms on page: {len(forms)}")
    for i, form in enumerate(forms):
        action = form.get("action", "N/A")
        method = form.get("method", "GET")
        inputs = form.find_all("input")
        print(f"    Form[{i}] action=\"{action}\" method={method}")
        for inp in inputs:
            inp_name = inp.get("name", "?")
            inp_type = inp.get("type", "text")
            inp_val = inp.get("value", "")[:30]
            inp_ph = inp.get("placeholder", "")[:30]
            print(f"      <input name=\"{inp_name}\" type={inp_type} "
                  f"value=\"{inp_val}\" placeholder=\"{inp_ph}\">")

    # 7. Pagination
    print(f"\n  Pagination elements:")
    for sel in ["ul.pagination", "nav.pagination", "div.pagination",
                "div.pager", "div.pages", ".paging",
                "[class*=pagination]", "[class*=paging]", "[class*=page]"]:
        try:
            found = soup.select(sel)
            if found:
                print(f"    Found {len(found)}x with selector: {sel}")
                for el in found[:2]:
                    links = el.find_all("a", href=True)
                    print(f"      Links inside: {len(links)}")
                    for a in links[:5]:
                        print(f"        → {a.get_text(strip=True)} "
                              f"href={a['href'][:60]}")
        except Exception:
            pass

    # Next/prev links
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True).lower()
        href = a["href"]
        if any(kw in text for kw in ("след", "next", "»", "→")):
            print(f"    'Next' link: text=\"{a.get_text(strip=True)}\" "
                  f"href={href[:80]}")
        if re.search(r"[?&]page=\d+", href) or re.search(r"/page/\d+", href):
            print(f"    Page link: text=\"{a.get_text(strip=True)}\" "
                  f"href={href[:80]}")

    # 8. Full text sample (first 2000 chars, cleaned)
    text = soup.get_text(separator="\n", strip=True)
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    print(f"\n  Page text sample (first 60 non-empty lines):")
    for line in lines[:60]:
        print(f"    │ {line[:100]}")


def analyze_detail_page(soup: BeautifulSoup):
    """Анализ страницы конкретного тендера."""
    print(f"\n{'─'*70}")
    print(f"  DETAIL PAGE STRUCTURE")
    print(f"{'─'*70}")

    # Все таблицы
    tables = soup.find_all("table")
    print(f"\n  Tables: {len(tables)}")
    for i, table in enumerate(tables[:5]):
        rows = table.find_all("tr")
        print(f"    Table[{i}]: {len(rows)} rows, "
              f"class={table.get('class', [])}")
        for tr in rows[:8]:
            cells = tr.find_all(["td", "th"])
            cell_texts = [c.get_text(strip=True)[:40] for c in cells]
            print(f"      {cell_texts}")

    # Definition lists (dl/dt/dd)
    dls = soup.find_all("dl")
    print(f"\n  Definition lists (dl): {len(dls)}")
    for i, dl in enumerate(dls[:3]):
        terms = dl.find_all("dt")
        for dt in terms[:10]:
            dd = dt.find_next_sibling("dd")
            dt_text = dt.get_text(strip=True)[:40]
            dd_text = dd.get_text(strip=True)[:60] if dd else "N/A"
            print(f"    {dt_text}: {dd_text}")

    # Key-value pairs in divs
    print(f"\n  Looking for label-value patterns:")
    for tag in soup.find_all(["div", "span", "p", "td", "th", "dt", "li"]):
        text = tag.get_text(strip=True)
        if any(kw in text.lower() for kw in [
            "заказчик", "цена", "нмц", "дата", "номер", "закон",
            "регион", "окпд", "инн", "статус", "площадка",
            "срок", "обеспечение", "контакт",
        ]):
            classes = tag.get("class", [])
            print(f"    <{tag.name} class=\"{' '.join(classes)}\">"
                  f" {text[:90]}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", type=str, help="Custom URL to analyze")
    parser.add_argument("--detail-url", type=str, default=DETAIL_URL)
    parser.add_argument("--catalog-url", type=str, default=CATALOG_URL)
    parser.add_argument("--search", type=str, default="",
                        help="Search query for catalog")
    parser.add_argument("--skip-detail", action="store_true")
    args = parser.parse_args()

    if args.url:
        soup = fetch(args.url)
        analyze_page(soup, "Custom URL")
        return

    # 1. Analyze catalog
    catalog_url = args.catalog_url
    if args.search:
        catalog_url += f"?search={args.search}"
    soup = fetch(catalog_url)
    analyze_page(soup, "CATALOG PAGE")

    # 2. Analyze detail page
    if not args.skip_detail:
        detail_soup = fetch(args.detail_url)
        analyze_page(detail_soup, "DETAIL PAGE")
        analyze_detail_page(detail_soup)


if __name__ == "__main__":
    main()