"""Aggregate stats over recorded games (games_history.jsonl).

Run:  python stats.py

Shows overall record, average placement by the tribe you ended on, and the
minions that appear most often in your winning vs losing fights.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

from cards import CardDb

HISTORY_FILE = Path(__file__).with_name("games_history.jsonl")


def load_games():
    if not HISTORY_FILE.exists():
        return []
    games = []
    for line in HISTORY_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            games.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return games


def own_snaps(game):
    """Our own per-combat boards (friendly-side snapshots of our seat)."""
    return game.get("history", {}).get(str(game.get("own_pid")), [])


def team_snaps(game):
    """One snapshot per combat round for our team: our own board where we
    have it, otherwise (duos) the teammate's. The tracker files a duos combat
    under whichever partner's board it captured, and the result belongs to
    the team either way. Returns [(snap, is_own)] sorted by round."""
    rounds = {s["round_num"]: (s, True) for s in own_snaps(game)}
    mate = game.get("teammate")
    if mate:
        for s in game.get("history", {}).get(str(mate), []):
            rounds.setdefault(s["round_num"], (s, False))
    return [rounds[r] for r in sorted(rounds)]


def main():
    cards = CardDb()
    games = load_games()
    if not games:
        print(f"No games recorded yet ({HISTORY_FILE.name} is empty).")
        print("Finished games are appended automatically while the tracker runs.")
        return

    print(f"=== {len(games)} recorded game(s) ===\n")

    # ----------------------------------------------------------- overall
    places = [g["place"] for g in games if g.get("place")]
    firsts = sum(1 for p in places if p == 1)
    top_half = sum(1 for g in games if g.get("place") and (
        g["place"] <= (2 if g.get("mode") == "duos" else 4)))
    if places:
        print(f"average placement: {sum(places) / len(places):.2f}"
              f"   1st: {firsts}/{len(places)}"
              f"   top half: {top_half}/{len(places)}")
    modes = Counter(g.get("mode", "?") for g in games)
    print("modes: " + ", ".join(f"{m} x{n}" for m, n in modes.items()) + "\n")

    # ------------------------------------------- placement by final tribe
    by_tribe = defaultdict(list)
    for g in games:
        snaps = own_snaps(g)
        if not snaps or not g.get("place"):
            continue

        class _M:  # tribe_label expects objects with .card_id
            def __init__(self, cid):
                self.card_id = cid

        minions = [_M(m["card_id"]) for m in snaps[-1]["minions"]]
        tribe = cards.tribe_label(minions) or "None"
        by_tribe[tribe].append(g["place"])
    if by_tribe:
        print("average placement by your final board's tribe:")
        for tribe, ps in sorted(by_tribe.items(), key=lambda kv: sum(kv[1]) / len(kv[1])):
            avg = sum(ps) / len(ps)
            print(f"  {tribe:<12} {avg:.2f}   ({len(ps)} game(s))")
        print()

    # ------------------------------------------ minions in wins vs losses
    win_counts: Counter = Counter()
    loss_counts: Counter = Counter()
    win_fights = loss_fights = 0
    for g in games:
        for snap in own_snaps(g):
            result = snap.get("result")
            if result not in ("win", "loss"):
                continue
            bucket = win_counts if result == "win" else loss_counts
            if result == "win":
                win_fights += 1
            else:
                loss_fights += 1
            for m in snap["minions"]:
                bucket[cards.name(m["card_id"], m.get("name", ""))] += 1

    def top(counter, fights, label):
        if not fights:
            return
        print(f"most common minions in {label} ({fights} fight(s)):")
        for name, n in counter.most_common(12):
            print(f"  {n:3}x  {name}")
        print()

    top(win_counts, win_fights, "winning fights")
    top(loss_counts, loss_fights, "losing fights")


if __name__ == "__main__":
    main()
