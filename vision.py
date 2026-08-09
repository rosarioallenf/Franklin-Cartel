"""
Optional: read a photo or scan of a filled-in paper scoresheet.

This is the insertion point for option 2 in the results-entry decision. It is
deliberately a *pre-fill*, never an authority. The function returns values
plus a per-field confidence, and the UI makes a human confirm every row before
anything is settled.

Handwriting is genuinely hard on exactly the fields that matter: a 7 against a
9, a 1 against a 7, and counting ticks in the Green/Skats columns. So the
contract here is "save typing", not "replace the typist".

Needs ANTHROPIC_API_KEY in the environment. Without it the app falls back to
manual entry and nothing breaks.
"""
from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# Current model IDs are claude-sonnet-5, claude-opus-5, claude-haiku-4-5-20251001.
# Sonnet is the right trade here: the job is reading handwritten digits, not
# reasoning, and a round costs about a penny.
MODEL = os.environ.get("CARTEL_VISION_MODEL", "claude-sonnet-5")

PROMPT = """You are transcribing a handwritten golf scoresheet. Return JSON only.

The printed columns are: Team, Player, Pts (the pre-printed quota), Pts_F, Pts_B,
Score, Green, Skats. The printed values in Team, Player and Pts are typed; every
other value is handwritten.

Rules for reading this sheet:
- Pts_F and Pts_B are Stableford points for the front and back nine. Typically
  4-30 each.
- Score is a stroke total, typically 65-110, and should be plausible against the
  points (more points means a lower score).
- The Green column marks closest-to-the-pin wins. A mark of any kind (X, tick,
  slash) counts as 1 unless several distinct marks are visible.
- The Skats column marks skins. Count the distinct marks: two ticks means 2.
- GUESTS. A row whose printed Pts column reads "guest" instead of a number is a
  guest. Guests play for greens and skins only, and nobody writes their points
  down - so a guest row with no points is NORMAL and that player DID play. Set
  "played": true and "is_guest": true for them, with null points.
- A row with no handwriting at all AND a printed number in the Pts column is a
  member who did not tee off. Report every field as null and set
  "played": false.
- In short: blank + printed quota = did not play. Blank + "guest" = played, but
  their points were never recorded.
- Never invent a value to fill a gap. If you cannot read a digit, return null
  for that field and say so in "uncertain".

For each player return:
  name, team, quota, points_front, points_back, score, greens, skins, played,
  is_guest, confidence ("high" | "medium" | "low"), uncertain (list of field
  names you could not read cleanly)

Return exactly this shape and nothing else:
{"players": [ ... ], "notes": "anything odd about the sheet"}"""


@dataclass
class VisionRow:
    name: str
    team: int | None = None
    quota: int | None = None
    points_front: int | None = None
    points_back: int | None = None
    score: int | None = None
    greens: int = 0
    skins: int = 0
    played: bool = True
    is_guest: bool = False
    confidence: str = "low"
    uncertain: list[str] = field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        return self.confidence != "high" or bool(self.uncertain)


@dataclass
class VisionRead:
    rows: list[VisionRow]
    notes: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def review_count(self) -> int:
        return sum(1 for r in self.rows if r.needs_review)


def available() -> bool:
    # The house rule decides first. Missing credentials would hide the panel
    # anyway, but "off because we decided" is a firmer thing than "off because
    # nobody happened to set a key".
    from .config import RULES
    if not RULES.photo_prefill_enabled:
        return False
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def _media_type(path: str) -> str:
    ext = Path(path).suffix.lower()
    return {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".webp": "image/webp"}.get(ext, "image/png")


def _pdf_to_png(pdf_path: str, out_dir: str) -> list[str]:
    """A scanned scoresheet arrives as a PDF with no text layer, so rasterize."""
    import subprocess
    stem = str(Path(out_dir) / "page")
    subprocess.run(["pdftoppm", "-r", "200", "-png", pdf_path, stem],
                   check=True, capture_output=True)
    return sorted(str(p) for p in Path(out_dir).glob("page*.png"))


def read_scoresheet(image_path: str, expected_names: list[str] | None = None,
                    tmp_dir: str = "/tmp/cartel_vision") -> VisionRead:
    """
    Pre-fill a settlement form from a photo or scan. Always followed by human
    confirmation in the UI.
    """
    if not available():
        raise RuntimeError(
            "Vision pre-fill needs ANTHROPIC_API_KEY set and the anthropic "
            "package installed. Use manual entry instead."
        )
    import anthropic

    Path(tmp_dir).mkdir(parents=True, exist_ok=True)
    paths = ([image_path] if not image_path.lower().endswith(".pdf")
             else _pdf_to_png(image_path, tmp_dir))

    content: list[dict] = []
    for p in paths:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": _media_type(p),
                       "data": base64.standard_b64encode(Path(p).read_bytes()).decode()},
        })

    prompt = PROMPT
    if expected_names:
        prompt += ("\n\nThe players posted for this round are, exactly: "
                   + ", ".join(expected_names)
                   + ". Use these spellings and do not report anyone else.")
    content.append({"type": "text", "text": prompt})

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=MODEL, max_tokens=2000,
        messages=[{"role": "user", "content": content}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    payload = _parse_json(text)

    rows = []
    for r in payload.get("players", []):
        rows.append(VisionRow(
            name=str(r.get("name", "")).strip(),
            team=_int(r.get("team")), quota=_int(r.get("quota")),
            points_front=_int(r.get("points_front")),
            points_back=_int(r.get("points_back")),
            score=_int(r.get("score")),
            greens=_int(r.get("greens")) or 0, skins=_int(r.get("skins")) or 0,
            played=bool(r.get("played", True)),
            is_guest=bool(r.get("is_guest", False)),
            confidence=str(r.get("confidence", "low")).lower(),
            uncertain=list(r.get("uncertain") or []),
        ))

    read = VisionRead(rows=rows, notes=str(payload.get("notes", "")))
    read.warnings.extend(_sanity_checks(read.rows, expected_names))
    return read


def _parse_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise ValueError(f"Could not parse a JSON reply:\n{text[:400]}")


def _int(v):
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _sanity_checks(rows: list[VisionRow], expected: list[str] | None) -> list[str]:
    """
    Cheap arithmetic checks the model can't fake its way past. These catch the
    common misreads without needing a second opinion.
    """
    out = []
    for r in rows:
        if not r.played:
            continue
        if r.is_guest and r.points_front is None:
            continue          # normal: nobody records a guest's points
        if r.points_front is None or r.points_back is None:
            out.append(f"{r.name}: missing a points figure - confirm by hand.")
            continue
        for label, v in (("front", r.points_front), ("back", r.points_back)):
            if not (0 <= v <= 40):
                out.append(f"{r.name}: {label} points read as {v}, outside 0-40.")
        if r.score is not None:
            if not (55 <= r.score <= 130):
                out.append(f"{r.name}: score read as {r.score}, outside 55-130.")
            # a rough Stableford sanity band: more points should mean fewer strokes
            total = r.points_front + r.points_back
            if total >= 30 and r.score > 95:
                out.append(f"{r.name}: {total} points against a score of {r.score} "
                           f"doesn't hang together - check both.")
            if total <= 12 and r.score < 80:
                out.append(f"{r.name}: {total} points against a score of {r.score} "
                           f"doesn't hang together - check both.")
        if r.greens > 3 or r.skins > 6:
            out.append(f"{r.name}: {r.greens} greens / {r.skins} skins looks high - "
                       f"recount the marks.")
    # A guest read as absent quietly shrinks the skat pot by their $10, and
    # nothing on screen would look wrong. Worth saying out loud.
    for r in rows:
        if r.is_guest and not r.played:
            out.append(
                f"{r.name} was read as a guest who did NOT play. If they were there, "
                f"tick Played - a guest with no points recorded still paid in.")

    if expected:
        got = {r.name for r in rows}
        missing = sorted(set(expected) - got)
        extra = sorted(got - set(expected))
        if missing:
            out.append(f"Posted but not found on the sheet: {', '.join(missing)}")
        if extra:
            out.append(f"Read off the sheet but not posted for this round: {', '.join(extra)}")
    return out
