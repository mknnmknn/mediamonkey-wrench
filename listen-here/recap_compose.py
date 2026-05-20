"""
Compose a monthly listening recap end-to-end:
  - Aggregate sections from MM5.DB (sandbox copy)
  - Optionally backfill mobile/portable plays from Last.fm scrobbles
  - Resolve per-track links: Bandcamp collection -> song.link fallback
  - Render Gutenberg-block content matching mankinlevine.com Listen/Here conventions
  - Resolve WP category + tag IDs (creating tags as needed)
  - POST as draft to mankinlevine.com

Outputs:
  recap_YYYY-MM.html        — local HTML preview
  link_cache.json           — persistent cache of per-track link resolutions
  tag_cache.json            — persistent cache of WP tag name -> id
  lastfm_scrobbles.json     — persistent local mirror of Last.fm scrobbles

Run:
  python recap_compose.py                       # previous calendar month, dry run
  python recap_compose.py --year 2026 --month 2 # explicit window, dry run
  python recap_compose.py --post                # POST draft to WP
"""
import argparse, sqlite3, datetime, json, os, sys, re, base64, urllib.request, urllib.parse, urllib.error, time, unicodedata, html as html_mod, glob, pathlib
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "MM5.DB")
SECRETS = os.path.join(HERE, "secrets.json")
BANDCAMP_CACHE = os.path.join(HERE, "bandcamp_collection.json")
LINK_CACHE = os.path.join(HERE, "link_cache.json")
TAG_CACHE = os.path.join(HERE, "tag_cache.json")
ART_CACHE = os.path.join(HERE, "art_cache.json")
ITUNES_ART_CACHE = os.path.join(HERE, "itunes_art_cache.json")
ARTIST_HIST = os.path.join(HERE, "top_artists_history.json")
LASTFM_CACHE = os.path.join(HERE, "lastfm_scrobbles.json")
THUMBS_DIR = os.path.expandvars(r"%APPDATA%\MediaMonkey5\Thumbs")
UA = "MM-Recap-Addon-Dev/0.5"

# ============================================================
#  CLI args + window selection
# ============================================================
def _default_window():
    """Default window is the previous calendar month."""
    today = datetime.date.today()
    y, m = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
    return y, m

ap = argparse.ArgumentParser(description="Generate a monthly Listen/Here recap.")
ap.add_argument("--year",  type=int, help="Window year (default: previous calendar month)")
ap.add_argument("--month", type=int, help="Window month 1-12 (default: previous calendar month)")
ap.add_argument("--post", action="store_true", help="POST a draft to WordPress (otherwise local preview only)")
ap.add_argument("--skip-lastfm", action="store_true", help="Skip Last.fm scrobble backfill even if creds present")
ap.add_argument("--deep-dives", dest="deep_dives", help="Comma-separated deep-dive picks (rank numbers like '1,3', artist names, 'auto' for top-3-by-ratio, or 'none' to skip). Default: interactive prompt.")
args = ap.parse_args()

if (args.year is None) ^ (args.month is None):
    ap.error("--year and --month must be provided together")
if args.year is None:
    args.year, args.month = _default_window()
if not (1 <= args.month <= 12):
    ap.error("--month must be 1-12")

WINDOW_START = datetime.datetime(args.year, args.month, 1)
WINDOW_END   = datetime.datetime(
    args.year + (1 if args.month == 12 else 0),
    1 if args.month == 12 else args.month + 1,
    1,
)
WINDOW_LABEL = WINDOW_START.strftime("%B, %Y")
HTML_OUT = os.path.join(HERE, f"recap_{WINDOW_START.strftime('%Y-%m')}.html")
POSTING = args.post

# Display tunables — see how Feb data shakes out, adjust as needed
VAULT_YEAR_DOMINANCE_PCT = 0.85   # From-the-Vault: collapse to one-year flat list if >= this fraction of items fall in one year
VAULT_MIN_AVG_PER_MONTH  = 3.0    # From-the-Vault: when no single year dominates, group by month only if avg items/month meets this; else by year
TOP_TRACKS_EXPANDED_TARGET = 10   # Top-Tracks: keep expanding play-count tiers until cumulative shown would exceed this; remainder collapses to "+ N more"
LASTFM_DEDUP_WINDOW_SEC  = 300    # Last.fm dedup window floor (seconds). Effective window per scrobble is max(this, track_length + LASTFM_DEDUP_LEN_BUFFER_SEC) — MM5 logs at ~80% and Last.fm scrobbles at end-of-track, so the gap scales with track length.
LASTFM_DEDUP_LEN_BUFFER_SEC = 120  # extra slack added to track length when computing the effective dedup window (covers clock skew, scrobble queueing)

print(f"Recap window: {WINDOW_LABEL}  (output: {os.path.basename(HTML_OUT)})")

# ============================================================
#  Setup: secrets, caches, DB connection
# ============================================================
with open(SECRETS, "r", encoding="utf-8") as f: secrets = json.load(f)
WP_URL  = secrets["wp_url"].rstrip("/")
WP_USER = secrets["wp_user"]
WP_PASS = secrets["wp_app_password"]
WP_AUTH = base64.b64encode(f"{WP_USER}:{WP_PASS}".encode()).decode("ascii")

link_cache = json.load(open(LINK_CACHE, encoding="utf-8")) if os.path.exists(LINK_CACHE) else {}
tag_cache  = json.load(open(TAG_CACHE,  encoding="utf-8")) if os.path.exists(TAG_CACHE)  else {}
art_cache  = json.load(open(ART_CACHE,  encoding="utf-8")) if os.path.exists(ART_CACHE)  else {}
itunes_art_cache = json.load(open(ITUNES_ART_CACHE, encoding="utf-8")) if os.path.exists(ITUNES_ART_CACHE) else {}
artist_history = json.load(open(ARTIST_HIST, encoding="utf-8")) if os.path.exists(ARTIST_HIST) else {}
bandcamp = json.load(open(BANDCAMP_CACHE, encoding="utf-8"))

# Build hash -> Thumbs path index once. Prefer 500px, fall back to full, then 200px.
print(f"Indexing MM Thumbs at {THUMBS_DIR} ...")
THUMBS_INDEX = {}  # hash -> {'500'|'200'|'full': path}
for p in pathlib.Path(THUMBS_DIR).rglob("*.jpg"):
    name = p.stem
    if name.endswith("-500px"):
        THUMBS_INDEX.setdefault(name[:-6], {})['500'] = str(p)
    elif name.endswith("-200px"):
        THUMBS_INDEX.setdefault(name[:-6], {})['200'] = str(p)
    elif name.endswith("-80px"):
        pass  # too small for blog
    else:
        THUMBS_INDEX.setdefault(name, {})['full'] = str(p)
print(f"  indexed {len(THUMBS_INDEX)} unique album-art hashes")

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.create_collation("IUNICODE", lambda a, b: (a.casefold() > b.casefold()) - (a.casefold() < b.casefold()))
con.row_factory = sqlite3.Row
cur = con.cursor()

# Attach an in-memory aux DB to hold Last.fm-derived play rows (the MM5 DB itself
# is opened read-only). All SQL aggregations query a PlayedAll view that UNIONs
# Played with aux.LastfmExtras, so adding scrobbles affects every stat uniformly.
con.executescript("""
    ATTACH DATABASE ':memory:' AS aux;
    CREATE TABLE aux.LastfmExtras (IDSong INTEGER NOT NULL, PlayDate REAL NOT NULL);
""")

OLE_EPOCH = datetime.datetime(1899, 12, 30)
def dt_to_ole(dt): return (dt - OLE_EPOCH).total_seconds() / 86400.0
def ole_to_dt(o):  return OLE_EPOCH + datetime.timedelta(days=o)

START_OLE = dt_to_ole(WINDOW_START)
END_OLE   = dt_to_ole(WINDOW_END)

def normalize(s):
    if not s: return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"\b(feat\.?|featuring|ft\.?|with)\b.*", "", s)
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def htm(s):  # html-escape for inline text
    return html_mod.escape(s or "", quote=False)

def save_caches():
    with open(LINK_CACHE, "w", encoding="utf-8") as f: json.dump(link_cache, f, ensure_ascii=False, indent=2)
    with open(TAG_CACHE,  "w", encoding="utf-8") as f: json.dump(tag_cache,  f, ensure_ascii=False, indent=2)
    with open(ART_CACHE,  "w", encoding="utf-8") as f: json.dump(art_cache,  f, ensure_ascii=False, indent=2)
    with open(ITUNES_ART_CACHE, "w", encoding="utf-8") as f: json.dump(itunes_art_cache, f, ensure_ascii=False, indent=2)

def predict_wp_slug(name):
    """Approximate WP's sanitize_title behavior for predicting tag URLs."""
    s = (name or "").lower().strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")

def wp_artist_link(name):
    """Render an artist name (bold, no link — internal tag links felt awkward)."""
    return f"<strong>{htm(name)}</strong>"

def album_inline(art_credit, album_title):
    """Render an inline album reference like (<em>Album</em>), linking to Bandcamp when possible."""
    if not album_title: return ""
    bc = fuzzy_bc_album(art_credit, album_title)
    body = f'<em>{htm(album_title)}</em>'
    if bc:
        body = f'<a href="{htm(bc)}">{body}</a>'
    return f' ({body})'

# ============================================================
#  Gutenberg block emitters
#  These produce native block-comment-wrapped markup so the WP editor treats
#  each unit as a real, editable block — not a single Custom HTML blob.
# ============================================================
def _block(name, attrs=None, inner=""):
    if attrs:
        attrs_json = json.dumps(attrs, ensure_ascii=False, separators=(",", ":"))
        open_tag = f"<!-- wp:{name} {attrs_json} -->"
    else:
        open_tag = f"<!-- wp:{name} -->"
    return f"{open_tag}\n{inner}\n<!-- /wp:{name} -->"

def b_para(html_inner):
    return _block("paragraph", None, f"<p>{html_inner}</p>")

def b_heading(text_or_html, level=2, as_html=False):
    body = text_or_html if as_html else htm(text_or_html)
    return _block("heading", {"level": level},
                  f'<h{level} class="wp-block-heading">{body}</h{level}>')

def b_list(inner_li_html_list, ordered=False):
    items = "\n".join(_block("list-item", None, f"<li>{li}</li>") for li in inner_li_html_list)
    attrs = {"ordered": True} if ordered else None
    tag = "ol" if ordered else "ul"
    return _block("list", attrs, f'<{tag} class="wp-block-list">\n{items}\n</{tag}>')

def b_image(src, alt="", media_id=None, link_url=None, size_slug="large"):
    """A wp:image block. media_id is set when posting; for hot-linked URLs it stays None."""
    attrs = {"sizeSlug": size_slug, "linkDestination": "custom" if link_url else "none"}
    if media_id is not None:
        attrs["id"] = media_id
    img_cls = f' class="wp-image-{media_id}"' if media_id is not None else ""
    img = f'<img src="{htm(src)}" alt="{htm(alt)}"{img_cls}/>'
    if link_url:
        img = f'<a href="{htm(link_url)}">{img}</a>'
    return _block("image", attrs,
                  f'<figure class="wp-block-image size-{size_slug}">{img}</figure>')

def b_columns(inner_column_blocks):
    inner = "\n".join(inner_column_blocks)
    return _block("columns", None, f'<div class="wp-block-columns">\n{inner}\n</div>')

def b_column(inner_blocks):
    if isinstance(inner_blocks, (list, tuple)):
        inner = "\n".join(inner_blocks)
    else:
        inner = inner_blocks
    return _block("column", None, f'<div class="wp-block-column">\n{inner}\n</div>')

def b_html(raw_html):
    """Escape hatch for arbitrary HTML (e.g. an artist-initials placeholder tile)."""
    return _block("html", None, raw_html)

def itunes_album_art(artist, album):
    """Find a 600px album-art URL on Apple's CDN. Returns URL or None."""
    key = f"{normalize(artist)}|{normalize(album)}"
    if key in itunes_art_cache:
        return itunes_art_cache[key]
    art_q = re.sub(r"\s*[&]\s*.+$", "", artist).strip() or artist
    qs = urllib.parse.urlencode({"term": f"{art_q} {album}", "entity": "album", "limit": 3, "media": "music"})
    try:
        data = http_get_json(f"https://itunes.apple.com/search?{qs}")
    except Exception:
        itunes_art_cache[key] = None; return None
    nart = normalize(artist); nalb = normalize(album)
    for r in data.get("results", []):
        s_art = normalize(r.get("artistName", ""))
        s_alb = normalize(r.get("collectionName", ""))
        title_match = (s_alb == nalb) or (s_alb and (s_alb in nalb or nalb in s_alb))
        artist_match = (s_art == nart) or (s_art and (s_art in nart or nart in s_art))
        if title_match and artist_match:
            url = r.get("artworkUrl100", "")
            if url:
                # Bump resolution; standard transform that Apple's CDN accepts
                url = url.replace("100x100bb", "600x600bb").replace("100x100", "600x600")
                itunes_art_cache[key] = url
                time.sleep(0.3)
                return url
    itunes_art_cache[key] = None
    time.sleep(0.3)
    return None

# ============================================================
#  Album-art extraction (from MM Thumbs cache) + WP upload
# ============================================================
def find_album_art(art_artist, album):
    """Return local 500px JPEG path, or None."""
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
    if not row: return None
    sizes = THUMBS_INDEX.get(row[0])
    if not sizes: return None
    return sizes.get('500') or sizes.get('full') or sizes.get('200')

def upload_to_wp_media(local_path):
    """Upload a local image to WP media; cache by local path. Returns {url, id} or None."""
    cached = art_cache.get(local_path)
    if isinstance(cached, dict) and cached.get("url"):
        return cached
    if isinstance(cached, str) and cached:
        # Migrate old string-shaped cache entries — we have the URL but no media id
        rec = {"url": cached, "id": None}
        art_cache[local_path] = rec
        return rec
    with open(local_path, "rb") as f:
        data = f.read()
    name = os.path.basename(local_path)
    req = urllib.request.Request(
        f"{WP_URL}/wp-json/wp/v2/media",
        data=data, method="POST",
        headers={
            "Authorization": f"Basic {WP_AUTH}",
            "Content-Type": "image/jpeg",
            "Content-Disposition": f'attachment; filename="{name}"',
            "User-Agent": UA,
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"  ! upload {name} failed: {e.code} {e.read().decode('utf-8','replace')[:200]}")
        return None
    rec = {"url": resp.get("source_url"), "id": resp.get("id")}
    art_cache[local_path] = rec
    return rec

def art_resolve(local_path, posting):
    """Resolve a local Thumbs path → {url, id}. file:// + id=None on dry runs."""
    if not local_path: return None
    if posting:
        return upload_to_wp_media(local_path)
    return {"url": "file:///" + local_path.replace("\\", "/"), "id": None}

def best_art(art_credit, album, local_path, posting):
    """Best-available album art: local Thumbs first, then iTunes hot-link. Returns {url, id} or None."""
    u = art_resolve(local_path, posting)
    if u: return u
    cdn = itunes_album_art(art_credit, album)
    if cdn: return {"url": cdn, "id": None}
    return None

# ============================================================
#  Bandcamp lookup
# ============================================================
album_idx = {}
album_title_idx = {}   # normalized album title -> list of (normalized_band, url)
for it in bandcamp["items"].values():
    band = it.get("band_name") or ""
    title = it.get("item_title") or ""
    url = it.get("item_url") or ""
    if not band or not title or not url: continue
    nb = normalize(band); nt = normalize(title)
    album_idx[(nb, nt)] = url
    album_title_idx.setdefault(nt, []).append((nb, url))

def reduce_artist(name):
    """Strip secondary collaborators: 'X & Y' -> 'X', 'X feat. Y' -> 'X'."""
    if not name: return ""
    s = re.split(r"\s+(?:feat\.?|featuring|ft\.?|with|vs\.?)\s+", name, maxsplit=1, flags=re.IGNORECASE)[0]
    s = re.split(r"\s*[&]\s*", s, maxsplit=1)[0]
    s = re.split(r"\s*,\s*", s, maxsplit=1)[0]
    return s.strip()

def fuzzy_bc_album(art_credit, album_title):
    """Find a Bandcamp album URL with fuzzy artist matching. Returns None if nothing matches."""
    if not album_title: return None
    nalb = normalize(album_title)
    if not nalb: return None
    nart_full = normalize(art_credit or "")
    nart_primary = normalize(reduce_artist(art_credit or ""))

    # Strategy 1: exact (full or primary) artist match
    for key_artist in (nart_full, nart_primary):
        if key_artist and (key_artist, nalb) in album_idx:
            return album_idx[(key_artist, nalb)]

    # Strategy 2 + 3: same album title, fuzzy artist (substring or compilation)
    for bc_band, bc_url in album_title_idx.get(nalb, []):
        # substring either direction (e.g. "the swell season" in
        # "the swell season marketa irglova glen hansard")
        if (bc_band and (bc_band in nart_full or nart_full in bc_band
                         or bc_band in nart_primary or nart_primary in bc_band)):
            return bc_url
        # compilation fallback (Various Artists comp containing this track)
        if bc_band in ("various artists", "various", "va"):
            return bc_url
    return None

# Track-level: build from tracklists keyed by 'aN' or 'tN' to find parent item
tralbum_to_url = {}
for it in bandcamp["items"].values():
    for k in ("tralbum_id", "album_id", "item_id"):
        v = it.get(k)
        if v:
            tralbum_to_url[str(v)] = (it.get("band_name") or "", it.get("item_url") or "")

track_idx = {}
for tl_key, tracks in bandcamp["tracklists"].items():
    if not isinstance(tracks, list): continue
    bare = tl_key.lstrip("at")
    band_url = tralbum_to_url.get(bare)
    if not band_url: continue
    band, parent_url = band_url
    if not parent_url: continue
    for tr in tracks:
        tr_title = tr.get("title") or ""
        if not tr_title: continue
        track_idx[(normalize(band), normalize(tr_title))] = parent_url   # always use parent album URL

def lookup_bandcamp(artist, album_artist, title, album):
    # Album-level match with fuzzy artist (handles " & X" credits, substring,
    # and compilation-on-VariousArtists)
    for credit in (album_artist or artist, artist):
        url = fuzzy_bc_album(credit, album)
        if url: return url
    # Track-level match (returns parent album URL)
    nart = normalize(artist); nalbart = normalize(album_artist or artist)
    ntit = normalize(title)
    for key in [(nart, ntit), (nalbart, ntit), (normalize(reduce_artist(artist)), ntit)]:
        if key in track_idx:
            return track_idx[key]
    return None

# ============================================================
#  iTunes Search -> song.link fallback
# ============================================================
def http_get_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def itunes_best(artist, title, album):
    art_q = re.sub(r"\s*[&]\s*.+$", "", artist).strip() or artist
    qs = urllib.parse.urlencode({"term": f"{art_q} {title}", "entity": "song", "limit": 5, "media": "music"})
    try:
        data = http_get_json(f"https://itunes.apple.com/search?{qs}")
    except Exception:
        return None, 0
    nart = normalize(artist); ntit = normalize(title); nalb = normalize(album or "")
    best, best_score = None, -1
    for r in data.get("results", []):
        s_art = normalize(r.get("artistName", ""))
        s_tit = normalize(r.get("trackName", ""))
        s_alb = normalize(r.get("collectionName", ""))
        # Title is load-bearing — refuse candidates with no title signal at all
        # (prevents artist-only false matches on common artists)
        if s_tit == ntit: t = 3
        elif ntit and (s_tit in ntit or ntit in s_tit): t = 1.5
        else: continue
        a = 0
        if s_art == nart: a = 2
        elif nart and (s_art in nart or nart in s_art): a = 1
        alb = 0
        if nalb and s_alb == nalb: alb = 1
        elif nalb and s_alb and (s_alb in nalb or nalb in s_alb): alb = 0.5
        score = t + a + alb
        if score > best_score: best, best_score = r, score
    return best, best_score

def songlink_pageurl(apple_url):
    qs = urllib.parse.urlencode({"url": apple_url})
    try:
        data = http_get_json(f"https://api.song.link/v1-alpha.1/links?{qs}")
        return data.get("pageUrl")
    except Exception:
        return None

def resolve_link(artist, album_artist, title, album):
    cache_key = f"{normalize(artist)}|{normalize(title)}|{normalize(album)}"
    cached = link_cache.get(cache_key)
    # Only treat cache entries with a real URL as authoritative. "none" entries
    # are retried every run — covers the case where a stricter threshold was
    # used at first-cache time, or where iTunes/Bandcamp got better matches since.
    if cached and cached.get("url"):
        return cached["url"], cached.get("source", "bandcamp")

    # 1. Bandcamp
    bc = lookup_bandcamp(artist, album_artist, title, album)
    if bc:
        link_cache[cache_key] = {"url": bc, "source": "bandcamp"}
        return bc, "bandcamp"

    # 2. iTunes -> song.link (threshold 2: title signal + any artist/album confirmation)
    match, score = itunes_best(artist, title, album)
    if match and score >= 2:
        apple_url = match.get("trackViewUrl")
        if apple_url:
            sl = songlink_pageurl(apple_url)
            if sl:
                link_cache[cache_key] = {"url": sl, "source": "songlink", "score": score}
                time.sleep(0.4)  # polite
                return sl, "songlink"

    link_cache[cache_key] = {"url": None, "source": "none", "score": score if match else None}
    return None, "none"

# ============================================================
#  Last.fm scrobble backfill
#  Fetches new scrobbles, persists them, dedups against in-window MM5 plays,
#  and inserts the non-dup ones into aux.LastfmExtras. All SQL aggregations
#  below query a PlayedAll view that UNIONs Played with aux.LastfmExtras —
#  so adding scrobbles affects every stat (top artists, busiest day, vault, ...)
#  uniformly without per-query plumbing.
# ============================================================
LASTFM_API = "https://ws.audioscrobbler.com/2.0/"
LASTFM_FIRSTRUN_BACKFILL_YEARS = 5    # how far back to seed the cache on first run
LASTFM_PAGE_SLEEP_SEC = 0.25

def _uts_to_naive_utc(uts):
    return datetime.datetime.fromtimestamp(int(uts), tz=datetime.timezone.utc).replace(tzinfo=None)

def _lastfm_extract(track):
    """Pull artist/title/album/uts from a Last.fm track entry; None for now-playing."""
    if (track.get("@attr") or {}).get("nowplaying") in ("true", True):
        return None
    uts = (track.get("date") or {}).get("uts")
    if not uts: return None
    return {
        "uts":    int(uts),
        "artist": (track.get("artist") or {}).get("#text", "") or "",
        "title":  track.get("name", "") or "",
        "album":  (track.get("album")  or {}).get("#text", "") or "",
    }

def fetch_lastfm_scrobbles():
    """Extend the local scrobble cache forward. Returns the full sorted list."""
    if args.skip_lastfm:
        print("Last.fm: skipped via --skip-lastfm")
        return []
    user    = secrets.get("lastfm_user", "")
    api_key = secrets.get("lastfm_api_key", "")
    if not user or not api_key or "your-lastfm" in user or "your-lastfm" in api_key:
        print("Last.fm: creds not set in secrets.json — skipping scrobble backfill")
        return []

    cache = json.load(open(LASTFM_CACHE, encoding="utf-8")) if os.path.exists(LASTFM_CACHE) else {"scrobbles": []}
    scrobbles = cache.get("scrobbles", [])
    if scrobbles:
        from_uts = max(s["uts"] for s in scrobbles) + 1
        print(f"Last.fm: {len(scrobbles):,} cached; fetching since {_uts_to_naive_utc(from_uts).strftime('%Y-%m-%d %H:%M')} UTC")
    else:
        backfill_from = WINDOW_START - datetime.timedelta(days=365 * LASTFM_FIRSTRUN_BACKFILL_YEARS)
        from_uts = int(backfill_from.timestamp())
        print(f"Last.fm: empty cache — backfilling from {backfill_from.strftime('%Y-%m-%d')} (~{LASTFM_FIRSTRUN_BACKFILL_YEARS} years)")

    seen_uts = {s["uts"] for s in scrobbles}
    page, fetched = 1, 0
    while True:
        qs = urllib.parse.urlencode({
            "method": "user.getrecenttracks",
            "user": user, "api_key": api_key, "format": "json",
            "limit": 200, "from": from_uts, "page": page,
        })
        try:
            data = http_get_json(f"{LASTFM_API}?{qs}", timeout=30)
        except urllib.error.HTTPError as e:
            print(f"  ! Last.fm HTTP {e.code}: {e.read().decode('utf-8','replace')[:200]}")
            break
        except Exception as e:
            print(f"  ! Last.fm fetch error on page {page}: {e}")
            break
        rt = (data or {}).get("recenttracks") or {}
        tracks = rt.get("track") or []
        if isinstance(tracks, dict): tracks = [tracks]
        total_pages = int((rt.get("@attr") or {}).get("totalPages") or 1)
        new_on_page = 0
        for tr in tracks:
            ext = _lastfm_extract(tr)
            if not ext or ext["uts"] in seen_uts: continue
            scrobbles.append(ext); seen_uts.add(ext["uts"]); new_on_page += 1
        fetched += new_on_page
        print(f"  page {page}/{total_pages}  (+{new_on_page} new, {fetched} total)")
        if page >= total_pages or not tracks:
            break
        page += 1
        time.sleep(LASTFM_PAGE_SLEEP_SEC)

    scrobbles.sort(key=lambda s: s["uts"])
    with open(LASTFM_CACHE, "w", encoding="utf-8") as f:
        json.dump({"scrobbles": scrobbles}, f, ensure_ascii=False)
    return scrobbles

lastfm_all = fetch_lastfm_scrobbles()

# Restrict to in-window scrobbles, match each to a Songs.ID, dedup vs MM5 plays
window_start_uts = int(WINDOW_START.timestamp())
window_end_uts   = int(WINDOW_END.timestamp())
window_scrobbles = [s for s in lastfm_all if window_start_uts <= s["uts"] < window_end_uts]
print(f"Last.fm: {len(window_scrobbles):,} scrobbles in window")

if window_scrobbles:
    # (norm_artist, norm_title) -> [(SongID, norm_album), ...]  built from MM5 Songs
    song_idx = {}
    for row in cur.execute(
        "SELECT ID, Artist, SongTitle, Album FROM Songs "
        "WHERE SongTitle IS NOT NULL AND Artist IS NOT NULL"
    ):
        a, t = normalize(row["Artist"]), normalize(row["SongTitle"])
        if not a or not t: continue
        song_idx.setdefault((a, t), []).append((row["ID"], normalize(row["Album"] or "")))

    # In-window MM5 plays per song, for variable-window dedup
    mm5_play_idx = {}
    for row in cur.execute(
        "SELECT IDSong, PlayDate FROM Played WHERE PlayDate >= ? AND PlayDate < ?",
        (START_OLE, END_OLE),
    ):
        mm5_play_idx.setdefault(row["IDSong"], []).append(row["PlayDate"])

    # Track length lookup (seconds) — MM5 stores SongLength as ms.
    # The effective dedup window scales with track length: MM5 logs at ~80% of
    # track, Last.fm scrobbles at end, so a 12-min track has ~2.5 min built-in
    # gap before any clock skew. A fixed 5-min window misses long tracks.
    song_len_sec = {}
    for row in cur.execute("SELECT ID, SongLength FROM Songs WHERE SongLength > 0"):
        song_len_sec[row["ID"]] = row["SongLength"] / 1000.0

    matched = unmatched = duplicate = 0
    unmatched_examples = []
    extras_rows = []
    for s in window_scrobbles:
        key = (normalize(s["artist"]), normalize(s["title"]))
        candidates = song_idx.get(key)
        if not candidates:
            unmatched += 1
            if len(unmatched_examples) < 5:
                unmatched_examples.append(f"{s['artist']} — {s['title']}")
            continue
        if len(candidates) > 1 and s["album"]:
            nalb = normalize(s["album"])
            best = [c for c in candidates if c[1] == nalb] or \
                   [c for c in candidates if c[1] and (c[1] in nalb or nalb in c[1])]
            song_id = (best or candidates)[0][0]
        else:
            song_id = candidates[0][0]
        scrobble_ole = dt_to_ole(_uts_to_naive_utc(s["uts"]))
        window_sec = max(LASTFM_DEDUP_WINDOW_SEC,
                         song_len_sec.get(song_id, 0) + LASTFM_DEDUP_LEN_BUFFER_SEC)
        window_ole = window_sec / 86400.0
        if any(abs(p - scrobble_ole) < window_ole for p in mm5_play_idx.get(song_id, [])):
            duplicate += 1
            continue
        matched += 1
        extras_rows.append((song_id, scrobble_ole))

    if extras_rows:
        con.executemany("INSERT INTO aux.LastfmExtras (IDSong, PlayDate) VALUES (?,?)", extras_rows)
    print(f"  merged: {matched} new plays · {duplicate} dup of MM5 · {unmatched} unmatched in Songs")
    if unmatched_examples:
        print("  unmatched examples: " + " | ".join(unmatched_examples))

# Unified view used by every aggregation below
con.executescript("""
    DROP VIEW IF EXISTS PlayedAll;
    CREATE TEMP VIEW PlayedAll AS
      SELECT IDSong, PlayDate FROM Played
      UNION ALL
      SELECT IDSong, PlayDate FROM aux.LastfmExtras
    ;
""")

# ============================================================
#  SQL aggregations
# ============================================================
def Q(sql, params=()):
    return cur.execute(sql, params).fetchall()

# Stats
stats_row = Q("""
    SELECT COUNT(*) AS plays,
           COUNT(DISTINCT p.IDSong) AS unique_tracks,
           COUNT(DISTINCT s.Artist) AS unique_artists,
           SUM(s.SongLength)/1000.0/60.0 AS minutes
    FROM PlayedAll p JOIN Songs s ON s.ID = p.IDSong
    WHERE p.PlayDate >= ? AND p.PlayDate < ?
""", (START_OLE, END_OLE))[0]

busiest = Q("""
    SELECT date(datetime((PlayDate - 25569)*86400, 'unixepoch')) AS d, COUNT(*) c
    FROM PlayedAll WHERE PlayDate >= ? AND PlayDate < ?
    GROUP BY d ORDER BY 2 DESC LIMIT 1
""", (START_OLE, END_OLE))[0]

# Rolling 12-month average for the "vs typical month" stat
baseline_start_ole = START_OLE - 365.0
baseline_total = Q("""
    SELECT COUNT(*) FROM PlayedAll WHERE PlayDate >= ? AND PlayDate < ?
""", (baseline_start_ole, START_OLE))[0][0]
baseline_monthly = (baseline_total / 12.0) if baseline_total else 0.0

# Top artists (by plays in window) — filter Various Artists
top_artists = Q("""
    SELECT s.Artist, COUNT(*) AS plays, COUNT(DISTINCT s.ID) AS uniq
    FROM PlayedAll p JOIN Songs s ON s.ID = p.IDSong
    WHERE p.PlayDate >= ? AND p.PlayDate < ?
      AND s.Artist NOT IN ('Various Artists', 'Various', 'VA')
    GROUP BY s.Artist ORDER BY 2 DESC, 3 DESC LIMIT 10
""", (START_OLE, END_OLE))

# Top albums — rank by plays * tracks_played (rewards both depth-of-listens and
# breadth-across-the-album); filter podcasts/feeds.
top_albums = Q("""
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
    LIMIT 11
""", (START_OLE, END_OLE))

# Top tracks (window) — pull more for tiered display
top_tracks = Q("""
    SELECT s.ID, s.Artist, COALESCE(NULLIF(s.AlbumArtist,''), s.Artist) AS album_artist,
           s.SongTitle, s.Album, s.Rating, COUNT(*) AS plays
    FROM PlayedAll p JOIN Songs s ON s.ID = p.IDSong
    WHERE p.PlayDate >= ? AND p.PlayDate < ?
    GROUP BY p.IDSong ORDER BY 7 DESC, s.Rating DESC LIMIT 60
""", (START_OLE, END_OLE))

# New to me — count first, then top 15 by plays
new_to_me_total = Q("""
    WITH firsts AS (SELECT IDSong, MIN(PlayDate) fp FROM PlayedAll GROUP BY IDSong)
    SELECT COUNT(*) FROM firsts WHERE fp >= ? AND fp < ?
""", (START_OLE, END_OLE))[0][0]

new_to_me = Q("""
    WITH firsts AS (SELECT IDSong, MIN(PlayDate) fp FROM PlayedAll GROUP BY IDSong)
    SELECT s.ID, s.Artist, COALESCE(NULLIF(s.AlbumArtist,''), s.Artist) AS album_artist,
           s.SongTitle, s.Album, s.Rating,
           (SELECT COUNT(*) FROM PlayedAll p2 WHERE p2.IDSong=s.ID AND p2.PlayDate>=? AND p2.PlayDate<?) AS plays
    FROM firsts f JOIN Songs s ON s.ID = f.IDSong
    WHERE f.fp >= ? AND f.fp < ?
    ORDER BY 7 DESC, s.Rating DESC LIMIT 15
""", (START_OLE, END_OLE, START_OLE, END_OLE))

# 5★: total count + top 5 by longest-prior-gap (rare re-surfacers from the canon).
five_star_total = Q("""
    SELECT COUNT(DISTINCT p.IDSong) FROM PlayedAll p JOIN Songs s ON s.ID = p.IDSong
    WHERE p.PlayDate >= ? AND p.PlayDate < ? AND s.Rating = 100
""", (START_OLE, END_OLE))[0][0]

# For each 5★ track played in window, find its most-recent prior play (if any).
# Sort by that ascending — oldest "last heard" floats up. NULL prior means
# played for the first time in window; rank those last so they don't dominate.
five_star = Q("""
    WITH win AS (
      SELECT IDSong, MIN(PlayDate) AS first_in_win
      FROM PlayedAll WHERE PlayDate >= ? AND PlayDate < ? GROUP BY IDSong
    ),
    prior AS (
      SELECT w.IDSong, MAX(p.PlayDate) AS last_before
      FROM win w LEFT JOIN PlayedAll p ON p.IDSong=w.IDSong AND p.PlayDate < w.first_in_win
      GROUP BY w.IDSong
    )
    SELECT s.ID, s.Artist, COALESCE(NULLIF(s.AlbumArtist,''), s.Artist) AS album_artist,
           s.SongTitle, s.Album, pr.last_before AS last_before_ole
    FROM win w JOIN prior pr ON pr.IDSong=w.IDSong JOIN Songs s ON s.ID=w.IDSong
    WHERE s.Rating = 100
    ORDER BY (CASE WHEN pr.last_before IS NULL THEN 1 ELSE 0 END), pr.last_before ASC
    LIMIT 5
""", (START_OLE, END_OLE))

# === Deep Dives: artists significantly above their 12-month baseline ===
# Window plays per artist
deep_dive_candidates = Q("""
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
""", (START_OLE, END_OLE, baseline_start_ole, START_OLE))

deep_dive_pool = []
for art, w_plays, w_tracks, b_plays in deep_dive_candidates:
    monthly_avg = b_plays / 12.0
    if b_plays < 5:   # need enough baseline to call this a "deep dive" not a discovery
        continue
    ratio = w_plays / max(monthly_avg, 0.1)
    if ratio >= 2.5:
        deep_dive_pool.append({
            "art": art, "plays": w_plays, "tracks": w_tracks,
            "monthly_avg": monthly_avg, "ratio": ratio,
        })
deep_dive_pool.sort(key=lambda d: (-d["ratio"], -d["plays"]))

def _parse_deep_dive_arg(arg, pool):
    """Parse --deep-dives arg: rank numbers ('1,3') or artist-name substrings."""
    out, seen = [], set()
    for tok in arg.split(","):
        tok = tok.strip()
        if not tok: continue
        if tok.isdigit():
            i = int(tok) - 1
            if 0 <= i < len(pool) and i not in seen:
                out.append(pool[i]); seen.add(i)
            continue
        tok_n = normalize(tok)
        for i, d in enumerate(pool):
            if i in seen: continue
            art_n = normalize(d["art"])
            if tok_n and (tok_n in art_n or art_n in tok_n):
                out.append(d); seen.add(i); break
    return out

# Interactive picker — list the full candidate pool, take a selection, trim to it.
if deep_dive_pool:
    print("\nDeep Dive candidates:")
    for i, dd in enumerate(deep_dive_pool[:10], 1):   # cap printed list to avoid wall of text
        ratio_str = f"{dd['ratio']:.1f}× typical" if dd["monthly_avg"] >= 1 else "well above usual"
        print(f"  {i:>2}. {dd['art']:<45} {dd['plays']:>3} plays / {dd['tracks']:>2} tr · {ratio_str}")

    raw = args.deep_dives
    if raw is None:
        try:
            raw = input("Pick deep dives (numbers like '1,3', artist names, empty=top 3, 'none' to skip): ").strip()
        except EOFError:
            raw = ""
    raw = (raw or "").strip()

    if raw.lower() == "none":
        deep_dives = []
    elif raw == "" or raw.lower() == "auto":
        deep_dives = deep_dive_pool[:3]
    else:
        deep_dives = _parse_deep_dive_arg(raw, deep_dive_pool)
        if not deep_dives:
            print(f"  ! couldn't match '{raw}' to any candidate; falling back to top 3")
            deep_dives = deep_dive_pool[:3]
    print(f"  → featuring: {', '.join(d['art'] for d in deep_dives) if deep_dives else '(none)'}")
else:
    deep_dives = []

# For each selected deep-dive artist, pull their albums-played in window
for dd in deep_dives:
    rows = Q("""
        SELECT COALESCE(NULLIF(s.AlbumArtist,''), s.Artist) AS art_credit,
               s.Album AS album, COUNT(*) AS plays, COUNT(DISTINCT s.ID) AS tracks
        FROM PlayedAll p JOIN Songs s ON s.ID = p.IDSong
        WHERE p.PlayDate >= ? AND p.PlayDate < ?
          AND s.Artist = ?
          AND s.Album IS NOT NULL AND s.Album <> ''
          AND s.Album NOT LIKE '%Podcast%' AND s.Album NOT LIKE '%KEXP%'
        GROUP BY art_credit, album ORDER BY 3 DESC, 4 DESC
    """, (START_OLE, END_OLE, dd["art"]))
    dd["albums"] = []
    for art_credit, album, plays, tracks in rows:
        dd["albums"].append({
            "art_credit": art_credit, "album": album, "plays": plays, "tracks": tracks,
            "art_path": find_album_art(art_credit, album),
            "url": fuzzy_bc_album(art_credit, album),
        })

# === First encounters: artists whose first-ever play in your library was this month ===
first_encounters_rows = Q("""
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
    LIMIT 5
""", (START_OLE, END_OLE, START_OLE, END_OLE, START_OLE, END_OLE))

# Comeback kids: previous play >= 365 days before window start, and played in window.
# Implemented as: songs with a play in window AND whose previous-most-recent play is >365 days before window start.
year_before = START_OLE - 365.0
comeback = Q("""
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
    ORDER BY 8 DESC, 7 DESC LIMIT 10
""", (START_OLE, END_OLE, year_before))

# (Genre mix dropped — too few genres in active use to be interesting.)

# ============================================================
#  Resolve links for every section that has them
# ============================================================
def annotate(rows):
    out = []
    for r in rows:
        d = dict(r)
        url, source = resolve_link(d["Artist"], d.get("album_artist") or d["Artist"], d["SongTitle"], d.get("Album",""))
        d["url"] = url; d["link_source"] = source
        out.append(d)
    return out

print("Resolving links (Bandcamp + song.link fallback)...")
top_tracks_rs = annotate(top_tracks)
new_to_me_rs  = annotate(new_to_me)
five_star_rs  = annotate(five_star)
comeback_rs   = annotate(comeback)
save_caches()

# Top albums also get a single link (album-level)
def itunes_album_url(artist, album):
    """Apple Music collection URL for an artist+album, or None."""
    art_q = re.sub(r"\s*[&]\s*.+$", "", artist).strip() or artist
    qs = urllib.parse.urlencode({"term": f"{art_q} {album}", "entity": "album", "limit": 5, "media": "music"})
    try:
        data = http_get_json(f"https://itunes.apple.com/search?{qs}")
    except Exception:
        return None
    nart = normalize(artist); nalb = normalize(album)
    for r in data.get("results", []):
        s_art = normalize(r.get("artistName", ""))
        s_alb = normalize(r.get("collectionName", ""))
        title_match = (s_alb == nalb) or (s_alb and (s_alb in nalb or nalb in s_alb))
        artist_match = (s_art == nart) or (s_art and (s_art in nart or nart in s_art))
        if title_match and artist_match:
            return r.get("collectionViewUrl")
    return None

def resolve_album_link(art, album):
    key = f"{normalize(art)}||{normalize(album)}"
    cached = link_cache.get(key)
    if cached and cached.get("url"):
        return cached["url"]
    url = fuzzy_bc_album(art, album)
    source = "bandcamp" if url else None
    if not url:
        apple_url = itunes_album_url(art, album)
        if apple_url:
            sl = songlink_pageurl(apple_url)
            if sl: url, source = sl, "songlink"
            time.sleep(0.4)
    link_cache[key] = {"url": url, "source": source or "none"}
    return url

top_albums_rs = []
for r in top_albums:
    d = dict(r)
    d["url"] = resolve_album_link(d["art"], d["album"])
    d["art_path"] = find_album_art(d["art"], d["album"])  # local Thumbs path or None
    top_albums_rs.append(d)
save_caches()
print(f"  top albums with art available: {sum(1 for d in top_albums_rs if d['art_path'])}/{len(top_albums_rs)}")

# ============================================================
#  Rendering — emit Gutenberg blocks so the WP editor treats each
#  section as real, editable blocks (paragraphs, headings, lists,
#  images, columns) rather than one Custom-HTML blob.
# ============================================================
def link_or_text(label_html, url):
    if url:
        return f'<a href="{htm(url)}">{label_html}</a>'
    return label_html

def li_track(d):
    """Inner-li content for a Top-Tracks entry (without the <li> wrapper)."""
    art_html = wp_artist_link(d["Artist"])
    tit = htm(d["SongTitle"])
    plays = d["plays"]
    track_html = link_or_text(f'<em>{tit}</em>', d.get("url"))
    album_part = album_inline(d.get("album_artist") or d["Artist"], d.get("Album",""))
    return f'{art_html} — {track_html}{album_part} · {plays} {"play" if plays==1 else "plays"}'

def li_track_simple(d):
    """Inner-li content for a 5★ / Vault entry (title — artist (album))."""
    tit = htm(d["SongTitle"])
    track_html = link_or_text(f'<em>{tit}</em>', d.get("url"))
    album_part = album_inline(d.get("album_artist") or d["Artist"], d.get("Album",""))
    return f'{track_html} — {wp_artist_link(d["Artist"])}{album_part}'

def album_cover_block(art_credit, album, local_path, link_url=None, size_slug="large"):
    """A wp:image block for an album cover, with a placeholder fallback when no art is available."""
    art = best_art(art_credit, album, local_path, POSTING)
    if art is None:
        initials = "".join(w[0] for w in (art_credit or "").split()[:2]).upper() or "—"
        return b_html(
            '<div style="aspect-ratio:1/1;background:linear-gradient(135deg,#e8e8e8,#bbb);'
            'display:flex;align-items:center;justify-content:center;'
            'color:#fff;font-size:2.2em;font-weight:600;letter-spacing:.05em">'
            f'{htm(initials)}</div>'
        )
    alt = f"Album cover: {art_credit} – {album}"
    return b_image(art["url"], alt=alt, media_id=art["id"], link_url=link_url, size_slug=size_slug)

def album_card_column(d, big=False):
    """A wp:column for a Top-Albums slot: cover image + 3-line caption paragraph."""
    art_label  = f"<strong>{htm(d['art'])}</strong>"
    album_html = link_or_text(f'<em>{htm(d["album"])}</em>', d.get("url"))
    stat = f'{d["plays"]} plays · {d["tracks_played"]} tracks' if big else f'{d["plays"]}p · {d["tracks_played"]}tr'
    cover   = album_cover_block(d["art"], d["album"], d.get("art_path"),
                                link_url=d.get("url"),
                                size_slug=("large" if big else "medium"))
    caption = b_para(f'{art_label}<br>{album_html}<br><span style="color:#888;font-size:.85em">{stat}</span>')
    return b_column([cover, caption])

def deep_dive_album_column(ab):
    """A wp:column for one of a Deep-Dives artist's albums (smaller, with link)."""
    ab_link = link_or_text(f'<em>{htm(ab["album"])}</em>', ab.get("url"))
    cover   = album_cover_block(ab["art_credit"], ab["album"], ab.get("art_path"),
                                link_url=ab.get("url"), size_slug="medium")
    caption = b_para(f'{ab_link}<br><span style="color:#888;font-size:.85em">{ab["plays"]} plays · {ab["tracks"]} tr</span>')
    return b_column([cover, caption])

def ordinal(n):
    if 11 <= (n % 100) <= 13: return f"{n}th"
    return f"{n}{['th','st','nd','rd','th','th','th','th','th','th'][n%10]}"

parts = []
displayed_top_tracks = []           # populated below, used for tag-resolution
displayed_first_encounters = []

# ---------- Intro ----------
parts.append(b_para(
    f'A new flavor of <em>Listen/Here</em>: instead of a deep dive on a single album, this is '
    f'a snapshot of what I actually listened to in {WINDOW_LABEL}. Source data is my MediaMonkey '
    f'library; links go to Bandcamp where the album is in my collection, otherwise to '
    f'<a href="https://song.link/">song.link</a> for cross-platform options.'
))

# ---------- By the Numbers ----------
parts.append(b_heading("By the Numbers", level=1))
mins = stats_row["minutes"] or 0
hrs  = mins / 60.0
busiest_dt = datetime.datetime.strptime(busiest["d"], "%Y-%m-%d")
busiest_label = f"{busiest_dt.strftime('%B')} {ordinal(busiest_dt.day)}"

delta_pct = ((stats_row["plays"] - baseline_monthly) / baseline_monthly * 100) if baseline_monthly else 0
delta_phrase = ""
if abs(delta_pct) >= 5:
    direction = "above" if delta_pct > 0 else "below"
    delta_phrase = f" — about <strong>{abs(delta_pct):.0f}% {direction}</strong> a typical month over the past year"

parts.append(b_para(
    f'<strong>{stats_row["plays"]:,}</strong> plays across '
    f'<strong>{stats_row["unique_tracks"]:,}</strong> unique tracks by '
    f'<strong>{stats_row["unique_artists"]:,}</strong> distinct artists{delta_phrase}. '
    f'About <strong>{hrs:.0f} hours</strong> of music ({mins:.0f} minutes). '
    f'Busiest day was <strong>{busiest_label}</strong> with {busiest["c"]} plays.'
))

# ---------- Deep Dives ----------
if deep_dives:
    parts.append(b_heading("Deep Dives", level=1))
    parts.append(b_para("Artists whose presence in the rotation jumped well above their usual."))
    for dd in deep_dives:
        ratio_phrase = f"about {dd['ratio']:.1f}× a typical month" if dd["monthly_avg"] >= 1 else "well above usual"
        parts.append(b_heading(dd["art"], level=2))
        parts.append(b_para(
            f'{dd["plays"]} plays across {dd["tracks"]} {"track" if dd["tracks"]==1 else "tracks"} — {ratio_phrase}'
        ))
        if dd["albums"]:
            parts.append(b_columns([deep_dive_album_column(ab) for ab in dd["albums"][:6]]))
        parts.append(b_para('<em>[your thoughts here]</em>'))

# ---------- Top Artists ----------
month_key = WINDOW_START.strftime("%Y-%m")
prior_months_keys = sorted(k for k in artist_history.keys() if k < month_key)

def freq_badge(name):
    if not prior_months_keys: return ""
    apps = sum(1 for k in prior_months_keys if name in artist_history.get(k, []))
    if apps == 0: return ""
    streak = 0
    for k in reversed(prior_months_keys):
        if name in artist_history.get(k, []): streak += 1
        else: break
    badge = f"{ordinal(apps + 1)} time in top 10"
    if streak >= 2:
        badge += f", {streak + 1} months running"
    return f' <span style="color:#888;font-size:.85em">({badge})</span>'

parts.append(b_heading("Top Artists", level=1))
parts.append(b_list([
    f'{wp_artist_link(r["Artist"])} · '
    f'{r["plays"]} plays across {r["uniq"]} {"track" if r["uniq"]==1 else "tracks"}'
    f'{freq_badge(r["Artist"])}'
    for r in top_artists
], ordered=True))

# ---------- Top Albums (3-4-4 grid via wp:columns) ----------
parts.append(b_heading("Top Albums", level=1))
parts.append(b_para('<em>[your thoughts here]</em>'))
row1 = top_albums_rs[:3]
row2 = top_albums_rs[3:7]
row3 = top_albums_rs[7:11]
if row1:
    parts.append(b_columns([album_card_column(d, big=True)  for d in row1]))
if row2:
    parts.append(b_columns([album_card_column(d, big=False) for d in row2]))
if row3:
    parts.append(b_columns([album_card_column(d, big=False) for d in row3]))

# ---------- Top Tracks ----------
parts.append(b_heading("Top Tracks", level=1))
tier_groups = []
last_plays = None
for d in top_tracks_rs:
    if d["plays"] != last_plays:
        tier_groups.append((d["plays"], []))
        last_plays = d["plays"]
    tier_groups[-1][1].append(d)

shown_list_items = []
trailing_summary = None
shown = 0
for i, (plays, tracks) in enumerate(tier_groups):
    # Always show the top tier; keep absorbing tiers while cumulative stays
    # within target. As soon as a tier would push us over, collapse and stop.
    if i == 0 or (shown + len(tracks)) <= TOP_TRACKS_EXPANDED_TARGET:
        for d in tracks:
            shown_list_items.append(li_track(d))
            displayed_top_tracks.append(d)
            shown += 1
    else:
        trailing_summary = f'+ {len(tracks)} more tracks at {plays} plays'
        break

if shown_list_items:
    parts.append(b_list(shown_list_items, ordered=True))
if trailing_summary:
    parts.append(b_para(f'<span style="color:#666">{trailing_summary}</span>'))

# ---------- First Encounters ----------
if first_encounters_rows:
    parts.append(b_heading("First Encounters", level=1))
    parts.append(b_para("Artists whose first-ever play in the library happened this month."))
    fe_lis = []
    for art, plays, tracks in first_encounters_rows:
        displayed_first_encounters.append(art)
        artist_albums = Q("""
            SELECT s.Album, COUNT(*) AS p, COUNT(DISTINCT s.ID) AS t
            FROM PlayedAll p JOIN Songs s ON s.ID = p.IDSong
            WHERE s.Artist = ? AND p.PlayDate >= ? AND p.PlayDate < ?
              AND s.Album IS NOT NULL AND s.Album <> ''
            GROUP BY s.Album ORDER BY 2 DESC
        """, (art, START_OLE, END_OLE))
        extra = ""
        if len(artist_albums) == 1:
            album_name = artist_albums[0][0]
            bc = fuzzy_bc_album(art, album_name)
            inner = f'<em>{htm(album_name)}</em>'
            if bc: inner = f'<a href="{htm(bc)}">{inner}</a>'
            extra = f' · from {inner}'
        elif len(artist_albums) >= 2:
            extra = f' · across {len(artist_albums)} albums'
        fe_lis.append(
            f'{wp_artist_link(art)} · {plays} {"play" if plays==1 else "plays"} '
            f'across {tracks} {"track" if tracks==1 else "tracks"}{extra}'
        )
    parts.append(b_list(fe_lis, ordered=False))

# ---------- Anywhere, Anytime ----------
parts.append(b_heading("Anywhere, Anytime", level=1))
if five_star_rs:
    grouped5 = []
    by_month5 = {}
    for d in five_star_rs:
        last_dt = ole_to_dt(d["last_before_ole"]) if d.get("last_before_ole") else None
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
        parts.append(b_para(f'{five_star_total} of my 5★ tracks surfaced this month. {intro_tail}'))
        parts.append(b_list([li_track_simple(d) for d in five_star_rs], ordered=False))
    else:
        parts.append(b_para(
            f'{five_star_total} of my 5★ tracks surfaced this month. '
            f'{n_word} whose previous play was furthest back:'
        ))
        for label, items in grouped5:
            parts.append(b_para(f'<strong>Last heard {htm(label)}</strong>'))
            parts.append(b_list([li_track_simple(d) for d in items], ordered=False))
else:
    parts.append(b_para('None of the 5★ tracks came up this month.'))

# ---------- From the Vault ----------
parts.append(b_heading("From the Vault", level=1))
if comeback_rs:
    months_order = []; by_month = {}
    years_order  = []; by_year  = {}
    for d in comeback_rs:
        last_dt = ole_to_dt(d["last_before_ole"]) if d.get("last_before_ole") else None
        m_label = last_dt.strftime("%B %Y") if last_dt else "long ago"
        y_label = last_dt.strftime("%Y")     if last_dt else "long ago"
        if m_label not in by_month: by_month[m_label] = []; months_order.append(m_label)
        by_month[m_label].append(d)
        if y_label not in by_year:  by_year[y_label]  = []; years_order.append(y_label)
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
        parts.append(b_para(f'{intro_base} {tail}'))
        parts.append(b_list([li_track_simple(d) for d in comeback_rs], ordered=False))
    elif avg_per_month < VAULT_MIN_AVG_PER_MONTH:
        parts.append(b_para(intro_base))
        for y in years_order:
            parts.append(b_para(f'<strong>{htm(y)}</strong>'))
            parts.append(b_list([li_track_simple(d) for d in by_year[y]], ordered=False))
    else:
        parts.append(b_para(intro_base))
        for m in months_order:
            parts.append(b_para(f'<strong>{htm(m)}</strong>'))
            parts.append(b_list([li_track_simple(d) for d in by_month[m]], ordered=False))
else:
    parts.append(b_para('Nothing returned from a long absence this month.'))

# ---------- Coda ----------
parts.append(b_heading("Coda", level=1))
parts.append(b_para('<em>[your closing thoughts here]</em>'))

# Gutenberg expects a blank line between top-level blocks
html_body = "\n\n".join(parts)

# Save HTML preview — Gutenberg comments are invisible in browsers; the CSS below
# approximates WP's block layout so the dry-run preview still looks roughly right.
_PREVIEW_CSS = """
body { font:16px/1.5 Georgia,serif; max-width:880px; margin:2em auto; padding:0 1em }
h2 { margin-top:2em }
li { margin:.3em 0 }
.wp-block-columns { display:flex; gap:1.2em; flex-wrap:wrap; margin:1em 0 }
.wp-block-column { flex:1 1 0; min-width:0 }
.wp-block-image { margin:0 0 .4em }
.wp-block-image img { width:100%; display:block; aspect-ratio:1/1; object-fit:cover }
.wp-block-image figure { margin:0 }
"""
with open(HTML_OUT, "w", encoding="utf-8") as f:
    f.write(
        f'<!doctype html><meta charset="utf-8"><title>preview</title>'
        f'<style>{_PREVIEW_CSS}</style>{html_body}'
    )
print(f"\nWrote local preview: {HTML_OUT}")

# ============================================================
#  Collect every distinct artist mentioned, for tagging
# ============================================================
def all_artists():
    """Only tag artists that are actually visible in the rendered post body."""
    s = set()
    for r in top_artists: s.add(r["Artist"])
    for d in top_albums_rs: s.add(d["art"])
    for dd in deep_dives: s.add(dd["art"])
    for art in displayed_first_encounters: s.add(art)
    for d in displayed_top_tracks + five_star_rs + comeback_rs:
        s.add(d["Artist"])
        if d.get("album_artist") and d["album_artist"] != d["Artist"]:
            s.add(d["album_artist"])
    return sorted(x.strip() for x in s if x and x.strip())

artist_tags = all_artists()
print(f"\nDistinct artists to tag: {len(artist_tags)}")

# ============================================================
#  WP: tag-resolution + post
# ============================================================
def wp_request(method, path, body=None):
    url = f"{WP_URL}/wp-json/wp/v2/{path.lstrip('/')}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Basic {WP_AUTH}",
        "Content-Type": "application/json",
        "User-Agent": UA,
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode("utf-8", errors="replace")
        try: parsed = json.loads(body_txt)
        except Exception: parsed = {"_raw": body_txt}
        return e.code, parsed

def get_or_create_tag(name):
    if name in tag_cache: return tag_cache[name]
    qs = urllib.parse.urlencode({"search": name, "per_page": 50})
    status, found = wp_request("GET", f"tags?{qs}")
    if status == 200 and isinstance(found, list):
        for t in found:
            if (t.get("name") or "").strip().lower() == name.strip().lower():
                tag_cache[name] = t["id"]; return t["id"]
    status, created = wp_request("POST", "tags", {"name": name})
    if status in (200, 201):
        tag_cache[name] = created["id"]; return created["id"]
    if isinstance(created, dict) and created.get("code") == "term_exists":
        tid = created.get("data", {}).get("term_id")
        if tid: tag_cache[name] = tid; return tid
    print(f"  ! could not resolve/create tag {name!r}: {status} {created}")
    return None

def post_draft():
    print("\nResolving tag IDs...")
    tag_ids = []
    for name in ["Listen/Here"] + artist_tags:
        tid = get_or_create_tag(name)
        if tid: tag_ids.append(tid)
    save_caches()
    print(f"  tag count: {len(tag_ids)}")

    payload = {
        "title":   f"Listen/Here: {WINDOW_LABEL}",
        "content": html_body,
        "status":  "draft",
        "categories": [90],   # Culture
        "tags": tag_ids,
    }
    status, resp = wp_request("POST", "posts", payload)
    if status in (200, 201):
        print(f"\n✓ POSTED draft (id={resp['id']}, status={resp['status']})")
        print(f"  edit URL: {WP_URL}/wp-admin/post.php?post={resp['id']}&action=edit")
    else:
        print(f"\n✗ post failed: {status} {resp}")

if POSTING:
    post_draft()
else:
    print("\n(no --post flag passed; skipping WP POST. Re-run with --post to draft to WP.)")

# Persist this month's top-10 artists to history (always — even on dry run, so
# rerunning the same month doesn't blow it away with stale data each time)
artist_history[month_key] = [r["Artist"] for r in top_artists]
with open(ARTIST_HIST, "w", encoding="utf-8") as f:
    json.dump(artist_history, f, ensure_ascii=False, indent=2)
print(f"  artist history: {len(artist_history)} months tracked")
