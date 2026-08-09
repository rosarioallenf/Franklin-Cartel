"""
Generate the printable scoresheet for a round.

Same layout as the existing Scorecard.pdf, except the two things that were
manual are now filled in:

  Pts_Quota          each player's rolling 5-round quota
  Team Points/side   the Xi value, being the sum of that team's quotas / 2

Only players who are actually posted count toward Xi. Blank rows are left
for hand entry on the course; a no-show simply never gets points, and the
app re-derives Xi from who actually played when the round is settled.
"""
from __future__ import annotations

from datetime import date

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from .config import RULES
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, KeepTogether,
)

COURSE_NAMES = {"N": "North", "S": "South"}

COLS = ["Team", "Player", "Pts_Quota", "Pts_Front", "Pts_Back", "Score", "Green", "Skins"]
COL_WIDTHS = [0.55 * inch, 1.85 * inch, 0.85 * inch, 0.85 * inch,
              0.8 * inch, 0.7 * inch, 0.65 * inch, 0.65 * inch]

TITLE = ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=16, alignment=TA_CENTER,
                       spaceAfter=2)
SUB = ParagraphStyle("s", fontName="Helvetica", fontSize=10.5, alignment=TA_CENTER,
                     textColor=colors.HexColor("#444444"), spaceAfter=10)
NOTE = ParagraphStyle("n", fontName="Helvetica-Oblique", fontSize=7.5,
                      textColor=colors.HexColor("#666666"), leading=10)


def build_scoresheet(
    path: str,
    *,
    round_no: int | None,
    played_on: date,
    course: str,
    teams: list[dict],
    quotas: dict,
    title: str = "2026 Cartel",
) -> str:
    """
    teams:  [{"team_no": 1, "tee_time": "11:06 AM", "players": [...]}, ...]
    quotas: name -> QuotaResult (a guest has quota None and is_guest True)
    """
    def q_of(name):
        q = quotas.get(name)
        return getattr(q, "quota", q)

    def is_guest(name):
        q = quotas.get(name)
        return bool(getattr(q, "is_guest", q is None))

    guests = [n for t in teams for n in t["players"] if is_guest(n)]
    doc = SimpleDocTemplate(
        path, pagesize=letter,
        leftMargin=0.7 * inch, rightMargin=0.7 * inch,
        topMargin=0.55 * inch, bottomMargin=0.5 * inch,
        title=f"{title} Scoresheet Round {round_no}",
    )

    story = [Paragraph(title, TITLE)]
    bits = []
    if round_no:
        bits.append(f"Round {round_no}")
    if hasattr(played_on, "strftime"):
        bits.append(f"{played_on.strftime('%a, %B')} {played_on.day}, {played_on.year}")
    else:
        bits.append(str(played_on))
    bits.append(f"{COURSE_NAMES.get(course, course)} Course")
    story.append(Paragraph("Scoresheet &nbsp;&#183;&nbsp; " + " &nbsp;&#183;&nbsp; ".join(bits), SUB))

    data = [COLS]
    style_cmds = [
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#3a3a3a")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("ALIGN", (2, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.6, colors.HexColor("#888888")),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 1), (-1, -1), 9.5),
        ("TOPPADDING", (0, 1), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 5),
    ]

    row = 1
    guest_rows = []
    for t in teams:
        players = t["players"]
        team_quota = sum(q_of(p) for p in players if not is_guest(p)) / 2.0
        first = row
        for i, p in enumerate(players):
            g = is_guest(p)
            if g:
                guest_rows.append(row)
            data.append([
                str(t["team_no"]) if i == 0 else "",
                p,
                ("guest" if g else str(q_of(p))),
                "", "", "", "", "",
            ])
            row += 1
        # the Xi row
        data.append(["", "Team Points/side", f"{team_quota:.1f}", "", "", "", "", ""])
        style_cmds += [
            ("SPAN", (0, first), (0, row)),
            ("ALIGN", (0, first), (0, row), "CENTER"),
            ("FONTNAME", (1, row), (2, row), "Helvetica-Bold"),
            ("BACKGROUND", (0, row), (-1, row), colors.HexColor("#e8e8e8")),
            ("LINEABOVE", (0, row), (-1, row), 0.9, colors.HexColor("#3a3a3a")),
            ("LINEBELOW", (0, row), (-1, row), 1.1, colors.HexColor("#3a3a3a")),
        ]
        row += 1

    for gr in guest_rows:
        style_cmds += [
            ("FONTNAME", (2, gr), (2, gr), "Helvetica-Oblique"),
            ("TEXTCOLOR", (1, gr), (2, gr), colors.HexColor("#777777")),
        ]

    tbl = Table(data, colWidths=COL_WIDTHS, repeatRows=1)
    tbl.setStyle(TableStyle(style_cmds))
    story.append(tbl)
    story.append(Spacer(1, 12))

    tee_note = " &nbsp;&#183;&nbsp; ".join(
        f"T{t['team_no']} {t['tee_time']}" for t in teams if t.get("tee_time"))
    lines = [
        f"<b>Tee times:</b> {tee_note}" if tee_note else "",
        "<b>Team Points/side</b> is the team quota for one nine: the sum of that team's "
        "Pts_Quota, halved. The team with the highest (points scored &minus; quota) wins "
        "the side. Enter Green and Skins as counts, not ticks &mdash; the two added "
        "together are that player's skats.",
    ]
    if guests:
        lines.append(
            "<b>Guest:</b> " + ", ".join(guests) + " &mdash; fewer than "
            f"{RULES.guest_min_rounds} rounds on file, so no quota. "
            f"${RULES.guest_ante:.0f} in, greens and skins only. Their points do not "
            "count toward the team total and they take no share of team money. "
            "<b>Write their points down anyway</b> &mdash; that is what earns them a "
            f"quota, and they become a full member automatically at "
            f"{RULES.guest_min_rounds} rounds.")
    short = [n for n in quotas
             if getattr(quotas[n], "short_history", False)]
    if short:
        lines.append("<b>Short history:</b> " + ", ".join(
            f"{n} (quota from {quotas[n].rounds_used} round(s))" for n in short))
    for ln in lines:
        if ln:
            story.append(Paragraph(ln, NOTE))
            story.append(Spacer(1, 3))

    doc.build(story)
    return path
