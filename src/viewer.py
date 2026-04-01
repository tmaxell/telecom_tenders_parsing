#!/usr/bin/env python3
"""
Tender Viewer & Exporter v2
============================
★ По умолчанию экспортирует только НЕ экспортированные ранее строки.
★ Помечает экспортированные в БД, чтобы не дублировать.
★ --include-exported — если нужно выгрузить всё.
★ --reset-exported — сбросить все метки (повторный экспорт всего).

python -m src.viewer --export excel
python -m src.viewer --export excel --include-exported
python -m src.viewer --stats
python -m src.viewer -i
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from src.database import Database

logger = logging.getLogger("viewer")


# ══════════════════════════════════════════════════════════════════
#  Reader
# ══════════════════════════════════════════════════════════════════

class TenderReader:

    def __init__(self, db_path: str = "data/tenders.db"):
        if not Path(db_path).exists():
            print(f"❌ БД не найдена: {db_path}")
            print("   Сначала: python -m src.collector_bico --search IMEI")
            sys.exit(1)
        self.db = Database(db_path)

    def get_last(self, n: int = 20, source: str = None) -> list[dict]:
        where = "WHERE source_id = ?" if source else ""
        params = (source,) if source else ()
        sql = f"""
            SELECT ocid, source_id, country, title, buyer_name,
                   value_amount, value_currency, status,
                   procurement_method, date_published,
                   tender_period_end, items_text,
                   collected_at, exported_at
            FROM raw_tenders {where}
            ORDER BY collected_at DESC LIMIT ?
        """
        rows = self.db.conn.execute(sql, (*params, n)).fetchall()
        return [dict(r) for r in rows]

    def search(self, text: str, limit: int = 50) -> list[dict]:
        pattern = f"%{text}%"
        sql = """
            SELECT ocid, source_id, country, title, description,
                   buyer_name, value_amount, value_currency,
                   date_published, tender_period_end, status,
                   items_text, exported_at
            FROM raw_tenders
            WHERE title LIKE ? OR description LIKE ?
                  OR items_text LIKE ? OR buyer_name LIKE ?
            ORDER BY date_published DESC LIMIT ?
        """
        rows = self.db.conn.execute(
            sql, (pattern, pattern, pattern, pattern, limit)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_matched(
        self, product: str = None, limit: int = 500,
    ) -> list[dict]:
        if product:
            sql = """
                SELECT * FROM matched_tenders
                WHERE matched_product = ?
                ORDER BY date_published DESC LIMIT ?
            """
            rows = self.db.conn.execute(sql, (product, limit)).fetchall()
        else:
            sql = """
                SELECT * FROM matched_tenders
                ORDER BY date_published DESC LIMIT ?
            """
            rows = self.db.conn.execute(sql, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        self.db.close()


# ══════════════════════════════════════════════════════════════════
#  Console table
# ══════════════════════════════════════════════════════════════════

def print_table(rows: list[dict], max_col_width: int = 40):
    if not rows:
        print("  (нет данных)")
        return

    skip = {"raw_json", "full_text", "description"}
    columns = [k for k in rows[0].keys() if k not in skip]

    widths = {}
    for col in columns:
        values = [str(row.get(col, ""))[:max_col_width] for row in rows]
        widths[col] = min(
            max(len(col), max(len(v) for v in values)),
            max_col_width,
        )

    header = " │ ".join(
        col.ljust(widths[col])[:widths[col]] for col in columns
    )
    sep = "─┼─".join("─" * widths[col] for col in columns)
    print(f" {header}")
    print(f" {sep}")

    for row in rows:
        line = " │ ".join(
            str(row.get(col, ""))[:widths[col]].ljust(widths[col])
            for col in columns
        )
        print(f" {line}")


def print_tender_detail(row: dict):
    print(f"\n{'━' * 70}")
    for key in [
        "ocid", "title", "source_id", "country", "buyer_name",
        "value_amount", "value_currency", "status",
        "procurement_method", "date_published", "tender_period_end",
        "items_text", "collected_at", "exported_at",
    ]:
        val = row.get(key, "")
        if val:
            label = key.replace("_", " ").title()
            print(f"  {label:18s}: {str(val)[:100]}")
    desc = row.get("description", "")
    if desc:
        print(f"  {'Description':18s}: {desc[:300]}")
    print(f"{'━' * 70}")


# ══════════════════════════════════════════════════════════════════
#  Export functions
# ══════════════════════════════════════════════════════════════════

def export_csv(rows: list[dict], path: str):
    if not rows:
        print("❌ Нет данных для экспорта")
        return
    exclude = {"raw_json"}
    fields = [k for k in rows[0].keys() if k not in exclude]

    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f, fieldnames=fields, extrasaction="ignore",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})

    print(f"✔ {len(rows)} записей → {path}")


def export_json(rows: list[dict], path: str):
    if not rows:
        print("❌ Нет данных для экспорта")
        return
    exclude = {"raw_json"}
    clean_rows = [
        {k: v for k, v in row.items() if k not in exclude}
        for row in rows
    ]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(clean_rows, f, ensure_ascii=False, indent=2, default=str)
    print(f"✔ {len(rows)} записей → {path}")


def export_excel(rows: list[dict], path: str):
    if not rows:
        print("❌ Нет данных для экспорта")
        return

    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        print("❌ pip install openpyxl")
        export_csv(rows, path.replace(".xlsx", ".csv"))
        return

    exclude = {"raw_json"}
    fields = [k for k in rows[0].keys() if k not in exclude]

    header_labels = {
        "ocid": "ID",
        "source_id": "Источник",
        "country": "Страна",
        "title": "Название тендера",
        "description": "Описание",
        "buyer_name": "Заказчик",
        "buyer_id": "ИНН",
        "value_amount": "Сумма",
        "value_currency": "Валюта",
        "status": "Статус",
        "procurement_method": "Закон/Метод",
        "date_published": "Дата публикации",
        "tender_period_start": "Начало приёма",
        "tender_period_end": "Дедлайн",
        "items_text": "Позиции",
        "collected_at": "Дата сбора",
        "exported_at": "Дата экспорта",
        "matched_product": "Продукт",
        "matched_keywords": "Ключевые слова",
        "match_snippet": "Контекст",
    }

    wb = Workbook()
    ws = wb.active
    ws.title = "Tenders"

    hdr_font = Font(bold=True, color="FFFFFF", size=11)
    hdr_fill = PatternFill(
        start_color="2F5496", end_color="2F5496", fill_type="solid",
    )
    hdr_align = Alignment(horizontal="center", wrap_text=True)
    alt_fill = PatternFill(
        start_color="F2F2F2", end_color="F2F2F2", fill_type="solid",
    )
    border = Border(
        left=Side(style="thin", color="D9D9D9"),
        right=Side(style="thin", color="D9D9D9"),
        top=Side(style="thin", color="D9D9D9"),
        bottom=Side(style="thin", color="D9D9D9"),
    )

    # Заголовки
    for ci, fn in enumerate(fields, 1):
        cell = ws.cell(
            row=1, column=ci,
            value=header_labels.get(fn, fn),
        )
        cell.font = hdr_font
        cell.fill = hdr_fill
        cell.alignment = hdr_align
        cell.border = border

    # Данные
    for ri, row in enumerate(rows, 2):
        for ci, fn in enumerate(fields, 1):
            val = row.get(fn, "")
            if isinstance(val, str) and len(val) > 500:
                val = val[:500] + "…"
            cell = ws.cell(row=ri, column=ci, value=val)
            cell.border = border
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            if ri % 2 == 0:
                cell.fill = alt_fill

    # Автоширина
    for ci, fn in enumerate(fields, 1):
        max_len = len(header_labels.get(fn, fn))
        for row in rows[:100]:
            val_len = len(str(row.get(fn, "")))
            max_len = max(max_len, min(val_len, 60))
        ws.column_dimensions[get_column_letter(ci)].width = min(
            max_len + 4, 65,
        )

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    wb.save(path)
    print(f"✔ {len(rows)} записей → {path}")


def export_html(rows: list[dict], path: str):
    if not rows:
        print("❌ Нет данных для экспорта")
        return

    exclude = {"raw_json"}
    fields = [k for k in rows[0].keys() if k not in exclude]

    header_labels = {
        "ocid": "ID", "source_id": "Источник", "country": "Страна",
        "title": "Название", "description": "Описание",
        "buyer_name": "Заказчик", "value_amount": "Сумма",
        "value_currency": "Валюта", "status": "Статус",
        "procurement_method": "Закон", "date_published": "Опубликовано",
        "tender_period_end": "Дедлайн", "items_text": "Позиции",
        "collected_at": "Собрано", "exported_at": "Экспорт",
        "matched_product": "Продукт",
        "matched_keywords": "Ключевые слова",
    }

    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    parts = [f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<title>Tender Export</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,sans-serif;background:#f5f5f5;padding:20px}}
h1{{color:#2F5496;margin-bottom:10px}}
.meta{{color:#888;margin-bottom:15px}}
#searchInput{{padding:8px 14px;width:400px;font-size:14px;
  border:1px solid #ccc;border-radius:6px;margin-bottom:15px}}
table{{border-collapse:collapse;width:100%;background:#fff;
  box-shadow:0 1px 4px rgba(0,0,0,.1);border-radius:8px;overflow:hidden}}
th{{background:#2F5496;color:#fff;padding:10px 8px;text-align:left;
  font-size:12px;cursor:pointer;white-space:nowrap}}
th:hover{{background:#3a68b5}}
td{{padding:8px;font-size:12px;border-bottom:1px solid #eee;
  max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
td:hover{{white-space:normal;overflow:visible}}
tr:nth-child(even){{background:#fafafa}}
tr:hover{{background:#e8f0fe}}
</style></head><body>
<h1>📋 Tender Export</h1>
<p class="meta">{now} | {len(rows)} записей</p>
<input id="searchInput" placeholder="🔍 Поиск…" onkeyup="
  var v=this.value.toLowerCase();
  document.querySelectorAll('#t tbody tr').forEach(function(r){{
    r.style.display=r.textContent.toLowerCase().includes(v)?'':'none'}})">
<table id="t"><thead><tr>"""]

    for i, f in enumerate(fields):
        label = header_labels.get(f, f)
        parts.append(
            f'<th onclick="sortTable({i})">{label}</th>'
        )
    parts.append("</tr></thead><tbody>")

    for row in rows:
        parts.append("<tr>")
        for f in fields:
            val = str(row.get(f, "") or "")[:200]
            val = (
                val.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            parts.append(f"<td>{val}</td>")
        parts.append("</tr>")

    parts.append("""</tbody></table>
<script>
function sortTable(c){var t=document.getElementById('t'),
b=t.querySelector('tbody'),r=Array.from(b.rows),
d=t.dataset.d==='a'?'d':'a';t.dataset.d=d;
r.sort(function(a,b){var x=a.cells[c].textContent,
y=b.cells[c].textContent;var nx=parseFloat(x.replace(/[^\\d.-]/g,'')),
ny=parseFloat(y.replace(/[^\\d.-]/g,''));
if(!isNaN(nx)&&!isNaN(ny)){x=nx;y=ny}
return x<y?(d==='a'?-1:1):x>y?(d==='a'?1:-1):0});
r.forEach(function(r){b.appendChild(r)})}
</script></body></html>""")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))
    print(f"✔ {len(rows)} записей → {path}")
    print(f"  Открыть: open {path}")


# ══════════════════════════════════════════════════════════════════
#  Stats
# ══════════════════════════════════════════════════════════════════

def print_stats(reader: TenderReader):
    stats = reader.db.get_stats()
    print(f"\n{'═' * 70}")
    print(f"  📊 СТАТИСТИКА")
    print(f"{'═' * 70}")
    print(f"  Всего в БД             : {stats['total_raw']}")
    print(f"  Не экспортировано      : {stats['unexported_raw']}")
    print(f"  Совпадений по keywords : {stats['total_matched']}")

    if stats["by_source"]:
        print(f"\n  📁 По источникам:")
        print_table(stats["by_source"])

    if stats["by_country"]:
        print(f"\n  🌍 По странам:")
        print_table(stats["by_country"])

    if stats["by_status"]:
        print(f"\n  📌 По статусам:")
        print_table(stats["by_status"])

    if stats["by_product"]:
        print(f"\n  🎯 По продуктам:")
        print_table(stats["by_product"])

    print(f"{'═' * 70}\n")


# ══════════════════════════════════════════════════════════════════
#  Interactive
# ══════════════════════════════════════════════════════════════════

def interactive_mode(reader: TenderReader):
    print(f"\n{'═' * 70}")
    print("  🔍 ИНТЕРАКТИВНЫЙ РЕЖИМ")
    print(f"{'═' * 70}")
    print("  last [N]           — последние N тендеров")
    print("  search <текст>     — поиск")
    print("  matched [product]  — совпадения")
    print("  stats              — статистика")
    print("  export <format>    — экспорт (csv/json/excel/html)")
    print("  reset-exported     — сбросить флаги экспорта")
    print("  quit")
    print(f"{'─' * 70}\n")

    while True:
        try:
            cmd = input("tender> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break

        if not cmd:
            continue

        parts = cmd.split(maxsplit=1)
        action = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if action in ("quit", "exit", "q"):
            break
        elif action == "last":
            n = int(arg) if arg.isdigit() else 20
            rows = reader.get_last(n)
            print_table(rows)
        elif action == "search":
            if not arg:
                print("  search <текст>")
                continue
            rows = reader.search(arg)
            print(f"\n  '{arg}': {len(rows)} результатов\n")
            print_table(rows)
        elif action == "matched":
            rows = reader.get_matched(product=arg if arg else None)
            print_table(rows)
        elif action == "stats":
            print_stats(reader)
        elif action == "reset-exported":
            reader.db.reset_export_flags()
            print("  ✔ Флаги экспорта сброшены")
        elif action == "export":
            fmt = arg.lower() if arg else "excel"
            _do_export(reader, fmt, include_exported=False)
        else:
            print(f"  Неизвестная команда: {action}")


# ══════════════════════════════════════════════════════════════════
#  Export orchestrator
# ══════════════════════════════════════════════════════════════════

def _do_export(
    reader: TenderReader,
    fmt: str,
    include_exported: bool = False,
    matched_only: bool = False,
    product: Optional[str] = None,
    source: Optional[str] = None,
):
    """Экспорт с пометкой экспортированных строк."""
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out_dir = "data/exports"
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    # Получить данные
    if matched_only:
        if include_exported:
            rows = reader.get_matched(product=product)
        else:
            rows = reader.db.get_unexported_matches()
            if product:
                rows = [
                    r for r in rows
                    if r.get("matched_product") == product
                ]
        prefix = "matched"
    else:
        if include_exported:
            rows = reader.db.get_all_raw(source=source)
        else:
            rows = reader.db.get_unexported_raw(source_id=source)
        prefix = "tenders"

    if not rows:
        if include_exported:
            print("❌ Нет данных в БД")
        else:
            print("✅ Нет новых (не экспортированных) записей")
            print("   --include-exported  чтобы выгрузить всё")
            print("   --reset-exported    чтобы сбросить метки")
        return

    new_label = "" if include_exported else "_new"
    print(f"\n  📦 Экспорт: {len(rows)} записей"
          f"{' (только новые)' if not include_exported else ''}")

    # Экспорт
    formats = (
        ["csv", "json", "excel", "html"] if fmt == "all" else [fmt]
    )
    for f in formats:
        path = f"{out_dir}/{prefix}{new_label}_{ts}"
        if f == "csv":
            export_csv(rows, f"{path}.csv")
        elif f == "json":
            export_json(rows, f"{path}.json")
        elif f in ("excel", "xlsx"):
            export_excel(rows, f"{path}.xlsx")
        elif f == "html":
            export_html(rows, f"{path}.html")

    # ★ Пометить как экспортированные
    ocids = [r["ocid"] for r in rows if r.get("ocid")]
    if matched_only:
        reader.db.mark_matches_exported(ocids)
    else:
        reader.db.mark_raw_exported(ocids)

    print(f"  ✔ {len(ocids)} записей помечены как экспортированные")


# ══════════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Tender Viewer & Exporter v2",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  python -m src.viewer --stats
  python -m src.viewer --export excel
  python -m src.viewer --export excel --include-exported
  python -m src.viewer --matched --export excel
  python -m src.viewer --reset-exported
  python -m src.viewer -i
        """,
    )

    parser.add_argument("--last", type=int, metavar="N")
    parser.add_argument("--search", type=str)
    parser.add_argument("--matched", action="store_true")
    parser.add_argument("--product", type=str)
    parser.add_argument("--source", type=str)
    parser.add_argument("--stats", action="store_true")
    parser.add_argument("--detail", type=str, metavar="OCID")
    parser.add_argument(
        "--export", type=str,
        choices=["csv", "json", "excel", "html", "all"],
    )
    parser.add_argument(
        "--include-exported", action="store_true",
        help="Экспортировать ВСЕ, включая ранее экспортированные",
    )
    parser.add_argument(
        "--reset-exported", action="store_true",
        help="Сбросить все метки экспорта",
    )
    parser.add_argument("--interactive", "-i", action="store_true")
    parser.add_argument("--db", type=str, default="data/tenders.db")

    args = parser.parse_args()

    if len(sys.argv) == 1:
        parser.print_help()
        print("\n  💡 python -m src.viewer --stats")
        print("  💡 python -m src.viewer --export excel")
        print("  💡 python -m src.viewer -i\n")
        return

    reader = TenderReader(args.db)

    try:
        if args.interactive:
            interactive_mode(reader)
            return

        if args.reset_exported:
            reader.db.reset_export_flags()
            print("✔ Все метки экспорта сброшены")
            return

        if args.stats:
            print_stats(reader)

        if args.last:
            rows = reader.get_last(args.last, source=args.source)
            print(f"\n  Последние {args.last}:\n")
            print_table(rows)

        if args.search:
            rows = reader.search(args.search)
            print(f"\n  '{args.search}': {len(rows)} результатов\n")
            print_table(rows)

        if args.detail:
            rows = [
                dict(r) for r in reader.db.conn.execute(
                    "SELECT * FROM raw_tenders WHERE ocid LIKE ?",
                    (f"%{args.detail}%",),
                ).fetchall()
            ]
            for r in rows:
                print_tender_detail(r)

        if args.matched and not args.export:
            rows = reader.get_matched(product=args.product)
            print(f"\n  Совпадения: {len(rows)}\n")
            print_table(rows)

        if args.export:
            _do_export(
                reader,
                fmt=args.export,
                include_exported=args.include_exported,
                matched_only=args.matched,
                product=args.product,
                source=args.source,
            )

    finally:
        reader.close()


if __name__ == "__main__":
    main()