"""
Diagnostic: hit Last.fm's user.getRecentTracks to confirm credentials work
end-to-end before letting recap_compose.py do a real backfill.

Reads credentials from secrets.json (default: listen-here/secrets.json),
prints the account's total scrobble count and the 5 most recent tracks.
No writes; safe to run repeatedly.

Usage:
    python scripts/lastfm_test.py [path/to/secrets.json]
"""
import json, sys, os, urllib.request, urllib.parse, urllib.error, datetime
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_SECRETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "secrets.json")
SECRETS = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SECRETS)

if not os.path.exists(SECRETS):
    print(f"ERROR: secrets file not found at {SECRETS}", file=sys.stderr)
    print("Copy secrets.example.json -> secrets.json and fill in your values.", file=sys.stderr)
    sys.exit(1)

with open(SECRETS, "r", encoding="utf-8") as f:
    s = json.load(f)

user    = s.get("lastfm_user", "").strip()
api_key = s.get("lastfm_api_key", "").strip()

if not user or "your-lastfm" in user:
    print("ERROR: lastfm_user is missing or still set to the example placeholder.", file=sys.stderr); sys.exit(1)
if not api_key or "your-lastfm" in api_key:
    print("ERROR: lastfm_api_key is missing or still set to the example placeholder.", file=sys.stderr); sys.exit(1)

qs = urllib.parse.urlencode({
    "method": "user.getrecenttracks",
    "user": user, "api_key": api_key, "format": "json", "limit": 5,
})
endpoint = f"https://ws.audioscrobbler.com/2.0/?{qs}"

print(f"GET ws.audioscrobbler.com/2.0/  method=user.getrecenttracks")
print(f"  user:    {user}")
print(f"  api_key: <{len(api_key)} chars, hidden>")

req = urllib.request.Request(endpoint, headers={"User-Agent": "mediamonkey-wrench/lastfm-test"})
try:
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode("utf-8"))
except urllib.error.HTTPError as e:
    body = e.read().decode("utf-8", errors="replace")
    print(f"\nHTTP {e.code} {e.reason}")
    print(f"  body: {body[:600]}")
    sys.exit(1)
except Exception as e:
    print(f"\nERROR: {e}")
    sys.exit(1)

# Last.fm returns {"error":N,"message":"..."} on auth/parameter errors with HTTP 200
if isinstance(data, dict) and data.get("error"):
    print(f"\nLast.fm error {data['error']}: {data.get('message','?')}")
    sys.exit(1)

rt = data.get("recenttracks") or {}
attr = rt.get("@attr") or {}
tracks = rt.get("track") or []
if isinstance(tracks, dict): tracks = [tracks]

print(f"\n  OK")
print(f"  account total scrobbles: {attr.get('total','?')}")
print(f"  recent (most-recent first):")
for tr in tracks:
    artist = (tr.get("artist") or {}).get("#text", "")
    album  = (tr.get("album")  or {}).get("#text", "")
    title  = tr.get("name", "")
    nowp   = (tr.get("@attr") or {}).get("nowplaying") in ("true", True)
    when   = "NOW PLAYING" if nowp else (tr.get("date") or {}).get("#text", "?")
    album_part = f"  [{album}]" if album else ""
    print(f"    {when:>22}   {artist} — {title}{album_part}")
