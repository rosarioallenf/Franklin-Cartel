"""
Cartel — the web app.

Run locally:   streamlit run app.py
Deploy:        see README.md

Five tabs, in the order the round actually happens:
  Today        prep a round from the tee sheet, print the scoresheet
  Enter scores type in the results, see the money before posting
  Standings    year-to-date member stats and leaderboards
  Roster       who's in the group, their tees and quotas
  Health       data problems and house reconciliation

Note: the Roster TAB is a screen in this app. The "Membership" SHEET in
Golf_Stats.xlsx is a different thing, read once at import and never again.
"""
from __future__ import annotations

import io
import os
import time
import tempfile
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from cartel import backup, db, storage, stats, scoring, reports, scoresheet, pipeline
from cartel.config import RULES, Stake, db_path, TEE_CODES
from cartel.teesheet import parse_tee_sheet, reconcile_names

st.set_page_config(page_title="Cartel", page_icon="⛳", layout="wide")

# Streamlit Cloud supplies configuration through st.secrets rather than the
# environment, so copy anything we recognise across before the db layer reads it.
for _key in ("CARTEL_DB_URL", "ANTHROPIC_API_KEY", "CARTEL_VISION_MODEL"):
    try:
        if _key in st.secrets and not os.environ.get(_key):
            os.environ[_key] = str(st.secrets[_key])
    except Exception:
        pass  # no secrets file locally, which is fine

OUT = Path(os.environ.get("CARTEL_OUT", "out"))
OUT.mkdir(parents=True, exist_ok=True)
COURSES = {"N": "North", "S": "South"}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

@st.cache_resource
def _ensure_db():
    storage.init_db(db_path())
    return True


def money(v) -> str:
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


def download(path: str, label: str, key: str) -> None:
    p = Path(path)
    if p.exists():
        st.download_button(label, p.read_bytes(), file_name=p.name, key=key,
                           mime="application/pdf" if p.suffix == ".pdf" else None)


def save_upload(uploaded) -> str:
    suffix = Path(uploaded.name).suffix or ".pdf"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(uploaded.getbuffer())
    tmp.close()
    return tmp.name


def _pf(prefill: dict, entry, field: str):
    """Prefer an AI pre-fill value, then whatever is already saved, then blank."""
    row = prefill.get(entry["name"])
    if row is not None:
        v = getattr(row, field, None)
        if v is not None:
            return v
    return entry[field] if field in entry.keys() else None


def _played(prefill: dict, entry) -> bool:
    row = prefill.get(entry["name"])
    if row is not None:
        # A guest's points are never written down, so a photo of a blank guest
        # row is not evidence of absence. Trust the tee sheet over the camera.
        if entry["is_guest"] and not row.played and not row.uncertain:
            return True
        return bool(row.played)
    # stored explicitly, and on by default - never inferred from whether points
    # exist, because a guest's points are not recorded at all
    return bool(entry["played"]) if "played" in entry.keys() else True


_ensure_db()

st.title("⛳ Cartel")
with storage.connect() as _c:
    _stake = storage.current_stake(_c)
    _n_members = len(storage.all_members(_c))
    _n_rounds = len(storage.list_rounds(_c, 1))

st.caption(
    f"Quota is the rounded average of your last {RULES.quota_window} rounds. "
    f"{_stake.describe()}. A member's ante splits a quarter to the front, a quarter "
    f"to the back and a half to greens and skins. Fewer than "
    f"{RULES.guest_min_rounds} rounds on file and you play as a guest — whole ante "
    f"to greens and skins, no team money."
)

if _n_members == 0 and _n_rounds == 0:
    st.info(
        "**Nothing loaded yet.** The history import hasn't been run, so every tab "
        "below will be empty. From a terminal in this folder:\n\n"
        "```\npython scripts/cartel_cli.py import Golf_Stats.xlsx --ytd YTD_Winnings.xlsx\n```\n\n"
        "Then refresh this page."
    )

# ---- who is using the app ----
# With one scorer this was pointless; it was always Allen. With several, the
# first question when a figure looks wrong is "who entered this?", and until
# now nothing recorded it. Honour system by design: the point is a record, not
# a lock.
def current_user() -> str:
    return st.session_state.get("who") or ""


with storage.connect() as _c:
    _roster_names = [r["name"] for r in storage.all_members(_c, active_only=True)]

_w1, _w2 = st.columns([3, 2])
with _w2:
    _picked = st.selectbox(
        "You are", ["— choose your name —"] + _roster_names,
        index=(_roster_names.index(current_user()) + 1
               if current_user() in _roster_names else 0),
        key="who_pick", label_visibility="collapsed",
    )
    st.session_state["who"] = "" if _picked.startswith("—") else _picked
if not current_user():
    st.caption(
        "Pick your name above before entering or posting a round — it's recorded "
        "against what you do, so anything odd can be traced back and asked about."
    )


def require_admin(what: str, key: str) -> bool:
    """
    A gate on the handful of actions that change things for everybody: the
    stake, the roster, removing a round, writing money off.

    Not security - anyone with the link is a friend. It stops somebody
    exploring the Health tab from changing the stake for the whole group by
    accident, which is the failure that actually happens.
    """
    with storage.connect() as conn:
        needed = storage.admin_passphrase_set(conn)
    if not needed or st.session_state.get("admin_ok"):
        return True
    st.caption(f"{what} needs the admin word.")
    word = st.text_input("Admin word", type="password", key=f"pw_{key}")
    if st.button("Unlock", key=f"unlock_{key}"):
        with storage.connect() as conn:
            if storage.check_admin_passphrase(conn, word):
                st.session_state["admin_ok"] = True
                st.rerun()
        st.error("That's not it.")
    return False


# ---- shutting down properly ----
# Closing the browser leaves the server running: harmless, but it does mean the
# window stays open until Ctrl+C. This stops both from inside the app.
_x1, _x2 = st.columns([5, 1])
with _x2:
    if st.session_state.get("confirm_exit"):
        st.caption("Sure?")
        if st.button("Yes, close", type="primary", width='stretch'):
            st.session_state["confirm_exit"] = False
            st.success("Cartel has stopped. You can close this browser tab.")
            st.caption("Double-click START.bat when you want it again.")
            time.sleep(1.5)          # let the message reach the browser first
            os._exit(0)              # the whole point: stop the server, not the script
        if st.button("Cancel", width='stretch'):
            st.session_state["confirm_exit"] = False
            st.rerun()
    else:
        if st.button("Exit app", width='stretch',
                     help="Stops the app and closes the black window."):
            st.session_state["confirm_exit"] = True
            st.rerun()

(tab_today, tab_scores, tab_standings, tab_player, tab_quota, tab_roster,
 tab_health) = st.tabs(
    ["Today", "Enter scores", "Standings", "Player", "Next quota", "Roster", "Health"]
)


# ==========================================================================
# TODAY — prep a round
# ==========================================================================
with tab_today:
    st.subheader("Set up a round")
    st.write(
        "Drop in the tee sheet PDF. The app reads the round number, date, course and "
        "pairings, works out everybody's current quota, and gives you a scoresheet to print."
    )

    def show_prepared(prep):
        """Same summary whichever route built the round."""
        st.session_state["round_id"] = prep.round_id
        st.success(
            f"Round {prep.round_no} is set up — {len(prep.quotas)} players. "
            f"Head to **Enter scores** when you're back in.")
        rows = []
        for t in prep.teams:
            xi = sum(prep.quotas[n].quota for n in t["players"]
                     if not prep.quotas[n].is_guest) / 2
            for i, n in enumerate(t["players"]):
                # Every value in a column must be the same type. Mixing the team
                # number with "" to blank the repeats made the column dtype
                # object, which Arrow cannot serialise - Streamlit then patched
                # it up and printed a traceback that looked like a real fault.
                rows.append({
                    "Team": str(t["team_no"]) if i == 0 else "",
                    "Tee": t["tee_time"] if i == 0 else "",
                    "Player": n,
                    "Quota": prep.quotas[n].display,
                    "Rounds on file": prep.quotas[n].rounds_available,
                    "Xi": f"{xi:.1f}" if i == 0 else "",
                })
        st.dataframe(pd.DataFrame(rows), hide_index=True, width='stretch')
        download(prep.scoresheet_path, "⬇ Scoresheet PDF", f"dl_ss_{prep.round_id}")
        if prep.guests:
            st.info(
                "**Guests this round:** " + ", ".join(prep.guests)
                + f". They pay {money(_stake.guest_ante)}, play for greens and skins "
                f"only, and are not counted in their team's points or quota. Record "
                f"their points anyway — at {RULES.guest_min_rounds} scored rounds "
                f"they become full members automatically.")
        for n, q in prep.quotas.items():
            if q.short_history:
                st.info(f"**{n}** — {q.note}")
        for w in prep.warnings:
            st.warning(w)

    from_pdf, by_hand = st.tabs(["From the tee sheet PDF", "Enter teams by hand"])

    with by_hand:
        st.write(
            "For when the club's sheet won't read — a new format, a photo instead "
            "of a PDF, or teams sorted out in the car park. Everything after this "
            "is identical: same quotas, same scoresheet, same settlement."
        )
        with storage.connect() as conn:
            roster = [r["name"] for r in storage.all_members(conn, active_only=True)]

        h1, h2, h3 = st.columns(3)
        m_round = h1.number_input("Round number", 1, 9999, step=1, key="m_round")
        m_date = h2.date_input("Date played", value=date.today(), key="m_date")
        m_course = h3.selectbox("Course", ["N", "S"],
                                format_func=lambda c: COURSES[c], key="m_course")

        n_teams = st.number_input("How many teams?", 2, 8, 4, step=1, key="m_nteams")
        st.caption(
            f"Pick {RULES.min_team_size}–{RULES.max_team_size} players per team. "
            f"Anyone not on the list can be added on the Roster tab first.")

        picked, times = [], []
        cols = st.columns(min(int(n_teams), 4))
        for i in range(int(n_teams)):
            with cols[i % len(cols)]:
                st.markdown(f"**Team {i + 1}**")
                times.append(st.text_input("Tee time", key=f"m_t{i}",
                                           placeholder="10:03 AM", label_visibility="collapsed"))
                picked.append(st.multiselect(f"Team {i + 1} players", roster,
                                             key=f"m_p{i}", label_visibility="collapsed"))

        chosen = [n for team in picked for n in team]
        dupes = {n for n in chosen if chosen.count(n) > 1}
        if dupes:
            st.error("On more than one team: " + ", ".join(sorted(dupes)))
        elif chosen:
            st.caption(f"{len(chosen)} players across "
                       f"{len([t for t in picked if t])} team(s)")

        if st.button("Build the scoresheet from these teams", type="primary",
                     disabled=bool(dupes) or not chosen, key="m_build"):
            try:
                sheet = pipeline.manual_tee_sheet(
                    m_date, m_course, picked, round_no=int(m_round),
                    tee_times=[t for t in times])
                prep = pipeline.prepare_round(manual=sheet, out_dir=str(OUT))
            except Exception as exc:
                st.error(str(exc))
            else:
                show_prepared(prep)

    with from_pdf:
        up = st.file_uploader("Tee sheet PDF", type=["pdf"], key="tee_up")
        add_guests = st.checkbox(
            "Add anyone not on the roster",
            value=True,
            help=f"With no rounds on file they play as guests: {money(_stake.guest_ante)} in, "
                 f"greens and skins only, no quota and no team money. They become full "
                 f"members automatically once they have {RULES.guest_min_rounds} rounds.",
        )

        if up is not None:
            path = save_upload(up)
            try:
                preview = parse_tee_sheet(path)
            except Exception as exc:
                st.error(f"Couldn't read that PDF: {exc}")
                preview = None

            if preview:
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Round", preview.round_no or "?")
                c2.metric("Date", preview.played_on.strftime("%d %b %Y") if preview.played_on else "?")
                c3.metric("Course", COURSES.get(preview.courses[0], "?") if preview.courses else "?")
                c4.metric("Players", len(preview.players))

                with storage.connect() as conn:
                    _, unknown = reconcile_names(preview.players, storage.member_names(conn))
                if unknown:
                    st.warning(
                        "Not on the roster: **" + "**, **".join(unknown) + "**. "
                        + ("They'll be added and will play as guests." if add_guests
                           else "Tick the box above, or add them on the Roster tab.")
                    )
                for w in preview.warnings:
                    st.warning(w)

                # The sheet no longer prints team numbers - show what was inferred so
                # it can be eyeballed against the paper before anything is committed.
                st.caption(
                    "Teams read off the sheet — check these against the printed "
                    "sheet. **The Team column can be edited**: change a number to "
                    "move somebody before the scoresheet is printed, so the team "
                    "quota on it is right."
                )
                team_grid = st.data_editor(
                    pd.DataFrame([
                        {"Team": g.team_no,
                         "Tee": g.tee_time,
                         "Course": COURSES.get(g.course, g.course)
                                   + (f" / {g.tees}" if g.tees else ""),
                         "Player": n}
                        for g in preview.groups for n in g.players
                    ]),
                    hide_index=True, width='stretch',
                    disabled=["Tee", "Course", "Player"],
                    column_config={
                        "Team": st.column_config.NumberColumn(
                            min_value=1, max_value=12, step=1,
                            help="Golf Genius had him in the wrong group, or he's "
                                 "swapping. Change it here and the printed quota "
                                 "follows."),
                    },
                    key="team_preview",
                )

                # Only take the hand-entry route if something actually moved -
                # otherwise the PDF stays the source, warnings and all.
                original = {n: g.team_no for g in preview.groups for n in g.players}
                edited_teams = {r["Player"]: int(r["Team"])
                                for _, r in team_grid.iterrows()}
                teams_changed = edited_teams != original
                if teams_changed:
                    moved = [n for n in original if original[n] != edited_teams[n]]
                    st.info(
                        f"**Moved:** {', '.join(moved)}. The scoresheet will be built "
                        f"from the teams above, not from the PDF."
                    )

                if preview.errors:
                    st.error(
                        "**This tee sheet didn't parse cleanly, so nothing will be imported.**\n\n"
                        + "\n".join(f"- {e}" for e in preview.errors)
                        + "\n\nFix the PDF or enter the pairings by hand — don't guess."
                    )

                # If the course couldn't be read, or was read from something
                # ambiguous, let it be corrected here. Telling somebody to
                # "change it if that's wrong" without giving them anywhere to
                # change it is worse than not mentioning it.
                detected = preview.courses[0] if preview.courses else "N"
                course_override = detected
                if preview.course_defaulted or len(preview.courses) > 1:
                    st.warning(
                        f"The course wasn't read cleanly, so **{COURSES[detected]}** "
                        f"was assumed. Set it correctly here before building the "
                        f"scoresheet — it decides which course the round is recorded on."
                    )
                    course_override = st.radio(
                        "Which course was this round played on?",
                        ["N", "S"],
                        index=0 if detected == "N" else 1,
                        format_func=lambda c: COURSES[c],
                        horizontal=True, key="course_fix",
                    )
                else:
                    st.caption(f"Course read from the sheet: **{COURSES[detected]}**")
                    if st.checkbox("That's wrong — let me set it", key="course_wrong"):
                        course_override = st.radio(
                            "Which course was this round played on?",
                            ["N", "S"],
                            index=0 if detected == "N" else 1,
                            format_func=lambda c: COURSES[c],
                            horizontal=True, key="course_fix2",
                        )

                if st.button("Build the scoresheet", type="primary",
                             disabled=bool(preview.errors)):
                    try:
                        if teams_changed:
                            by_team: dict[int, list[str]] = {}
                            for player, t in edited_teams.items():
                                by_team.setdefault(t, []).append(player)
                            times = {g.team_no: g.tee_time for g in preview.groups}
                            ordered = sorted(by_team)
                            sheet = pipeline.manual_tee_sheet(
                                preview.played_on, course_override,
                                [by_team[t] for t in ordered],
                                round_no=preview.round_no,
                                tee_times=[times.get(t, "") for t in ordered])
                            prep = pipeline.prepare_round(
                                manual=sheet, out_dir=str(OUT),
                                add_unknown_as_guest=add_guests)
                            show_prepared(prep)
                            st.stop()
                        prep = pipeline.prepare_round(path, out_dir=str(OUT),
                                                      course=course_override,
                                                      add_unknown_as_guest=add_guests)
                    except Exception as exc:
                        st.error(str(exc))
                    else:
                        st.session_state["round_id"] = prep.round_id
                        st.success(
                            f"Round {prep.round_no} is set up — {len(prep.quotas)} players. "
                            f"Head to **Enter scores** when you're back in."
                        )
                        rows = []
                        for t in prep.teams:
                            xi = sum(prep.quotas[n].quota for n in t["players"]
                                     if not prep.quotas[n].is_guest) / 2
                            for i, n in enumerate(t["players"]):
                                rows.append({
                                    "Team": str(t["team_no"]) if i == 0 else "",
                                    "Tee": t["tee_time"] if i == 0 else "",
                                    "Player": n,
                                    "Quota": prep.quotas[n].display,
                                    "Rounds on file": prep.quotas[n].rounds_available,
                                    "Xi": f"{xi:.1f}" if i == 0 else "",
                                })
                        st.dataframe(pd.DataFrame(rows), hide_index=True,
                                     width='stretch')
                        download(prep.scoresheet_path, "⬇ Scoresheet PDF", "dl_ss")
                        if prep.guests:
                            st.info(
                                "**Guests this round:** " + ", ".join(prep.guests)
                                + f". They pay {money(_stake.guest_ante)}, play for greens and "
                                f"skins only, and are not counted in their team's points or "
                                f"quota. Record their points anyway — at "
                                f"{RULES.guest_min_rounds} scored rounds they become full "
                                f"members automatically.")
                        for n, q in prep.quotas.items():
                            if q.short_history:
                                st.info(f"**{n}** — {q.note}")
                        for w in prep.warnings:
                            st.warning(w)

    st.divider()
    st.subheader("Recent rounds")
    with storage.connect() as conn:
        rounds = storage.list_rounds(conn, 15)
        carry = storage.pending_carry(conn)
    if carry:
        st.info(f"{money(carry)} is carried forward into the next round's skat pot.")
    if rounds:
        st.dataframe(pd.DataFrame([{
            "Round": r["round_no"], "Date": r["played_on"],
            "Course": COURSES.get(r["course"], r["course"]),
            "Players": r["n_posted"], "Status": r["status"],
        } for r in rounds]), hide_index=True, width='stretch')


# ==========================================================================
# ENTER SCORES
# ==========================================================================
with tab_scores:
    st.subheader("Enter the results")

    with storage.connect() as conn:
        rounds = storage.list_rounds(conn, 30)
    if not rounds:
        st.info("No rounds yet. Set one up on the **Today** tab.")
    else:
        labels = {
            f"Round {r['round_no'] or '?'} — {r['played_on']} — "
            f"{COURSES.get(r['course'], r['course'])} ({r['status']})": r["round_id"]
            for r in rounds
        }
        default = 0
        if st.session_state.get("round_id") in labels.values():
            default = list(labels.values()).index(st.session_state["round_id"])
        picked = st.selectbox("Which round?", list(labels), index=default)
        rid = labels[picked]

        with storage.connect() as conn:
            rnd = storage.get_round(conn, rid)
            entries = storage.load_entries(conn, rid)

        if rnd["status"] == "posted":
            st.success(
                f"**POSTED** — Round {rnd['round_no'] or rnd['round_id']}, "
                f"{rnd['played_on']}. The money is settled and standings include it. "
                "The Post button below is locked; tick the box to re-post only if you "
                "need to correct something."
            )
        elif rnd["status"] == "draft":
            st.info(
                f"**NOT POSTED YET** — Round {rnd['round_no'] or rnd['round_id']}, "
                f"{rnd['played_on']}. Nothing counts toward money, quotas or "
                "standings until you post it."
            )

        # A round can be rained off after the scoresheet is printed. Nothing bad
        # happens if it's just left alone, but tidying it keeps this list short.
        if rnd["status"] == "draft":
            with storage.connect() as conn:
                scored = conn.execute(
                    """SELECT COUNT(*) c FROM entries WHERE round_id = ?
                       AND points_front IS NOT NULL""", (rid,)).fetchone()["c"]
            if scored == 0:
                with st.expander("Round cancelled? (rained off, not enough players)"):
                    st.caption(
                        "Leaving it alone is harmless — an unplayed round is ignored "
                        "by quotas, standings and money, and any pot carried over is "
                        "untouched. Removing it just keeps this list tidy."
                    )
                    _ok_cancel = require_admin("Removing a round", "cancel")
                    if st.button("Remove this round", key=f"cancel_{rid}", disabled=not _ok_cancel):
                        with storage.connect() as conn:
                            conn.execute("DELETE FROM rounds WHERE round_id = ?", (rid,))
                        st.success("Removed.")
                        st.rerun()

        # optional AI pre-fill
        try:
            from cartel import vision
            vision_ok = vision.available()
        except Exception:
            vision_ok = False

        if vision_ok:
            with st.expander("Pre-fill from a photo of the paper sheet (optional)"):
                st.caption(
                    f"Reads a photo or scan and fills the grid below, then checks it "
                    f"against basic arithmetic. **You still confirm every row before "
                    f"posting** — handwritten 7s and 9s are not worth trusting blind. "
                    f"Uses {vision.MODEL}; about a penny a round."
                )
                shot = st.file_uploader("Photo or scan", type=["png", "jpg", "jpeg", "pdf"],
                                        key="vis_up")
                if shot is not None and st.button("Read it"):
                    with st.spinner("Reading the sheet..."):
                        try:
                            read = vision.read_scoresheet(
                                save_upload(shot),
                                expected_names=[e["name"] for e in entries])
                        except Exception as exc:
                            st.error(f"Couldn't read it: {exc}")
                        else:
                            st.session_state["prefill"] = {
                                r.name: r for r in read.rows}
                            st.success(
                                f"Read {len(read.rows)} rows. "
                                f"{read.review_count} need a closer look.")
                            for w in read.warnings:
                                st.warning(w)
                            if read.notes:
                                st.info(read.notes)
        else:
            st.caption(
                "Scores are entered by hand. Photo pre-fill is switched off in "
                "the house rules (photo_prefill_enabled in cartel/config.py)."
            )

        prefill = st.session_state.get("prefill", {})
        grid = pd.DataFrame([{
            "Team": e["team_no"],
            "Player": e["name"],
            "Quota": "guest" if e["is_guest"] else str(e["quota"]),
            "Front": _pf(prefill, e, "points_front"),
            "Back": _pf(prefill, e, "points_back"),
            "Score": _pf(prefill, e, "score"),
            "Greens": _pf(prefill, e, "greens") or 0,
            "Skins": _pf(prefill, e, "skins") or 0,
            "Played": _played(prefill, e),
        } for e in entries]) if entries else pd.DataFrame()

        if grid.empty:
            st.warning("No players on this round.")
        else:
            st.caption(
                "Untick **Played** only for a genuine no-show — they come out of every "
                "pot and out of their team's quota. Greens and Skins are counts; the "
                "two added together are that player's skats. A **guest** pays "
                f"{money(RULES.guest_ante)} into the skat pot and plays for greens and "
                "skins only — but **enter their points anyway**. They don't count "
                "toward the team, and they're what earns the guest a quota: at "
                f"{RULES.guest_min_rounds} scored rounds they become a full member "
                "automatically."
            )
            with st.expander("Somebody turned up who isn't on the sheet"):
                st.caption(
                    "Adds them to this round with the quota they'd have had on the "
                    "day. Their team can be changed in the grid afterwards, like "
                    "anyone else's."
                )
                with storage.connect() as conn:
                    on_sheet = {e["name"] for e in storage.load_entries(conn, rid)}
                    spare = [r["name"] for r in storage.all_members(conn)
                             if r["name"] not in on_sheet]
                a1, a2, a3 = st.columns([3, 1, 1])
                who_extra = a1.selectbox("Player", spare, key=f"add_who_{rid}") \
                    if spare else None
                team_extra = a2.number_input("Team", 1, 12, 1, key=f"add_team_{rid}")
                if a3.button("Add", key=f"add_go_{rid}", disabled=not who_extra):
                    with storage.connect() as conn:
                        q = stats.current_quotas(
                            conn, before=rnd["played_on"], names=[who_extra])[who_extra]
                        storage.add_entry(conn, rid, who_extra, int(team_extra),
                                          q.quota, q.is_guest)
                    st.success(
                        f"{who_extra} added to team {int(team_extra)} "
                        f"({'guest' if q.is_guest else f'quota {q.quota}'}).")
                    st.rerun()
                if not spare:
                    st.caption("Everyone on the roster is already in this round.")

            edited = st.data_editor(
                grid, hide_index=True, width='stretch',
                disabled=["Player", "Quota"],
                column_config={
                    "Team": st.column_config.NumberColumn(
                        min_value=1, max_value=12, step=1,
                        help="Change this to move somebody to another team — for "
                             "the man who missed his tee time and went out with a "
                             "later group. The team quota follows automatically."),
                    "Front": st.column_config.NumberColumn(min_value=0, max_value=40, step=1),
                    "Back": st.column_config.NumberColumn(min_value=0, max_value=40, step=1),
                    "Score": st.column_config.NumberColumn(min_value=50, max_value=140, step=1),
                    "Greens": st.column_config.NumberColumn(
                        min_value=scoring.HARD_LIMITS["greens"][0],
                        max_value=scoring.HARD_LIMITS["greens"][1], step=1,
                        help="Hard limit — nothing above has ever been recorded."),
                    "Skins": st.column_config.NumberColumn(
                        min_value=scoring.HARD_LIMITS["skins"][0],
                        max_value=scoring.HARD_LIMITS["skins"][1], step=1,
                        help="Hard limit — nothing above has ever been recorded."),
                    "Played": st.column_config.CheckboxColumn(),
                },
                key=f"grid_{rid}",
            )

            payload = []
            for _, r in edited.iterrows():
                played = bool(r["Played"])
                payload.append({
                    "name": r["Player"],
                    "team_no": int(r["Team"]),
                    "played": played,
                    "points_front": int(r["Front"]) if played and pd.notna(r["Front"]) else None,
                    "points_back": int(r["Back"]) if played and pd.notna(r["Back"]) else None,
                    "score": int(r["Score"]) if played and pd.notna(r["Score"]) else None,
                    "greens": int(r["Greens"] or 0) if played else 0,
                    "skins": int(r["Skins"] or 0) if played else 0,
                })

            # The day's paperwork, available whenever the round is posted -
            # not just in the moment after pressing Post.
            if rnd["status"] == "posted":
                with st.expander("📄 Reports for this round", expanded=True):
                    st.caption(
                        "Regenerated from what's stored. Nothing is changed by "
                        "opening these."
                    )
                    try:
                        paths = pipeline.rebuild_reports(rid, out_dir=str(OUT))
                    except Exception as exc:
                        st.error(f"Couldn't build the reports: {exc}")
                    else:
                        r1, r2, r3 = st.columns(3)
                        with r1:
                            download(paths["round_pdf"],
                                     "⬇ Results for the day", f"rr_{rid}")
                        with r2:
                            download(paths["ytd_pdf"],
                                     "⬇ Year-to-date stats", f"ry_{rid}")
                        with r3:
                            download(paths["workbook"],
                                     "⬇ Excel workbook", f"rw_{rid}")
                        st.caption(f"Saved to `{OUT.resolve()}`")

            already_posted = rnd["status"] == "posted"
            unlock = True
            if already_posted:
                unlock = st.checkbox(
                    "Let me re-post this round (only needed to correct a mistake)",
                    key=f"unlock_{rid}",
                )

            c1, c2 = st.columns(2)
            preview_clicked = c1.button("Work out the money", width='stretch')
            if not current_user():
                st.info("Pick your name at the top before posting.")
            post_clicked = c2.button(
                "Already posted" if (already_posted and not unlock) else "Post the round",
                type="primary", width='stretch',
                disabled=already_posted and not unlock,
                help=("This round is already posted. Tick the box above if you need "
                      "to correct it." if already_posted and not unlock else None),
            )

            queries = scoring.check_entries([
                scoring.PlayerEntry(
                    name=r["name"], team_no=0, quota=None if r["greens"] is None else 1,
                    is_guest=False, played=r["played"],
                    points_front=r["points_front"], points_back=r["points_back"],
                    score=r["score"], greens=r["greens"], skins=r["skins"])
                for r in payload
            ]) if payload else []

            acknowledged = True
            if queries:
                st.warning(
                    f"**{len(queries)} entr{'y' if len(queries) == 1 else 'ies'} "
                    f"worth a second look.** None of these is impossible, so nothing "
                    f"is blocked — but each one has caught a typo before."
                )
                st.dataframe(pd.DataFrame([{
                    "Player": q.name, "Field": q.field, "Entered": q.value,
                    "Why it's flagged": q.message,
                } for q in queries]), hide_index=True, width='stretch')
                acknowledged = st.checkbox(
                    "I've checked these against the paper — they're right",
                    key=f"ack_{rid}")
                if not acknowledged:
                    st.caption(
                        "Tick the box to post. You can still **Work out the money** "
                        "to see what these figures would produce.")

            if post_clicked and not current_user():
                st.error("Pick your name at the top first — a posted round records "
                         "who posted it.")
                post_clicked = False

            if post_clicked and queries and not acknowledged:
                st.error("Check the flagged entries first, then tick the box.")
                post_clicked = False

            if preview_clicked or post_clicked:
                try:
                    out = pipeline.settle_round(rid, payload, out_dir=str(OUT),
                                                post=post_clicked)
                except Exception as exc:
                    st.error(str(exc))
                else:
                    res = out["result"]
                    st.session_state.pop("prefill", None)
                    if not post_clicked:
                        st.warning(
                            "### This round is NOT posted yet\n\n"
                            "These are the figures it *would* settle to. Nothing has "
                            "been recorded — the standings, the quotas and the money "
                            "are all unchanged.\n\n"
                            "**Press *Post the round* below when you're happy.**"
                        )
                    if post_clicked:
                        with storage.connect() as conn:
                            conn.execute(
                                "UPDATE rounds SET posted_by = ? WHERE round_id = ?",
                                (current_user(), rid))
                            storage.log_activity(
                                conn, current_user(), "posted round",
                                f"R{rnd['round_no'] or rid} on {rnd['played_on']}, "
                                f"{res.n_players} players, "
                                f"{money(res.total_collected)} in")
                        # State the effect, not the intention. "Standings are
                        # updated" is a claim; the season total before and after
                        # is evidence, and it makes a round that did not land
                        # obvious instead of something to be puzzled over later.
                        played = date.fromisoformat(rnd["played_on"])
                        with storage.connect() as conn:
                            after_yr = stats.year_summary(conn, played.year)
                            rnd_now = storage.get_round(conn, rid)
                        st.success(
                            f"**Posted.** {played.strftime('%d %b %Y')} on "
                            f"{COURSES.get(rnd_now['course'], rnd_now['course'])} is "
                            f"now one of **{after_yr['rounds']} rounds** in "
                            f"{played.year}, and the season pot reads "
                            f"**{money(after_yr['collected'])}**."
                        )
                        if played.year != date.today().year:
                            st.warning(
                                f"This round is dated **{rnd['played_on']}**, which is not "
                                f"the current year. It will not appear in the "
                                f"{date.today().year} standings. If the date is wrong, "
                                f"the tee sheet was misread — tell Claude."
                            )

                    k1, k2, k3, k4 = st.columns(4)
                    k1.metric("Players", res.n_players,
                              delta=f"{res.n_guests} guest" if res.n_guests else None,
                              delta_color="off")
                    k2.metric("In the pot", money(res.total_collected))
                    k3.metric("Per side", money(res.pot_per_side),
                              help=f"${res.stake.team_per_side:,.2f} x {res.n_team_players} "
                                   f"member(s) eligible for team play")
                    k4.metric("Per skat", money(res.skat_value),
                              help=f"{res.total_skats} skats out of {money(res.skat_pot)} "
                                   f"(${res.stake.skat_per_member:,.2f} from each member, "
                                   f"${res.stake.guest_ante:,.2f} from each guest, "
                                   f"{res.n_players} player(s), guests included)")

                    st.markdown("**Team play**")
                    st.dataframe(pd.DataFrame([{
                        "Side": s.side.title(), "Team": s.team_no,
                        "Points": s.points, "Xi": f"{s.quota:.1f}", "Net": f"{s.net:+.1f}",
                        "Result": "WIN" if s.is_winner else "",
                        "Per player": money(s.payout_per_player) if s.is_winner else "",
                        "Players": ", ".join(s.players),
                    } for s in res.sides]), hide_index=True, width='stretch')

                    st.markdown("**Settlement**")
                    st.dataframe(pd.DataFrame([{
                        "Player": p.name + (" (guest)" if p.is_guest else ""),
                        "Team": p.team_no,
                        "Greens": p.greens, "Skins": p.skins,
                        "In": round(p.ante, 2),
                        "Team $": round(p.team_money, 2), "Skat $": round(p.skat_money, 2),
                        "Won": round(p.total, 2), "Net": round(p.net, 2),
                    } for p in sorted(res.payouts.values(), key=lambda x: -x.total)]),
                        hide_index=True, width='stretch')

                    for w in res.warnings:
                        st.warning(w)
                    if not post_clicked:
                        st.info(
                            "Still not posted. Scroll up and press **Post the round** "
                            "to record it."
                        )
                    if res.carried_money:
                        st.info(f"{money(res.carried_money)} carried to the next round.")

                    if out["round_pdf"]:
                        download(out["round_pdf"], "⬇ Results PDF", "dl_res")
                    if post_clicked:
                        download(out["ytd_pdf"], "⬇ Year-to-date stats PDF", "dl_ytd")
                        download(out["workbook"], "⬇ Excel workbook", "dl_xl")
                        b = out.get("backup")
                        if b is not None and b.ok:
                            st.caption(f"💾 Backed up to `{b.path}`")
                        elif b is not None:
                            st.warning(
                                f"The round is posted, but the backup didn't run: "
                                f"{b.skipped}. Copy `data\\cartel.db` somewhere safe.")


# ==========================================================================
# STANDINGS
# ==========================================================================
with tab_standings:
    year = st.selectbox("Year", list(range(date.today().year, 2021, -1)), key="yr")
    with storage.connect() as conn:
        ytd = stats.year_to_date(conn, year)
        boards = stats.leaderboards(conn, year)
        rec = stats.house_reconciliation(conn, year)
        yr = stats.year_summary(conn, year)

    # All four cover the WHOLE year. They used to be mixed: rounds and money
    # counted only what this app had settled, while players and skats counted
    # the full year - so two rounds and $590 sat next to a year's worth of skats.
    active = ytd[ytd["Rds"] > 0]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Rounds", yr["rounds"],
              help=f"Rounds completed in {year}, imported history included")
    c2.metric("Players in the mix", len(active))
    c3.metric("Money through the pot", money(yr["collected"]),
              help=f"{yr['player_rounds']:,} player-rounds in {year} at the house "
                   f"rate. Includes rounds imported from the old system.")
    c4.metric("Skats won", int(active["Skats"].sum()))

    if rec["rounds"]:
        st.caption(
            f"Of that, **{rec['rounds']} round(s)** were settled in this app "
            f"({money(rec['collected'])} in, {money(rec['paid_out'])} out, "
            f"{money(rec['unaccounted'])} unaccounted). The rest is imported "
            f"history, where the money came from the group's YTD Winnings sheet."
        )

    # A-Z, matching the printed report. Rank carries the standing instead.
    show = active[["Name", "Rank", "Pts", "Guest", "Tee", "Rds", "Rds_Window",
                   "Team$", "Skat$", "Won$", "$/Rd", "Greens", "Grns/Rd",
                   "Skins", "Skins/Rd", "Skats", "Skats/Rd"]].copy().sort_values("Name")
    show["Pts"] = show.apply(
        lambda r: "guest" if r["Guest"] else
        ("" if pd.isna(r["Pts"]) else f"{int(r['Pts'])}"), axis=1)
    show = show.drop(columns=["Guest"])
    # Formatting via Streamlit's own column config rather than a pandas Styler.
    # Styler.background_gradient quietly requires matplotlib, which is a 50 MB
    # dependency for one shaded column - and it is installed often enough as a
    # side effect elsewhere that the breakage only shows up on a clean machine.
    top = float(show["Won$"].max()) if len(show) else 0.0
    st.dataframe(
        show, hide_index=True, width='stretch', height=520,
        column_config={
            "Rank": st.column_config.NumberColumn(
                "Rank", format="%d",
                help=f"Position on $ won per round played, 1 = highest. Needs at "
                     f"least {RULES.rank_min_rounds} rounds in the last "
                     f"{RULES.rank_window_months} months; blank means not yet. "
                     f"Players level on $/Rd share a rank."),
            "Rds_Window": st.column_config.NumberColumn(
                f"Rds/{RULES.rank_window_months}mo", format="%d",
                help=f"Rounds in the {RULES.rank_window_months} months to the last "
                     f"round on file. {RULES.rank_min_rounds} or more to be ranked."),
            "Won$": st.column_config.ProgressColumn(
                "Won $", format="dollar", min_value=0.0,
                max_value=max(top, 1.0),
                help="Team money plus skat money for the year"),
            "Team$": st.column_config.NumberColumn("Team $", format="dollar"),
            "Skat$": st.column_config.NumberColumn("Skat $", format="dollar"),
            "$/Rd": st.column_config.NumberColumn("$/Rd", format="dollar"),
            "Rds": st.column_config.NumberColumn("Rds", format="%d"),
            "Greens": st.column_config.NumberColumn("Grns", format="%d"),
            "Skins": st.column_config.NumberColumn("Skins", format="%d"),
            "Skats": st.column_config.NumberColumn("Skats", format="%d"),
            "Grns/Rd": st.column_config.NumberColumn("Grns/Rd", format="%.2f"),
            "Skins/Rd": st.column_config.NumberColumn("Skins/Rd", format="%.2f"),
            "Skats/Rd": st.column_config.NumberColumn("Skats/Rd", format="%.2f"),
        },
    )
    st.caption(
        f"Listed A-Z. **Rank** is by $ per round, 1 = highest, and needs at least "
        f"{RULES.rank_min_rounds} rounds in the last {RULES.rank_window_months} "
        f"months — click any column heading to re-sort. "
        f"Money is the opening balance seeded from the group's YTD Winnings sheet "
        f"({money(active['Seed_Team$'].sum() + active['Seed_Skat$'].sum())}) plus "
        f"{money(active['Posted_Team$'].sum() + active['Posted_Skat$'].sum())} settled "
        f"in this app since. Rounds, greens and skins are counted from the full points "
        f"history."
    )

    st.subheader(f"{year} season awards")
    with storage.connect() as conn:
        season = stats.season_awards(conn, year)
    if season["as_of"]:
        st.caption(
            f"**As if the season ended today** — everything below is worked out "
            f"from rounds already settled, up to and including Round "
            f"{season['as_of_round']} on {season['as_of']} "
            f"({season['rounds']} rounds so far). It moves as rounds are posted."
        )
    cols = st.columns(3)
    for i, (title, block) in enumerate(season["awards"].items()):
        with cols[i % 3]:
            st.markdown(f"**{title}**")
            if block["table"].empty:
                st.caption("Nothing to show yet.")
            else:
                st.dataframe(block["table"], hide_index=True, width='stretch')
            st.caption(block["blurb"])

    with storage.connect() as conn:
        tag = storage.anchor_tag(conn)
    OUT.mkdir(parents=True, exist_ok=True)
    st_csv = OUT / f"Cartel_Standings_{year}_{tag}.csv"
    st_pdf = OUT / f"Cartel_Standings_{year}_{tag}.pdf"
    try:
        active.to_csv(st_csv, index=False)
        with storage.connect() as conn:
            reports.standings_report(str(st_pdf), conn, year)
        standings_note = f"Saved to `{OUT.resolve()}`"
    except Exception as exc:
        standings_note = f"Could not save to the out folder ({exc})"

    d1, d2 = st.columns(2)
    with d1:
        st.download_button("⬇ Standings as CSV", active.to_csv(index=False).encode(),
                           file_name=st_csv.name, width='stretch')
    with d2:
        if st_pdf.exists():
            st.download_button("⬇ Standings as PDF (for printing)",
                               st_pdf.read_bytes(), file_name=st_pdf.name,
                               mime="application/pdf", width='stretch')
    st.caption(standings_note)


# ==========================================================================
# PLAYER
# ==========================================================================
with tab_player:
    st.subheader("One player, everything")
    with storage.connect() as conn:
        roster = [r["name"] for r in storage.all_members(conn)]
        yrs = db.read_sql(
            "SELECT DISTINCT substr(played_on,1,4) AS \"Year\" FROM v_player_rounds "
            "ORDER BY 1 DESC", conn)
    years = [int(y) for y in yrs["Year"]] or [date.today().year]

    pc1, pc2 = st.columns([2, 1])
    who = pc1.selectbox("Player", roster, key="pl_who") if roster else None
    p_year = pc2.selectbox("Year", years, key="pl_year")

    if who:
        with storage.connect() as conn:
            card = stats.player_card(conn, who, int(p_year))
        s_ = card["summary"]

        if not s_ or not s_.get("Rds"):
            st.info(f"{who} hasn't played a round in {p_year}.")
        else:
            k = st.columns(5)
            k[0].metric("Quota", "guest" if s_.get("Guest") else
                        (f"{int(s_['Pts'])}" if pd.notna(s_.get("Pts")) else "—"))
            k[1].metric("Rounds", int(s_["Rds"]))
            k[2].metric("Won", money(s_["Won$"]))
            k[3].metric("Per round", money(s_["$/Rd"]))
            k[4].metric("Rank", "—" if pd.isna(s_.get("Rank")) else int(s_["Rank"]),
                        help=f"By $ per round. Needs {RULES.rank_min_rounds}+ rounds "
                             f"in the last {RULES.rank_window_months} months.")

            g1, g2 = st.columns(2)
            with g1:
                st.markdown("**Quota through the year**")
                trend = card["quota_trend"]
                if len(trend) > 1 and trend["Quota"].notna().any():
                    st.line_chart(trend.set_index("Date")["Quota"], height=220)
                else:
                    st.caption("Not enough rounds yet to show a trend.")
            with g2:
                st.markdown("**Skats**")
                st.caption(
                    f"{int(s_['Greens'])} green(s) and {int(s_['Skins'])} skin(s) "
                    f"in {int(s_['Rds'])} round(s) — "
                    f"{s_['Skats/Rd']:.2f} per round.")
                st.caption(
                    f"Money: {money(s_['Team$'])} from team play, "
                    f"{money(s_['Skat$'])} from greens and skins.")

            st.markdown(f"**Every {p_year} round**")
            st.dataframe(card["rounds"], hide_index=True, width='stretch',
                         column_config={
                             "Team $": st.column_config.NumberColumn(format="dollar"),
                             "Skat $": st.column_config.NumberColumn(format="dollar"),
                             "Won $": st.column_config.NumberColumn(format="dollar"),
                         })

            st.markdown("**What the next quota rests on**")
            st.caption(
                f"The most recent {RULES.quota_window} scored rounds, whatever year "
                f"they fall in — a quota is not a calendar thing.")
            st.dataframe(card["basis"], hide_index=True, width='stretch')

            st.download_button(
                f"⬇ {who}'s {p_year} rounds as CSV",
                card["rounds"].to_csv(index=False).encode(),
                file_name=f"Player_{who.replace(' ', '_')}_{p_year}.csv")


# ==========================================================================
# NEXT QUOTA — the five rounds each quota rests on
# ==========================================================================
with tab_quota:
    st.caption(
        f"The most recent {RULES.quota_window} scored rounds behind each player's "
        "next quota. Same rounds, same order the app itself uses — if something "
        "here looks wrong, the quota is wrong too."
    )

    with storage.connect() as conn:
        members = [r["name"] for r in storage.all_members(conn)]

    who = st.multiselect("Players (leave empty for everyone)", members,
                         key="qb_who")
    with storage.connect() as conn:
        basis = stats.quota_basis(conn, names=who or None)

    if basis.empty:
        st.info("No scored rounds on file yet.")
    else:
        st.dataframe(
            basis.style.apply(
                lambda r: ["font-weight: bold" if str(r["Date"]).startswith(("Average", "Quota"))
                           else "" for _ in r],
                axis=1),
            hide_index=True, width='stretch',
            height=min(900, 40 + 35 * len(basis)),
        )
        # Written to the out folder as well as offered as a download, and named
        # the same way as every other artifact. A report that only exists inside
        # the browser is one you can't find again a week later.
        # Named after the last SETTLED round, not today. The report describes the
        # state of the data, so regenerating it on a different day with no golf
        # in between must produce the same filename, not a third copy.
        with storage.connect() as conn:
            tag = storage.anchor_tag(conn)
        OUT.mkdir(parents=True, exist_ok=True)
        csv_path = OUT / f"Next_Quota_{tag}.csv"
        pdf_path = OUT / f"Next_Quota_{tag}.pdf"
        try:
            basis.to_csv(csv_path, index=False)
            with storage.connect() as conn:
                reports.quota_basis_report(str(pdf_path), conn, names=who or None)
            saved = f"Saved to `{OUT.resolve()}`"
        except Exception as exc:
            saved = f"Could not save to the out folder ({exc})"

        c1, c2 = st.columns(2)
        with c1:
            st.download_button(
                "⬇ Download as CSV",
                basis.to_csv(index=False).encode("utf-8"),
                file_name=csv_path.name, mime="text/csv", width='stretch')
        with c2:
            if pdf_path.exists():
                st.download_button(
                    "⬇ Download as PDF (for printing)",
                    pdf_path.read_bytes(), file_name=pdf_path.name,
                    mime="application/pdf", width='stretch')
        st.caption(saved)
        st.caption(
            "A player short of a full window still gets a quota from what they "
            "have; one below the minimum rounds plays as a guest and shows "
            "\u201cguest\u201d on the Quota row."
        )



# ==========================================================================
# ROSTER  (the app screen; the Excel worksheet is called "Membership")
# ==========================================================================
with tab_roster:
    st.subheader("Roster")
    st.caption(
        f"Tee is which set of tees they play. Anyone with fewer than "
        f"{RULES.guest_min_rounds} scored rounds on file plays as a guest, and becomes "
        f"a full member **automatically** on their next round once they have "
        f"{RULES.guest_min_rounds} — nothing to press. *Quota so far* just shows their "
        f"progress toward that. Manual quota is a separate override for fixing a "
        f"number by hand; leave it blank and the rolling average does the work."
    )
    with storage.connect() as conn:
        members = storage.all_members(conn)
        quotas = stats.current_quotas(conn)

    frame = pd.DataFrame([{
        "Name": m["name"], "Tee": m["tee"], "Active": bool(m["active"]),
        "Quota": quotas[m["name"]].display,
        "Rounds on file": quotas[m["name"]].rounds_available,
        "Used in average": quotas[m["name"]].rounds_used,
        "Quota so far": quotas[m["name"]].provisional_quota,
        "Manual quota": m["manual_quota"],
        "Note": quotas[m["name"]].note,
    } for m in members])

    edited = st.data_editor(
        frame, hide_index=True, width='stretch', height=520,
        disabled=["Name", "Quota", "Rounds on file", "Used in average",
                  "Quota so far", "Note"],
        column_config={
            "Tee": st.column_config.SelectboxColumn(options=list(TEE_CODES)),
            "Manual quota": st.column_config.NumberColumn(min_value=0, max_value=60, step=1),
        },
        key="members_grid",
    )

    _ok_roster = require_admin("Editing the roster", "roster")
    if st.button("Save roster changes", type="primary", disabled=not _ok_roster):
        with storage.connect() as conn:
            for _, r in edited.iterrows():
                mq = int(r["Manual quota"]) if pd.notna(r["Manual quota"]) else None
                storage.upsert_member(conn, r["Name"], r["Tee"], bool(r["Active"]), mq)
        st.success("Saved.")
        st.rerun()

    st.divider()
    with st.form("add_member"):
        st.markdown("**Add a member**")
        c1, c2, c3 = st.columns([2, 1, 1])
        name = c1.text_input("Name")
        tee = c2.selectbox("Tee", list(TEE_CODES))
        mq = c3.number_input("Manual quota (0 = none)", 0, 60, 0,
                             help="Leave at 0 to let the rolling average decide. With "
                                  "no history they'll play as a guest until they have "
                                  f"{RULES.guest_min_rounds} rounds.")
        if st.form_submit_button("Add") and name.strip():
            with storage.connect() as conn:
                storage.upsert_member(conn, name.strip(), tee, True,
                                      int(mq) if mq else None)
            st.success(f"Added {name.strip()}.")
            st.rerun()


# ==========================================================================
# HEALTH
# ==========================================================================
with tab_health:
    st.subheader("Data health")
    year = st.selectbox("Year", list(range(date.today().year, 2021, -1)), key="hyr")
    with storage.connect() as conn:
        rec = stats.house_reconciliation(conn, year)
        yr = stats.year_summary(conn, year)
        ytd_all = stats.year_to_date(conn, year)
        rows = db.read_sql(
            """SELECT r.round_no AS "Round", r.played_on AS "Date", r.course AS "Course",
                      COUNT(e.name) AS "Players",
                      SUM(CASE WHEN e.points_front IS NULL THEN 1 ELSE 0 END) AS "NoShows",
                      r.status AS "Status", r.carried_out AS "Carried"
               FROM rounds r LEFT JOIN entries e ON e.round_id=r.round_id
               WHERE r.played_on >= ? AND r.played_on < ?
               GROUP BY r.round_id ORDER BY r.played_on DESC""",
            conn, params=[f"{year}-01-01", f"{year+1}-01-01"])
        odd_course = db.read_sql(
            "SELECT round_no, played_on, course FROM rounds WHERE course NOT IN ('N','S')",
            conn)
        inactive = db.read_sql(
            """SELECT m.name, COUNT(e.name) AS "rounds" FROM members m
               LEFT JOIN entries e ON e.name=m.name
               WHERE m.active=0 GROUP BY m.name HAVING COUNT(e.name) > 0
               ORDER BY rounds DESC""", conn)

    # Two scopes, each labelled. Showing the app-settled figures alone put $590
    # here next to $6,830 on Standings, which reads as a discrepancy and isn't.
    st.markdown(f"**The {year} season — everything, imported history included**")
    y1, y2, y3 = st.columns(3)
    paid_all = float(ytd_all["Won$"].sum())
    y1.metric("Collected", money(yr["collected"]),
              help=f"{yr['player_rounds']:,} player-rounds across {yr['rounds']} rounds")
    y2.metric("Paid out", money(paid_all),
              help="Seeded opening balances plus everything settled here")
    y3.metric("Difference", money(yr["collected"] - paid_all),
              delta=None if abs(yr["collected"] - paid_all) < 0.01 else "see below",
              delta_color="off")

    gap = yr["collected"] - paid_all - yr["written_off"]
    if yr["written_off"]:
        st.caption(
            f"Includes {money(yr['written_off'])} written off — money collected in "
            f"{year} that could never be paid out."
        )
    if abs(gap) >= 0.01:
        st.info(
            f"**{money(gap)} of {year} money was collected but never paid out.** "
            f"It comes from the imported history, where the old system recorded no "
            f"winning team on some sides, so a side pot was never distributed. "
            f"Nothing in this app caused it and nothing here can recover it."
        )
        with st.expander("Write it off, so the year balances"):
            st.caption(
                "Records it as an explicit line: money collected that nobody won. "
                "The year then reads zero difference, with the write-off shown "
                "beside it.\n\n"
                "**It is not shared out among players.** Nobody actually won this "
                "money — the old system simply failed to record who took the side. "
                "Crediting it to whoever had the most skins would put figures in "
                "the standings and the Order of Merit that never happened, and "
                "would put the app at odds with your own YTD Winnings sheet."
            )
            wo_reason = st.text_input(
                "Reason", key=f"wo_reason_{year}",
                value=f"Legacy shortfall: sides with no winning team recorded in the "
                      f"old system before {year} data was imported. Unrecoverable.")
            _ok_writeoff = require_admin("Writing money off", "writeoff")
            if st.button(f"Write off {money(gap)}", key=f"wo_go_{year}",
                         disabled=not _ok_writeoff):
                with storage.connect() as conn:
                    storage.write_off(conn, year, gap, wo_reason,
                                      date.today().isoformat())
                st.success(f"{money(gap)} written off. The year now balances.")
                st.rerun()

    with storage.connect() as conn:
        wo_hist = [dict(r) for r in storage.writeoff_history(conn, year)]
    if wo_hist:
        st.caption("Written off:")
        st.dataframe(pd.DataFrame([{
            "Recorded": h["recorded_on"], "Amount": money(h["amount"]),
            "Reason": h["reason"] or "",
        } for h in wo_hist]), hide_index=True, width='stretch')

    st.markdown("**Of that, the rounds settled in this app**")
    st.caption(
        "Every dollar collected in a round settled here must be paid out or "
        "explicitly carried. Imported rounds are excluded: their money came from "
        "the seeded ledger, not from a settlement."
    )
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Collected", money(rec["collected"]))
    c2.metric("Paid out", money(rec["paid_out"]))
    c3.metric("Carried", money(rec["carried"]))
    c4.metric("Unaccounted", money(rec["unaccounted"]),
              delta=None if abs(rec["unaccounted"]) < 0.01 else "needs a look",
              delta_color="inverse")

    if abs(rec["unaccounted"]) >= 0.01:
        st.warning(
            f"{money(rec['unaccounted'])} was collected in a round settled here but "
            f"never paid out or carried. That shouldn't happen — the engine asserts "
            f"against it — so please report it."
        )

    if not odd_course.empty:
        st.error("Rounds with a course that isn't North or South:")
        st.dataframe(odd_course, hide_index=True, width='stretch')

    if not inactive.empty:
        st.warning(
            "These names have rounds on file but aren't on the active roster. A variant "
            "spelling of an existing member splits their history and gives them the "
            "wrong quota — see scripts/merge_names.py."
        )
        st.dataframe(inactive, hide_index=True, width='stretch')

    with storage.connect() as conn:
        guesty = db.read_sql(
            """SELECT m.name AS "Name", COUNT(v.round_id) AS "Rounds on file"
               FROM members m LEFT JOIN v_player_rounds v ON v.name = m.name
               WHERE m.active = 1 GROUP BY m.name
               HAVING COUNT(v.round_id) < ? ORDER BY 2 DESC, 1""",
            conn, params=[RULES.guest_min_rounds])
        seed_total = db.read_sql(
            """SELECT year AS "Year", COUNT(*) AS "Members",
                      SUM(team_money) AS "Seed Team$", SUM(skat_money) AS "Seed Skat$"
               FROM ledger_seed GROUP BY year ORDER BY year DESC""", conn)

    if not guesty.empty:
        st.info(
            f"On the roster but with fewer than {RULES.guest_min_rounds} scored rounds "
            f"on file, so they'd play as guests today:"
        )
        st.dataframe(guesty, hide_index=True, width='stretch')

    with storage.connect() as conn:
        unscored = db.read_sql(
            """SELECT e.name AS "Name", COUNT(*) AS "Rounds played, no points",
                      MAX(r.played_on) AS "Most recent"
               FROM entries e JOIN rounds r ON r.round_id = e.round_id
               WHERE e.is_guest = 1 AND e.played = 1 AND e.points_front IS NULL
                 AND r.status IN ('legacy','posted')
               GROUP BY e.name ORDER BY 2 DESC, 1""", conn)

    if not unscored.empty:
        st.warning(
            "**These guests played but had no points written down.** A round with no "
            f"points can't count toward the {RULES.guest_min_rounds} needed for a "
            "quota, so these players will stay guests however often they turn up. "
            "Nothing to fix retroactively — just record their points from now on and "
            "they'll graduate on their own."
        )
        st.dataframe(unscored, hide_index=True, width='stretch')

    if not seed_total.empty:
        st.markdown("**Seeded opening balances**")
        st.caption("Taken from the group's YTD Winnings sheet. Never recomputed.")
        st.dataframe(seed_total, hide_index=True, width='stretch')

    st.dataframe(rows, hide_index=True, width='stretch')

    with storage.connect() as conn:
        stranded = db.read_sql(
            """SELECT r.round_no AS "Round", r.played_on AS "Date", r.course AS "Course",
                      COUNT(e.name) AS "Players",
                      SUM(CASE WHEN e.points_front IS NOT NULL THEN 1 ELSE 0 END)
                        AS "Scores entered"
               FROM rounds r JOIN entries e ON e.round_id = r.round_id
               WHERE r.status = 'draft'
               GROUP BY r.round_id
               HAVING SUM(CASE WHEN e.points_front IS NOT NULL THEN 1 ELSE 0 END) > 0
               ORDER BY r.played_on DESC""", conn)
        dupes = db.read_sql(
            """SELECT played_on AS "Date", COUNT(*) AS "Rounds"
               FROM rounds GROUP BY played_on HAVING COUNT(*) > 1
               ORDER BY played_on DESC""", conn)

    if not stranded.empty:
        st.error(
            "**Scores entered but never posted.** These rounds have scores on them "
            "and are still drafts, so none of it counts — not the money, not the "
            "quotas, not the standings. Go to **Enter scores**, pick the round, and "
            "press **Post the round**."
        )
        st.dataframe(stranded, hide_index=True, width='stretch')

    if not dupes.empty:
        st.warning(
            "**More than one round on the same date.** Usually the same day "
            "prepared twice — often once under the wrong course. Make sure the "
            "scores are on the one you mean to post, and cancel the other on the "
            "Enter scores tab."
        )
        st.dataframe(dupes, hide_index=True, width='stretch')

    st.divider()
    st.markdown("**The stake**")
    with storage.connect() as conn:
        stake_now = storage.current_stake(conn)
        history = [dict(r) for r in storage.stake_history(conn)]
    st.caption(
        f"In force now: **{stake_now.describe()}**. A member's ante always splits "
        f"a quarter to the front, a quarter to the back and a half to greens and "
        f"skins. A guest's whole ante goes to greens and skins; guests take no "
        f"team money because they have no quota."
    )
    st.caption(
        "A round takes the stake in force when its **scoresheet is prepared**, and "
        "keeps it. Changing this never disturbs a round already prepared or played."
    )

    with st.expander("Change the stake"):
        s1, s2, s3 = st.columns(3)
        new_member = s1.number_input("Member ante", min_value=1.0, max_value=1000.0,
                                     value=float(stake_now.member_ante), step=5.0)
        new_guest = s2.number_input("Guest ante", min_value=1.0, max_value=1000.0,
                                    value=float(stake_now.guest_ante), step=5.0)
        from_date = s3.date_input("In force from", value=date.today())
        note = st.text_input("Note (optional)",
                             placeholder="e.g. agreed at the AGM")

        preview = None
        try:
            preview = Stake(new_member, new_guest)
            preview.validate()
            st.caption(f"That would be: {preview.describe()}")
        except ValueError as exc:
            st.error(str(exc))
            preview = None

        _ok_stake = require_admin("Changing the stake", "stake")
        if st.button("Record this change", type="primary",
                     disabled=preview is None or not _ok_stake):
            with storage.connect() as conn:
                storage.set_stake(conn, new_member, new_guest,
                                  from_date.isoformat(), note or None)
            st.success(f"Stake recorded: {preview.describe()}")
            st.rerun()

    if len(history) > 1:
        st.caption("Every change, most recent first:")
        st.dataframe(pd.DataFrame([{
            "In force from": h["set_on"],
            "Member": money(h["member_ante"]),
            "Guest": money(h["guest_ante"]),
            "Note": h["note"] or "",
        } for h in history]), hide_index=True, width='stretch')

    mixed = None
    with storage.connect() as conn:
        mixed = db.read_sql(
            """SELECT DISTINCT member_ante AS "Member", guest_ante AS "Guest"
               FROM rounds WHERE status IN ('legacy','posted')
                 AND played_on >= ? AND played_on < ?""",
            conn, params=[f"{year}-01-01", f"{year+1}-01-01"])
    if len(mixed) > 1:
        st.warning(
            f"**{year} contains rounds played at more than one stake.** Money won "
            f"and $ per round therefore mix stakes, so the standings rank rewards "
            f"having played the dearer rounds as well as playing well. Worth saying "
            f"out loud when the season is judged."
        )

    st.divider()
    st.markdown("**Who did what**")
    with storage.connect() as conn:
        acts = [dict(a) for a in storage.recent_activity(conn, 60)]
    if acts:
        st.dataframe(pd.DataFrame([{
            "When": a["at"].replace("T", " "),
            "Who": a["who"],
            "Did": a["action"],
            "Detail": a["detail"] or "",
        } for a in acts]), hide_index=True, width='stretch', height=260)
    else:
        st.caption("Nothing recorded yet. Posting a round writes the first entry.")

    st.divider()
    st.markdown("**The admin word**")
    with storage.connect() as conn:
        has_pw = storage.admin_passphrase_set(conn)
    if has_pw:
        st.caption(
            "Set. Changing the stake, editing the roster, removing a round and "
            "writing money off all need it. Everything else — including posting "
            "a round — is open to anyone."
            + ("  \n\n**Unlocked for this session.**"
               if st.session_state.get("admin_ok") else "")
        )
    else:
        st.warning(
            "**No admin word set.** Anyone with the link can change the stake, "
            "edit the roster, remove a round or write money off. Fine while it's "
            "just you; set one before you share the link with the group."
        )
    with st.expander("Set or change the admin word"):
        st.caption(
            "One word the group's organisers share. It is stored scrambled, never "
            "as the word itself, and it is not real security — everyone with the "
            "link is a friend. It stops somebody exploring this tab from changing "
            "the stake for the whole group by accident."
        )
        if has_pw and not st.session_state.get("admin_ok"):
            st.caption("Unlock above first to change it.")
        else:
            new_pw = st.text_input("New admin word", type="password", key="new_pw")
            again = st.text_input("Again", type="password", key="new_pw2")
            if st.button("Save the admin word", disabled=not new_pw):
                if new_pw != again:
                    st.error("Those two don't match.")
                elif len(new_pw) < 4:
                    st.error("A bit longer, please.")
                else:
                    with storage.connect() as conn:
                        storage.set_admin_passphrase(conn, new_pw)
                        storage.log_activity(conn, current_user() or "someone",
                                             "changed the admin word")
                    st.session_state["admin_ok"] = True
                    st.success("Saved.")
                    st.rerun()

    st.divider()
    st.markdown("**Backups**")
    st.caption(
        f"A snapshot is taken automatically every time a round is posted, into "
        f"`{backup.backup_dir()}` — deliberately outside the app folder, so "
        f"installing an update can't touch it. The newest {backup.KEEP} are kept; "
        f"each is about 0.3 MB."
    )
    bks = backup.list_backups()
    bc1, bc2 = st.columns([1, 2])
    with bc1:
        if st.button("Back up now"):
            r = backup.make_backup(reason="manual")
            if r.ok:
                st.success(f"Saved {r.path.name}")
            else:
                st.warning(r.skipped)
            st.rerun()
    with bc2:
        if bks:
            st.caption(f"Most recent: `{bks[0].name}`")
    if bks:
        st.dataframe(pd.DataFrame([{
            "Backup": f.name,
            "Taken": pd.Timestamp(f.stat().st_mtime, unit="s").strftime("%d %b %Y %H:%M"),
            "Size": f"{f.stat().st_size / 1024:,.0f} KB",
        } for f in bks[:15]]), hide_index=True, width='stretch')
    else:
        st.info("No backups yet. One is taken the next time you post a round.")

    st.divider()
    st.markdown("**Files this app has produced**")
    st.caption(f"Everything is written to `{OUT.resolve()}`")
    try:
        files = sorted(OUT.glob("*"), key=lambda f: f.stat().st_mtime, reverse=True)
    except Exception:
        files = []
    if files:
        st.dataframe(pd.DataFrame([{
            "File": f.name,
            "Written": pd.Timestamp(f.stat().st_mtime, unit="s").strftime("%d %b %Y %H:%M"),
            "Size": f"{f.stat().st_size / 1024:,.0f} KB",
        } for f in files[:40]]), hide_index=True, width='stretch')
    else:
        st.info(
            "Nothing in the out folder yet. Scoresheets, results, stats reports and "
            "the workbook all land there as you produce them."
        )

    st.divider()
    st.markdown("**House rules in force**")
    st.json(RULES.as_dict())
