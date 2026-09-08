"""Address failures remain actionable without exposing response bodies or credentials."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace

import pytest
from click.testing import CliRunner


@pytest.fixture
def address_payload():
    return {
        "P-example": {
            "postalId": "P-example",
            "label": "Home",
            "address": {"line1": "M. JEAN DUPONT", "line4": "1 RUE DE LA PAIX"},
            "postalCode": "75001",
            "locality": "PARIS",
            "isPrimary": True,
        }
    }


@pytest.fixture
def response(app):
    return app.requests.Response()


@pytest.mark.parametrize(
    ("status", "body", "message"),
    [
        (
            403,
            "<title>Site indisponible - Incident en cours - La Poste</title>",
            "La Poste returned its service-unavailable page (HTTP 403). Check your browser session or try again later.",
        ),
        (
            503,
            "Unavailable",
            "Could not fetch saved addresses (HTTP 503). Try again later.",
        ),
        (401, "Unauthorized", "La Poste requires a new login (HTTP 401)."),
        (
            412,
            '{"error":"need_reconnect"}',
            "La Poste requires a new login (HTTP 412).",
        ),
        (403, "Forbidden", "La Poste rejected the address request (HTTP 403)."),
        (
            200,
            "<html>Unavailable</html>",
            "La Poste returned an invalid address response. Try again later.",
        ),
    ],
)
def test_address_response_errors(app, response, status, body, message):
    response.status_code = status
    response.ok = status < 400
    response.content = body.encode()
    session = SimpleNamespace(get=lambda *args, **kwargs: response)
    with pytest.raises(app.click.ClickException) as error:
        app.fetch_sender_addresses(session)
    assert str(error.value).startswith(message)


def test_address_connection_error(app):
    def get(*args, **kwargs):
        raise app.requests.RequestsError("synthetic-secret")

    with pytest.raises(app.click.ClickException) as error:
        app.fetch_sender_addresses(SimpleNamespace(get=get))
    assert (
        str(error.value)
        == "Could not reach La Poste to fetch saved addresses. Check your connection and try again."
    )


def test_address_normalization(app, response, address_payload):
    response.content = json.dumps(address_payload).encode()
    session = SimpleNamespace(get=lambda *args, **kwargs: response)
    assert app.fetch_sender_addresses(session)[0]["streetName"] == "1 RUE DE LA PAIX"


@pytest.fixture
def address_server(app, monkeypatch):
    """Serve HTTP locally to exercise the CLI and its real curl transport."""
    reply = {"status": 200, "body": b"{}"}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(reply["status"])
            self.end_headers()
            self.wfile.write(reply["body"])

        def log_message(self, format, *args):
            # Synthetic server traffic stays quiet during command-output checks.
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
        yield reply
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_addresses_command_outage(app, address_server):
    address_server.update(
        status=403,
        body=b"<title>Site indisponible - Incident en cours - La Poste</title>synthetic-secret",
    )
    result = CliRunner().invoke(app.cli, ["addresses"])
    assert (result.exit_code, result.output) == (
        1,
        "Error: La Poste returned its service-unavailable page (HTTP 403). Check your browser session or try again later.\n",
    )


def test_addresses_command_success(app, address_server, address_payload):
    address_server["body"] = json.dumps(address_payload).encode()
    result = CliRunner().invoke(app.cli, ["addresses"])
    assert (result.exit_code, "1 RUE DE LA PAIX" in result.output) == (0, True)


def test_addresses_command_empty(app, address_server):
    result = CliRunner().invoke(app.cli, ["addresses"])
    assert (result.exit_code, result.output) == (0, "No saved addresses.\n")


def test_addresses_command_with_browser_login(
    app, address_server, address_payload, monkeypatch
):
    address_server["body"] = json.dumps(address_payload).encode()
    monkeypatch.setattr(
        app,
        "_auth_broker",
        lambda *args: {
            "browser": "chrome",
            "userId": "123",
            "cookies": "session=stale",
        },
    )
    jar = app.requests.Cookies()
    jar.set("pa_user", json.dumps({"id": "123"}), domain=".laposte.fr")
    monkeypatch.setattr(app, "get_browser_jar", lambda *args: jar.jar)
    result = CliRunner().invoke(app.cli, ["addresses"])
    assert (result.exit_code, "1 RUE DE LA PAIX" in result.output) == (0, True)
