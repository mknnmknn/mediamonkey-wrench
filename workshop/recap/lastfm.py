"""
Last.fm scrobble backfill.

Two responsibilities:

  fetch_scrobbles(secrets, ...)         pull new pages from Last.fm, merge
                                        into a persistent local cache, return
                                        the full sorted scrobble list.

  merge_to_aux(con, lastfm_all, ...)    filter to the recap window, match each
                                        scrobble to a Songs.ID, dedup against
                                        in-window MM5 plays (variable window
                                        scaled by track length), and insert
                                        the survivors into aux.LastfmExtras
                                        for the PlayedAll view to pick up.

The variable-width dedup is the part that matters: MM5 logs PlayDate at ~80%
of the track and Last.fm scrobbles at end-of-track, so the gap scales with
track length. A flat 5-min window misses long tracks (e.g., 12-min jazz
quartet pieces) and double-counts them.
"""
from __future__ import annotations

import datetime
import json
import os
import time
import urllib.error
import urllib.parse

from .db import dt_to_ole, local_dt_to_ole
from .http import get_json
from .normalize import normalize


LASTFM_API = "https://ws.audioscrobbler.com/2.0/"


def _uts_to_naive_utc(uts: int) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(int(uts), tz=datetime.timezone.utc).replace(tzinfo=None)


def _extract_track(track: dict) -> dict | None:
    """Pull artist/title/album/uts from a Last.fm track entry; None for now-playing."""
    if (track.get("@attr") or {}).get("nowplaying") in ("true", True):
        return None
    uts = (track.get("date") or {}).get("uts")
    if not uts:
        return None
    return {
        "uts":    int(uts),
        "artist": (track.get("artist") or {}).get("#text", "") or "",
        "title":  track.get("name", "") or "",
        "album":  (track.get("album")  or {}).get("#text", "") or "",
    }


def fetch_scrobbles(*,
                    cache_path: str,
                    user: str | None,
                    api_key: str | None,
                    window_start: datetime.datetime,
                    backfill_years: int = 5,
                    page_sleep_sec: float = 0.25,
                    skip: bool = False,
                    log=print) -> list[dict]:
    """
    Extend the local scrobble cache forward. Returns the full list, sorted by uts.
    Skips with a log line if creds are missing or `skip=True`.
    """
    if skip:
        log("Last.fm: skipped via --skip-lastfm")
        return []
    if not user or not api_key or "your-lastfm" in user or "your-lastfm" in api_key:
        log("Last.fm: creds not set in secrets.json — skipping scrobble backfill")
        return []

    cache = (
        json.load(open(cache_path, encoding="utf-8"))
        if os.path.exists(cache_path) else {"scrobbles": []}
    )
    scrobbles: list[dict] = cache.get("scrobbles", [])

    if scrobbles:
        from_uts = max(s["uts"] for s in scrobbles) + 1
        log(f"Last.fm: {len(scrobbles):,} cached; fetching since "
            f"{_uts_to_naive_utc(from_uts).strftime('%Y-%m-%d %H:%M')} UTC")
    else:
        backfill_from = window_start - datetime.timedelta(days=365 * backfill_years)
        from_uts = int(backfill_from.timestamp())
        log(f"Last.fm: empty cache — backfilling from {backfill_from.strftime('%Y-%m-%d')} "
            f"(~{backfill_years} years)")

    seen_uts = {s["uts"] for s in scrobbles}
    page, fetched = 1, 0
    while True:
        qs = urllib.parse.urlencode({
            "method": "user.getrecenttracks",
            "user": user, "api_key": api_key, "format": "json",
            "limit": 200, "from": from_uts, "page": page,
        })
        try:
            data = get_json(f"{LASTFM_API}?{qs}", timeout=30)
        except urllib.error.HTTPError as e:
            log(f"  ! Last.fm HTTP {e.code}: {e.read().decode('utf-8','replace')[:200]}")
            break
        except Exception as e:
            log(f"  ! Last.fm fetch error on page {page}: {e}")
            break
        rt = (data or {}).get("recenttracks") or {}
        tracks = rt.get("track") or []
        if isinstance(tracks, dict):
            tracks = [tracks]
        total_pages = int((rt.get("@attr") or {}).get("totalPages") or 1)
        new_on_page = 0
        for tr in tracks:
            ext = _extract_track(tr)
            if not ext or ext["uts"] in seen_uts:
                continue
            scrobbles.append(ext)
            seen_uts.add(ext["uts"])
            new_on_page += 1
        fetched += new_on_page
        log(f"  page {page}/{total_pages}  (+{new_on_page} new, {fetched} total)")
        if page >= total_pages or not tracks:
            break
        page += 1
        time.sleep(page_sleep_sec)

    scrobbles.sort(key=lambda s: s["uts"])
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump({"scrobbles": scrobbles}, f, ensure_ascii=False)
    return scrobbles


def merge_all_to_aux(con,
                     lastfm_all: list[dict],
                     *,
                     dedup_window_sec_floor: int = 300,
                     dedup_len_buffer_sec:   int = 120,
                     log=print) -> dict:
    """
    All-time variant of merge_to_aux. Used by the artist page (and any other
    surface that wants historical stats rather than a single recap window).
    Same per-track dedup rules as the windowed variant.

    Returns: {"matched": int, "duplicate": int, "unmatched": int}
    """
    cur = con.cursor()

    song_idx: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for row in cur.execute(
        "SELECT ID, Artist, SongTitle, Album FROM Songs "
        "WHERE SongTitle IS NOT NULL AND Artist IS NOT NULL"
    ):
        a, t = normalize(row["Artist"]), normalize(row["SongTitle"])
        if not a or not t:
            continue
        song_idx.setdefault((a, t), []).append((row["ID"], normalize(row["Album"] or "")))

    mm5_play_idx: dict[int, list[float]] = {}
    for row in cur.execute("SELECT IDSong, PlayDate FROM Played"):
        mm5_play_idx.setdefault(row["IDSong"], []).append(row["PlayDate"])

    song_len_sec: dict[int, float] = {}
    for row in cur.execute("SELECT ID, SongLength FROM Songs WHERE SongLength > 0"):
        song_len_sec[row["ID"]] = row["SongLength"] / 1000.0

    diag = {"matched": 0, "duplicate": 0, "unmatched": 0}
    extras_rows: list[tuple[int, float]] = []
    for s in lastfm_all:
        key = (normalize(s["artist"]), normalize(s["title"]))
        candidates = song_idx.get(key)
        if not candidates:
            diag["unmatched"] += 1
            continue
        if len(candidates) > 1 and s["album"]:
            nalb = normalize(s["album"])
            best = [c for c in candidates if c[1] == nalb] or \
                   [c for c in candidates if c[1] and (c[1] in nalb or nalb in c[1])]
            song_id = (best or candidates)[0][0]
        else:
            song_id = candidates[0][0]
        scrobble_ole = dt_to_ole(_uts_to_naive_utc(s["uts"]))
        window_sec = max(
            dedup_window_sec_floor,
            song_len_sec.get(song_id, 0) + dedup_len_buffer_sec,
        )
        window_ole = window_sec / 86400.0
        if any(abs(p - scrobble_ole) < window_ole for p in mm5_play_idx.get(song_id, [])):
            diag["duplicate"] += 1
            continue
        diag["matched"] += 1
        extras_rows.append((song_id, scrobble_ole))

    if extras_rows:
        con.executemany(
            "INSERT INTO aux.LastfmExtras (IDSong, PlayDate) VALUES (?,?)",
            extras_rows,
        )
    log(f"Last.fm (all-time): {diag['matched']} merged · "
        f"{diag['duplicate']} dup of MM5 · {diag['unmatched']} unmatched")
    return diag


def merge_to_aux(con,
                 lastfm_all: list[dict],
                 *,
                 window_start: datetime.datetime,
                 window_end:   datetime.datetime,
                 dedup_window_sec_floor: int = 300,
                 dedup_len_buffer_sec:   int = 120,
                 log=print) -> dict:
    """
    Match each in-window scrobble to a Songs.ID, dedup against MM5 plays, and
    INSERT the survivors into aux.LastfmExtras. Returns diagnostics:

      {"matched": int, "duplicate": int, "unmatched": int,
       "unmatched_examples": list[str], "in_window": int}
    """
    # Window boundaries are local; PlayDate is UTC.
    start_ole = local_dt_to_ole(window_start)
    end_ole   = local_dt_to_ole(window_end)
    win_start_uts = int(window_start.timestamp())
    win_end_uts   = int(window_end.timestamp())

    window_scrobbles = [s for s in lastfm_all if win_start_uts <= s["uts"] < win_end_uts]
    log(f"Last.fm: {len(window_scrobbles):,} scrobbles in window")
    diag = {
        "in_window": len(window_scrobbles),
        "matched": 0, "duplicate": 0, "unmatched": 0,
        "unmatched_examples": [],
    }
    if not window_scrobbles:
        return diag

    cur = con.cursor()

    # (norm_artist, norm_title) -> [(SongID, norm_album), ...]
    song_idx: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for row in cur.execute(
        "SELECT ID, Artist, SongTitle, Album FROM Songs "
        "WHERE SongTitle IS NOT NULL AND Artist IS NOT NULL"
    ):
        a, t = normalize(row["Artist"]), normalize(row["SongTitle"])
        if not a or not t:
            continue
        song_idx.setdefault((a, t), []).append((row["ID"], normalize(row["Album"] or "")))

    # In-window MM5 plays per song (for variable-window dedup)
    mm5_play_idx: dict[int, list[float]] = {}
    for row in cur.execute(
        "SELECT IDSong, PlayDate FROM Played WHERE PlayDate >= ? AND PlayDate < ?",
        (start_ole, end_ole),
    ):
        mm5_play_idx.setdefault(row["IDSong"], []).append(row["PlayDate"])

    # Track length lookup (seconds). MM5 stores SongLength in ms.
    song_len_sec: dict[int, float] = {}
    for row in cur.execute("SELECT ID, SongLength FROM Songs WHERE SongLength > 0"):
        song_len_sec[row["ID"]] = row["SongLength"] / 1000.0

    extras_rows: list[tuple[int, float]] = []
    for s in window_scrobbles:
        key = (normalize(s["artist"]), normalize(s["title"]))
        candidates = song_idx.get(key)
        if not candidates:
            diag["unmatched"] += 1
            if len(diag["unmatched_examples"]) < 5:
                diag["unmatched_examples"].append(f"{s['artist']} — {s['title']}")
            continue
        if len(candidates) > 1 and s["album"]:
            nalb = normalize(s["album"])
            best = [c for c in candidates if c[1] == nalb] or \
                   [c for c in candidates if c[1] and (c[1] in nalb or nalb in c[1])]
            song_id = (best or candidates)[0][0]
        else:
            song_id = candidates[0][0]
        scrobble_ole = dt_to_ole(_uts_to_naive_utc(s["uts"]))
        window_sec = max(
            dedup_window_sec_floor,
            song_len_sec.get(song_id, 0) + dedup_len_buffer_sec,
        )
        window_ole = window_sec / 86400.0
        if any(abs(p - scrobble_ole) < window_ole for p in mm5_play_idx.get(song_id, [])):
            diag["duplicate"] += 1
            continue
        diag["matched"] += 1
        extras_rows.append((song_id, scrobble_ole))

    if extras_rows:
        con.executemany(
            "INSERT INTO aux.LastfmExtras (IDSong, PlayDate) VALUES (?,?)",
            extras_rows,
        )

    log(f"  merged: {diag['matched']} new plays · {diag['duplicate']} dup of MM5 "
        f"· {diag['unmatched']} unmatched in Songs")
    if diag["unmatched_examples"]:
        log("  unmatched examples: " + " | ".join(diag["unmatched_examples"]))
    return diag
