"""
String-level helpers used everywhere — normalization for fuzzy matching,
HTML-escaping for inline text, and a couple of name/number formatters.
"""
from __future__ import annotations

import html as _html_mod
import re
import unicodedata


def normalize(s: str) -> str:
    """
    Lossy lowercase ASCII-ish form for fuzzy matching. Strips diacritics,
    truncates "feat./featuring/ft./with X" tails (Daniel relocates feat-credits
    to the artist field, so titles never need them), and collapses non-word
    punctuation to spaces.
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"\b(feat\.?|featuring|ft\.?|with)\b.*", "", s)
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def htm(s: str | None) -> str:
    """HTML-escape inline text. Doesn't quote attribute values — use only for body content."""
    return _html_mod.escape(s or "", quote=False)


def reduce_artist(name: str) -> str:
    """
    Strip secondary collaborators for fuzzy artist matching:
    'X & Y' -> 'X', 'X feat. Y' -> 'X', 'X, Y' -> 'X'.
    """
    if not name:
        return ""
    s = re.split(r"\s+(?:feat\.?|featuring|ft\.?|with|vs\.?)\s+",
                 name, maxsplit=1, flags=re.IGNORECASE)[0]
    s = re.split(r"\s*[&]\s*",    s, maxsplit=1)[0]
    s = re.split(r"\s*,\s*",      s, maxsplit=1)[0]
    return s.strip()


def predict_wp_slug(name: str) -> str:
    """Approximate WP's sanitize_title() so we can predict tag URLs."""
    s = (name or "").lower().strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


def ordinal(n: int) -> str:
    """1 -> '1st', 2 -> '2nd', 11 -> '11th', 22 -> '22nd', ..."""
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    return f"{n}{['th','st','nd','rd','th','th','th','th','th','th'][n % 10]}"
