"""Контракт сайту-джерела.

Новий сайт = один модуль у `sources/`, що реалізує `Source`, і рядок у
`REGISTRY`. Решта (діалог, воркер, dedup, алерти) працює з нормалізованими
`Listing`/`Slot` і про конкретний сайт не знає.
"""

from __future__ import annotations

from typing import Protocol

from ..models import Country, Filter, Listing


class SourceError(Exception):
    """Джерело не відповіло як очікувалось: статус, формат, вимоги до запиту."""


class Source(Protocol):
    key: str  # префікс ключів dedup і значення `subscription.source`
    title: str  # назва для людей: кнопка в діалозі, підпис алерту

    async def countries(self, continent: str) -> list[Country]:
        """Країни континенту, де зараз є відкриті сити, найбільші першими."""
        ...

    async def search(self, flt: Filter, *, max_pages: int | None) -> list[Listing]:
        """Оголошення під фільтр, найновіші першими. `None` — усі сторінки."""
        ...
