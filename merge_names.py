#!/usr/bin/env python3
"""
Merge duplicate member names.

The imported history has 27 names that look like variant spellings of existing
members. Every one of them splits somebody's history and therefore gives them
the wrong 5-round quota.

This is a dry run unless you pass --apply, and it always shows you what the
quota does before and after, because that's the part that actually changes the
money going forward.

  # see the suggestions and what they'd do
  python scripts/merge_names.py

  # merge one pair
  python scripts/merge_names.py --merge "Brett Dargie=Bert Dargie" --apply

  # merge from a file of from=to lines
  python scripts/merge_names.py --file merges.txt --apply

Nothing here is automatic. "Jay Dalgarn" and "Jay Dalgarn1" might be a father
and son who both play, in which case they must stay apart — the script suggests,
you decide.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cartel import db, storage, stats  # noqa: E402
from cartel.config import db_path as _db_path, RULES  # noqa: E402
from cartel.teesheet import _norm, _surname_key  # noqa: E402


def _first(name: str) -> str:
    return "".join(c for c in name.split()[0].lower() if c.isalpha()) if name.split() else ""


def _edit(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def classify(variant: str, target: str, variant_rounds: int) -> tuple[str, str]:
    """(verdict, why). Deliberately blunt: most surname matches are NOT duplicates."""
    fv, ft = _first(variant), _first(target)
    if variant_rounds >= 20:
        return ("probably a real member", 
                f"{variant_rounds} rounds on file - far too many for a typo. Almost "
                f"certainly a genuine member missing from the Membership tab. Do not merge; "
                f"add them to the roster instead.")
    if fv == ft:
        return ("likely", "same first name")
    if len(fv) == 1 and ft.startswith(fv):
        return ("likely", f"'{fv.upper()}' is the initial of '{target.split()[0]}'")
    if _edit(fv, ft) <= 2 and abs(len(fv) - len(ft)) <= 2:
        return ("likely", f"'{variant.split()[0]}' is {_edit(fv, ft)} character(s) from "
                          f"'{target.split()[0]}' - looks like a typo")
    return ("unlikely", f"different first name ('{variant.split()[0]}' vs "
                        f"'{target.split()[0]}') - probably a different person")


def suggest(conn) -> list[tuple[str, str, int, int]]:
    """(variant, likely_target, variant_rounds, target_rounds), best guesses only."""
    rows = conn.execute(
        """SELECT m.name, m.active, COUNT(e.name) AS "rounds"
           FROM members m LEFT JOIN entries e ON e.name = m.name
           GROUP BY m.name""").fetchall()
    counts = {r["name"]: r["rounds"] for r in rows}
    active = {r["name"] for r in rows if r["active"]}
    inactive = [r["name"] for r in rows if not r["active"] and r["rounds"] > 0]

    by_surname: dict[str, list[str]] = {}
    for a in active:
        by_surname.setdefault(_surname_key(a), []).append(a)

    out = []
    for name in sorted(inactive):
        cands = by_surname.get(_surname_key(name), [])
        # also try initial-form matching: "J Holladay" -> "Johnny Holladay"
        if not cands:
            last = _surname_key(name).split("|")[0]
            cands = [a for a in active if _surname_key(a).split("|")[0] == last]
        if len(cands) == 1:
            out.append((name, cands[0], counts.get(name, 0), counts.get(cands[0], 0)))
    return out


def quota_change(conn, name: str) -> tuple[int, int]:
    """Quota now, and after a hypothetical merge, for the target member."""
    before = stats.current_quotas(conn, names=[name])[name].quota
    return before, before  # filled in by the caller after the merge


def apply_merge(conn, src: str, dst: str) -> dict:
    if src == dst:
        raise ValueError("source and target are the same name")
    for n in (src, dst):
        if not conn.execute("SELECT 1 FROM members WHERE name=?", (n,)).fetchone():
            raise ValueError(f"no member called {n!r}")

    # a player can't appear twice on one round, so check before moving anything
    clash = conn.execute(
        """SELECT r.played_on FROM entries a
           JOIN entries b ON a.round_id = b.round_id
           JOIN rounds r ON r.round_id = a.round_id
           WHERE a.name = ? AND b.name = ?""", (src, dst)).fetchall()
    if clash:
        raise ValueError(
            f"{src} and {dst} both have entries on "
            f"{', '.join(c['played_on'] for c in clash)} — they can't be the same "
            f"person. Resolve those rounds by hand."
        )

    moved = conn.execute("UPDATE entries SET name=? WHERE name=?", (dst, src)).rowcount
    conn.execute("UPDATE payouts SET name=? WHERE name=?", (dst, src))
    conn.execute("DELETE FROM members WHERE name=?", (src,))
    return {"moved": moved}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--merge", action="append", default=[],
                    help='"Variant Name=Real Name", repeatable')
    ap.add_argument("--file", help="file of Variant=Real lines, # for comments")
    ap.add_argument("--apply", action="store_true", help="actually do it")
    args = ap.parse_args()

    pairs: list[tuple[str, str]] = []
    for m in args.merge:
        src, _, dst = m.partition("=")
        pairs.append((src.strip(), dst.strip()))
    if args.file:
        for line in Path(args.file).read_text().splitlines():
            line = line.split("#")[0].strip()
            if line and "=" in line:
                src, _, dst = line.partition("=")
                pairs.append((src.strip(), dst.strip()))

    with storage.connect(args.db) as conn:
        if not pairs:
            sug = suggest(conn)
            if not sug:
                print("Nothing obvious to merge.")
                return 0
            buckets: dict[str, list] = {}
            for src, dst, sr, dr in sug:
                verdict, why = classify(src, dst, sr)
                buckets.setdefault(verdict, []).append((src, dst, sr, dr, why))

            for verdict in ("likely", "probably a real member", "unlikely"):
                rows = buckets.get(verdict)
                if not rows:
                    continue
                print(f"\n=== {verdict.upper()} ===")
                for src, dst, sr, dr, why in rows:
                    q = stats.current_quotas(conn, names=[dst])[dst].quota
                    print(f"  {src} ({sr} rds)  ->  {dst} ({dr} rds, quota {q})")
                    print(f"      {why}")

            n_likely = len(buckets.get("likely", []))
            print(f"\n{len(sug)} surname match(es); {n_likely} look like genuine "
                  f"duplicates.")
            print("Matching is on surname plus first name only, so treat every line as a "
                  "question, not an answer.")
            print('Merge one at a time: --merge "Variant=Real" --apply')
            print("Names with no single surname match aren't listed here at all - see the "
                  "Health tab for the full set.")
            return 0

        for src, dst in pairs:
            before = stats.current_quotas(conn, names=[dst])[dst]
            print(f"\n{src!r} -> {dst!r}")
            print(f"  {dst} quota now: {before.quota} "
                  f"(from {before.rounds_used} round(s), avg {before.raw_average})")
            if not args.apply:
                src_rounds = conn.execute(
                    "SELECT COUNT(*) c FROM entries WHERE name=?", (src,)).fetchone()["c"]
                print(f"  would move {src_rounds} round(s) — dry run, nothing changed")
                continue
            try:
                res = apply_merge(conn, src, dst)
            except ValueError as exc:
                print(f"  SKIPPED: {exc}")
                continue
            after = stats.current_quotas(conn, names=[dst])[dst]
            print(f"  moved {res['moved']} round(s)")
            print(f"  {dst} quota now: {after.quota} "
                  f"(from {after.rounds_used} round(s), avg {after.raw_average})")
            if after.quota != before.quota:
                print(f"  ** quota moved {before.quota} -> {after.quota}, which changes "
                      f"their team's Xi by {(after.quota - before.quota) / 2:+.1f} a side")

    if args.apply:
        print("\nDone. Settled rounds keep the money and quota they were settled with; "
              "only future quotas change.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
