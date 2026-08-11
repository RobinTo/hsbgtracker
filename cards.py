"""Card-ID -> display-name / races lookup.

Primary source is HearthstoneJSON (downloaded once, slimmed and cached next
to this file). Names observed in Power.log itself are merged in as a
fallback, so the tracker still shows most names with no network at all.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.request
from pathlib import Path

CACHE_FILE = Path(__file__).with_name("cards_cache.json")
HSJSON_URL = "https://api.hearthstonejson.com/v1/latest/enUS/cards.json"
CACHE_VERSION = 2

RACE_LABELS = {
    "BEAST": "Beast",
    "DEMON": "Demon",
    "DRAGON": "Dragon",
    "ELEMENTAL": "Elemental",
    "MECHANICAL": "Mech",
    "MURLOC": "Murloc",
    "NAGA": "Naga",
    "PIRATE": "Pirate",
    "QUILBOAR": "Quilboar",
    "UNDEAD": "Undead",
    "ALL": "Amalgam",
}


class CardDb:
    def __init__(self):
        self._cards: dict[str, list] = {}  # id -> [name, [races]]
        self._learned: dict[str, str] = {}
        self._lock = threading.Lock()
        self._load_cache()

    # ------------------------------------------------------------------ load

    def _load_cache(self):
        if not CACHE_FILE.exists():
            return
        try:
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("v") == CACHE_VERSION:
                with self._lock:
                    self._cards = data["cards"]
        except (OSError, json.JSONDecodeError, KeyError):
            pass

    def ensure_downloaded(self, on_done=None):
        """Fetch HearthstoneJSON in a background thread if not cached yet."""
        if self._cards:
            if on_done:
                on_done(True)
            return

        def work():
            ok = False
            try:
                req = urllib.request.Request(
                    HSJSON_URL, headers={"User-Agent": "hstracker/1.0 (+local BG tracker)"}
                )
                with urllib.request.urlopen(req, timeout=60) as resp:
                    cards = json.load(resp)
                slim = {}
                for c in cards:
                    if "name" not in c:
                        continue
                    races = c.get("races") or ([c["race"]] if "race" in c else [])
                    slim[c["id"]] = [c["name"], races]
                with self._lock:
                    self._cards = slim
                CACHE_FILE.write_text(
                    json.dumps({"v": CACHE_VERSION, "cards": slim}, ensure_ascii=False),
                    encoding="utf-8",
                )
                ok = True
            except Exception:
                pass  # offline is fine — log-learned names still work
            if on_done:
                on_done(ok)

        threading.Thread(target=work, daemon=True).start()

    # ----------------------------------------------------------------- learn

    def learn(self, card_id: str, name: str):
        if card_id and name:
            self._learned[card_id] = name

    def learn_all(self, mapping: dict):
        self._learned.update(mapping)

    # ---------------------------------------------------------------- lookup

    def _entry(self, card_id: str):
        with self._lock:
            for cid in (card_id, card_id.removesuffix("_G")):
                if cid in self._cards:
                    return self._cards[cid]
        return None

    def name(self, card_id: str, fallback: str = "") -> str:
        if fallback:
            return fallback
        entry = self._entry(card_id)
        if entry:
            return entry[0]
        for cid in (card_id, card_id.removesuffix("_G")):
            if cid in self._learned:
                return self._learned[cid]
        return self._prettify(card_id)

    def races(self, card_id: str) -> list:
        entry = self._entry(card_id)
        return entry[1] if entry else []

    def tribe_label(self, minions) -> str:
        """Dominant tribe of a board, if one covers at least half of it."""
        if not minions:
            return ""
        counts: dict[str, int] = {}
        for m in minions:
            for race in self.races(m.card_id):
                if race == "ALL":
                    continue  # amalgams count toward nothing specific
                counts[race] = counts.get(race, 0) + 1
        if not counts:
            return ""
        race, n = max(counts.items(), key=lambda kv: kv[1])
        if n * 2 >= len(minions):
            return RACE_LABELS.get(race, race.title())
        return "Mixed"

    @staticmethod
    def _prettify(card_id: str) -> str:
        # "BG23_000" -> "BG23 000" — last-resort display only.
        return re.sub(r"[_]+", " ", card_id) or "?"
