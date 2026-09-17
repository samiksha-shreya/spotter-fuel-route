"""Geocoding with a persistent SQLite cache, rate limiting, and a fallback provider.

Primary provider is Nominatim; when it throttles or fails, requests fall back
to Photon (photon.komoot.io), which is also OSM-based. Every lookup - forward
or reverse, whichever provider served it - is cached forever in SQLite, so
repeated requests and restarts never pay the geocoding cost twice. Point
NOMINATIM_URL at a self-hosted instance to remove the ~1 request/second
public-instance rate limit.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Optional

import requests
from django.conf import settings

_MIN_INTERVAL_SECONDS = 1.05  # Nominatim public-instance policy


class GeocodeError(Exception):
    pass




_TOKEN_RE = None


def _query_tokens(text: str) -> set[str]:
    import re as _re
    return {
        t
        for t in _re.split(r"[^a-z0-9]+", text.lower())
        if len(t) >= 3 and t != "usa"
    }


def _levenshtein(a: str, b: str, cap: int = 3) -> int:
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _token_fuzzy_match(tokens: set[str], words: set[str]) -> bool:
    for t in tokens:
        for w in words:
            if not w:
                continue
            if t in w or w in t:
                return True
            tol = 1 if min(len(t), len(w)) <= 4 else 2
            if abs(len(t) - len(w)) <= tol and _levenshtein(t, w) <= tol:
                return True
    return False


def _nominatim_match_ok(result: dict, query_text: str) -> bool:
    """Guard Nominatim's fuzzy matching: a garbage query can fuzzy-match an
    unrelated real place. Accept the match when it is a well-known place
    (high importance) or shares at least one query token with the display
    name (fuzzy token compare, so typos like 'Seattel' still match
    'Seattle'). Single-token queries are accepted as-is."""
    import re as _re

    tokens = _query_tokens(query_text)
    if len(tokens) < 2:
        return True
    try:
        importance = float(result.get("importance") or 0.0)
    except (TypeError, ValueError):
        importance = 0.0
    if importance >= 0.5:
        return True
    words = {
        w
        for w in _re.split(r"[^a-z0-9]+", str(result.get("display_name") or "").lower())
        if w
    }
    return _token_fuzzy_match(tokens, words)


class Geocoder:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_request_at = 0.0
        self._nom_down_until = 0.0  # sticky failover: skip Nominatim while it throttles us
        self._db = sqlite3.connect(settings.GEOCODE_CACHE_PATH, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS geocode_cache (key TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        )
        self._db.commit()

    # -- internals ---------------------------------------------------------

    def _cache_get(self, key: str) -> Optional[dict]:
        row = self._db.execute("SELECT payload FROM geocode_cache WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def _cache_set(self, key: str, payload: dict) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO geocode_cache (key, payload) VALUES (?, ?)",
                (key, json.dumps(payload)),
            )
            self._db.commit()

    def _throttle(self) -> None:
        with self._lock:
            wait = _MIN_INTERVAL_SECONDS - (time.monotonic() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()

    def _get(self, path: str, params: dict) -> list[dict] | dict:
        last_exc: Exception | None = None
        for attempt in range(4):
            self._throttle()
            try:
                resp = requests.get(
                    settings.NOMINATIM_URL + path,
                    params=params,
                    headers={"User-Agent": settings.HTTP_USER_AGENT},
                    timeout=15,
                )
                resp.raise_for_status()
                return resp.json()
            except requests.HTTPError as exc:
                last_exc = exc
                if resp.status_code == 429:  # rate limited: back off and retry
                    time.sleep(5 * (attempt + 1))
                    continue
                raise GeocodeError(f"geocoding request failed: {exc}") from exc
            except requests.RequestException as exc:
                last_exc = exc
                time.sleep(2 * (attempt + 1))  # transient network/timeout
        raise GeocodeError(f"geocoding request failed after retries: {last_exc}")

    def _photon_get(self, path: str, params: dict) -> dict:
        """One Photon request; returns the raw FeatureCollection dict."""
        self._throttle()
        try:
            resp = requests.get(
                settings.PHOTON_URL + path,
                params=params,
                headers={"User-Agent": settings.HTTP_USER_AGENT},
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            raise GeocodeError(f"photon request failed: {exc}") from exc

    @staticmethod
    def _photon_point(payload: dict, query: str = "") -> Optional[tuple[float, float]]:
        """First US feature; Photon does fuzzy matching, so guard it.

        Two failure modes seen in production testing: a foreign query can
        match a foreign place (no country filter on Photon's side), and a
        garbage query can fuzzy-match an unrelated US place. Reject both.
        """
        import re as _re

        features = payload.get("features") or []
        us_features = [
            f
            for f in features
            if str((f.get("properties") or {}).get("countrycode", "")).lower() == "us"
        ]
        if not us_features:
            return None
        tokens = {
            t
            for t in _re.split(r"[^a-z0-9]+", query.lower())
            if len(t) >= 3 and t != "usa"
        }
        if len(tokens) >= 2:
            props = us_features[0].get("properties") or {}
            hay_words = {
                w
                for k in ("name", "city", "town", "village", "county", "state", "street", "district")
                for w in _re.split(r"[^a-z0-9]+", str(props.get(k) or "").lower())
            }
            if not tokens & hay_words:
                return None
        lon, lat = us_features[0]["geometry"]["coordinates"][:2]
        return (float(lat), float(lon))

    def _photon_reverse(self, lat: float, lon: float) -> dict:
        payload = self._photon_get("/reverse", {"lat": lat, "lon": lon})
        features = payload.get("features") or []
        if not features:
            return {}
        raw = features[0].get("properties") or {}
        return {
            "state": raw.get("state", ""),
            "city": raw.get("city") or raw.get("town") or raw.get("village")
                    or raw.get("county") or raw.get("name") or "",
        }

    # -- public API --------------------------------------------------------

    def forward(self, *, city: str = "", state: str = "", street: str = "", query: str = "") -> Optional[tuple[float, float]]:
        """Geocode a US address/place to (lat, lon); None when not found."""
        if query:
            key = f"fwd2|{query.strip().lower()}"  # v2: fallback path hardened (US-only, token match)
            params: dict = {"q": query, "format": "json", "limit": 1, "countrycodes": "us"}
        else:
            key = f"fwd2|{street.lower()}|{city.lower()}|{state.lower()}"
            params = {
                "street": street,
                "city": city,
                "state": state,
                "country": "USA",
                "format": "json",
                "limit": 1,
            }
        cached = self._cache_get(key)
        if cached is not None:
            return tuple(cached["point"]) if cached["point"] else None
        point = None
        photon_q = query or ", ".join(p for p in (street, city, state, "USA") if p)
        if time.monotonic() < self._nom_down_until:
            point = self._photon_point(self._photon_get("/api/", {"q": photon_q, "limit": 5}), photon_q)
        else:
            try:
                results = self._get("/search", params)
                if isinstance(results, list) and results and _nominatim_match_ok(results[0], photon_q):
                    point = (float(results[0]["lat"]), float(results[0]["lon"]))
                else:
                    # Primary found nothing: try the fallback before giving up.
                    point = self._photon_point(self._photon_get("/api/", {"q": photon_q, "limit": 5}), photon_q)
            except GeocodeError:
                self._nom_down_until = time.monotonic() + 300  # retry primary after 5 min
                point = self._photon_point(self._photon_get("/api/", {"q": photon_q, "limit": 5}), photon_q)
        self._cache_set(key, {"point": point})
        return point

    def reverse(self, lat: float, lon: float) -> dict:
        """Reverse geocode to {'state': ..., 'city': ...} (either may be '')."""
        key = f"rev|{lat:.4f}|{lon:.4f}"
        cached = self._cache_get(key)
        if cached is not None:
            return cached["address"]
        address = {}
        if time.monotonic() >= self._nom_down_until:
            try:
                result = self._get("/reverse", {"lat": lat, "lon": lon, "format": "json", "zoom": 10})
                if isinstance(result, dict) and "address" in result:
                    raw = result["address"]
                    address = {
                        "state": raw.get("state", ""),
                        "city": raw.get("city") or raw.get("town") or raw.get("village") or raw.get("county") or "",
                    }
            except GeocodeError:
                self._nom_down_until = time.monotonic() + 300
                address = self._photon_reverse(lat, lon)
        else:
            address = self._photon_reverse(lat, lon)
        self._cache_set(key, {"address": address})
        return address


_geocoder: Optional[Geocoder] = None
_geocoder_lock = threading.Lock()


def get_geocoder() -> Geocoder:
    global _geocoder
    with _geocoder_lock:
        if _geocoder is None:
            _geocoder = Geocoder()
    return _geocoder
