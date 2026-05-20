"""
Per-project file locations. Defaults point at ../../listen-here/ relative to
this file (the existing data-files location); pass an explicit dir to override.

Data files DO NOT live inside workshop/. Workshop is code; listen-here is data.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Paths:
    """All file paths the recap engine touches."""
    data_dir:           str   # base for everything that's not explicitly absolute
    db:                 str   # MM5 SQLite copy
    secrets:            str   # WP / Bandcamp / Last.fm creds
    bandcamp_cache:     str   # snapshot of Bandcamp fancollection
    link_cache:         str   # per-track resolved URLs
    tag_cache:          str   # WP tag name → id
    art_cache:          str   # local image path → {"url","id"} for WP media
    itunes_art_cache:   str   # iTunes Search album-art hot-link URLs
    artist_history:     str   # per-month top-10 artists
    lastfm_cache:       str   # all-time Last.fm scrobbles
    thumbs_dir:         str   # MM5 album-art thumbs (read-only)
    output_dir:         str   # where to write recap_YYYY-MM.html


def default_paths(data_dir: str | None = None,
                  thumbs_dir: str | None = None,
                  output_dir: str | None = None) -> Paths:
    """
    By default, data_dir = <repo>/listen-here, thumbs_dir = %APPDATA%/MediaMonkey5/Thumbs,
    output_dir = data_dir (preview HTML lands next to caches).
    """
    if data_dir is None:
        here = os.path.dirname(os.path.abspath(__file__))           # workshop/recap/
        repo = os.path.abspath(os.path.join(here, "..", ".."))      # repo root
        data_dir = os.path.join(repo, "listen-here")
    if thumbs_dir is None:
        thumbs_dir = os.path.expandvars(r"%APPDATA%\MediaMonkey5\Thumbs")
    if output_dir is None:
        output_dir = data_dir

    j = lambda *a: os.path.join(data_dir, *a)
    return Paths(
        data_dir         = data_dir,
        db               = j("MM5.DB"),
        secrets          = j("secrets.json"),
        bandcamp_cache   = j("bandcamp_collection.json"),
        link_cache       = j("link_cache.json"),
        tag_cache        = j("tag_cache.json"),
        art_cache        = j("art_cache.json"),
        itunes_art_cache = j("itunes_art_cache.json"),
        artist_history   = j("top_artists_history.json"),
        lastfm_cache     = j("lastfm_scrobbles.json"),
        thumbs_dir       = thumbs_dir,
        output_dir       = output_dir,
    )
