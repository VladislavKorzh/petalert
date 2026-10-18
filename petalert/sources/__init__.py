from __future__ import annotations

from collections.abc import Callable

from ..config import Settings
from ..http import Http
from .base import Notice, Source, SourceError
from .mmh import MindMyHouse
from .ths import TrustedHousesitters

Factory = Callable[[Http, Settings, "Notice | None"], Source]

# A new site = its module plus one line here. Every subscription watches it
# unless the user turns it off in /sites. Order = order in /sites.
REGISTRY: dict[str, Factory] = {
    "ths": lambda http, settings, notice: TrustedHousesitters(
        http, settings.ths_api_version, notice=notice
    ),
    "mmh": lambda http, settings, notice: MindMyHouse(http, notice=notice),
}


def build_sources(http: Http, settings: Settings, notice: Notice | None = None) -> dict[str, Source]:
    return {key: factory(http, settings, notice) for key, factory in REGISTRY.items()}


__all__ = ["REGISTRY", "Source", "SourceError", "build_sources"]
