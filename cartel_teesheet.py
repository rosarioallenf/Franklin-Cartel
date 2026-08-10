"""
Read the 2026 Cartel tee sheet PDF.

The club changed the sheet in August 2026: it no longer prints a Team
number, and the course heading is sometimes missing. Teams are now shown
only as alternating white/grey bands, one tee time each.

Do NOT key off the shading. The grey band is a drawn rectangle but a white
band is *nothing at all* - no object is emitted for it - so a shading-based
reader finds only every other team, and finds none on a page whose single
team happens to be white.

What is present on every boundary is the dashed rule: ~193 tiny rects
sharing one 'top'. Those, plus the solid rule under the Time/Players
header, delimit every block regardless of colour. Blocks are then checked
against a row pitch derived from the document itself, so a bad split is
caught rather than quietly mis-assigning a player to the wrong team.

Tee time is a guide, not an absolute. Two teams can share one, and the
scheduler moves them around. It is carried through as a convenience label
for the printed sheet and is used for exactly one parse check - a block
holding two times means two teams got merged - but nothing groups, keys,
orders or validates against it.
"""
from __future__ import annotations

import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

import pdfplumber

from cartel_config import RULES

TIME_RE = re.compile(r"^\d{1,2}:\d{2}\s*(AM|PM)$", re.I)
BARE_TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")
ROUND_RE = re.compile(r"Round\s+(\d+)", re.I)
DATE_RE = re.compile(
    r"(Mon|Tue|Tues|Wed|Thu|Thur|Thurs|Fri|Sat|Sun)[a-z]*,?\s+"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{1,2})",
    re.I,
)
YEAR_RE = re.compile(r"\b(20\d{2})\b")
COURSE_WORDS = {"north": "N", "south": "S", "n": "N", "s": "S"}
DEFAULT_COURSE = "N"          # sheet omits the course -> North, but say so loudly

MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}

# --- block detection tuning -------------------------------------------------
SEP_MIN_SEGMENTS = 20         # dashes sharing one 'top' before it counts as a rule
SEP_DASH_MAX_H = 2.0
SEP_DASH_MAX_W = 3.0
PITCH_TOL = 0.25              # block height must be within this many rows of a whole number
COLUMN_SPLIT_X = 65.0         # left of this is the Time column
GUTTER_MAX_X = 100.0          # course heading sits hard left


@dataclass
class TeeGroup:
    team_no: int
    tee_time: str
    course: str
    players: list[str] = field(default_factory=list)
    tees: str | None = None            # e.g. "Blue" from a "North / Blue" heading
    page: int = 1


@dataclass
class TeeSheet:
    round_no: int | None
    played_on: date | None
    groups: list[TeeGroup] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)      # hard stops - do not import
    course_defaulted: bool = False
    row_pitch: float | None = None

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def courses(self) -> list[str]:
        seen = []
        for g in self.groups:
            if g.course not in seen:
                seen.append(g.course)
        return seen

    @property
    def players(self) -> list[str]:
        return [p for g in self.groups for p in g.players]

    def as_rows(self) -> list[dict]:
        return [{"name": p, "team_no": g.team_no, "tee_time": g.tee_time, "course": g.course}
                for g in self.groups for p in g.players]


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------

def _grey(rect) -> float | None:
    """First colour component, whatever colour space pdfplumber reports."""
    c = rect.get("non_stroking_color")
    if c is None:
        return None
    if isinstance(c, (int, float)):
        return float(c)
    return float(c[0]) if len(c) else None


def _separator_tops(page) -> list[float]:
    """y of every dashed rule, found by counting co-linear dash segments."""
    c = Counter(round(r["top"], 1) for r in page.rects
                if r["height"] < SEP_DASH_MAX_H and r["width"] < SEP_DASH_MAX_W)
    return sorted(t for t, n in c.items() if n >= SEP_MIN_SEGMENTS)


def _header_bar_top(page) -> float | None:
    """Top of the dark Time/Players header bar."""
    dark = [r["top"] for r in page.rects
            if r["height"] > 4 and r["width"] > 40
            and (_grey(r) is not None and _grey(r) < 0.5)]
    return min(dark) if dark else None


def _header_rule_bottom(page, bar_top: float | None) -> float | None:
    """Bottom of the thin solid rule under the header row - the table's top edge."""
    thin = [r["bottom"] for r in page.rects
            if 1.0 <= r["height"] <= 2.5 and r["width"] > 40
            and (bar_top is None or r["top"] >= bar_top)]
    return min(thin) if thin else None


def _text_lines(words, tol: float = 3.0) -> list[tuple[float, str]]:
    """Cluster words into visual lines by vertical position."""
    out: list[tuple[float, list[str]]] = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if out and abs(w["top"] - out[-1][0]) <= tol:
            out[-1][1].append(w["text"])
        else:
            out.append((w["top"], [w["text"]]))
    return [(top, " ".join(parts).strip()) for top, parts in out]


def _course_from_page(page, words, bar_top: float | None) -> tuple[str | None, str | None, str]:
    """
    Returns (course_code, tees, raw_heading).

    The heading sits hard left between the subtitle and the header bar, and
    reads either "North", "South", or "North / Blue" (course / tee colour).
    """
    ceiling = bar_top if bar_top is not None else 150.0
    raw = " ".join(w["text"] for w in words
                   if 60 < w["top"] < ceiling - 5 and w["x0"] < GUTTER_MAX_X).strip()
    if not raw:
        return None, None, ""
    parts = [p.strip() for p in raw.split("/") if p.strip()]
    code = _course_code(parts[0]) if parts else None
    tees = parts[1] if len(parts) > 1 else None
    return code, tees, raw


def _course_code(text: str) -> str | None:
    """
    Read a course out of the heading.

    Matched by looking for the word, not by demanding the heading equal it.
    The exact-match version worked on "North" and "South" and silently defaulted
    on "South Course" - reporting that it could not read a course out of a
    string with the course plainly in it.
    """
    t = text.strip().lower()
    if not t:
        return None
    if "south" in t:
        return "S"
    if "north" in t:
        return "N"
    return COURSE_WORDS.get(t)


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

def parse_tee_sheet(pdf_path: str, default_year: int | None = None) -> TeeSheet:
    sheet = TeeSheet(round_no=None, played_on=None)
    year = default_year
    blocks: list[dict] = []          # raw blocks before numbering/validation

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            words = page.extract_words()
            if not words:
                continue

            header_text = " ".join(w["text"] for w in words[:40])
            if sheet.round_no is None:
                m = ROUND_RE.search(header_text)
                if m:
                    sheet.round_no = int(m.group(1))
            if year is None:
                m = YEAR_RE.search(header_text)
                if m:
                    year = int(m.group(1))
            if sheet.played_on is None:
                m = DATE_RE.search(header_text)
                if m:
                    sheet.played_on = date(year or date.today().year,
                                           MONTHS[m.group(2)[:3].lower()], int(m.group(3)))

            bar_top = _header_bar_top(page)
            top_edge = _header_rule_bottom(page, bar_top)
            seps = [s for s in _separator_tops(page)
                    if top_edge is not None and s > top_edge]

            code, tees, raw = _course_from_page(page, words, bar_top)
            if code is None:
                code = DEFAULT_COURSE
                sheet.course_defaulted = True
                if raw:
                    sheet.warnings.append(
                        f"Page {page.page_number}: couldn't read the course from "
                        f"\u201c{raw}\u201d - defaulted to North. Change it if that's wrong.")
                else:
                    sheet.warnings.append(
                        f"Page {page.page_number}: no course printed on the tee sheet - "
                        f"defaulted to North. Change it if that's wrong.")

            if top_edge is None or not seps:
                sheet.warnings.append(
                    f"Page {page.page_number}: no dashed team separators found - "
                    f"falling back to tee-time proximity, which is less reliable. "
                    f"Check the teams below carefully.")
                blocks.extend(_blocks_by_time(page, words, code, tees))
                continue

            bounds = [top_edge] + seps
            for a, b in zip(bounds[:-1], bounds[1:]):
                rows = [w for w in words if a < w["top"] < b - 2]
                left = [w for w in rows if w["x0"] < COLUMN_SPLIT_X]
                right = [w for w in rows if w["x0"] >= COLUMN_SPLIT_X]

                times = [s for _, s in _text_lines(left) if TIME_RE.match(s)]
                names = [s for _, s in _text_lines(right)
                         if s.lower() not in ("players", "time") and not TIME_RE.match(s)]

                blocks.append({"page": page.page_number, "y0": a, "y1": b,
                               "height": b - a, "times": times, "players": names,
                               "course": code, "tees": tees})

    if not blocks:
        sheet.errors.append("No tee groups found at all - is this the right PDF?")
        return sheet

    # --- row pitch, derived from the document itself ------------------------
    pitches = [b["height"] / len(b["players"]) for b in blocks if b["players"]]
    pitch = statistics.median(pitches) if pitches else None
    sheet.row_pitch = round(pitch, 2) if pitch else None

    for i, b in enumerate(blocks, start=1):
        n = len(b["players"])
        g = TeeGroup(team_no=i, tee_time=(b["times"][0].upper() if b["times"] else "?"),
                     course=b["course"], players=b["players"], tees=b["tees"], page=b["page"])
        sheet.groups.append(g)

        where = f"Team {i} (page {b['page']}, {g.tee_time})"
        if not (RULES.min_team_size <= n <= RULES.max_team_size):
            sheet.errors.append(
                f"{where} parsed with {n} player(s) [{', '.join(b['players']) or 'none'}] - "
                f"expected {RULES.min_team_size} to {RULES.max_team_size}.")
        if pitch and n:
            rows_pred = b["height"] / pitch
            if abs(rows_pred - n) > PITCH_TOL:
                sheet.errors.append(
                    f"{where} is {b['height']:.1f}pt tall, which is {rows_pred:.2f} rows at "
                    f"{pitch:.2f}pt each, but {n} name(s) were read. The block split is wrong.")
        if len(b["times"]) > 1:
            sheet.errors.append(
                f"{where} contains {len(b['times'])} tee times ({', '.join(b['times'])}) - "
                f"two teams have been merged into one block.")

    if len(sheet.courses) > 1:
        sheet.warnings.append(
            "This round has groups on more than one course ("
            + ", ".join(sheet.courses)
            + "). The round is stored under the first one - check that's right.")

    return sheet


def _blocks_by_time(page, words, course: str, tees: str | None) -> list[dict]:
    """Legacy fallback: assign each name to the nearest tee time."""
    left = [w for w in words if w["x0"] < COLUMN_SPLIT_X]
    right = [w for w in words if w["x0"] >= COLUMN_SPLIT_X]
    times = [(t, s) for t, s in _text_lines(left) if TIME_RE.match(s)]
    if not times:
        return []
    header_y = next((t for t, s in _text_lines(right) if s.strip().lower() == "players"),
                    min(t for t, _ in times) - 30)
    names = [(t, s) for t, s in _text_lines(right)
             if t > header_y and s.lower() not in ("players", "time") and not TIME_RE.match(s)]

    buckets: dict[float, list[str]] = {t: [] for t, _ in times}
    for top, name in names:
        buckets[min(buckets, key=lambda t: abs(t - top))].append(name)
    return [{"page": page.page_number, "y0": t, "y1": t, "height": 0.0,
             "times": [s], "players": buckets[t], "course": course, "tees": tees}
            for t, s in sorted(times)]


# ---------------------------------------------------------------------------
# roster matching
# ---------------------------------------------------------------------------

def reconcile_names(parsed: list[str], roster: set[str]) -> tuple[dict[str, str], list[str]]:
    """
    Match parsed names to the roster. Exact first, then case/space-insensitive,
    then a last-name + first-initial fallback. Anything left over is returned
    for a human to resolve, never guessed.

    Note: names like "Jay Dalgarn" and "Jay Dalgarn1" are two different people
    (father and son). The surname fallback deliberately refuses to choose when
    more than one roster entry matches, which is what keeps them apart.
    """
    mapping: dict[str, str] = {}
    unmatched: list[str] = []

    norm = {_norm(r): r for r in roster}
    surname: dict[str, list[str]] = {}
    for r in roster:
        surname.setdefault(_surname_key(r), []).append(r)

    for p in parsed:
        if p in roster:
            mapping[p] = p
        elif _norm(p) in norm:
            mapping[p] = norm[_norm(p)]
        else:
            cands = surname.get(_surname_key(p), [])
            if len(cands) == 1:
                mapping[p] = cands[0]
            else:
                unmatched.append(p)
    return mapping, unmatched


def _norm(s: str) -> str:
    return re.sub(r"[^a-z]", "", s.lower())


def _surname_key(s: str) -> str:
    parts = [p for p in re.split(r"\s+", s.strip()) if p]
    if not parts:
        return ""
    last = re.sub(r"[^a-z]", "", parts[-1].lower())
    first = re.sub(r"[^a-z]", "", parts[0].lower())[:1]
    return f"{last}|{first}"


# pdfminer logs a FontBBox warning every time a page object is touched, because
# one font in the club's tee sheet omits an optional bounding box. Harmless,
# but it floods the console and buries anything that matters.
import logging as _logging
_logging.getLogger("pdfminer").setLevel(_logging.ERROR)
_logging.getLogger("pdfplumber").setLevel(_logging.ERROR)
