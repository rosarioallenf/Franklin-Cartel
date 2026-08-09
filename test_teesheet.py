"""
Tee sheet parsing, against the real August 2026 format.

The club dropped the Team number column in August 2026. Teams are now
delimited by dashed rules (the grey banding is decorative and, crucially,
absent for white blocks). R61 is the fixture because it exercises the two
things that break naive parsers: it spans two pages, and its second page
carries a "North / Blue" heading rather than a bare course name.
"""
from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from cartel.teesheet import parse_tee_sheet, reconcile_names

warnings.filterwarnings("ignore")

R61 = Path(__file__).parent / "fixtures" / "R61_SatAugust1.pdf"


@pytest.fixture(scope="module")
def sheet():
    return parse_tee_sheet(str(R61))


def test_round_and_date(sheet):
    assert sheet.round_no == 61
    assert sheet.played_on.isoformat() == "2026-08-01"


def test_parses_without_errors(sheet):
    assert sheet.ok, sheet.errors


def test_four_teams_numbered_across_the_page_break(sheet):
    # 3 teams on page 1, 1 on page 2 - numbering must not restart
    assert [g.team_no for g in sheet.groups] == [1, 2, 3, 4]
    assert [g.page for g in sheet.groups] == [1, 1, 1, 2]


def test_team_membership(sheet):
    assert [len(g.players) for g in sheet.groups] == [5, 5, 4, 4]
    assert sheet.groups[0].players[0] == "Edwin Watkins"
    assert sheet.groups[1].players == [
        "Par Bolina", "Ward Dillard", "John Harlin", "Mike Alday", "Allen Rosario"]
    assert sheet.groups[3].players[-1] == "Craig Brent"
    assert len(sheet.players) == 18


def test_tee_times_are_labels_not_keys(sheet):
    # each block carries its own time; identical times must not merge blocks
    assert [g.tee_time for g in sheet.groups] == [
        "10:03 AM", "10:12 AM", "10:28 AM", "12:21 PM"]


def test_course_slash_tees_heading(sheet):
    # "North / Blue" -> course North, tees Blue
    assert sheet.courses == ["N"]
    assert sheet.groups[3].tees == "Blue"
    assert sheet.groups[0].tees is None
    assert sheet.course_defaulted is False


def test_row_pitch_is_consistent(sheet):
    assert sheet.row_pitch == pytest.approx(13.5, abs=0.3)


def test_dalgarns_are_never_merged():
    """Father and son. The surname fallback must refuse to pick one."""
    roster = {"Jay Dalgarn", "Jay Dalgarn1", "Mike Alday"}
    mapping, unmatched = reconcile_names(["Jay Dalgarn", "Jay Dalgarn1"], roster)
    assert mapping["Jay Dalgarn"] == "Jay Dalgarn"
    assert mapping["Jay Dalgarn1"] == "Jay Dalgarn1"
    assert unmatched == []


def test_ambiguous_surname_is_never_guessed():
    roster = {"Jay Dalgarn", "Jay Dalgarn1"}
    _, unmatched = reconcile_names(["J Dalgarn"], roster)
    assert unmatched == ["J Dalgarn"]


# --------------------------------------------------------------------------
# Quota basis report
# --------------------------------------------------------------------------

def test_quota_basis_matches_the_applied_quota(tmp_path):
    """
    The report must never disagree with the quota the app actually uses.
    Both read the same view with the same ordering and window.
    """
    import sqlite3
    from pathlib import Path
    from cartel import storage, stats

    db = Path(__file__).resolve().parents[1] / "data" / "cartel.db"
    if not db.exists():
        pytest.skip("no local database")

    with storage.connect(str(db)) as conn:
        names = [r["name"] for r in storage.all_members(conn)][:12]
        basis = stats.quota_basis(conn, names=names)
        applied = stats.current_quotas(conn, names=names)

    for n in basis["Player"].unique():
        block = basis[basis["Player"] == n]
        shown = block[block["Date"] == "Quota"]["Total"].iloc[0]
        assert str(shown) == applied[n].display, f"{n}: report {shown} vs applied {applied[n].display}"


def test_quota_basis_window_never_exceeds_the_rule():
    from pathlib import Path
    from cartel import storage, stats
    from cartel.config import RULES

    db = Path(__file__).resolve().parents[1] / "data" / "cartel.db"
    if not db.exists():
        pytest.skip("no local database")

    with storage.connect(str(db)) as conn:
        basis = stats.quota_basis(conn)

    for n, block in basis.groupby("Player"):
        rounds = block[~block["Date"].astype(str).str.startswith(("Average", "Quota"))]
        assert len(rounds) <= RULES.quota_window, f"{n} shows {len(rounds)} rounds"


# --------------------------------------------------------------------------
# reading the course out of the heading
# --------------------------------------------------------------------------

def test_the_course_is_matched_by_word_not_by_equality():
    """
    The exact-match version handled "North" and "South" and defaulted on
    "South Course" - then reported it could not read a course out of a string
    with the course plainly in it.
    """
    from cartel.teesheet import _course_code

    for text, expected in [
        ("North", "N"), ("South", "S"),
        ("North Course", "N"), ("South Course", "S"),
        ("SOUTH COURSE", "S"), ("The South Course", "S"),
        ("South / Blue", "S"), ("N", "N"), ("S", "S"),
    ]:
        assert _course_code(text) == expected, f"{text!r} read as {_course_code(text)!r}"


def test_a_heading_with_no_course_in_it_still_returns_nothing():
    """Better to default and say so than to guess from a tee colour."""
    from cartel.teesheet import _course_code
    assert _course_code("Blue") is None
    assert _course_code("") is None
