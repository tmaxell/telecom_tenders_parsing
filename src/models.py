"""Модели данных проекта."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional


@dataclass
class Tender:
    """Нормализованный тендер из OCDS-релиза."""

    ocid: str                                       # Open Contracting ID
    release_id: str
    source_id: str                                  # id из sources.yaml
    country: str

    title: str = ""
    description: str = ""
    status: str = ""                                # active / closed / cancelled …
    procurement_method: str = ""

    value_amount: Optional[float] = None
    value_currency: str = ""

    buyer_name: str = ""
    buyer_id: str = ""

    tender_period_start: Optional[str] = None
    tender_period_end: Optional[str] = None
    date_published: Optional[str] = None

    items_text: str = ""                            # склеенные описания items
    raw_json: str = ""                              # полный JSON релиза

    collected_at: str = field(
        default_factory=lambda: datetime.utcnow().isoformat()
    )

    # --- helpers -----------------------------------------------------------
    def searchable_text(self) -> str:
        """Объединённый текст для поиска по ключевым словам."""
        parts = [
            self.title,
            self.description,
            self.items_text,
            self.buyer_name,
        ]
        return " ".join(p for p in parts if p).lower()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_ocds_release(
        cls,
        release: dict,
        source_id: str,
        country: str,
    ) -> "Tender":
        """Фабрика: OCDS release → Tender."""
        tender_obj = release.get("tender", {})
        value = tender_obj.get("value", {})
        period = tender_obj.get("tenderPeriod", {})
        buyer = release.get("buyer", {})

        # собираем тексты items
        items_parts: list[str] = []
        for item in tender_obj.get("items", []):
            if desc := item.get("description"):
                items_parts.append(desc)
            classification = item.get("classification", {})
            if cdesc := classification.get("description"):
                items_parts.append(cdesc)

        return cls(
            ocid=release.get("ocid", ""),
            release_id=release.get("id", ""),
            source_id=source_id,
            country=country,
            title=tender_obj.get("title", ""),
            description=tender_obj.get("description", ""),
            status=tender_obj.get("status", ""),
            procurement_method=tender_obj.get("procurementMethod", ""),
            value_amount=value.get("amount"),
            value_currency=value.get("currency", ""),
            buyer_name=buyer.get("name", ""),
            buyer_id=buyer.get("id", ""),
            tender_period_start=period.get("startDate"),
            tender_period_end=period.get("endDate"),
            date_published=release.get("date"),
            items_text=" | ".join(items_parts),
            raw_json=json.dumps(release, ensure_ascii=False, default=str),
        )


@dataclass
class MatchResult:
    """Результат совпадения тендера с ключевыми словами."""

    ocid: str
    source_id: str
    country: str
    title: str
    description: str
    buyer_name: str
    value_amount: Optional[float]
    value_currency: str
    tender_period_end: Optional[str]
    date_published: Optional[str]

    matched_product: str           # имя продукта из keywords.yaml
    matched_keywords: str          # через запятую
    match_snippet: str             # фрагмент текста вокруг совпадения

    raw_json: str = ""