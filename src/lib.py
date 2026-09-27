"""Shared HTTP helper for umd.io / PlanetTerp clients. stdlib only, no deps."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "umd-course-recommender/1.0 (personal use)"
RETRIES = 3
BACKOFF = 0.5  # seconds, doubles each retry


def http_get_json(url, params=None, timeout=15):
    if params:
        qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        url = f"{url}?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    last_err = None
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except (TimeoutError, urllib.error.URLError, ConnectionError) as e:
            last_err = e
            if attempt < RETRIES - 1:
                time.sleep(BACKOFF * (2 ** attempt))
    raise last_err
