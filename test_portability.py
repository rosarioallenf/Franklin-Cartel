"""
SQL that works on SQLite but not on Postgres.

The app runs on a file locally and on hosted Postgres for the group. Three
things have already broken only on Postgres, each one silent until deployment:

  - a HAVING clause referencing a SELECT alias (SQLite allows it, Postgres
    requires the expression)
  - unquoted column aliases, which Postgres folds to lower case
  - a SQLite-only PRAGMA used to inspect a table, which on Postgres aborts the
    whole transaction and buries the real error

These check the source rather than needing a live Postgres, so they run
everywhere - including on Allen's machine, before he deploys.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SOURCES = [ROOT / "app.py"] + sorted((ROOT / "cartel").glob("*.py")) \
    + sorted((ROOT / "scripts").glob("*.py"))

SQL_BLOCK = re.compile(r'"""(.*?)"""', re.S)


def sql_blocks():
    for path in SOURCES:
        text = path.read_text()
        for block in SQL_BLOCK.findall(text):
            if re.search(r"\bSELECT\b", block, re.I):
                yield path.name, block


def test_having_never_references_a_select_alias():
    """
    Postgres evaluates HAVING before the SELECT list exists, so an alias there
    is an error. SQLite accepts it, so this only ever breaks in production.
    """
    offenders = []
    for name, block in sql_blocks():
        m = re.search(r"\bHAVING\b(.*?)(?:\bORDER\b|\bLIMIT\b|$)", block, re.I | re.S)
        if not m:
            continue
        clause = m.group(1)
        aliases = re.findall(r'\bAS\s+"?(\w+)"?', block, re.I)
        for alias in aliases:
            if re.search(rf'\b{re.escape(alias)}\b', clause):
                offenders.append(f"{name}: HAVING references alias {alias!r}")
    assert not offenders, "; ".join(offenders)


def test_every_column_alias_is_quoted():
    """
    Postgres folds unquoted identifiers to lower case, so `AS Date` comes back
    as `date` and anything matching on the name breaks.
    """
    offenders = []
    for name, block in sql_blocks():
        for alias in re.findall(r"\bAS\s+([A-Za-z_]\w*)", block):
            if alias.upper() in ("IDENTITY", "SELECT"):
                continue
            # An already-lower-case alias is safe: folding it changes nothing.
            # Only mixed or upper case can come back differently.
            if alias == alias.lower():
                continue
            offenders.append(f"{name}: unquoted alias {alias!r}")
    assert not offenders, ("quote these, e.g. AS \"Date\": " + "; ".join(offenders))


def test_sqlite_only_pragmas_are_guarded_by_a_dialect_check():
    """
    A PRAGMA sent to Postgres aborts the transaction, so every later statement
    fails with 'current transaction is aborted' and the real cause is hidden.
    """
    for path in SOURCES:
        text = path.read_text()
        if "PRAGMA" not in text:
            continue
        assert "is_postgres()" in text, (
            f"{path.name} uses a PRAGMA without checking the dialect first")


def test_the_migration_covers_every_table_in_the_schema():
    """
    A table added to the schema but not to MIGRATE_TABLES is silently left
    behind on deployment. The stake history nearly went that way.
    """
    source = (ROOT / "cartel" / "storage.py").read_text()
    # Read the SCHEMA constant itself. Scanning the whole file also matches the
    # comment explaining what CREATE TABLE IF NOT EXISTS does, which is prose.
    schema = re.search(r'SCHEMA = """(.*?)"""', source, re.S).group(1)
    tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", schema))
    cli = (ROOT / "scripts" / "cartel_cli.py").read_text()
    listed = set(re.findall(r'"(\w+)"', re.search(
        r"MIGRATE_TABLES = \[(.*?)\]", cli, re.S).group(1)))
    missing = tables - listed
    assert not missing, f"not copied on deployment: {', '.join(sorted(missing))}"
