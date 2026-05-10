"""
Diagnostic: post a tiny draft to your WordPress site to confirm the auth
chain (REST API + Application Password) is working end-to-end.

Reads credentials from secrets.json (default: listen-here/secrets.json).
Leaves a draft in WP — review/delete in wp-admin once verified.

Usage:
    python scripts/wp_test.py [path/to/secrets.json]
"""
import json, sys, base64, urllib.request, urllib.error, datetime, os
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_SECRETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "secrets.json")
SECRETS = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SECRETS)

if not os.path.exists(SECRETS):
    print(f"ERROR: secrets file not found at {SECRETS}", file=sys.stderr)
    print("Copy secrets.example.json -> secrets.json and fill in your values.", file=sys.stderr)
    sys.exit(1)

with open(SECRETS, "r", encoding="utf-8") as f:
    s = json.load(f)

wp_url = s["wp_url"].rstrip("/")
wp_user = s["wp_user"]
wp_pass = s["wp_app_password"]   # spaces are fine; WP normalizes

auth_token = base64.b64encode(f"{wp_user}:{wp_pass}".encode("utf-8")).decode("ascii")

now = datetime.datetime.now()
title = f"WP connection test — {now.strftime('%Y-%m-%d %H:%M:%S')}"
content = (
    "<p><strong>This is a connection test from <code>listen-here</code>.</strong></p>"
    "<p>If you see this draft in your WP admin, the auth chain is working "
    "(WordPress REST API + Application Password). Safe to delete.</p>"
)

payload = json.dumps({
    "title": title,
    "content": content,
    "status": "draft",     # never auto-publish
    "excerpt": "Connection test from listen-here.",
}).encode("utf-8")

endpoint = f"{wp_url}/wp-json/wp/v2/posts"
print(f"POST {endpoint}")
print(f"  user: {wp_user}")
print(f"  password: <{len(wp_pass)} chars, hidden>")
print(f"  title: {title}")

req = urllib.request.Request(
    endpoint,
    data=payload,
    method="POST",
    headers={
        "Authorization": f"Basic {auth_token}",
        "Content-Type": "application/json",
        "User-Agent": "mediamonkey-wrench/listen-here",
    },
)

try:
    with urllib.request.urlopen(req, timeout=30) as r:
        status = r.status
        resp = json.loads(r.read().decode("utf-8"))
except urllib.error.HTTPError as e:
    body = e.read().decode("utf-8", errors="replace")
    print(f"\nHTTP {e.code} {e.reason}")
    print(f"  body: {body[:600]}")
    sys.exit(1)
except Exception as e:
    print(f"\nERROR: {e}")
    sys.exit(1)

print(f"\n  HTTP {status}  OK")
print(f"  post id:    {resp.get('id')}")
print(f"  status:     {resp.get('status')}")
print(f"  preview:    {resp.get('link')}")
print(f"  edit URL:   {wp_url}/wp-admin/post.php?post={resp.get('id')}&action=edit")
print(f"  drafts:     {wp_url}/wp-admin/edit.php?post_status=draft&post_type=post")
