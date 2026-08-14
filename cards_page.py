"""Generate cards.html — a browsable view of the current Battlegrounds pool.

Run:  python cards_page.py        (builds and opens in the browser)

Data comes from the tracker's card cache (cards_cache.json), which refreshes
on every game patch, so the pool tracks the live game. Presentation lives in
cards_template.html. Career lines on the heroes tab come from
games_history.jsonl.
"""

from __future__ import annotations

import json
import time
import webbrowser
from pathlib import Path

from cards import CardDb
from stats import load_games

TEMPLATE = Path(__file__).with_name("cards_template.html")
OUTPUT = Path(__file__).with_name("cards.html")


def _hero_career() -> dict[str, list[int]]:
    """base hero card id -> list of placements from recorded games."""
    out: dict[str, list[int]] = {}
    for g in load_games():
        own = g.get("history", {}).get(str(g.get("own_pid")), [])
        card = ""
        for s in own:
            if s.get("hero_card_id"):
                card = s["hero_card_id"].split("_SKIN_")[0]
                break
        if card and g.get("place"):
            out.setdefault(card, []).append(g["place"])
    return out


def build_html(cards: CardDb | None = None) -> str:
    cards = cards or CardDb()
    pool = cards.bg_pool()
    data = {
        "generated": time.strftime("%Y-%m-%d %H:%M"),
        "minions": pool.get("minions", []),
        "spells": pool.get("spells", []),
        "trinkets": pool.get("trinkets", []),
        "heroes": pool.get("heroes", []),
        "career": _hero_career(),
    }
    return TEMPLATE.read_text(encoding="utf-8").replace(
        "/*__DATA__*/", json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    )


def build(open_browser: bool = False, cards: CardDb | None = None) -> Path:
    OUTPUT.write_text(build_html(cards), encoding="utf-8")
    if open_browser:
        webbrowser.open(OUTPUT.as_uri())
    return OUTPUT


if __name__ == "__main__":
    path = build(open_browser=True)
    print(f"built {path}")
