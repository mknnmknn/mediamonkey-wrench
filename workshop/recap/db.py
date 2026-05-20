"""
SQLite connection setup + OLE date helpers + the aux/PlayedAll plumbing that
lets the rest of the engine query a unified play stream (MM5 plays ∪ Last.fm
scrobbles) via the read-only MM5 connection.
"""
from __future__ import annotations

import datetime
import sqlite3


OLE_EPOCH = datetime.datetime(1899, 12, 30)


def dt_to_ole(dt: datetime.datetime) -> float:
    """Convert a naive datetime to MM5's OLE-date (days since 1899-12-30)."""
    return (dt - OLE_EPOCH).total_seconds() / 86400.0


def ole_to_dt(o: float) -> datetime.datetime:
    """Inverse of dt_to_ole."""
    return OLE_EPOCH + datetime.timedelta(days=o)


def open_connection(db_path: str) -> sqlite3.Connection:
    """
    Open MM5.DB read-only, attach an in-memory aux DB for Last.fm extras,
    and prepare the aux schema. The PlayedAll view is created later (after
    Last.fm has populated aux.LastfmExtras) via create_played_all_view().
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.create_collation(
        "IUNICODE",
        lambda a, b: (a.casefold() > b.casefold()) - (a.casefold() < b.casefold()),
    )
    con.row_factory = sqlite3.Row
    con.executescript("""
        ATTACH DATABASE ':memory:' AS aux;
        CREATE TABLE aux.LastfmExtras (
            IDSong   INTEGER NOT NULL,
            PlayDate REAL    NOT NULL
        );
    """)
    return con


def create_played_all_view(con: sqlite3.Connection) -> None:
    """
    (Re)create PlayedAll as the UNION of Played (MM5) and aux.LastfmExtras.
    Call this after Last.fm extras have been inserted and before running
    aggregations. Safe to call repeatedly.
    """
    con.executescript("""
        DROP VIEW IF EXISTS PlayedAll;
        CREATE TEMP VIEW PlayedAll AS
          SELECT IDSong, PlayDate FROM Played
          UNION ALL
          SELECT IDSong, PlayDate FROM aux.LastfmExtras
        ;
    """)


def q(con: sqlite3.Connection, sql: str, params=()) -> list[sqlite3.Row]:
    """Execute + fetchall, sugar."""
    return con.execute(sql, params).fetchall()
