"""The contract for a source site.

A new site = one module in `sources/` implementing `Source`, plus a line in
`REGISTRY`. Everything else (dialog, worker, dedup, alerts) works with the
normalized `Listing`/`Slot` and knows nothing about the specific site.
"""

from __future__ import annotations

from typing import Protocol

from ..models import Country, Filter, Listing


class SourceError(Exception):
    """The source didn't respond as expected: status, format, request requirements."""


class Source(Protocol):
    key: str  # dedup key prefix and the value of `subscription.source`
    title: str  # human-readable name: dialog button, alert footer

    async def countries(self, continent: str) -> list[Country]:
        """Countries of the continent that have open sits right now, largest first."""
        ...

    async def search(self, flt: Filter, *, max_pages: int | None) -> list[Listing]:
        """Listings matching the filter, newest first. `None` means all pages."""
        ...
