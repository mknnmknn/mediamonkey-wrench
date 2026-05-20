"""
Bandcamp fan-collection index + fuzzy album/track lookup.

The collection JSON is scraped/dumped externally (Bandcamp's
`/api/fancollection/1/collection_items` endpoint, undocumented but stable).
We just consume it: build a few lookup indexes, expose `lookup()` and
`fuzzy_album()` for the link-resolver to call.
"""
from __future__ import annotations

import json

from .normalize import normalize, reduce_artist


class BandcampIndex:
    """In-memory indexes built once from the cached fancollection JSON."""

    def __init__(self, collection_path: str):
        with open(collection_path, encoding="utf-8") as f:
            self.collection = json.load(f)
        # (normalized_band, normalized_album_title) -> item_url
        self.album_idx: dict[tuple[str, str], str] = {}
        # normalized_album_title -> [(normalized_band, item_url), ...]
        self.album_title_idx: dict[str, list[tuple[str, str]]] = {}
        # (normalized_band, normalized_track_title) -> parent_album_url
        self.track_idx: dict[tuple[str, str], str] = {}

        for it in self.collection["items"].values():
            band = it.get("band_name") or ""
            title = it.get("item_title") or ""
            url = it.get("item_url") or ""
            if not (band and title and url):
                continue
            nb, nt = normalize(band), normalize(title)
            self.album_idx[(nb, nt)] = url
            self.album_title_idx.setdefault(nt, []).append((nb, url))

        # Track-level: walk tracklists keyed by 'aN'/'tN' to find each track's parent item.
        tralbum_to_url: dict[str, tuple[str, str]] = {}
        for it in self.collection["items"].values():
            for k in ("tralbum_id", "album_id", "item_id"):
                v = it.get(k)
                if v:
                    tralbum_to_url[str(v)] = (it.get("band_name") or "", it.get("item_url") or "")
        for tl_key, tracks in self.collection["tracklists"].items():
            if not isinstance(tracks, list):
                continue
            bare = tl_key.lstrip("at")
            band_url = tralbum_to_url.get(bare)
            if not band_url:
                continue
            band, parent_url = band_url
            if not parent_url:
                continue
            for tr in tracks:
                tr_title = tr.get("title") or ""
                if not tr_title:
                    continue
                self.track_idx[(normalize(band), normalize(tr_title))] = parent_url

    # -- public API --

    def fuzzy_album(self, art_credit: str | None, album_title: str | None) -> str | None:
        """
        Fuzzy artist matching on an exact (normalized) album-title match. Returns
        a Bandcamp album URL or None.

        Strategies, in order:
          1. Exact match on either the full or reduced ('X & Y' -> 'X') artist.
          2. Substring artist match (handles 'The Swell Season' inside
             'The Swell Season Markéta Irglová Glen Hansard').
          3. Compilation fallback (Various-Artists comp containing the track).
        """
        if not album_title:
            return None
        nalb = normalize(album_title)
        if not nalb:
            return None
        nart_full = normalize(art_credit or "")
        nart_primary = normalize(reduce_artist(art_credit or ""))

        for key_artist in (nart_full, nart_primary):
            if key_artist and (key_artist, nalb) in self.album_idx:
                return self.album_idx[(key_artist, nalb)]

        for bc_band, bc_url in self.album_title_idx.get(nalb, []):
            if bc_band and (bc_band in nart_full or nart_full in bc_band
                            or bc_band in nart_primary or nart_primary in bc_band):
                return bc_url
            if bc_band in ("various artists", "various", "va"):
                return bc_url
        return None

    def lookup(self, artist: str, album_artist: str | None,
               title: str, album: str | None) -> str | None:
        """
        Bandcamp URL for a track: try album-level fuzzy match first (handles
        compilations / "& X" credits / substring artist), then fall back to
        track-level lookup (which returns the *parent album* URL).
        """
        for credit in (album_artist or artist, artist):
            url = self.fuzzy_album(credit, album)
            if url:
                return url
        nart = normalize(artist)
        nalbart = normalize(album_artist or artist)
        ntit = normalize(title)
        for key in [
            (nart, ntit),
            (nalbart, ntit),
            (normalize(reduce_artist(artist)), ntit),
        ]:
            if key in self.track_idx:
                return self.track_idx[key]
        return None
