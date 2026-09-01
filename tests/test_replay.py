"""Regression harness: replay stored Power.logs and assert known-good facts.

The logs live in tests/data/ (gitignored, machine-local):
  duos_game1.log   full duos game from 2026-08-11, 13 rounds, won lobby
  solo_capped.log  solo game from 2026-08-11 that hit the old 10MB log cap

Run:  python tests/test_replay.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from parser import replay  # noqa: E402

DATA = Path(__file__).with_name("data")
FAILURES = []


def check(label, actual, expected):
    if actual != expected:
        FAILURES.append(f"{label}: expected {expected!r}, got {actual!r}")


def test_duos_game1():
    path = DATA / "duos_game1.log"
    if not path.exists():
        print("SKIP duos_game1 (log not present)")
        return
    t0 = time.time()
    g = replay(str(path), debug=False)
    print(f"replayed duos_game1 in {time.time() - t0:.1f}s")

    check("duos: is_battlegrounds", g.is_battlegrounds, True)
    check("duos: is_duos", g.is_duos, True)
    check("duos: friendly", g.friendly_controller, 6)
    check("duos: teammate", g.teammate_id, 7)
    check("duos: game_over", g.game_over, True)
    check(
        "duos: teams",
        g.duo_teams(),
        {6: 1, 7: 1, 3: 3, 2: 3, 5: 2, 4: 2, 1: 4, 8: 4},
    )
    check(
        "duos: places",
        {p: st["place"] for p, st in g.player_status().items()},
        {6: 1, 7: 1, 2: 2, 3: 2, 4: 3, 5: 3, 1: 4, 8: 4},
    )
    check(
        "duos: thorim rounds",
        [s.round_num for s in g.history.get(1, [])],
        [2, 5, 8, 11],
    )
    check(
        "duos: thorim results",  # r12 is a ghost fight, won via face damage
        [s.result for s in g.history.get(1, [])],
        ["loss", "win", "win", "win"],
    )
    check(
        "duos: eudora results",
        [s.result for s in g.history.get(4, [])],
        ["win", "win", "loss", "win"],
    )
    # The r14 final combat is announced after MAIN_START (tag ordering varies
    # by patch) — the late-entry path recovers it, hence 11 teammate boards.
    check("duos: teammate snapshot count", len(g.history.get(7, [])), 11)
    check("duos: teammate final round", g.history.get(7, [])[-1].round_num, 14)
    check("duos: name of pid 1", g.player_names.get(1), "MrAzaghast")
    check("duos: hp_track length", len(g.hp_track), 13)
    check("duos: final effective hp", g.hp_track[-1][1], 20)


def test_solo_capped():
    path = DATA / "solo_capped.log"
    if not path.exists():
        print("SKIP solo_capped (log not present)")
        return
    t0 = time.time()
    g = replay(str(path), debug=False)
    print(f"replayed solo_capped in {time.time() - t0:.1f}s")

    check("solo: is_battlegrounds", g.is_battlegrounds, True)
    check("solo: is_duos", g.is_duos, False)
    check("solo: friendly", g.friendly_controller, 6)
    check("solo: teammate", g.teammate_id, 0)
    check("solo: game_over (log capped mid-game)", g.game_over, False)
    check("solo: lobby size", len(g.lobby_heroes()), 8)
    check("solo: shudderwock", g.lobby_heroes()[1].name, "Shudderwock")
    check(
        "solo: last-seen rounds (own board included as pid 6)",
        {pid: s.round_num for pid, s in g.snapshots.items()},
        {1: 1, 5: 2, 4: 3, 3: 4, 2: 5, 8: 6, 7: 7, 6: 7},
    )


def main():
    test_duos_game1()
    test_solo_capped()
    if FAILURES:
        print(f"\nFAIL ({len(FAILURES)}):")
        for f in FAILURES:
            print("  " + f)
        sys.exit(1)
    print("\nall checks passed")


if __name__ == "__main__":
    main()
