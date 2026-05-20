"""
Per-track and per-album link resolution.

Flow (track-level):
  1. Local link cache hit with a *real* URL          -> return cached URL
  2. Bandcamp fan-collection match                   -> return Bandcamp URL
  3. iTunes Search match (score >= 2) -> song.link  -> return song.link URL
  4. None — cached as such, but retried next run (in case thresholds/matchers improve)

Cache entries marked source="none" are intentionally NOT authoritative — we
retry them every run. That's how loosening the iTunes threshold later picks up
old failures without nuking the cache.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from .bandcamp import BandcampIndex
from .http import DEFAULT_UA, get_json
from .normalize import normalize


class LinkResolver:
    def __init__(self,
                 link_cache_path: str,
                 bandcamp: BandcampIndex,
                 *,
                 songlink_sleep_sec: float = 0.4,
                 itunes_sleep_sec: float = 0.3,
                 log = None):
        self.path = link_cache_path
        self.bc = bandcamp
        self.songlink_sleep_sec = songlink_sleep_sec
        self.itunes_sleep_sec = itunes_sleep_sec
        self._log = log or (lambda *a, **kw: None)
        self.cache: dict[str, dict] = (
            json.load(open(link_cache_path, encoding="utf-8"))
            if os.path.exists(link_cache_path) else {}
        )
        # Per-run outcome counters; printed by `summary()` at compose end.
        self.stats: dict[str, int] = {
            "cache_hit": 0, "bandcamp_hit": 0, "songlink_hit": 0,
            "itunes_no_results": 0, "itunes_low_score": 0,
            "itunes_error": 0, "songlink_error": 0,
            "songlink_rate_limited": 0, "no_trackview": 0,
        }
        # If song.link returns 429, set this to skip further calls for the rest
        # of the run — they'll all 429 anyway and we waste time hammering them.
        self._songlink_disabled = False

    # -- public --

    def resolve_track(self, artist: str, album_artist: str | None,
                      title: str, album: str | None) -> tuple[str | None, str]:
        """Return (url, source) where source ∈ {'bandcamp','songlink','none'}."""
        key = f"{normalize(artist)}|{normalize(title)}|{normalize(album)}"
        cached = self.cache.get(key)
        # Only trust cache entries whose url is a real string — self-heal past
        # corruption (e.g. an old code path wrote a tuple here, now serialized
        # as a JSON list; treat it as a miss and re-resolve cleanly).
        if cached and isinstance(cached.get("url"), str) and cached["url"]:
            self.stats["cache_hit"] += 1
            return cached["url"], cached.get("source", "bandcamp")

        bc = self.bc.lookup(artist, album_artist, title, album)
        if bc:
            self.cache[key] = {"url": bc, "source": "bandcamp"}
            self.stats["bandcamp_hit"] += 1
            return bc, "bandcamp"

        match, score, itunes_status = self._itunes_best(artist, title, album)
        if itunes_status == "error":
            self.stats["itunes_error"] += 1
            self._log(f"  link: ITUNES-ERROR  {artist!s} — {title!s}")
            # Don't poison the cache with a "none" on transient errors — leave
            # it absent so the next run retries.
            return None, "none"

        if not match:
            self.stats["itunes_no_results"] += 1
            self._log(f"  link: no-match    {artist!s} — {title!s}")
            self.cache[key] = {"url": None, "source": "none", "score": None}
            return None, "none"

        if score < 2:
            self.stats["itunes_low_score"] += 1
            self._log(f"  link: low-score   {artist!s} — {title!s}  (score={score}, "
                      f"got {match.get('artistName','?')} / {match.get('trackName','?')})")
            self.cache[key] = {"url": None, "source": "none", "score": score}
            return None, "none"

        apple_url = match.get("trackViewUrl")
        if not apple_url:
            self.stats["no_trackview"] += 1
            self.cache[key] = {"url": None, "source": "none", "score": score}
            return None, "none"

        sl, sl_status = self._songlink_pageurl(apple_url)
        if sl_status in ("rate-limited", "disabled"):
            # Don't cache — preserve the entry so a future run (after the limit
            # clears) can try again. Without this, every rate-limited track
            # would get cached as "none" and stay broken even when song.link
            # is healthy again.
            self.stats["songlink_rate_limited"] += 1
            return None, "none"
        if sl_status == "error":
            self.stats["songlink_error"] += 1
            self._log(f"  link: songlink-error  {artist!s} — {title!s}")
            # Same logic — don't poison cache on transient errors
            return None, "none"
        if not sl:
            # status=ok but no pageUrl — legit "song.link doesn't have this one"
            self.stats["songlink_error"] += 1
            self.cache[key] = {"url": None, "source": "none", "score": score}
            return None, "none"

        self.cache[key] = {"url": sl, "source": "songlink", "score": score}
        time.sleep(self.songlink_sleep_sec)
        self.stats["songlink_hit"] += 1
        return sl, "songlink"

    def summary(self) -> str:
        s = self.stats
        parts = [
            f"{s['cache_hit']} cache",
            f"{s['bandcamp_hit']} bandcamp",
            f"{s['songlink_hit']} songlink",
            f"{s['itunes_no_results']} no-itunes-match",
            f"{s['itunes_low_score']} below-threshold",
            f"{s['itunes_error']} itunes-errors",
            f"{s['songlink_error']} songlink-errors",
        ]
        if s["songlink_rate_limited"]:
            parts.append(f"{s['songlink_rate_limited']} songlink-RATE-LIMITED")
        if s["no_trackview"]:
            parts.append(f"{s['no_trackview']} no-trackview")
        return "link resolver: " + " · ".join(parts)

    def resolve_album(self, artist: str, album: str) -> str | None:
        """Album-level link: Bandcamp preferred, Apple Music -> song.link fallback."""
        key = f"{normalize(artist)}||{normalize(album)}"
        cached = self.cache.get(key)
        if cached and isinstance(cached.get("url"), str) and cached["url"]:
            return cached["url"]

        url = self.bc.fuzzy_album(artist, album)
        source = "bandcamp" if url else None
        if not url:
            apple_url = self._itunes_album_url(artist, album)
            if apple_url:
                sl, sl_status = self._songlink_pageurl(apple_url)
                if sl_status in ("rate-limited", "disabled"):
                    # Don't poison cache — next run can pick up after limit clears
                    return None
                if sl_status == "ok" and sl:
                    url, source = sl, "songlink"
                    time.sleep(self.songlink_sleep_sec)
        self.cache[key] = {"url": url, "source": source or "none"}
        return url

    def save(self) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.cache, f, ensure_ascii=False, indent=2)

    # -- internals --

    def _itunes_best(self, artist: str, title: str, album: str | None):
        """
        Best (track, score, status) from iTunes Search. Status is "ok" or "error";
        the resolver treats "error" as transient (won't poison the cache).
        Refuses candidates with no title signal at all to prevent artist-only
        false matches on common artists.

        Strips parens groups from the title before searching ("Foo (Live)" -> "Foo",
        "This Must Be The Place (Naive Melody)" -> "This Must Be The Place") since
        iTunes's tokenizer treats parens as literal and often returns no results.
        """
        art_q = re.sub(r"\s*[&]\s*.+$", "", artist).strip() or artist
        # Strip parenthetical suffixes from the *query* (we still score against
        # the full title for scoring purposes, so disambiguation stays intact).
        title_q = re.sub(r"\s*\([^)]*\)\s*", " ", title).strip() or title
        qs = urllib.parse.urlencode({
            "term": f"{art_q} {title_q}",
            "entity": "song",
            "limit": 5,
            "media": "music",
        })
        try:
            data = get_json(f"https://itunes.apple.com/search?{qs}")
        except Exception as e:
            self._log(f"  itunes: HTTP error for {artist!s} — {title!s}: {e}")
            return None, 0, "error"
        nart = normalize(artist)
        ntit = normalize(title)
        nalb = normalize(album or "")
        best, best_score = None, -1
        for r in data.get("results", []):
            s_art = normalize(r.get("artistName", ""))
            s_tit = normalize(r.get("trackName", ""))
            s_alb = normalize(r.get("collectionName", ""))
            if s_tit == ntit:
                t = 3
            elif ntit and (s_tit in ntit or ntit in s_tit):
                t = 1.5
            else:
                continue
            a = 0
            if s_art == nart:
                a = 2
            elif nart and (s_art in nart or nart in s_art):
                a = 1
            alb = 0
            if nalb and s_alb == nalb:
                alb = 1
            elif nalb and s_alb and (s_alb in nalb or nalb in s_alb):
                alb = 0.5
            score = t + a + alb
            if score > best_score:
                best, best_score = r, score
        return best, best_score, "ok"

    def _itunes_album_url(self, artist: str, album: str) -> str | None:
        """Apple Music collectionViewUrl for artist+album, or None."""
        art_q = re.sub(r"\s*[&]\s*.+$", "", artist).strip() or artist
        qs = urllib.parse.urlencode({
            "term": f"{art_q} {album}",
            "entity": "album",
            "limit": 5,
            "media": "music",
        })
        try:
            data = get_json(f"https://itunes.apple.com/search?{qs}")
        except Exception:
            return None
        nart, nalb = normalize(artist), normalize(album)
        for r in data.get("results", []):
            s_art = normalize(r.get("artistName", ""))
            s_alb = normalize(r.get("collectionName", ""))
            title_match = (s_alb == nalb) or (s_alb and (s_alb in nalb or nalb in s_alb))
            artist_match = (s_art == nart) or (s_art and (s_art in nart or nart in s_art))
            if title_match and artist_match:
                return r.get("collectionViewUrl")
        return None

    def _songlink_pageurl(self, apple_url: str) -> tuple[str | None, str]:
        """
        Returns (pageUrl_or_None, status) where status is one of:
          "ok"            — request succeeded; pageUrl may still be None if
                            song.link has no entry for this Apple URL.
          "error"         — transient HTTP or parse error.
          "rate-limited"  — got 429; we self-disable for the rest of the run.
          "disabled"      — caller already disabled by an earlier 429.
        """
        if self._songlink_disabled:
            return None, "disabled"
        qs = urllib.parse.urlencode({"url": apple_url})
        req = urllib.request.Request(
            f"https://api.song.link/v1-alpha.1/links?{qs}",
            headers={"User-Agent": DEFAULT_UA, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode("utf-8"))
            return data.get("pageUrl"), "ok"
        except urllib.error.HTTPError as e:
            if e.code == 429:
                self._songlink_disabled = True
                self._log("  songlink: HTTP 429 (rate-limited) — disabling further "
                          "song.link calls for this run; cache untouched so a later "
                          "run can pick up after the limit clears.")
                return None, "rate-limited"
            return None, "error"
        except Exception:
            return None, "error"
