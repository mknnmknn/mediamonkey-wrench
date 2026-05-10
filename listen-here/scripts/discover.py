"""
Diagnostic: inspect a MediaMonkey 5 library database (read-only).

Prints all tables with row counts, schemas for the key tables (Songs, Played,
Albums, Artists, Covers, etc.), and confirms the OLE-date format that
MediaMonkey uses in datetime columns.

Useful when something feels off in recap output, when adapting the recap
generator to a different MM build, or just as a quick "what's in there?".

Usage:
    python scripts/discover.py [path/to/MM5.DB]

If no path is given, looks for MM5.DB in the parent directory (handy when
this script runs from listen-here/scripts/ and the working DB sits at
listen-here/MM5.DB).
"""
import sqlite3, sys, datetime, os
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "MM5.DB")
DB = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)

if not os.path.exists(DB):
    print(f"ERROR: database not found at {DB}", file=sys.stderr)
    print("Pass a path as the first argument, or place MM5.DB in the parent directory.", file=sys.stderr)
    sys.exit(1)

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
# MM stamps a custom IUNICODE collation onto TEXT columns. Register a Python
# stub so we can scan those tables without sqlite errors.
con.create_collation(
    "IUNICODE",
    lambda a, b: (a.casefold() > b.casefold()) - (a.casefold() < b.casefold()),
)
cur = con.cursor()

def q(sql, *args):
    return cur.execute(sql, args).fetchall()

print("=" * 70)
print(f"DB: {DB}  ({os.path.getsize(DB)/1024/1024:.1f} MB)")
print("=" * 70)

print("\n--- ALL TABLES ---")
tables = [r[0] for r in q("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
for t in tables:
    try:
        cnt = cur.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
    except Exception as e:
        cnt = f"err: {e}"
    print(f"  {t:40s} {cnt}")

print("\n--- VIEWS ---")
for r in q("SELECT name FROM sqlite_master WHERE type='view' ORDER BY name"):
    print(f"  {r[0]}")

key_tables = ["Songs", "Played", "Albums", "Artists", "Covers",
              "ArtistsSongs", "ArtistsAlbums", "GenresSongs",
              "Playlists", "PlaylistSongs", "DBInfo"]
print("\n--- KEY TABLE SCHEMAS ---")
for t in [x for x in key_tables if x in tables]:
    print(f"\n[{t}]")
    for cid, name, ctype, notnull, dflt, pk in q(f'PRAGMA table_info("{t}")'):
        flags = []
        if pk: flags.append("PK")
        if notnull: flags.append("NN")
        flag_str = f"  ({', '.join(flags)})" if flags else ""
        print(f"  {name:30s} {ctype:20s}{flag_str}")

print("\n--- PLAYED: SAMPLE + OLE-DATE DECODE ---")
if "Played" in tables:
    rows = q("SELECT * FROM Played ORDER BY ROWID DESC LIMIT 3")
    for r in rows:
        print(f"  {r}")
    print("\n  Date-column candidates (OLE -> ISO):")
    for col in [r[1] for r in q("PRAGMA table_info(Played)")]:
        try:
            sample = cur.execute(
                f"SELECT {col} FROM Played WHERE {col} IS NOT NULL ORDER BY ROWID DESC LIMIT 1"
            ).fetchone()
            if sample and isinstance(sample[0], (int, float)) and 30000 < sample[0] < 60000:
                ole = sample[0]
                dt = datetime.datetime(1899, 12, 30) + datetime.timedelta(days=ole)
                print(f"    {col} = {ole}  ->  {dt.isoformat()}")
        except Exception:
            pass

print("\n--- DBInfo ---")
if "DBInfo" in tables:
    try:
        for r in q("SELECT * FROM DBInfo LIMIT 20"):
            print(f"  {r}")
    except Exception as e:
        print(f"  err: {e}")
