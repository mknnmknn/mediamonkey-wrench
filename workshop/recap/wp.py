"""
WordPress REST client. Wraps wp/v2/{media,tags,posts} with the bits this
project needs: image upload, tag find-or-create with persistent caching,
and draft posting.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from .http import DEFAULT_UA


class WPClient:
    def __init__(self, wp_url: str, user: str, app_password: str,
                 *, ua: str = DEFAULT_UA, tag_cache_path: str | None = None):
        self.url = wp_url.rstrip("/")
        self.user = user
        self.token = base64.b64encode(f"{user}:{app_password}".encode()).decode("ascii")
        self.ua = ua
        self.tag_cache_path = tag_cache_path
        self.tag_cache: dict[str, int] = (
            json.load(open(tag_cache_path, encoding="utf-8"))
            if tag_cache_path and os.path.exists(tag_cache_path) else {}
        )

    # -- raw request --

    def _request(self, method: str, path: str, body=None) -> tuple[int, dict | list]:
        url = f"{self.url}/wp-json/wp/v2/{path.lstrip('/')}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Basic {self.token}",
            "Content-Type": "application/json",
            "User-Agent": self.ua,
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body_txt = e.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(body_txt)
            except Exception:
                parsed = {"_raw": body_txt}
            return e.code, parsed

    # -- media --

    def upload_image(self, local_path: str) -> dict | None:
        """Upload a local image to WP media library. Returns {url, id} or None."""
        with open(local_path, "rb") as f:
            data = f.read()
        name = os.path.basename(local_path)
        req = urllib.request.Request(
            f"{self.url}/wp-json/wp/v2/media",
            data=data, method="POST",
            headers={
                "Authorization": f"Basic {self.token}",
                "Content-Type": "image/jpeg",
                "Content-Disposition": f'attachment; filename="{name}"',
                "User-Agent": self.ua,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                resp = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            print(f"  ! upload {name} failed: {e.code} "
                  f"{e.read().decode('utf-8','replace')[:200]}")
            return None
        return {"url": resp.get("source_url"), "id": resp.get("id")}

    # -- tags --

    def get_or_create_tag(self, name: str) -> int | None:
        if name in self.tag_cache:
            return self.tag_cache[name]
        qs = urllib.parse.urlencode({"search": name, "per_page": 50})
        status, found = self._request("GET", f"tags?{qs}")
        if status == 200 and isinstance(found, list):
            for t in found:
                if (t.get("name") or "").strip().lower() == name.strip().lower():
                    self.tag_cache[name] = t["id"]
                    return t["id"]
        status, created = self._request("POST", "tags", {"name": name})
        if status in (200, 201) and isinstance(created, dict):
            self.tag_cache[name] = created["id"]
            return created["id"]
        if isinstance(created, dict) and created.get("code") == "term_exists":
            tid = created.get("data", {}).get("term_id")
            if tid:
                self.tag_cache[name] = tid
                return tid
        print(f"  ! could not resolve/create tag {name!r}: {status} {created}")
        return None

    def save_tag_cache(self) -> None:
        if self.tag_cache_path:
            with open(self.tag_cache_path, "w", encoding="utf-8") as f:
                json.dump(self.tag_cache, f, ensure_ascii=False, indent=2)

    # -- posts --

    def post_draft(self, *, title: str, content_html: str,
                   tag_ids: list[int], category_ids: list[int]) -> tuple[int, dict | list]:
        return self._request("POST", "posts", {
            "title":      title,
            "content":    content_html,
            "status":     "draft",
            "categories": category_ids,
            "tags":       tag_ids,
        })

    # -- convenience --

    def edit_url(self, post_id: int) -> str:
        return f"{self.url}/wp-admin/post.php?post={post_id}&action=edit"
