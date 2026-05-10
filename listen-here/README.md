# listen-here

Generates monthly listening recap posts for a self-hosted WordPress blog, sourced from your MediaMonkey 5 play history.

Each post includes:

- **By the numbers** — plays, unique tracks, artists, listening time, busiest day, comparison to your rolling 12-month average
- **Deep Dives** — artists whose presence in the rotation jumped well above their usual baseline (with per-artist album-thumbnail strip and a slot for personal commentary)
- **Top artists** — ranked list, with a "Nth time in top 10" / "N months running" badge once enough history accumulates
- **Top albums** — 3/4/3 visual grid with album art
- **Top tracks** — tiered (the top tier as items, lower tiers collapsed into "+ N more tracks at K plays")
- **First encounters** — artists whose first-ever play in the library happened this month
- **Anywhere, Anytime** — 5★ tracks surfacing this month, ranked by longest prior absence, grouped by month-and-year of last play
- **From the Vault** — tracks returning after >1 year, grouped by month-and-year of last play
- **Notes** — placeholder for your closing prose

Track links resolve to **Bandcamp** when the album is in your collection, then to a **[song.link](https://song.link/)** universal page (via iTunes Search). Album mentions inline are linked to Bandcamp where possible. Missing album art falls back to iTunes' CDN, then to an artist-initials placeholder.

## Requirements

- Python 3.10+ (stdlib only, no external dependencies)
- MediaMonkey 5 with play history in the `Played` table
- Self-hosted WordPress with REST API enabled and Application Passwords
- Optional: a Bandcamp account whose collection is public

## Setup

1. Copy `secrets.example.json` to `secrets.json` and fill in:
   - `wp_url`, `wp_user`, `wp_app_password` — generate the app password in WP admin under **Users → Profile → Application Passwords**
   - `bandcamp_user` — your Bandcamp username, used to scrape your owned-album collection
   - `lastfm_user`, `lastfm_api_key` — optional, reserved for future mobile-play backfill
2. Shut MediaMonkey down (so its SQLite WAL is flushed), then copy your library DB next to the script:
   ```
   cp "%APPDATA%\MediaMonkey5\MM5.DB" .
   ```
   If you've moved your DB elsewhere, look at the `DBName=` line in `MediaMonkey.ini`. The script reads `MM5.DB` from its own directory.

## Usage

```bash
# Dry run — generates a local HTML preview, no posting
python recap_compose.py

# Generate and post a draft to WordPress
python recap_compose.py --post
```

A draft is left in WordPress; nothing publishes automatically. You review and tweak in `wp-admin/edit.php?post_status=draft` before clicking Publish yourself.

## Caches

The script creates several JSON caches next to itself so repeat runs are fast and don't hammer external APIs:

- `bandcamp_collection.json` — your full Bandcamp collection (refresh by deleting)
- `link_cache.json` — per-track resolved URLs (Bandcamp / song.link / none)
- `art_cache.json` — local album-art path → WP media URL after upload
- `itunes_art_cache.json` — iTunes Search album-art hot-link URLs
- `tag_cache.json` — WP tag name → ID
- `top_artists_history.json` — your top-10 artists per month, used for the frequency badges

All are listed in the repo `.gitignore`.

## Status

v0.1. Works, but rough edges:

- The recap window (year, month) and category ID are hardcoded near the top of the script. Will move to CLI flags in the next pass.
- Bandcamp lookup uses an undocumented endpoint (`/api/fancollection/1/collection_items`). Stable in practice but unsupported by Bandcamp.
- Mobile / portable plays are not captured in MediaMonkey's `Played` table — only desktop plays. A Last.fm-scrobble backfill is planned.

## Why "listen-here"

It's the name of a thread of posts on [mankinlevine.com](https://mankinlevine.com) where I write short notes about specific listening. This generator is the monthly-aggregate companion to that.
