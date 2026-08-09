"""
Variable stakes, and the entry checks.

The stake can change; already-played rounds must not. These tests exist because
money that silently restates itself is worse than money that is simply wrong -
wrong is noticed.
"""
from __future__ import annotations

import pytest

from cartel import storage
from cartel.config import LEGACY_STAKE, RULES, Stake
from cartel.scoring import PlayerEntry, check_entries, score_round


# --------------------------------------------------------------------------
# the split is derived, never configured
# --------------------------------------------------------------------------

@pytest.mark.parametrize("ante", [20, 25, 30, 50, 100])
def test_a_member_ante_always_splits_quarter_quarter_half(ante):
    s = Stake(member_ante=ante, guest_ante=ante / 2)
    assert s.team_per_side == pytest.approx(ante * 0.25)
    assert s.skat_per_member == pytest.approx(ante * 0.50)
    assert 2 * s.team_per_side + s.skat_per_member == pytest.approx(ante), \
        "the three shares must add back to the ante at any stake"


def test_the_split_cannot_be_set_by_hand():
    """Three numbers kept consistent by hand is three chances to get it wrong."""
    with pytest.raises(TypeError):
        Stake(member_ante=20, guest_ante=10, team_per_side=99)


def test_a_guest_under_half_the_member_ante_is_refused():
    """House rule, Cartel Stats Admin, Aug 2026."""
    Stake(50, 25).validate()
    Stake(50, 30).validate()
    with pytest.raises(ValueError, match="half the member ante"):
        Stake(50, 20).validate()


def test_a_free_guest_is_refused():
    with pytest.raises(ValueError):
        Stake(20, 0).validate()


# --------------------------------------------------------------------------
# the pots at any stake
# --------------------------------------------------------------------------

def field(members=11, guests=1):
    e = [PlayerEntry(f"M{i}", 1 + i % 3, 20, 10, 10) for i in range(members)]
    if e:
        e[0].greens = 1
    e += [PlayerEntry(f"G{i}", 1, quota=None) for i in range(guests)]
    return e


@pytest.mark.parametrize("member,guest", [(20, 10), (50, 25), (50, 30), (100, 60)])
def test_every_dollar_staked_comes_back_out(member, guest):
    r = score_round(field(), stake=Stake(member, guest))
    r.check_balance()


def test_the_skat_pot_is_what_people_actually_paid_in(member=50, guest=30):
    """
    Sized on contributions, not on a member's share times everybody. The moment
    the two antes differ, the old way credited a guest with money they never
    staked - and the books would not have balanced.
    """
    r = score_round(field(members=11, guests=1), stake=Stake(member, guest))
    assert r.skat_pot == pytest.approx(11 * member * 0.5 + 1 * guest)
    assert r.skat_pot == pytest.approx(11 * 25 + 30)


def test_a_guest_pays_the_guest_rate_whatever_it_is():
    r = score_round(field(members=4, guests=1), stake=Stake(50, 30))
    assert r.payouts["G0"].ante == 30.0
    assert r.payouts["M0"].ante == 50.0
    assert r.total_collected == pytest.approx(4 * 50 + 30)


def test_guests_never_enlarge_the_team_pot_at_any_stake():
    r = score_round(field(members=4, guests=3), stake=Stake(50, 30))
    assert r.pot_per_side == pytest.approx(4 * 12.50), "four members, not seven"


def test_a_refund_returns_what_each_player_staked():
    from cartel.config import HouseRules
    rules = HouseRules(no_skat_policy="refund")
    entries = [PlayerEntry(f"M{i}", 1 + i % 2, 20, 10, 10) for i in range(4)]
    entries.append(PlayerEntry("G", 1, quota=None))
    r = score_round(entries, rules=rules, stake=Stake(50, 30))
    assert r.payouts["M0"].skat_money == pytest.approx(25.0)
    assert r.payouts["G"].skat_money == pytest.approx(30.0), \
        "a guest gets back what they put in, not a member's share"
    r.check_balance()


# --------------------------------------------------------------------------
# a stake change must not disturb the past
# --------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "c.db")
    monkeypatch.setenv("CARTEL_DB", path)
    monkeypatch.setenv("CARTEL_BACKUP_DIR", str(tmp_path / "bk"))
    monkeypatch.delenv("CARTEL_DB_URL", raising=False)
    storage.init_db(path)
    return path


def test_a_new_database_opens_at_the_legacy_rate(db):
    with storage.connect(db) as conn:
        assert storage.current_stake(conn).member_ante == LEGACY_STAKE.member_ante
        assert storage.current_stake(conn).guest_ante == LEGACY_STAKE.guest_ante


def test_changing_the_stake_keeps_the_old_one_on_record(db):
    with storage.connect(db) as conn:
        storage.set_stake(conn, 50, 25, "2026-09-01", "agreed at the AGM")
        history = storage.stake_history(conn)
        assert len(history) == 2, "a change adds to the history, never overwrites"
        assert storage.current_stake(conn).member_ante == 50


def test_a_round_keeps_the_stake_it_was_prepared_at(db):
    """The whole point: announce a change, and last week's round is untouched."""
    with storage.connect(db) as conn:
        rid = storage.create_round(conn, "2026-08-01", "N", round_no=61)
        conn.execute("UPDATE rounds SET member_ante=?, guest_ante=? WHERE round_id=?",
                     (20.0, 10.0, rid))
        storage.set_stake(conn, 50, 25, "2026-09-01", "doubled")

        assert storage.current_stake(conn).member_ante == 50
        assert storage.round_stake(conn, rid).member_ante == 20, \
            "a settled round must not be restated when the stake moves"


def test_the_season_pot_uses_each_rounds_own_rate(db, tmp_path):
    """
    Applying today's stake to imported rounds would restate the whole season
    the day the stake changed - the exact bug fixed on Standings and Health.
    """
    from cartel import stats
    with storage.connect(db) as conn:
        for name in ("A", "B"):
            storage.upsert_member(conn, name, "W")
        for date_, rate in (("2026-03-01", 20.0), ("2026-09-01", 50.0)):
            rid = storage.create_round(conn, date_, "N", status="legacy")
            conn.execute(
                "UPDATE rounds SET member_ante=?, guest_ante=? WHERE round_id=?",
                (rate, rate / 2, rid))
            storage.save_entries(conn, rid, [
                {"name": n, "team_no": 1, "quota": 20, "played": 1,
                 "points_front": 10, "points_back": 10, "score": 85}
                for n in ("A", "B")])

    with storage.connect(db) as conn:
        summary = stats.year_summary(conn, 2026)

    assert summary["collected"] == pytest.approx(2 * 20 + 2 * 50), \
        "each round must be counted at the rate it was played at"


# --------------------------------------------------------------------------
# entry checks
# --------------------------------------------------------------------------

def test_a_blow_up_is_queried_not_blocked():
    """Six players have genuinely scored 0 on a side, all with 99+ scores."""
    q = check_entries([PlayerEntry("X", 1, 20, 0, 14, score=104)])
    assert len(q) == 1 and q[0].field == "front points"


def test_a_career_nine_is_queried_not_blocked():
    q = check_entries([PlayerEntry("X", 1, 20, 28, 15, score=67)])
    assert any("exceptional" in x.message for x in q)


def test_a_points_total_typed_into_the_score_column_is_caught():
    """Both real errors in four years of history were exactly this."""
    q = check_entries([PlayerEntry("X", 1, 20, 11, 10, score=21)])
    assert any("Score column" in x.message for x in q)


def test_an_ordinary_bad_day_is_left_alone():
    """100-109 happens roughly monthly. Nagging on it teaches people to click through."""
    assert check_entries([PlayerEntry("X", 1, 20, 8, 9, score=105)]) == []


def test_a_really_high_score_is_still_queried():
    q = check_entries([PlayerEntry("X", 1, 20, 8, 9, score=115)])
    assert any("worth confirming" in x.message for x in q)


def test_an_ordinary_round_raises_nothing():
    assert check_entries([PlayerEntry("X", 1, 20, 15, 16, score=82)]) == []


def test_a_guest_is_never_queried_for_missing_points():
    assert check_entries([PlayerEntry("Guest", 1, quota=None)]) == []


def test_hard_limits_come_from_the_course_not_from_the_trend():
    """
    Set from what the course allows, not from what has happened. A limit built
    on four years of history blocks the first legitimate outlier, and Murphy
    guarantees there will be one.
    """
    from cartel.config import RULES
    from cartel.scoring import HARD_LIMITS

    assert HARD_LIMITS["skins"] == (0, RULES.holes), \
        "one skin per hole, so a player could theoretically win all 18"
    assert HARD_LIMITS["greens"][1] > RULES.par_threes - 1


def test_eleven_skins_is_allowed_but_asks_you_to_re_read_the_sheet():
    q = check_entries([PlayerEntry("Hot", 1, 20, 15, 16, score=70, skins=11)])
    assert q, "11 skins should be queried"
    assert any("every player's skins" in x.message for x in q), \
        "a misread column usually shows in more than one row"


def test_eighteen_skins_for_one_player_is_not_blocked():
    """Absurd, but arithmetically possible. The app must not stand in the way."""
    from cartel.scoring import HARD_LIMITS
    assert 18 <= HARD_LIMITS["skins"][1]
    r = score_round([PlayerEntry("Perfect", 1, 20, 20, 20, score=60, skins=18),
                     PlayerEntry("Other", 2, 20, 10, 10, score=90)])
    r.check_balance()
    assert r.total_skats == 18


def test_more_skins_than_holes_is_flagged_as_impossible():
    """The strong check: a hole yields at most one skin, whoever won it."""
    from cartel.config import RULES
    entries = [PlayerEntry(f"P{i}", 1 + i % 2, 20, 12, 13, score=82, skins=5)
               for i in range(4)]          # 20 skins across the field
    q = check_entries(entries)
    assert any(x.name == "The whole field" and x.field == "skins" for x in q)
    assert any(str(RULES.holes) in x.message for x in q)


def test_a_busy_but_possible_round_is_left_alone():
    """13 field skins is the most ever recorded. It must pass silently."""
    entries = [PlayerEntry(f"P{i}", 1 + i % 2, 20, 12, 13, score=82, skins=s)
               for i, s in enumerate([4, 3, 2, 2, 1, 1])]
    assert check_entries(entries) == []


def test_more_greens_than_par_threes_is_flagged():
    from cartel.config import RULES
    entries = [PlayerEntry(f"P{i}", 1 + i % 2, 20, 12, 13, score=82, greens=2)
               for i in range(RULES.par_threes)]     # twice the par 3s
    q = check_entries(entries)
    assert any(x.name == "The whole field" and x.field == "greens" for x in q)


# --------------------------------------------------------------------------
# writing off money that can never be paid out
# --------------------------------------------------------------------------

def test_a_write_off_balances_the_year_without_touching_anyone(db):
    """
    The shortfall came from the old system failing to record a winning team, so
    nobody won it. Spreading it over players would put figures in the standings
    that never happened; recording it as a write-off states the truth instead.
    """
    from datetime import date
    from cartel import stats

    with storage.connect(db) as conn:
        for n in ("A", "B"):
            storage.upsert_member(conn, n, "W")
        rid = storage.create_round(conn, "2026-03-01", "N", status="legacy")
        storage.save_entries(conn, rid, [
            {"name": n, "team_no": 1, "quota": 20, "played": 1,
             "points_front": 10, "points_back": 10, "score": 85} for n in ("A", "B")])
        storage.set_seed(conn, "A", 2026, 10.0, 5.0)      # deliberately short

        before = stats.year_to_date(conn, 2026)["Won$"].sum()
        y = stats.year_summary(conn, 2026)
        gap = y["collected"] - before
        assert gap > 0

        storage.write_off(conn, 2026, gap, "legacy shortfall", date.today().isoformat())

        y2 = stats.year_summary(conn, 2026)
        after = stats.year_to_date(conn, 2026)["Won$"].sum()

    assert y2["written_off"] == pytest.approx(gap)
    assert y2["collected"] - after - y2["written_off"] == pytest.approx(0.0)
    assert after == pytest.approx(before), "no player's winnings may change"


def test_a_write_off_keeps_its_reason(db):
    from datetime import date
    with storage.connect(db) as conn:
        storage.write_off(conn, 2026, 53.42, "sides with no winning team recorded",
                          date.today().isoformat())
        hist = storage.writeoff_history(conn, 2026)
    assert len(hist) == 1
    assert "no winning team" in hist[0]["reason"], \
        "an unexplained adjustment is worse than a visible discrepancy"


def test_write_offs_are_per_year(db):
    from datetime import date
    with storage.connect(db) as conn:
        storage.write_off(conn, 2026, 53.42, "x", date.today().isoformat())
        assert storage.written_off(conn, 2026) == pytest.approx(53.42)
        assert storage.written_off(conn, 2027) == 0.0
