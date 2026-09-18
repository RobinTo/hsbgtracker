"""Card-ID -> name / races / text / stats lookup.

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
CACHE_VERSION = 6

TAG_RE = re.compile(r"</?[bi]>|\[x\]")

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
        # id -> [name, races, text, techLevel, attack, health]
        self._cards: dict[str, list] = {}
        self._dbf: dict[str, str] = {}  # str(dbfId) -> card id
        self._bg: dict = {}  # current BG pool: minions/spells/trinkets/heroes
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
                    self._dbf = data.get("dbf", {})
                    self._bg = data.get("bg", {})
        except (OSError, json.JSONDecodeError, KeyError):
            pass

    def ensure_downloaded(self, on_done=None, force=False):
        """Fetch HearthstoneJSON in a background thread if not cached yet.
        force=True re-downloads (used on a new game patch); the old data
        keeps serving until the fresh set swaps in."""
        if self._cards and not force:
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
                dbf = {}
                bg = {"minions": [], "spells": [], "trinkets": [], "heroes": [],
                      "buddies": []}
                by_dbf_full = {c["dbfId"]: c for c in cards if "dbfId" in c}
                # buddy dbfId -> the hero it belongs to
                hero_of_buddy = {
                    c["battlegroundsBuddyDbfId"]: c for c in cards
                    if c.get("battlegroundsHero") and c.get("battlegroundsBuddyDbfId")
                }
                for c in cards:
                    if "name" not in c:
                        continue
                    races = c.get("races") or ([c["race"]] if "race" in c else [])
                    text = TAG_RE.sub("", c.get("text", "")).replace("\n", " ").strip()
                    slim[c["id"]] = [
                        c["name"], races, text, c.get("techLevel", 0),
                        c.get("attack", 0), c.get("health", 0),
                    ]
                    if "dbfId" in c:
                        dbf[str(c["dbfId"])] = c["id"]
                    # The current Battlegrounds pool, for the card browser.
                    mech = (c.get("mechanics") or []) + (c.get("referencedTags") or [])
                    duos = bool(c.get("isBattlegroundsDuosExclusive"))
                    if c.get("isBattlegroundsPoolMinion"):
                        bg["minions"].append({
                            "id": c["id"], "n": c["name"], "t": c.get("techLevel", 0),
                            "r": races, "x": text, "a": c.get("attack", 0),
                            "h": c.get("health", 0), "m": mech, "d": duos,
                        })
                    elif c.get("isBattlegroundsBuddy"):
                        if c.get("battlegroundsNormalDbfId"):
                            continue  # golden copy — the base card covers it
                        hero = hero_of_buddy.get(c.get("dbfId"), {})
                        bg["buddies"].append({
                            "id": c["id"], "n": c["name"], "t": c.get("techLevel", 0),
                            "r": races, "x": text, "a": c.get("attack", 0),
                            "h": c.get("health", 0), "m": mech,
                            "hn": hero.get("name", ""), "hid": hero.get("id", ""),
                        })
                    elif c.get("isBattlegroundsPoolSpell"):
                        bg["spells"].append({
                            "id": c["id"], "n": c["name"], "t": c.get("techLevel", 0),
                            "c": c.get("cost", 0), "x": text, "m": mech, "d": duos,
                        })
                    elif c.get("type") == "BATTLEGROUND_TRINKET":
                        bg["trinkets"].append({
                            "id": c["id"], "n": c["name"], "c": c.get("cost", 0),
                            "r": c.get("battlegroundsAssociatedRaces") or [],
                            "x": text, "m": mech, "d": duos,
                        })
                    elif c.get("battlegroundsHero"):
                        power = by_dbf_full.get(c.get("heroPowerDbfId"), {})
                        buddy = by_dbf_full.get(c.get("battlegroundsBuddyDbfId"), {})
                        bg["heroes"].append({
                            "id": c["id"], "n": c["name"],
                            "ar": c.get("armor", 0),
                            "pn": power.get("name", ""),
                            "px": TAG_RE.sub("", power.get("text", ""))
                                  .replace("\n", " ").strip(),
                            "bn": buddy.get("name", ""),
                            "bt": buddy.get("techLevel", 0),
                        })
                with self._lock:
                    self._cards = slim
                    self._dbf = dbf
                    self._bg = bg
                CACHE_FILE.write_text(
                    json.dumps(
                        {"v": CACHE_VERSION, "cards": slim, "dbf": dbf, "bg": bg},
                        ensure_ascii=False,
                    ),
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

    def known(self, card_id: str) -> bool:
        return self._entry(card_id) is not None

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

    def text(self, card_id: str) -> str:
        entry = self._entry(card_id)
        return entry[2] if entry and len(entry) > 2 else ""

    def tech_level(self, card_id: str) -> int:
        entry = self._entry(card_id)
        return entry[3] if entry and len(entry) > 3 else 0

    def base_stats(self, card_id: str) -> tuple[int, int] | None:
        """Printed attack/health. Golden ids resolve to their own (doubled)
        entry when present; otherwise the base entry is doubled here."""
        with self._lock:
            entry = self._cards.get(card_id)
            if entry is None and card_id.endswith("_G"):
                base = self._cards.get(card_id.removesuffix("_G"))
                if base and len(base) > 5:
                    return base[4] * 2, base[5] * 2
                return None
        if entry and len(entry) > 5:
            return entry[4], entry[5]
        return None

    def card_by_dbf(self, dbf_id: int) -> str:
        with self._lock:
            return self._dbf.get(str(dbf_id), "")

    def bg_pool(self) -> dict:
        """Current Battlegrounds pool (minions/spells/trinkets/heroes/buddies),
        empty until the card DB has downloaded at least once."""
        with self._lock:
            return self._bg

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
