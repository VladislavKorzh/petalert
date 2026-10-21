"""Towns from OpenStreetMap's Nominatim, over mocked HTTP: the request, the
answer, the cache and failures."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from petalert.geocode import SEARCH, USER_AGENT, GeocodeError, Geocoder, Town
from petalert.http import Http

# What Nominatim answered for "malaga" in Spain on 2026-10-21 (trimmed).
MALAGA = {
    "lat": "36.7647913", "lon": "-4.4422005", "category": "boundary", "type": "administrative",
    "addresstype": "municipality", "name": "Málaga",
    "display_name": "Málaga, Málaga-Costa del Sol, Malaga, Andalusia, Spain",
    "address": {"municipality": "Málaga", "state": "Andalusia", "country": "Spain", "country_code": "es"},
}


class Fake:
    def __init__(self, handler, *, proxy: str | None = None):
        self.requests: list[httpx.Request] = []
        self.proxied: list[bool] = []

        def factory(proxy_url):
            def record(request):
                self.requests.append(request)
                self.proxied.append(proxy_url is not None)
                return handler(request)

            return httpx.AsyncClient(transport=httpx.MockTransport(record))

        async def nosleep(_):
            return None

        self.http = Http(proxy, "always", min_interval=0, client_factory=factory, sleep=nosleep)
        self.geocoder = Geocoder(self.http)

    def run(self, coro_factory):
        async def go():
            try:
                return await coro_factory()
            finally:
                await self.http.aclose()

        return asyncio.run(go())


def test_the_best_match_in_english_and_its_country():
    fake = Fake(lambda request: httpx.Response(200, json=[MALAGA]))
    town = fake.run(lambda: fake.geocoder.town("malaga", "es"))
    assert town == Town("Málaga", "es", 36.7647913, -4.4422005)
    assert town.label == "Málaga, Spain"
    (request,) = fake.requests
    assert str(request.url).startswith(SEARCH) and request.headers["User-Agent"] == USER_AGENT
    assert dict(request.url.params) == {
        "q": "malaga", "format": "jsonv2", "limit": "1", "addressdetails": "1", "accept-language": "en",
        "countrycodes": "es",
    }


def test_each_town_is_looked_up_once_a_miss_too():
    fake = Fake(lambda request: httpx.Response(200, json=[MALAGA] if request.url.params["q"] == "Malaga" else []))

    async def go():
        first = await fake.geocoder.town("Malaga")
        again = await fake.geocoder.town("  malaga ")
        missing = [await fake.geocoder.town("malagaa"), await fake.geocoder.town("malagaa")]
        elsewhere = await fake.geocoder.town("malaga", "es")  # another question
        return first, again, missing, elsewhere

    first, again, missing, elsewhere = fake.run(go)
    assert first == again and missing == [None, None] and elsewhere is None
    assert [r.url.params["q"] for r in fake.requests] == ["Malaga", "malagaa", "malaga"]


def test_a_country_of_its_own_name_is_said_once():
    san_marino = {**MALAGA, "name": "San Marino", "address": {"country": "San Marino", "country_code": "sm"}}
    fake = Fake(lambda request: httpx.Response(200, json=[san_marino]))
    assert fake.run(lambda: fake.geocoder.town("san marino")).label == "San Marino"


@pytest.mark.parametrize(
    "response, error",
    [
        (httpx.Response(503, text="overloaded"), "HTTP 503: overloaded"),
        (httpx.Response(200, text="<html>a new design</html>"), "answer changed"),
        (httpx.Response(200, json=[{"name": "Málaga"}]), "answer changed"),  # no coordinates
    ],
)
def test_a_failed_lookup_is_an_error_not_a_miss(response, error):
    fake = Fake(lambda request: response)
    with pytest.raises(GeocodeError, match=error):
        fake.run(lambda: fake.geocoder.town("malaga"))


def test_a_transport_failure_is_an_error_too():
    def down(request):
        raise httpx.ConnectError("no route", request=request)

    fake = Fake(down)
    with pytest.raises(GeocodeError, match="ConnectError"):
        fake.run(lambda: fake.geocoder.town("malaga"))


def test_goes_direct_even_when_the_proxy_is_always_on():
    fake = Fake(lambda request: httpx.Response(200, json=[MALAGA]), proxy="http://proxy:1")
    assert fake.run(lambda: fake.geocoder.town("malaga")) and fake.proxied == [False]
