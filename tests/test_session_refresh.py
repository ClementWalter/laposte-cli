"""Browser imports use current cookies without changing the configured account."""

import json
from types import SimpleNamespace

import pytest


@pytest.fixture(params=["cookie_header", "cookies"])
def saved_session(app, monkeypatch, request):
    config = {"browser": "chrome", "userId": "123", request.param: "session=old"}
    monkeypatch.setattr(app, "_auth_broker", lambda *args: config)
    return config


@pytest.fixture
def browser_jar(app, monkeypatch):
    jar = [
        SimpleNamespace(name="pa_user", value=json.dumps({"id": "123"})),
        SimpleNamespace(name="session", value="fresh"),
        SimpleNamespace(name="lpel_cel", value="current-draft"),
    ]
    monkeypatch.setattr(app, "get_browser_jar", lambda *args: jar)
    return jar


def test_browser_import_uses_current_cookies(app, saved_session, browser_jar):
    assert app.require_login()["cookies"] is browser_jar


def test_browser_import_reads_current_draft(app, saved_session, browser_jar):
    assert app.require_login()["sendingId"] == "current-draft"


@pytest.mark.parametrize("error", [OSError("unavailable"), ValueError("unreadable")])
def test_browser_import_falls_back_when_unavailable(
    app, monkeypatch, saved_session, error
):
    def get_jar(*args):
        raise error

    monkeypatch.setattr(app, "get_browser_jar", get_jar)
    assert app.require_login()["cookies"] == "session=old"


def test_browser_import_falls_back_when_logged_out(app, saved_session, browser_jar):
    browser_jar.clear()
    assert app.require_login()["cookies"] == "session=old"


def test_browser_import_preserves_configured_account(app, saved_session, browser_jar):
    browser_jar[0].value = json.dumps({"id": "different-account"})
    assert app.require_login()["cookies"] == "session=old"
