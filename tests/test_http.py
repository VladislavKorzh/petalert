from __future__ import annotations

import asyncio

import httpx
import pytest

from petalert.http import Http

URL = "https://example.com/api"


def make_http(statuses: list[int | Exception], *, proxy_url=None, mode="fallback"):
    """Http із мокнутим транспортом: віддає статуси по черзі, пише, чи йшов через проксі."""
    calls: list[bool] = []
    sleeps: list[float] = []
    queue = list(statuses)

    def factory(proxy):
        def handler(request):
            calls.append(proxy is not None)
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return httpx.Response(item, text="body")

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def sleep(seconds):
        sleeps.append(seconds)

    http = Http(proxy_url, mode, min_interval=0, client_factory=factory, sleep=sleep)
    return http, calls, sleeps


def get(http: Http) -> httpx.Response:
    async def run():
        try:
            return await http.get(URL)
        finally:
            await http.aclose()

    return asyncio.run(run())


def test_ok_goes_direct_once():
    http, calls, sleeps = make_http([200], proxy_url="http://proxy:1")
    assert get(http).status_code == 200
    assert calls == [False] and sleeps == []


def test_403_direct_then_proxy():
    http, calls, sleeps = make_http([403, 200], proxy_url="http://proxy:1")
    assert get(http).status_code == 200
    assert calls == [False, True]
    assert len(sleeps) == 1 and sleeps[0] >= 2


def test_without_proxy_retries_direct_and_returns_last_response():
    http, calls, sleeps = make_http([429, 503, 429])
    assert get(http).status_code == 429
    assert calls == [False, False, False]
    assert len(sleeps) == 2 and sleeps[1] >= 6


def test_always_mode_uses_proxy_from_the_first_attempt():
    http, calls, _ = make_http([200], proxy_url="http://proxy:1", mode="always")
    get(http)
    assert calls == [True]


def test_non_retryable_status_is_returned_immediately():
    http, calls, _ = make_http([406], proxy_url="http://proxy:1")
    assert get(http).status_code == 406
    assert calls == [False]


def test_transport_errors_retry_and_finally_raise():
    boom = httpx.ConnectError("down")
    http, calls, _ = make_http([boom, boom, boom], proxy_url="http://proxy:1")
    with pytest.raises(httpx.ConnectError):
        get(http)
    assert calls == [False, True, True]
