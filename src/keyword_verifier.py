"""
Keyword Verifier
================
Постпроцессинг: проверяет вхождение ключевых слов в текст тендера.

Режимы:
  - exact       : точное вхождение фразы (case-insensitive)
  - fuzzy       : с учётом падежей, числа (лемматизация)
  - token       : все слова фразы есть в тексте (порядок неважен)
  - regex       : кастомное regex-выражение
"""

from __future__ import annotations

import re
import logging
from typing import Optional
from dataclasses import dataclass

logger = logging.getLogger("keyword_verifier")


# ══════════════════════════════════════════════════════════════════
#  Lemmatizer (опциональная зависимость)
# ══════════════════════════════════════════════════════════════════

_LEMMATIZER = None


def get_lemmatizer():
    """Lazy-load pymorphy2 для морфологии."""
    global _LEMMATIZER
    if _LEMMATIZER is None:
        try:
            import pymorphy2
            _LEMMATIZER = pymorphy2.MorphAnalyzer()
            logger.debug("pymorphy2 loaded")
        except ImportError:
            logger.warning(
                "pymorphy2 not installed. "
                "Install: pip install pymorphy2 pymorphy2-dicts-ru"
            )
            _LEMMATIZER = False
    return _LEMMATIZER if _LEMMATIZER is not False else None


# ══════════════════════════════════════════════════════════════════
#  Verification result
# ══════════════════════════════════════════════════════════════════

@dataclass
class VerificationResult:
    """Результат проверки одного ключевого слова."""
    keyword: str
    matched: bool
    mode: str
    snippet: str = ""
    position: int = -1


# ══════════════════════════════════════════════════════════════════
#  Verifier
# ══════════════════════════════════════════════════════════════════

class KeywordVerifier:
    """
    Проверяет вхождение ключевых слов в текст тендера.

    Поддерживает несколько режимов:
      - exact  : "маркетинговая платформа" → ищет точно эту фразу
      - fuzzy  : "маркетинговая платформа" → найдёт
                 "маркетинговой платформой" (с морфологией)
      - token  : "маркетинговая платформа" → найдёт если есть
                 "маркетинговая" И "платформа" в любом месте
      - regex  : использует keyword как готовое regex-выражение
    """

    def __init__(
        self,
        mode: str = "fuzzy",
        case_sensitive: bool = False,
        min_keyword_length: int = 3,
    ):
        """
        Args:
            mode: exact | fuzzy | token | regex
            case_sensitive: учитывать регистр
            min_keyword_length: игнорировать ключевики короче N символов
        """
        self.mode = mode
        self.case_sensitive = case_sensitive
        self.min_keyword_length = min_keyword_length

        if mode == "fuzzy":
            self.lemmatizer = get_lemmatizer()
            if not self.lemmatizer:
                logger.warning(
                    "Fuzzy mode unavailable, falling back to 'exact'"
                )
                self.mode = "exact"

    def verify(
        self,
        text: str,
        keyword: str,
    ) -> VerificationResult:
        """
        Проверить вхождение одного ключевого слова в текст.

        Returns:
            VerificationResult с флагом matched и контекстом
        """
        if not text or not keyword:
            return VerificationResult(
                keyword=keyword,
                matched=False,
                mode=self.mode,
            )

        keyword = keyword.strip()
        if len(keyword) < self.min_keyword_length:
            return VerificationResult(
                keyword=keyword,
                matched=False,
                mode=self.mode,
                snippet="(too short)",
            )

        # Нормализация регистра
        if not self.case_sensitive:
            text_search = text.lower()
            keyword_search = keyword.lower()
        else:
            text_search = text
            keyword_search = keyword

        # Диспетчер по режиму
        if self.mode == "exact":
            return self._verify_exact(
                text_search, keyword_search, keyword, text,
            )
        elif self.mode == "fuzzy":
            return self._verify_fuzzy(
                text, keyword, text_search, keyword_search,
            )
        elif self.mode == "token":
            return self._verify_token(
                text_search, keyword_search, keyword,
            )
        elif self.mode == "regex":
            return self._verify_regex(text, keyword)
        else:
            logger.error("Unknown mode: %s", self.mode)
            return VerificationResult(
                keyword=keyword,
                matched=False,
                mode=self.mode,
            )

    # ── Exact match ───────────────────────────────────────────────

    def _verify_exact(
        self,
        text_search: str,
        keyword_search: str,
        keyword_orig: str,
        text_orig: str,
    ) -> VerificationResult:
        """Точное вхождение подстроки."""
        pos = text_search.find(keyword_search)
        if pos == -1:
            return VerificationResult(
                keyword=keyword_orig,
                matched=False,
                mode="exact",
            )

        # Вырезаем контекст ±50 символов
        start = max(0, pos - 50)
        end = min(len(text_orig), pos + len(keyword_search) + 50)
        snippet = (
            ("…" if start > 0 else "")
            + text_orig[start:end]
            + ("…" if end < len(text_orig) else "")
        )

        return VerificationResult(
            keyword=keyword_orig,
            matched=True,
            mode="exact",
            snippet=snippet,
            position=pos,
        )

    # ── Fuzzy match (с морфологией) ───────────────────────────────

    def _verify_fuzzy(
        self,
        text: str,
        keyword: str,
        text_lower: str,
        keyword_lower: str,
    ) -> VerificationResult:
        """
        Поиск с учётом морфологии (лемматизация).

        "маркетинговая платформа" → найдёт:
          - маркетинговой платформой
          - маркетинговые платформы
          - маркетинговую платформу
        """
        if not self.lemmatizer:
            # Фоллбэк на exact
            return self._verify_exact(
                text_lower, keyword_lower, keyword, text,
            )

        # Лемматизируем ключевое слово
        kw_tokens = self._tokenize(keyword_lower)
        kw_lemmas = [
            self.lemmatizer.parse(t)[0].normal_form
            for t in kw_tokens
        ]

        # Лемматизируем текст построчно (для производительности)
        # Ищем окно с нужными леммами подряд
        text_tokens = self._tokenize(text_lower)
        text_lemmas = [
            self.lemmatizer.parse(t)[0].normal_form
            for t in text_tokens
        ]

        # Поиск подпоследовательности kw_lemmas в text_lemmas
        kw_len = len(kw_lemmas)
        for i in range(len(text_lemmas) - kw_len + 1):
            window = text_lemmas[i : i + kw_len]
            if window == kw_lemmas:
                # Нашли совпадение — берём оригинальные токены
                matched_tokens = text_tokens[i : i + kw_len]
                # Находим позицию в исходном тексте
                pattern = r"\b" + r"\W+".join(
                    re.escape(t) for t in matched_tokens
                ) + r"\b"
                m = re.search(pattern, text_lower, re.IGNORECASE)
                if m:
                    pos = m.start()
                    start = max(0, pos - 50)
                    end = min(len(text), m.end() + 50)
                    snippet = (
                        ("…" if start > 0 else "")
                        + text[start:end]
                        + ("…" if end < len(text) else "")
                    )
                    return VerificationResult(
                        keyword=keyword,
                        matched=True,
                        mode="fuzzy",
                        snippet=snippet,
                        position=pos,
                    )

        return VerificationResult(
            keyword=keyword,
            matched=False,
            mode="fuzzy",
        )

    # ── Token match (все слова есть, порядок неважен) ─────────────

    def _verify_token(
        self,
        text_search: str,
        keyword_search: str,
        keyword_orig: str,
    ) -> VerificationResult:
        """
        Проверяет что ВСЕ слова из ключевой фразы есть в тексте
        (порядок и форма неважны, но все должны быть).

        "маркетинговая платформа" → найдёт:
          - "платформа для маркетинговых целей"
          - "система маркетинговая, входит в платформу"
        """
        kw_tokens = set(self._tokenize(keyword_search))
        text_tokens = set(self._tokenize(text_search))

        if kw_tokens.issubset(text_tokens):
            # Все токены найдены
            return VerificationResult(
                keyword=keyword_orig,
                matched=True,
                mode="token",
                snippet=f"Tokens: {kw_tokens}",
            )

        missing = kw_tokens - text_tokens
        return VerificationResult(
            keyword=keyword_orig,
            matched=False,
            mode="token",
            snippet=f"Missing: {missing}",
        )

    # ── Regex match ───────────────────────────────────────────────

    def _verify_regex(
        self,
        text: str,
        keyword: str,
    ) -> VerificationResult:
        """Keyword — готовое regex-выражение."""
        try:
            flags = 0 if self.case_sensitive else re.IGNORECASE
            pattern = re.compile(keyword, flags)
            m = pattern.search(text)
            if m:
                pos = m.start()
                start = max(0, pos - 50)
                end = min(len(text), m.end() + 50)
                snippet = (
                    ("…" if start > 0 else "")
                    + text[start:end]
                    + ("…" if end < len(text) else "")
                )
                return VerificationResult(
                    keyword=keyword,
                    matched=True,
                    mode="regex",
                    snippet=snippet,
                    position=pos,
                )
            else:
                return VerificationResult(
                    keyword=keyword,
                    matched=False,
                    mode="regex",
                )
        except re.error as exc:
            logger.error("Invalid regex '%s': %s", keyword, exc)
            return VerificationResult(
                keyword=keyword,
                matched=False,
                mode="regex",
                snippet=f"regex error: {exc}",
            )

    # ── Tokenizer ─────────────────────────────────────────────────

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """Разбить текст на слова (алфавитно-цифровые токены)."""
        # Убираем пунктуацию, оставляем буквы, цифры, дефисы
        tokens = re.findall(r"[\w-]+", text, re.UNICODE)
        # Фильтруем короткие (предлоги, союзы)
        return [t for t in tokens if len(t) >= 2]

    # ── Batch verification ────────────────────────────────────────

    def verify_any(
        self,
        text: str,
        keywords: list[str],
    ) -> tuple[bool, list[VerificationResult]]:
        """
        Проверить список ключевых слов.

        Returns:
            (matched_any, results)
            matched_any — True если хотя бы одно ключевое слово нашлось
            results — список VerificationResult для каждого keyword
        """
        results = []
        matched_any = False

        for kw in keywords:
            res = self.verify(text, kw)
            results.append(res)
            if res.matched:
                matched_any = True

        return matched_any, results


# ══════════════════════════════════════════════════════════════════
#  Standalone test
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(levelname)s: %(message)s",
    )

    # Тестовый текст
    test_text = """
    ООО "ЛАССЕЛСБЕРГЕР" объявляет тендер на услуги SEO-продвижения сайта.
    Требуется маркетинговая платформа для управления кампаниями.
    Бюджет: 500 000 рублей. Регион: Республика Башкортостан.
    """

    keywords = [
        "маркетинговая платформа",
        "SMS firewall",
        "SEO продвижение",
        "управление кампаниями",
    ]

    for mode in ["exact", "fuzzy", "token"]:
        print(f"\n{'='*60}")
        print(f"  MODE: {mode}")
        print(f"{'='*60}")

        verifier = KeywordVerifier(mode=mode)
        matched, results = verifier.verify_any(test_text, keywords)

        print(f"  Matched any: {matched}\n")
        for r in results:
            status = "✔" if r.matched else "✘"
            print(f"  {status} '{r.keyword}'")
            if r.snippet:
                print(f"     → {r.snippet[:100]}")