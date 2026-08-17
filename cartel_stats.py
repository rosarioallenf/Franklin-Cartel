"""
Cumulative statistics.

Year-to-date money = the seeded opening balance from the group's own YTD
Winnings sheet, plus everything this app has settled since. Rounds, greens and
skins are counted from the points history, which was checked against the report
dated 18-Jul-26 and matches for all 44 members.
"""
from __future__ import annotations

import pandas as pd

from cartel_config import RULES, LEGACY_STAKE, Stake
from cartel_quota import compute_quota, QuotaResult
import cartel_db as db
import cartel_storage as storage


def player_history(conn, year: int | None = None) -> pd.DataFrame:
    sql = "SELECT * FROM v_player_rounds"
    params: list = []
    if year:
        sql += " WHERE played_on >= ? AND played_on < ?"
        params += [f"{year}-01-01", f"{year + 1}-01-01"]
    sql += " ORDER BY played_on DESC, round_id DESC, name"
    df = db.read_sql(sql, conn, params=params)
    if not df.empty:
        df["played_on"] = pd.to_datetime(df["played_on"])
    return df


def _quota_inputs(conn, before: str | None = None):
    """
    Everything current_quotas needs, in three queries flat.

    Returns (rounds on file per player, the most recent N point totals per
    player, manual overrides). The window function that picks the recent N
    works on both SQLite and Postgres.

    The per-player version issued three queries each - 138 for the roster,
    and it was called eleven times in one render. Fine against a local file;
    minutes against a database reached over the internet.
    """
    where = "WHERE points_total IS NOT NULL"
    params: list = []
    if before:
        where += " AND played_on < ?"
        params.append(before)

    counts = {r["name"]: r["c"] for r in conn.execute(
        f'SELECT name, COUNT(*) AS "c" FROM v_player_rounds {where} GROUP BY name',
        params)}

    recent: dict[str, list[int]] = {}
    rows = conn.execute(
        f"SELECT name, points_total FROM ("
        f"  SELECT name, points_total, ROW_NUMBER() OVER ("
        f"    PARTITION BY name ORDER BY played_on DESC, round_id DESC"
        f'  ) AS "rn" FROM v_player_rounds {where}'
        f') t WHERE "rn" <= ?',
        params + [RULES.quota_window])
    for r in rows:
        recent.setdefault(r["name"], []).append(r["points_total"])

    manuals = {r["name"]: r["manual_quota"] for r in conn.execute(
        'SELECT name, manual_quota FROM members WHERE manual_quota IS NOT NULL')}

    return counts, recent, manuals


def current_quotas(conn, before: str | None = None,
                   names: list[str] | None = None) -> dict[str, QuotaResult]:
    """
    name -> QuotaResult for every member (or just `names`).
    `before` (ISO date) computes quotas as they stood before that date.
    """
    if names is None:
        names = [r["name"] for r in storage.all_members(conn)]

    counts, recent, manuals = _quota_inputs(conn, before)

    out: dict[str, QuotaResult] = {}
    for n in names:
        available = counts.get(n, 0)
        pts = recent.get(n, [])
        q = compute_quota(n, pts, rounds_available=available)

        manual = manuals.get(n)
        if manual is not None:
            q.quota = manual
            q.is_guest = False
            q.note = (f"Manual override in place ({manual}). "
                      + (f"The rolling average would say {q.raw_average:.1f}."
                         if q.raw_average is not None
                         else f"Only {available} round(s) on file, so there would "
                              f"otherwise be no quota."))
        out[n] = q
    return out


def year_to_date(conn, year: int) -> pd.DataFrame:
    """The full member stats table for a calendar year."""
    hist = player_history(conn, year)
    seed = storage.seeds(conn, year)

    members = pd.DataFrame(
        [{"Name": r["name"], "Tee": r["tee"], "Tee_No": r["tee_no"], "Active": r["active"]}
         for r in storage.all_members(conn)]
    )
    if members.empty:
        members = pd.DataFrame(columns=["Name", "Tee", "Tee_No", "Active"])

    if hist.empty:
        agg = pd.DataFrame(columns=["Name", "Rds", "Greens", "Skins",
                                    "Posted_Team$", "Posted_Skat$", "Ante$"])
    else:
        agg = (hist.groupby("name")
                   .agg(Rds=("round_id", "count"),
                        Greens=("greens", "sum"), Skins=("skins", "sum"),
                        **{"Posted_Team$": ("team_money", "sum"),
                           "Posted_Skat$": ("skat_money", "sum"),
                           "Ante$": ("ante", "sum")})
                   .reset_index().rename(columns={"name": "Name"}))

    df = members.merge(agg, on="Name", how="outer")
    for c in ["Rds", "Greens", "Skins", "Posted_Team$", "Posted_Skat$", "Ante$"]:
        if c not in df:
            df[c] = 0.0
        df[c] = df[c].fillna(0)

    df["Seed_Team$"] = df["Name"].map(lambda n: seed.get(n, {}).get("team_money", 0.0))
    df["Seed_Skat$"] = df["Name"].map(lambda n: seed.get(n, {}).get("skat_money", 0.0))
    df["Team$"] = df["Seed_Team$"] + df["Posted_Team$"]
    df["Skat$"] = df["Seed_Skat$"] + df["Posted_Skat$"]
    df["Won$"] = df["Team$"] + df["Skat$"]
    df["Skats"] = df["Greens"] + df["Skins"]

    quotas = current_quotas(conn, names=list(df["Name"]))
    df["Pts"] = df["Name"].map(lambda n: quotas[n].quota if n in quotas else None)
    df["Guest"] = df["Name"].map(lambda n: quotas[n].is_guest if n in quotas else False)

    rds = df["Rds"].replace(0, pd.NA)
    for num, out in [("Won$", "$/Rd"), ("Greens", "Grns/Rd"),
                     ("Skins", "Skins/Rd"), ("Skats", "Skats/Rd")]:
        df[out] = (df[num] / rds).fillna(0.0).astype(float)

    # An empty history leaves these as dtype object, and nlargest() then refuses
    # to sort them - so a brand new database crashed the Standings tab. Force the
    # types regardless of whether there are any rows.
    for c in ["Rds", "Greens", "Skins", "Skats"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype(int)
    for c in ["Seed_Team$", "Seed_Skat$", "Posted_Team$", "Posted_Skat$",
              "Team$", "Skat$", "Won$", "$/Rd", "Grns/Rd", "Skins/Rd", "Skats/Rd"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0).astype(float)
    df["Guest"] = df["Guest"].fillna(False).astype(bool)
    # Pts is genuinely absent for a guest, so it stays nullable - but as a float
    # rather than object, or sorting on it fails the same way.
    df["Pts"] = pd.to_numeric(df["Pts"], errors="coerce").astype(float)

    # Rank on earnings per round, 1 = highest. Computed once here so the
    # screen, the PDF and the CSV cannot disagree about who is where.
    # Ties share a rank (1, 2, 2, 4) - two players on identical $/Rd are level,
    # and inventing an order between them would be arbitrary.
    # Rounds inside the ranking window, measured back from the last round on
    # file rather than from today - so the same report always ranks the same
    # people, whenever it's produced.
    df["Rds_Window"] = df["Name"].map(rounds_in_window(conn)).fillna(0).astype(int)

    # Nullable Int64, not object: a plain pd.NA makes the column dtype object,
    # and sorting on an object column raises. Same trap as the Pts column.
    eligible = df["Rds_Window"] >= RULES.rank_min_rounds
    ranks = pd.Series(pd.NA, index=df.index, dtype="Int64")
    if eligible.any():
        ranks.loc[eligible] = (
            df.loc[eligible, "$/Rd"].rank(ascending=False, method="min")
            .astype(int).astype("Int64"))
    df["Rank"] = ranks

    cols = ["Name", "Rank", "Rds_Window", "Pts", "Guest", "Tee", "Tee_No", "Rds",
            "Seed_Team$", "Seed_Skat$", "Posted_Team$", "Posted_Skat$",
            "Team$", "Skat$", "Won$", "$/Rd",
            "Greens", "Grns/Rd", "Skins", "Skins/Rd", "Skats", "Skats/Rd", "Active"]
    return df[cols].sort_values("Name").reset_index(drop=True)


def rounds_in_window(conn, months: int | None = None) -> dict[str, int]:
    """
    Completed rounds per player inside the ranking window.

    Counted back from the most recent round on file, not from today. A report
    describing a settled state must not change because a fortnight passed with
    no golf - otherwise somebody drops out of the ranking without playing, or
    failing to play, anything.
    """
    months = months or RULES.rank_window_months
    row = conn.execute("SELECT MAX(played_on) m FROM v_player_rounds").fetchone()
    if row is None or row["m"] is None:
        return {}
    anchor = pd.Timestamp(row["m"])
    cutoff = (anchor - pd.DateOffset(months=months)).date().isoformat()
    return {r["name"]: r["n"] for r in conn.execute(
        "SELECT name, COUNT(*) n FROM v_player_rounds WHERE played_on > ? GROUP BY name",
        (cutoff,))}


def leaderboards(conn, year: int, min_rounds: int = 4,
                 ytd: pd.DataFrame | None = None) -> dict[str, pd.DataFrame]:
    df = year_to_date(conn, year) if ytd is None else ytd
    played = df[(df["Rds"] >= min_rounds) & (~df["Guest"])]
    if played.empty:
        # Nothing to rank yet. Return the right shape so the UI still draws.
        empty = played
        return {
            "Most money won": empty[["Name", "Rds", "Won$", "$/Rd"]],
            "Best per round": empty[["Name", "Rds", "Won$", "$/Rd"]],
            "Most skats": empty[["Name", "Rds", "Greens", "Skins", "Skats"]],
            "Best skat rate": empty[["Name", "Rds", "Skats", "Skats/Rd"]],
            "Highest quota": empty[["Name", "Rds", "Pts"]],
            "Most team money": empty[["Name", "Rds", "Team$"]],
        }
    return {
        "Most money won": played.nlargest(10, "Won$")[["Name", "Rds", "Won$", "$/Rd"]],
        "Best per round": played.nlargest(10, "$/Rd")[["Name", "Rds", "Won$", "$/Rd"]],
        "Most skats": played.nlargest(10, "Skats")[["Name", "Rds", "Greens", "Skins", "Skats"]],
        "Best skat rate": played.nlargest(10, "Skats/Rd")[["Name", "Rds", "Skats", "Skats/Rd"]],
        "Highest quota": played.nlargest(10, "Pts")[["Name", "Rds", "Pts"]],
        "Most team money": played.nlargest(10, "Team$")[["Name", "Rds", "Team$"]],
    }


def year_summary(conn, year: int) -> dict:
    """
    The whole calendar year, imported rounds included.

    Distinct from house_reconciliation, which only covers rounds this app
    settled. Mixing the two is how the standings header ended up showing two
    rounds and $590 next to a full year's worth of skats.

    Money in the pot is taken from the recorded ante where there is one, and
    otherwise from the house rate for that player - imported rounds carry no
    ante, but everyone who completed one paid the going rate.
    """
    rows = conn.execute(
        """SELECT v.is_guest, COALESCE(p.ante, -1) AS ante,
                  r.member_ante, r.guest_ante
           FROM v_player_rounds v
           JOIN rounds r ON r.round_id = v.round_id
           LEFT JOIN payouts p ON p.round_id = v.round_id AND p.name = v.name
           WHERE v.played_on >= ? AND v.played_on < ?""",
        (f"{year}-01-01", f"{year + 1}-01-01")).fetchall()

    # Each round at ITS OWN rate. Applying today's stake to imported rounds
    # would restate the whole season the day the stake changed.
    collected = 0.0
    for r in rows:
        if r["ante"] >= 0:
            collected += r["ante"]                 # what was actually charged
            continue
        rate = Stake(member_ante=r["member_ante"], guest_ante=r["guest_ante"]) \
            if r["member_ante"] is not None else LEGACY_STAKE
        collected += rate.ante_for(bool(r["is_guest"]))

    n_rounds = conn.execute(
        """SELECT COUNT(DISTINCT round_id) n FROM v_player_rounds
           WHERE played_on >= ? AND played_on < ?""",
        (f"{year}-01-01", f"{year + 1}-01-01")).fetchone()["n"]

    return {"rounds": n_rounds, "player_rounds": len(rows), "collected": collected,
            "written_off": storage.written_off(conn, year)}


def house_reconciliation(conn, year: int) -> dict:
    """
    Money in versus money out, for rounds this app settled. Legacy rounds are
    excluded because their money lives in the seed, not in payouts.
    """
    rounds = db.read_sql("""SELECT * FROM rounds WHERE status='posted'
           AND played_on >= ? AND played_on < ?""",
        conn, params=[f"{year}-01-01", f"{year + 1}-01-01"])
    if rounds.empty:
        return {"rounds": 0, "player_rounds": 0, "collected": 0.0,
                "paid_out": 0.0, "carried": 0.0, "unaccounted": 0.0}

    pay = db.read_sql("""SELECT p.* FROM payouts p JOIN rounds r ON r.round_id = p.round_id
           WHERE r.status='posted' AND r.played_on >= ? AND r.played_on < ?""",
        conn, params=[f"{year}-01-01", f"{year + 1}-01-01"])

    collected = float(pay["ante"].sum())
    paid = float(pay["team_money"].sum() + pay["skat_money"].sum())
    carried = float(rounds["carried_out"].sum())
    return {
        "rounds": int(rounds["round_id"].nunique()),
        "player_rounds": len(pay),
        "collected": collected,
        "paid_out": paid,
        "carried": carried,
        "unaccounted": collected - paid - carried,
    }


def quota_basis(conn, names: list[str] | None = None,
                before: str | None = None) -> pd.DataFrame:
    """
    The rounds each player's NEXT quota will be built from, one row per round
    plus an Average row per player.

    Deliberately mirrors storage.recent_points - same view, same filter, same
    ORDER BY, same window - so this report can never disagree with the quota
    the app actually applies. If the two ever diverge, that's a bug worth
    hearing about, not a rounding quirk to explain away.
    """
    if names is None:
        names = [r["name"] for r in storage.all_members(conn)]

    window = RULES.quota_window

    # One query for everybody, not one per player. Same view, same filter, same
    # ordering as storage.recent_points - the window function just takes the top
    # N per player in a single pass instead of a query each.
    where = "WHERE points_total IS NOT NULL"
    params: list = []
    if before:
        where += " AND played_on < ?"
        params.append(before)
    sql = (
        f"SELECT name, played_on, score, points_front, points_back, points_total "
        f"FROM ( SELECT name, played_on, score, points_front, points_back, "
        f"       points_total, ROW_NUMBER() OVER (PARTITION BY name "
        f'       ORDER BY played_on DESC, round_id DESC) AS "rn" '
        f"       FROM v_player_rounds {where} ) t "
        f'WHERE "rn" <= ? ORDER BY name, "rn"'
    )
    # The same counts current_quotas uses - one query, not one per player.
    counts, _recent, _manuals = _quota_inputs(conn, before)

    by_player: dict[str, list[dict]] = {}
    for r in conn.execute(sql, params + [window]):
        d = dict(r)
        by_player.setdefault(d.pop("name"), []).append(d)

    frames = []
    for n in sorted(names):
        rows = by_player.get(n, [])
        if not rows:
            continue

        # Total holds round scores, an average with a decimal, and the word
        # "guest" for a player without a quota. Mixed types break the Arrow
        # conversion Streamlit uses, so the whole column is text.
        # Every column is text. Score/Front/Back are whole numbers on the round
        # rows and blank on the Average and Quota rows; left numeric, pandas
        # promotes them to float and they print as 90.0 rather than 90.
        def _n(v):
            return "" if v is None else str(int(v))

        block = [{"Player": n, "Date": r["played_on"], "Score": _n(r["score"]),
                  "Front": _n(r["points_front"]), "Back": _n(r["points_back"]),
                  "Total": _n(r["points_total"])} for r in rows]

        totals = [r["points_total"] for r in rows]
        avg = sum(totals) / len(totals)
        available = counts.get(n, 0)
        q = compute_quota(n, totals, rounds_available=available)

        block.append({
            "Player": n,
            "Date": f"Average of {len(totals)}" + ("" if len(totals) == window
                                                   else f" (short of {window})"),
            "Score": "", "Front": "", "Back": "",
            "Total": f"{avg:.1f}",
        })
        block.append({
            "Player": n, "Date": "Quota", "Score": "",
            "Front": "", "Back": "",
            "Total": q.display,
        })
        frames.append(pd.DataFrame(block))

    if not frames:
        return pd.DataFrame(columns=["Player", "Date", "Score", "Front", "Back", "Total"])
    return pd.concat(frames, ignore_index=True)


# --------------------------------------------------------------------------
# season awards and the player page
# --------------------------------------------------------------------------

def season_awards(conn, year: int, ytd: pd.DataFrame | None = None) -> dict:
    """
    The season as it stands right now - "if it ended today".

    Every award is computed from rounds already settled, so running this in
    August gives August's answer and running it in December gives the year's.
    Nothing is frozen or cached; the awards move as rounds are posted.

    Money awards use the eligibility rule that governs the standings ranking:
    at least `rank_min_rounds` rounds in the window. Counting awards (most
    skats, most rounds) have no threshold - they measure turning up, and
    turning up is the qualification.
    """
    df = year_to_date(conn, year) if ytd is None else ytd
    played = df[df["Rds"] > 0].copy()
    eligible = played[played["Rank"].notna()]

    hist = player_history(conn, year)
    anchor = storage.last_posted_round(conn)

    def top(frame, column, cols, biggest=True, n=3):
        if frame.empty or column not in frame:
            return pd.DataFrame(columns=cols)
        ranked = frame.nlargest(n, column) if biggest else frame.nsmallest(n, column)
        return ranked[cols].reset_index(drop=True)

    awards: dict[str, dict] = {}

    awards["Order of Merit"] = {
        "blurb": "Most money won over the season.",
        "table": top(played, "Won$", ["Name", "Rds", "Won$"]),
    }
    awards["Best per round"] = {
        "blurb": (f"Highest $ won per round played. Needs {RULES.rank_min_rounds}+ "
                  f"rounds in the last {RULES.rank_window_months} months."),
        "table": top(eligible, "$/Rd", ["Name", "Rds", "$/Rd"]),
    }
    awards["Most skats"] = {
        "blurb": "Greens and skins added together. No qualification: turn up and win them.",
        "table": top(played, "Skats", ["Name", "Rds", "Greens", "Skins", "Skats"]),
    }
    awards["Sharpest iron"] = {
        "blurb": "Most greens — closest to the pin on a par 3.",
        "table": top(played, "Greens", ["Name", "Rds", "Greens", "Grns/Rd"]),
    }
    awards["Most skins"] = {
        "blurb": "Most holes won outright.",
        "table": top(played, "Skins", ["Name", "Rds", "Skins", "Skins/Rd"]),
    }
    awards["Iron man"] = {
        "blurb": "Most rounds played.",
        "table": top(played, "Rds", ["Name", "Rds", "Won$"]),
    }

    if not hist.empty:
        rounds = hist.copy()
        rounds["Date"] = rounds["played_on"].dt.date.astype(str)
        best = rounds.nlargest(3, "points_total")[
            ["name", "Date", "points_total", "score"]]
        best.columns = ["Name", "Date", "Points", "Score"]
        awards["Round of the year"] = {
            "blurb": "The highest points total anyone has posted in a single round.",
            "table": best.reset_index(drop=True),
        }

        with_score = rounds[rounds["score"].notna() & (rounds["score"] > 0)]
        if not with_score.empty:
            low = with_score.nsmallest(3, "score")[["name", "Date", "score", "points_total"]]
            low.columns = ["Name", "Date", "Score", "Points"]
            awards["Lowest score"] = {
                "blurb": "The lowest gross score of the season.",
                "table": low.reset_index(drop=True),
            }

    # Most improved: quota now against quota at the turn of the year. Only for
    # players who had a quota back then, or "improvement" is just arriving.
    start = current_quotas(conn, before=f"{year}-01-01")
    now = current_quotas(conn)
    moved = []
    for name, then in start.items():
        if then.is_guest or then.quota is None:
            continue
        current = now.get(name)
        if current is None or current.is_guest or current.quota is None:
            continue
        rds = int(played.loc[played["Name"] == name, "Rds"].sum())
        if rds < RULES.rank_min_rounds:
            continue
        moved.append({"Name": name, "Rds": rds, "Then": then.quota,
                      "Now": current.quota, "Gain": current.quota - then.quota})
    if moved:
        m = pd.DataFrame(moved)
        awards["Most improved"] = {
            "blurb": (f"Biggest rise in quota since 1 January. Needs "
                      f"{RULES.rank_min_rounds}+ rounds and a quota at the start "
                      f"of the year."),
            "table": m.nlargest(3, "Gain").reset_index(drop=True),
        }

    return {
        "as_of": anchor["played_on"] if anchor is not None else None,
        "as_of_round": (anchor["round_no"] or anchor["round_id"]) if anchor is not None else None,
        "rounds": int(year_summary(conn, year)["rounds"]),
        "awards": awards,
    }


def player_card(conn, name: str, year: int) -> dict:
    """Everything about one member, for the player page."""
    ytd = year_to_date(conn, year)
    row = ytd[ytd["Name"] == name]
    summary = row.iloc[0].to_dict() if not row.empty else {}

    hist = player_history(conn, year)
    rounds = hist[hist["name"] == name].copy()
    if not rounds.empty:
        rounds["Date"] = rounds["played_on"].dt.date.astype(str)
        rounds = rounds[["Date", "course", "team_no", "points_front", "points_back",
                         "points_total", "score", "greens", "skins",
                         "team_money", "skat_money"]]
        rounds.columns = ["Date", "Course", "Team", "Front", "Back", "Points",
                          "Score", "Greens", "Skins", "Team $", "Skat $"]
        rounds["Won $"] = rounds["Team $"] + rounds["Skat $"]

    # Quota after each round of the year, so the trend can be drawn
    # What their quota was after each round, so the trend can be drawn. Rebuilt
    # from the history rather than stored: quotas are a moving five-round
    # average and were never meant to be frozen per round.
    # One query for this player's whole scored history, then the rolling quota
    # is worked out in memory. The old version asked the database twice for
    # every point on the chart - fine against a local file, but a man with
    # eighteen rounds cost thirty-six round trips across the internet.
    every = [dict(r) for r in conn.execute(
        """SELECT played_on, points_total FROM v_player_rounds
           WHERE name = ? AND points_total IS NOT NULL
           ORDER BY played_on ASC, round_id ASC""", (name,))]

    trend = []
    dates = sorted(hist[hist["name"] == name]["played_on"].dt.date.astype(str)) \
        if not hist.empty else []
    for d in dates:
        # everything up to and including that date, most recent first -
        # exactly what the query used to return
        upto = [r["points_total"] for r in every if str(r["played_on"])[:10] <= d]
        recent = list(reversed(upto))[:RULES.quota_window]
        q = compute_quota(name, recent, rounds_available=len(upto))
        trend.append({"Date": d, "Quota": q.quota})

    return {
        "name": name,
        "summary": summary,
        "rounds": rounds if not hist.empty else pd.DataFrame(),
        "quota_trend": pd.DataFrame(trend),
        "basis": quota_basis(conn, names=[name]),
    }
