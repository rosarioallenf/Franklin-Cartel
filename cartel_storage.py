"""
Storage. One SQLite file, plain stdlib sqlite3, portable DDL.

The model follows the rule that the quota is a moving target and there is no
point trying to reconstruct what it was on some past afternoon:

  entries      round-by-round POINTS history. Drives quotas and the round,
               green and skin counts. Legacy rows carry no money.
  ledger_seed  the authoritative year-to-date money each member starts from,
               taken from the group's own YTD Winnings sheet. Never recomputed,
               never overwritten.
  payouts      money for rounds this app settled, added on top of the seed.

So year-to-date money = seed + everything posted since. Nothing has to be
back-derived from the old winner flags, which is what made the legacy figures
impossible to reconcile.
"""
from __future__ import annotations

import hashlib
import secrets
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import cartel_db as db
from cartel_config import db_path, TEE_CODES

SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    name    TEXT PRIMARY KEY,
    tee     TEXT NOT NULL DEFAULT 'W',
    tee_no  INTEGER,
    active  INTEGER NOT NULL DEFAULT 1,
    manual_quota INTEGER          -- overrides the rolling average when set
);

CREATE TABLE IF NOT EXISTS ledger_seed (
    name       TEXT NOT NULL,
    year       INTEGER NOT NULL,
    team_money REAL NOT NULL DEFAULT 0,
    skat_money REAL NOT NULL DEFAULT 0,
    source     TEXT,
    PRIMARY KEY (name, year)
);

-- What a round costs, and every time it has changed. A history rather than a
-- single value: "the stake is $50" answers less than "it went to $50 in March,
-- and here is what it was before".
CREATE TABLE IF NOT EXISTS stakes (
    stake_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    member_ante  REAL NOT NULL,
    guest_ante   REAL NOT NULL,
    set_on       TEXT NOT NULL,
    note         TEXT
);

-- Money collected in a year that can never be paid out, and why. Kept as an
-- explicit line rather than spread across players: the shortfall came from the
-- old system failing to record a winning team, so nobody actually won it, and
-- crediting it to somebody would put a figure in the standings that never
-- happened. A written-off amount is a fact; a redistributed one is a guess.
CREATE TABLE IF NOT EXISTS writeoffs (
    writeoff_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    year         INTEGER NOT NULL,
    amount       REAL NOT NULL,
    reason       TEXT,
    recorded_on  TEXT NOT NULL
);

-- Who did what, and when. With one scorer this was never needed - it was
-- always Allen. With several, "who entered this round?" is the first question
-- asked when a figure looks wrong, and the app had no answer.
CREATE TABLE IF NOT EXISTS activity (
    activity_id INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    who         TEXT NOT NULL,
    action      TEXT NOT NULL,
    detail      TEXT
);

-- Small key/value store for app-level settings, e.g. the admin passphrase hash.
CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rounds (
    round_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    round_no    INTEGER,
    played_on   TEXT NOT NULL,
    course      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'draft',  -- legacy | draft | posted
    carry_in    REAL NOT NULL DEFAULT 0,
    carried_out REAL NOT NULL DEFAULT 0,
    -- captured when the round is PREPARED, so a stake change announced later
    -- never disturbs a round whose scoresheet is already printed
    member_ante REAL,
    guest_ante  REAL,
    posted_by   TEXT,
    notes       TEXT,
    UNIQUE (played_on, course)
);

CREATE TABLE IF NOT EXISTS entries (
    round_id     INTEGER NOT NULL REFERENCES rounds(round_id) ON DELETE CASCADE,
    name         TEXT    NOT NULL,
    team_no      INTEGER NOT NULL,
    tee_time     TEXT,
    quota        INTEGER,                  -- NULL for a guest
    is_guest     INTEGER NOT NULL DEFAULT 0,
    played       INTEGER NOT NULL DEFAULT 1,   -- explicit; a guest has no points
    points_front INTEGER,
    points_back  INTEGER,
    score        INTEGER,
    greens       INTEGER NOT NULL DEFAULT 0,
    skins        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (round_id, name)
);

CREATE TABLE IF NOT EXISTS side_outcomes (
    round_id  INTEGER NOT NULL REFERENCES rounds(round_id) ON DELETE CASCADE,
    side      TEXT    NOT NULL,
    team_no   INTEGER NOT NULL,
    points    INTEGER NOT NULL,
    quota     REAL    NOT NULL,
    net       REAL    NOT NULL,
    is_winner INTEGER NOT NULL,
    PRIMARY KEY (round_id, side, team_no)
);

CREATE TABLE IF NOT EXISTS payouts (
    round_id   INTEGER NOT NULL REFERENCES rounds(round_id) ON DELETE CASCADE,
    name       TEXT    NOT NULL,
    team_money REAL    NOT NULL DEFAULT 0,
    skat_money REAL    NOT NULL DEFAULT 0,
    ante       REAL    NOT NULL DEFAULT 0,
    is_guest   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (round_id, name)
);

CREATE INDEX IF NOT EXISTS ix_entries_name ON entries(name);

-- Every round a player actually completed, legacy or app-settled. Points and
-- counts come from here; money only exists on rows the app settled.
CREATE VIEW IF NOT EXISTS v_player_rounds AS
    SELECT e.name,
           r.round_id,
           r.played_on,
           r.round_no,
           r.course,
           r.status,
           e.team_no,
           e.is_guest,
           e.points_front,
           e.points_back,
           e.points_front + e.points_back AS points_total,
           e.score,
           e.greens,
           e.skins,
           e.greens + e.skins AS skats,
           COALESCE(p.team_money, 0) AS team_money,
           COALESCE(p.skat_money, 0) AS skat_money,
           COALESCE(p.ante, 0)       AS ante
    FROM entries e
    JOIN rounds r ON r.round_id = e.round_id
    LEFT JOIN payouts p ON p.round_id = e.round_id AND p.name = e.name
    WHERE e.played = 1
      AND (e.is_guest = 1 OR (e.points_front IS NOT NULL AND e.points_back IS NOT NULL))
      AND r.status IN ('legacy', 'posted');
"""


@contextmanager
def connect(path: str | Path | None = None):
    """
    Opens SQLite by default, or Postgres when CARTEL_DB_URL is set. Callers
    never need to know which.
    """
    conn = db.open_connection(path or db_path())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Columns added after the first release. Existing databases are upgraded in
# place: CREATE TABLE IF NOT EXISTS does nothing to a table that already exists,
# so new columns have to be added explicitly or a working install silently keeps
# the old shape.
MIGRATIONS = [
    ("rounds", "member_ante", "REAL"),
    ("rounds", "guest_ante", "REAL"),
    ("rounds", "posted_by", "TEXT"),
]


def _columns(conn, table: str) -> set[str]:
    """
    Which columns a table already has.

    Asked by dialect rather than by trying SQLite's PRAGMA and catching the
    failure: on Postgres a bad statement aborts the whole transaction, so the
    fallback query fails too and the real error is buried under
    "current transaction is aborted".
    """
    if db.is_postgres():
        return {r["column_name"] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = ?", (table,))}
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn) -> list[str]:
    applied = []
    for table, column, coltype in MIGRATIONS:
        if column not in _columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
            applied.append(f"{table}.{column}")
    return applied


def init_db(path: str | Path | None = None) -> None:
    from cartel_config import LEGACY_STAKE
    with connect(path) as conn:
        conn.executescript(db.translate_ddl(SCHEMA))
        _migrate(conn)
        # Seed the opening stake once, so there is never a round with no rate.
        if conn.execute("SELECT COUNT(*) c FROM stakes").fetchone()["c"] == 0:
            conn.execute(
                "INSERT INTO stakes (member_ante, guest_ante, set_on, note) "
                "VALUES (?,?,?,?)",
                (LEGACY_STAKE.member_ante, LEGACY_STAKE.guest_ante, "1900-01-01",
                 "Opening rate, as played before the app"))
        # Any round already on file predates the setting, so it played at the
        # legacy rate by definition.
        conn.execute(
            "UPDATE rounds SET member_ante = ?, guest_ante = ? "
            "WHERE member_ante IS NULL",
            (LEGACY_STAKE.member_ante, LEGACY_STAKE.guest_ante))


# --------------------------------------------------------------------------
# members
# --------------------------------------------------------------------------

def upsert_member(conn, name: str, tee: str = "W", active: bool = True,
                  manual_quota: int | None = None) -> None:
    conn.execute(
        """INSERT INTO members (name, tee, tee_no, active, manual_quota)
           VALUES (?,?,?,?,?)
           ON CONFLICT(name) DO UPDATE SET
             tee=excluded.tee, tee_no=excluded.tee_no,
             active=excluded.active, manual_quota=excluded.manual_quota""",
        (name.strip(), tee, TEE_CODES.get(tee), int(active), manual_quota),
    )


def all_members(conn, active_only: bool = False):
    sql = "SELECT * FROM members"
    if active_only:
        sql += " WHERE active = 1"
    return conn.execute(sql + " ORDER BY name").fetchall()


def member_names(conn) -> set[str]:
    return {r["name"] for r in conn.execute("SELECT name FROM members")}


def manual_quota(conn, name: str) -> int | None:
    row = conn.execute("SELECT manual_quota FROM members WHERE name = ?", (name,)).fetchone()
    return row["manual_quota"] if row else None


# --------------------------------------------------------------------------
# the seeded ledger
# --------------------------------------------------------------------------

def set_seed(conn, name: str, year: int, team_money: float, skat_money: float,
             source: str = "YTD Winnings sheet") -> None:
    conn.execute(
        """INSERT INTO ledger_seed (name, year, team_money, skat_money, source)
           VALUES (?,?,?,?,?)
           ON CONFLICT(name, year) DO UPDATE SET
             team_money=excluded.team_money, skat_money=excluded.skat_money,
             source=excluded.source""",
        (name.strip(), year, float(team_money), float(skat_money), source),
    )


def seeds(conn, year: int) -> dict[str, dict]:
    return {r["name"]: {"team_money": r["team_money"], "skat_money": r["skat_money"],
                        "source": r["source"]}
            for r in conn.execute("SELECT * FROM ledger_seed WHERE year = ?", (year,))}


# --------------------------------------------------------------------------
# quota history
# --------------------------------------------------------------------------

def rounds_on_file(conn, name: str, before: str | None = None) -> int:
    """Total completed rounds for a player. Drives the guest test."""
    sql = ("SELECT COUNT(*) c FROM v_player_rounds "
           "WHERE name = ? AND points_total IS NOT NULL")
    params: list = [name]
    if before:
        sql += " AND played_on < ?"
        params.append(before)
    return conn.execute(sql, params).fetchone()["c"]


def recent_points(conn, name: str, window: int, before: str | None = None) -> list[int]:
    """Most recent `window` round point totals for a player, newest first."""
    sql = ("SELECT points_total FROM v_player_rounds "
           "WHERE name = ? AND points_total IS NOT NULL")
    params: list = [name]
    if before:
        sql += " AND played_on < ?"
        params.append(before)
    sql += " ORDER BY played_on DESC, round_id DESC LIMIT ?"
    params.append(window)
    return [r["points_total"] for r in conn.execute(sql, params)]


# --------------------------------------------------------------------------
# rounds
# --------------------------------------------------------------------------

def write_off(conn, year: int, amount: float, reason: str, on: str) -> None:
    conn.execute(
        "INSERT INTO writeoffs (year, amount, reason, recorded_on) VALUES (?,?,?,?)",
        (int(year), float(amount), reason, on))


def written_off(conn, year: int) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) a FROM writeoffs WHERE year = ?",
        (int(year),)).fetchone()
    return float(row["a"] or 0.0)


def writeoff_history(conn, year: int | None = None):
    sql = "SELECT * FROM writeoffs"
    params: list = []
    if year is not None:
        sql += " WHERE year = ?"
        params.append(int(year))
    return conn.execute(sql + " ORDER BY recorded_on DESC, writeoff_id DESC",
                        params).fetchall()


# --------------------------------------------------------------------------
# who did what
# --------------------------------------------------------------------------

def log_activity(conn, who: str, action: str, detail: str = "") -> None:
    """Append-only. Nothing here is ever edited or removed."""
    conn.execute(
        "INSERT INTO activity (at, who, action, detail) VALUES (?,?,?,?)",
        (datetime.now().isoformat(timespec="seconds"), (who or "unknown").strip(),
         action, detail))


def recent_activity(conn, limit: int = 100):
    return conn.execute(
        "SELECT * FROM activity ORDER BY at DESC, activity_id DESC LIMIT ?",
        (limit,)).fetchall()


# --------------------------------------------------------------------------
# app settings, and the admin passphrase
# --------------------------------------------------------------------------

def get_setting(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(conn, key: str, value: str) -> None:
    conn.execute(
        """INSERT INTO app_settings (key, value) VALUES (?,?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (key, value))


def _hash_passphrase(passphrase: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", passphrase.encode("utf-8"), salt.encode("utf-8"), 120_000).hex()


def set_admin_passphrase(conn, passphrase: str) -> None:
    """
    Stored as a salted hash, never as the word itself. This is not a defence
    against a determined attacker - it is a gate that stops somebody exploring
    the Health tab from changing the stake for the whole group by accident.
    """
    salt = secrets.token_hex(16)
    set_setting(conn, "admin_salt", salt)
    set_setting(conn, "admin_hash", _hash_passphrase(passphrase, salt))


def admin_passphrase_set(conn) -> bool:
    return get_setting(conn, "admin_hash") is not None


def check_admin_passphrase(conn, passphrase: str) -> bool:
    stored = get_setting(conn, "admin_hash")
    salt = get_setting(conn, "admin_salt")
    if not stored or not salt:
        return True          # none set yet: nothing to check against
    return secrets.compare_digest(stored, _hash_passphrase(passphrase, salt))


def current_stake(conn):
    """The stake in force now — the most recent change."""
    from cartel_config import LEGACY_STAKE, Stake
    row = conn.execute(
        "SELECT member_ante, guest_ante FROM stakes "
        "ORDER BY set_on DESC, stake_id DESC LIMIT 1").fetchone()
    if row is None:
        return LEGACY_STAKE
    return Stake(member_ante=row["member_ante"], guest_ante=row["guest_ante"])


def set_stake(conn, member_ante: float, guest_ante: float, set_on: str,
              note: str | None = None) -> None:
    """Record a change. Never overwrites: the history is the point."""
    from cartel_config import Stake
    Stake(member_ante, guest_ante).validate()
    conn.execute(
        "INSERT INTO stakes (member_ante, guest_ante, set_on, note) VALUES (?,?,?,?)",
        (float(member_ante), float(guest_ante), set_on, note))


def stake_history(conn):
    return conn.execute(
        "SELECT * FROM stakes ORDER BY set_on DESC, stake_id DESC").fetchall()


def round_stake(conn, round_id: int):
    """The rate a given round was played at, not today's rate."""
    from cartel_config import LEGACY_STAKE, Stake
    row = conn.execute(
        "SELECT member_ante, guest_ante FROM rounds WHERE round_id = ?",
        (round_id,)).fetchone()
    if row is None or row["member_ante"] is None:
        return LEGACY_STAKE
    return Stake(member_ante=row["member_ante"], guest_ante=row["guest_ante"])


def create_round(conn, played_on: str, course: str, round_no: int | None = None,
                 carry_in: float = 0.0, status: str = "draft") -> int:
    cur = conn.execute(
        """INSERT INTO rounds (round_no, played_on, course, status, carry_in)
           VALUES (?,?,?,?,?)
           ON CONFLICT(played_on, course) DO UPDATE SET round_no=excluded.round_no
           RETURNING round_id""",
        (round_no, played_on, course, status, carry_in),
    )
    return cur.fetchone()["round_id"]


def get_round(conn, round_id: int):
    return conn.execute("SELECT * FROM rounds WHERE round_id = ?", (round_id,)).fetchone()


def list_rounds(conn, limit: int = 50, exclude_legacy: bool = False):
    sql = """SELECT r.*, COUNT(e.name) AS n_posted
             FROM rounds r LEFT JOIN entries e ON e.round_id = r.round_id"""
    if exclude_legacy:
        sql += " WHERE r.status != 'legacy'"
    sql += " GROUP BY r.round_id ORDER BY r.played_on DESC, r.round_id DESC LIMIT ?"
    return conn.execute(sql, (limit,)).fetchall()


def last_posted_round(conn):
    """
    The most recent settled round. Reports about the CURRENT STATE of the data
    are named after this, not after today's date - otherwise the same report
    generated on three different days produces three filenames and identical
    content, which makes it look like three different reports.
    """
    return conn.execute(
        """SELECT round_id, round_no, played_on, course FROM rounds
           WHERE status = 'posted'
           ORDER BY played_on DESC, round_id DESC LIMIT 1"""
    ).fetchone()


def anchor_tag(conn) -> str:
    """A filename fragment identifying the data's state, e.g. 'R61_2026-08-01'."""
    r = last_posted_round(conn)
    if r is None:
        return "no_rounds_yet"
    return f"R{r['round_no'] or r['round_id']}_{r['played_on']}"


def pending_carry(conn, before: str | None = None,
                  exclude_round_id: int | None = None) -> float:
    """
    Money held over from the last settled round.

    `before` and `exclude_round_id` matter because scoresheets get printed out of
    order - Sunday's sheet often comes off the printer before Thursday's scores
    are entered. Resolving the carry against the round's own date, rather than
    against whatever happened to be settled at the time, keeps the chain intact.
    """
    sql = "SELECT carried_out FROM rounds WHERE status='posted'"
    params: list = []
    if before:
        sql += " AND played_on < ?"
        params.append(before)
    if exclude_round_id is not None:
        sql += " AND round_id != ?"
        params.append(exclude_round_id)
    sql += " ORDER BY played_on DESC, round_id DESC LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    return row["carried_out"] if row else 0.0


def save_entries(conn, round_id: int, rows: list[dict]) -> None:
    conn.execute("DELETE FROM entries WHERE round_id = ?", (round_id,))
    conn.executemany(
        """INSERT INTO entries
             (round_id, name, team_no, tee_time, quota, is_guest, played,
              points_front, points_back, score, greens, skins)
           VALUES (:round_id,:name,:team_no,:tee_time,:quota,:is_guest,:played,
                   :points_front,:points_back,:score,:greens,:skins)""",
        [{"round_id": round_id, "tee_time": None, "quota": None, "is_guest": 0,
          "played": 1, "points_front": None, "points_back": None, "score": None,
          "greens": 0, "skins": 0, **r}
         for r in rows],
    )


def add_entry(conn, round_id: int, name: str, team_no: int,
              quota: int | None, is_guest: bool = False) -> None:
    """
    Add one player to a round that already exists.

    For the man who turns up late, or was left off the sheet. Adding him here
    beats rebuilding the round, which would throw away everyone else's scores.
    """
    conn.execute(
        """INSERT INTO entries (round_id, name, team_no, quota, is_guest, played)
           VALUES (?,?,?,?,?,1)
           ON CONFLICT(round_id, name) DO UPDATE SET
             team_no = excluded.team_no""",
        (round_id, name.strip(), int(team_no), quota, int(is_guest)))


def load_entries(conn, round_id: int):
    return conn.execute(
        "SELECT * FROM entries WHERE round_id = ? ORDER BY team_no, name", (round_id,)
    ).fetchall()


def post_round(conn, round_id: int, result) -> None:
    conn.execute("DELETE FROM side_outcomes WHERE round_id = ?", (round_id,))
    conn.executemany(
        """INSERT INTO side_outcomes (round_id, side, team_no, points, quota, net, is_winner)
           VALUES (?,?,?,?,?,?,?)""",
        [(round_id, s.side, s.team_no, s.points, s.quota, s.net, int(s.is_winner))
         for s in result.sides],
    )
    conn.execute("DELETE FROM payouts WHERE round_id = ?", (round_id,))
    conn.executemany(
        """INSERT INTO payouts (round_id, name, team_money, skat_money, ante, is_guest)
           VALUES (?,?,?,?,?,?)""",
        [(round_id, p.name, p.team_money, p.skat_money, p.ante, int(p.is_guest))
         for p in result.payouts.values()],
    )
    conn.execute(
        "UPDATE rounds SET status='posted', carried_out=? WHERE round_id=?",
        (result.carried_money, round_id),
    )


def unpost_round(conn, round_id: int) -> None:
    conn.execute("DELETE FROM side_outcomes WHERE round_id = ?", (round_id,))
    conn.execute("DELETE FROM payouts WHERE round_id = ?", (round_id,))
    conn.execute("UPDATE rounds SET status='draft', carried_out=0 WHERE round_id=?", (round_id,))
