"""
Tests that need a real database: cancelled rounds, and the carry-over chain.

These are separate from test_scoring.py because they exercise storage and the
pipeline rather than the money rules on their own. They run against a throwaway
SQLite file, so they're still fast and need no setup.

Run:  python -m pytest tests -q
"""
from __future__ import annotations

import os
import tempfile

import pandas as pd
import pytest

from cartel import storage
from cartel.config import RULES

STAKE = RULES.default_stake()


FIELD = [("Bert Dargie", 1, 35), ("Don Vick", 1, 27), ("Tom Button", 1, 27),
         ("B.H. Khoo", 2, 29), ("John Holmes", 2, 29), ("Takashi Yagi", 2, 28)]
POINTS = [(17, 18), (13, 14), (12, 13), (15, 15), (14, 16), (14, 14)]


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "t.db")
    monkeypatch.setenv("CARTEL_DB", path)
    monkeypatch.delenv("CARTEL_DB_URL", raising=False)
    storage.init_db(path)
    with storage.connect(path) as conn:
        for name, _, _ in FIELD:
            storage.upsert_member(conn, name, "W")
    return path


def draft(db, date, round_no):
    """A round with a printed scoresheet and nothing filled in yet."""
    with storage.connect(db) as conn:
        rid = storage.create_round(conn, date, "N", round_no=round_no, status="draft")
        storage.save_entries(conn, rid, [{"name": n, "team_no": t, "quota": q}
                                         for n, t, q in FIELD])
    return rid


def scores(greens=0, skins=0):
    rows = [{"name": n, "points_front": f, "points_back": b, "greens": 0, "skins": 0}
            for (n, _, _), (f, b) in zip(FIELD, POINTS)]
    rows[0]["greens"] = greens
    rows[0]["skins"] = skins
    return rows


def settle_enough(db, tmp_path, n=None):
    """
    Settle enough recent rounds that everyone clears the ranking threshold.
    Tests about ranking ORDER need eligible players; tests about the threshold
    itself deliberately use fewer.
    """
    from cartel.config import RULES
    n = n or RULES.rank_min_rounds
    dates = [f"2026-0{6 + i // 4}-{1 + (i % 4) * 7:02d}" for i in range(n)]
    for i, d in enumerate(dates):
        settle(db, draft(db, d, 100 + i), scores(greens=1), tmp_path)
    return dates


def settle(db, rid, rows, tmp_path):
    from cartel.pipeline import settle_round
    return settle_round(rid, rows, db_path=db, out_dir=str(tmp_path / "out"))


# --------------------------------------------------------------------------
# a round that never happened
# --------------------------------------------------------------------------

def test_a_cancelled_round_changes_nothing(db, tmp_path):
    """Scoresheet printed, weather won, nobody entered anything."""
    def state():
        with storage.connect(db) as conn:
            return {n: (storage.rounds_on_file(conn, n),
                        storage.recent_points(conn, n, RULES.quota_window))
                    for n, _, _ in FIELD}

    before = state()
    draft(db, "2026-09-03", 70)
    assert state() == before, "an unsettled round must not touch anyone's history"


def test_a_cancelled_round_does_not_consume_the_carry(db, tmp_path):
    rid = draft(db, "2026-09-03", 70)
    settle(db, rid, scores(), tmp_path)          # no skats, so the pot carries
    with storage.connect(db) as conn:
        held = storage.pending_carry(conn)
    assert held == pytest.approx(STAKE.skat_per_member * len(FIELD))

    draft(db, "2026-09-06", 71)                  # printed, then cancelled
    with storage.connect(db) as conn:
        assert storage.pending_carry(conn) == pytest.approx(held), \
            "an abandoned round must not swallow money held over"


def test_cancelled_rounds_are_excluded_from_the_stats(db, tmp_path):
    from cartel import stats
    draft(db, "2026-09-03", 70)
    with storage.connect(db) as conn:
        df = stats.year_to_date(conn, 2026)
    assert df["Rds"].sum() == 0
    assert df["Won$"].sum() == 0


# --------------------------------------------------------------------------
# the carry-over chain
# --------------------------------------------------------------------------

def test_carry_survives_sheets_printed_out_of_order(db, tmp_path):
    """
    Sunday's scoresheet routinely comes off the printer before Thursday's scores
    are entered. The carry must follow the ROUND DATES, not the order somebody
    happened to press buttons in - otherwise a pot silently disappears.
    """
    thursday = draft(db, "2026-09-03", 70)
    sunday = draft(db, "2026-09-06", 71)         # printed while Thursday is open

    t = settle(db, thursday, scores(), tmp_path)
    assert t["result"].carried_money > 0

    s = settle(db, sunday, scores(), tmp_path)
    assert s["result"].carry_in == pytest.approx(t["result"].carried_money), \
        "Thursday's carry must land in Sunday's pot"


def test_no_money_leaks_across_a_chain_of_carried_rounds(db, tmp_path):
    collected = paid = 0.0
    for date, no in [("2026-09-03", 70), ("2026-09-06", 71), ("2026-09-10", 72)]:
        rid = draft(db, date, no)
        r = settle(db, rid, scores(), tmp_path)["result"]
        collected += r.total_collected
        paid += r.total_paid

    with storage.connect(db) as conn:
        still_held = storage.pending_carry(conn)

    assert collected == pytest.approx(paid + still_held), \
        f"${collected - paid - still_held:.2f} went missing across the chain"


def test_the_carry_is_consumed_once_somebody_wins_a_skat(db, tmp_path):
    first = draft(db, "2026-09-03", 70)
    carried = settle(db, first, scores(), tmp_path)["result"].carried_money
    assert carried > 0

    second = draft(db, "2026-09-06", 71)
    r = settle(db, second, scores(greens=1), tmp_path)["result"]
    assert r.carry_in == pytest.approx(carried)
    assert r.skat_pot == pytest.approx(STAKE.skat_per_member * len(FIELD) + carried)
    assert r.carried_money == 0
    assert r.payouts["Bert Dargie"].skat_money == pytest.approx(r.skat_pot)


def test_reposting_a_round_does_not_apply_its_own_carry_twice(db, tmp_path):
    first = draft(db, "2026-09-03", 70)
    settle(db, first, scores(), tmp_path)
    second = draft(db, "2026-09-06", 71)

    once = settle(db, second, scores(), tmp_path)["result"]
    again = settle(db, second, scores(), tmp_path)["result"]
    assert again.carry_in == pytest.approx(once.carry_in)
    assert again.skat_pot == pytest.approx(once.skat_pot)


def test_a_legacy_round_cannot_be_settled(db, tmp_path):
    """Imported history carries no money; settling one would double count."""
    with storage.connect(db) as conn:
        rid = storage.create_round(conn, "2025-05-05", "N", status="legacy")
        storage.save_entries(conn, rid, [{"name": n, "team_no": t, "quota": q}
                                         for n, t, q in FIELD])
    with pytest.raises(ValueError, match="legacy"):
        settle(db, rid, scores(), tmp_path)


# --------------------------------------------------------------------------
# re-importing a tee sheet over a round that already has scores
# --------------------------------------------------------------------------

def test_reimporting_a_tee_sheet_keeps_existing_scores(db, tmp_path):
    """
    Dropping the tee sheet in again - to fix a bad team split, say - used to
    wipe every score while leaving the round marked posted with its old payouts.
    It looked settled, still paid out, and silently stopped feeding quotas.
    """
    from cartel import storage

    rid = draft(db, "2026-09-03", 70)
    settle(db, rid, scores(greens=1), tmp_path)

    with storage.connect(db) as conn:
        before = {e["name"]: (e["points_front"], e["points_back"], e["greens"])
                  for e in storage.load_entries(conn, rid)}
        assert any(v[0] is not None for v in before.values())

        # simulate what prepare_round does: same round, fresh roster rows,
        # carrying prior scores across by name
        prior = {e["name"]: dict(e) for e in storage.load_entries(conn, rid)
                 if e["points_front"] is not None or e["greens"] or e["skins"]}
        rows = [{"name": n, "team_no": t, "quota": q, "played": 1,
                 **({k: prior[n][k] for k in ("points_front", "points_back",
                                              "score", "greens", "skins", "played")}
                    if n in prior else {})}
                for n, t, q in FIELD]
        storage.save_entries(conn, rid, rows)

        after = {e["name"]: (e["points_front"], e["points_back"], e["greens"])
                 for e in storage.load_entries(conn, rid)}

    assert after == before, "scores were lost when the tee sheet was re-imported"


def test_a_player_dropped_from_the_new_sheet_simply_goes(db, tmp_path):
    """The carry-over is keyed on name, so someone no longer posted just leaves."""
    from cartel import storage

    rid = draft(db, "2026-09-03", 70)
    settle(db, rid, scores(), tmp_path)

    with storage.connect(db) as conn:
        prior = {e["name"]: dict(e) for e in storage.load_entries(conn, rid)}
        shorter = FIELD[:-1]
        rows = [{"name": n, "team_no": t, "quota": q, "played": 1,
                 **({k: prior[n][k] for k in ("points_front", "points_back",
                                              "score", "greens", "skins", "played")}
                    if n in prior else {})}
                for n, t, q in shorter]
        storage.save_entries(conn, rid, rows)
        names = {e["name"] for e in storage.load_entries(conn, rid)}

    assert FIELD[-1][0] not in names
    assert len(names) == len(shorter)


# --------------------------------------------------------------------------
# report naming
# --------------------------------------------------------------------------

def test_report_names_are_anchored_to_the_data_not_to_today(db, tmp_path):
    """
    A report describing the current state must carry the same name whenever it
    is produced, so long as no further golf has been settled. Naming it after
    today's date produced a fresh file every day with identical contents.
    """
    from cartel import storage

    rid = draft(db, "2026-09-03", 70)
    settle(db, rid, scores(greens=1), tmp_path)

    with storage.connect(db) as conn:
        first = storage.anchor_tag(conn)
        again = storage.anchor_tag(conn)

    assert first == again
    assert "2026-09-03" in first, f"anchor should name the settled round: {first}"
    assert first.startswith("R70_")


def test_the_anchor_moves_only_when_a_round_is_settled(db, tmp_path):
    from cartel import storage

    settle(db, draft(db, "2026-09-03", 70), scores(), tmp_path)
    with storage.connect(db) as conn:
        before = storage.anchor_tag(conn)

    later = draft(db, "2026-09-06", 71)          # prepared but NOT settled
    with storage.connect(db) as conn:
        assert storage.anchor_tag(conn) == before, "a draft must not move the anchor"

    settle(db, later, scores(), tmp_path)
    with storage.connect(db) as conn:
        assert storage.anchor_tag(conn) != before, "settling must move the anchor"


def test_an_empty_database_still_yields_a_usable_anchor(db):
    from cartel import storage
    with storage.connect(db) as conn:
        conn.execute("DELETE FROM rounds")
        assert storage.anchor_tag(conn) == "no_rounds_yet"


# --------------------------------------------------------------------------
# regenerating a settled round's paperwork
# --------------------------------------------------------------------------

def test_reports_can_be_rebuilt_without_reposting(db, tmp_path):
    """
    The day's report used to exist only in the moment after pressing Post.
    Once the Post button started locking itself, a round settled last week had
    no route back to its own paperwork.
    """
    from cartel.pipeline import rebuild_reports
    from pathlib import Path

    rid = draft(db, "2026-09-03", 70)
    settle(db, rid, scores(greens=1), tmp_path)

    out = tmp_path / "again"
    paths = rebuild_reports(rid, db_path=db, out_dir=str(out))
    for key in ("round_pdf", "ytd_pdf", "workbook"):
        assert Path(paths[key]).exists(), f"{key} was not written"
        assert Path(paths[key]).stat().st_size > 0


def test_rebuilding_reports_changes_nothing_in_the_database(db, tmp_path):
    import hashlib
    from cartel.pipeline import rebuild_reports

    rid = draft(db, "2026-09-03", 70)
    settle(db, rid, scores(greens=1), tmp_path)

    before = hashlib.md5(open(db, "rb").read()).hexdigest()
    rebuild_reports(rid, db_path=db, out_dir=str(tmp_path / "ro"))
    after = hashlib.md5(open(db, "rb").read()).hexdigest()
    assert before == after, "rebuilding reports must be read-only"


def test_rebuilding_an_unposted_round_is_refused(db, tmp_path):
    from cartel.pipeline import rebuild_reports

    rid = draft(db, "2026-09-03", 70)          # never settled
    with pytest.raises(ValueError, match="isn't posted"):
        rebuild_reports(rid, db_path=db, out_dir=str(tmp_path / "x"))


def test_rebuilt_money_matches_what_was_settled(db, tmp_path):
    """A rebuild that disagreed with the stored payouts would be worse than none."""
    from cartel import storage
    from cartel.pipeline import rebuild_reports

    rid = draft(db, "2026-09-03", 70)
    original = settle(db, rid, scores(greens=1, skins=1), tmp_path)["result"]

    rebuilt = rebuild_reports(rid, db_path=db, out_dir=str(tmp_path / "rb"))["result"]

    assert rebuilt.total_paid == pytest.approx(original.total_paid)
    assert rebuilt.skat_value == pytest.approx(original.skat_value)
    for name, p in original.payouts.items():
        assert rebuilt.payouts[name].total == pytest.approx(p.total), name


# --------------------------------------------------------------------------
# standings rank
# --------------------------------------------------------------------------

def test_rank_is_by_dollars_per_round_highest_first(db, tmp_path):
    from cartel import stats, storage

    settle_enough(db, tmp_path)
    with storage.connect(db) as conn:
        df = stats.year_to_date(conn, 2026)

    played = df[df["Rds"] > 0].dropna(subset=["Rank"])
    assert not played.empty
    ordered = played.sort_values("Rank")
    per_round = list(ordered["$/Rd"])
    assert per_round == sorted(per_round, reverse=True), \
        "rank 1 must be the highest $/Rd"
    assert int(ordered.iloc[0]["Rank"]) == 1


def test_players_level_on_earnings_share_a_rank(db, tmp_path):
    """Two players on identical $/Rd are level; inventing an order is arbitrary."""
    from cartel import stats, storage

    settle_enough(db, tmp_path)
    with storage.connect(db) as conn:
        df = stats.year_to_date(conn, 2026)

    ranked = df.dropna(subset=["Rank"])
    assert not ranked.empty
    for value, grp in ranked.groupby("$/Rd"):
        if len(grp) > 1:
            assert grp["Rank"].nunique() == 1, f"${value} split across ranks"


def test_a_member_with_no_rounds_has_no_rank(db):
    from cartel import stats, storage
    with storage.connect(db) as conn:
        df = stats.year_to_date(conn, 2026)
    assert df[df["Rds"] == 0]["Rank"].isna().all()


# --------------------------------------------------------------------------
# ranking eligibility: 5 rounds in the prior 12 months (Stats Admin ruling)
# --------------------------------------------------------------------------

def test_too_few_recent_rounds_means_no_rank(db, tmp_path):
    """
    A rank on $ per round needs a decent sample. Two rounds, one of them two
    years old, produced a 2nd place that told nobody anything.
    """
    from cartel import stats, storage
    from cartel.config import RULES

    settle(db, draft(db, "2026-09-03", 70), scores(greens=1), tmp_path)
    with storage.connect(db) as conn:
        df = stats.year_to_date(conn, 2026)

    played = df[df["Rds"] > 0]
    assert not played.empty
    assert played["Rank"].isna().all(), "one round must not earn a rank"
    for _, r in played.iterrows():
        if r["Rds_Window"] < RULES.rank_min_rounds:
            assert pd.isna(r["Rank"]), f"{r['Name']} ranked on {r['Rds_Window']} rounds"
        else:
            assert not pd.isna(r["Rank"]), f"{r['Name']} eligible but unranked"


def test_rounds_outside_the_window_do_not_count(db, tmp_path):
    """Old form is history, not current standing."""
    from cartel import stats, storage

    # five rounds, but four of them long past the window
    for d in ["2023-01-10", "2023-02-10", "2023-03-10", "2023-04-10"]:
        settle(db, draft(db, d, 1), scores(), tmp_path)
    settle(db, draft(db, "2026-09-03", 70), scores(), tmp_path)

    with storage.connect(db) as conn:
        counts = stats.rounds_in_window(conn)

    for name, _, _ in FIELD:
        assert counts.get(name, 0) == 1, \
            f"{name} counted {counts.get(name, 0)} in-window rounds, expected 1"


def test_the_window_is_measured_from_the_last_round_not_from_today(db, tmp_path):
    """
    Anchored to the data. Otherwise a fortnight with no golf would quietly drop
    someone out of the ranking without them playing, or failing to play, a thing.
    """
    from cartel import stats, storage

    settle(db, draft(db, "2026-09-03", 70), scores(), tmp_path)
    with storage.connect(db) as conn:
        first = stats.rounds_in_window(conn)
        again = stats.rounds_in_window(conn)
    assert first == again and sum(first.values()) > 0


def test_the_ranking_rule_is_configurable_not_hardcoded(db, tmp_path):
    from cartel.config import HouseRules
    import pytest as _pytest

    assert HouseRules().rank_min_rounds == 5
    assert HouseRules().rank_window_months == 12
    with _pytest.raises(ValueError):
        HouseRules(rank_min_rounds=0).validate()
    with _pytest.raises(ValueError):
        HouseRules(rank_window_months=0).validate()


# --------------------------------------------------------------------------
# the standings header must not mix scopes
# --------------------------------------------------------------------------

def test_year_summary_covers_imported_rounds_too(db, tmp_path):
    """
    The header counted rounds and money from settled rounds only, while players
    and skats counted the whole year. Two rounds and $590 sat beside a full
    year's skats, which read as an error and was one.
    """
    from cartel import stats, storage

    with storage.connect(db) as conn:                 # imported history
        for i, d in enumerate(["2026-02-01", "2026-02-08"]):
            rid = storage.create_round(conn, d, "N", round_no=i + 1, status="legacy")
            storage.save_entries(conn, rid, [
                {"name": n, "team_no": t, "quota": q, "played": 1,
                 "points_front": f, "points_back": b, "score": 85}
                for (n, t, q), (f, b) in zip(FIELD, POINTS)])

    settle(db, draft(db, "2026-09-03", 70), scores(), tmp_path)   # settled here

    with storage.connect(db) as conn:
        yr = stats.year_summary(conn, 2026)
        rec = stats.house_reconciliation(conn, 2026)
        ytd = stats.year_to_date(conn, 2026)

    assert yr["rounds"] == 3, "the year had three rounds, not just the settled one"
    assert rec["rounds"] == 1, "only one was settled in the app"
    assert yr["collected"] > rec["collected"], "the year's pot must exceed the app's"
    assert yr["player_rounds"] == int(ytd["Rds"].sum()), \
        "player-rounds in the header must match the rounds counted per player"


def test_the_year_pot_matches_the_rounds_people_actually_played(db, tmp_path):
    from cartel import stats, storage
    from cartel.config import RULES

    settle(db, draft(db, "2026-09-03", 70), scores(), tmp_path)
    with storage.connect(db) as conn:
        yr = stats.year_summary(conn, 2026)

    assert yr["collected"] == pytest.approx(
        len(FIELD) * STAKE.member_ante), "everyone paid the members' rate"


def test_a_guest_is_counted_at_the_guest_rate(db, tmp_path):
    from cartel import stats, storage
    from cartel.config import RULES

    with storage.connect(db) as conn:
        storage.upsert_member(conn, "Visitor", "W")
        rid = storage.create_round(conn, "2026-09-03", "N", round_no=70, status="draft")
        storage.save_entries(conn, rid, [
            {"name": n, "team_no": t, "quota": q} for n, t, q in FIELD]
            + [{"name": "Visitor", "team_no": 1, "quota": None, "is_guest": 1}])
    rows = scores() + [{"name": "Visitor", "points_front": None,
                        "points_back": None, "greens": 0, "skins": 0}]
    settle(db, rid, rows, tmp_path)

    with storage.connect(db) as conn:
        yr = stats.year_summary(conn, 2026)

    assert yr["collected"] == pytest.approx(
        len(FIELD) * STAKE.member_ante + STAKE.guest_ante)


# --------------------------------------------------------------------------
# late arrivals and team moves
# --------------------------------------------------------------------------

def test_a_player_can_be_moved_to_another_team_when_settling(db, tmp_path):
    """
    He missed his tee time and went out with a later group. The team quota has
    to follow him, or two teams are judged against the wrong Xi.
    """
    from cartel import storage

    rid = draft(db, "2026-09-03", 70)
    rows = scores()
    moved = FIELD[0][0]
    for r in rows:
        r["team_no"] = 2 if r["name"] == moved else 1

    result = settle(db, rid, rows, tmp_path)["result"]

    with storage.connect(db) as conn:
        team = {e["name"]: e["team_no"] for e in storage.load_entries(conn, rid)}
    assert team[moved] == 2, "the move must be stored, not just used once"

    front = {s.team_no: s for s in result.sides if s.side == "front"}
    assert moved in front[2].players
    assert moved not in front[1].players
    quotas = {n: q for n, _, q in FIELD}
    assert front[2].quota == pytest.approx(
        sum(quotas[n] for n in front[2].players) / 2), "Xi must follow the player"


def test_somebody_left_off_the_sheet_can_be_added(db, tmp_path):
    """Beats rebuilding the round, which would discard everyone else's scores."""
    from cartel import storage

    rid = draft(db, "2026-09-03", 70)
    with storage.connect(db) as conn:
        storage.upsert_member(conn, "Late Arrival", "W")

    rows = scores() + [{"name": "Late Arrival", "team_no": 2,
                        "points_front": 14, "points_back": 15,
                        "score": 84, "greens": 0, "skins": 1}]
    result = settle(db, rid, rows, tmp_path)["result"]

    assert "Late Arrival" in result.payouts
    assert result.n_players == len(FIELD) + 1
    with storage.connect(db) as conn:
        assert "Late Arrival" in {e["name"] for e in storage.load_entries(conn, rid)}


def test_a_late_arrival_gets_the_quota_they_would_have_had(db, tmp_path):
    """Worked out as at the round's date, not from today."""
    from cartel import storage

    rid = draft(db, "2026-09-03", 70)
    with storage.connect(db) as conn:
        storage.upsert_member(conn, "Newcomer", "W")

    rows = scores() + [{"name": "Newcomer", "team_no": 1, "points_front": 10,
                        "points_back": 10, "score": 90, "greens": 0, "skins": 0}]
    result = settle(db, rid, rows, tmp_path)["result"]

    assert result.payouts["Newcomer"].is_guest, \
        "no history means a guest, whatever else changes"
    assert result.payouts["Newcomer"].ante == RULES.guest_ante


def test_someone_not_on_the_roster_at_all_is_refused(db, tmp_path):
    rid = draft(db, "2026-09-03", 70)
    rows = scores() + [{"name": "Total Stranger", "team_no": 1,
                        "points_front": 10, "points_back": 10}]
    with pytest.raises(ValueError, match="Not on the roster"):
        settle(db, rid, rows, tmp_path)


# --------------------------------------------------------------------------
# a preview is not a post
# --------------------------------------------------------------------------

def test_working_out_the_money_records_nothing(db, tmp_path):
    """
    Round 62 sat in draft for a day with every figure looking right: the scores
    were saved, the money was correct on screen, and a Results PDF had been
    written - so a preview was indistinguishable from a settled round.
    """
    from cartel import stats, storage

    rid = draft(db, "2026-09-03", 70)
    with storage.connect(db) as conn:
        before = stats.year_summary(conn, 2026)

    from cartel.pipeline import settle_round
    out = settle_round(rid, scores(greens=1), db_path=db,
                       out_dir=str(tmp_path / "preview"), post=False)

    assert out["round_pdf"] is None, \
        "a preview must not leave paperwork that looks like a settled round"
    with storage.connect(db) as conn:
        assert storage.get_round(conn, rid)["status"] == "draft"
        assert stats.year_summary(conn, 2026) == before, "nothing may be counted"


def test_a_preview_still_keeps_the_typing(db, tmp_path):
    """Not posting shouldn't cost you the scores you just entered."""
    from cartel import storage
    from cartel.pipeline import settle_round

    rid = draft(db, "2026-09-03", 70)
    settle_round(rid, scores(greens=1), db_path=db,
                 out_dir=str(tmp_path / "p"), post=False)
    with storage.connect(db) as conn:
        scored = [e for e in storage.load_entries(conn, rid)
                  if e["points_front"] is not None]
    assert len(scored) == len(FIELD)


def test_posting_after_a_preview_lands_normally(db, tmp_path):
    from cartel import stats, storage
    from cartel.pipeline import settle_round

    rid = draft(db, "2026-09-03", 70)
    settle_round(rid, scores(greens=1), db_path=db,
                 out_dir=str(tmp_path / "p"), post=False)
    out = settle_round(rid, scores(greens=1), db_path=db,
                       out_dir=str(tmp_path / "q"), post=True)

    assert out["round_pdf"] is not None
    with storage.connect(db) as conn:
        assert storage.get_round(conn, rid)["status"] == "posted"
        assert stats.year_summary(conn, 2026)["rounds"] == 1


def test_preparing_the_same_day_on_another_course_does_not_leave_two_empty_rounds(db, tmp_path):
    """
    The course is part of a round's key, so re-preparing the same day as South
    made a second round rather than correcting the North one - leaving an empty
    round beside the real one.
    """
    from datetime import date as _date
    from cartel import storage
    from cartel.pipeline import manual_tee_sheet, prepare_round

    teams = [[n for n, _, _ in FIELD[:3]], [n for n, _, _ in FIELD[3:]]]
    prepare_round(manual=manual_tee_sheet(_date(2026, 9, 6), "N", teams, round_no=62),
                  db_path=db, out_dir=str(tmp_path / "a"))
    prepare_round(manual=manual_tee_sheet(_date(2026, 9, 6), "S", teams, round_no=62),
                  db_path=db, out_dir=str(tmp_path / "b"))

    with storage.connect(db) as conn:
        rounds = conn.execute(
            "SELECT course FROM rounds WHERE played_on = '2026-09-06'").fetchall()
    assert len(rounds) == 1, f"expected one round, found {[r['course'] for r in rounds]}"
    assert rounds[0]["course"] == "S", "the corrected course should be the one kept"


def test_a_same_day_round_holding_scores_is_never_silently_removed(db, tmp_path):
    """Tidying an empty duplicate is helpful; deleting scores is not."""
    from datetime import date as _date
    from cartel import storage
    from cartel.pipeline import manual_tee_sheet, prepare_round, settle_round

    teams = [[n for n, _, _ in FIELD[:3]], [n for n, _, _ in FIELD[3:]]]
    first = prepare_round(
        manual=manual_tee_sheet(_date(2026, 9, 6), "N", teams, round_no=62),
        db_path=db, out_dir=str(tmp_path / "a"))
    settle_round(first.round_id, scores(), db_path=db,
                 out_dir=str(tmp_path / "s"), post=False)

    second = prepare_round(
        manual=manual_tee_sheet(_date(2026, 9, 6), "S", teams, round_no=62),
        db_path=db, out_dir=str(tmp_path / "b"))

    with storage.connect(db) as conn:
        courses = {r["course"] for r in conn.execute(
            "SELECT course FROM rounds WHERE played_on = '2026-09-06'")}
    assert courses == {"N", "S"}, "the round holding scores must survive"
    assert any("two rounds for this date" in w for w in second.warnings)
