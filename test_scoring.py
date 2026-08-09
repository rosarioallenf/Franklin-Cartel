"""
Tests for the house rules.

The point of these is that the money is arguable in a way code usually isn't.
When somebody says "hang on, that's not how we do guests", the fix belongs in
config.py and the argument belongs here.

Run:  python -m pytest tests -q
"""
from __future__ import annotations

import pytest

from cartel.config import HouseRules, RULES

STAKE = RULES.default_stake()
from cartel.quota import compute_quota, team_side_quota
from cartel.scoring import PlayerEntry, score_round


# --------------------------------------------------------------------------
# quota
# --------------------------------------------------------------------------

def test_quota_is_rounded_mean_of_last_five():
    assert compute_quota("x", [35, 35, 35, 35, 35]).quota == 35


def test_quota_ignores_anything_past_the_window():
    q = compute_quota("x", [30, 30, 30, 30, 30, 1, 1, 1])
    assert q.quota == 30
    assert q.rounds_used == 5


def test_quota_rounds_half_up_not_down():
    # 28.6 -> 29 on the Access report; truncation would have said 28
    assert compute_quota("x", [29, 29, 29, 28, 28]).quota == 29
    # a two-round window, on a player with plenty of history, to land on .5
    two = HouseRules(quota_window=2)
    assert compute_quota("x", [14, 13], two, rounds_available=20).quota == 14
    assert compute_quota("x", [13, 12], two, rounds_available=20).quota == 13


def test_quota_half_even_is_available_but_not_the_default():
    assert RULES.quota_rounding == "half_up"
    r = HouseRules(quota_rounding="half_even", quota_window=2)
    assert compute_quota("x", [13, 12], r, rounds_available=20).quota == 12


def test_three_or_four_rounds_gets_a_quota_from_what_there_is():
    """Austin Polivka has 4 rounds on file and a real quota of 37."""
    q = compute_quota("x", [40, 38, 36, 34])
    assert not q.is_guest
    assert q.quota == 37
    assert q.short_history and q.needs_review


def test_exactly_three_rounds_is_a_member_not_a_guest():
    """The rule is FEWER than three, so three qualifies. B Lawrence has 3."""
    q = compute_quota("x", [23, 23, 23])
    assert not q.is_guest
    assert q.quota == 23


def test_fewer_than_three_rounds_is_a_guest_with_no_quota():
    for history in ([], [20], [20, 30]):
        q = compute_quota("newbie", history)
        assert q.is_guest, history
        assert q.quota is None
        assert q.display == "guest"
        assert q.needs_review


def test_the_guest_threshold_is_configurable():
    lenient = HouseRules(guest_min_rounds=1)
    assert not compute_quota("x", [20], lenient).is_guest
    strict = HouseRules(guest_min_rounds=5)
    assert compute_quota("x", [20, 20, 20, 20], strict).is_guest


def test_rounds_available_drives_the_guest_test_not_the_window():
    """The caller may only fetch 5 rounds; the guest test needs the true count."""
    q = compute_quota("x", [20, 20], rounds_available=40)
    assert not q.is_guest and q.quota == 20


def test_team_side_quota_is_half_the_sum_and_drops_guests():
    assert team_side_quota([35, 27, 27]) == 44.5              # Round 60, Team 1
    assert team_side_quota([13, 26, 34, 25]) == 49.0          # Round 60, Team 3
    assert team_side_quota([35, 27, 27, None]) == 44.5        # guest contributes nothing


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def entry(name, team, quota, front, back, greens=0, skins=0, score=None):
    return PlayerEntry(name=name, team_no=team, quota=quota, points_front=front,
                       points_back=back, score=score, greens=greens, skins=skins)


def guest(name, team, front=None, back=None, greens=0, skins=0):
    """A guest's points are usually never written down, hence the defaults."""
    return PlayerEntry(name=name, team_no=team, quota=None, points_front=front,
                       points_back=back, greens=greens, skins=skins)


def simple_round(front_a=30, back_a=30, front_b=20, back_b=20, **kw):
    """Two teams of two. Team A beats team B unless told otherwise."""
    return [
        entry("A1", 1, 20, front_a // 2, back_a // 2, **kw),
        entry("A2", 1, 20, front_a - front_a // 2, back_a - back_a // 2),
        entry("B1", 2, 20, front_b // 2, back_b // 2),
        entry("B2", 2, 20, front_b - front_b // 2, back_b - back_b // 2),
    ]


# --------------------------------------------------------------------------
# team money
# --------------------------------------------------------------------------

def test_side_pot_is_five_dollars_times_every_eligible_player():
    r = score_round(simple_round())
    assert r.n_players == 4
    assert r.pot_per_side == 20.0


def test_winning_team_splits_the_whole_side_pot():
    r = score_round(simple_round())
    assert r.payouts["A1"].team_money == pytest.approx(20.0)
    assert r.payouts["B1"].team_money == 0.0


def test_winner_is_highest_net_not_highest_points():
    entries = [
        entry("Big1", 1, 40, 25, 25), entry("Big2", 1, 40, 25, 25),   # 50 pts, quota 40
        entry("Sml1", 2, 10, 15, 15), entry("Sml2", 2, 10, 15, 15),   # 30 pts, quota 10
    ]
    r = score_round(entries)
    winner = next(s for s in r.sides if s.side == "front" and s.is_winner)
    assert winner.team_no == 2                  # +20 beats +10
    assert r.payouts["Big1"].team_money == 0


def test_a_tie_splits_across_every_player_on_every_tied_team():
    r = score_round(simple_round(front_a=30, front_b=30))
    winners = r.winners("front")
    assert len(winners) == 2
    assert sum(w.payout_per_player * len(w.players) for w in winners) == pytest.approx(
        r.pot_per_side)
    assert any("tied" in w.lower() for w in r.warnings)


def test_a_three_way_tie_still_balances():
    entries = [entry(f"T{t}P{i}", t, 20, 10, 10) for t in (1, 2, 3) for i in (1, 2)]
    r = score_round(entries)
    assert len(r.winners("front")) == 3
    r.check_balance()
    for p in r.payouts.values():
        assert p.team_money == pytest.approx(2 * STAKE.team_per_side)


def test_uneven_teams_pay_differently_per_player_and_say_so():
    entries = [
        entry("A1", 1, 20, 20, 20), entry("A2", 1, 20, 20, 20),
        entry("B1", 2, 20, 5, 5), entry("B2", 2, 20, 5, 5), entry("B3", 2, 20, 5, 5),
    ]
    r = score_round(entries)
    assert r.pot_per_side == 25.0
    assert r.payouts["A1"].team_money == pytest.approx(2 * 12.5)
    assert any("uneven" in w.lower() for w in r.warnings)


# --------------------------------------------------------------------------
# guests
# --------------------------------------------------------------------------

def test_a_guest_pays_ten_not_twenty():
    entries = simple_round() + [guest("G", 1, 10, 10)]
    r = score_round(entries)
    assert r.payouts["G"].ante == 10.0
    assert r.payouts["A1"].ante == 20.0
    assert r.total_collected == 4 * 20.0 + 10.0


def test_a_guest_with_no_points_recorded_still_played():
    """Nobody writes a guest's points down, so a blank row is not a no-show."""
    r = score_round(simple_round() + [guest("G", 1)])
    assert "G" in r.payouts
    assert r.n_players == 5
    assert r.payouts["G"].ante == 10.0
    assert r.skat_pot == 5 * STAKE.skat_per_member      # the guest's $10 is in there
    r.check_balance()


def test_a_guest_marked_absent_is_out_of_everything():
    entries = simple_round() + [PlayerEntry("G", 1, quota=None, played=False)]
    r = score_round(entries)
    assert "G" not in r.payouts
    assert r.n_players == 4
    assert r.skat_pot == 4 * STAKE.skat_per_member
    assert any("not playing" in w.lower() for w in r.warnings)


def test_a_member_marked_played_with_no_points_is_flagged_not_absorbed():
    entries = simple_round() + [PlayerEntry("Sloppy", 1, quota=20, played=True)]
    r = score_round(entries)
    assert "Sloppy" not in r.payouts
    assert any("no points recorded" in w for w in r.warnings)
    r.check_balance()


def test_the_house_worked_example_eleven_members_and_one_guest():
    """$230 in: $110 team ($55 a side), $120 skats including the guest's $10."""
    entries = [entry(f"M{i}", 1 + i % 3, 20, 10, 10) for i in range(11)]
    entries.append(guest("Guest", 1))
    entries[0].greens = 1
    r = score_round(entries)
    assert r.n_players == 12 and r.n_team_players == 11 and r.n_guests == 1
    assert r.total_collected == 230.0
    assert r.pot_per_side == 55.0
    assert sum(s.payout_per_player * len(s.players) for s in r.sides if s.is_winner) \
        == pytest.approx(110.0)
    assert r.skat_pot == 120.0
    r.check_balance()


def test_the_house_worked_example_ten_members_and_two_guests():
    """$220 in: $100 team ($50 a side), $120 skats."""
    entries = [entry(f"M{i}", 1 + i % 2, 20, 10, 10) for i in range(10)]
    entries += [guest("G1", 1), guest("G2", 2)]
    entries[0].skins = 1
    r = score_round(entries)
    assert r.total_collected == 220.0
    assert r.pot_per_side == 50.0
    assert r.skat_pot == 120.0
    r.check_balance()


def test_a_guest_does_not_enlarge_the_team_pot():
    """$5 x eligible members only - the guest bought no team action."""
    r = score_round(simple_round() + [guest("G", 1, 10, 10)])
    assert r.n_players == 5
    assert r.n_team_players == 4
    assert r.pot_per_side == 20.0            # not 25


def test_a_guest_does_enlarge_the_skat_pot():
    r = score_round(simple_round() + [guest("G", 1, 10, 10, greens=1)])
    assert r.skat_pot == 5 * STAKE.skat_per_member


def test_a_guests_points_do_not_count_for_their_team():
    """A monstrous guest round must not carry the team."""
    entries = simple_round(front_a=10, back_a=10, front_b=30, back_b=30)
    entries.append(guest("Ringer", 1, 40, 40))
    r = score_round(entries)
    front_t1 = next(s for s in r.sides if s.side == "front" and s.team_no == 1)
    assert front_t1.points == 10             # the guest's 40 is excluded
    assert "Ringer" not in front_t1.players
    assert r.winners("front")[0].team_no == 2


def test_a_guest_is_not_in_their_teams_quota():
    entries = simple_round() + [guest("G", 1, 10, 10)]
    r = score_round(entries)
    front_t1 = next(s for s in r.sides if s.side == "front" and s.team_no == 1)
    assert front_t1.quota == 20.0            # (20+20)/2, guest adds nothing


def test_a_guest_takes_no_share_of_team_money_even_on_a_winning_team():
    r = score_round(simple_round() + [guest("G", 1, 10, 10)])
    assert r.winners("front")[0].team_no == 1
    assert r.payouts["G"].team_money == 0.0
    assert r.payouts["A1"].team_money > 0


def test_a_guest_wins_skat_money_like_anyone_else():
    entries = simple_round() + [guest("G", 1, 10, 10, greens=1)]
    r = score_round(entries)
    assert r.total_skats == 1
    assert r.payouts["G"].skat_money == pytest.approx(r.skat_pot)
    assert r.payouts["G"].net == pytest.approx(r.skat_pot - 10.0)


def test_a_guest_round_still_balances():
    entries = simple_round(greens=1) + [guest("G1", 1, skins=2), guest("G2", 2)]
    r = score_round(entries)
    r.check_balance()
    assert r.total_paid + r.carried_money == pytest.approx(
        r.total_collected + r.carry_in)


def test_quota_none_and_is_guest_can_never_disagree():
    assert PlayerEntry("x", 1, quota=None).is_guest
    assert PlayerEntry("x", 1, quota=25, is_guest=True).quota is None


def test_an_all_guest_field_pays_no_team_money_but_still_settles_skats():
    entries = [guest("G1", 1, greens=1), guest("G2", 1, skins=1)]
    r = score_round(entries)
    assert r.pot_per_side == 0.0
    assert all(p.team_money == 0 for p in r.payouts.values())
    assert r.skat_value == pytest.approx(10.0)
    r.check_balance()


def test_a_lone_eligible_team_gets_its_team_money_back():
    """No opponent means no contest, so nobody wins and nobody loses."""
    entries = [entry("A1", 1, 20, 10, 10), entry("A2", 1, 20, 10, 10),
               guest("G", 2, greens=1)]
    r = score_round(entries)
    assert any("one team" in w.lower() for w in r.warnings)
    assert r.payouts["A1"].team_money == pytest.approx(2 * STAKE.team_per_side)
    r.check_balance()


# --------------------------------------------------------------------------
# skat money
# --------------------------------------------------------------------------

def test_skat_value_is_the_pot_over_the_skat_count():
    """The house worked example: 15 players, $150, 10 skats -> $15 each."""
    entries = [entry(f"P{i}", 1 + i % 3, 20, 10, 10) for i in range(15)]
    entries[0].greens = 4
    entries[1].skins = 6
    r = score_round(entries)
    assert r.skat_pot == 150.0
    assert r.total_skats == 10
    assert r.skat_value == pytest.approx(15.0)
    assert r.payouts["P0"].skat_money == pytest.approx(60.0)
    assert r.payouts["P1"].skat_money == pytest.approx(90.0)


def test_twelve_skats_pay_twelve_fifty():
    entries = [entry(f"P{i}", 1 + i % 3, 20, 10, 10) for i in range(15)]
    entries[0].skins = 12
    assert score_round(entries).skat_value == pytest.approx(12.5)


def test_greens_and_skins_are_worth_the_same():
    entries = [entry(f"P{i}", 1 + i % 2, 20, 10, 10) for i in range(4)]
    entries[0].greens = 1
    entries[1].skins = 1
    r = score_round(entries)
    assert r.payouts["P0"].skat_money == pytest.approx(r.payouts["P1"].skat_money)


def test_a_round_with_no_skats_carries_the_pot_by_default():
    r = score_round(simple_round())
    assert r.total_skats == 0
    assert r.carried_money == pytest.approx(r.skat_pot)
    r.check_balance()


def test_a_round_with_no_skats_can_refund_instead():
    rules = HouseRules(no_skat_policy="refund")
    r = score_round(simple_round(), rules=rules)
    assert r.carried_money == 0
    for p in r.payouts.values():
        assert p.skat_money == pytest.approx(STAKE.skat_per_member)
    r.check_balance()


def test_carry_in_rolls_into_the_next_skat_pot():
    r = score_round(simple_round(greens=1), carry_in=40.0)
    assert r.skat_pot == pytest.approx(4 * STAKE.skat_per_member + 40.0)
    assert r.payouts["A1"].skat_money == pytest.approx(80.0)
    r.check_balance()


# --------------------------------------------------------------------------
# no-shows
# --------------------------------------------------------------------------

def test_a_no_show_is_out_of_the_pots_and_out_of_the_quota():
    entries = simple_round()
    entries.append(PlayerEntry(name="Ghost", team_no=1, quota=30, played=False))
    r = score_round(entries)
    assert r.n_players == 4
    assert "Ghost" not in r.payouts
    front = next(s for s in r.sides if s.side == "front" and s.team_no == 1)
    assert front.quota == 20.0
    assert any("not playing" in w.lower() for w in r.warnings)


# --------------------------------------------------------------------------
# the real round 60
# --------------------------------------------------------------------------

def test_round_60_reproduces_the_paper_scoresheet():
    entries = [
        entry("Bert Dargie", 1, 35, 17, 24, greens=1, skins=2, score=72),
        entry("Don Vick", 1, 27, 11, 14, greens=1, skins=1, score=84),
        entry("Tom Button", 1, 27, 13, 12, score=84),
        # a guest who played; nobody records a guest's points
        PlayerEntry(name="Casey Kennedy", team_no=1, quota=None),
        entry("Allen Rosario", 2, 19, 12, 11, skins=2, score=86),
        entry("B.H. Khoo", 2, 29, 13, 17, skins=1, score=79),
        entry("John Holmes", 2, 29, 15, 13, greens=1, score=80),
        entry("Takashi Yagi", 2, 28, 14, 16, greens=1, score=79),
        entry("Craig Brent", 3, 13, 7, 8, score=93),
        entry("Mike Stansbury", 3, 26, 9, 13, score=85),
        entry("Philip McCutchan", 3, 34, 18, 16, skins=1, score=75),
        entry("Wayne Whisman", 3, 25, 11, 11, score=86),
    ]
    r = score_round(entries)

    assert r.n_players == 12            # 11 members plus the guest
    assert r.n_team_players == 11
    assert r.total_collected == 230.0   # 11 x $20 + $10
    assert r.pot_per_side == 55.0       # $110 of team money, split front and back
    assert r.total_skats == 11
    assert r.skat_pot == 120.0          # $110 from members + the guest's $10
    assert r.skat_value == pytest.approx(120 / 11)

    # the three Team Points/side figures printed on the paper sheet
    xi = {(s.team_no): s.quota for s in r.sides if s.side == "front"}
    assert xi == {1: 44.5, 2: 52.5, 3: 49.0}

    assert r.winners("front")[0].team_no == 2       # 54 vs 52.5 = +1.5
    assert r.winners("back")[0].team_no == 1        # 50 vs 44.5 = +5.5
    assert r.payouts["Bert Dargie"].total == pytest.approx(18.33 + 3 * 120 / 11, abs=0.01)
    assert r.payouts["Casey Kennedy"].total == 0.0
    assert r.payouts["Casey Kennedy"].net == pytest.approx(-10.0)
    assert r.total_paid == pytest.approx(230.0)


# --------------------------------------------------------------------------
# invariants
# --------------------------------------------------------------------------

def test_every_dollar_in_comes_back_out():
    entries = [entry(f"P{i}", 1 + i % 4, 15 + i, 8 + i % 7, 9 + i % 5,
                     greens=i % 2, skins=i % 3) for i in range(19)]
    entries.append(guest("G", 2, skins=1))
    r = score_round(entries)
    r.check_balance()


def test_an_empty_round_is_refused_rather_than_settled():
    with pytest.raises(ValueError):
        score_round([PlayerEntry(name="Ghost", team_no=1, quota=20, played=False)])


def test_the_ante_has_to_add_up():
    with pytest.raises(ValueError):
        HouseRules(ante_per_player=25.0).validate()


def test_a_guest_ante_that_isnt_the_skat_share_is_refused():
    with pytest.raises(ValueError):
        HouseRules(guest_ante=5.0).validate()


def test_house_rules_as_shipped_are_consistent():
    RULES.validate()
    assert STAKE.member_ante == 20.0
    assert STAKE.guest_ante == STAKE.skat_per_member == 10.0
    assert RULES.guest_min_rounds == 3


# --------------------------------------------------------------------------
# guest promotion
# --------------------------------------------------------------------------

def test_a_guest_with_no_scored_rounds_shows_no_quota_so_far():
    q = compute_quota("newbie", [])
    assert q.is_guest
    assert q.provisional_quota is None
    assert "nothing to base a quota on" in q.note


def test_a_guest_partway_there_shows_progress_but_stays_a_guest():
    q = compute_quota("visitor", [26, 22])
    assert q.is_guest and q.quota is None      # two rounds is under the bar
    assert q.provisional_quota == 24           # display only, never applied
    assert q.provisional_rounds == 2
    assert "1 more" in q.note


def test_promotion_happens_on_the_fourth_round_with_no_intervention():
    """
    Rounds 1-3 as a guest, round 4 as a full member. Nothing to press: the only
    thing that changes anything is somebody writing the guest's points down.
    """
    history = []
    for expected_guest in (True, True, True, False):
        q = compute_quota("New Guy", list(reversed(history)),
                          rounds_available=len(history))
        assert q.is_guest is expected_guest, f"after {len(history)} scored round(s)"
        history.append(24)

    fourth = compute_quota("New Guy", [24, 24, 24], rounds_available=3)
    assert not fourth.is_guest and fourth.quota == 24


def test_an_unscored_guest_round_never_counts_toward_graduation():
    """Attendance isn't data - three blank rounds leave them a guest."""
    q = compute_quota("Ghost Guest", [], rounds_available=0)
    assert q.is_guest and q.provisional_quota is None


def test_a_guests_points_never_reach_their_team_even_once_recorded():
    """Recording them earns a quota; it must not leak into team scoring."""
    entries = simple_round(front_a=10, back_a=10, front_b=30, back_b=30)
    entries.append(guest("Ringer", 1, 40, 40))          # points now recorded
    r = score_round(entries)
    front_t1 = next(s for s in r.sides if s.side == "front" and s.team_no == 1)
    assert front_t1.points == 10
    assert r.payouts["Ringer"].team_money == 0.0
    assert r.payouts["Ringer"].ante == 10.0
    r.check_balance()
