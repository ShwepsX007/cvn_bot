"""Тесты не запускают Telegram polling, планировщик и реальные сетевые запросы."""

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("BOT_TOKEN", "0:test")


@pytest.fixture(scope="session", autouse=True)
def offline_lifespan():
    import web_app

    original = web_app.app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        yield

    web_app.app.router.lifespan_context = lifespan
    yield
    web_app.app.router.lifespan_context = original


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    import database as db

    monkeypatch.setattr(db, "DB_NAME", str(tmp_path / "test.db"))
    db.init_db()
    return db
