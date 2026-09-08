#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Apply OneBrain read-only policy to native Click command callbacks."""

from __future__ import annotations

import functools
import json
import os
from pathlib import Path


# Unknown commands are writes until their read behavior has been reviewed.
READS = {
    "whatsapp": {"status", "list", "chats", "read", "download", "pull", "sync", "listen"},
    "telegram": {"whoami", "chats", "read", "search", "download", "folders", "folder", "listen"},
    "email": {"accounts", "mailboxes", "list", "search", "read", "attachments", "auth status"},
    "slack": {"whoami", "workspaces", "channels", "read", "thread", "search", "users", "info", "canvas", "download", "permalink", "resolve", "resolve-name"},
    "gdrive": {"whoami", "auth list", "auth status", "drive search", "drive ls", "drive get", "drive comments", "drive download", "docs read", "sheets read", "sheets info"},
    "notion": {"whoami", "search", "page", "pages", "query", "comments", "resolve", "schema", "templates", "users"},
    "deezer": {"whoami", "album", "artist", "artist-radio", "artist-related", "artist-top", "chart", "download", "export-likes", "flow", "genres", "history", "likes", "playlist", "playlists", "resolve", "search", "track"},
    "ecoledirecte": {"whoami", "contacts", "download", "homework", "messages", "notes", "read", "timetable"},
    "rentalready": {"whoami", "doctor", "overview", "profile", "projection", "property", "reservations"},
    "cafeyn": {"whoami", "articles", "editions", "issue", "list", "read"},
    "franc-tireur": {"whoami", "dump", "numero", "numeros", "page", "pdf", "read", "search", "toc"},
    "milibris": {"whoami", "dump", "issue", "issues", "kiosks", "material", "pdf", "read", "search", "titles", "toc"},
    "laposte": {"whoami", "addresses"},
    "pajemploi": {"bulletin", "doctor", "documents", "salarie", "salaries"},
    "imessage": {"chats", "read", "search"},
    "sentry": {"whoami", "issue", "issues", "latest", "projects"},
}


def install(group, connector: str) -> None:
    """Guard every leaf before its callback; group selection cannot grant write access."""
    import click

    def wrap(command, parts):
        if hasattr(command, "commands"):
            for name, child in command.commands.items():
                wrap(child, [*parts, name])
            return
        callback = command.callback
        if callback is None or getattr(callback, "_onebrain_guard", False):
            return

        @functools.wraps(callback)
        def guarded(*args, **kwargs):
            root = Path(os.environ.get("BRAIN_SUPPORT", str(Path.home() / "Library/Application Support/Brain")))
            try:
                policy = json.loads((root / "connector-permissions.json").read_text())
            except FileNotFoundError:
                policy = {}
            except (OSError, ValueError):
                raise click.ClickException("OneBrain permissions cannot be read; repair them in the app") from None
            if not isinstance(policy, dict) or (connector in policy and not isinstance(policy[connector], dict)):
                raise click.ClickException("OneBrain permissions are invalid; repair them in the app")
            # Standalone CLIs retain their behavior until this connector is managed by the app.
            enabled = connector in policy and policy[connector].get("read_only", True) is not False
            operation = " ".join(parts)
            # Login and vault maintenance change credentials, not provider content.
            auth_operations = {"login", "auth login", "auth-status", "auth status", "auth-sync", "auth sync"}
            if enabled and operation not in READS.get(connector, set()) | auth_operations:
                raise click.ClickException(f"{connector} is read-only in OneBrain; this action is blocked")
            if enabled and operation == "listen" and kwargs.get("exec_cmd"):
                raise click.ClickException("Listener commands are disabled while this connector is read-only")
            return callback(*args, **kwargs)

        guarded._onebrain_guard = True
        command.callback = guarded

    wrap(group, [])
