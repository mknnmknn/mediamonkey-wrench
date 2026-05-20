"""
Workshop — FastAPI app. Local single-user developer tool for the Listen/Here
recap and (later) other MediaMonkey-adjacent workflows.

Bind defaults to 127.0.0.1:8765 — there's no auth and the app freely reads
your data files. Don't expose it publicly.

Run:
    python -m workshop.server
    (browse to http://127.0.0.1:8765/)
"""
from __future__ import annotations

import base64
import datetime
import io
import json
import os
import re
from contextlib import redirect_stdout

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .recap import artist as artist_q
from .recap import charts
from .recap.art import ArtResolver, find_album_art_path, index_thumbs
from .recap.bandcamp import BandcampIndex
from .recap.compose import RecapWindow, compose_recap, list_deep_dive_candidates
from .recap.db import create_played_all_view, open_connection
from .recap.lastfm import merge_all_to_aux
from .recap.normalize import normalize
from .recap.paths import default_paths


# --------------------------------------------------------------------------
#  Module-level caches for expensive-to-build indexes that don't change
#  during the life of a server process. (Thumbs walk = 8k+ files; Bandcamp
#  index = parses the full fan-collection JSON.)
# --------------------------------------------------------------------------

_THUMBS_INDEX: dict | None = None
_BANDCAMP: BandcampIndex | None = None
_ART: ArtResolver | None = None


def _thumbs():
    global _THUMBS_INDEX
    if _THUMBS_INDEX is None:
        _THUMBS_INDEX = index_thumbs(default_paths().thumbs_dir)
    return _THUMBS_INDEX


def _bandcamp():
    global _BANDCAMP
    if _BANDCAMP is None:
        _BANDCAMP = BandcampIndex(default_paths().bandcamp_cache)
    return _BANDCAMP


def _art_resolver():
    global _ART
    if _ART is None:
        p = default_paths()
        _ART = ArtResolver(
            p.art_cache, p.itunes_art_cache,
            wp_upload_fn=lambda local: None,   # unused in dry-run / artist-page contexts
        )
    return _ART


def _window_from_form(year: int | None, month: int | None,
                      start_date: str | None, end_date: str | None) -> RecapWindow:
    """
    Build a RecapWindow from a route's form fields. Accepts either a
    (year, month) pair (clean calendar month) or (start_date, end_date) ISO
    strings (inclusive custom range). 400s with a sensible error on bad input.
    """
    if start_date and end_date:
        try:
            start_d = datetime.date.fromisoformat(start_date)
            end_d   = datetime.date.fromisoformat(end_date)
        except ValueError:
            raise HTTPException(400, "bad date format (use YYYY-MM-DD)")
        if end_d < start_d:
            raise HTTPException(400, "end date is before start date")
        return RecapWindow.from_dates(start_d, end_d)
    if year and month:
        if not (1 <= month <= 12):
            raise HTTPException(400, "month must be 1-12")
        return RecapWindow.from_month(year, month)
    raise HTTPException(400, "missing window parameters (need year+month or start_date+end_date)")


def _window_iso_bounds(window: RecapWindow) -> tuple[str, str]:
    """Return (start_iso, end_iso_inclusive) — the form fields the next screen carries."""
    start_iso = window.start.date().isoformat()
    end_iso   = (window.end - datetime.timedelta(days=1)).date().isoformat()
    return start_iso, end_iso


def _teed_log(buf: io.StringIO):
    """Return a print-like function that writes to both the buffer AND stdout,
    so progress shows live in the uvicorn terminal while the browser is waiting."""
    def _log(*a, **kw):
        print(*a, **kw)            # uvicorn terminal — live progress
        print(*a, **kw, file=buf)  # template "Run log" — visible after response
    return _log


HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES = Jinja2Templates(directory=os.path.join(HERE, "templates"))

app = FastAPI(title="Workshop")
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")


# --------------------------------------------------------------------------
#  Local-file image proxy
#
#  The recap engine emits file:/// URLs for album covers when not posting
#  (because the source is the local MM5 Thumbs folder). Browsers won't load
#  file:// images from an http://localhost page, so we rewrite them in the
#  preview HTML to /art/<token> URLs that point at this proxy. The token is
#  the local path, base64-url-encoded; we verify it lives under the Thumbs
#  root before serving so no random-disk-read shenanigans.
# --------------------------------------------------------------------------

_FILE_URL_RE = re.compile(r'file:///([^"\s]+)')


def _path_to_token(path: str) -> str:
    return base64.urlsafe_b64encode(path.encode("utf-8")).decode("ascii")


def _token_to_path(token: str) -> str:
    return base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")


def _rewrite_file_urls(html: str) -> str:
    """Swap file:/// URLs in rendered HTML to /art/<token> proxy URLs."""
    def repl(m: re.Match) -> str:
        raw = m.group(1)
        # The recap writes file:///C:/Users/.../foo.jpg — turn that back into a
        # native path. Forward slashes are fine on Windows for our purposes.
        path = raw.replace("/", os.sep) if os.sep != "/" else raw
        return f"/art/{_path_to_token(path)}"
    return _FILE_URL_RE.sub(repl, html)


@app.get("/art/{token}")
def serve_art(token: str):
    paths = default_paths()
    try:
        path = _token_to_path(token)
    except Exception:
        raise HTTPException(400, "bad token")
    norm_req    = os.path.normcase(os.path.normpath(path))
    norm_thumbs = os.path.normcase(os.path.normpath(paths.thumbs_dir))
    if not norm_req.startswith(norm_thumbs):
        raise HTTPException(403, "outside thumbs root")
    if not os.path.exists(path):
        raise HTTPException(404, "not found")
    return FileResponse(path, media_type="image/jpeg")


# --------------------------------------------------------------------------
#  Routes
# --------------------------------------------------------------------------

@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse(url="/recap")


@app.get("/recap", response_class=HTMLResponse)
def recap_form(request: Request) -> HTMLResponse:
    today = datetime.date.today()
    if today.month == 1:
        y, m = today.year - 1, 12
    else:
        y, m = today.year, today.month - 1
    # Defaults for the range fields: last 14 days ending today
    today = datetime.date.today()
    default_end   = today.isoformat()
    default_start = (today - datetime.timedelta(days=14)).isoformat()
    return TEMPLATES.TemplateResponse(request=request, name="recap_form.html", context={
        "year": y,
        "month": m,
        "months": _MONTHS,
        "years": _year_choices(),
        "default_start": default_start,
        "default_end":   default_end,
    })


@app.post("/recap/run", response_class=HTMLResponse)
def recap_run(request: Request,
              year: int | None = Form(None),
              month: int | None = Form(None),
              start_date: str | None = Form(None),
              end_date: str | None = Form(None),
              skip_lastfm: bool = Form(False)) -> HTMLResponse:
    window = _window_from_form(year, month, start_date, end_date)
    start_iso, end_iso = _window_iso_bounds(window)

    log_buf = io.StringIO()
    pool, diag = list_deep_dive_candidates(
        window=window, skip_lastfm=skip_lastfm,
        log=_teed_log(log_buf),
    )
    return TEMPLATES.TemplateResponse(request=request, name="recap_candidates.html", context={
        "start_date": start_iso,
        "end_date":   end_iso,
        "skip_lastfm": skip_lastfm,
        "candidates": pool,
        "diag": diag,
        "label": window.label,
        "is_month": window.is_month,
        "log": log_buf.getvalue(),
    })


def _picker_from_names(picked_names: list[str]):
    """Build a deep_dive_picker that filters the pool to the given artist names."""
    wanted = {normalize(p) for p in picked_names if p.strip()}
    def picker(pool: list[dict]) -> list[dict]:
        return [d for d in pool if normalize(d["art"]) in wanted]
    return picker


@app.post("/recap/compose", response_class=HTMLResponse)
def recap_compose_view(request: Request,
                       year: int | None = Form(None),
                       month: int | None = Form(None),
                       start_date: str | None = Form(None),
                       end_date: str | None = Form(None),
                       skip_lastfm: bool = Form(False),
                       pick: list[str] = Form(default=[])) -> HTMLResponse:
    window = _window_from_form(year, month, start_date, end_date)
    start_iso, end_iso = _window_iso_bounds(window)

    log_buf = io.StringIO()
    result = compose_recap(
        window=window,
        deep_dive_picker=_picker_from_names(pick),
        skip_lastfm=skip_lastfm,
        posting=False,
        write_preview=True,
        log=_teed_log(log_buf),
    )
    with open(result.html_path, encoding="utf-8") as f:
        preview_html = f.read()
    preview_html = _rewrite_file_urls(preview_html)

    return TEMPLATES.TemplateResponse(request=request, name="recap_preview.html", context={
        "start_date": start_iso,
        "end_date":   end_iso,
        "skip_lastfm": skip_lastfm,
        "picks": pick,
        "label": result.window.label,
        "preview_html": preview_html,
        "diag": result.diag,
        "artists_to_tag": result.artists_to_tag,
        "log": log_buf.getvalue(),
    })


@app.post("/recap/draft", response_class=HTMLResponse)
def recap_draft(request: Request,
                year: int | None = Form(None),
                month: int | None = Form(None),
                start_date: str | None = Form(None),
                end_date: str | None = Form(None),
                skip_lastfm: bool = Form(False),
                pick: list[str] = Form(default=[])) -> HTMLResponse:
    window = _window_from_form(year, month, start_date, end_date)
    log_buf = io.StringIO()
    result = compose_recap(
        window=window,
        deep_dive_picker=_picker_from_names(pick),
        skip_lastfm=skip_lastfm,
        posting=True,
        write_preview=False,
        log=_teed_log(log_buf),
    )
    return TEMPLATES.TemplateResponse(request=request, name="recap_drafted.html", context={
        "label": result.window.label,
        "post_id": result.post_id,
        "edit_url": result.edit_url,
        "diag": result.diag,
        "artists_to_tag": result.artists_to_tag,
        "log": log_buf.getvalue(),
    })


# --------------------------------------------------------------------------
#  Artist page
#
#  Both routes (search + detail) open a fresh DB connection, merge the
#  cached Last.fm scrobbles into the in-memory aux table, and create the
#  PlayedAll view — same plumbing as the recap, but using the all-time
#  variant of the scrobble merge so artist stats span full history.
# --------------------------------------------------------------------------

def _open_db_with_lastfm():
    """Open MM5.DB and prepare PlayedAll with all-time Last.fm scrobbles merged in."""
    paths = default_paths()
    con = open_connection(paths.db)
    if os.path.exists(paths.lastfm_cache):
        with open(paths.lastfm_cache, encoding="utf-8") as f:
            scrobbles = (json.load(f) or {}).get("scrobbles", [])
        if scrobbles:
            merge_all_to_aux(con, scrobbles, log=print)
    create_played_all_view(con)
    return con


@app.get("/artist", response_class=HTMLResponse)
def artist_search(request: Request,
                  q: str = "", mode: str = "exact") -> HTMLResponse:
    """Search form + (optional) results list."""
    results: list[dict] = []
    if q.strip():
        con = _open_db_with_lastfm()
        try:
            results = artist_q.search_artists(con, q, mode=mode)
        finally:
            con.close()
    return TEMPLATES.TemplateResponse(request=request, name="artist_search.html", context={
        "q": q,
        "mode": mode,
        "results": results,
    })


@app.get("/artist/view", response_class=HTMLResponse)
def artist_detail(request: Request,
                  name: str, mode: str = "exact") -> HTMLResponse:
    if not name.strip():
        return RedirectResponse(url="/artist")

    con = _open_db_with_lastfm()
    try:
        overview      = artist_q.overview(con, name, mode)
        if overview is None:
            return TEMPLATES.TemplateResponse(request=request, name="artist_detail.html", context={
                "name": name, "mode": mode, "overview": None,
            })
        granularity, time_buckets = artist_q.plays_over_time(con, name, mode)
        cumulative_tracks = artist_q.cumulative_unique_tracks(con, name, mode, time_buckets, granularity)
        top_tracks    = artist_q.top_tracks(con, name, mode, limit=60)
        top_albums    = artist_q.top_albums(con, name, mode, limit=9)
        five_star     = artist_q.five_star_tracks(con, name, mode)
        all_plays     = artist_q.all_track_play_counts(con, name, mode)
        appearances   = artist_q.other_appearances(con, name) if mode == "anywhere" else []

        # Enrich top-albums with cover URLs + Bandcamp links for the 3×3 grid
        thumbs = _thumbs()
        bc     = _bandcamp()
        art    = _art_resolver()
        for a in top_albums:
            a["art_path"]   = find_album_art_path(con.cursor(), a["art_credit"], a["album"], thumbs)
            rec = art.best(a["art_credit"], a["album"], a["art_path"], posting=False)
            if rec and rec["url"]:
                # File:// URLs → /art/<token> proxy. iTunes CDN URLs left alone.
                if rec["url"].startswith("file:///"):
                    raw = rec["url"][len("file:///"):]
                    path = raw.replace("/", os.sep) if os.sep != "/" else raw
                    a["cover_url"] = f"/art/{_path_to_token(path)}"
                else:
                    a["cover_url"] = rec["url"]
            else:
                a["cover_url"] = None
            a["bandcamp_url"] = bc.fuzzy_album(a["art_credit"], a["album"])
    finally:
        con.close()

    diversity = artist_q.diversity(all_plays)

    # Tier the top-tracks list (same pattern as recap): keep adding play tiers
    # until cumulative shown exceeds the target, then collapse remainder.
    TIER_TARGET = 15
    tier_groups: list[tuple[int, list[dict]]] = []
    last_plays = None
    for d in top_tracks:
        if d["plays"] != last_plays:
            tier_groups.append((d["plays"], []))
            last_plays = d["plays"]
        tier_groups[-1][1].append(d)
    shown_tracks: list[dict] = []
    trailing_more: str | None = None
    for i, (plays, tracks) in enumerate(tier_groups):
        if i == 0 or (len(shown_tracks) + len(tracks)) <= TIER_TARGET:
            shown_tracks.extend(tracks)
        else:
            trailing_more = f"+ {len(tracks)} more tracks at {plays} plays"
            break

    chart_plays  = [b["plays"] for b in time_buckets]
    chart_labels = [b["label"] for b in time_buckets]
    plays_chart = (
        charts.dual_area_curve(chart_plays, cumulative_tracks, chart_labels)
        if chart_plays else ""
    )
    chart_peaks = {
        "plays_peak":      max(chart_plays) if chart_plays else 0,
        "tracks_total":    cumulative_tracks[-1] if cumulative_tracks else 0,
    }

    # Diversity chart: top 30 tracks horizontal
    div_tracks = top_tracks[:30]
    div_chart_values = [t["plays"] for t in div_tracks]
    div_chart_labels = [f'{t["SongTitle"]}' for t in div_tracks]
    diversity_chart = charts.bars_horizontal(div_chart_values, div_chart_labels) if div_chart_values else ""

    return TEMPLATES.TemplateResponse(request=request, name="artist_detail.html", context={
        "name": name,
        "mode": mode,
        "overview": overview,
        "granularity": granularity,
        "plays_chart": plays_chart,
        "chart_peaks": chart_peaks,
        "shown_tracks": shown_tracks,
        "trailing_more": trailing_more,
        "top_albums": top_albums,
        "five_star": five_star,
        "appearances": appearances,
        "diversity": diversity,
        "diversity_chart": diversity_chart,
    })


# --------------------------------------------------------------------------
#  Form options
# --------------------------------------------------------------------------

_MONTHS = [(i, datetime.date(2000, i, 1).strftime("%B")) for i in range(1, 13)]


def _year_choices() -> list[int]:
    """A handful of years around now — enough range without dropdown sprawl."""
    this_year = datetime.date.today().year
    return list(range(this_year - 1, this_year + 1))


# --------------------------------------------------------------------------
#  Dev entry
# --------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("workshop.server:app", host="127.0.0.1", port=8765, reload=False)
