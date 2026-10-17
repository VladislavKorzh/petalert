from __future__ import annotations

from datetime import date

import pytest

from petalert.bot.dialog import Pick, countries_keyboard, parse_date_range, parse_duration
from petalert.bot.manage import SubAction
from petalert.models import PETS

TODAY = date(2026, 10, 17)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2026-11-01 2027-01-31", (date(2026, 11, 1), date(2027, 1, 31))),
        ("01.11.2026 - 31.01.2027", (date(2026, 11, 1), date(2027, 1, 31))),
        ("from 1/11/2026 to 31/1/2027", (date(2026, 11, 1), date(2027, 1, 31))),
        ("2026-09-01 2026-12-01", (TODAY, date(2026, 12, 1))),  # past start → today
    ],
)
def test_parse_date_range_accepts(text, expected):
    assert parse_date_range(text, TODAY) == expected


@pytest.mark.parametrize(
    "text,error",
    [
        ("next month", "two dates"),
        ("2026-11-01", "two dates"),
        ("2026-11-01 2026-10-31", "after the start"),
        ("2026-02-30 2026-03-05", "doesn't exist"),
        ("2026-01-01 2026-02-01", "past"),
    ],
)
def test_parse_date_range_rejects(text, error):
    with pytest.raises(ValueError, match=error):
        parse_date_range(text, TODAY)


@pytest.mark.parametrize(
    "text,expected",
    [("7", (7, None)), ("7+", (7, None)), (" 7 - 21 ", (7, 21)), ("7–21", (7, 21)), ("7 to 7", (7, 7))],
)
def test_parse_duration_accepts(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "week", "0", "21-7", "400", "7-400"])
def test_parse_duration_rejects(text):
    with pytest.raises(ValueError):
        parse_duration(text)


def test_callback_data_fits_telegram_64_bytes():
    packed = [Pick(step="pet", value=p).pack() for p in PETS]
    packed += [Pick(step="cont", value="south-america").pack(), Pick(step="ctry", value="199").pack()]
    packed.append(SubAction(action="resume", sub_id=10**12).pack())
    assert max(len(p.encode()) for p in packed) <= 64


def test_country_pages():
    countries = [[f"c{i}", f"Country {i}", 100 - i] for i in range(23)]
    first = countries_keyboard(countries, 0).inline_keyboard
    assert first[0][0].text == "🌍 Any country"
    labels = [b.text for row in first for b in row]
    assert "Country 0 · 100" in labels and "Country 10 · 90" not in labels
    assert "Next ▶" in labels and "◀ Prev" not in labels

    last = [b.text for row in countries_keyboard(countries, 2).inline_keyboard for b in row]
    assert "Country 22 · 78" in last and "◀ Prev" in last and "Next ▶" not in last


def test_exclude_mode_marks_countries_and_offers_done():
    countries = [[f"c{i}", f"Country {i}", 100 - i] for i in range(12)]
    rows = countries_keyboard(countries, 0, excluded=["c1"]).inline_keyboard
    labels = [b.text for row in rows for b in row]
    assert "🌍 Any country" not in labels and "🚫 All except…" not in labels
    assert "🚫 Country 1 · 99" in labels and "Country 0 · 100" in labels
    assert "Done ✔ (1 excluded)" in labels
    steps = {Pick.unpack(b.callback_data).step for row in rows for b in row}
    assert steps == {"excl", "epage", "edone", "cancel"}
