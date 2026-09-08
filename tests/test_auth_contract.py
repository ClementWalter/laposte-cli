"""Credential broker contract tests use synthetic sessions without provider access."""
import subprocess
from types import SimpleNamespace

import pytest
from click.testing import CliRunner


def test_load_prefers_broker(app, monkeypatch):
    monkeypatch.setattr(app, "_auth_broker", lambda *args: {"api_id": 1, "api_hash": "vault"})
    assert app.load_config() == {"api_id": 1, "api_hash": "vault"}

def test_load_legacy_when_broker_unavailable(app, monkeypatch):
    monkeypatch.setattr(app, "_auth_broker", lambda *args: None)
    app.CONFIG_FILE.write_text('{"api_id": 2, "api_hash": "local"}')
    assert app.load_config() == {"api_id": 2, "api_hash": "local"}

@pytest.mark.parametrize("code", [0, 3])
def test_save_body_uses_stdin(app, monkeypatch, code):
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs.get("input")))
        return SimpleNamespace(returncode=code, stdout='{"pending": false}')
    monkeypatch.setattr(subprocess, "run", run)
    app._auth_broker("save", {"password": "synthetic-secret"})
    assert calls == [(["claudine-secret", "auth", "save", "laposte"], '{"password": "synthetic-secret"}')]

def test_status_never_emits_credentials(app, monkeypatch):
    monkeypatch.setattr(app, "_auth_broker", lambda *args: None)
    app.CONFIG_FILE.write_text('{"password": "synthetic-secret"}')
    result = CliRunner().invoke(app.cli, ["auth-status", "--json"])
    assert "synthetic-secret" not in result.output

def test_sync_pending_is_retryable(app, monkeypatch):
    monkeypatch.setattr(app, "_auth_broker", lambda *args: {"configured": True, "pending": True})
    assert CliRunner().invoke(app.cli, ["auth-sync"]).exit_code == 3


def test_vault_session_needs_no_browser(app, monkeypatch):
    monkeypatch.setattr(app, "_auth_broker", lambda *args: {"cookie_header": "session=synthetic", "userId": "123"})
    monkeypatch.setattr(app, "get_browser_jar", lambda *args: pytest.fail("browser access"))
    assert app.require_login()["cookies"] == "session=synthetic"


def test_shared_cookie_session_needs_no_browser(app, monkeypatch):
    monkeypatch.setattr(app, "_auth_broker", lambda *args: {"cookies": "session=synthetic", "userId": "123"})
    monkeypatch.setattr(app, "get_browser_jar", lambda *args: pytest.fail("browser access"))
    assert app.require_login()["cookies"] == "session=synthetic"


def test_whoami_accepts_shared_cookie_session(app, monkeypatch):
    monkeypatch.setattr(app, "fetch_sender_addresses", lambda session: [])
    monkeypatch.setattr(app, "_auth_broker", lambda *args: {"cookies": "session=synthetic", "userId": "123"})
    monkeypatch.setattr(app, "get_browser_jar", lambda *args: pytest.fail("browser access"))
    assert CliRunner().invoke(app.cli, ["whoami"]).exit_code == 0
