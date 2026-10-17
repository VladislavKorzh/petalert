"""HTTP до сайтів-джерел: ретраї, backoff, проксі як запасний шлях, ввічливість.

Проксі за замовчуванням — лише запасний шлях: THS віддає API напряму, і кожен
запит через резидентний пул коштує трафік. Тому перша спроба йде напряму, а
повторні після 403/429/5xx — через проксі, якщо він заданий (`fallback`).
`always` — для випадку, коли прямий IP дроплета вже заблокували.

Один екземпляр на процес, і всі запити (тік воркера й діалог /new) проходять
через спільний lock: не частіше одного запиту на `min_interval` секунд.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Literal

import httpx

log = logging.getLogger(__name__)

RETRYABLE = frozenset({403, 429, 500, 502, 503, 504})
ATTEMPTS = 3
BACKOFF = (2.0, 6.0)  # секунди перед 2-ю і 3-ю спробою (плюс jitter)
TIMEOUT = httpx.Timeout(30.0)

ClientFactory = Callable[[str | None], httpx.AsyncClient]


def _default_client(proxy: str | None) -> httpx.AsyncClient:
    return httpx.AsyncClient(proxy=proxy, timeout=TIMEOUT)


class Http:
    def __init__(
        self,
        proxy_url: str | None = None,
        proxy_mode: Literal["fallback", "always"] = "fallback",
        *,
        min_interval: float = 2.0,
        client_factory: ClientFactory = _default_client,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.proxy_url = proxy_url
        self.proxy_mode = proxy_mode
        self.min_interval = min_interval
        self._factory = client_factory
        self._sleep = sleep
        self._clients: dict[bool, httpx.AsyncClient] = {}
        self._lock = asyncio.Lock()
        self._last = 0.0

    def _client(self, via_proxy: bool) -> httpx.AsyncClient:
        if via_proxy not in self._clients:
            self._clients[via_proxy] = self._factory(self.proxy_url if via_proxy else None)
        return self._clients[via_proxy]

    async def _send(self, via_proxy: bool, url: str, params, headers) -> httpx.Response:
        async with self._lock:
            wait = self._last + self.min_interval - time.monotonic()
            if wait > 0:
                await self._sleep(wait)
            try:
                return await self._client(via_proxy).get(url, params=params, headers=headers)
            finally:
                self._last = time.monotonic()

    async def get(self, url: str, *, params=None, headers=None) -> httpx.Response:
        """Повертає останню відповідь (будь-який статус — розбирає викликач).

        Ретраїть лише 403/429/5xx і транспортні збої; якщо всі спроби впали на
        транспорті, піднімає останній `httpx.TransportError`.
        """
        result: httpx.Response | httpx.TransportError | None = None
        for attempt in range(ATTEMPTS):
            if attempt:
                await self._sleep(BACKOFF[attempt - 1] * random.uniform(1.0, 1.3))
            via_proxy = bool(self.proxy_url) and (self.proxy_mode == "always" or attempt > 0)
            try:
                result = await self._send(via_proxy, url, params, headers)
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
