"""Спільні для всіх сайтів типи: фільтр підписки, оголошення, проміжок дат.

Фільтр не прив'язаний до джерела: кожне джерело саме перекладає його у свій
запит. Остаточне рішення «підходить чи ні» ухвалюють `listing_matches` і
`matching_slots` тут, а не API сайту, — фільтр в API лише економить сторінки.
THS уже мовчки ігнорує частину полів (`countryCode`), і якщо колись почне
ігнорувати ще якесь, без локальної перевірки це був би спам на сотні алертів.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date

CONTINENTS: dict[str, str] = {
    "europe": "Europe",
    "asia": "Asia",
    "africa": "Africa",
    "oceania": "Oceania",
    "north-america": "North America",
    "south-america": "South America",
}

# Рівно ті рядки, що приймає THS у `pets[].type` і віддає в `animals[].name`.
# Невідомий тип API мовчки перетворює на 0 результатів, тож список закритий.
PETS: tuple[str, ...] = (
    "dog", "cat", "horse", "bird", "fish", "reptile", "poultry", "farm animal", "small pet",
)


@dataclass(frozen=True)
class Filter:
    continent: str
    country: str | None = None  # slug країни у джерела (з Source.countries)
    date_from: date | None = None
    date_to: date | None = None
    min_days: int | None = None
    max_days: int | None = None
    pets: tuple[str, ...] = ()  # OR: в оголошенні є хоч одна з цих тварин
    # «Уся Європа, крім UK». Лише разом із country=None. API виключень не вміє,
    # тож джерело питає весь континент, а відсікає локальний фільтр.
    exclude_countries: tuple[str, ...] = ()

    def to_json(self) -> str:
        """Канонічний JSON — водночас ключ групування однакових підписок."""
        data = {
            "continent": self.continent,
            "country": self.country,
            "date_from": self.date_from.isoformat() if self.date_from else None,
            "date_to": self.date_to.isoformat() if self.date_to else None,
            "min_days": self.min_days,
            "max_days": self.max_days,
            "pets": sorted(self.pets),
        }
        # Порожнє поле не пишемо: інакше в уже збережених підписок змінився б
        # ключ групи, і query_state осиротів би.
        if self.exclude_countries:
            data["exclude_countries"] = sorted(self.exclude_countries)
        return json.dumps(data, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> "Filter":
        d = json.loads(raw)
        return cls(
            continent=d["continent"],
            country=d.get("country"),
            date_from=date.fromisoformat(d["date_from"]) if d.get("date_from") else None,
            date_to=date.fromisoformat(d["date_to"]) if d.get("date_to") else None,
            min_days=d.get("min_days"),
            max_days=d.get("max_days"),
            pets=tuple(d.get("pets") or ()),
            exclude_countries=tuple(d.get("exclude_countries") or ()),
        )


@dataclass(frozen=True)
class Slot:
    """Один проміжок дат оголошення (assignment у THS)."""

    id: str
    start: date
    end: date
    is_open: bool  # не підтверджений і не на розгляді
    applicants: int | None = None

    @property
    def days(self) -> int:
        # Так рахує сам THS: 2026-12-09..2027-01-09 — це 31, а не 32.
        return (self.end - self.start).days


@dataclass(frozen=True)
class Listing:
    source: str
    id: str
    title: str
    url: str
    continent: str
    country: str
    place: str  # «Espiche, Faro, Portugal» — лише для показу
    photo_url: str | None
    pets: tuple[tuple[str, int], ...]  # (("cat", 2), ("dog", 1))
    slots: tuple[Slot, ...] = field(default=())


@dataclass(frozen=True)
class Country:
    slug: str
    name: str
    count: int


def listing_matches(listing: Listing, flt: Filter) -> bool:
    if listing.continent != flt.continent:
        return False
    if flt.country and listing.country != flt.country:
        return False
    if listing.country in flt.exclude_countries:
        return False
    if flt.pets and not {name for name, _ in listing.pets} & set(flt.pets):
        return False
    return True


def matching_slots(listing: Listing, flt: Filter, today: date) -> list[Slot]:
    """Відкриті проміжки, що підходять під фільтр.

    Семантика дат — як у THS: проміжок має ПОВНІСТЮ вміщатися у вікно
    (перетину недостатньо), а вже розпочаті проміжки не цікаві.
    """
    lower = max(today, flt.date_from) if flt.date_from else today
    out = []
    for slot in listing.slots:
        if not slot.is_open or slot.start < lower:
            continue
        if flt.date_to and slot.end > flt.date_to:
            continue
        if flt.min_days is not None and slot.days < flt.min_days:
            continue
        if flt.max_days is not None and slot.days > flt.max_days:
            continue
        out.append(slot)
    return out


def slot_key(listing: Listing, slot: Slot) -> str:
    return f"{listing.source}:{slot.id}"
