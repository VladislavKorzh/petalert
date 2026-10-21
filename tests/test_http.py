from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from curl_cffi.requests.exceptions import RequestException

from petalert import http as http_module
from petalert.http import Http

URL = "https://example.com/api"


def make_http(statuses: list[int | Exception], *, proxy_url=None, mode="fallback"):
    """Http with a mocked transport: returns statuses in order, records whether the proxy was used."""
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


def test_a_request_can_ask_for_fallback_whatever_the_setting():
    http, calls, _ = make_http([503, 200], proxy_url="http://proxy:1", mode="always")

    async def run():
        try:
            return await http.get(URL, proxy_mode="fallback")
        finally:
            await http.aclose()

    assert asyncio.run(run()).status_code == 200
    assert calls == [False, True]


def test_a_request_can_go_as_a_browsers():
    used: list[str] = []

    def factory(kind):
        def make(proxy):
            def handler(request):
                used.append(f"{kind}{'+proxy' if proxy else ''}")
                return httpx.Response(403 if len(used) == 1 else 200)

            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        return make

    async def nosleep(_):
        return None

    http = Http("http://proxy:1", min_interval=0, client_factory=factory("plain"),
                browser_factory=factory("browser"), sleep=nosleep)

    async def run():
        try:
            await http.get(URL, browser=True)  # 403, then through the proxy
            await http.get(URL)
        finally:
            await http.aclose()

    asyncio.run(run())
    assert used == ["browser", "browser+proxy", "plain"]


class FakeSession:
    """curl_cffi's AsyncSession as BrowserTransport uses it."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.sent: list[tuple] = []
        self.closed = False
        self.fail: Exception | None = None

    async def request(self, method, url, *, headers, content):
        self.sent.append((method, url, headers, content))
        if self.fail:
            raise self.fail
        return SimpleNamespace(
            status_code=200,
            headers=httpx.Headers({"Content-Type": "application/json", "Content-Encoding": "br", "Content-Length": "9"}),
            content=b'{"a": 1}',  # curl has decoded it
        )

    async def close(self):
        self.closed = True


def test_browser_transport_lets_curl_be_chrome_and_hands_back_the_decoded_body(monkeypatch):
    sessions: list[FakeSession] = []
    monkeypatch.setattr(http_module, "AsyncSession", lambda **kw: sessions.append(FakeSession(**kw)) or sessions[-1])

    async def run():
        async with http_module._browser_client("http://proxy:1") as client:
            resp = await client.get(URL, params={"q": "1"}, headers={"X-Requested-With": "XMLHttpRequest"})
            sessions[0].fail = RequestException("timed out")
            with pytest.raises(httpx.TransportError):
                await client.get(URL)
            return resp

    resp = asyncio.run(run())
    (session,) = sessions
    assert session.kwargs["impersonate"] == "chrome" and session.kwargs["proxy"] == "http://proxy:1"
    method, url, headers, content = session.sent[0]
    assert (method, url, content) == ("GET", URL + "?q=1", None)
    names = {k.lower() for k, _ in headers}
    assert "x-requested-with" in names and names.isdisjoint({"accept", "user-agent", "accept-encoding", "host"})
    assert resp.json() == {"a": 1} and "content-encoding" not in resp.headers
    assert session.closed
