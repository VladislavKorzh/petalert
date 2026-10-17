from __future__ import annotations

from ..config import Settings
from ..http import Http
from .base import Source, SourceError
from .ths import Notice, TrustedHousesitters

# Порядок = порядок кнопок у діалозі /new.
REGISTRY = {"ths": TrustedHousesitters}


def build_sources(http: Http, settings: Settings, notice: Notice | None = None) -> dict[str, Source]:
    return {"ths": TrustedHousesitters(http, settings.ths_api_version, notice=notice)}


__all__ = ["REGISTRY", "Source", "SourceError", "build_sources"]
