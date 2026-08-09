"""
Render the whole app headlessly and assert nothing blows up.

This exists because of a real escape: `Styler.background_gradient` quietly
requires matplotlib, which was installed in the development environment as a
side effect of something else and absent on a clean machine. Every unit test
passed; the app crashed the moment a real user opened the Standings tab.

Unit tests check the money. This checks that the screens actually draw. It runs
every tab in one pass, because Streamlit executes all tab bodies on every script
run rather than lazily.

Run:  python -m pytest tests -q
"""
from __future__ import annotations

from pathlib import Path

import pytest

from cartel import storage

pytest.importorskip("streamlit.testing.v1",
                    reason="needs a Streamlit new enough to have AppTest")

ROOT = Path(__file__).resolve().parent.parent
FIELD = [("Bert Dargie", 1, 35), ("Don Vick", 1, 27), ("Tom Button", 1, 27),
         ("B.H. Khoo", 2, 29), ("John Holmes", 2, 29), ("Takashi Yagi", 2, 28)]
POINTS = [(17, 18), (13, 14), (12, 13), (15, 15), (14, 16), (14, 14)]


@pytest.fixture()
def populated_db(tmp_path, monkeypatch):
    """A database with enough in it that every tab has something to draw."""
    path = str(tmp_path / "app.db")
    monkeypatch.setenv("CARTEL_DB", path)
    monkeypatch.delenv("CARTEL_DB_URL", raising=False)
    storage.init_db(path)

    with storage.connect(path) as conn:
        for name, _, _ in FIELD:
            storage.upsert_member(conn, name, "W")
            storage.set_seed(conn, name, 2026, 100.0, 120.0)
        # enough history that quotas exist rather than everyone being a guest
        for i, date in enumerate(["2026-05-01", "2026-05-08", "2026-05-15", "2026-05-22"]):
            rid = storage.create_round(conn, date, "N", round_no=i + 1, status="legacy")
            storage.save_entries(conn, rid, [
                {"name": n, "team_no": t, "quota": q, "played": 1,
                 "points_front": f, "points_back": b, "score": 85, "greens": 0, "skins": 0}
                for (n, t, q), (f, b) in zip(FIELD, POINTS)])
        # one draft, so the Enter-scores tab has a round to select
        rid = storage.create_round(conn, "2026-06-01", "N", round_no=5, status="draft")
        storage.save_entries(conn, rid, [{"name": n, "team_no": t, "quota": q}
                                         for n, t, q in FIELD])
    return path


def run_app(monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.chdir(ROOT)
    return AppTest.from_file(str(ROOT / "app.py"), default_timeout=180).run()


def test_the_app_renders_without_raising(populated_db, monkeypatch):
    at = run_app(monkeypatch)
    assert not at.exception, "; ".join(str(e.value) for e in at.exception)


EXPECTED_TABS = ["Today", "Enter scores", "Standings", "Player", "Next quota",
                 "Roster", "Health"]


def test_every_tab_is_present(populated_db, monkeypatch):
    """
    Checks labels, not a count. A count tells you the number changed; labels
    tell you which tab went missing, which is the thing you need to know.
    """
    at = run_app(monkeypatch)
    drew = [t.label for t in at.tabs]
    # The Today screen nests its own pair (tee sheet PDF / teams by hand), so
    # compare the main tabs as a subset in order rather than the raw list.
    main = [t for t in drew if t in EXPECTED_TABS]
    assert main == EXPECTED_TABS, f"expected {EXPECTED_TABS}, drew {drew}"


def test_a_round_can_be_set_up_without_a_readable_pdf(populated_db, monkeypatch):
    """
    The club's tee sheet has already changed format once and will again. A
    format change should cost two minutes of typing, not a lost round.
    """
    at = run_app(monkeypatch)
    drew = [t.label for t in at.tabs]
    assert "Enter teams by hand" in drew
    assert "From the tee sheet PDF" in drew


def test_the_tables_actually_draw(populated_db, monkeypatch):
    """A silent failure would show as zero tables rather than an exception."""
    at = run_app(monkeypatch)
    assert len(at.dataframe) >= 3, f"only {len(at.dataframe)} table(s) rendered"


def test_it_renders_on_an_empty_database(tmp_path, monkeypatch):
    """A fresh install before the import must explain itself, not crash."""
    path = str(tmp_path / "empty.db")
    monkeypatch.setenv("CARTEL_DB", path)
    monkeypatch.delenv("CARTEL_DB_URL", raising=False)
    storage.init_db(path)

    at = run_app(monkeypatch)
    assert not at.exception, "; ".join(str(e.value) for e in at.exception)
    assert any("Nothing loaded yet" in i.value for i in at.info), \
        "an empty database should say so rather than showing blank tabs"


def test_no_optional_plotting_dependency_is_required(populated_db, monkeypatch):
    """
    matplotlib must not creep back in. It is not in requirements.txt, so if the
    app starts needing it the app breaks on every clean install but not here.
    """
    # strip comments, or this trips on the note explaining why they're banned
    source = "\n".join(ln.split("#")[0] for ln in (ROOT / "app.py").read_text().splitlines())
    for banned in ("background_gradient", "matplotlib", ".bar(", "highlight_max"):
        assert banned not in source, (
            f"{banned!r} in app.py pulls in matplotlib, which is not a declared "
            f"dependency and will crash on a clean install"
        )
