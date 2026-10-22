import os
import sqlite3

import pytest

from apps.admin_console.routers import chats
from artemis.data_engine.chat_sessions import ChatStore


@pytest.fixture(params=["native", "portable"], autouse=True)
def platform(request, monkeypatch):
    # "portable" simulates a platform without O_NOFOLLOW (Windows) on this host.
    if request.param == "portable":
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    return request.param


@pytest.fixture
def chat(library, monkeypatch):
    identity = library.seed("Check login", status="completed")
    with sqlite3.connect(library.db) as connection:
        connection.execute(
            "UPDATE run_meta SET requested_by='qa@example.com' WHERE session_id=?", (identity,)
        )
    monkeypatch.setattr(chats, "DB_PATH", library.db)
    monkeypatch.setattr(chats, "TRACES_PATH", library.traces)
    return identity


@pytest.mark.asyncio
async def test_every_turn_carries_its_own_results_note(qa, chat, library):
    library.write(chat, "notes/results.md", "| Mục | Trạng thái | Kết quả | Bằng chứng |\n")
    os.utime(library.traces / chat / "notes" / "results.md", (1000, 1000))
    second = ChatStore(library.db).new_turn(chat, "tiếp tục", "second")["turn_id"]
    ChatStore(library.db).transition(second, "running")
    ChatStore(library.db).transition(second, "failed")

    body = (await qa.get(f"/api/chats/{chat}")).json()

    first, latest = body["results"]
    assert first["results_note"] == {
        "text": "| Mục | Trạng thái | Kết quả | Bằng chứng |\n",
        "modified_at": 1000.0,
        "truncated": False,
    }
    assert latest["results_note"] is None
    assert body["verdict"]["result"] == latest


@pytest.mark.asyncio
async def test_verdict_result_carries_note_without_changing_the_verdict(qa, chat, library):
    library.write(chat, "notes/results.md", "TOTAL: Planned 1 / PASS 1")

    body = (await qa.get(f"/api/chats/{chat}")).json()

    assert body["verdict"]["result"]["results_note"]["text"] == "TOTAL: Planned 1 / PASS 1"
    assert "pass" not in body["verdict"]["result"]["label_state"]


@pytest.mark.asyncio
async def test_note_is_capped_at_64_kb(qa, chat, library):
    library.write(chat, "notes/results.md", "a" * (64 * 1024 + 500))

    note = (await qa.get(f"/api/chats/{chat}")).json()["results"][-1]["results_note"]

    assert len(note["text"].encode()) == 64 * 1024
    assert note["truncated"] is True


@pytest.mark.asyncio
async def test_symlinked_note_is_ignored(qa, chat, library, tmp_path):
    outside = tmp_path / "secret.md"
    outside.write_text("not yours")
    notes = library.traces / chat / "notes"
    notes.mkdir(parents=True)
    (notes / "results.md").symlink_to(outside)

    body = (await qa.get(f"/api/chats/{chat}")).json()

    assert body["results"][-1]["results_note"] is None


@pytest.mark.asyncio
async def test_other_owner_cannot_read_note(qa2, chat, library):
    library.write(chat, "notes/results.md", "private")
    response = await qa2.get(f"/api/chats/{chat}")
    assert response.status_code == 200
    assert "private" not in response.text
    assert response.json()["results"][-1]["results_note"] is None


def swap_at_open(monkeypatch, library, chat, swap):
    """Run ``swap`` once, right before the results note is first opened (after any path check)."""
    note = library.traces / chat / "notes" / "results.md"
    triggers = {str(note), str(library.traces), os.path.realpath(library.traces)}
    real_open = os.open
    done = []

    def open_after_swap(path, *args, **kwargs):
        if not done and os.fspath(path) in triggers:
            done.append(True)
            swap(note)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_after_swap)
    return done


@pytest.mark.asyncio
async def test_note_swapped_for_a_link_at_open_is_ignored(qa, chat, library, tmp_path, monkeypatch):
    library.write(chat, "notes/results.md", "mine")
    outside = tmp_path / "secret.md"
    outside.write_text("not yours")

    def swap(note):
        note.unlink()
        note.symlink_to(outside)

    done = swap_at_open(monkeypatch, library, chat, swap)

    body = (await qa.get(f"/api/chats/{chat}")).json()

    assert done
    assert body["results"][-1]["results_note"] is None


@pytest.mark.asyncio
async def test_ancestor_swapped_for_a_link_at_open_is_ignored(
    qa, chat, library, tmp_path, monkeypatch
):
    library.write(chat, "notes/results.md", "mine")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "results.md").write_text("not yours")

    def swap(note):
        note.parent.rename(note.parent.with_name("notes.moved"))
        note.parent.symlink_to(elsewhere, target_is_directory=True)

    done = swap_at_open(monkeypatch, library, chat, swap)

    body = (await qa.get(f"/api/chats/{chat}")).json()

    assert done
    assert "not yours" not in str(body)
    assert body["results"][-1]["results_note"] is None
