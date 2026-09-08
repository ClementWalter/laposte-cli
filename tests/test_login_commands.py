"""Authentication commands validate access and respect explicit local logout."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from click.testing import CliRunner


@pytest.fixture
def auth_state(app, monkeypatch):
    state = {"config": {"cookies": "session=old", "userId": "123"}, "pending": False}

    def broker(action, payload=None):
        if action == "load":
            return state["config"]
        if action == "save":
            state["config"] = payload
        return {"configured": True, "pending": state["pending"]}

    monkeypatch.setattr(app, "_auth_broker", broker)
    monkeypatch.setattr(
        app, "get_browser_cookies", lambda browser: ("session=fresh", "123")
    )
    monkeypatch.setattr(app, "get_browser_jar", lambda browser: [])
    return state


@pytest.fixture
def validation(app, monkeypatch):
    state = {"allowed": True, "calls": 0}

    def validate(session):
        state["calls"] += 1
        if not state["allowed"]:
            raise app.click.ClickException("Session rejected")
        return []

    monkeypatch.setattr(app, "fetch_sender_addresses", validate)
    return state


def test_login_validates_before_saving(app, auth_state, validation):
    validation["allowed"] = False
    CliRunner().invoke(app.cli, ["login"])
    assert auth_state["config"]["cookies"] == "session=old"


def test_login_rejection_is_reported(app, auth_state, validation):
    validation["allowed"] = False
    result = CliRunner().invoke(app.cli, ["login"])
    assert (result.exit_code, result.output) == (1, "Error: Session rejected\n")


def test_login_json_reports_pending_sync(app, auth_state, validation):
    auth_state["pending"] = True
    result = CliRunner().invoke(app.cli, ["login", "--json"])
    assert json.loads(result.output) == {
        "authenticated": True,
        "user_id": "123",
        "browser": "chrome",
        "sync_pending": True,
    }


def test_whoami_checks_server(app, auth_state, validation):
    validation["allowed"] = False
    result = CliRunner().invoke(app.cli, ["whoami"])
    assert (result.exit_code, result.output) == (1, "Error: Session rejected\n")


def test_whoami_json_contains_only_identity(app, auth_state, validation):
    result = CliRunner().invoke(app.cli, ["whoami", "--json"])
    assert json.loads(result.output) == {
        "authenticated": True,
        "user_id": "123",
        "browser": "chrome",
    }


def test_logout_blocks_saved_session(app, auth_state, monkeypatch):
    CliRunner().invoke(app.cli, ["logout"])
    monkeypatch.setattr(
        app, "_auth_broker", lambda *args: pytest.fail("broker access after logout")
    )
    result = CliRunner().invoke(app.cli, ["whoami"])
    assert (result.exit_code, result.output) == (
        1,
        "Error: Not logged in. Run 'laposte login'.\n",
    )


@pytest.mark.parametrize("suffix", [".json", ".auth-pending"])
def test_logout_removes_local_credentials(app, auth_state, suffix):
    path = app.CONFIG_FILE.with_suffix(suffix)
    path.write_text("synthetic-secret")
    CliRunner().invoke(app.cli, ["logout"])
    assert not path.exists()


def test_logout_is_idempotent(app, auth_state):
    CliRunner().invoke(app.cli, ["logout"])
    result = CliRunner().invoke(app.cli, ["logout", "--json"])
    assert (result.exit_code, json.loads(result.output)) == (
        0,
        {"authenticated": False},
    )


def test_auth_status_respects_logout(app, auth_state):
    CliRunner().invoke(app.cli, ["logout"])
    result = CliRunner().invoke(app.cli, ["auth-status", "--json"])
    assert json.loads(result.output)["configured"] is False


def test_auth_sync_cannot_restore_logged_out_session(app, auth_state):
    CliRunner().invoke(app.cli, ["logout"])
    result = CliRunner().invoke(app.cli, ["auth-sync"])
    assert (result.exit_code, result.output) == (
        1,
        "Error: Not logged in. Run 'laposte login'.\n",
    )


def test_failed_login_preserves_logout(app, auth_state, validation):
    CliRunner().invoke(app.cli, ["logout"])
    validation["allowed"] = False
    CliRunner().invoke(app.cli, ["login"])
    assert app.load_config() == {}


def test_pending_login_retains_local_sync_marker(app, auth_state, validation):
    auth_state["pending"] = True
    CliRunner().invoke(app.cli, ["login"])
    assert app.CONFIG_FILE.with_suffix(".auth-pending").exists()


def test_pending_auth_sync_retains_local_login(app, auth_state, validation):
    auth_state["pending"] = True
    CliRunner().invoke(app.cli, ["login"])
    CliRunner().invoke(app.cli, ["auth-sync"])
    assert app.CONFIG_FILE.with_suffix(".auth-pending").exists()


def test_first_use_requires_login(app, monkeypatch):
    monkeypatch.setattr(app, "_auth_broker", lambda *args: None)
    monkeypatch.setattr(
        app, "get_browser_jar", lambda *args: pytest.fail("implicit browser login")
    )
    result = CliRunner().invoke(app.cli, ["whoami"])
    assert (result.exit_code, result.output) == (
        1,
        "Error: Not logged in. Run 'laposte login'.\n",
    )


@pytest.fixture
def provider(app, monkeypatch):
    """Exercise real HTTP validation without using the live account."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, format, *args):
            # Provider diagnostics must not interfere with CLI output assertions.
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(
        app, "ADDRESSES_URL", f"http://127.0.0.1:{server.server_port}/addresses"
    )
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_login_logout_login_lifecycle(app, auth_state, provider):
    runner = CliRunner()
    first = runner.invoke(app.cli, ["login"])
    identity = runner.invoke(app.cli, ["whoami"])
    logout = runner.invoke(app.cli, ["logout"])
    logged_out = runner.invoke(app.cli, ["whoami"])
    login = runner.invoke(app.cli, ["login"])
    restored = runner.invoke(app.cli, ["whoami"])
    assert (
        first.exit_code,
        identity.exit_code,
        logout.exit_code,
        logged_out.exit_code,
        login.exit_code,
        restored.exit_code,
    ) == (0, 0, 0, 1, 0, 0)
