"""Generate stats.html — a self-contained dark dashboard over games_history.jsonl.

Run:  python stats_page.py        (builds and opens in the browser)

The page is static: data is embedded at build time, so re-run (or use the
tracker's 📈 button) to refresh. Presentation lives in stats_template.html.
"""

from __future__ import annotations

import json
import time
import webbrowser
from pathlib import Path
from types import SimpleNamespace

from cards import CardDb
from stats import load_games, own_snaps

TEMPLATE = Path(__file__).with_name("stats_template.html")
OUTPUT = Path(__file__).with_name("stats.html")


def build_html(cards: CardDb | None = None) -> str:
    """Compose the dashboard HTML with fresh data embedded."""
    cards = cards or CardDb()
    records = []
    for g in load_games():
        snaps = own_snaps(g)
        final = snaps[-1] if snaps else None
        tribe = ""
        final_board = []
        if final:
            tribe = cards.tribe_label(
                [SimpleNamespace(card_id=m["card_id"]) for m in final["minions"]]
            )
            final_board = [
                {
                    "n": cards.name(m["card_id"], m.get("name", "")),
                    "a": m["attack"],
                    "h": m["health"],
                    "g": bool(m.get("golden")),
                }
                for m in final["minions"]
            ]
        hp_track = g.get("hp_track", [])
        rounds = hp_track[-1][0] if hp_track else (final["round_num"] if final else 0)
        fights = [
            {
                "r": s["round_num"],
                "res": s.get("result", ""),
                "dmg": s.get("result_dmg", 0),
                "board": [
                    cards.name(m["card_id"], m.get("name", "")) for m in s["minions"]
                ],
            }
            for s in snaps
        ]
        records.append(
            {
                "ts": g.get("ts", ""),
                "mode": g.get("mode", "?"),
                "hero": g.get("heroes", {}).get(str(g.get("own_pid")), "?"),
                "place": g.get("place", 0),
                "rounds": rounds,
                "tribe": tribe,
                "hp": [hp for _r, hp in hp_track],
                "finalBoard": final_board,
                "fights": fights,
            }
        )

    data = {
        "generated": time.strftime("%Y-%m-%d %H:%M"),
        "games": records,
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
