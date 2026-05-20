"""
Thin CLI wrapper over workshop.recap.compose. Drop-in replacement for the
old listen-here/recap_compose.py — same flags, same UX, but the engine is
now the workshop.recap package.

Invocation (from repo root):
    python -m workshop.recap_cli --year 2026 --month 2
    python -m workshop.recap_cli --year 2026 --month 2 --post
    python -m workshop.recap_cli --year 2026 --month 2 --deep-dives "Donny Hathaway"
"""
from __future__ import annotations

import argparse
import datetime
import sys

from .recap.compose import RecapWindow, compose_recap
from .recap.normalize import normalize


def _default_window() -> tuple[int, int]:
    today = datetime.date.today()
    if today.month == 1:
        return today.year - 1, 12
    return today.year, today.month - 1


def _build_picker(arg_value: str | None):
    """
    Build a deep_dive_picker callable from the --deep-dives CLI arg.

    - None         → interactive prompt (numbers / names / empty=auto / 'none'=skip)
    - "auto"/""    → top 3 by ratio
    - "none"       → skip the section
    - "1,3"        → ranks
    - "Donny H..."  → substring-match candidate names
    """

    def _parse_explicit(arg: str, pool: list[dict]) -> list[dict]:
        out, seen = [], set()
        for tok in arg.split(","):
            tok = tok.strip()
            if not tok:
                continue
            if tok.isdigit():
                i = int(tok) - 1
                if 0 <= i < len(pool) and i not in seen:
                    out.append(pool[i]); seen.add(i)
                continue
            tok_n = normalize(tok)
            for i, d in enumerate(pool):
                if i in seen:
                    continue
                art_n = normalize(d["art"])
                if tok_n and (tok_n in art_n or art_n in tok_n):
                    out.append(d); seen.add(i)
                    break
        return out

    def picker(pool: list[dict]) -> list[dict]:
        if not pool:
            return []
        print("\nDeep Dive candidates:")
        for i, dd in enumerate(pool[:10], 1):
            ratio_str = (f"{dd['ratio']:.1f}× typical"
                         if dd["monthly_avg"] >= 1 else "well above usual")
            print(f"  {i:>2}. {dd['art']:<45} {dd['plays']:>3} plays / "
                  f"{dd['tracks']:>2} tr · {ratio_str}")

        raw = arg_value
        if raw is None:
            try:
                raw = input("Pick deep dives (numbers like '1,3', artist names, "
                            "empty=top 3, 'none' to skip): ").strip()
            except EOFError:
                raw = ""
        raw = (raw or "").strip()

        if raw.lower() == "none":
            picks = []
        elif raw == "" or raw.lower() == "auto":
            picks = pool[:3]
        else:
            picks = _parse_explicit(raw, pool)
            if not picks:
                print(f"  ! couldn't match '{raw}' to any candidate; falling back to top 3")
                picks = pool[:3]
        print(f"  → featuring: "
              f"{', '.join(d['art'] for d in picks) if picks else '(none)'}")
        return picks

    return picker


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="Generate a Listen/Here recap.")
    ap.add_argument("--year",  type=int, help="Window year (with --month). Default: previous calendar month.")
    ap.add_argument("--month", type=int, help="Window month 1-12 (with --year)")
    ap.add_argument("--start", help="Custom range start date (YYYY-MM-DD, inclusive)")
    ap.add_argument("--end",   help="Custom range end date (YYYY-MM-DD, inclusive)")
    ap.add_argument("--post", action="store_true",
                    help="POST a draft to WordPress (otherwise local preview only)")
    ap.add_argument("--skip-lastfm", action="store_true",
                    help="Skip Last.fm scrobble backfill even if creds present")
    ap.add_argument("--deep-dives", dest="deep_dives",
                    help="Deep-dive picks: numbers ('1,3'), artist-name substrings, "
                         "'auto' (top-3-by-ratio), or 'none' (skip section). "
                         "Default: interactive prompt.")
    args = ap.parse_args(argv)

    month_args_given = (args.year is not None) or (args.month is not None)
    range_args_given = (args.start is not None) or (args.end is not None)
    if month_args_given and range_args_given:
        ap.error("use either --year/--month or --start/--end, not both")

    if range_args_given:
        if not (args.start and args.end):
            ap.error("--start and --end must be provided together")
        try:
            start_d = datetime.date.fromisoformat(args.start)
            end_d   = datetime.date.fromisoformat(args.end)
        except ValueError as e:
            ap.error(f"bad date: {e}")
        if end_d < start_d:
            ap.error("--end must be on or after --start")
        window = RecapWindow.from_dates(start_d, end_d)
    else:
        if (args.year is None) ^ (args.month is None):
            ap.error("--year and --month must be provided together")
        if args.year is None:
            args.year, args.month = _default_window()
        if not (1 <= args.month <= 12):
            ap.error("--month must be 1-12")
        window = RecapWindow.from_month(args.year, args.month)

    picker = _build_picker(args.deep_dives)

    result = compose_recap(
        window=window,
        deep_dive_picker=picker,
        skip_lastfm=args.skip_lastfm,
        posting=args.post,
    )

    if not args.post:
        print("\n(no --post flag passed; skipping WP POST. Re-run with --post to draft to WP.)")
    return 0 if result is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
