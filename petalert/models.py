"""Site-agnostic types: subscription filter, listing, date slot.

The filter is not tied to a source: each source translates it into its own
query. The final "matches or not" decision is made here by `listing_matches`
and `matching_slots`, not by the site's API — the API filter only saves pages.
THS already silently ignores some fields (`countryCode`); if it ever starts
ignoring another one, without the local check that would mean hundreds of
spam alerts.
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

# Exactly the strings THS accepts in `pets[].type` and returns in `animals[].name`.
# The API silently turns an unknown type into 0 results, so the list is closed.
PETS: tuple[str, ...] = (
    "dog", "cat", "horse", "bird", "fish", "reptile", "poultry", "farm animal", "small pet",
)


@dataclass(frozen=True)
class Filter:
    continent: str
    country: str | None = None  # the source's country slug (from Source.countries)
    date_from: date | None = None
    date_to: date | None = None
    min_days: int | None = None
    max_days: int | None = None
    pets: tuple[str, ...] = ()  # OR: the listing has at least one of these pets
    # "All of Europe except the UK". Only with country=None. The API can't
    # exclude, so the source queries the whole continent and the local filter
    # drops the excluded countries.
    exclude_countries: tuple[str, ...] = ()

    def to_json(self) -> str:
        """Canonical JSON — also the key that groups identical subscriptions."""
        data = {
            "continent": self.continent,
            "country": self.country,
            "date_from": self.date_from.isoformat() if self.date_from else None,
            "date_to": self.date_to.isoformat() if self.date_to else None,
            "min_days": self.min_days,
            "max_days": self.max_days,
            "pets": sorted(self.pets),
        }
        # Omitted when empty: otherwise already stored subscriptions would get a
        # new group key and orphan their query_state.
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
    """One date slot of a listing (an "assignment" in THS)."""

    id: str
    start: date
    end: date
    is_open: bool  # neither confirmed nor under review
    applicants: int | None = None

    @property
    def days(self) -> int:
        # This is how THS counts: 2026-12-09..2027-01-09 is 31, not 32.
        return (self.end - self.start).days


@dataclass(frozen=True)
class Listing:
    source: str
    id: str
    title: str
    url: str
    continent: str
    country: str
    place: str  # "Espiche, Faro, Portugal" — display only
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
    """Open slots that match the filter.

    Date semantics follow THS: a slot must fit ENTIRELY inside the window
    (overlap is not enough), and slots that have already started don't count.
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
