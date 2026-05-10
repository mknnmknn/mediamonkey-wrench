# scripts/

Small diagnostics that aren't part of the main recap pipeline but are handy when something feels off.

## `discover.py`

Reads a MediaMonkey 5 `MM5.DB` file (read-only) and prints all tables, row counts, key-table schemas, and a sample of the `Played` table with OLE-date decoding. Useful when investigating MM's schema, adapting the recap generator to a different MM build, or just exploring what's in your library.

```bash
# Defaults to ../MM5.DB next to the recap script
python scripts/discover.py

# Or point at an arbitrary DB
python scripts/discover.py "C:/path/to/MM5.DB"
```

## `wp_test.py`

Posts a tiny "connection test" draft to your WordPress site to confirm REST API + Application Password are working. The draft lands as `status: draft` — review and delete in `wp-admin` once verified.

```bash
# Defaults to ../secrets.json
python scripts/wp_test.py

# Or point at a different secrets file
python scripts/wp_test.py path/to/secrets.json
```

If this works but the main recap doesn't, the problem is likely in tag resolution, post payload, or media uploads — not auth.
