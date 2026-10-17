from __future__ import annotations

from collections.abc import Callable

from ..config import Settings
from ..http import Http
from .base import Source, SourceError
from .ths import Notice, TrustedHousesitters

Factory = Callable[[Http, Settings, "Notice | None"], Source]

# A new site = its module plus one line here. Order = button order in /new.
REGISTRY: dict[str, Factory] = {
    "ths": lambda http, settings, notice: TrustedHousesitters(
        http, settings.ths_api_version, notice=notice
    ),
}


def build_sources(http: Http, settings: Settings, notice: Notice | None = None) -> dict[str, Source]:
    return {key: factory(http, settings, notice) for key, factory in REGISTRY.items()}


__all__ = ["REGISTRY", "Source", "SourceError", "build_sources"]
