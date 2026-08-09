"""
Backups and the hand-entry route.

The database is a single file holding four and a half years of golf. These
tests exist because "I'll copy it after each round" is not a backup strategy,
and because the club's tee sheet format has already changed once.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cartel import backup, storage


@pytest.fixture()
def populated_app(tmp_path, monkeypatch):
    """A database with a settled round, so every tab has something to draw."""
    from cartel.pipeline import settle_round
    db = tmp_path / "app.db"
    monkeypatch.setenv("CARTEL_DB", str(db))
    monkeypatch.setenv("CARTEL_BACKUP_DIR", str(tmp_path / "bk"))
    monkeypatch.delenv("CARTEL_DB_URL", raising=False)
    storage.init_db(str(db))
    with storage.connect(str(db)) as conn:
        for n, _, _ in FIELD_A:
            storage.upsert_member(conn, n, "W")
        rid = storage.create_round(conn, "2026-06-01", "N", round_no=1, status="draft")
        storage.save_entries(conn, rid, [{"name": n, "team_no": t, "quota": q}
                                         for n, t, q in FIELD_A])
    settle_round(rid, [{"name": n, "points_front": f, "points_back": b,
                        "score": 85, "greens": 0, "skins": 0}
                       for (n, _, _), (f, b) in zip(FIELD_A, POINTS_A)],
                 db_path=str(db), out_dir=str(tmp_path / "out"))
    return str(db)


@pytest.fixture()
def live(tmp_path, monkeypatch):
    db = tmp_path / "cartel.db"
    monkeypatch.setenv("CARTEL_DB", str(db))
    monkeypatch.setenv("CARTEL_BACKUP_DIR", str(tmp_path / "DB_Backup"))
    monkeypatch.delenv("CARTEL_DB_URL", raising=False)
    storage.init_db(str(db))
    with storage.connect(str(db)) as conn:
        storage.upsert_member(conn, "Bert Dargie", "B")
    return db


def test_a_backup_is_a_working_database_not_just_a_file(live):
    r = backup.make_backup(reason="R61-posted", when="2026-08-01")
    assert r.ok, r.skipped
    conn = sqlite3.connect(str(r.path))
    names = [x[0] for x in conn.execute("SELECT name FROM members")]
    conn.close()
    assert "Bert Dargie" in names, "the snapshot must be openable and complete"


def test_the_backup_is_named_after_the_round_not_the_clock(live):
    r = backup.make_backup(reason="R61-posted", when="2026-08-01")
    assert "2026-08-01" in r.path.name
    assert "r61-posted" in r.path.name


def test_backing_the_same_round_up_twice_does_not_pile_up(live):
    a = backup.make_backup(reason="R61-posted", when="2026-08-01")
    b = backup.make_backup(reason="R61-posted", when="2026-08-01")
    assert a.path == b.path
    assert len(backup.list_backups()) == 1


def test_backups_live_outside_the_app_folder(monkeypatch):
    """
    Updates are installed by extracting a zip over the app folder. A backup
    inside the thing being overwritten is not a backup.
    """
    monkeypatch.delenv("CARTEL_BACKUP_DIR", raising=False)
    app_root = Path(backup.__file__).resolve().parent.parent
    assert backup.backup_dir().resolve() != app_root.resolve()
    assert app_root.resolve() not in backup.backup_dir().resolve().parents


def test_old_backups_are_pruned_but_recent_ones_kept(live):
    for i in range(1, 9):
        backup.make_backup(reason=f"R{i}-posted", when=f"2026-01-{i:02d}", keep=5)
    kept = backup.list_backups()
    assert len(kept) == 5, f"kept {len(kept)}"
    assert "2026-01-08" in kept[0].name, "the newest must survive"


def test_pruning_is_safe_when_timestamps_tie(live):
    """
    Windows records file times coarsely, so several backups written in one
    sitting - a weekend's two rounds entered together - can share a timestamp.
    Sorting on time alone then returns them in directory order, which is
    alphabetical, i.e. OLDEST first: pruning kept the oldest and deleted the
    newest. Found by this suite running on Allen's machine, not on mine.
    """
    import os
    import time

    for i in range(1, 9):
        backup.make_backup(reason=f"R{i}-posted", when=f"2026-01-{i:02d}", keep=99)

    frozen = time.time()
    for f in backup.backup_dir().glob("cartel_*.db"):
        os.utime(f, (frozen, frozen))          # force the tie

    assert backup.list_backups()[0].name.count("2026-01-08") == 1, \
        "ordering must not depend on the filesystem's timestamp resolution"

    backup.prune(keep=5)
    survived = sorted(f.name for f in backup.list_backups())
    assert len(survived) == 5
    assert any("2026-01-08" in n for n in survived), "the newest must survive a tie"
    assert not any("2026-01-01" in n for n in survived), "the oldest should have gone"


def test_a_failed_backup_never_stops_the_round(live, monkeypatch, tmp_path):
    """
    Settling a round must not fail because a backup could not be written. Here
    the backup folder is blocked by a FILE of the same name, so mkdir cannot
    succeed whatever the permissions.
    """
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    monkeypatch.setenv("CARTEL_BACKUP_DIR", str(blocker / "DB_Backup"))

    r = backup.make_backup(reason="R61-posted")          # must not raise
    assert not r.ok
    assert r.skipped, "a failure must explain itself rather than raise"


def test_restore_refuses_without_confirmation(live):
    r = backup.make_backup(reason="before")
    with pytest.raises(ValueError, match="confirm"):
        backup.restore(r.path)


def test_restore_keeps_the_database_it_displaces(live):
    """The mistake people make is restoring the wrong file."""
    good = backup.make_backup(reason="good")

    with storage.connect(str(live)) as conn:          # then something goes wrong
        conn.execute("DELETE FROM members")

    backup.restore(good.path, confirm=True)
    with storage.connect(str(live)) as conn:
        assert len(storage.all_members(conn)) == 1, "the good data should be back"

    assert any("before-restore" in b.name for b in backup.list_backups()), \
        "the displaced database must be kept, so a wrong restore can be undone"


# --------------------------------------------------------------------------
# teams entered by hand
# --------------------------------------------------------------------------

def test_hand_entered_teams_build_the_same_shape_as_a_pdf(live):
    from datetime import date
    from cartel.pipeline import manual_tee_sheet

    sheet = manual_tee_sheet(date(2026, 8, 9), "S",
                             [["A", "B", "C"], ["D", "E", "F", "G"]],
                             round_no=62, tee_times=["10:00 AM", "10:09 AM"])
    assert sheet.round_no == 62
    assert sheet.courses == ["S"]
    assert [len(g.players) for g in sheet.groups] == [3, 4]
    assert [g.team_no for g in sheet.groups] == [1, 2]


def test_blank_slots_and_empty_teams_are_dropped(live):
    from datetime import date
    from cartel.pipeline import manual_tee_sheet

    sheet = manual_tee_sheet(date(2026, 8, 9), "N",
                             [["A", "B", "C", ""], [], ["D", "E", "F"]])
    assert [len(g.players) for g in sheet.groups] == [3, 3]
    assert [g.team_no for g in sheet.groups] == [1, 2], "teams renumber contiguously"


def test_an_odd_team_size_is_flagged_not_refused(live):
    """Sized against the house rules, whatever they currently say."""
    from datetime import date
    from cartel.config import RULES
    from cartel.pipeline import manual_tee_sheet

    too_small = ["A"] * (RULES.min_team_size - 1)
    too_big = ["P%d" % i for i in range(RULES.max_team_size + 1)]
    sheet = manual_tee_sheet(date(2026, 8, 9), "N", [too_small, too_big])

    assert len(sheet.warnings) == 2, sheet.warnings
    assert all("outside the usual" in w for w in sheet.warnings)
    assert len(sheet.groups) == 2, "flagged, but still built - the round happened"


def test_prepare_refuses_both_routes_at_once(live):
    from datetime import date
    from cartel.pipeline import manual_tee_sheet, prepare_round

    sheet = manual_tee_sheet(date(2026, 8, 9), "N", [["A", "B", "C"]])
    with pytest.raises(ValueError, match="not both"):
        prepare_round("some.pdf", manual=sheet)
    with pytest.raises(ValueError, match="Nothing to prepare"):
        prepare_round()


# --------------------------------------------------------------------------
# photo pre-fill
# --------------------------------------------------------------------------

def test_a_blank_guest_row_is_not_read_as_absence():
    """
    Nobody writes a guest's points down. Reading that blank row as "did not
    play" would drop their $10 from the skat pot, and nothing on screen would
    look wrong - the pot would just be quietly $10 light.
    """
    from cartel import vision

    absent_guest = vision.VisionRow(name="Visitor", is_guest=True, played=False)
    warnings = vision._sanity_checks([absent_guest], None)
    assert any("still paid in" in w for w in warnings)


def test_a_guest_with_no_points_raises_no_false_alarm():
    from cartel import vision

    present_guest = vision.VisionRow(name="Visitor", is_guest=True, played=True)
    assert vision._sanity_checks([present_guest], None) == []


def test_a_member_with_no_points_is_still_flagged():
    from cartel import vision

    sloppy = vision.VisionRow(name="Member", is_guest=False, played=True)
    assert any("missing a points figure" in w
               for w in vision._sanity_checks([sloppy], None))


def test_the_prompt_explains_the_guest_case():
    """The model can only get this right if it is told the rule."""
    from cartel import vision
    assert "guest" in vision.PROMPT.lower()
    assert "did play" in vision.PROMPT.lower() or "DID play" in vision.PROMPT


def test_the_model_id_is_a_current_one():
    from cartel import vision
    assert vision.MODEL in ("claude-sonnet-5", "claude-opus-5",
                            "claude-haiku-4-5-20251001", "claude-fable-5")


def test_photo_prefill_stays_off_even_with_a_key_present(monkeypatch):
    """
    Switched off by decision, not by absent credentials. A feature that is off
    only because nobody set a key comes back on the day somebody does.
    """
    from cartel import vision
    from cartel.config import RULES

    assert RULES.photo_prefill_enabled is False, "the house rule is off"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-pretend")
    assert vision.available() is False, "the house rule must beat the key"


def test_turning_the_house_rule_on_is_the_only_way_in(monkeypatch):
    from cartel import vision
    from cartel.config import HouseRules

    on = HouseRules(photo_prefill_enabled=True)
    on.validate()                       # a valid configuration, just not ours
    assert on.photo_prefill_enabled is True
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert vision.available() is False, "still needs a key as well"


# --------------------------------------------------------------------------
# season awards
# --------------------------------------------------------------------------

FIELD_A = [("Bert Dargie", 1, 35), ("Don Vick", 1, 27), ("Tom Button", 1, 27),
           ("B.H. Khoo", 2, 29), ("John Holmes", 2, 29), ("Takashi Yagi", 2, 28)]
POINTS_A = [(17, 18), (13, 14), (12, 13), (15, 15), (14, 16), (14, 14)]


@pytest.fixture()
def season(tmp_path, monkeypatch):
    """A short season: six players, five settled rounds."""
    from cartel.pipeline import settle_round
    db = tmp_path / "s.db"
    monkeypatch.setenv("CARTEL_DB", str(db))
    monkeypatch.setenv("CARTEL_BACKUP_DIR", str(tmp_path / "bk"))
    monkeypatch.delenv("CARTEL_DB_URL", raising=False)
    storage.init_db(str(db))
    with storage.connect(str(db)) as conn:
        for n, _, _ in FIELD_A:
            storage.upsert_member(conn, n, "W")

    for i, d in enumerate(["2026-06-01", "2026-06-08", "2026-06-15",
                           "2026-06-22", "2026-06-29"]):
        with storage.connect(str(db)) as conn:
            rid = storage.create_round(conn, d, "N", round_no=i + 1, status="draft")
            storage.save_entries(conn, rid, [{"name": n, "team_no": t, "quota": q}
                                             for n, t, q in FIELD_A])
        rows = [{"name": n, "points_front": f, "points_back": b, "score": 85,
                 "greens": 1 if n == "Bert Dargie" else 0,
                 "skins": 1 if n == "B.H. Khoo" else 0}
                for (n, _, _), (f, b) in zip(FIELD_A, POINTS_A)]
        settle_round(rid, rows, db_path=str(db), out_dir=str(tmp_path / "out"))
    return str(db)


def test_awards_are_as_if_the_season_ended_today(season):
    """
    Not frozen, not cached: computed from whatever has been settled. Asking in
    August gives August's answer; asking in December gives the year's.
    """
    from cartel import stats

    with storage.connect(season) as conn:
        a = stats.season_awards(conn, 2026)

    assert a["as_of"] == "2026-06-29", "anchored to the last settled round"
    assert a["as_of_round"] == 5
    assert a["rounds"] == 5


def test_the_awards_move_when_another_round_is_posted(season, tmp_path):
    from cartel import stats
    from cartel.pipeline import settle_round

    with storage.connect(season) as conn:
        before = stats.season_awards(conn, 2026)

    with storage.connect(season) as conn:
        rid = storage.create_round(conn, "2026-07-06", "N", round_no=6, status="draft")
        storage.save_entries(conn, rid, [{"name": n, "team_no": t, "quota": q}
                                         for n, t, q in FIELD_A])
    settle_round(rid, [{"name": n, "points_front": f, "points_back": b, "score": 85,
                        "greens": 0, "skins": 0}
                       for (n, _, _), (f, b) in zip(FIELD_A, POINTS_A)],
                 db_path=season, out_dir=str(tmp_path / "out2"))

    with storage.connect(season) as conn:
        after = stats.season_awards(conn, 2026)

    assert after["as_of"] == "2026-07-06", "the cut-off must follow the last round"
    assert after["rounds"] == before["rounds"] + 1


def test_money_awards_respect_the_eligibility_rule(season):
    """Counting awards have no threshold; money awards use the standings rule."""
    from cartel import stats
    from cartel.config import RULES

    with storage.connect(season) as conn:
        ytd = stats.year_to_date(conn, 2026)
        a = stats.season_awards(conn, 2026)["awards"]

    eligible = set(ytd[ytd["Rank"].notna()]["Name"])
    for name in a["Best per round"]["table"]["Name"]:
        assert name in eligible, f"{name} ranked without clearing the threshold"

    assert not a["Most skats"]["table"].empty, \
        "counting awards must not be gated - turning up is the qualification"
    assert str(RULES.rank_min_rounds) in a["Best per round"]["blurb"]


def test_the_awards_that_matter_are_all_there(season):
    from cartel import stats
    with storage.connect(season) as conn:
        names = set(stats.season_awards(conn, 2026)["awards"])
    for expected in ("Order of Merit", "Best per round", "Most skats",
                     "Sharpest iron", "Most skins", "Iron man",
                     "Round of the year"):
        assert expected in names, f"{expected} missing"


def test_a_player_card_covers_the_year_and_the_quota_basis(season):
    from cartel import stats
    with storage.connect(season) as conn:
        card = stats.player_card(conn, "Bert Dargie", 2026)

    assert card["summary"]["Rds"] == 5
    assert len(card["rounds"]) == 5
    assert list(card["rounds"]["Date"]) == sorted(card["rounds"]["Date"], reverse=True)
    assert not card["basis"].empty, "the quota basis must be shown"
    assert len(card["quota_trend"]) == 5


def test_a_player_card_for_someone_who_never_played_is_empty_not_broken(season):
    from cartel import stats
    with storage.connect(season) as conn:
        storage.upsert_member(conn, "Never Played", "W")
    with storage.connect(season) as conn:
        card = stats.player_card(conn, "Never Played", 2026)
    assert card["rounds"].empty
    assert card["quota_trend"].empty


def test_moving_a_player_before_printing_moves_the_team_quota(live):
    """
    Golf Genius put him in the wrong group, or he's swapping. Fixing it before
    the scoresheet is printed means the quota ON the sheet is right - fixing it
    afterwards means the paper disagrees with the app all afternoon.
    """
    from datetime import date
    from cartel import storage
    from cartel.pipeline import manual_tee_sheet, prepare_round

    with storage.connect(live) as conn:
        for n in ("A", "B", "C", "D", "E", "F"):
            storage.upsert_member(conn, n, "W", manual_quota=20)

    before = prepare_round(
        manual=manual_tee_sheet(date(2026, 9, 6), "N",
                                [["A", "B", "C"], ["D", "E", "F"]], round_no=80),
        out_dir="/tmp/mv_before")
    xi_before = {t["team_no"]: sum(before.quotas[n].quota for n in t["players"]) / 2
                 for t in before.teams}
    assert xi_before == {1: 30.0, 2: 30.0}

    after = prepare_round(
        manual=manual_tee_sheet(date(2026, 9, 6), "N",
                                [["A", "B"], ["C", "D", "E", "F"]], round_no=80),
        out_dir="/tmp/mv_after")
    xi_after = {t["team_no"]: sum(after.quotas[n].quota for n in t["players"]) / 2
                for t in after.teams}
    assert xi_after == {1: 20.0, 2: 40.0}, \
        "both teams' quotas must follow the player who moved"


def test_every_rendered_table_survives_arrow(populated_app, monkeypatch):
    """
    Streamlit sends dataframes to the browser as Arrow, which needs one type per
    column. Blanking repeated values with "" beside a team NUMBER made the column
    dtype object; Streamlit patched it silently and printed a traceback that
    looked like a real fault.

    Checked by serialising what the app actually renders, rather than by reading
    the source - a column can be built in a dozen places and only one of them
    has to be wrong.
    """
    pa = pytest.importorskip("pyarrow")
    from streamlit.testing.v1 import AppTest
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    monkeypatch.chdir(root)
    at = AppTest.from_file(str(root / "app.py"), default_timeout=300).run()

    bad = []
    for element in list(at.dataframe) + list(at.get("data_editor")):
        frame = element.value
        try:
            pa.Table.from_pandas(frame, preserve_index=False)
        except Exception as exc:
            bad.append(f"{list(frame.columns)}: {exc}")
    assert not bad, "tables Arrow cannot serialise:\n" + "\n".join(bad)
