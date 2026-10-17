"""Process config: `.env`, overridden by the real environment.

The `.env` reader deliberately does NOT write to `os.environ`: otherwise values
from the file would stick in the process environment for good and shadow later
edits to `.env`.
"""

from __future__ import annotations

import os
import pathlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_THS_API_VERSION = "2025-08-14"


def read_dotenv(path: pathlib.Path) -> dict[str, str]:
    """Minimal .env reader. The process environment is NOT modified."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        # Inline comment after the value: `KEY=value  # explanation`.
        value = value.split(" #", 1)[0]
        values.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return values


def env_values(path: pathlib.Path | None = None) -> dict[str, str]:
    """`.env` overridden by the real environment: an environment variable beats the file."""
    values = read_dotenv(path) if path is not None else {}
    values.update(os.environ)
    return values


def _ids(raw: str) -> frozenset[int]:
    return frozenset(int(part) for part in raw.replace(" ", "").split(",") if part)


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_user_ids: frozenset[int]
    admin_chat_id: int | None
    proxy_url: str | None
    proxy_mode: Literal["fallback", "always"]
    poll_interval: timedelta
    db_path: pathlib.Path
    ths_api_version: str

    @classmethod
    def load(cls, values: Mapping[str, str] | None = None) -> "Settings":
        v = env_values(ROOT / ".env") if values is None else values
        mode = v.get("PROXY_MODE") or "fallback"
        if mode not in ("fallback", "always"):
            raise ValueError(f"PROXY_MODE must be fallback or always, not {mode!r}")
        admin_chat = v.get("ADMIN_CHAT_ID") or ""
        db_path = pathlib.Path(v.get("DB_PATH") or "var/petalert.sqlite3")
        return cls(
            bot_token=v.get("BOT_TOKEN") or "",
            admin_user_ids=_ids(v.get("ADMIN_USER_IDS") or ""),
            admin_chat_id=int(admin_chat) if admin_chat else None,
            proxy_url=v.get("PROXY_URL") or None,
            proxy_mode=mode,  # type: ignore[arg-type]
            poll_interval=timedelta(minutes=float(v.get("POLL_INTERVAL_MIN") or 30)),
            db_path=db_path if db_path.is_absolute() else ROOT / db_path,
            ths_api_version=v.get("THS_API_VERSION") or DEFAULT_THS_API_VERSION,
        )
