"""Address removal requires an exact ID and verifies the server's resulting state."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace
from urllib.parse import unquote

import pytest
from click.testing import CliRunner


@pytest.mark.parametrize("status", [200, 204, 404])
def test_delete_uses_encoded_id(app, status):
    calls = []

    def delete(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=status)

    app.delete_sender_address(SimpleNamespace(delete=delete), "P-example/1")
    assert calls == [(app.ADDRESSES_URL + "/P-example%2F1", {"timeout": 15})]


@pytest.mark.parametrize("status", [401, 403, 500])
def test_delete_rejection_is_actionable(app, status):
    session = SimpleNamespace(
        delete=lambda *args, **kwargs: SimpleNamespace(status_code=status)
    )
    with pytest.raises(app.click.ClickException) as error:
        app.delete_sender_address(session, "P-old")
    assert str(error.value) == f"Could not remove address P-old (HTTP {status})."


def test_delete_connection_failure_does_not_claim_success(app):
    def delete(*args, **kwargs):
        raise app.requests.RequestsError("synthetic-secret")

    with pytest.raises(app.click.ClickException) as error:
        app.delete_sender_address(SimpleNamespace(delete=delete), "P-old")
    assert (
        str(error.value)
        == "Could not confirm removal of P-old. Run 'laposte addresses --json' before retrying."
    )


@pytest.fixture
def address_book(app, monkeypatch):
    state = {
        "items": {
            "P-old": {
                "postalId": "P-old",
                "label": "Old home",
                "address": {"line4": "1 OLD STREET"},
            },
            "P-current": {"postalId": "P-current", "label": "Home", "isPrimary": True},
        },
        "deleted": [],
        "retain": False,
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(state["items"]).encode())

        def do_DELETE(self):
            address_id = unquote(self.path.rsplit("/", 1)[-1])
            state["deleted"].append(address_id)
            if not state["retain"]:
                state["items"].pop(address_id, None)
            self.send_response(204)
            self.end_headers()

        def log_message(self, format, *args):
            # Synthetic HTTP logs stay separate from the command output under test.
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(
        app, "ADDRESSES_URL", f"http://127.0.0.1:{server.server_port}/addresses"
    )
    monkeypatch.setattr(
        app,
        "_auth_broker",
        lambda *args: {"cookies": "session=synthetic", "userId": "123"},
    )
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_remove_requires_explicit_confirmation(app, address_book):
    result = CliRunner().invoke(app.cli, ["addresses", "remove", "P-old"])
    assert (result.exit_code, address_book["deleted"]) == (1, [])


def test_remove_dry_run_makes_no_changes(app, address_book):
    result = CliRunner().invoke(app.cli, ["addresses", "remove", "P-old", "--dry-run"])
    assert (result.exit_code, address_book["deleted"]) == (0, [])


def test_remove_preserves_other_addresses(app, address_book):
    result = CliRunner().invoke(app.cli, ["addresses", "remove", "P-old", "--yes"])
    assert (result.exit_code, list(address_book["items"])) == (0, ["P-current"])


def test_remove_checks_result(app, address_book):
    address_book["retain"] = True
    result = CliRunner().invoke(app.cli, ["addresses", "remove", "P-old", "--yes"])
    assert (result.exit_code, result.output) == (
        1,
        "Error: Address P-old is still present after removal; check La Poste before retrying.\n",
    )


def test_remove_missing_address_is_idempotent(app, address_book):
    result = CliRunner().invoke(app.cli, ["addresses", "remove", "P-absent", "--yes"])
    assert (result.exit_code, address_book["deleted"]) == (0, [])


def test_addresses_json_exposes_full_ids(app, address_book):
    result = CliRunner().invoke(app.cli, ["addresses", "--json"])
    assert json.loads(result.output)[0]["id"] == "P-old"
