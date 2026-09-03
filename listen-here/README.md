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
   - `lastfm_user`, `lastfm_api_key` — optional. When present, scrobbles are
     pulled and merged in to cover mobile/portable plays MediaMonkey never sees.
2. Shut MediaMonkey down (so nothing is mid-write), then copy your library DB
   into this directory. **Check where your DB actually is first** — the
   `%APPDATA%` path is only the default, and a leftover stub often sits there
   even when the real library lives elsewhere. The authoritative answer is the
   `DBName=` line in `%APPDATA%\MediaMonkey5\MediaMonkey.ini`:
   ```
   grep -i DBName "%APPDATA%\MediaMonkey5\MediaMonkey.ini"
   cp "<the path DBName points at>" listen-here/MM5.DB
   ```
   Sanity-check the copy: it should be a few hundred MB, and
   `select max(PlayDate) from Played` should land near your last listen. The
   tools read `listen-here/MM5.DB` and open it read-only — your live library is
   never touched.

## Usage

The current entry point is the `workshop.recap_cli` module, run from the repo
root. The original single-file `recap_compose.py` is still here and still works,
but it lacks the custom-range flags; new work goes to the module.

```bash
# Dry run — previous calendar month, local HTML preview, no posting
python -m workshop.recap_cli

# Explicit month
python -m workshop.recap_cli --year 2026 --month 2

# Arbitrary date range (inclusive), instead of a month
python -m workshop.recap_cli --start 2026-01-15 --end 2026-02-20

# Skip the Last.fm backfill even if credentials are present
python -m workshop.recap_cli --skip-lastfm

# Choose deep dives non-interactively: ranks, names, "auto", or "none"
python -m workshop.recap_cli --deep-dives "1,3"

# Generate and post a draft to WordPress
python -m workshop.recap_cli --post
```

Without `--deep-dives`, the CLI prints the candidate pool and prompts for a pick.

A draft is left in WordPress; nothing publishes automatically. You review and tweak in `wp-admin/edit.php?post_status=draft` before clicking Publish yourself.

## Caches

The script creates several JSON caches next to itself so repeat runs are fast and don't hammer external APIs:

- `bandcamp_collection.json` — your full Bandcamp collection (refresh by deleting)
- `link_cache.json` — per-track resolved URLs (Bandcamp / song.link / none)
- `art_cache.json` — local album-art path → WP media URL after upload
- `itunes_art_cache.json` — iTunes Search album-art hot-link URLs
- `tag_cache.json` — WP tag name → ID
- `top_artists_history.json` — your top-10 artists per month, used for the frequency badges
- `lastfm_scrobbles.json` — local mirror of your all-time Last.fm scrobbles

All are gitignored **except** `lastfm_scrobbles.json`, which was committed by
accident in `4af0525` and is still tracked.

## Status

Works end to end — monthly recaps have been generated and drafted to WordPress.
Rough edges:

- The WP category ID is hardcoded (`CULTURE_CATEGORY_ID = 90` in
  `workshop/recap/compose.py`). The recap window is not; it takes CLI flags.
- Bandcamp lookup uses an undocumented endpoint (`/api/fancollection/1/collection_items`). Stable in practice but unsupported by Bandcamp.
- Mobile / portable plays are missing from MediaMonkey's `Played` table. The
  Last.fm backfill covers this: scrobbles are matched to library songs and
  deduped against desktop plays with a per-track variable window, then merged
  through an in-memory view. The MM5 file itself is never written to.
- Test coverage is limited to the UTC/local boundary (see below); everything
  else is untested.

## Tests

Stdlib `unittest`, no dependencies:

```bash
python -m unittest discover -s workshop/tests -t .
```

`workshop/tests/test_timezone.py` pins the UTC/local invariants described
below. The assertions derive their expectations from the running machine's own
timezone rules, so they hold anywhere; the DST case skips itself in zones that
do not observe it. Checked against the pre-fix code by stubbing the conversions
back to identity — 6 of the 11 fail there, including the MM5-vs-Last.fm
agreement check in all three months tested.

## A note on time zones

MediaMonkey stores `Played.PlayDate` as an OLE date in **UTC**, while a recap
window means a *local* calendar month. `workshop/recap/db.py` keeps the two
straight:

- `local_dt_to_ole()` converts a window boundary local → UTC before it is
  compared against `PlayDate`.
- `ole_to_local_dt()` converts a `PlayDate` back to local wall-clock before it
  is displayed or bucketed.
- SQL that groups by day/week/month passes SQLite's `'localtime'` modifier.
- `dt_to_ole` / `ole_to_dt` remain the raw, timezone-blind primitives, used only
  where both sides are already UTC — the Last.fm scrobble path.

Both conversions go through `astimezone()`, so DST is resolved per instant
(the offset is −6h in January and −5h in August, not a fixed constant).

This was previously wrong in two ways: month windows were compared against UTC
`PlayDate` values as if they were local, and the Last.fm half of the merged
stream filtered on local time while the MediaMonkey half filtered on UTC — so
the two sources used boundaries hours apart. Correcting it moved 5 plays into
August 2026 (1,651 → 1,656) and shifts some late-evening plays to the day they
actually happened.

## Why "listen-here"

It's the name of a thread of posts on [mankinlevine.com](https://mankinlevine.com) where I write short notes about specific listening. This generator is the monthly-aggregate companion to that.
