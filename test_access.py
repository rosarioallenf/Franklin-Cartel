"""
Who did what, and the gate on the handful of actions that change things for
everybody.

Neither of these mattered while Allen was the only person using the app. Both
matter the moment the link is shared: "who entered this round?" needs an answer,
and somebody exploring the Health tab must not be able to change the stake for
the whole group by accident.
"""
from __future__ import annotations

import pytest

from cartel import storage


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "a.db")
    monkeypatch.setenv("CARTEL_DB", path)
    monkeypatch.setenv("CARTEL_BACKUP_DIR", str(tmp_path / "bk"))
    monkeypatch.delenv("CARTEL_DB_URL", raising=False)
    storage.init_db(path)
    return path


# --------------------------------------------------------------------------
# the record of who did what
# --------------------------------------------------------------------------

def test_activity_is_recorded_with_a_name_and_a_time(db):
    with storage.connect(db) as conn:
        storage.log_activity(conn, "Bert Dargie", "posted round", "R62")
        rows = storage.recent_activity(conn)
    assert len(rows) == 1
    assert rows[0]["who"] == "Bert Dargie"
    assert rows[0]["action"] == "posted round"
    assert rows[0]["at"]


def test_the_log_is_append_only_and_newest_first(db):
    with storage.connect(db) as conn:
        for i in range(3):
            storage.log_activity(conn, f"P{i}", "posted round", f"R{i}")
        rows = storage.recent_activity(conn)
    assert [r["detail"] for r in rows] == ["R2", "R1", "R0"]


def test_an_unnamed_action_is_still_recorded(db):
    """Better a gap that says 'unknown' than no line at all."""
    with storage.connect(db) as conn:
        storage.log_activity(conn, "", "posted round")
        assert storage.recent_activity(conn)[0]["who"] == "unknown"


def test_a_round_remembers_who_posted_it(db):
    with storage.connect(db) as conn:
        rid = storage.create_round(conn, "2026-09-06", "N", round_no=63)
        conn.execute("UPDATE rounds SET posted_by = ? WHERE round_id = ?",
                     ("Bert Dargie", rid))
        assert storage.get_round(conn, rid)["posted_by"] == "Bert Dargie"


# --------------------------------------------------------------------------
# the admin word
# --------------------------------------------------------------------------

def test_nothing_is_locked_until_a_word_is_set(db):
    """A fresh install must not lock its owner out of his own app."""
    with storage.connect(db) as conn:
        assert not storage.admin_passphrase_set(conn)
        assert storage.check_admin_passphrase(conn, "anything at all")


def test_the_right_word_opens_it_and_a_wrong_one_does_not(db):
    with storage.connect(db) as conn:
        storage.set_admin_passphrase(conn, "birdie")
        assert storage.admin_passphrase_set(conn)
        assert storage.check_admin_passphrase(conn, "birdie")
        assert not storage.check_admin_passphrase(conn, "bogey")
        assert not storage.check_admin_passphrase(conn, "")


def test_the_word_itself_is_never_stored(db):
    """Only a salted hash. The database will be on a hosted server."""
    with storage.connect(db) as conn:
        storage.set_admin_passphrase(conn, "birdie")
        stored = storage.get_setting(conn, "admin_hash")
        salt = storage.get_setting(conn, "admin_salt")
    assert "birdie" not in (stored or "")
    assert len(stored) == 64 and salt


def test_two_installs_with_the_same_word_hash_differently(db, tmp_path, monkeypatch):
    """Salted, so one leaked hash says nothing about another install."""
    with storage.connect(db) as conn:
        storage.set_admin_passphrase(conn, "birdie")
        first = storage.get_setting(conn, "admin_hash")

    other = str(tmp_path / "b.db")
    storage.init_db(other)
    with storage.connect(other) as conn:
        storage.set_admin_passphrase(conn, "birdie")
        second = storage.get_setting(conn, "admin_hash")
    assert first != second


def test_changing_the_word_replaces_the_old_one(db):
    with storage.connect(db) as conn:
        storage.set_admin_passphrase(conn, "birdie")
        storage.set_admin_passphrase(conn, "eagle")
        assert storage.check_admin_passphrase(conn, "eagle")
        assert not storage.check_admin_passphrase(conn, "birdie")


def test_posting_a_round_is_never_gated(db):
    """
    The whole point of hosting is that others can post while Allen is away.
    Locking the one action they need would defeat it.
    """
    from pathlib import Path
    app = (Path(__file__).resolve().parent.parent / "app.py").read_text()
    gated = [line for line in app.splitlines()
             if "require_admin(" in line and "def " not in line]
    assert gated, "the gate should be in use somewhere"
    for line in gated:
        assert "post" not in line.lower(), f"posting must stay open: {line.strip()}"
