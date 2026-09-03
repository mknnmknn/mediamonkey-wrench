"""
Per-artist queries for the Artist page.

Two interaction modes flow through the page:
  exact    — Songs.Artist string equals the canonical name (solo work only).
  anywhere — Songs.Artist string CONTAINS the canonical name (catches "X & Y",
             "Y & X", featured-credits Daniel has promoted into the artist
             field, etc.). LIKE-based with ASCII-case-insensitive collation.

All aggregations run against PlayedAll (MM5 plays UNION Last.fm scrobble extras),
the same source the recap engine uses — so the merged-listening counts here
match the recap page exactly.
"""
from __future__ import annotations

import datetime
import sqlite3

from .db import ole_to_local_dt
from .normalize import normalize


def _filter(mode: str) -> tuple[str, callable]:
    """
    Returns (sql_fragment_for_after_Artist, param_builder). Used as:
        f"WHERE s.Artist {frag}", (param_builder(name),)
    """
    if mode == "anywhere":
        return "LIKE ? COLLATE NOCASE", lambda name: f"%{name}%"
    return "= ?", lambda name: name


# --------------------------------------------------------------------------
#  Search
# --------------------------------------------------------------------------

def search_artists(con: sqlite3.Connection, query: str, *,
                   mode: str = "exact", limit: int = 50) -> list[dict]:
    """
    Find canonical Artist values matching the query (unicode-aware via
    normalize()) and aggregate their basic stats. Returns rows with:
      name, plays, unique_tracks, first_played (date|None), most_recent (date|None)
    """
    nq = normalize(query)
    if not nq:
        return []
    all_artists = con.execute(
        "SELECT DISTINCT Artist FROM Songs "
        "WHERE Artist IS NOT NULL AND Artist <> ''"
    ).fetchall()
    if mode == "exact":
        matched = [r[0] for r in all_artists if normalize(r[0]) == nq]
    else:
        matched = [r[0] for r in all_artists if nq in normalize(r[0])]
    if not matched:
        return []

    placeholders = ",".join(["?"] * len(matched))
    rows = con.execute(f"""
        SELECT s.Artist AS name,
               COUNT(*)              AS plays,
               COUNT(DISTINCT s.ID)  AS unique_tracks,
               MIN(p.PlayDate)       AS first_ole,
               MAX(p.PlayDate)       AS last_ole
        FROM PlayedAll p JOIN Songs s ON s.ID = p.IDSong
        WHERE s.Artist IN ({placeholders})
        GROUP BY s.Artist
        ORDER BY plays DESC, s.Artist
        LIMIT ?
    """, [*matched, limit]).fetchall()

    out = []
    for r in rows:
        out.append({
            "name":          r["name"],
            "plays":         r["plays"],
            "unique_tracks": r["unique_tracks"],
            "first_played":  ole_to_local_dt(r["first_ole"]).date() if r["first_ole"] else None,
            "most_recent":   ole_to_local_dt(r["last_ole"]).date()  if r["last_ole"]  else None,
        })
    return out


# --------------------------------------------------------------------------
#  Detail-page queries
# --------------------------------------------------------------------------

def overview(con: sqlite3.Connection, name: str, mode: str) -> dict | None:
    op, getp = _filter(mode)
    row = con.execute(f"""
        SELECT COUNT(*)             AS plays,
               COUNT(DISTINCT s.ID) AS unique_tracks,
               COUNT(DISTINCT s.Album) AS unique_albums,
               MIN(p.PlayDate) AS first_ole,
               MAX(p.PlayDate) AS last_ole,
               SUM(s.SongLength)/1000.0/60.0 AS minutes,
               COUNT(DISTINCT CASE WHEN s.Rating = 100 THEN s.ID END) AS five_star_tracks
        FROM PlayedAll p JOIN Songs s ON s.ID = p.IDSong
        WHERE s.Artist {op}
    """, (getp(name),)).fetchone()
    if not row or not row["plays"]:
        return None
    return {
        "plays":            row["plays"],
        "unique_tracks":    row["unique_tracks"],
        "unique_albums":    row["unique_albums"],
        "first_played":     ole_to_local_dt(row["first_ole"]).date() if row["first_ole"] else None,
        "most_recent":      ole_to_local_dt(row["last_ole"]).date()  if row["last_ole"]  else None,
        "minutes":          row["minutes"] or 0,
        "five_star_tracks": row["five_star_tracks"] or 0,
    }


def plays_over_time(con: sqlite3.Connection, name: str, mode: str,
                    granularity: str = "auto") -> tuple[str, list[dict]]:
    """
    Returns (granularity_used, buckets) where each bucket is
      {"label": str, "period_start": date, "plays": int}

    granularity ∈ {'month', 'week', 'auto'}. Auto picks weekly when the
    artist's total span is ≤ 6 months, else monthly.
    """
    op, getp = _filter(mode)
    span = con.execute(f"""
        SELECT MIN(p.PlayDate) AS first_ole, MAX(p.PlayDate) AS last_ole
        FROM PlayedAll p JOIN Songs s ON s.ID = p.IDSong
        WHERE s.Artist {op}
    """, (getp(name),)).fetchone()
    if not span or not span["first_ole"]:
        return "month", []
    first_dt = ole_to_local_dt(span["first_ole"])
    last_dt  = ole_to_local_dt(span["last_ole"])
    span_days = (last_dt - first_dt).days

    if granularity == "auto":
        granularity = "week" if span_days <= 180 else "month"

    # OLE -> datetime in SQL: (PlayDate - 25569) seconds-from-epoch, then
    # 'localtime' so buckets land on local calendar days, not UTC ones.
    fmt = "%Y-%m" if granularity == "month" else "%Y-%W"
    rows = con.execute(f"""
        SELECT strftime(?, datetime((p.PlayDate - 25569)*86400, 'unixepoch', 'localtime')) AS bucket,
               COUNT(*) AS plays,
               MIN(p.PlayDate) AS first_in_bucket
        FROM PlayedAll p JOIN Songs s ON s.ID = p.IDSong
        WHERE s.Artist {op}
        GROUP BY bucket
        ORDER BY bucket
    """, (fmt, getp(name))).fetchall()

    buckets: list[dict] = []
    for r in rows:
        first = ole_to_local_dt(r["first_in_bucket"])
        if granularity == "month":
            label = first.strftime("%b %Y")
            period_start = first.replace(day=1).date()
        else:
            # Week bucket — label as the Monday of that week
            monday = first - datetime.timedelta(days=first.weekday())
            label  = monday.strftime("%b %-d") if hasattr(monday, "isoformat") else monday.strftime("%b %d")
            period_start = monday.date()
        buckets.append({"label": label, "period_start": period_start, "plays": r["plays"]})

    # Fill gaps with zero-play buckets so the chart shape is honest
    buckets = _fill_gaps(buckets, granularity)
    return granularity, buckets


def _fill_gaps(buckets: list[dict], granularity: str) -> list[dict]:
    if not buckets:
        return []
    filled: list[dict] = []
    cur = buckets[0]["period_start"]
    last = buckets[-1]["period_start"]
    idx = 0
    while cur <= last:
        if idx < len(buckets) and buckets[idx]["period_start"] == cur:
            filled.append(buckets[idx])
            idx += 1
        else:
            label = (cur.strftime("%b %Y") if granularity == "month"
                     else cur.strftime("%b %d"))
            filled.append({"label": label, "period_start": cur, "plays": 0})
        # advance one bucket
        if granularity == "month":
            cur = (cur.replace(day=1)
                   + datetime.timedelta(days=32)).replace(day=1)
        else:
            cur = cur + datetime.timedelta(days=7)
    return filled


def cumulative_unique_tracks(con: sqlite3.Connection, name: str, mode: str,
                             buckets: list[dict], granularity: str) -> list[int]:
    """
    Given the time-bucket sequence from plays_over_time(), return a parallel
    list of "cumulative distinct tracks discovered up to and including each
    bucket." A track is "discovered" in the bucket containing its first-ever
    play (within the matched artist scope).

    Curve is strictly non-decreasing; tells the catalog-exploration story.
    """
    if not buckets:
        return []
    op, getp = _filter(mode)
    fmt = "%Y-%m" if granularity == "month" else "%Y-%W"
    # Param order is the order `?` appears in the SQL text, not logical
    # execution order — the WHERE-clause artist comes first, then strftime fmt.
    rows = con.execute(f"""
        WITH firsts AS (
          SELECT s.ID, MIN(p.PlayDate) AS first_ole
          FROM PlayedAll p JOIN Songs s ON s.ID = p.IDSong
          WHERE s.Artist {op}
          GROUP BY s.ID
        )
        SELECT strftime(?, datetime((first_ole - 25569)*86400, 'unixepoch', 'localtime')) AS bucket,
               COUNT(*) AS new_tracks
        FROM firsts
        GROUP BY bucket
        ORDER BY bucket
    """, (getp(name), fmt)).fetchall()
    new_per_bucket = {r["bucket"]: r["new_tracks"] for r in rows}

    cum: list[int] = []
    running = 0
    for b in buckets:
        if granularity == "month":
            bkey = b["period_start"].strftime("%Y-%m")
        else:
            bkey = b["period_start"].strftime("%Y-%W")
        running += new_per_bucket.get(bkey, 0)
        cum.append(running)
    return cum


def top_tracks(con: sqlite3.Connection, name: str, mode: str,
               limit: int = 60) -> list[dict]:
    op, getp = _filter(mode)
    rows = con.execute(f"""
        SELECT s.ID, s.Artist, s.SongTitle, s.Album,
               COALESCE(NULLIF(s.AlbumArtist,''), s.Artist) AS album_artist,
               s.Rating, COUNT(*) AS plays
        FROM PlayedAll p JOIN Songs s ON s.ID = p.IDSong
        WHERE s.Artist {op}
        GROUP BY p.IDSong
        ORDER BY plays DESC, s.Rating DESC
        LIMIT ?
    """, (getp(name), limit)).fetchall()
    return [dict(r) for r in rows]


def top_albums(con: sqlite3.Connection, name: str, mode: str,
               limit: int = 12) -> list[dict]:
    op, getp = _filter(mode)
    rows = con.execute(f"""
        SELECT COALESCE(NULLIF(s.AlbumArtist,''), s.Artist) AS art_credit,
               s.Album AS album,
               COUNT(*) AS plays,
               COUNT(DISTINCT s.ID) AS tracks_played
        FROM PlayedAll p JOIN Songs s ON s.ID = p.IDSong
        WHERE s.Artist {op}
          AND s.Album IS NOT NULL AND s.Album <> ''
        GROUP BY art_credit, album
        ORDER BY (COUNT(*) * COUNT(DISTINCT s.ID)) DESC, COUNT(*) DESC
        LIMIT ?
    """, (getp(name), limit)).fetchall()
    return [dict(r) for r in rows]


def five_star_tracks(con: sqlite3.Connection, name: str, mode: str) -> list[dict]:
    op, getp = _filter(mode)
    rows = con.execute(f"""
        SELECT s.ID, s.Artist, s.SongTitle, s.Album,
               COALESCE(NULLIF(s.AlbumArtist,''), s.Artist) AS album_artist,
               COUNT(*) AS plays
        FROM PlayedAll p JOIN Songs s ON s.ID = p.IDSong
        WHERE s.Artist {op}
          AND s.Rating = 100
        GROUP BY p.IDSong
        ORDER BY plays DESC, s.SongTitle
    """, (getp(name),)).fetchall()
    return [dict(r) for r in rows]


def all_track_play_counts(con: sqlite3.Connection, name: str, mode: str) -> list[int]:
    """Just the play-count integers for every distinct track, sorted desc.
    Used for diversity statistics; no display columns needed."""
    op, getp = _filter(mode)
    rows = con.execute(f"""
        SELECT COUNT(*) AS plays
        FROM PlayedAll p JOIN Songs s ON s.ID = p.IDSong
        WHERE s.Artist {op}
        GROUP BY p.IDSong
        ORDER BY plays DESC
    """, (getp(name),)).fetchall()
    return [r[0] for r in rows]


def other_appearances(con: sqlite3.Connection, canonical_name: str,
                      limit: int = 30) -> list[dict]:
    """
    Tracks where this artist appears as part of a multi-artist credit. Only
    meaningful in 'anywhere' mode — when toggled on at the detail page, this
    section surfaces e.g. "Calexico & Iron & Wine — Two Silver Trees" for an
    Iron & Wine page.
    """
    rows = con.execute("""
        SELECT s.ID, s.Artist, s.SongTitle, s.Album,
               COUNT(*) AS plays
        FROM PlayedAll p JOIN Songs s ON s.ID = p.IDSong
        WHERE s.Artist LIKE ? COLLATE NOCASE
          AND s.Artist <> ?
        GROUP BY p.IDSong
        ORDER BY plays DESC, s.SongTitle
        LIMIT ?
    """, (f"%{canonical_name}%", canonical_name, limit)).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
#  Diversity / concentration stats
# --------------------------------------------------------------------------

def diversity(play_counts: list[int]) -> dict:
    """
    Given a descending list of per-track play counts, compute:
      - total_plays, total_tracks
      - top_5_pct, top_10_pct, top_20_pct (% of plays in those top-N tracks)
      - half_at_n (number of top tracks that contain 50% of plays)
    """
    total = sum(play_counts)
    n_tracks = len(play_counts)
    out = {"total_plays": total, "total_tracks": n_tracks,
           "top_5_pct": 0, "top_10_pct": 0, "top_20_pct": 0, "half_at_n": None}
    if total == 0 or n_tracks == 0:
        return out
    cum = 0
    for i, p in enumerate(play_counts):
        cum += p
        n = i + 1
        if n == 5:  out["top_5_pct"]  = cum * 100 / total
        if n == 10: out["top_10_pct"] = cum * 100 / total
        if n == 20: out["top_20_pct"] = cum * 100 / total
        if out["half_at_n"] is None and cum * 2 >= total:
            out["half_at_n"] = n
    # For very small catalogs the top-N% may not have been computed; clamp.
    if n_tracks < 5:  out["top_5_pct"]  = 100.0
    if n_tracks < 10: out["top_10_pct"] = 100.0 if out["top_10_pct"] == 0 else out["top_10_pct"]
    if n_tracks < 20: out["top_20_pct"] = 100.0 if out["top_20_pct"] == 0 else out["top_20_pct"]
    return out
