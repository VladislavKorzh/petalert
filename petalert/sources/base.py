"""The contract for a source site.

A new site = one module in `sources/` implementing `Source`, plus a factory
line in `REGISTRY` (sources/__init__.py). Everything else (dialog, worker,
dedup, alerts) works with the normalized `Listing`/`Slot` and knows nothing
about the specific site.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol

from ..models import Country, Filter, Listing

# Tells the admin about something the source fixed or noticed on its own.
Notice = Callable[[str], Awaitable[None]]


class SourceError(Exception):
    """The source didn't respond as expected: status, format, request requirements."""


class TooBroad(SourceError):
    """A search the site can't answer in full; the message is for the user."""


class Source(Protocol):
    key: str  # the site's id: slot-key prefix, `watch`/`chat_source`/`query_state`.source
    title: str  # human-readable name: /sites button, alert footer
    # Optional: `always_complete = True` if every search returns all matches
    # whatever max_pages (one request is the whole site): every tick is then a
    # full crawl and the worker schedules none.

    async def countries(self, continent: str) -> list[Country]:
        """Countries of the continent that have open sits right now, largest first."""
        ...

    async def search(self, flt: Filter, *, max_pages: int | None) -> list[Listing]:
        """Listings matching the filter, newest first.

        `max_pages=None` means everything (no cap). Otherwise it's a lower bound
        on effort: at least the newest `max_pages` pages, and a source may
        return more when fetching everything is cheap.
        """
        ...

    def by_start(self, flt: Filter) -> AsyncIterator[list[Listing]]:
        """For /search: listings in the filter's place with an open slot
        starting on or after `flt.date_from`, ordered by the first such start
        (nearest first), a page at a time. The caller stops reading once it is
        past its dates, and filters locally: a page may hold more than asked
        (another country of the continent, say). An async generator."""
        ...
