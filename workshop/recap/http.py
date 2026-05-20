"""
Tiny shared HTTP helper. Centralizes the User-Agent header and JSON-decoding
behavior so individual callers don't reinvent it.
"""
from __future__ import annotations

import json
import urllib.request


DEFAULT_UA = "MM-Recap-Addon-Dev/0.6"


def get_json(url: str, timeout: int = 20, ua: str = DEFAULT_UA) -> dict:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": ua, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))
