"""Drive, UNC and outside-root handle paths through both open_under callers (CHE-1905)."""

import json
import os
from pathlib import Path
import sqlite3
import sys

import pytest

from apps.admin_console.routers import chats
from apps.admin_console.services import run_artifacts
from tests.unit.admin_console.test_run_bundle_api import _zip

DRIVE = ("C:\\traces", "\\\\?\\C:\\traces")
UNC = ("\\\\server\\share\\traces", "\\\\?\\UNC\\server\\share\\traces")
OTHER_SHARE = ("\\\\server\\share\\traces", "\\\\?\\UNC\\server\\share\\elsewhere")
OTHER_DRIVE = ("C:\\traces", "\\\\?\\D:\\traces")


@pytest.fixture
def windows_paths(library, monkeypatch):
    """Show the portable open a Windows-style root (as realpath says) and handle path (as the OS says)."""
    real_realpath = os.path.realpath

    def use(root: str, handle_root: str):
        def realpath(path, **kwargs):
            # Only the portable open sees the Windows-style root; the rest of the app keeps its own.
            if sys._getframe(1).f_code.co_name == "_open_where_it_lives":
                return root
            return real_realpath(path, **kwargs)

        def handle_path(fd):
            actual = Path(real_realpath(f"/proc/self/fd/{fd}"))
            return f"{handle_root}/{actual.relative_to(real_realpath(library.traces)).as_posix()}"

        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
        monkeypatch.setattr(os.path, "realpath", realpath)
        monkeypatch.setattr(run_artifacts, "_handle_path", handle_path)

    return use


@pytest.fixture
def chat(library, monkeypatch):
    identity = library.seed("Check login", status="completed")
    with sqlite3.connect(library.db) as connection:
        connection.execute(
            "UPDATE run_meta SET requested_by='qa@example.com' WHERE session_id=?", (identity,)
        )
    monkeypatch.setattr(chats, "DB_PATH", library.db)
    monkeypatch.setattr(chats, "TRACES_PATH", library.traces)
    library.write(identity, "notes/results.md", "inside")
    return identity


FORMS = [
    pytest.param(DRIVE, True, id="drive"),
    pytest.param(UNC, True, id="unc"),
    pytest.param(OTHER_SHARE, False, id="unc-outside-root"),
    pytest.param(OTHER_DRIVE, False, id="drive-outside-root"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("paths", "readable"), FORMS)
async def test_chat_note_follows_the_handle_path_form(qa, chat, windows_paths, paths, readable):
    windows_paths(*paths)

    note = (await qa.get(f"/api/chats/{chat}")).json()["results"][-1]["results_note"]

    assert (note and note["text"]) == ("inside" if readable else None)


@pytest.mark.asyncio
@pytest.mark.parametrize(("paths", "readable"), FORMS)
async def test_bundle_follows_the_handle_path_form(qa, library, windows_paths, paths, readable):
    sid = library.seed("forms")
    library.write(sid, "notes/finding.md", "INSIDE-NOTE")
    windows_paths(*paths)

    async with qa:
        archive = _zip(await qa.get(f"/api/runs/{sid}/bundle.zip"))

    skipped = {entry["name"] for entry in json.loads(archive.read("manifest.json"))["skipped"]}
    assert ("notes/finding.md" not in skipped) == readable
    assert ("notes/finding.md" in archive.namelist()) == readable
