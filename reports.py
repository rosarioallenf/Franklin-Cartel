"""
Output: the after-the-round settlement sheet, the year-to-date member stats
report, and an Excel workbook in the same shape as Golf_Stats.xlsx so the
existing spreadsheet workflow keeps working.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib.styles import ParagraphStyle
from openpyxl.styles import Font
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer,
)

from .config import RULES
from . import db, storage, stats

COURSE_NAMES = {"N": "North", "S": "South"}

H1 = ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=15, alignment=TA_CENTER, spaceAfter=2)
H2 = ParagraphStyle("h2", fontName="Helvetica", fontSize=10, alignment=TA_CENTER,
                    textColor=colors.HexColor("#444"), spaceAfter=10)
H3 = ParagraphStyle("h3", fontName="Helvetica-Bold", fontSize=10.5, spaceBefore=10, spaceAfter=4)
BODY = ParagraphStyle("b", fontName="Helvetica", fontSize=8.5, leading=11)
SMALL = ParagraphStyle("sm", fontName="Helvetica-Oblique", fontSize=7.5,
                       textColor=colors.HexColor("#666"), leading=10)

GRID = [
    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ("FONTSIZE", (0, 0), (-1, 0), 8),
    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#3a3a3a")),
    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
    ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
    ("FONTSIZE", (0, 1), (-1, -1), 8),
    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999")),
    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ("TOPPADDING", (0, 0), (-1, -1), 3),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f4f4")]),
]


def _money(v) -> str:
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


# --------------------------------------------------------------------------
# round settlement
# --------------------------------------------------------------------------

def round_report(path: str, *, round_no, played_on, course, result, entries: dict) -> str:
    """entries: name -> PlayerEntry, used for the detail columns."""
    doc = SimpleDocTemplate(path, pagesize=letter,
                            leftMargin=0.6 * inch, rightMargin=0.6 * inch,
                            topMargin=0.5 * inch, bottomMargin=0.5 * inch,
                            title=f"Round {round_no} Results")
    story = [Paragraph("2026 Cartel &#183; Round Results", H1)]
    field = f"{result.n_players} players"
    if result.n_guests:
        field += f" ({result.n_team_players} playing teams, {result.n_guests} guest)"
    story.append(Paragraph(
        f"Round {round_no} &nbsp;&#183;&nbsp; "
        f"{played_on.strftime('%a, %B')} {played_on.day}, {played_on.year} &nbsp;&#183;&nbsp; "
        f"{COURSE_NAMES.get(course, course)} Course &nbsp;&#183;&nbsp; "
        f"{field} &nbsp;&#183;&nbsp; "
        f"{_money(result.total_collected)} in the pot", H2))

    # ---- sides ----
    story.append(Paragraph("Team play", H3))
    cell = ParagraphStyle("cell", fontName="Helvetica", fontSize=8, leading=9.5)
    cell_b = ParagraphStyle("cellb", fontName="Helvetica-Bold", fontSize=8, leading=9.5)

    data = [["Side", "Team", "Players", "Points", "Quota (Xi)", "Net", "Result", "Per player"]]
    for side in ("front", "back"):
        rows = [s for s in result.sides if s.side == side]
        rows.sort(key=lambda s: -s.net)
        for s in rows:
            data.append([
                side.title(), str(s.team_no),
                Paragraph(", ".join(s.players), cell_b if s.is_winner else cell),
                str(s.points), f"{s.quota:.1f}", f"{s.net:+.1f}",
                "WIN" if s.is_winner else "",
                _money(s.payout_per_player) if s.is_winner else "",
            ])
    t = Table(data, colWidths=[0.45 * inch, 0.4 * inch, 2.7 * inch, 0.5 * inch,
                               0.66 * inch, 0.45 * inch, 0.48 * inch, 0.66 * inch])
    style = list(GRID) + [("ALIGN", (3, 1), (-1, -1), "CENTER")]
    for i, r in enumerate(data[1:], start=1):
        if r[6] == "WIN":
            style += [("FONTNAME", (0, i), (-1, i), "Helvetica-Bold"),
                      ("BACKGROUND", (0, i), (-1, i), colors.HexColor("#dff0d8"))]
    t.setStyle(TableStyle(style))
    story.append(t)
    story.append(Paragraph(
        f"Each side carried {_money(result.pot_per_side)}: "
        f"${result.stake.team_per_side:,.2f} x {result.n_team_players} member(s) eligible for "
        f"team play, so {_money(2 * result.pot_per_side)} of team money in all. Guests "
        f"put in ${result.stake.guest_ante:,.2f} for greens and skins only and are not part of "
        f"any team's points or quota. Played at {result.stake.describe()}.", SMALL))

    # ---- skats ----
    story.append(Paragraph("Greens and skins", H3))
    member_share = result.stake.skat_per_member * result.n_team_players
    guest_share = result.stake.guest_ante * result.n_guests
    breakdown = (f"{_money(member_share)} from {result.n_team_players} member(s)"
                 + (f" plus {_money(guest_share)} from {result.n_guests} guest(s)"
                    if result.n_guests else "")
                 + (f" plus {_money(result.carry_in)} carried in"
                    if result.carry_in else ""))
    story.append(Paragraph(
        f"Skat pot {_money(result.skat_pot)} ({breakdown}) &nbsp;&#183;&nbsp; "
        f"{result.total_skats} skats &nbsp;&#183;&nbsp; "
        f"<b>{_money(result.skat_value)} per skat</b>. A skat is a green or a skin; "
        f"both are worth the same.", BODY))
    story.append(Spacer(1, 4))

    # ---- money ----
    story.append(Paragraph("Settlement", H3))
    data = [["Player", "T", "Quota", "Front", "Back", "Total", "Score",
             "Grn", "Skn", "In", "Team $", "Skat $", "Won", "Net"]]
    payouts = sorted(result.payouts.values(), key=lambda p: (-p.total, p.name))
    for p in payouts:
        e = entries.get(p.name)
        data.append([
            p.name + (" (guest)" if p.is_guest else ""), str(p.team_no),
            "guest" if p.is_guest else (str(e.quota) if e else ""),
            str(e.points_front) if e else "",
            str(e.points_back) if e else "", str(e.points_total) if e else "",
            str(e.score) if e and e.score else "",
            str(p.greens), str(p.skins), _money(p.ante),
            _money(p.team_money), _money(p.skat_money),
            _money(p.total), f"{'+' if p.net >= 0 else '-'}${abs(p.net):,.2f}",
        ])
    data.append(["TOTAL", "", "", "", "", "", "", "", "",
                 _money(result.total_collected),
                 _money(sum(p.team_money for p in payouts)),
                 _money(sum(p.skat_money for p in payouts)),
                 _money(result.total_paid),
                 _money(result.total_paid - result.total_collected)])
    t = Table(data, colWidths=[1.42 * inch, 0.22 * inch, 0.42 * inch, 0.36 * inch,
                               0.33 * inch, 0.36 * inch, 0.4 * inch, 0.3 * inch,
                               0.3 * inch, 0.42 * inch, 0.56 * inch, 0.56 * inch,
                               0.56 * inch, 0.56 * inch])
    style = list(GRID) + [
        ("ALIGN", (1, 1), (-1, -1), "CENTER"),
        ("FONTNAME", (0, len(data) - 1), (-1, len(data) - 1), "Helvetica-Bold"),
        ("BACKGROUND", (0, len(data) - 1), (-1, len(data) - 1), colors.HexColor("#e8e8e8")),
    ]
    for i, p in enumerate(payouts, start=1):
        if p.net > 0:
            style.append(("TEXTCOLOR", (13, i), (13, i), colors.HexColor("#1a7f37")))
        elif p.net < 0:
            style.append(("TEXTCOLOR", (13, i), (13, i), colors.HexColor("#b22222")))
        if p.is_guest:
            style.append(("TEXTCOLOR", (0, i), (2, i), colors.HexColor("#777777")))
    t.setStyle(TableStyle(style))
    story.append(t)

    if result.carried_money:
        story.append(Spacer(1, 6))
        story.append(Paragraph(
            f"<b>{_money(result.carried_money)} carried forward</b> to the next round.", BODY))

    if result.warnings:
        story.append(Paragraph("Notes", H3))
        for w in result.warnings:
            story.append(Paragraph("&#183; " + w, SMALL))

    doc.build(story)
    return path


# --------------------------------------------------------------------------
# year to date member stats
# --------------------------------------------------------------------------

def ytd_report(path: str, conn, year: int, as_of: date | None = None) -> str:
    df = stats.year_to_date(conn, year)
    df = df[df["Rds"] > 0].copy()
    as_of = as_of or date.today()

    doc = SimpleDocTemplate(path, pagesize=landscape(letter),
                            leftMargin=0.4 * inch, rightMargin=0.4 * inch,
                            topMargin=0.45 * inch, bottomMargin=0.4 * inch,
                            title=f"Cartel Member Stats {year}")
    story = [Paragraph("Cartel Member Points and Stats", H1)]
    story.append(Paragraph(
        f"{year} calendar year &nbsp;&#183;&nbsp; as of "
        f"{as_of.strftime('%d-%b-%y')} &nbsp;&#183;&nbsp; {len(df)} members with rounds", H2))

    head = ["Name", "Pts", "T", "Rds", "Team $", "Skat $", "Won $", "$/Rd",
            "Grns", "Grns/Rd", "Skins", "Skins/Rd", "Skats", "Skats/Rd"]
    data = [head]
    for _, r in df.iterrows():
        data.append([
            r["Name"],
            "guest" if r["Guest"] else (f"{int(r['Pts'])}" if pd.notna(r["Pts"]) else ""),
            r["Tee"], f"{int(r['Rds'])}",
            _money(r["Team$"]), _money(r["Skat$"]), _money(r["Won$"]), _money(r["$/Rd"]),
            f"{int(r['Greens'])}", f"{r['Grns/Rd']:.2f}",
            f"{int(r['Skins'])}", f"{r['Skins/Rd']:.2f}",
            f"{int(r['Skats'])}", f"{r['Skats/Rd']:.2f}",
        ])
    data.append(["TOTAL", "", "", f"{int(df['Rds'].sum())}",
                 _money(df["Team$"].sum()), _money(df["Skat$"].sum()),
                 _money(df["Won$"].sum()), "", f"{int(df['Greens'].sum())}", "",
                 f"{int(df['Skins'].sum())}", "", f"{int(df['Skats'].sum())}", ""])

    widths = [1.62 * inch, 0.44 * inch, 0.3 * inch, 0.4 * inch, 0.72 * inch, 0.72 * inch,
              0.74 * inch, 0.66 * inch, 0.48 * inch, 0.64 * inch, 0.5 * inch, 0.66 * inch,
              0.52 * inch, 0.66 * inch]
    t = Table(data, colWidths=widths, repeatRows=1)
    style = list(GRID) + [
        ("ALIGN", (1, 1), (-1, -1), "CENTER"),
        ("FONTSIZE", (0, 1), (-1, -1), 7.5),
        ("FONTNAME", (0, len(data) - 1), (-1, len(data) - 1), "Helvetica-Bold"),
        ("BACKGROUND", (0, len(data) - 1), (-1, len(data) - 1), colors.HexColor("#e0e0e0")),
    ]
    for i, (_, r) in enumerate(df.iterrows(), start=1):
        if r["Guest"]:
            style.append(("TEXTCOLOR", (0, i), (2, i), colors.HexColor("#777777")))
    t.setStyle(TableStyle(style))
    story.append(t)

    seed_team = df["Seed_Team$"].sum()
    seed_skat = df["Seed_Skat$"].sum()
    post_team = df["Posted_Team$"].sum()
    post_skat = df["Posted_Skat$"].sum()
    rec = stats.house_reconciliation(conn, year)

    story.append(Spacer(1, 8))
    story.append(Paragraph(
        f"<b>How this adds up.</b> Opening balance carried in from the group's YTD "
        f"Winnings sheet: {_money(seed_team)} team + {_money(seed_skat)} skats = "
        f"{_money(seed_team + seed_skat)}. Settled in this app since: "
        f"{_money(post_team)} team + {_money(post_skat)} skats = "
        f"{_money(post_team + post_skat)} across {rec['rounds']} round(s).", SMALL))
    story.append(Paragraph(
        f"<b>Rounds settled here balance:</b> {_money(rec['collected'])} collected, "
        f"{_money(rec['paid_out'])} paid out, {_money(rec['carried'])} carried, "
        f"{_money(rec['unaccounted'])} unaccounted.", SMALL))
    story.append(Paragraph(
        f"Pts is the current quota: the rounded average of that member's most recent "
        f"{RULES.quota_window} rounds. A member with fewer than "
        f"{RULES.guest_min_rounds} rounds on file has no quota and plays as a guest "
        f"(a guest's whole ante goes to greens and skins; no team money either way). "
        f"Rounds, greens and skins are counted from the full points history; money is the "
        f"seeded opening balance plus everything settled here since.", SMALL))

    doc.build(story)
    return path


# --------------------------------------------------------------------------
# excel export, same shape as Golf_Stats.xlsx
# --------------------------------------------------------------------------

def export_workbook(path: str, conn, year: int | None = None) -> str:
    """
    Write the workbook back out in the group's own shape, so the spreadsheet
    workflow keeps working and the data is never trapped in the app.
    """
    hist = db.read_sql("""SELECT r.played_on AS "Date", r.course AS "Course", e.team_no AS "Team",
                  e.name AS "Player", e.points_front AS "Points_Front",
                  e.points_back AS "Points_Back", e.score AS "Score",
                  e.greens AS "Greens", e.skins AS "Skins",
                  e.quota AS "Pts_Quota", e.is_guest AS "Guest",
                  r.round_no AS "RoundNo", r.status AS "Source"
           FROM entries e
           JOIN rounds r ON r.round_id = e.round_id
           WHERE r.status IN ('legacy','posted') AND e.points_front IS NOT NULL
           ORDER BY r.played_on DESC, r.round_id DESC, e.team_no, e.name""", conn)

    members = pd.DataFrame([{"Name": r["name"], "Tee": r["tee"], "Active": r["active"],
                             "Manual_Quota": r["manual_quota"]}
                            for r in storage.all_members(conn)])
    year = year or date.today().year
    ytd = stats.year_to_date(conn, year)
    members = members.merge(
        ytd[["Name", "Pts", "Guest", "Rds", "Team$", "Skat$", "Won$",
             "Greens", "Skins", "Skats"]], on="Name", how="left")

    ledger = db.read_sql("""SELECT name AS "Name", year AS "Year", team_money AS "Seed_Team$",
                  skat_money AS "Seed_Skat$", source AS "Source"
           FROM ledger_seed ORDER BY year DESC, name""", conn)

    settled = db.read_sql("""SELECT r.round_no AS "Round", r.played_on AS "Date", r.course AS "Course",
                  p.name AS "Player", p.is_guest AS "Guest", p.ante AS "In$",
                  p.team_money AS "Team$", p.skat_money AS "Skat$",
                  p.team_money + p.skat_money AS "Won$"
           FROM payouts p JOIN rounds r ON r.round_id = p.round_id
           WHERE r.status='posted'
           ORDER BY r.played_on DESC, r.round_id DESC, p.name""", conn)

    with pd.ExcelWriter(path, engine="openpyxl") as xl:
        hist.to_excel(xl, sheet_name="Running_Stats", index=False)
        members.to_excel(xl, sheet_name="Membership", index=False)
        ytd.to_excel(xl, sheet_name=f"YTD_{year}", index=False)
        ledger.to_excel(xl, sheet_name="Ledger_Seed", index=False)
        if not settled.empty:
            settled.to_excel(xl, sheet_name="Settled_Rounds", index=False)
        for ws in xl.book.worksheets:
            for col in ws.columns:
                width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
                ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 9), 26)
            for row in ws.iter_rows():
                for c in row:
                    c.font = Font(name="Arial", bold=c.font.bold, size=c.font.size)
            ws.freeze_panes = "A2"
    return path


# --------------------------------------------------------------------------
# the two on-screen reports, as printable PDFs
# --------------------------------------------------------------------------

def _anchor_date(conn) -> str:
    r = storage.last_posted_round(conn)
    return r["played_on"] if r is not None else "today"


def _anchor_line(conn) -> str:
    r = storage.last_posted_round(conn)
    if r is None:
        return "No rounds settled yet"
    return (f"As things stand after Round {r['round_no'] or r['round_id']}, "
            f"{r['played_on']} ({COURSE_NAMES.get(r['course'], r['course'])})")


def quota_basis_report(path: str, conn, names: list[str] | None = None) -> str:
    """
    The rounds each player's next quota rests on, one page-flowing table.

    Same data as the Next quota tab and the CSV - all three call
    stats.quota_basis, so they cannot disagree.
    """
    basis = stats.quota_basis(conn, names=names)

    doc = SimpleDocTemplate(path, pagesize=letter,
                            leftMargin=0.7 * inch, rightMargin=0.7 * inch,
                            topMargin=0.5 * inch, bottomMargin=0.5 * inch,
                            title="Cartel - basis for the next quota")
    story = [Paragraph("Basis for the Next Quota", H1),
             Paragraph(_anchor_line(conn), H2)]

    if basis.empty:
        story.append(Paragraph("No scored rounds on file yet.", BODY))
        doc.build(story)
        return path

    head = ["Player", "Date", "Score", "Front", "Back", "Total"]
    data = [head]
    summary_rows = []
    for _, r in basis.iterrows():
        label = str(r["Date"])
        if label.startswith(("Average", "Quota")):
            summary_rows.append(len(data))
        data.append([r["Player"], label, r["Score"], r["Front"], r["Back"], r["Total"]])

    t = Table(data, colWidths=[1.7 * inch, 1.5 * inch, 0.75 * inch, 0.7 * inch,
                               0.7 * inch, 0.75 * inch], repeatRows=1)
    style = list(GRID) + [("ALIGN", (2, 1), (-1, -1), "CENTER")]
    for i in summary_rows:
        style += [("FONTNAME", (0, i), (-1, i), "Helvetica-Bold"),
                  ("BACKGROUND", (0, i), (-1, i), colors.HexColor("#eeeeee"))]
    # a rule under each player's block, so the eye can find the groupings
    for i in summary_rows:
        if i + 1 < len(data) and data[i + 1][1] not in ("Quota",):
            style.append(("LINEBELOW", (0, i), (-1, i), 0.9, colors.HexColor("#3a3a3a")))
    t.setStyle(TableStyle(style))
    story.append(t)

    story.append(Spacer(1, 8))
    story.append(Paragraph(
        f"Quota is the rounded average of a member's most recent "
        f"{RULES.quota_window} scored rounds. A member with fewer than "
        f"{RULES.guest_min_rounds} scored rounds has no quota and plays as a guest. "
        f"These are the same rounds, in the same order, that the app itself uses to "
        f"set quotas.", SMALL))
    doc.build(story)
    return path


def standings_report(path: str, conn, year: int) -> str:
    """Year-to-date standings as a printable table. Landscape, it is wide."""
    # Alphabetical, so a name can be found without hunting. Standing is carried
    # by the Rank column instead, which ranks on $/Rd rather than total won -
    # money won rewards turning up, $/Rd rewards playing well.
    df = stats.year_to_date(conn, year)
    df = df[df["Rds"] > 0].copy().sort_values("Name")

    doc = SimpleDocTemplate(path, pagesize=landscape(letter),
                            leftMargin=0.4 * inch, rightMargin=0.4 * inch,
                            topMargin=0.45 * inch, bottomMargin=0.4 * inch,
                            title=f"Cartel Standings {year}")
    story = [Paragraph(f"{year} Standings", H1),
             Paragraph(_anchor_line(conn) +
                       " &nbsp;&#183;&nbsp; listed A-Z &nbsp;&#183;&nbsp; "
                       "Rank is by $ per round, 1 = highest", H2)]

    head = ["Name", "Rank", "Pts", "T", "Rds", "Team $", "Skat $", "Won $", "$/Rd",
            "Grns", "Skins", "Skats", "Skats/Rd"]
    data = [head]
    for _, r in df.iterrows():
        data.append([
            r["Name"], ("\u2013" if pd.isna(r["Rank"]) else str(int(r["Rank"]))),
            "guest" if r["Guest"] else (f"{int(r['Pts'])}" if pd.notna(r["Pts"]) else ""),
            r["Tee"], f"{int(r['Rds'])}",
            _money(r["Team$"]), _money(r["Skat$"]), _money(r["Won$"]), _money(r["$/Rd"]),
            f"{int(r['Greens'])}", f"{int(r['Skins'])}", f"{int(r['Skats'])}",
            f"{r['Skats/Rd']:.2f}",
        ])
    data.append(["TOTAL", "", "", "", f"{int(df['Rds'].sum())}",
                 _money(df["Team$"].sum()), _money(df["Skat$"].sum()),
                 _money(df["Won$"].sum()), "", f"{int(df['Greens'].sum())}",
                 f"{int(df['Skins'].sum())}", f"{int(df['Skats'].sum())}", ""])

    widths = [1.6, 0.45, 0.45, 0.3, 0.4, 0.78, 0.78, 0.8, 0.7, 0.5, 0.5, 0.5, 0.7]
    t = Table(data, colWidths=[w * inch for w in widths], repeatRows=1)
    style_extra = [
        ("ALIGN", (1, 1), (-1, -1), "CENTER"),
        ("FONTNAME", (1, 1), (1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 1), (-1, -1), 7.5),
        ("FONTNAME", (0, len(data) - 1), (-1, len(data) - 1), "Helvetica-Bold"),
        ("BACKGROUND", (0, len(data) - 1), (-1, len(data) - 1), colors.HexColor("#e0e0e0")),
    ]
    # tint the top three on $/Rd so the leaders still stand out at a glance
    for i, (_, r) in enumerate(df.iterrows(), start=1):
        if pd.notna(r["Rank"]) and int(r["Rank"]) <= 3:
            style_extra.append(
                ("BACKGROUND", (0, i), (-1, i), colors.HexColor("#dff0d8")))
    t.setStyle(TableStyle(list(GRID) + style_extra))
    story.append(t)
    story.append(Spacer(1, 8))
    unranked = int(df["Rank"].isna().sum())
    story.append(Paragraph(
        f"<b>Rank</b> is position on $ won per round played, 1 being the highest. "
        f"To be ranked a member needs at least {RULES.rank_min_rounds} rounds in the "
        f"{RULES.rank_window_months} months to {_anchor_date(conn)}; "
        f"{unranked} member(s) here show \u2013 because they don't yet. Players level "
        f"on $/Rd share a rank. Money won rewards turning up as well as playing well; "
        f"$ per round separates the two.", SMALL))
    doc.build(story)
    return path
