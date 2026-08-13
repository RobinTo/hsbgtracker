"""Elevated helper for the disconnect trick.

Blocks Hearthstone's outbound traffic via a Windows Firewall rule for a few
seconds, then removes the rule — all in one elevated process, so the
restore cannot be lost if the tracker dies. Launched by the tracker's
"disconnect" button through a UAC prompt; can also be run by hand:

    python reconnect.py --exe "R:\\Games\\Hearthstone\\Hearthstone.exe" --seconds 6
    python reconnect.py --exe ... --clear        (just remove a leftover rule)
"""

from __future__ import annotations

import argparse
import subprocess
import time

RULE = "BGTracker HS Disconnect"


def netsh(*args: str) -> int:
    return subprocess.run(
        ["netsh", "advfirewall", "firewall", *args],
        capture_output=True, text=True,
    ).returncode


def rule_exists() -> bool:
    return netsh("show", "rule", f"name={RULE}") == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", required=True)
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--clear", action="store_true")
    a = ap.parse_args()

    netsh("delete", "rule", f"name={RULE}")  # idempotent cleanup
    if a.clear:
        return
    netsh(
        "add", "rule", f"name={RULE}", "dir=out",
        f"program={a.exe}", "action=block",
    )
    try:
        time.sleep(max(1.0, min(a.seconds, 30.0)))
    finally:
        netsh("delete", "rule", f"name={RULE}")


if __name__ == "__main__":
    main()
