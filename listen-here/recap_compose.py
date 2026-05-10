"""
Compose the January 2026 listening recap end-to-end:
  - Aggregate sections from MM5.DB (sandbox copy)
  - Resolve per-track links: Bandcamp collection -> song.link fallback
  - Render HTML matching mankinlevine.com Listen/Here conventions
  - Resolve WP category + tag IDs (creating tags as needed)
  - POST as draft to mankinlevine.com

Outputs:
  recap_jan2026.html — local HTML preview
  link_cache.json    — persistent cache of per-track link resolutions
  tag_cache.json     — persistent cache of WP tag name -> id

Run:
  python recap_compose.py            # dry run only (writes HTML, no POST)
  python recap_compose.py --post     # also POSTs draft to WP
"""
import sqlite3, datetime, json, os, sys, re, base64, urllib.request, urllib.parse, urllib.error, time, unicodedata, html as html_mod, glob, pathlib
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
HTML_OUT = os.path.join(HERE, "recap_jan2026.html")
THUMBS_DIR = os.path.expandvars(r"%APPDATA%\MediaMonkey5\Thumbs")
UA = "MM-Recap-Addon-Dev/0.4"

WINDOW_LABEL = "January, 2026"
WINDOW_START = datetime.datetime(2026, 1, 1)
WINDOW_END   = datetime.datetime(2026, 2, 1)

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
    """Upload a local image to WP media; cache by local path."""
    if local_path in art_cache:
        return art_cache[local_path]
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
    url = resp.get("source_url")
    art_cache[local_path] = url
    return url

def art_url(local_path, posting):
    """Resolve a local Thumbs path to a URL — file:// for dry-run, WP-uploaded URL for posting."""
    if not local_path: return None
    if posting:
        return upload_to_wp_media(local_path)
    return "file:///" + local_path.replace("\\", "/")

def best_art_url(art_credit, album, local_path, posting):
    """Best-available album art URL: local Thumbs first, then iTunes hot-link."""
    u = art_url(local_path, posting)
    if u: return u
    return itunes_album_art(art_credit, album)

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
        score = 0
        if s_tit == ntit: score += 3
        elif ntit and (s_tit in ntit or ntit in s_tit): score += 1.5
        if s_art == nart: score += 2
        elif nart and (s_art in nart or nart in s_art): score += 1
        if nalb and s_alb == nalb: score += 1
        elif nalb and s_alb and (s_alb in nalb or nalb in s_alb): score += 0.5
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
    if cache_key in link_cache:
        c = link_cache[cache_key]
        return c.get("url"), c.get("source")

    # 1. Bandcamp
    bc = lookup_bandcamp(artist, album_artist, title, album)
    if bc:
        link_cache[cache_key] = {"url": bc, "source": "bandcamp"}
        return bc, "bandcamp"

    # 2. iTunes -> song.link (require score >= 3)
    match, score = itunes_best(artist, title, album)
    if match and score >= 3:
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
    FROM Played p JOIN Songs s ON s.ID = p.IDSong
    WHERE p.PlayDate >= ? AND p.PlayDate < ?
""", (START_OLE, END_OLE))[0]

busiest = Q("""
    SELECT date(datetime((PlayDate - 25569)*86400, 'unixepoch')) AS d, COUNT(*) c
    FROM Played WHERE PlayDate >= ? AND PlayDate < ?
    GROUP BY d ORDER BY 2 DESC LIMIT 1
""", (START_OLE, END_OLE))[0]

# Rolling 12-month average for the "vs typical month" stat
baseline_start_ole = START_OLE - 365.0
baseline_total = Q("""
    SELECT COUNT(*) FROM Played WHERE PlayDate >= ? AND PlayDate < ?
""", (baseline_start_ole, START_OLE))[0][0]
baseline_monthly = (baseline_total / 12.0) if baseline_total else 0.0

# Top artists (by plays in window) — filter Various Artists
top_artists = Q("""
    SELECT s.Artist, COUNT(*) AS plays, COUNT(DISTINCT s.ID) AS uniq
    FROM Played p JOIN Songs s ON s.ID = p.IDSong
    WHERE p.PlayDate >= ? AND p.PlayDate < ?
      AND s.Artist NOT IN ('Various Artists', 'Various', 'VA')
    GROUP BY s.Artist ORDER BY 2 DESC, 3 DESC LIMIT 10
""", (START_OLE, END_OLE))

# Top albums — filter podcasts/feeds (we don't want KEXP Song of the Day style)
top_albums = Q("""
    SELECT COALESCE(NULLIF(s.AlbumArtist,''), s.Artist) AS art,
           s.Album AS album,
           COUNT(*) AS plays,
           COUNT(DISTINCT s.ID) AS tracks_played
    FROM Played p JOIN Songs s ON s.ID = p.IDSong
    WHERE p.PlayDate >= ? AND p.PlayDate < ?
      AND s.Album IS NOT NULL AND s.Album <> ''
      AND s.Album NOT LIKE '%Podcast%'
      AND s.Album NOT LIKE '%Song of the Day%'
      AND s.Album NOT LIKE '%KEXP%'
    GROUP BY art, album ORDER BY 3 DESC, 4 DESC LIMIT 10
""", (START_OLE, END_OLE))

# Top tracks (window) — pull more for tiered display
top_tracks = Q("""
    SELECT s.ID, s.Artist, COALESCE(NULLIF(s.AlbumArtist,''), s.Artist) AS album_artist,
           s.SongTitle, s.Album, s.Rating, COUNT(*) AS plays
    FROM Played p JOIN Songs s ON s.ID = p.IDSong
    WHERE p.PlayDate >= ? AND p.PlayDate < ?
    GROUP BY p.IDSong ORDER BY 7 DESC, s.Rating DESC LIMIT 60
""", (START_OLE, END_OLE))

# New to me — count first, then top 15 by plays
new_to_me_total = Q("""
    WITH firsts AS (SELECT IDSong, MIN(PlayDate) fp FROM Played GROUP BY IDSong)
    SELECT COUNT(*) FROM firsts WHERE fp >= ? AND fp < ?
""", (START_OLE, END_OLE))[0][0]

new_to_me = Q("""
    WITH firsts AS (SELECT IDSong, MIN(PlayDate) fp FROM Played GROUP BY IDSong)
    SELECT s.ID, s.Artist, COALESCE(NULLIF(s.AlbumArtist,''), s.Artist) AS album_artist,
           s.SongTitle, s.Album, s.Rating,
           (SELECT COUNT(*) FROM Played p2 WHERE p2.IDSong=s.ID AND p2.PlayDate>=? AND p2.PlayDate<?) AS plays
    FROM firsts f JOIN Songs s ON s.ID = f.IDSong
    WHERE f.fp >= ? AND f.fp < ?
    ORDER BY 7 DESC, s.Rating DESC LIMIT 15
""", (START_OLE, END_OLE, START_OLE, END_OLE))

# 5★: total count + top 5 by longest-prior-gap (rare re-surfacers from the canon).
five_star_total = Q("""
    SELECT COUNT(DISTINCT p.IDSong) FROM Played p JOIN Songs s ON s.ID = p.IDSong
    WHERE p.PlayDate >= ? AND p.PlayDate < ? AND s.Rating = 100
""", (START_OLE, END_OLE))[0][0]

# For each 5★ track played in window, find its most-recent prior play (if any).
# Sort by that ascending — oldest "last heard" floats up. NULL prior means
# played for the first time in window; rank those last so they don't dominate.
five_star = Q("""
    WITH win AS (
      SELECT IDSong, MIN(PlayDate) AS first_in_win
      FROM Played WHERE PlayDate >= ? AND PlayDate < ? GROUP BY IDSong
    ),
    prior AS (
      SELECT w.IDSong, MAX(p.PlayDate) AS last_before
      FROM win w LEFT JOIN Played p ON p.IDSong=w.IDSong AND p.PlayDate < w.first_in_win
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
      FROM Played p JOIN Songs s ON s.ID = p.IDSong
      WHERE p.PlayDate >= ? AND p.PlayDate < ?
        AND s.Artist NOT IN ('Various Artists','Various','VA')
      GROUP BY s.Artist
    ),
    base AS (
      SELECT s.Artist AS art, COUNT(*) AS b_plays
      FROM Played p JOIN Songs s ON s.ID = p.IDSong
      WHERE p.PlayDate >= ? AND p.PlayDate < ?
      GROUP BY s.Artist
    )
    SELECT win.art, win.w_plays, win.w_tracks, COALESCE(base.b_plays,0) AS b_plays
    FROM win LEFT JOIN base USING(art)
    WHERE win.w_plays >= 15
""", (START_OLE, END_OLE, baseline_start_ole, START_OLE))

deep_dives = []
for art, w_plays, w_tracks, b_plays in deep_dive_candidates:
    monthly_avg = b_plays / 12.0
    if b_plays < 5:   # need enough baseline to call this a "deep dive" not a discovery
        continue
    ratio = w_plays / max(monthly_avg, 0.1)
    if ratio >= 2.5:
        deep_dives.append({
            "art": art, "plays": w_plays, "tracks": w_tracks,
            "monthly_avg": monthly_avg, "ratio": ratio,
        })
deep_dives.sort(key=lambda d: (-d["ratio"], -d["plays"]))
deep_dives = deep_dives[:3]

# For each deep-dive artist, pull their albums-played in window
for dd in deep_dives:
    rows = Q("""
        SELECT COALESCE(NULLIF(s.AlbumArtist,''), s.Artist) AS art_credit,
               s.Album AS album, COUNT(*) AS plays, COUNT(DISTINCT s.ID) AS tracks
        FROM Played p JOIN Songs s ON s.ID = p.IDSong
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
      FROM Played p JOIN Songs s ON s.ID = p.IDSong
      WHERE s.Artist IS NOT NULL AND s.Artist <> ''
        AND s.Artist NOT IN ('Various Artists','Various','VA')
      GROUP BY s.Artist
    )
    SELECT fap.art,
           (SELECT COUNT(*) FROM Played p2 JOIN Songs s2 ON s2.ID=p2.IDSong
            WHERE s2.Artist = fap.art AND p2.PlayDate >= ? AND p2.PlayDate < ?) AS plays,
           (SELECT COUNT(DISTINCT p2.IDSong) FROM Played p2 JOIN Songs s2 ON s2.ID=p2.IDSong
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
      FROM Played WHERE PlayDate >= ? AND PlayDate < ? GROUP BY IDSong
    ),
    prior AS (
      SELECT w.IDSong, MAX(p.PlayDate) AS last_before
      FROM win_plays w JOIN Played p ON p.IDSong = w.IDSong
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
def resolve_album_link(art, album):
    key = f"{normalize(art)}||{normalize(album)}"
    if key in link_cache: return link_cache[key].get("url")
    url = fuzzy_bc_album(art, album)
    if not url:
        match, score = itunes_best(art, "", album)  # title blank — match on artist+album
        if match and score >= 2:
            sl = songlink_pageurl(match.get("trackViewUrl",""))
            if sl: url = sl
            time.sleep(0.4)
    link_cache[key] = {"url": url, "source": "bandcamp" if url and "bandcamp" in (url or "") else "songlink" if url else "none"}
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
#  HTML rendering
# ============================================================
def link_or_text(label_html, url):
    if url:
        return f'<a href="{htm(url)}">{label_html}</a>'
    return label_html

def li_track(d):
    art_html = wp_artist_link(d["Artist"])
    tit = htm(d["SongTitle"])
    plays = d["plays"]
    track_html = link_or_text(f'<em>{tit}</em>', d.get("url"))
    album_part = album_inline(d.get("album_artist") or d["Artist"], d.get("Album",""))
    return f'<li>{art_html} — {track_html}{album_part} · {plays} {"play" if plays==1 else "plays"}</li>'

parts = []
POSTING = "--post" in sys.argv
displayed_top_tracks = []   # populated during top-tracks rendering, used for tagging

parts.append(f'<p>A new flavor of <em>Listen/Here</em>: instead of a deep dive on a single album, this is a snapshot of what I actually listened to in {WINDOW_LABEL}. Source data is my MediaMonkey library; links go to Bandcamp where the album is in my collection, otherwise to <a href="https://song.link/">song.link</a> for cross-platform options.</p>')

# By the numbers
def ordinal(n):
    if 11 <= (n % 100) <= 13: return f"{n}th"
    return f"{n}{['th','st','nd','rd','th','th','th','th','th','th'][n%10]}"

parts.append('<h2>By the numbers</h2>')
mins = stats_row["minutes"] or 0
hrs  = mins / 60.0
busiest_dt = datetime.datetime.strptime(busiest["d"], "%Y-%m-%d")
busiest_label = f"{busiest_dt.strftime('%B')} {ordinal(busiest_dt.day)}"

# Optional comparison line if month is meaningfully off baseline
delta_pct = ((stats_row["plays"] - baseline_monthly) / baseline_monthly * 100) if baseline_monthly else 0
delta_phrase = ""
if abs(delta_pct) >= 5:
    direction = "above" if delta_pct > 0 else "below"
    delta_phrase = f" — about <strong>{abs(delta_pct):.0f}% {direction}</strong> a typical month over the past year"

parts.append(
    f'<p><strong>{stats_row["plays"]:,}</strong> plays across '
    f'<strong>{stats_row["unique_tracks"]:,}</strong> unique tracks by '
    f'<strong>{stats_row["unique_artists"]:,}</strong> distinct artists{delta_phrase}. '
    f'About <strong>{hrs:.0f} hours</strong> of music ({mins:.0f} minutes). '
    f'Busiest day was <strong>{busiest_label}</strong> with {busiest["c"]} plays.</p>'
)

# Deep Dives — artist-anchored, baseline-aware, with per-artist commentary slot
def render_album_thumb(album_d, size_px):
    art_credit = htm(album_d["art_credit"]); album = htm(album_d["album"])
    url = album_d.get("url")
    img_src = best_art_url(album_d["art_credit"], album_d["album"], album_d.get("art_path"), POSTING)
    img_html = (
        f'<img src="{htm(img_src)}" alt="Album cover: {art_credit} – {album}" '
        f'style="width:{size_px}px;height:{size_px}px;object-fit:cover;display:block">'
        if img_src else
        f'<div style="width:{size_px}px;height:{size_px}px;background:#eee;display:flex;'
        'align-items:center;justify-content:center;color:#888;font-size:0.8em">no art</div>'
    )
    if url:
        img_html = f'<a href="{htm(url)}">{img_html}</a>'
    return img_html

if deep_dives:
    parts.append('<h2>Deep Dives</h2>')
    parts.append('<p>Artists whose presence in the rotation jumped well above their usual.</p>')
    for dd in deep_dives:
        ratio_phrase = f"about {dd['ratio']:.1f}× a typical month" if dd["monthly_avg"] >= 1 else "well above usual"
        parts.append(
            '<div style="margin:1.8em 0;padding-top:1em;border-top:1px solid #eee">'
            f'<h3 style="margin:0 0 .2em">{htm(dd["art"])}</h3>'
            f'<p style="margin:0 0 .8em;color:#666;font-size:.9em">'
            f'{dd["plays"]} plays across {dd["tracks"]} {"track" if dd["tracks"]==1 else "tracks"} — {ratio_phrase}'
            '</p>'
        )
        if dd["albums"]:
            parts.append(
                '<div style="display:flex;gap:.8em;flex-wrap:wrap;margin:.6em 0 1em">'
            )
            for ab in dd["albums"][:6]:   # cap to 6 album thumbs to avoid sprawl
                ab_album = htm(ab["album"])
                ab_link = link_or_text(f'<em>{ab_album}</em>', ab.get("url"))
                parts.append(
                    '<figure style="margin:0;flex:0 0 auto">'
                    f'{render_album_thumb(ab, 130)}'
                    '<figcaption style="margin-top:.3em;font-size:.78em;line-height:1.25;'
                    'max-width:130px;color:#444">'
                    f'{ab_link}<br>'
                    f'<span style="color:#888">{ab["plays"]} plays · {ab["tracks"]} tr</span>'
                    '</figcaption></figure>'
                )
            parts.append('</div>')
        parts.append(f'<p><em>[your thoughts here]</em></p></div>')

# Top artists — with linked names + frequency badge based on history
month_key = WINDOW_START.strftime("%Y-%m")
prior_months_keys = sorted(k for k in artist_history.keys() if k < month_key)

def freq_badge(name):
    if not prior_months_keys: return ""   # nothing tracked yet
    apps = sum(1 for k in prior_months_keys if name in artist_history.get(k, []))
    if apps == 0: return ""               # first appearance, no badge
    # streak: how many consecutive prior months ending immediately before this one
    streak = 0
    for k in reversed(prior_months_keys):
        if name in artist_history.get(k, []): streak += 1
        else: break
    suffix = "nd" if apps == 2 else "rd" if apps == 3 else "th"
    badge = f"{apps+1}{suffix} time in top 10"
    if streak >= 2:
        badge += f", {streak+1} months running"
    return f' <span style="color:#888;font-size:.85em">({badge})</span>'

parts.append('<h2>Top artists</h2><ol>')
for r in top_artists:
    parts.append(
        f'<li>{wp_artist_link(r["Artist"])} · '
        f'{r["plays"]} plays across {r["uniq"]} {"track" if r["uniq"]==1 else "tracks"}'
        f'{freq_badge(r["Artist"])}</li>'
    )
parts.append('</ol>')

# Top albums — 3 / 4 / 3 visual layout
parts.append('<h2>Top albums</h2>')

def render_album_card(d, big=False):
    art = htm(d["art"]); album = htm(d["album"])
    url = d.get("url")
    art_label = f"<strong>{art}</strong>"
    img_src = best_art_url(d["art"], d["album"], d.get("art_path"), POSTING)
    if img_src:
        img_html = (
            f'<img src="{htm(img_src)}" alt="Album cover: {art} – {album}" '
            'style="width:100%;display:block;aspect-ratio:1/1;object-fit:cover">'
        )
    else:
        # Cleaner missing-art placeholder: artist initials in a styled tile
        initials = "".join(w[0] for w in d["art"].split()[:2]).upper() or "—"
        img_html = (
            '<div style="aspect-ratio:1/1;background:linear-gradient(135deg,#e8e8e8,#bbb);'
            'display:flex;align-items:center;justify-content:center;'
            'color:#fff;font-size:2.2em;font-weight:600;letter-spacing:.05em">'
            f'{htm(initials)}</div>'
        )
    if url:
        img_html = f'<a href="{htm(url)}">{img_html}</a>'
    fs = ".95em" if big else ".82em"
    sub = ".88em" if big else ".75em"
    stat = f'{d["plays"]} plays · {d["tracks_played"]} tracks' if big else f'{d["plays"]}p · {d["tracks_played"]}tr'
    return (
        '<figure style="margin:0">' + img_html +
        f'<figcaption style="margin-top:.45em;font-size:{fs};line-height:1.3">'
        f'{art_label}<br>'
        f'{link_or_text(f"<em>{album}</em>", url)}<br>'
        f'<span style="color:#888;font-size:{sub}">{stat}</span>'
        '</figcaption></figure>'
    )

row1 = top_albums_rs[:3]
row2 = top_albums_rs[3:7]
row3 = top_albums_rs[7:10]

if row1:
    parts.append('<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:1.2em;margin:1em 0">')
    for d in row1: parts.append(render_album_card(d, big=True))
    parts.append('</div>')
if row2:
    parts.append('<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:.9em;margin:1em 0">')
    for d in row2: parts.append(render_album_card(d, big=False))
    parts.append('</div>')
if row3:
    parts.append('<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:.9em;margin:1em 0">')
    for d in row3: parts.append(render_album_card(d, big=False))
    parts.append('</div>')

# Top tracks — show top tier(s) as items, summarize lower tiers as one-line counts
parts.append('<h2>Top tracks</h2>')
groups = []          # list of (plays, [tracks])
last_plays = None
for d in top_tracks_rs:
    if d["plays"] != last_plays:
        groups.append((d["plays"], []))
        last_plays = d["plays"]
    groups[-1][1].append(d)

shown = 0
parts.append('<ol>')
i = 0
while i < len(groups):
    plays, tracks = groups[i]
    # Show this tier fully if it's the first tier OR if it has only a few items AND we haven't shown much yet
    show_fully = (i == 0) or (len(tracks) <= 2 and shown < 5)
    if show_fully:
        for d in tracks:
            parts.append(li_track(d))
            displayed_top_tracks.append(d)
            shown += 1
        i += 1
    else:
        parts.append(f'<li style="list-style:none;color:#666;margin-top:.4em">+ {len(tracks)} more tracks at {plays} plays</li>')
        i += 1
        # only show one collapsed tier; below that it gets noisy
        break
parts.append('</ol>')

# First encounters — artists whose first-ever play in the library was in window
displayed_first_encounters = []
if first_encounters_rows:
    parts.append('<h2>First encounters</h2>')
    parts.append('<p>Artists whose first-ever play in the library happened this month.</p><ul>')
    for art, plays, tracks in first_encounters_rows:
        displayed_first_encounters.append(art)
        # Lookup the albums their plays came from this month
        artist_albums = Q("""
            SELECT s.Album, COUNT(*) AS p, COUNT(DISTINCT s.ID) AS t
            FROM Played p JOIN Songs s ON s.ID = p.IDSong
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
        parts.append(
            f'<li>{wp_artist_link(art)} · {plays} {"play" if plays==1 else "plays"} '
            f'across {tracks} {"track" if tracks==1 else "tracks"}{extra}</li>'
        )
    parts.append('</ul>')

# Anywhere, Anytime — 5 from the 5★ list, grouped by Month YYYY of prior play
parts.append('<h2>Anywhere, Anytime</h2>')
if five_star_rs:
    parts.append(
        f'<p>{five_star_total} of my 5★ tracks surfaced this month. Five whose previous play was furthest back:</p>'
    )
    grouped5 = []
    by_month5 = {}
    for d in five_star_rs:
        last_dt = ole_to_dt(d["last_before_ole"]) if d.get("last_before_ole") else None
        label = last_dt.strftime("%B %Y") if last_dt else "First time on record"
        if label not in by_month5:
            by_month5[label] = []
            grouped5.append((label, by_month5[label]))
        by_month5[label].append(d)
    for label, items in grouped5:
        parts.append(f'<p style="margin:1em 0 .2em"><strong>Last heard {htm(label)}</strong></p><ul style="margin:0 0 1em">')
        for d in items:
            tit = htm(d["SongTitle"])
            track_html = link_or_text(f'<em>{tit}</em>', d.get("url"))
            album_part = album_inline(d.get("album_artist") or d["Artist"], d.get("Album",""))
            parts.append(f'<li>{track_html} — {wp_artist_link(d["Artist"])}{album_part}</li>')
        parts.append('</ul>')
else:
    parts.append('<p>None of the 5★ tracks came up this month.</p>')

# From the Vault — grouped by Month YYYY, oldest first
parts.append('<h2>From the Vault</h2>')
if comeback_rs:
    parts.append('<p>Tracks I hadn\'t reached for in over a year, returning this month.</p>')
    # Group by "Month YYYY" of last_before_ole; preserve oldest-first order from SQL
    grouped = []   # list of (month_label, [items]); preserves insertion order
    by_month = {}
    for d in comeback_rs:
        last_dt = ole_to_dt(d["last_before_ole"]) if d.get("last_before_ole") else None
        label = last_dt.strftime("%B %Y") if last_dt else "long ago"
        if label not in by_month:
            by_month[label] = []
            grouped.append((label, by_month[label]))
        by_month[label].append(d)
    for label, items in grouped:
        parts.append(f'<p style="margin:1em 0 .2em"><strong>{htm(label)}</strong></p><ul style="margin:0 0 1em">')
        for d in items:
            tit = htm(d["SongTitle"])
            track_html = link_or_text(f'<em>{tit}</em>', d.get("url"))
            album_part = album_inline(d.get("album_artist") or d["Artist"], d.get("Album",""))
            parts.append(f'<li>{track_html} — {wp_artist_link(d["Artist"])}{album_part}</li>')
        parts.append('</ul>')
else:
    parts.append('<p>Nothing returned from a long absence this month.</p>')

# (Genre mix section dropped.)

# Closing notes placeholder
parts.append('<h2>Notes</h2><p><em>[your closing thoughts here]</em></p>')

html_body = "\n".join(parts)

# Save HTML preview
with open(HTML_OUT, "w", encoding="utf-8") as f:
    f.write(f'<!doctype html><meta charset="utf-8"><title>preview</title><style>body{{font:16px/1.5 Georgia,serif;max-width:780px;margin:2em auto;padding:0 1em}}h2{{margin-top:2em}}li{{margin:.3em 0}}</style>{html_body}')
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

if "--post" in sys.argv:
    post_draft()
else:
    print("\n(no --post flag passed; skipping WP POST. Re-run with --post to draft to WP.)")

# Persist this month's top-10 artists to history (always — even on dry run, so
# rerunning the same month doesn't blow it away with stale data each time)
artist_history[month_key] = [r["Artist"] for r in top_artists]
with open(ARTIST_HIST, "w", encoding="utf-8") as f:
    json.dump(artist_history, f, ensure_ascii=False, indent=2)
print(f"  artist history: {len(artist_history)} months tracked")
