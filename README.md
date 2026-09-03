# mediamonkey-wrench

Tools I've built for myself around [MediaMonkey 5](https://www.mediamonkey.com/) and the listening data it accumulates. Shared in case anyone else finds them useful.

## Tools

### [listen-here/](listen-here/) (public)

Generates monthly listening recap posts to a self-hosted WordPress blog, sourced from MediaMonkey 5 play history. Posts include album art, Bandcamp linking where I own the record, and [song.link](https://song.link/) for everything else. Used at [mankinlevine.com](https://mankinlevine.com).

This directory also holds the local data the tools read: your `MM5.DB` copy and
the JSON caches. The original single-file generator (`recap_compose.py`) still
lives here and still runs.

### [workshop/](workshop/)

The current engine, and where new work goes. `workshop/recap/` is the recap
generator split into modules; `workshop/recap_cli.py` is the CLI that replaced
`listen-here/recap_compose.py`:

```bash
python -m workshop.recap_cli --year 2026 --month 2
```

`workshop/server.py` is a local FastAPI app over the same engine — a browser
flow for composing and drafting a recap, plus a per-artist page with play
history, top tracks/albums, and inline SVG charts:

```bash
python -m workshop.server   # http://127.0.0.1:8765/
```

It binds to localhost, has no authentication, and reads your data files freely.
Don't expose it. Needs the packages in `workshop/requirements.txt`; the recap
engine itself is stdlib-only.

## Future ideas

- **now-playing share** — desktop image generator for sharing the currently playing track to Instagram Stories and the like
- **library diagnostics** — duplicate detection, missing-metadata reports, listening-pattern analytics
- ...

## License

MIT — see [LICENSE](LICENSE).
