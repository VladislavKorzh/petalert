"""HTTP to source sites: retries, backoff, proxy as a fallback, politeness.

By default the proxy is only a fallback: THS serves the API directly, and every
request through a residential pool costs traffic. So the first attempt goes
direct, and retries after 403/429/5xx go through the proxy if one is set
(`fallback`). `always` is for when the server's own IP is already blocked
(THS blocks datacenter ranges such as DigitalOcean). A source that isn't
blocked can ask for `fallback` per request, whatever the setting.

A request can also ask to go as a browser's (`browser=True`): curl_cffi then
sends it with Chrome's TLS handshake. Some sites (Nomador, behind Cloudflare)
challenge Python's own handshake whatever the IP, so a proxy is no help there.

One instance per process, and all requests (worker ticks and the /new dialog)
share a lock: at most one request every `min_interval` seconds.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Literal

import httpx
from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException

log = logging.getLogger(__name__)

RETRYABLE = frozenset({403, 429, 500, 502, 503, 504})
ATTEMPTS = 3
BACKOFF = (2.0, 6.0)  # seconds before the 2nd and 3rd attempt (plus jitter)
TIMEOUT_SECONDS = 30.0
TIMEOUT = httpx.Timeout(TIMEOUT_SECONDS)
# Request headers curl sets itself: its browser's own, or the connection's.
_CURL_SETS = frozenset({"host", "connection", "accept-encoding", "content-length", "user-agent", "cookie"})
# Response headers about the body as sent; curl has already decoded it.
_DECODED = frozenset({"content-encoding", "content-length", "transfer-encoding"})

ClientFactory = Callable[[str | None], httpx.AsyncClient]


class BrowserTransport(httpx.AsyncBaseTransport):
    """httpx requests sent by curl_cffi the way Chrome sends them: its TLS
    handshake, HTTP/2 settings, User-Agent and cookies."""

    def __init__(self, proxy: str | None):
        self._session = AsyncSession(impersonate="chrome", proxy=proxy, timeout=TIMEOUT_SECONDS)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        headers = [(k, v) for k, v in request.headers.multi_items() if k.lower() not in _CURL_SETS]
        try:
            resp = await self._session.request(
                request.method, str(request.url), headers=headers, content=request.content or None
            )
        except RequestException as exc:
            raise httpx.TransportError(repr(exc), request=request) from exc
        return httpx.Response(
            resp.status_code,
            headers=[(k, v) for k, v in resp.headers.multi_items() if k.lower() not in _DECODED],
            content=resp.content,
            request=request,
        )

    async def aclose(self) -> None:
        await self._session.close()


def _default_client(proxy: str | None) -> httpx.AsyncClient:
    return httpx.AsyncClient(proxy=proxy, timeout=TIMEOUT)


def _browser_client(proxy: str | None) -> httpx.AsyncClient:
    client = httpx.AsyncClient(transport=BrowserTransport(proxy), timeout=TIMEOUT)
    # httpx's default "Accept: */*" gives a script away (Nomador's Cloudflare
    # challenges it, 2026-10-21); without it curl sends Chrome's.
    del client.headers["Accept"]
    return client


class Http:
    def __init__(
        self,
        proxy_url: str | None = None,
        proxy_mode: Literal["fallback", "always"] = "fallback",
        *,
        min_interval: float = 2.0,
        client_factory: ClientFactory = _default_client,
        browser_factory: ClientFactory = _browser_client,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.proxy_url = proxy_url
        self.proxy_mode = proxy_mode
        self.min_interval = min_interval
        self._factories = {False: client_factory, True: browser_factory}
        self._sleep = sleep
        self._clients: dict[tuple[bool, bool], httpx.AsyncClient] = {}
        self._lock = asyncio.Lock()
        self._last = 0.0

    def _client(self, via_proxy: bool, browser: bool) -> httpx.AsyncClient:
        key = (via_proxy, browser)
        if key not in self._clients:
            self._clients[key] = self._factories[browser](self.proxy_url if via_proxy else None)
        return self._clients[key]

    async def _send(self, via_proxy: bool, browser: bool, url: str, params, headers) -> httpx.Response:
        async with self._lock:
            wait = self._last + self.min_interval - time.monotonic()
            if wait > 0:
                await self._sleep(wait)
            try:
                return await self._client(via_proxy, browser).get(url, params=params, headers=headers)
            finally:
                self._last = time.monotonic()

    async def get(
        self,
        url: str,
        *,
        params=None,
        headers=None,
        proxy_mode: Literal["fallback", "always"] | None = None,
        browser: bool = False,
    ) -> httpx.Response:
        """Return the last response (any status — the caller interprets it).

        Only 403/429/5xx and transport failures are retried; if every attempt
        failed at the transport level, the last `httpx.TransportError` is raised.
        `proxy_mode` overrides the instance's mode for this request; `browser`
        sends it the way Chrome would (BrowserTransport).
        """
        mode = proxy_mode or self.proxy_mode
        result: httpx.Response | httpx.TransportError | None = None
        for attempt in range(ATTEMPTS):
            if attempt:
                await self._sleep(BACKOFF[attempt - 1] * random.uniform(1.0, 1.3))
            via_proxy = bool(self.proxy_url) and (mode == "always" or attempt > 0)
            try:
                result = await self._send(via_proxy, browser, url, params, headers)
            except httpx.TransportError as exc:
                log.warning("GET %s (proxy=%s): %r", url, via_proxy, exc)
                result = exc
                continue
            if result.status_code not in RETRYABLE:
                return result
            log.warning("GET %s (proxy=%s): HTTP %s", url, via_proxy, result.status_code)
        assert result is not None
        if isinstance(result, httpx.TransportError):
            raise result
        return result

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()
