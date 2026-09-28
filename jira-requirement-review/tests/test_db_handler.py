"""db_handler: email -> UUID resolution against public."user", with a hit-only cache."""

from __future__ import annotations

import pytest

from utils import db_handler


@pytest.fixture(autouse=True)
def _clear_cache():
    db_handler._user_id_by_email_cache.clear()
    yield
    db_handler._user_id_by_email_cache.clear()


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class FakeSession:
    def __init__(self, rows):
        self._rows = rows
        self.closed = False

    def execute(self, stmt, params):
        return FakeResult(self._rows)

    def close(self):
        self.closed = True


def test_resolves_and_caches(monkeypatch):
    session = FakeSession([("a@example.com", "uuid-a"), ("b@example.com", "uuid-b")])
    monkeypatch.setattr(db_handler.db, "get_session", lambda: session)

    ids = db_handler.get_user_ids_by_emails(["a@example.com", "b@example.com"])
    assert set(ids) == {"uuid-a", "uuid-b"}
    assert session.closed is True

    def _boom():
        raise AssertionError("should use cache, not query again")

    monkeypatch.setattr(db_handler.db, "get_session", _boom)
    assert db_handler.get_user_ids_by_emails(["a@example.com"]) == ["uuid-a"]


def test_unknown_email_is_dropped_not_fatal(monkeypatch):
    session = FakeSession([("a@example.com", "uuid-a")])
    monkeypatch.setattr(db_handler.db, "get_session", lambda: session)
    ids = db_handler.get_user_ids_by_emails(["a@example.com", "missing@example.com"])
    assert ids == ["uuid-a"]


def test_empty_input_short_circuits(monkeypatch):
    def _boom():
        raise AssertionError("should not touch DB for empty input")

    monkeypatch.setattr(db_handler.db, "get_session", _boom)
    assert db_handler.get_user_ids_by_emails([]) == []


def test_db_error_degrades_to_cached_results_only(monkeypatch):
    class RaisingSession:
        def execute(self, *a, **k):
            raise RuntimeError("db down")

        def close(self):
            pass

    monkeypatch.setattr(db_handler.db, "get_session", lambda: RaisingSession())
    assert db_handler.get_user_ids_by_emails(["a@example.com"]) == []


def test_duplicates_are_deduplicated(monkeypatch):
    session = FakeSession([("a@example.com", "uuid-a")])
    monkeypatch.setattr(db_handler.db, "get_session", lambda: session)
    ids = db_handler.get_user_ids_by_emails(["a@example.com", "a@example.com", " a@example.com "])
    assert ids == ["uuid-a"]
