from __future__ import annotations

import copy
import json
import pathlib
from datetime import date

import pytest

from petalert.sources.ths import parse_listing

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
RAW = json.loads((FIXTURES / "ths_search_portugal.json").read_text())["results"]


def raw(listing_id: str) -> dict:
    return copy.deepcopy(next(r for r in RAW if r["id"] == listing_id))


def test_every_fixture_listing_parses():
    assert len([parse_listing(r) for r in RAW]) == len(RAW)


def test_fields_url_and_photo():
    listing = parse_listing(raw("2214923"))
    assert listing.source == "ths"
    assert listing.url == "https://www.trustedhousesitters.com/house-and-pet-sitting-assignments/l/2214923/"
    assert listing.continent == "europe" and listing.country == "portugal"
    assert listing.place == "Espiche, Faro, Portugal" and listing.country_code == "pt"
    assert (listing.lat, listing.lon) == (37.10189, -8.73795)
    assert listing.photo_url.startswith(
        "https://res.cloudinary.com/trustedhousesitters/image/upload/t_social_media_preview/remote_media/"
    )
    assert listing.pets == (("dog", 1),)
    (slot,) = listing.slots
    assert (slot.start, slot.end, slot.days, slot.is_open) == (
        date(2026, 12, 9), date(2027, 1, 9), 31, True,
    )


def test_missing_coordinates_are_fine():
    r = raw("2214923")
    del r["location"]["coordinates"]
    assert (parse_listing(r).lat, parse_listing(r).lon) == (None, None)


def test_confirmed_and_reviewing_slots_are_not_open():
    confirmed = parse_listing(raw("3146164")).slots
    assert [s.is_open for s in confirmed] == [False, True]
    reviewing = parse_listing(raw("3152528")).slots
    assert [s.is_open for s in reviewing] == [True, False]


def test_multiline_title_is_flattened():
    assert "\n" not in parse_listing(raw("2117522")).title


def test_cover_photo_wins_and_missing_photos_are_fine():
    r = raw("2214923")
    cover = next(p for p in r["photos"] if p["coverPhoto"])
    assert parse_listing(r).photo_url.endswith(cover["publicId"])
    r["photos"] = []
    assert parse_listing(r).photo_url is None


@pytest.mark.parametrize(
    "path",
    [("id",), ("assignments",), ("location", "countrySlug"), ("assignments", 0, "isConfirmed")],
)
def test_missing_required_field_raises(path):
    r = raw("3146164")
    target = r
    for key in path[:-1]:
        target = target[key]
    del target[path[-1]]
    with pytest.raises(KeyError):
        parse_listing(r)


def test_published_is_london_time_converted_to_utc_and_slot_ids_are_sequenced():
    from datetime import UTC, datetime

    r = raw("2214923")
    r["published"] = "2026-10-17T17:27:00"  # BST
    listing = parse_listing(r)
    assert listing.published == datetime(2026, 10, 17, 16, 27, tzinfo=UTC)
    assert listing.slots[0].seq == int(listing.slots[0].id)
    r["published"] = None
    assert parse_listing(r).published is None
