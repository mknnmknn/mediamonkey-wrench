"""
Compose a monthly Listen/Here recap end-to-end.

Pulls together every other module in this package: open MM5.DB, set up the
PlayedAll view, optionally backfill Last.fm scrobbles, run the SQL
aggregations, resolve per-track links, render Gutenberg blocks, optionally
upload art + draft to WP.

The CLI imports this module; later phases (the FastAPI server) will too.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Callable

from . import blocks as B
from . import queries as Q
from .art import ArtResolver, find_album_art_path, index_thumbs
from .bandcamp import BandcampIndex
from .db import (create_played_all_view, local_dt_to_ole, ole_to_local_dt,
                 open_connection)
from .lastfm import fetch_scrobbles, merge_to_aux
from .links import LinkResolver
from .normalize import htm, normalize, ordinal
from .paths import Paths, default_paths
from .wp import WPClient


# ---------- Algorithm / rendering tunables ----------

VAULT_YEAR_DOMINANCE_PCT    = 0.85   # From-the-Vault: collapse to one-year flat list at this dominance
VAULT_MIN_AVG_PER_MONTH     = 3.0    # below this avg items/month-group, prefer year grouping
TOP_TRACKS_EXPANDED_TARGET  = 10     # keep adding tiers until cumulative exceeds this
LASTFM_DEDUP_WINDOW_SEC     = 300    # min dedup window (per scrobble; actual is max(this, song_len + buffer))
LASTFM_DEDUP_LEN_BUFFER_SEC = 120
LASTFM_BACKFILL_YEARS       = 5      # first-run horizon
DEEP_DIVE_MIN_W_PLAYS       = 15
DEEP_DIVE_MIN_BASELINE      = 5
DEEP_DIVE_RATIO_THRESHOLD   = 2.5
CULTURE_CATEGORY_ID         = 90     # WP category for Listen/Here posts
ALWAYS_INCLUDED_TAG         = "Listen/Here"


# ---------- Public types ----------

@dataclass
class RecapWindow:
    """
    Time window for a recap. Constructed via classmethods rather than directly:

      RecapWindow.from_month(2026, 2)              # February 2026
      RecapWindow.from_dates(date(2026,1,15), date(2026,2,20))   # custom range

    `start` is inclusive, `end` is exclusive (half-open) — matches the SQL
    WHERE PlayDate >= ? AND PlayDate < ? pattern used throughout.

    is_month=True means the window is a clean calendar-month aligned span;
    only those windows get persisted to artist_history (and only those get
    the "Nth time in top 10" badges, since the badge math is monthly).
    """
    start:    datetime.datetime
    end:      datetime.datetime
    label:    str
    slug:     str
    is_month: bool

    @classmethod
    def from_month(cls, year: int, month: int) -> "RecapWindow":
        start = datetime.datetime(year, month, 1)
        end = (datetime.datetime(year + 1, 1, 1) if month == 12
               else datetime.datetime(year, month + 1, 1))
        return cls(
            start=start, end=end,
            label=start.strftime("%B, %Y"),
            slug=start.strftime("%Y-%m"),
            is_month=True,
        )

    @classmethod
    def from_dates(cls, start_date: datetime.date, end_date: datetime.date) -> "RecapWindow":
        """
        Both arguments are inclusive calendar dates. The internal `end` is
        bumped one day forward so PlayDate < end captures the whole end day.

        If the range happens to align with a calendar month (start.day==1 and
        end is the last day of the same month), this returns a month-style
        window so badges / history / filenames stay consistent.
        """
        start = datetime.datetime(start_date.year, start_date.month, start_date.day)
        end_inclusive = datetime.datetime(end_date.year, end_date.month, end_date.day)
        end = end_inclusive + datetime.timedelta(days=1)

        # Detect month alignment so e.g. "2026-02-01 to 2026-02-28" routes through from_month
        month_aligned = (
            start.day == 1
            and end.day == 1
            and (
                (end.year == start.year and end.month == start.month + 1)
                or (end.year == start.year + 1 and start.month == 12 and end.month == 1)
            )
        )
        if month_aligned:
            return cls.from_month(start.year, start.month)

        if start.year == end_inclusive.year:
            if start.month == end_inclusive.month:
                label = (f"{start.strftime('%b')} {start.day}–"
                         f"{end_inclusive.day}, {start.year}")
            else:
                label = (f"{start.strftime('%b')} {start.day} – "
                         f"{end_inclusive.strftime('%b')} {end_inclusive.day}, {start.year}")
        else:
            label = (f"{start.strftime('%b')} {start.day}, {start.year} – "
                     f"{end_inclusive.strftime('%b')} {end_inclusive.day}, {end_inclusive.year}")
        slug = f"{start.strftime('%Y-%m-%d')}_{end_inclusive.strftime('%Y-%m-%d')}"
        return cls(start=start, end=end, label=label, slug=slug, is_month=False)


# Picker receives the sorted candidate pool, returns the chosen subset (any order).
DeepDivePicker = Callable[[list[dict]], list[dict]]


def auto_top_3_picker(pool: list[dict]) -> list[dict]:
    """Default picker: top 3 by ratio (legacy behavior before interactive picker)."""
    return pool[:3]


def list_deep_dive_candidates(*,
                              window: RecapWindow,
                              paths: Paths | None = None,
                              skip_lastfm: bool = False,
                              log = print) -> tuple[list[dict], dict]:
    """
    Run the pipeline up through the deep-dive candidate computation and stop.
    Returns (sorted_candidate_pool, lastfm_merge_diag).

    Used by the web app for the picker UI without committing to a full render.
    Each call re-opens the DB and re-fetches Last.fm — fine because Last.fm
    is incremental and DB queries are sub-second.
    """
    paths = paths or default_paths()

    with open(paths.secrets, encoding="utf-8") as f:
        secrets = json.load(f)

    con = open_connection(paths.db)
    lastfm_all = fetch_scrobbles(
        cache_path     = paths.lastfm_cache,
        user           = secrets.get("lastfm_user"),
        api_key        = secrets.get("lastfm_api_key"),
        window_start   = window.start,
        backfill_years = LASTFM_BACKFILL_YEARS,
        skip           = skip_lastfm,
        log            = log,
    )
    diag = merge_to_aux(
        con, lastfm_all,
        window_start             = window.start,
        window_end               = window.end,
        dedup_window_sec_floor   = LASTFM_DEDUP_WINDOW_SEC,
        dedup_len_buffer_sec     = LASTFM_DEDUP_LEN_BUFFER_SEC,
        log                      = log,
    )
    create_played_all_view(con)

    start_ole = local_dt_to_ole(window.start)
    end_ole   = local_dt_to_ole(window.end)
    baseline_start_ole = start_ole - 365.0

    dd_raw = Q.deep_dive_candidate_rows(con, start_ole, end_ole, baseline_start_ole)
    pool: list[dict] = []
    for art_name, w_plays, w_tracks, b_plays in dd_raw:
        monthly_avg = b_plays / 12.0
        if b_plays < DEEP_DIVE_MIN_BASELINE:
            continue
        ratio = w_plays / max(monthly_avg, 0.1)
        if ratio >= DEEP_DIVE_RATIO_THRESHOLD:
            pool.append({
                "art": art_name, "plays": w_plays, "tracks": w_tracks,
                "monthly_avg": monthly_avg, "ratio": ratio,
            })
    pool.sort(key=lambda d: (-d["ratio"], -d["plays"]))
    con.close()
    return pool, diag


@dataclass
class ComposeResult:
    window:         RecapWindow
    html_body:      str                 # Gutenberg-block content
    html_path:      str | None          # local preview file, if written
    artists_to_tag: list[str]
    diag:           dict                # lastfm + run stats
    post_id:        int | None  = None
    edit_url:       str | None  = None


# ---------- Orchestrator ----------

def compose_recap(*,
                  window: RecapWindow,
                  paths: Paths | None = None,
                  deep_dive_picker: DeepDivePicker = auto_top_3_picker,
                  skip_lastfm: bool = False,
                  posting: bool = False,
                  write_preview: bool = True,
                  log = print) -> ComposeResult:
    paths = paths or default_paths()
    log(f"Recap window: {window.label}  (output: recap_{window.slug}.html)")

    # ----- caches + secrets -----
    with open(paths.secrets, encoding="utf-8") as f:
        secrets = json.load(f)
    artist_history = (
        json.load(open(paths.artist_history, encoding="utf-8"))
        if os.path.exists(paths.artist_history) else {}
    )

    bandcamp = BandcampIndex(paths.bandcamp_cache)
    links = LinkResolver(paths.link_cache, bandcamp, log=log)

    # WP client + art resolver (the resolver needs the WP upload function)
    wp: WPClient | None = None
    if posting:
        wp = WPClient(
            secrets["wp_url"], secrets["wp_user"], secrets["wp_app_password"],
            tag_cache_path=paths.tag_cache,
        )
        upload_fn = wp.upload_image
    else:
        upload_fn = lambda local: None   # unused in dry-run

    art = ArtResolver(paths.art_cache, paths.itunes_art_cache, upload_fn)

    # ----- thumbs + DB -----
    log(f"Indexing MM Thumbs at {paths.thumbs_dir} ...")
    thumbs_index = index_thumbs(paths.thumbs_dir)
    log(f"  indexed {len(thumbs_index)} unique album-art hashes")

    con = open_connection(paths.db)
    cur = con.cursor()

    # ----- Last.fm fetch + merge -----
    lastfm_all = fetch_scrobbles(
        cache_path     = paths.lastfm_cache,
        user           = secrets.get("lastfm_user"),
        api_key        = secrets.get("lastfm_api_key"),
        window_start   = window.start,
        backfill_years = LASTFM_BACKFILL_YEARS,
        skip           = skip_lastfm,
        log            = log,
    )
    diag = merge_to_aux(
        con, lastfm_all,
        window_start             = window.start,
        window_end               = window.end,
        dedup_window_sec_floor   = LASTFM_DEDUP_WINDOW_SEC,
        dedup_len_buffer_sec     = LASTFM_DEDUP_LEN_BUFFER_SEC,
        log                      = log,
    )

    # PlayedAll view is the source of truth for every aggregation below.
    create_played_all_view(con)

    # ----- SQL aggregations -----
    start_ole = local_dt_to_ole(window.start)
    end_ole   = local_dt_to_ole(window.end)
    baseline_start_ole = start_ole - 365.0
    year_before_ole    = start_ole - 365.0

    stats_row       = Q.stats(con, start_ole, end_ole)
    busiest_row     = Q.busiest_day(con, start_ole, end_ole)
    baseline_total  = Q.baseline_total_12mo(con, baseline_start_ole, start_ole)
    baseline_monthly = (baseline_total / 12.0) if baseline_total else 0.0

    top_artists_rows = Q.top_artists(con, start_ole, end_ole)
    top_albums_rows  = Q.top_albums(con, start_ole, end_ole)
    top_tracks_rows  = Q.top_tracks(con, start_ole, end_ole)
    first_enc_rows   = Q.first_encounters(con, start_ole, end_ole)
    five_star_count  = Q.five_star_total(con, start_ole, end_ole)
    five_star_rows   = Q.five_star(con, start_ole, end_ole)
    comeback_rows    = Q.comeback(con, start_ole, end_ole, year_before_ole)
    dd_raw           = Q.deep_dive_candidate_rows(con, start_ole, end_ole, baseline_start_ole)

    # ----- deep-dive candidate pool (filtered + sorted) -----
    deep_dive_pool: list[dict] = []
    for art_name, w_plays, w_tracks, b_plays in dd_raw:
        monthly_avg = b_plays / 12.0
        if b_plays < DEEP_DIVE_MIN_BASELINE:
            continue
        ratio = w_plays / max(monthly_avg, 0.1)
        if ratio >= DEEP_DIVE_RATIO_THRESHOLD:
            deep_dive_pool.append({
                "art": art_name, "plays": w_plays, "tracks": w_tracks,
                "monthly_avg": monthly_avg, "ratio": ratio,
            })
    deep_dive_pool.sort(key=lambda d: (-d["ratio"], -d["plays"]))

    deep_dives: list[dict] = deep_dive_picker(deep_dive_pool) if deep_dive_pool else []

    # For each selected deep-dive artist, fetch albums + art + Bandcamp links.
    for dd in deep_dives:
        rows = Q.deep_dive_artist_albums(con, start_ole, end_ole, dd["art"])
        dd["albums"] = []
        for art_credit, album, plays, tracks in rows:
            dd["albums"].append({
                "art_credit": art_credit, "album": album,
                "plays": plays, "tracks": tracks,
                "art_path": find_album_art_path(cur, art_credit, album, thumbs_index),
                "url":      bandcamp.fuzzy_album(art_credit, album),
            })

    # ----- annotate top-tracks / new-to-me / 5★ / comeback with link URLs -----
    log("Resolving links (Bandcamp + song.link fallback)...")

    def annotate(rows):
        out = []
        for r in rows:
            d = dict(r)
            url, source = links.resolve_track(
                d["Artist"], d.get("album_artist") or d["Artist"],
                d["SongTitle"], d.get("Album", ""),
            )
            d["url"] = url
            d["link_source"] = source
            out.append(d)
        return out

    top_tracks_rs = annotate(top_tracks_rows)
    five_star_rs  = annotate(five_star_rows)
    comeback_rs   = annotate(comeback_rows)
    log("  " + links.summary())
    links.save()

    # Top albums: each gets an album-level link + a local art path
    top_albums_rs = []
    for r in top_albums_rows:
        d = dict(r)
        d["url"] = links.resolve_album(d["art"], d["album"])
        d["art_path"] = find_album_art_path(cur, d["art"], d["album"], thumbs_index)
        top_albums_rs.append(d)
    links.save()
    log(f"  top albums with art available: "
        f"{sum(1 for d in top_albums_rs if d['art_path'])}/{len(top_albums_rs)}")

    # ----- render -----
    html_body, displayed_top_tracks, displayed_first_encounters = _render(
        window=window, art=art, posting=posting, bandcamp=bandcamp,
        stats_row=stats_row, busiest_row=busiest_row,
        baseline_monthly=baseline_monthly,
        top_artists_rows=top_artists_rows, top_albums_rs=top_albums_rs,
        top_tracks_rs=top_tracks_rs, first_enc_rows=first_enc_rows,
        five_star_count=five_star_count, five_star_rs=five_star_rs,
        comeback_rs=comeback_rs, deep_dives=deep_dives,
        artist_history=artist_history,
        con=con, start_ole=start_ole, end_ole=end_ole,
    )

    art.save()

    # ----- write preview -----
    html_path = None
    if write_preview:
        html_path = os.path.join(paths.output_dir, f"recap_{window.slug}.html")
        _write_preview(html_path, html_body)
        log(f"\nWrote local preview: {html_path}")

    # ----- collect tags -----
    artists_to_tag = _collect_artists_to_tag(
        top_artists_rows, top_albums_rs, deep_dives,
        displayed_first_encounters, displayed_top_tracks,
        five_star_rs, comeback_rs,
    )
    log(f"\nDistinct artists to tag: {len(artists_to_tag)}")

    # ----- optionally POST draft -----
    post_id = None
    edit_url = None
    if posting and wp is not None:
        log("\nResolving tag IDs...")
        tag_ids = []
        for name in [ALWAYS_INCLUDED_TAG] + artists_to_tag:
            tid = wp.get_or_create_tag(name)
            if tid:
                tag_ids.append(tid)
        wp.save_tag_cache()
        log(f"  tag count: {len(tag_ids)}")
        status, resp = wp.post_draft(
            title=f"Listen/Here: {window.label}",
            content_html=html_body,
            tag_ids=tag_ids,
            category_ids=[CULTURE_CATEGORY_ID],
        )
        if status in (200, 201) and isinstance(resp, dict):
            post_id = resp["id"]
            edit_url = wp.edit_url(post_id)
            log(f"\n✓ POSTED draft (id={post_id}, status={resp.get('status')})")
            log(f"  edit URL: {edit_url}")
        else:
            log(f"\n✗ post failed: {status} {resp}")

    # ----- persist artist history (only for clean-month windows; custom ranges
    # are ad-hoc explorations and shouldn't perturb monthly badge math) -----
    if window.is_month:
        artist_history[window.slug] = [r["Artist"] for r in top_artists_rows]
        with open(paths.artist_history, "w", encoding="utf-8") as f:
            json.dump(artist_history, f, ensure_ascii=False, indent=2)
        log(f"  artist history: {len(artist_history)} months tracked")
    else:
        log(f"  artist history: skipped (custom date range, not month-aligned)")

    return ComposeResult(
        window=window, html_body=html_body, html_path=html_path,
        artists_to_tag=artists_to_tag, diag=diag,
        post_id=post_id, edit_url=edit_url,
    )


# ---------- Rendering helpers (block emission) ----------

def _li_track(d: dict) -> str:
    """Inner-li content for a Top-Tracks entry."""
    art_html = f"<strong>{htm(d['Artist'])}</strong>"
    tit = htm(d["SongTitle"])
    plays = d["plays"]
    track_html = B.link_or_text(f'<em>{tit}</em>', d.get("url"))
    album_part = _album_inline(d.get("album_artist") or d["Artist"], d.get("Album", ""), None)
    return f'{art_html} — {track_html}{album_part} · {plays} {"play" if plays == 1 else "plays"}'


def _li_track_simple(d: dict) -> str:
    """5★ / Vault inner-li: title — artist (album)."""
    tit = htm(d["SongTitle"])
    track_html = B.link_or_text(f'<em>{tit}</em>', d.get("url"))
    album_part = _album_inline(d.get("album_artist") or d["Artist"], d.get("Album", ""), None)
    return f'{track_html} — <strong>{htm(d["Artist"])}</strong>{album_part}'


def _album_inline(art_credit: str, album_title: str | None, bandcamp_url: str | None) -> str:
    """Render an inline album reference like (<em>Album</em>), with Bandcamp link if known."""
    if not album_title:
        return ""
    body = f'<em>{htm(album_title)}</em>'
    if bandcamp_url:
        body = f'<a href="{htm(bandcamp_url)}">{body}</a>'
    return f' ({body})'


def _album_cover_block(art: ArtResolver, art_credit: str, album: str,
                       local_path: str | None, posting: bool,
                       link_url: str | None = None,
                       size_slug: str = "large") -> str:
    """wp:image block for a cover, with an initials placeholder fallback."""
    rec = art.best(art_credit, album, local_path, posting)
    if rec is None:
        initials = "".join(w[0] for w in (art_credit or "").split()[:2]).upper() or "—"
        return B.raw_html(
            '<div style="aspect-ratio:1/1;background:linear-gradient(135deg,#e8e8e8,#bbb);'
            'display:flex;align-items:center;justify-content:center;'
            'color:#fff;font-size:2.2em;font-weight:600;letter-spacing:.05em">'
            f'{htm(initials)}</div>'
        )
    return B.image(rec["url"], alt=f"Album cover: {art_credit} – {album}",
                   media_id=rec["id"], link_url=link_url, size_slug=size_slug)


def _album_card_column(art_resolver: ArtResolver, d: dict, posting: bool, big: bool) -> str:
    art_label  = f"<strong>{htm(d['art'])}</strong>"
    album_html = B.link_or_text(f'<em>{htm(d["album"])}</em>', d.get("url"))
    stat = (f'{d["plays"]} plays · {d["tracks_played"]} tracks'
            if big else f'{d["plays"]}p · {d["tracks_played"]}tr')
    cover = _album_cover_block(art_resolver, d["art"], d["album"], d.get("art_path"),
                               posting, link_url=d.get("url"),
                               size_slug=("large" if big else "medium"))
    caption = B.paragraph(
        f'{art_label}<br>{album_html}<br>'
        f'<span style="color:#888;font-size:.85em">{stat}</span>'
    )
    return B.column([cover, caption])


def _deep_dive_album_column(art_resolver: ArtResolver, ab: dict, posting: bool) -> str:
    ab_link = B.link_or_text(f'<em>{htm(ab["album"])}</em>', ab.get("url"))
    cover = _album_cover_block(art_resolver, ab["art_credit"], ab["album"],
                               ab.get("art_path"), posting,
                               link_url=ab.get("url"), size_slug="medium")
    caption = B.paragraph(
        f'{ab_link}<br>'
        f'<span style="color:#888;font-size:.85em">{ab["plays"]} plays · {ab["tracks"]} tr</span>'
    )
    return B.column([cover, caption])


def _freq_badge(name: str, artist_history: dict, current_month_key: str) -> str:
    prior_months = sorted(k for k in artist_history.keys() if k < current_month_key)
    if not prior_months:
        return ""
    apps = sum(1 for k in prior_months if name in artist_history.get(k, []))
    if apps == 0:
        return ""
    streak = 0
    for k in reversed(prior_months):
        if name in artist_history.get(k, []):
            streak += 1
        else:
            break
    badge = f"{ordinal(apps + 1)} time in top 10"
    if streak >= 2:
        badge += f", {streak + 1} months running"
    return f' <span style="color:#888;font-size:.85em">({badge})</span>'


def _render(*, window: RecapWindow, art: ArtResolver, posting: bool,
            bandcamp: BandcampIndex,
            stats_row, busiest_row, baseline_monthly,
            top_artists_rows, top_albums_rs, top_tracks_rs,
            first_enc_rows, five_star_count, five_star_rs, comeback_rs,
            deep_dives, artist_history, con, start_ole, end_ole) -> tuple[str, list, list]:
    """
    Emit the full recap as Gutenberg-block markup. Returns
    (html_body, displayed_top_tracks, displayed_first_encounters) — the
    latter two feed into the tag collector.
    """
    # Bind a local album_inline that uses bandcamp for lookups
    def album_inline_bc(art_credit: str, album_title: str | None) -> str:
        if not album_title:
            return ""
        bc = bandcamp.fuzzy_album(art_credit, album_title)
        return _album_inline(art_credit, album_title, bc)

    # Inject the bandcamp-aware album_inline into the per-track li renderers
    def li_track(d):
        art_html = f"<strong>{htm(d['Artist'])}</strong>"
        tit = htm(d["SongTitle"])
        plays = d["plays"]
        track_html = B.link_or_text(f'<em>{tit}</em>', d.get("url"))
        album_part = album_inline_bc(d.get("album_artist") or d["Artist"], d.get("Album", ""))
        return f'{art_html} — {track_html}{album_part} · {plays} {"play" if plays == 1 else "plays"}'

    def li_track_simple(d):
        tit = htm(d["SongTitle"])
        track_html = B.link_or_text(f'<em>{tit}</em>', d.get("url"))
        album_part = album_inline_bc(d.get("album_artist") or d["Artist"], d.get("Album", ""))
        return f'{track_html} — <strong>{htm(d["Artist"])}</strong>{album_part}'

    parts: list[str] = []
    displayed_top_tracks: list[dict] = []
    displayed_first_encounters: list[str] = []

    # ---------- Intro ----------
    parts.append(B.paragraph(
        f'A new flavor of <em>Listen/Here</em>: instead of a deep dive on a single album, this is '
        f'a snapshot of what I actually listened to in {window.label}. Source data is my MediaMonkey '
        f'library; links go to Bandcamp where the album is in my collection, otherwise to '
        f'<a href="https://song.link/">song.link</a> for cross-platform options.'
    ))

    # ---------- By the Numbers ----------
    parts.append(B.heading("By the Numbers", level=1))
    mins = stats_row["minutes"] or 0
    hrs = mins / 60.0
    busiest_dt = datetime.datetime.strptime(busiest_row["d"], "%Y-%m-%d")
    busiest_label = f"{busiest_dt.strftime('%B')} {ordinal(busiest_dt.day)}"
    delta_pct = ((stats_row["plays"] - baseline_monthly) / baseline_monthly * 100) if baseline_monthly else 0
    delta_phrase = ""
    if abs(delta_pct) >= 5:
        direction = "above" if delta_pct > 0 else "below"
        delta_phrase = (f" — about <strong>{abs(delta_pct):.0f}% {direction}</strong> "
                        f"a typical month over the past year")
    parts.append(B.paragraph(
        f'<strong>{stats_row["plays"]:,}</strong> plays across '
        f'<strong>{stats_row["unique_tracks"]:,}</strong> unique tracks by '
        f'<strong>{stats_row["unique_artists"]:,}</strong> distinct artists{delta_phrase}. '
        f'About <strong>{hrs:.0f} hours</strong> of music ({mins:.0f} minutes). '
        f'Busiest day was <strong>{busiest_label}</strong> with {busiest_row["c"]} plays.'
    ))

    # ---------- Deep Dives ----------
    if deep_dives:
        parts.append(B.heading("Deep Dives", level=1))
        parts.append(B.paragraph("Artists whose presence in the rotation jumped well above their usual."))
        for dd in deep_dives:
            ratio_phrase = (f"about {dd['ratio']:.1f}× a typical month"
                            if dd["monthly_avg"] >= 1 else "well above usual")
            parts.append(B.heading(dd["art"], level=2))
            parts.append(B.paragraph(
                f'{dd["plays"]} plays across {dd["tracks"]} '
                f'{"track" if dd["tracks"] == 1 else "tracks"} — {ratio_phrase}'
            ))
            if dd["albums"]:
                parts.append(B.columns([
                    _deep_dive_album_column(art, ab, posting) for ab in dd["albums"][:6]
                ]))
            parts.append(B.paragraph('<em>[your thoughts here]</em>'))

    # ---------- Top Artists ----------
    # Frequency badges (Nth time in top 10 / N months running) only make sense
    # for clean-month windows; custom date ranges skip them.
    parts.append(B.heading("Top Artists", level=1))
    badge_fn = ((lambda name: _freq_badge(name, artist_history, window.slug))
                if window.is_month else (lambda name: ""))
    parts.append(B.list_block([
        f'<strong>{htm(r["Artist"])}</strong> · '
        f'{r["plays"]} plays across {r["uniq"]} {"track" if r["uniq"] == 1 else "tracks"}'
        f'{badge_fn(r["Artist"])}'
        for r in top_artists_rows
    ], ordered=True))

    # ---------- Top Albums ----------
    parts.append(B.heading("Top Albums", level=1))
    parts.append(B.paragraph('<em>[your thoughts here]</em>'))
    row1 = top_albums_rs[:3]
    row2 = top_albums_rs[3:7]
    row3 = top_albums_rs[7:11]
    if row1:
        parts.append(B.columns([_album_card_column(art, d, posting, big=True)  for d in row1]))
    if row2:
        parts.append(B.columns([_album_card_column(art, d, posting, big=False) for d in row2]))
    if row3:
        parts.append(B.columns([_album_card_column(art, d, posting, big=False) for d in row3]))

    # ---------- Top Tracks ----------
    parts.append(B.heading("Top Tracks", level=1))
    tier_groups: list[tuple[int, list[dict]]] = []
    last_plays = None
    for d in top_tracks_rs:
        if d["plays"] != last_plays:
            tier_groups.append((d["plays"], []))
            last_plays = d["plays"]
        tier_groups[-1][1].append(d)

    shown_list_items: list[str] = []
    trailing_summary: str | None = None
    shown = 0
    for i, (plays, tracks) in enumerate(tier_groups):
        if i == 0 or (shown + len(tracks)) <= TOP_TRACKS_EXPANDED_TARGET:
            for d in tracks:
                shown_list_items.append(li_track(d))
                displayed_top_tracks.append(d)
                shown += 1
        else:
            trailing_summary = f'+ {len(tracks)} more tracks at {plays} plays'
            break
    if shown_list_items:
        parts.append(B.list_block(shown_list_items, ordered=True))
    if trailing_summary:
        parts.append(B.paragraph(f'<span style="color:#666">{trailing_summary}</span>'))

    # ---------- First Encounters ----------
    if first_enc_rows:
        parts.append(B.heading("First Encounters", level=1))
        parts.append(B.paragraph("Artists whose first-ever play in the library happened this month."))
        fe_lis: list[str] = []
        for art_name, plays, tracks in first_enc_rows:
            displayed_first_encounters.append(art_name)
            albums = Q.artist_albums_in_window(con, start_ole, end_ole, art_name)
            extra = ""
            if len(albums) == 1:
                album_name = albums[0][0]
                bc = bandcamp.fuzzy_album(art_name, album_name)
                inner = f'<em>{htm(album_name)}</em>'
                if bc:
                    inner = f'<a href="{htm(bc)}">{inner}</a>'
                extra = f' · from {inner}'
            elif len(albums) >= 2:
                extra = f' · across {len(albums)} albums'
            fe_lis.append(
                f'<strong>{htm(art_name)}</strong> · {plays} {"play" if plays == 1 else "plays"} '
                f'across {tracks} {"track" if tracks == 1 else "tracks"}{extra}'
            )
        parts.append(B.list_block(fe_lis, ordered=False))

    # ---------- Anywhere, Anytime ----------
    parts.append(B.heading("Anywhere, Anytime", level=1))
    if five_star_rs:
        grouped5: list[tuple[str, list[dict]]] = []
        by_month5: dict[str, list[dict]] = {}
        for d in five_star_rs:
            last_dt = ole_to_local_dt(d["last_before_ole"]) if d.get("last_before_ole") else None
            label = last_dt.strftime("%B %Y") if last_dt else "First time on record"
            if label not in by_month5:
                by_month5[label] = []
                grouped5.append((label, by_month5[label]))
            by_month5[label].append(d)

        n = len(five_star_rs)
        n_word = {1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five"}.get(n, str(n))
        if len(grouped5) == 1:
            only_label = grouped5[0][0]
            if only_label == "First time on record":
                intro_tail = f"{n_word} with no recorded prior play."
            else:
                intro_tail = f"{n_word} whose most recent play was way back in {htm(only_label)}."
            parts.append(B.paragraph(
                f'{five_star_count} of my 5★ tracks surfaced this month. {intro_tail}'
            ))
            parts.append(B.list_block([li_track_simple(d) for d in five_star_rs], ordered=False))
        else:
            parts.append(B.paragraph(
                f'{five_star_count} of my 5★ tracks surfaced this month. '
                f'{n_word} whose previous play was furthest back:'
            ))
            for label, items in grouped5:
                parts.append(B.paragraph(f'<strong>Last heard {htm(label)}</strong>'))
                parts.append(B.list_block([li_track_simple(d) for d in items], ordered=False))
    else:
        parts.append(B.paragraph('None of the 5★ tracks came up this month.'))

    # ---------- From the Vault ----------
    parts.append(B.heading("From the Vault", level=1))
    if comeback_rs:
        months_order: list[str] = []; by_month: dict[str, list[dict]] = {}
        years_order:  list[str] = []; by_year:  dict[str, list[dict]] = {}
        for d in comeback_rs:
            last_dt = ole_to_local_dt(d["last_before_ole"]) if d.get("last_before_ole") else None
            m_label = last_dt.strftime("%B %Y") if last_dt else "long ago"
            y_label = last_dt.strftime("%Y") if last_dt else "long ago"
            if m_label not in by_month:
                by_month[m_label] = []; months_order.append(m_label)
            by_month[m_label].append(d)
            if y_label not in by_year:
                by_year[y_label] = []; years_order.append(y_label)
            by_year[y_label].append(d)

        n_items = len(comeback_rs)
        top_year, top_year_items = max(by_year.items(), key=lambda kv: len(kv[1]))
        dominant = (len(top_year_items) / n_items) >= VAULT_YEAR_DOMINANCE_PCT
        avg_per_month = n_items / max(len(by_month), 1)
        intro_base = "Tracks I hadn't reached for in over a year, returning this month."

        if dominant and top_year != "long ago":
            if len(by_year) == 1:
                tail = f'These all were last played in <strong>{htm(top_year)}</strong>.'
            else:
                tail = f'Most were last played in <strong>{htm(top_year)}</strong>.'
            parts.append(B.paragraph(f'{intro_base} {tail}'))
            parts.append(B.list_block([li_track_simple(d) for d in comeback_rs], ordered=False))
        elif avg_per_month < VAULT_MIN_AVG_PER_MONTH:
            parts.append(B.paragraph(intro_base))
            for y in years_order:
                parts.append(B.paragraph(f'<strong>{htm(y)}</strong>'))
                parts.append(B.list_block([li_track_simple(d) for d in by_year[y]], ordered=False))
        else:
            parts.append(B.paragraph(intro_base))
            for m in months_order:
                parts.append(B.paragraph(f'<strong>{htm(m)}</strong>'))
                parts.append(B.list_block([li_track_simple(d) for d in by_month[m]], ordered=False))
    else:
        parts.append(B.paragraph('Nothing returned from a long absence this month.'))

    # ---------- Coda ----------
    parts.append(B.heading("Coda", level=1))
    parts.append(B.paragraph('<em>[your closing thoughts here]</em>'))

    return "\n\n".join(parts), displayed_top_tracks, displayed_first_encounters


def _collect_artists_to_tag(top_artists_rows, top_albums_rs, deep_dives,
                            displayed_first_encounters, displayed_top_tracks,
                            five_star_rs, comeback_rs) -> list[str]:
    """Tag only artists actually visible in the rendered post body."""
    s: set[str] = set()
    for r in top_artists_rows:
        s.add(r["Artist"])
    for d in top_albums_rs:
        s.add(d["art"])
    for dd in deep_dives:
        s.add(dd["art"])
    for art_name in displayed_first_encounters:
        s.add(art_name)
    for d in list(displayed_top_tracks) + list(five_star_rs) + list(comeback_rs):
        s.add(d["Artist"])
        if d.get("album_artist") and d["album_artist"] != d["Artist"]:
            s.add(d["album_artist"])
    return sorted(x.strip() for x in s if x and x.strip())


_PREVIEW_CSS = """
body { font:16px/1.5 Georgia,serif; max-width:880px; margin:2em auto; padding:0 1em }
h1, h2 { margin-top:2em }
li { margin:.3em 0 }
.wp-block-columns { display:flex; gap:1.2em; flex-wrap:wrap; margin:1em 0 }
.wp-block-column { flex:1 1 0; min-width:0 }
.wp-block-image { margin:0 0 .4em }
.wp-block-image img { width:100%; display:block; aspect-ratio:1/1; object-fit:cover }
.wp-block-image figure { margin:0 }
"""


def _write_preview(path: str, html_body: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            f'<!doctype html><meta charset="utf-8"><title>preview</title>'
            f'<style>{_PREVIEW_CSS}</style>{html_body}'
        )
