from __future__ import annotations

from aiogram import Dispatcher

from ..db import Store
from ..geocode import Geocoder
from ..scheduler import Worker
from . import browse, dialog, manage, search
from .access import AccessGate


def build_dispatcher(
    store: Store,
    sources: dict,
    worker: Worker,
    admins: frozenset[int],
    geocoder: Geocoder,
    *,
    alerts: bool = True,
) -> Dispatcher:
    # store/sources/worker/geocoder/alerts reach handlers as workflow data
    # (matched by argument name); `searching`: chats with a /search running.
    # `alerts` off (ALERTS=off): the handlers say so instead of promising alerts.
    dp = Dispatcher(
        store=store, sources=sources, worker=worker, geocoder=geocoder, alerts=alerts, searching=set()
    )
    dp.update.outer_middleware(AccessGate(store, admins, alerts=alerts))
    # Order matters: commands before the dialog's text steps, plain text as a
    # search after them, the catch-all last.
    commands, fallback = manage.build_routers()
    dp.include_routers(commands, browse.build_router(), dialog.build_router(), search.build_router(), fallback)
    return dp
