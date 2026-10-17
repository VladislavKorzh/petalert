from __future__ import annotations

from datetime import date

from petalert.bot.format import (
    CAPTION_LIMIT,
    TEXT_LIMIT,
    country_name,
    describe_filter,
    render_alert,
    render_digest,
    tg_len,
)
from petalert.models import Filter, Listing, Slot


def listing(title="Cosy flat", lid="1", pets=(("cat", 2), ("dog", 1))) -> Listing:
    return Listing(
        source="ths", id=lid, title=title, url=f"https://x/l/{lid}/", continent="europe",
        country="portugal", place="Espiche, Faro, Portugal", photo_url=None, pets=pets,
    )


def slot(i: int) -> Slot:
    return Slot(str(i), date(2026, 12, 1), date(2026, 12, 14), True, applicants=i % 3)


def test_alert_layout():
    text, markup = render_alert(listing(), [slot(1)], [3], "TrustedHousesitters")
    assert text == (
        "🏡 <b>Cosy flat</b>\n\n📍 Espiche, Faro, Portugal\n🐈 2 cats · 🐕 1 dog\n\n"
        "🆕 <b>New date:</b>\n📅 <b>1 Dec – 14 Dec 2026</b>\n      └ ⏳ 13 days · 👥 1 applicant\n\n"
        "🔔 Subscription #3 · TrustedHousesitters"
    )
    button = markup.inline_keyboard[0][0]
    assert button.url == "https://x/l/1/" and button.text == "🔗 Open on TrustedHousesitters"


def test_zero_applicants_is_highlighted():
    text, _ = render_alert(listing(), [slot(3)], [1], "THS")
    assert "🟢 no applicants yet" in text


def test_caption_is_truncated_by_dropping_date_lines():
    text, _ = render_alert(listing(title="x" * 500), [slot(i) for i in range(30)], [1, 2], "THS")
    assert tg_len(text) <= CAPTION_LIMIT
    assert "more dates" in text and text.endswith("🔔 Subscription #1, #2 · THS")


def test_html_is_escaped():
    text, _ = render_alert(listing(title="<Cats & Dogs>"), [slot(1)], [1], "THS")
    assert "&lt;Cats &amp; Dogs&gt;" in text


def test_digest_reports_how_many_items_fit():
    items = [(listing(title="y" * 200, lid=str(i)), [slot(i)]) for i in range(40)]
    text, included = render_digest(items)
    assert tg_len(text) <= TEXT_LIMIT
    assert 0 < included < 40
    assert text.count("<a href") == included
    assert text.startswith(f"📋 <b>{included} more new listings</b>")


def test_describe_filter():
    flt = Filter(
        "europe", country="bosnia-and-herzegovina", date_from=date(2026, 11, 1),
        date_to=date(2027, 1, 31), min_days=7, max_days=21, pets=("cat", "dog"),
    )
    assert describe_filter(flt) == (
        "🌍 Where: Bosnia and Herzegovina (Europe)\n📅 Dates: 1 Nov 2026 – 31 Jan 2027\n"
        "⏳ Length: 7–21 days\n🐾 Pets: 🐈 cat or 🐕 dog"
    )
    assert describe_filter(Filter("europe", exclude_countries=("united-kingdom", "ireland"))).startswith(
        "🌍 Where: Europe, except Ireland, United Kingdom\n"
    )
    assert describe_filter(Filter("asia")) == (
        "🌍 Where: Asia, any country\n📅 Dates: any\n⏳ Length: any\n🐾 Pets: any"
    )


def test_country_name():
    assert country_name("united-kingdom") == "United Kingdom"
    assert country_name("isle-of-man") == "Isle of Man"


def test_tg_len_counts_like_telegram():
    # Tags don't count, entities count as one character, an emoji is 2 UTF-16 units.
    assert tg_len("<b>a&amp;b</b>") == 3
    assert tg_len("🏡 x") == 4
