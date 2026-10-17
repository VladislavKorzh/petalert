"""Форма query для THS. Кожне поле тут перевірене живим запитом — і кожна
помилка мовчазна: зайвий чи неправильний ключ API не відхиляє, а просто
повертає не ті оголошення (або нуль)."""

from __future__ import annotations

from datetime import date

from petalert.models import PETS, Filter
from petalert.sources.ths import build_query

TODAY = date(2026, 10, 17)


def test_continent_only_by_default():
    q = build_query(Filter(continent="europe"), TODAY)
    f = q["filters"]
    assert f["geoHierarchy"] == {"continentSlug": "europe"}
    assert f["activeMembership"] is True
    assert f["assignments"] == {"confirmed": False, "reviewing": False, "dateFrom": "2026-10-17"}
    assert "pets" not in f
    assert q["sort"] == [{"published": "desc"}]
    assert q["page"] == 1 and q["resultsPerPage"] == 100


def test_country_uses_slug_never_country_code():
    f = build_query(Filter(continent="europe", country="portugal"), TODAY)["filters"]
    # countryCode API мовчки ігнорує — фільтр «Португалія» віддав би весь світ.
    assert f["geoHierarchy"] == {"countrySlug": "portugal"}


def test_past_date_from_is_clamped_to_today_and_date_to_only_when_set():
    a = build_query(Filter(continent="asia", date_from=date(2026, 1, 1)), TODAY)["filters"]["assignments"]
    assert a["dateFrom"] == "2026-10-17"
    assert "dateTo" not in a

    a = build_query(
        Filter(continent="asia", date_from=date(2026, 11, 1), date_to=date(2027, 1, 31)), TODAY
    )["filters"]["assignments"]
    assert (a["dateFrom"], a["dateTo"]) == ("2026-11-01", "2027-01-31")


def test_only_given_duration_bounds_are_sent():
    a = build_query(Filter(continent="asia", min_days=7), TODAY)["filters"]["assignments"]
    assert a["durationInDays"] == {"minimum": 7}
    a = build_query(Filter(continent="asia", max_days=30), TODAY)["filters"]["assignments"]
    assert a["durationInDays"] == {"maximum": 30}
    a = build_query(Filter(continent="asia"), TODAY)["filters"]["assignments"]
    assert "durationInDays" not in a


def test_pet_types_are_exactly_the_site_strings():
    f = build_query(Filter(continent="europe", pets=("small pet", "cat")), TODAY)["filters"]
    assert f["pets"] == [{"type": "cat"}, {"type": "small pet"}]
    # Невідомий тип = 0 результатів без жодної помилки; список закритий.
    assert PETS == (
        "dog", "cat", "horse", "bird", "fish", "reptile", "poultry", "farm animal", "small pet",
    )


def test_page_and_facets():
    q = build_query(Filter(continent="europe"), TODAY, 3, facets=("country",))
    assert q["page"] == 3 and q["facets"] == ["country"]


def test_exclusions_are_not_sent_to_the_api():
    # THS виключень не вміє: питаємо весь континент, відсікає локальний фільтр.
    f = build_query(Filter("europe", exclude_countries=("united-kingdom",)), TODAY)["filters"]
    assert f["geoHierarchy"] == {"continentSlug": "europe"}
    assert "united-kingdom" not in str(f)
