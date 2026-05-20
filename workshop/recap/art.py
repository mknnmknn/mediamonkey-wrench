"""
Album-art pipeline.

For each album we want a URL plus (if we're posting) a WP media ID:
  1. Local Thumbs cache hit on the right hash + size (preferred — your owned
     art, scaled appropriately for the blog).
  2. Apple's CDN hot-link from iTunes Search (a fallback that displays cleanly
     but can't be replaced from the media library).
  3. Nothing — caller emits an artist-initials placeholder tile.

The `art_cache.json` file maps a local Thumbs path to its WP-media record
{"url": ..., "id": ...} so repeated runs and POSTs don't re-upload anything.
A legacy migration path tolerates the old shape (cache value was just the URL).
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sqlite3
import time
import urllib.parse

from .http import get_json
from .normalize import normalize


def index_thumbs(thumbs_dir: str) -> dict[str, dict[str, str]]:
    """
    Walk the MM5 Thumbs cache and return: hash -> {'500'|'200'|'full': path}.
    Prefers 500px for the blog; 80px is too small and ignored.
    """
    index: dict[str, dict[str, str]] = {}
    for p in pathlib.Path(thumbs_dir).rglob("*.jpg"):
        name = p.stem
        if name.endswith("-500px"):
            index.setdefault(name[:-6], {})["500"] = str(p)
        elif name.endswith("-200px"):
            index.setdefault(name[:-6], {})["200"] = str(p)
        elif name.endswith("-80px"):
            pass
        else:
            index.setdefault(name, {})["full"] = str(p)
    return index


def find_album_art_path(cur: sqlite3.Cursor, art_artist: str, album: str,
                        thumbs_index: dict[str, dict[str, str]]) -> str | None:
    """Local Thumbs path (500px preferred) for the given album credit, or None."""
    sql = """
    SELECT c.PictureDataHash
    FROM Songs s JOIN Covers c ON c.IDSong = s.ID
    WHERE s.Album = ?
      AND COALESCE(NULLIF(s.AlbumArtist,''), s.Artist) = ?
      AND c.PictureDataHash IS NOT NULL AND c.PictureDataHash <> ''
      AND c.CoverType = 3
    GROUP BY c.PictureDataHash
    LIMIT 1
    """
    row = cur.execute(sql, (album, art_artist)).fetchone()
    if not row:
        return None
    sizes = thumbs_index.get(row[0])
    if not sizes:
        return None
    return sizes.get("500") or sizes.get("full") or sizes.get("200")


class ArtResolver:
    """
    Resolves album art to {"url","id"} dicts.

      best(art, album, local_path, posting) -> {"url","id"} | None

    When posting, uploads local images to WP media (cached). When not posting,
    returns a file:// URL for local preview. iTunes CDN URLs are returned with
    id=None — they hot-link rather than embed.
    """

    def __init__(self,
                 art_cache_path: str,
                 itunes_art_cache_path: str,
                 wp_upload_fn,           # callable(local_path) -> {"url", "id"} | None
                 *,
                 itunes_sleep_sec: float = 0.3):
        self.art_path = art_cache_path
        self.itunes_path = itunes_art_cache_path
        self.upload = wp_upload_fn
        self.itunes_sleep_sec = itunes_sleep_sec
        self.art_cache: dict = (
            json.load(open(art_cache_path, encoding="utf-8"))
            if os.path.exists(art_cache_path) else {}
        )
        self.itunes_cache: dict = (
            json.load(open(itunes_art_cache_path, encoding="utf-8"))
            if os.path.exists(itunes_art_cache_path) else {}
        )

    # -- public --

    def best(self, art_credit: str, album: str,
             local_path: str | None, posting: bool) -> dict | None:
        """{"url","id"} or None. Used by the renderer for each cover."""
        u = self._resolve_local(local_path, posting)
        if u:
            return u
        cdn = self._itunes_album_art(art_credit, album)
        if cdn:
            return {"url": cdn, "id": None}
        return None

    def save(self) -> None:
        with open(self.art_path, "w", encoding="utf-8") as f:
            json.dump(self.art_cache, f, ensure_ascii=False, indent=2)
        with open(self.itunes_path, "w", encoding="utf-8") as f:
            json.dump(self.itunes_cache, f, ensure_ascii=False, indent=2)

    # -- internals --

    def _resolve_local(self, local_path: str | None, posting: bool) -> dict | None:
        if not local_path:
            return None
        if posting:
            return self._upload_to_wp(local_path)
        return {"url": "file:///" + local_path.replace("\\", "/"), "id": None}

    def _upload_to_wp(self, local_path: str) -> dict | None:
        cached = self.art_cache.get(local_path)
        if isinstance(cached, dict) and cached.get("url"):
            return cached
        if isinstance(cached, str) and cached:
            # legacy entry: URL only, no media id
            rec = {"url": cached, "id": None}
            self.art_cache[local_path] = rec
            return rec
        rec = self.upload(local_path)
        if rec is not None:
            self.art_cache[local_path] = rec
        return rec

    def _itunes_album_art(self, artist: str, album: str) -> str | None:
        """600px album-art URL on Apple's CDN, or None. Cached."""
        key = f"{normalize(artist)}|{normalize(album)}"
        if key in self.itunes_cache:
            return self.itunes_cache[key]
        art_q = re.sub(r"\s*[&]\s*.+$", "", artist).strip() or artist
        qs = urllib.parse.urlencode({
            "term": f"{art_q} {album}",
            "entity": "album",
            "limit": 3,
            "media": "music",
        })
        try:
            data = get_json(f"https://itunes.apple.com/search?{qs}")
        except Exception:
            self.itunes_cache[key] = None
            return None
        nart, nalb = normalize(artist), normalize(album)
        for r in data.get("results", []):
            s_art = normalize(r.get("artistName", ""))
            s_alb = normalize(r.get("collectionName", ""))
            title_match = (s_alb == nalb) or (s_alb and (s_alb in nalb or nalb in s_alb))
            artist_match = (s_art == nart) or (s_art and (s_art in nart or nart in s_art))
            if title_match and artist_match:
                url = r.get("artworkUrl100", "")
                if url:
                    url = url.replace("100x100bb", "600x600bb").replace("100x100", "600x600")
                    self.itunes_cache[key] = url
                    time.sleep(self.itunes_sleep_sec)
                    return url
        self.itunes_cache[key] = None
        time.sleep(self.itunes_sleep_sec)
        return None
