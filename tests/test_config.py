from __future__ import annotations

import os
from datetime import timedelta

import pytest

from petalert.config import Settings, env_values, read_dotenv


def test_dotenv_is_read_without_touching_os_environ(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# коментар\nPETALERT_X=from-file\nPETALERT_Y='quoted'  # пояснення\n\n")
    monkeypatch.delenv("PETALERT_X", raising=False)

    assert read_dotenv(env) == {"PETALERT_X": "from-file", "PETALERT_Y": "quoted"}
    assert "PETALERT_X" not in os.environ


def test_process_environment_beats_the_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("PETALERT_X=from-file\n")
    monkeypatch.setenv("PETALERT_X", "from-env")

    assert env_values(env)["PETALERT_X"] == "from-env"


def test_settings_parse_types_and_defaults():
    s = Settings.load(
        {
            "BOT_TOKEN": "t",
            "ADMIN_USER_IDS": "1, 2,",
            "ADMIN_CHAT_ID": "-100",
            "POLL_INTERVAL_MIN": "15",
            "DB_PATH": "/tmp/x.sqlite3",
        }
    )
    assert s.admin_user_ids == frozenset({1, 2})
    assert s.admin_chat_id == -100
    assert s.poll_interval == timedelta(minutes=15)
    assert s.proxy_url is None and s.proxy_mode == "fallback"
    assert str(s.db_path) == "/tmp/x.sqlite3"
    assert s.ths_api_version == "2025-08-14"


def test_unknown_proxy_mode_is_rejected():
    with pytest.raises(ValueError):
        Settings.load({"PROXY_MODE": "sometimes"})
