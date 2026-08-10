"""
Automatic backups of the database.

The whole history lives in one file. Losing it loses four and a half years of
golf, so a copy is taken automatically whenever anything important changes -
not when somebody remembers.

Where it goes
-------------
By default, a folder called DB_Backup NEXT TO the app folder:

    C:\\Cartel\\cartel-app\\      the app
    C:\\Cartel\\DB_Backup\\       the backups

Deliberately outside the app folder. Updates are installed by extracting a zip
over cartel-app, and a backup that lives inside the thing being overwritten is
not a backup. Override with CARTEL_BACKUP_DIR.

How the copy is made
--------------------
Through SQLite's own backup API rather than copying the file. A plain file copy
of a database that something else has open can capture a half-written page; the
backup API takes a consistent snapshot regardless.

When it runs
------------
  - after a round is posted          (the moment there is new money to lose)
  - after the history import         (a clean starting point to fall back to)
  - whenever asked, from the Health tab

Hosted Postgres is skipped: there is no local file, and Supabase runs its own.
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import cartel_db as db
from cartel_config import db_path

KEEP = int(os.environ.get("CARTEL_BACKUP_KEEP", "60"))
STAMP = re.compile(r"^cartel_(\d{4}-\d{2}-\d{2})_")


@dataclass
class BackupResult:
    path: Path | None
    reason: str
    skipped: str | None = None
    pruned: int = 0

    @property
    def ok(self) -> bool:
        return self.path is not None


def backup_dir() -> Path:
    """DB_Backup beside the app folder, unless told otherwise."""
    override = os.environ.get("CARTEL_BACKUP_DIR")
    if override:
        return Path(override)
    app_root = Path(__file__).resolve().parent.parent      # .../cartel-app
    return app_root.parent / "DB_Backup"                   # .../DB_Backup


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower() or "manual"


def make_backup(reason: str = "manual", when: str | None = None,
                keep: int = KEEP) -> BackupResult:
    """
    Take a snapshot. Never raises: a failed backup must not stop a round being
    settled, it just has to say so.

    `when` is the date the backup is ABOUT (the round's date), not the clock.
    Two backups of the same round on the same day overwrite rather than pile up.
    """
    if db.is_postgres():
        return BackupResult(None, reason, skipped="hosted database - Supabase backs itself up")

    source = Path(db_path())
    if not source.exists():
        return BackupResult(None, reason, skipped="no database file yet")

    try:
        target_dir = backup_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = when or datetime.now().strftime("%Y-%m-%d")
        target = target_dir / f"cartel_{stamp}_{_slug(reason)}.db"

        src = sqlite3.connect(str(source))
        try:
            dst = sqlite3.connect(str(target))
            try:
                src.backup(dst)          # consistent snapshot, not a file copy
            finally:
                dst.close()
        finally:
            src.close()

        return BackupResult(target, reason, pruned=prune(keep))
    except Exception as exc:
        return BackupResult(None, reason, skipped=f"could not write a backup ({exc})")


def _order_key(f: Path):
    """
    Newest first, deterministically.

    Sorting on modification time alone is not safe. Windows records file times
    at a coarse resolution, so several backups written in one sitting - which is
    exactly what happens when a weekend's two rounds are entered together - can
    share an identical timestamp. Python's sort is stable, so tied files then
    come back in directory order, which on Windows is alphabetical: OLDEST
    first. Pruning would then delete the newest backups and keep the oldest,
    which is the worst thing a backup system can do.

    The filename carries the round date, so it breaks the tie meaningfully.
    """
    return (f.stat().st_mtime, f.name)


def list_backups() -> list[Path]:
    """Newest first."""
    d = backup_dir()
    if not d.exists():
        return []
    return sorted(d.glob("cartel_*.db"), key=_order_key, reverse=True)


def prune(keep: int = KEEP) -> int:
    """Drop the oldest once there are more than `keep`. Files are ~0.3 MB."""
    existing = list_backups()
    removed = 0
    for old in existing[keep:]:
        try:
            old.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def restore(backup: str | Path, confirm: bool = False) -> Path:
    """
    Put a backup back, having first set the current database aside.

    Refuses without confirm=True, because this replaces live data. The database
    it displaces is kept as ...before-restore..., so a restore can itself be
    undone - the mistake people actually make is restoring the wrong file.
    """
    if not confirm:
        raise ValueError(
            "restore() replaces the live database. Pass confirm=True once you "
            "are sure which backup you want.")

    backup = Path(backup)
    if not backup.exists():
        raise FileNotFoundError(backup)

    live = Path(db_path())
    if live.exists():
        aside = backup_dir() / (
            f"cartel_{datetime.now().strftime('%Y-%m-%d-%H%M%S')}_before-restore.db")
        aside.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(live, aside)

    live.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup, live)
    return live
