"""
Recap engine — produces a monthly Listen/Here recap as Gutenberg-block HTML,
optionally drafting it to WordPress.

Top-level entry point is `compose.compose_recap(...)`. Sub-modules:

  paths       per-project file locations (defaults to ../listen-here/)
  normalize   string helpers (normalize, htm, ordinal, …)
  db          SQLite connection + OLE date helpers + aux/PlayedAll setup
  blocks      Gutenberg block emitters (paragraph, heading, image, columns, …)
  bandcamp    fan-collection index + fuzzy album/track lookup
  links       link cache + iTunes Search + song.link fallback
  art         Thumbs index + WP media upload + best-art resolution
  lastfm      scrobble fetch + dedup + merge into aux.LastfmExtras
  queries     SQL aggregations (by-the-numbers, deep dives, top-N, vault, …)
  wp          REST helper + tags + draft posting
  compose     orchestrator that wires it all together
"""
