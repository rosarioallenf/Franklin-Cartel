"""
The three things the app does, end to end.

  import_history   one-off: points history from Golf_Stats.xlsx, opening money
                   balances from YTD_Winnings.xlsx
  prepare_round    tee sheet PDF in, draft round + printable scoresheet out
  settle_round     scores in, money and reports out
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from cartel_config import RULES, db_path as _db_path
import cartel_backup as backup
import cartel_storage as storage
import cartel_scoring as scoring
import cartel_stats as stats
import cartel_reports as reports
import cartel_scoresheet as scoresheet
from cartel_teesheet import TeeGroup, TeeSheet, parse_tee_sheet, reconcile_names

# The workbook has been through a few hands, so accept the obvious aliases.
COL_ALIASES = {
    "player": "Name", "name": "Name",
    "team": "TeamNo", "teamno": "TeamNo", "team_no": "TeamNo",
    "date": "Date", "course": "Course",
    "points_front": "Points_Front", "pts_front": "Points_Front",
    "points_back": "Points_Back", "pts_back": "Points_Back",
    "score": "Score", "greens": "Greens", "green": "Greens",
    "skins": "Skins", "skats": "Skins",
}


def _normalise(df: pd.DataFrame) -> pd.DataFrame:
    df = df.loc[:, ~df.columns.astype(str).str.startswith("Unnamed")]
    df = df.rename(columns={c: COL_ALIASES.get(str(c).strip().lower(), c)
                            for c in df.columns})
    return df


# --------------------------------------------------------------------------
# one-off import
# --------------------------------------------------------------------------

def import_history(xlsx_path: str, ytd_path: str | None = None,
                   db_path: str | None = None, year: int | None = None) -> dict:
    """
    Load the legacy workbook as POINTS ONLY, then seed each member's opening
    money from the YTD Winnings sheet.

    No money is derived from the old Front_Winner / Back_Winner flags. Those
    flags are incomplete, and the group's own YTD figures are authoritative.
    """
    storage.init_db(db_path)
    hist = _normalise(pd.read_excel(xlsx_path, sheet_name="Running_Stats"))
    members = _normalise(pd.read_excel(xlsx_path, sheet_name="Membership"))
    report: dict = {"warnings": [], "rounds": 0, "entries": 0, "seeded": 0}

    required = {"Date", "Course", "TeamNo", "Name", "Points_Front", "Points_Back"}
    missing = required - set(hist.columns)
    if missing:
        raise ValueError(f"Running_Stats is missing column(s): {', '.join(sorted(missing))}")

    year = year or int(pd.Timestamp(hist["Date"].max()).year)

    with storage.connect(db_path) as conn:
        for _, m in members.iterrows():
            storage.upsert_member(conn, str(m["Name"]).strip(),
                                  str(m.get("Tee", "W")).strip() or "W")

        roster = storage.member_names(conn)
        extra = sorted(set(hist["Name"].astype(str).str.strip()) - roster)
        for n in extra:
            storage.upsert_member(conn, n, "W", active=False)
        if extra:
            report["warnings"].append(
                f"In the history but not on the Membership tab, added as inactive: "
                f"{', '.join(extra)}")
        missing_members = sorted(roster - set(hist["Name"].astype(str).str.strip()))
        if missing_members:
            report["warnings"].append(
                f"On the Membership tab but with no rounds on file: "
                f"{', '.join(missing_members)}")

        # a blank Course would make groupby drop the row and silently lose a round
        blank = hist["Course"].isna()
        if blank.any():
            report["warnings"].append(
                f"{int(blank.sum())} row(s) had no Course and would have been dropped: "
                + "; ".join(f"{pd.Timestamp(r['Date']).date()} {r['Name']}"
                            for _, r in hist[blank].iterrows()))
            hist.loc[blank, "Course"] = "?"

        hist = hist.sort_values("Date")
        odd_courses: dict[str, list] = {}
        for (played_on, course), g in hist.groupby(["Date", "Course"], sort=True):
            iso = pd.Timestamp(played_on).date().isoformat()
            code = str(course).strip().upper()[:1]
            if code not in ("N", "S"):
                odd_courses.setdefault(str(course), []).append(iso)
                code = "?"

            rid = storage.create_round(conn, iso, code, status="legacy")
            rows = []
            for _, r in g.iterrows():
                rows.append({
                    "name": str(r["Name"]).strip(),
                    "team_no": int(r["TeamNo"]),
                    "quota": None,       # not stored for legacy rounds, by design
                    "is_guest": 0,
                    "played": 1,
                    "points_front": int(r["Points_Front"]),
                    "points_back": int(r["Points_Back"]),
                    "score": int(r["Score"]) if pd.notna(r.get("Score")) else None,
                    "greens": int(r.get("Greens") or 0),
                    "skins": int(r.get("Skins") or 0),
                })
            storage.save_entries(conn, rid, rows)
            report["rounds"] += 1
            report["entries"] += len(rows)

        for bad, dates in odd_courses.items():
            # The signature we keep seeing: the value in Course is the number of
            # players that day, i.e. the field count was typed one column across.
            looks_like_count = []
            for iso in dates:
                n = conn.execute(
                    """SELECT COUNT(*) c FROM entries e JOIN rounds r
                       ON r.round_id = e.round_id
                       WHERE r.played_on = ? AND r.course = '?'""", (iso,)).fetchone()["c"]
                if str(bad).strip() == str(n):
                    looks_like_count.append(iso)

            msg = (f"Course recorded as {bad!r} on {len(dates)} round(s) "
                   f"({', '.join(dates)}) - filed as unknown, so those rounds are "
                   f"missing from any per-course figures.")
            if looks_like_count:
                msg += (f" On {', '.join(looks_like_count)} the value equals that day's "
                        f"player count, so it looks like the field count landed in the "
                        f"Course column.")
            msg += (" Fix with:  python scripts/cartel_cli.py fix-course "
                    f"{dates[0]} N --apply")
            report["warnings"].append(msg)

        orphans = conn.execute(
            """SELECT r.played_on, COUNT(e.name) n FROM rounds r
               JOIN entries e ON e.round_id = r.round_id
               WHERE r.course = '?' GROUP BY r.round_id HAVING COUNT(e.name) < 3""").fetchall()
        for o in orphans:
            report["warnings"].append(
                f"{o['played_on']}: {o['n']} player(s) sit in a round of their own "
                f"because their Course cell was blank. Their team is short a player, "
                f"so that round's team money is wrong until it's merged back. Fix with:"
                f"  python scripts/cartel_cli.py fix-course {o['played_on']} N --apply")

        if ytd_path:
            report.update(_seed_ledger(conn, ytd_path, year, report))

    # A clean point to fall back to before anyone has entered a thing.
    report["backup"] = backup.make_backup(reason="after-import")
    return report


def _seed_ledger(conn, ytd_path: str, year: int, report: dict) -> dict:
    """
    Read the YTD Winnings sheet.

    The supplied file labels its columns Team$ / Green$ / Skin$, but the third
    is the sum of the first two on every row, so it's really Team / Skat / Total.
    Detect that rather than trust the headers.
    """
    ytd = pd.read_excel(ytd_path)
    ytd.columns = [str(c).strip() for c in ytd.columns]
    name_col = next((c for c in ytd.columns if c.lower() in ("name", "player")), ytd.columns[0])
    nums = [c for c in ytd.columns if c != name_col
            and pd.api.types.is_numeric_dtype(ytd[c])]

    if len(nums) < 2:
        raise ValueError(f"Need at least two money columns in {ytd_path}, found {nums}")

    team_col, skat_col = nums[0], nums[1]
    if len(nums) >= 3:
        third = nums[2]
        is_total = ((ytd[team_col] + ytd[skat_col] - ytd[third]).abs() < 0.005).all()
        if is_total:
            report["warnings"].append(
                f"{Path(ytd_path).name}: column {third!r} equals {team_col!r} + "
                f"{skat_col!r} on every row, so it is the total won, not skin money. "
                f"Read as Team / Skat / Total. Worth relabelling the sheet: 'Green$' "
                f"is really greens AND skins combined.")
        else:
            report["warnings"].append(
                f"{Path(ytd_path).name}: found three money columns and the third is "
                f"not the sum of the first two. Seeded from {team_col!r} and "
                f"{skat_col!r} - check that's right.")

    seeded = 0
    unknown = []
    roster = storage.member_names(conn)
    for _, r in ytd.iterrows():
        name = str(r[name_col]).strip()
        if not name or name.lower() == "nan":
            continue
        if name not in roster:
            unknown.append(name)
            continue
        storage.set_seed(conn, name, year, float(r[team_col] or 0), float(r[skat_col] or 0),
                         source=f"{Path(ytd_path).name} ({team_col} / {skat_col})")
        seeded += 1

    if unknown:
        report["warnings"].append(
            f"In {Path(ytd_path).name} but not on the roster, so not seeded: "
            f"{', '.join(unknown)}")

    totals = conn.execute(
        "SELECT SUM(team_money) t, SUM(skat_money) s FROM ledger_seed WHERE year=?",
        (year,)).fetchone()
    report["seed_totals"] = {"team": totals["t"] or 0.0, "skat": totals["s"] or 0.0}
    return {"seeded": seeded}


# --------------------------------------------------------------------------
# before the round
# --------------------------------------------------------------------------

@dataclass
class PreparedRound:
    round_id: int
    round_no: int | None
    played_on: date
    course: str
    teams: list[dict]
    quotas: dict[str, object]          # name -> QuotaResult
    unknown_players: list[str]
    warnings: list[str] = field(default_factory=list)
    scoresheet_path: str | None = None

    @property
    def guests(self) -> list[str]:
        return [n for n, q in self.quotas.items() if q.is_guest]


def prepare_round(tee_sheet_pdf: str | None = None, db_path: str | None = None,
                  out_dir: str = ".", add_unknown_as_guest: bool = True,
                  manual: TeeSheet | None = None,
                  course: str | None = None) -> PreparedRound:
    """
    Set a round up, either from the club's tee sheet PDF or from teams typed in
    by hand.

    The hand-entry route exists because the club's sheet has already changed
    format once, and will again. A format change should cost an extra two
    minutes of typing, not stop the round. Everything downstream - quotas, the
    scoresheet, settlement - is identical either way, because both paths build
    the same TeeSheet object.
    """
    if manual is not None and tee_sheet_pdf is not None:
        raise ValueError("Give a PDF or hand-entered teams, not both.")
    if manual is None and tee_sheet_pdf is None:
        raise ValueError("Nothing to prepare from - pass a PDF or hand-entered teams.")

    sheet = manual if manual is not None else parse_tee_sheet(tee_sheet_pdf)
    warnings = list(sheet.warnings)

    if sheet.errors:
        raise ValueError(
            "The tee sheet didn't parse cleanly, so nothing was imported:\n  - "
            + "\n  - ".join(sheet.errors))

    if sheet.played_on is None:
        raise ValueError("Couldn't read the date off the tee sheet - enter it by hand.")

    with storage.connect(db_path) as conn:
        roster = storage.member_names(conn)
        mapping, unknown = reconcile_names(sheet.players, roster)

        if unknown:
            if add_unknown_as_guest:
                for u in unknown:
                    storage.upsert_member(conn, u, "W")
                    mapping[u] = u
                warnings.append(
                    f"Not on the roster, added with no history so they play as guests: "
                    f"{', '.join(unknown)}")
                unknown = []
            else:
                warnings.append(
                    f"On the tee sheet but not on the roster: {', '.join(unknown)}. "
                    f"Add them on the Roster tab, or drop them.")

        # An explicit course beats whatever was read off the PDF: the reader can
        # be defeated by a heading it doesn't recognise, and the person holding
        # the sheet cannot.
        if course is None:
            course = sheet.courses[0] if sheet.courses else "N"
        else:
            for g in sheet.groups:
                g.course = course
        if sheet.course_defaulted:
            warnings.append(
                "The tee sheet didn't state the course, so this round was filed as "
                "North. If they played South, fix it on the Rounds tab before settling.")
        iso = sheet.played_on.isoformat()
        # A round is keyed on date AND course, so preparing the same day again
        # under a different course makes a SECOND round rather than correcting
        # the first. That is how 8 August ended up with an abandoned North
        # round and a South one holding all the scores.
        same_day = conn.execute(
            "SELECT round_id, course, status FROM rounds WHERE played_on = ? "
            "AND course != ?", (iso, course)).fetchall()
        for other in same_day:
            n_scored = conn.execute(
                """SELECT COUNT(*) c FROM entries WHERE round_id = ?
                   AND points_front IS NOT NULL""", (other["round_id"],)).fetchone()["c"]
            if other["status"] == "posted" or n_scored:
                warnings.append(
                    f"There is already a {other['course']} round on {iso} with "
                    f"{n_scored} score(s) on it, status {other['status']}. You now "
                    f"have two rounds for this date - make sure you enter scores "
                    f"against the right one, and cancel the other."
                )
            else:
                conn.execute("DELETE FROM rounds WHERE round_id = ?", (other["round_id"],))
                warnings.append(
                    f"Removed an earlier empty {other['course']} round for {iso} - "
                    f"it was the same day prepared under a different course, and "
                    f"had no scores on it."
                )

        # Re-importing a tee sheet over a round that already has scores used to
        # wipe them: save_entries clears the round and writes fresh, empty rows,
        # while the round stayed marked 'posted' with its old payouts intact.
        # The result looked settled, still paid out, but no longer fed anyone's
        # quota - silent, and only visible weeks later as a wrong average.
        # So carry any existing scores across, keyed on player name.
        existing = conn.execute(
            "SELECT * FROM rounds WHERE played_on = ? AND course = ?", (iso, course)
        ).fetchone()
        prior: dict[str, dict] = {}
        if existing is not None:
            for e in storage.load_entries(conn, existing["round_id"]):
                if e["points_front"] is not None or e["greens"] or e["skins"]:
                    prior[e["name"]] = dict(e)
            if prior:
                warnings.append(
                    f"This round already had scores for {len(prior)} player(s). They "
                    f"have been kept. Anyone dropped from the new tee sheet has lost "
                    f"their scores; anyone added starts blank."
                )
            if existing["status"] == "posted":
                warnings.append(
                    "This round was already POSTED. Re-importing the tee sheet leaves "
                    "the old money in place until you post it again - so go to Enter "
                    "scores, check the grid, and post it once more."
                )

        rid = storage.create_round(conn, iso, course, round_no=sheet.round_no,
                                   carry_in=storage.pending_carry(conn), status="draft")
        # Captured now, not at settlement: the stake is announced well in
        # advance, so the rate that applies is the one in force when the
        # scoresheet is printed.
        stake = storage.current_stake(conn)
        conn.execute(
            "UPDATE rounds SET member_ante = ?, guest_ante = ? WHERE round_id = ?",
            (stake.member_ante, stake.guest_ante, rid))

        names = [mapping[p] for p in sheet.players if p in mapping]
        qres = stats.current_quotas(conn, before=iso, names=names)

        teams, rows = [], []
        for g in sheet.groups:
            resolved = [mapping[p] for p in g.players if p in mapping]
            if not resolved:
                continue
            teams.append({"team_no": g.team_no, "tee_time": g.tee_time, "players": resolved})
            for p in resolved:
                rows.append({"name": p, "team_no": g.team_no, "tee_time": g.tee_time,
                             "quota": qres[p].quota,
                             "is_guest": int(qres[p].is_guest),
                             "played": 1,
                             # keep any scores this player already had
                             **({k: prior[p][k] for k in
                                 ("points_front", "points_back", "score",
                                  "greens", "skins", "played")}
                                if p in prior else {})})
        storage.save_entries(conn, rid, rows)

    prepared = PreparedRound(
        round_id=rid, round_no=sheet.round_no, played_on=sheet.played_on, course=course,
        teams=teams, quotas=qres, unknown_players=unknown, warnings=warnings,
    )

    Path(out_dir).mkdir(parents=True, exist_ok=True)
    path = str(Path(out_dir) / f"Scoresheet_R{sheet.round_no or 'x'}_{iso}.pdf")
    scoresheet.build_scoresheet(
        path, round_no=sheet.round_no, played_on=sheet.played_on, course=course,
        teams=teams, quotas=qres,
    )
    prepared.scoresheet_path = path
    return prepared


def manual_tee_sheet(played_on, course: str, teams: list[list[str]],
                     round_no: int | None = None,
                     tee_times: list[str] | None = None) -> TeeSheet:
    """
    Build a tee sheet from teams typed in by hand.

    teams is a list of lists of player names, in team order. Blank names are
    dropped, so the caller can hand over a fixed-size grid without tidying it.
    """
    tee_times = tee_times or []
    groups = []
    for i, members in enumerate(teams, start=1):
        players = [n.strip() for n in members if n and n.strip()]
        if not players:
            continue
        groups.append(TeeGroup(
            team_no=len(groups) + 1,
            tee_time=(tee_times[i - 1] if i - 1 < len(tee_times) else ""),
            course=course, players=players))

    sheet = TeeSheet(round_no=round_no, played_on=played_on, groups=groups)
    if not groups:
        sheet.warnings.append("No teams were entered.")
    for g in groups:
        if not (RULES.min_team_size <= len(g.players) <= RULES.max_team_size):
            sheet.warnings.append(
                f"Team {g.team_no} has {len(g.players)} player(s), outside the usual "
                f"{RULES.min_team_size}-{RULES.max_team_size}.")
    return sheet


# --------------------------------------------------------------------------
# after the round
# --------------------------------------------------------------------------

def settle_round(round_id: int, results: list[dict], db_path: str | None = None,
                 out_dir: str = ".", post: bool = True) -> dict:
    """
    results: [{"name", "points_front", "points_back", "score", "greens", "skins",
               "played"}, ...]

    "played" is explicit and defaults to True. Leave a guest's points blank -
    nobody records them - and untick "played" only for a genuine no-show.
    """
    with storage.connect(db_path) as conn:
        rnd = storage.get_round(conn, round_id)
        if rnd is None:
            raise ValueError(f"No round {round_id}")
        if rnd["status"] == "legacy":
            raise ValueError(
                "That's an imported legacy round. Its money lives in the seeded "
                "ledger and settling it here would double count.")

        existing = {r["name"]: r for r in storage.load_entries(conn, round_id)}
        by_name = {r["name"]: r for r in results}
        # Somebody who turned up but wasn't on the sheet gets added here rather
        # than sending the organiser back to rebuild the round. Their quota is
        # worked out as at this round's date, exactly as it would have been.
        unknown = sorted(set(by_name) - set(existing))
        if unknown:
            roster = storage.member_names(conn)
            missing = [n for n in unknown if n not in roster]
            if missing:
                raise ValueError(
                    f"Not on the roster at all: {', '.join(missing)}. Add them on "
                    f"the Roster tab first.")
            qres = stats.current_quotas(conn, before=rnd["played_on"], names=unknown)
            for name in unknown:
                storage.add_entry(conn, round_id, name,
                                  team_no=int(by_name[name].get("team_no") or 1),
                                  quota=qres[name].quota,
                                  is_guest=qres[name].is_guest)
            existing = {r["name"]: r for r in storage.load_entries(conn, round_id)}

        rows, entries = [], []
        for name, e in existing.items():
            r = by_name.get(name, {})
            pf, pb = r.get("points_front"), r.get("points_back")
            is_guest = bool(e["is_guest"]) or e["quota"] is None
            # Played is explicit and defaults to on. A guest's points are never
            # written down, so a blank guest row still means they played.
            played = bool(r.get("played", True))
            team_no = int(r.get("team_no") or e["team_no"])
            rows.append({
                "name": name, "team_no": team_no, "tee_time": e["tee_time"],
                "quota": e["quota"], "is_guest": int(is_guest), "played": int(played),
                "points_front": pf, "points_back": pb, "score": r.get("score"),
                "greens": int(r.get("greens") or 0), "skins": int(r.get("skins") or 0),
            })
            entries.append(scoring.PlayerEntry(
                name=name, team_no=team_no, quota=e["quota"], is_guest=is_guest,
                played=played,
                points_front=pf, points_back=pb, score=r.get("score"),
                greens=int(r.get("greens") or 0), skins=int(r.get("skins") or 0),
            ))
        storage.save_entries(conn, round_id, rows)

        # Resolve the carry NOW, against this round's own date, rather than
        # trusting whatever was pending when the scoresheet was printed. Sheets
        # get printed ahead of time and rounds get settled out of order.
        carry = storage.pending_carry(conn, before=rnd["played_on"],
                                      exclude_round_id=round_id)
        conn.execute("UPDATE rounds SET carry_in = ? WHERE round_id = ?",
                     (carry, round_id))
        result = scoring.score_round(entries, carry_in=carry,
                                     stake=storage.round_stake(conn, round_id))
        if post:
            storage.post_round(conn, round_id, result)

        played_on = datetime.fromisoformat(rnd["played_on"]).date()
        out = {"result": result, "round_pdf": None}

        # Only a POSTED round produces paperwork. A preview used to write the
        # same Results PDF, so working out the money and actually posting it
        # were indistinguishable - which is how a round sat in draft for a day
        # with every figure looking correct.
        if post:
            Path(out_dir).mkdir(parents=True, exist_ok=True)
            tag = f"R{rnd['round_no'] or round_id}_{rnd['played_on']}"
            round_pdf = str(Path(out_dir) / f"Results_{tag}.pdf")
            reports.round_report(round_pdf, round_no=rnd["round_no"],
                                 played_on=played_on, course=rnd["course"],
                                 result=result, entries={e.name: e for e in entries})
            out["round_pdf"] = round_pdf
        if post:
            year = played_on.year
            ytd_pdf = str(Path(out_dir) / f"Cartel_Member_Stats_{year}_{rnd['played_on']}.pdf")
            reports.ytd_report(ytd_pdf, conn, year, as_of=played_on)
            xlsx = str(Path(out_dir) / f"Golf_Stats_{rnd['played_on']}.xlsx")
            reports.export_workbook(xlsx, conn, year)
            out.update(ytd_pdf=ytd_pdf, workbook=xlsx,
                       reconciliation=stats.house_reconciliation(conn, year))

    # Outside the connection block: the snapshot must be taken after this
    # round's writes have committed, or it captures the state before them.
    if post:
        out["backup"] = backup.make_backup(
            reason=f"R{rnd['round_no'] or round_id}-posted", when=rnd["played_on"])
    return out


def rebuild_reports(round_id: int, db_path: str | None = None,
                    out_dir: str = ".") -> dict:
    """
    Regenerate a settled round's paperwork WITHOUT touching the database.

    Needed because the day's report used to appear only in the moment after you
    pressed Post, and the Post button now locks itself once a round is settled -
    so there was no way back to the report for a round finished last week.

    Reads stored entries, recomputes the same result the settlement produced,
    and writes the files. Nothing is saved, so this is safe to run any time.
    """
    with storage.connect(db_path) as conn:
        rnd = storage.get_round(conn, round_id)
        if rnd is None:
            raise ValueError(f"No round {round_id}")
        if rnd["status"] != "posted":
            raise ValueError(
                "That round isn't posted yet, so there is no settled result to "
                "report. Enter the scores and post it first.")

        entries = [
            scoring.PlayerEntry(
                name=e["name"], team_no=e["team_no"], quota=e["quota"],
                is_guest=bool(e["is_guest"]), played=bool(e["played"]),
                points_front=e["points_front"], points_back=e["points_back"],
                score=e["score"], greens=e["greens"], skins=e["skins"])
            for e in storage.load_entries(conn, round_id)
        ]
        result = scoring.score_round(entries, carry_in=rnd["carry_in"] or 0.0,
                                     stake=storage.round_stake(conn, round_id))

        played_on = datetime.fromisoformat(rnd["played_on"]).date()
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        tag = f"R{rnd['round_no'] or round_id}_{rnd['played_on']}"

        paths = {}
        paths["round_pdf"] = str(Path(out_dir) / f"Results_{tag}.pdf")
        reports.round_report(paths["round_pdf"], round_no=rnd["round_no"],
                             played_on=played_on, course=rnd["course"],
                             result=result, entries={e.name: e for e in entries})

        year = played_on.year
        paths["ytd_pdf"] = str(Path(out_dir) /
                               f"Cartel_Member_Stats_{year}_{rnd['played_on']}.pdf")
        reports.ytd_report(paths["ytd_pdf"], conn, year, as_of=played_on)

        paths["workbook"] = str(Path(out_dir) / f"Golf_Stats_{rnd['played_on']}.xlsx")
        reports.export_workbook(paths["workbook"], conn, year)

        paths["result"] = result
        return paths
