#!/usr/bin/env python3
"""
Command line entry points, for the one-off jobs and for anyone who'd rather
not click.

  # load the legacy workbook and seed the opening money balances (run once)
  python scripts/cartel_cli.py import Golf_Stats.xlsx --ytd YTD_Winnings.xlsx

  # prep a round from the tee sheet and print the scoresheet
  python scripts/cartel_cli.py prepare SunJuly26-TeeSheet.pdf --out out/

  # settle it from a CSV of results
  python scripts/cartel_cli.py settle 213 results.csv --out out/

  # regenerate the year-to-date report without touching anything
  python scripts/cartel_cli.py report 2026 --out out/

  # a round was rained off after the scoresheet was printed
  python scripts/cartel_cli.py cancel 2026-09-06 --apply

  # repair a round whose Course column holds something that isn't N or S
  python scripts/cartel_cli.py fix-course 2026-02-28 S --apply

  # copy everything from the local file into a hosted database, when you're
  # ready to share it with the group
  python scripts/cartel_cli.py migrate --to "postgresql://..." --apply

The settle CSV wants a header row of:
  name,points_front,points_back,score,greens,skins[,played]
Leave a guest's points blank - nobody records them. Set played to n/no/0 only
for a genuine no-show.
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cartel import db, pipeline, reports, storage, stats  # noqa: E402
from cartel.config import db_path as _db_path  # noqa: E402


def require_db(args) -> bool:
    """
    Every command except `import` needs a database that already has history in
    it. Say so in English rather than letting SQLite raise 'no such table'.
    """
    try:
        with storage.connect(args.db) as conn:
            n = conn.execute("SELECT COUNT(*) c FROM rounds").fetchone()["c"]
    except Exception:
        n = None

    if n is None:
        where = ("the database at " + str(args.db)) if not db.is_postgres() else "the database"
        print(f"No data yet - {where} hasn't been set up.\n\n"
              f"Run this first:\n"
              f"  python scripts/cartel_cli.py import Golf_Stats.xlsx --ytd YTD_Winnings.xlsx")
        return False
    if n == 0:
        print("The database is set up but has no rounds in it. Run the import first:\n"
              "  python scripts/cartel_cli.py import Golf_Stats.xlsx --ytd YTD_Winnings.xlsx")
        return False
    return True


def cmd_import(args) -> int:
    rep = pipeline.import_history(args.xlsx, ytd_path=args.ytd, db_path=args.db)
    print(f"Imported {rep['rounds']} rounds, {rep['entries']} player-rounds "
          f"(points only - no money is derived from the old winner flags).")
    if rep.get("seeded"):
        t = rep.get("seed_totals", {})
        print(f"Seeded opening balances for {rep['seeded']} member(s): "
              f"${t.get('team', 0):,.2f} team + ${t.get('skat', 0):,.2f} skats.")
    for w in rep["warnings"]:
        print(f"  ! {w}")
    return 0


def cmd_prepare(args) -> int:
    if not require_db(args):
        return 1
    p = pipeline.prepare_round(args.pdf, db_path=args.db, out_dir=args.out,
                               add_unknown_as_guest=args.add_guests)
    print(f"Round {p.round_no} — {p.played_on} — course {p.course} (id {p.round_id})")
    for t in p.teams:
        xi = sum(p.quotas[n].quota for n in t["players"]
                 if not p.quotas[n].is_guest) / 2
        names = ", ".join(f"{n} ({p.quotas[n].display})" for n in t["players"])
        print(f"  Team {t['team_no']} {t['tee_time']}  Xi={xi:.1f}  {names}")
    if p.guests:
        print(f"  ! Guest(s), ${__import__('cartel.config', fromlist=['RULES']).RULES.guest_ante:.0f} "
              f"in for greens and skins only: {', '.join(p.guests)}")
    for w in p.warnings:
        print(f"  ! {w}")
    if p.unknown_players:
        print(f"  ! Unresolved names, nothing generated for them: "
              f"{', '.join(p.unknown_players)}")
    print(f"\nScoresheet: {p.scoresheet_path}")
    return 0


def cmd_settle(args) -> int:
    if not require_db(args):
        return 1
    rows = []
    with open(args.csv, newline="") as fh:
        for r in csv.DictReader(fh):
            def num(key):
                v = (r.get(key) or "").strip()
                return int(v) if v else None
            played_raw = (r.get("played") or "").strip().lower()
            rows.append({
                "name": r["name"].strip(),
                "played": played_raw not in ("n", "no", "0", "false"),
                "points_front": num("points_front"),
                "points_back": num("points_back"),
                "score": num("score"),
                "greens": num("greens") or 0,
                "skins": num("skins") or 0,
            })

    out = pipeline.settle_round(args.round_id, rows, db_path=args.db,
                               out_dir=args.out, post=not args.dry_run)
    res = out["result"]
    field = f"{res.n_players} players"
    if res.n_guests:
        field += f" ({res.n_team_players} on teams, {res.n_guests} guest)"
    print(f"{field} — ${res.total_collected:.2f} in — "
          f"${res.pot_per_side:.2f} a side — "
          f"{res.total_skats} skats at ${res.skat_value:.2f}")
    for side in ("front", "back"):
        for s in sorted([x for x in res.sides if x.side == side], key=lambda x: -x.net):
            mark = f"WIN ${s.payout_per_player:.2f} ea" if s.is_winner else ""
            print(f"  {side:5} T{s.team_no}  {s.points:3} pts  Xi {s.quota:6.1f}  "
                  f"{s.net:+6.1f}  {mark}")
    print()
    for p in sorted(res.payouts.values(), key=lambda x: -x.total):
        tag = " (guest)" if p.is_guest else ""
        print(f"  {p.name + tag:28} in ${p.ante:5.2f}  won ${p.total:7.2f}  "
              f"({p.net:+.2f})")
    for w in res.warnings:
        print(f"  ! {w}")
    print(f"\nResults: {out['round_pdf']}")
    if not args.dry_run:
        print(f"Stats:   {out['ytd_pdf']}")
        print(f"Excel:   {out['workbook']}")
        print(f"Books:   {out['reconciliation']}")
    else:
        print("(dry run — nothing was posted)")
    return 0


def cmd_fix_course(args) -> int:
    """
    Repair a round whose Course is not N or S.

    Two shapes of damage, handled differently:

      relabel  the whole round carries the wrong code (someone typed the player
               count into the Course cell). Just correct it.

      merge    part of the round is fine and a stray row or two got orphaned
               into a round of their own by a blank Course. Those players must
               be moved back onto the real round, not given a round of their
               own - otherwise their team is short and the side money is wrong.
    """
    if not require_db(args):
        return 1
    course = args.course.strip().upper()[:1]
    if course not in ("N", "S"):
        print(f"Course must be N or S (got {args.course!r}).")
        return 1

    with storage.connect(args.db) as conn:
        rounds = conn.execute(
            "SELECT * FROM rounds WHERE played_on = ? ORDER BY round_id", (args.date,)
        ).fetchall()
        if not rounds:
            print(f"No rounds on {args.date}.")
            return 1

        broken = [r for r in rounds if r["course"] not in ("N", "S")]
        target = next((r for r in rounds if r["course"] == course), None)

        if not broken:
            print(f"{args.date}: nothing to fix, course already "
                  f"{', '.join(sorted({r['course'] for r in rounds}))}.")
            return 0

        for r in broken:
            n = conn.execute("SELECT COUNT(*) c FROM entries WHERE round_id = ?",
                             (r["round_id"],)).fetchone()["c"]
            names = [x["name"] for x in conn.execute(
                "SELECT name FROM entries WHERE round_id = ? ORDER BY name",
                (r["round_id"],))]

            if target is None:
                print(f"{args.date}: relabel course {r['course']!r} -> {course} "
                      f"({n} player(s))")
                if args.apply:
                    conn.execute("UPDATE rounds SET course = ? WHERE round_id = ?",
                                 (course, r["round_id"]))
                    target = conn.execute("SELECT * FROM rounds WHERE round_id = ?",
                                          (r["round_id"],)).fetchone()
            else:
                print(f"{args.date}: merge {n} orphaned player(s) into the existing "
                      f"{course} round -> {', '.join(names)}")
                clash = conn.execute(
                    """SELECT e.name FROM entries e WHERE e.round_id = ?
                       AND e.name IN (SELECT name FROM entries WHERE round_id = ?)""",
                    (r["round_id"], target["round_id"])).fetchall()
                if clash:
                    print(f"   SKIPPED: {', '.join(c['name'] for c in clash)} already "
                          f"on the {course} round - resolve by hand.")
                    continue
                if args.apply:
                    conn.execute("UPDATE entries SET round_id = ? WHERE round_id = ?",
                                 (target["round_id"], r["round_id"]))
                    conn.execute("DELETE FROM rounds WHERE round_id = ?", (r["round_id"],))

        if args.apply:
            left = conn.execute(
                """SELECT r.course, COUNT(e.name) n FROM rounds r
                   LEFT JOIN entries e ON e.round_id = r.round_id
                   WHERE r.played_on = ? GROUP BY r.round_id""", (args.date,)).fetchall()
            print("   now: " + ", ".join(f"{x['course']} with {x['n']} player(s)"
                                         for x in left))
        else:
            print("   (dry run - add --apply to make the change)")
    return 0


# Order matters: parents before children, because of the foreign keys.
# Parents before children, because of the foreign keys. Anything added to the
# schema must be added here too, or a deployment silently leaves it behind -
# which is how the stake history nearly got lost.
MIGRATE_TABLES = ["members", "ledger_seed", "stakes", "writeoffs",
                  "app_settings", "activity", "rounds", "entries",
                  "side_outcomes", "payouts"]


def cmd_migrate(args) -> int:
    """
    Copy the whole database somewhere else - normally the local file up to a
    hosted Postgres when the pilot is over.

    Nothing is read from the source but rows, and nothing is computed on the way
    through, so what lands is exactly what you had. The source is left untouched.
    """
    import os
    if not require_db(args):
        return 1

    source_desc = "Postgres" if db.is_postgres() else f"the file at {args.db}"
    print(f"Copying from {source_desc}")
    print(f"          to {args.to.split('@')[-1] if '@' in args.to else args.to}\n")

    with storage.connect(args.db) as src:
        data = {t: [dict(r) for r in src.execute(f"SELECT * FROM {t}")]
                for t in MIGRATE_TABLES}
    for t in MIGRATE_TABLES:
        print(f"   {t:<15} {len(data[t]):>6} row(s)")

    if not args.apply:
        print("\n(dry run - add --apply to actually copy)")
        return 0

    # point the storage layer at the destination for the duration
    previous = os.environ.get("CARTEL_DB_URL")
    os.environ["CARTEL_DB_URL"] = args.to
    try:
        try:
            storage.init_db()
        except Exception as exc:
            print(f"\nCouldn't connect to the destination.\n\n{exc}\n")
            print("Things worth checking:")
            print("  - the whole connection string is in quotes")
            print("  - you replaced the word PASSWORD with your actual password")
            print("  - the Supabase project isn't paused (check the dashboard)")
            print("  - you copied the Transaction pooler string, not Direct connection")
            return 1
        with storage.connect() as dst:
            existing = dst.execute("SELECT COUNT(*) c FROM rounds").fetchone()["c"]
            if existing and not args.force:
                print(f"\nThe destination already has {existing} round(s). Refusing to "
                      f"copy on top of it.\nAdd --force if you really mean to replace it.")
                return 1
            # Always clear, even on a fresh destination: init_db seeds an opening
            # stake, and that row collides with the one being copied across.
            for t in reversed(MIGRATE_TABLES):
                dst.execute(f"DELETE FROM {t}")

            for t in MIGRATE_TABLES:
                rows = data[t]
                if not rows:
                    continue
                cols = list(rows[0].keys())
                placeholders = ", ".join(f":{c}" for c in cols)
                dst.executemany(
                    f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({placeholders})", rows)

            # Postgres keeps its own counter for each identity column; move each
            # one past what we copied, or the next insert reuses an id.
            if db.is_postgres():
                for table, column in (("rounds", "round_id"), ("stakes", "stake_id")):
                    dst.execute(
                        f"SELECT setval(pg_get_serial_sequence('{table}','{column}'), "
                        f"COALESCE((SELECT MAX({column}) FROM {table}), 1))")

            check = {t: dst.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
                     for t in MIGRATE_TABLES}
    finally:
        if previous is None:
            os.environ.pop("CARTEL_DB_URL", None)
        else:
            os.environ["CARTEL_DB_URL"] = previous

    print("\nVerifying what arrived:")
    ok = True
    for t in MIGRATE_TABLES:
        match = check[t] == len(data[t])
        ok &= match
        print(f"   {t:<15} {check[t]:>6} of {len(data[t]):<6} {'ok' if match else 'MISMATCH'}")

    if ok:
        print("\nDone. Your local file is untouched - keep it as a backup.\n"
              "From now on, set CARTEL_DB_URL before running anything and the app "
              "will use the hosted database.")
    return 0 if ok else 1


def cmd_cancel(args) -> int:
    """
    Remove a round that never happened - rained off, not enough players, printed
    twice by mistake.

    Only ever touches a round with no scores in it. A settled round is refused
    outright: if one of those needs undoing, it has money attached and should be
    corrected by re-entering the scores instead.
    """
    if not require_db(args):
        return 1

    with storage.connect(args.db) as conn:
        rounds = conn.execute(
            "SELECT * FROM rounds WHERE played_on = ? ORDER BY round_id", (args.date,)
        ).fetchall()
        if not rounds:
            print(f"No rounds on {args.date}.")
            return 1

        for r in rounds:
            if args.course and r["course"] != args.course.strip().upper()[:1]:
                continue
            if r["status"] != "draft":
                print(f"{args.date} {r['course']}: status is '{r['status']}', not a "
                      f"draft - refusing. A settled round has money attached; correct "
                      f"it by re-entering the scores instead.")
                continue

            scored = conn.execute(
                """SELECT COUNT(*) c FROM entries WHERE round_id = ?
                   AND points_front IS NOT NULL""", (r["round_id"],)).fetchone()["c"]
            total = conn.execute("SELECT COUNT(*) c FROM entries WHERE round_id = ?",
                                 (r["round_id"],)).fetchone()["c"]
            if scored:
                print(f"{args.date} {r['course']}: {scored} player(s) already have "
                      f"points entered - refusing. Post it, or clear the scores first.")
                continue

            print(f"{args.date} {r['course']}: round {r['round_no']}, "
                  f"{total} player(s) posted, no scores entered")
            if args.apply:
                conn.execute("DELETE FROM rounds WHERE round_id = ?", (r["round_id"],))
                print("   removed")
            else:
                print("   (dry run - add --apply to remove it)")
    return 0


def cmd_report(args) -> int:
    if not require_db(args):
        return 1
    Path(args.out).mkdir(parents=True, exist_ok=True)
    with storage.connect(args.db) as conn:
        pdf = str(Path(args.out) / f"Cartel_Member_Stats_{args.year}.pdf")
        reports.ytd_report(pdf, conn, args.year)
        xl = str(Path(args.out) / f"Golf_Stats_{args.year}.xlsx")
        reports.export_workbook(xl, conn, args.year)
        rec = stats.house_reconciliation(conn, args.year)
    print(f"{pdf}\n{xl}\nBooks: {rec}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Cartel golf stats")
    ap.add_argument("--db", default=None, help="path to the SQLite file")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("import", help="load the legacy Golf_Stats.xlsx")
    p.add_argument("xlsx")
    p.add_argument("--ytd", help="YTD_Winnings.xlsx, the opening money balances")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("prepare", help="tee sheet PDF in, scoresheet out")
    p.add_argument("pdf")
    p.add_argument("--out", default="out")
    p.add_argument("--no-add-guests", dest="add_guests", action="store_false",
                   default=True,
                   help="fail on unrecognised names instead of adding them as guests")
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("settle", help="results CSV in, money out")
    p.add_argument("round_id", type=int)
    p.add_argument("csv")
    p.add_argument("--out", default="out")
    p.add_argument("--dry-run", action="store_true",
                   help="show the money without posting the round")
    p.set_defaults(func=cmd_settle)

    p = sub.add_parser("fix-course", help="repair a round with a bad Course value")
    p.add_argument("date", help="the round date, e.g. 2026-02-28")
    p.add_argument("course", help="N or S")
    p.add_argument("--apply", action="store_true", help="actually make the change")
    p.set_defaults(func=cmd_fix_course)

    p = sub.add_parser("cancel", help="remove a round that never happened")
    p.add_argument("date", help="the round date, e.g. 2026-09-06")
    p.add_argument("--course", help="N or S, if two rounds share the date")
    p.add_argument("--apply", action="store_true", help="actually remove it")
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("migrate", help="copy this database to a hosted one")
    p.add_argument("--to", required=True, help="destination connection string")
    p.add_argument("--apply", action="store_true", help="actually copy")
    p.add_argument("--force", action="store_true",
                   help="replace the destination if it already has rounds")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("report", help="regenerate the year-to-date report")
    p.add_argument("year", type=int, nargs="?", default=date.today().year)
    p.add_argument("--out", default="out")
    p.set_defaults(func=cmd_report)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
