"""Абстрактный базовый класс для всех источников тендеров."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Generator

from src.models import Tender


class BaseTenderSource(ABC):
    """
    Каждый источник (OCDS API, bicotender, zakupki.gov.ru и т.д.)
    наследуется от этого класса.
    """

    source_id: str = "base"
    source_name: str = "Base Source"
    country: str = "??"

    @abstractmethod
    def collect(self, **kwargs) -> Generator[Tender, None, None]:
        """Yield нормализованных тендеров."""
        ...

    @abstractmethod
    def test_connection(self) -> dict:
        """Проверка доступности. Returns {"ok": bool, "error": str}."""
        ...