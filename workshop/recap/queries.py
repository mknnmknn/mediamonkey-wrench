"""
SQL aggregations against PlayedAll. Every function takes an open connection
plus the window's OLE bounds; PlayedAll must exist (call
db.create_played_all_view first).

Returns rows are sqlite3.Row objects; the renderer converts to dicts and
attaches link/art metadata.
"""
from __future__ import annotations

import sqlite3
from typing import Iterable


def stats(con: sqlite3.Connection, start_ole: float, end_ole: float) -> sqlite3.Row:
    return con.execute("""
        SELECT COUNT(*) AS plays,
               COUNT(DISTINCT p.IDSong) AS unique_tracks,
               COUNT(DISTINCT s.Artist) AS unique_artists,
               SUM(s.SongLength)/1000.0/60.0 AS minutes
        FROM PlayedAll p JOIN Songs s ON s.ID = p.IDSong
        WHERE p.PlayDate >= ? AND p.PlayDate < ?
    """, (start_ole, end_ole)).fetchone()


def busiest_day(con: sqlite3.Connection, start_ole: float, end_ole: float) -> sqlite3.Row:
    return con.execute("""
        SELECT date(datetime((PlayDate - 25569)*86400, 'unixepoch', 'localtime')) AS d, COUNT(*) c
        FROM PlayedAll WHERE PlayDate >= ? AND PlayDate < ?
        GROUP BY d ORDER BY 2 DESC LIMIT 1
    """, (start_ole, end_ole)).fetchone()


def baseline_total_12mo(con: sqlite3.Connection, baseline_start_ole: float, start_ole: float) -> int:
    row = con.execute("""
        SELECT COUNT(*) FROM PlayedAll WHERE PlayDate >= ? AND PlayDate < ?
    """, (baseline_start_ole, start_ole)).fetchone()
    return row[0] or 0


def top_artists(con: sqlite3.Connection, start_ole: float, end_ole: float,
                limit: int = 10) -> list[sqlite3.Row]:
    return con.execute("""
        SELECT s.Artist, COUNT(*) AS plays, COUNT(DISTINCT s.ID) AS uniq
        FROM PlayedAll p JOIN Songs s ON s.ID = p.IDSong
        WHERE p.PlayDate >= ? AND p.PlayDate < ?
          AND s.Artist NOT IN ('Various Artists', 'Various', 'VA')
        GROUP BY s.Artist ORDER BY 2 DESC, 3 DESC LIMIT ?
    """, (start_ole, end_ole, limit)).fetchall()


def top_albums(con: sqlite3.Connection, start_ole: float, end_ole: float,
               limit: int = 11) -> list[sqlite3.Row]:
    """Ranked by plays * tracks_played — rewards both depth and breadth."""
    return con.execute("""
        SELECT COALESCE(NULLIF(s.AlbumArtist,''), s.Artist) AS art,
               s.Album AS album,
               COUNT(*) AS plays,
               COUNT(DISTINCT s.ID) AS tracks_played
        FROM PlayedAll p JOIN Songs s ON s.ID = p.IDSong
        WHERE p.PlayDate >= ? AND p.PlayDate < ?
          AND s.Album IS NOT NULL AND s.Album <> ''
          AND s.Album NOT LIKE '%Podcast%'
          AND s.Album NOT LIKE '%Song of the Day%'
          AND s.Album NOT LIKE '%KEXP%'
        GROUP BY art, album
        ORDER BY (COUNT(*) * COUNT(DISTINCT s.ID)) DESC, COUNT(*) DESC
        LIMIT ?
    """, (start_ole, end_ole, limit)).fetchall()


def top_tracks(con: sqlite3.Connection, start_ole: float, end_ole: float,
               limit: int = 60) -> list[sqlite3.Row]:
    return con.execute("""
        SELECT s.ID, s.Artist, COALESCE(NULLIF(s.AlbumArtist,''), s.Artist) AS album_artist,
               s.SongTitle, s.Album, s.Rating, COUNT(*) AS plays
        FROM PlayedAll p JOIN Songs s ON s.ID = p.IDSong
        WHERE p.PlayDate >= ? AND p.PlayDate < ?
        GROUP BY p.IDSong ORDER BY 7 DESC, s.Rating DESC LIMIT ?
    """, (start_ole, end_ole, limit)).fetchall()


def first_encounters(con: sqlite3.Connection, start_ole: float, end_ole: float,
                     limit: int = 5) -> list[sqlite3.Row]:
    """Artists whose first-ever play in the (merged) library happened this window."""
    return con.execute("""
        WITH first_artist_play AS (
          SELECT s.Artist AS art, MIN(p.PlayDate) AS first_play
          FROM PlayedAll p JOIN Songs s ON s.ID = p.IDSong
          WHERE s.Artist IS NOT NULL AND s.Artist <> ''
            AND s.Artist NOT IN ('Various Artists','Various','VA')
          GROUP BY s.Artist
        )
        SELECT fap.art,
               (SELECT COUNT(*) FROM PlayedAll p2 JOIN Songs s2 ON s2.ID=p2.IDSong
                WHERE s2.Artist = fap.art AND p2.PlayDate >= ? AND p2.PlayDate < ?) AS plays,
               (SELECT COUNT(DISTINCT p2.IDSong) FROM PlayedAll p2 JOIN Songs s2 ON s2.ID=p2.IDSong
                WHERE s2.Artist = fap.art AND p2.PlayDate >= ? AND p2.PlayDate < ?) AS tracks
        FROM first_artist_play fap
        WHERE fap.first_play >= ? AND fap.first_play < ?
        ORDER BY 2 DESC, 1
        LIMIT ?
    """, (start_ole, end_ole, start_ole, end_ole, start_ole, end_ole, limit)).fetchall()


def artist_albums_in_window(con: sqlite3.Connection, start_ole: float, end_ole: float,
                            artist: str) -> list[sqlite3.Row]:
    """Albums an artist's plays in this window touched, by play count."""
    return con.execute("""
        SELECT s.Album, COUNT(*) AS p, COUNT(DISTINCT s.ID) AS t
        FROM PlayedAll p JOIN Songs s ON s.ID = p.IDSong
        WHERE s.Artist = ? AND p.PlayDate >= ? AND p.PlayDate < ?
          AND s.Album IS NOT NULL AND s.Album <> ''
        GROUP BY s.Album ORDER BY 2 DESC
    """, (artist, start_ole, end_ole)).fetchall()


def five_star_total(con: sqlite3.Connection, start_ole: float, end_ole: float) -> int:
    row = con.execute("""
        SELECT COUNT(DISTINCT p.IDSong) FROM PlayedAll p JOIN Songs s ON s.ID = p.IDSong
        WHERE p.PlayDate >= ? AND p.PlayDate < ? AND s.Rating = 100
    """, (start_ole, end_ole)).fetchone()
    return row[0] or 0


def five_star(con: sqlite3.Connection, start_ole: float, end_ole: float,
              limit: int = 5) -> list[sqlite3.Row]:
    """
    5★ tracks in window, sorted by longest-prior-absence first (rare surfaces
    from the canon float to the top). NULL prior (first time on record)
    ranks last so it doesn't dominate.
    """
    return con.execute("""
        WITH win AS (
          SELECT IDSong, MIN(PlayDate) AS first_in_win
          FROM PlayedAll WHERE PlayDate >= ? AND PlayDate < ? GROUP BY IDSong
        ),
        prior AS (
          SELECT w.IDSong, MAX(p.PlayDate) AS last_before
          FROM win w LEFT JOIN PlayedAll p
            ON p.IDSong = w.IDSong AND p.PlayDate < w.first_in_win
          GROUP BY w.IDSong
        )
        SELECT s.ID, s.Artist, COALESCE(NULLIF(s.AlbumArtist,''), s.Artist) AS album_artist,
               s.SongTitle, s.Album, pr.last_before AS last_before_ole
        FROM win w JOIN prior pr ON pr.IDSong=w.IDSong JOIN Songs s ON s.ID=w.IDSong
        WHERE s.Rating = 100
        ORDER BY (CASE WHEN pr.last_before IS NULL THEN 1 ELSE 0 END), pr.last_before ASC
        LIMIT ?
    """, (start_ole, end_ole, limit)).fetchall()


def comeback(con: sqlite3.Connection, start_ole: float, end_ole: float,
             year_before_ole: float, limit: int = 10) -> list[sqlite3.Row]:
    """Tracks whose most recent prior play was >1 year before the window."""
    return con.execute("""
        WITH win_plays AS (
          SELECT IDSong, MIN(PlayDate) AS first_in_win, COUNT(*) AS plays_in_win
          FROM PlayedAll WHERE PlayDate >= ? AND PlayDate < ? GROUP BY IDSong
        ),
        prior AS (
          SELECT w.IDSong, MAX(p.PlayDate) AS last_before
          FROM win_plays w JOIN PlayedAll p ON p.IDSong = w.IDSong
          WHERE p.PlayDate < w.first_in_win
          GROUP BY w.IDSong
        )
        SELECT s.ID, s.Artist, COALESCE(NULLIF(s.AlbumArtist,''), s.Artist) AS album_artist,
               s.SongTitle, s.Album, s.Rating,
               w.plays_in_win AS plays,
               (w.first_in_win - pr.last_before) AS gap_days,
               pr.last_before AS last_before_ole
        FROM win_plays w JOIN prior pr ON pr.IDSong = w.IDSong JOIN Songs s ON s.ID = w.IDSong
        WHERE pr.last_before < ?
        ORDER BY 8 DESC, 7 DESC LIMIT ?
    """, (start_ole, end_ole, year_before_ole, limit)).fetchall()


def deep_dive_candidate_rows(con: sqlite3.Connection, start_ole: float, end_ole: float,
                             baseline_start_ole: float) -> list[sqlite3.Row]:
    """
    Raw (art, w_plays, w_tracks, b_plays) candidates with w_plays >= 15.
    Caller filters by ratio/baseline and decides selection.
    """
    return con.execute("""
        WITH win AS (
          SELECT s.Artist AS art, COUNT(*) AS w_plays, COUNT(DISTINCT s.ID) AS w_tracks
          FROM PlayedAll p JOIN Songs s ON s.ID = p.IDSong
          WHERE p.PlayDate >= ? AND p.PlayDate < ?
            AND s.Artist NOT IN ('Various Artists','Various','VA')
          GROUP BY s.Artist
        ),
        base AS (
          SELECT s.Artist AS art, COUNT(*) AS b_plays
          FROM PlayedAll p JOIN Songs s ON s.ID = p.IDSong
          WHERE p.PlayDate >= ? AND p.PlayDate < ?
          GROUP BY s.Artist
        )
        SELECT win.art, win.w_plays, win.w_tracks, COALESCE(base.b_plays,0) AS b_plays
        FROM win LEFT JOIN base USING(art)
        WHERE win.w_plays >= 15
    """, (start_ole, end_ole, baseline_start_ole, start_ole)).fetchall()


def deep_dive_artist_albums(con: sqlite3.Connection, start_ole: float, end_ole: float,
                            artist: str) -> list[sqlite3.Row]:
    """Album breakdown for a single deep-dive artist (Podcast/KEXP filtered)."""
    return con.execute("""
        SELECT COALESCE(NULLIF(s.AlbumArtist,''), s.Artist) AS art_credit,
               s.Album AS album, COUNT(*) AS plays, COUNT(DISTINCT s.ID) AS tracks
        FROM PlayedAll p JOIN Songs s ON s.ID = p.IDSong
        WHERE p.PlayDate >= ? AND p.PlayDate < ?
          AND s.Artist = ?
          AND s.Album IS NOT NULL AND s.Album <> ''
          AND s.Album NOT LIKE '%Podcast%' AND s.Album NOT LIKE '%KEXP%'
        GROUP BY art_credit, album ORDER BY 3 DESC, 4 DESC
    """, (start_ole, end_ole, artist)).fetchall()
