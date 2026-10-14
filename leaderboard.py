"""Opponent ratings from Blizzard's public Battlegrounds leaderboards.

The game never logs other players' ratings, so — like Firestone — we look
battletags up on the official leaderboard (top ~5000 solo / ~1700 duos per
region, cutoff 8000). Names carry no #discriminator, so duplicates are
possible; the best-ranked match wins.

Boards are fetched on a background thread, cached to disk and refreshed
hourly; lookups never block.
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import threading
import time
import urllib.request
from pathlib import Path

URL = ("https://hearthstone.blizzard.com/en-us/api/community/leaderboardsData"
       "?region={region}&leaderboardId={board}&page={page}")
CACHE_DIR = Path(__file__).with_name("leaderboard_cache")
MAX_AGE = 3600
RETRY_AFTER = 300  # after a failed fetch
MIN_RATING = 8000  # the boards' cutoff; unlisted players are below it

# Battle.net region byte (GameAccountId hi >> 32) -> leaderboard region
REGIONS = {1: "US", 2: "EU", 3: "AP", 4: "AP", 5: "AP"}


def _get(region: str, board: str, page: int) -> dict:
    url = URL.format(region=region, board=board, page=page)
    req = urllib.request.Request(url, headers={"User-Agent": "bg-tracker"})
    with urllib.request.urlopen(req, timeout=20) as fh:
        return json.load(fh)


def fetch_board(region: str, board: str) -> list[dict]:
    first = _get(region, board, 1)["leaderboard"]
    rows = list(first["rows"])
    pages = range(2, first["pagination"]["totalPages"] + 1)
    with cf.ThreadPoolExecutor(8) as ex:
        for data in ex.map(lambda p: _get(region, board, p), pages):
            rows += data["leaderboard"]["rows"]
    return rows


class Leaderboards:
    def __init__(self, on_update=None):
        self.on_update = on_update  # called (from a worker thread) on new data
        self._boards: dict[tuple, dict] = {}  # key -> {"at": ts, "names": {...}}
        self._busy: set[tuple] = set()
        self._failed: dict[tuple, float] = {}
        self._lock = threading.Lock()

    def lookup(self, region_byte: int, duos: bool, name: str):
        """(rank, rating) or None if unlisted; "?" while the board loads."""
        region = REGIONS.get(region_byte)
        if not region or not name:
            return None
        key = (region, "battlegroundsduo" if duos else "battlegrounds")
        with self._lock:
            board = self._boards.get(key)
        if board is None:
            board = self._load_cache(key)
        if board is None or time.time() - board["at"] > MAX_AGE:
            self._refresh(key)
        if board is None:
            return "?"
        return board["names"].get(name.split("#")[0])

    def _cache_path(self, key) -> Path:
        return CACHE_DIR / f"{key[1]}_{key[0]}.json"

    def _load_cache(self, key):
        try:
            data = json.loads(self._cache_path(key).read_text(encoding="utf-8"))
            board = self._index(data["at"], data["rows"])
        except (OSError, ValueError, KeyError):
            return None
        with self._lock:
            self._boards.setdefault(key, board)
        return board

    @staticmethod
    def _index(at: float, rows: list[dict]) -> dict:
        names: dict[str, tuple[int, int]] = {}
        for r in rows:  # rows are rank-ordered: keep the best-ranked duplicate
            names.setdefault(r["accountid"], (r["rank"], r["rating"]))
        return {"at": at, "names": names}

    def _refresh(self, key):
        with self._lock:
            if key in self._busy or time.time() - self._failed.get(key, 0) < RETRY_AFTER:
                return
            self._busy.add(key)
        threading.Thread(target=self._fetch, args=(key,), daemon=True).start()

    def _fetch(self, key):
        try:
            rows = fetch_board(*key)
            now = time.time()
            CACHE_DIR.mkdir(exist_ok=True)
            self._cache_path(key).write_text(
                json.dumps({"at": now, "rows": rows}, ensure_ascii=False),
                encoding="utf-8")
            board = self._index(now, rows)
            with self._lock:
                self._boards[key] = board
                self._failed.pop(key, None)
        except Exception:  # network down, API changed: retry later
            with self._lock:
                self._failed[key] = time.time()
            return
        finally:
            with self._lock:
                self._busy.discard(key)
        if self.on_update:
            self.on_update()


def fmt_rating(hit) -> str:
    if hit == "?":
        return ""
    if hit is None:
        return f"<{MIN_RATING // 1000}k"
    return f"{hit[1]:,}"
