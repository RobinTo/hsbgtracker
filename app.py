"""BG Tracker — lightweight Hearthstone Battlegrounds opponent-board tracker.

Tails Power.log, remembers each opponent's board from the last combat you
fought against them, and shows it when you hover their row.

Run:  pythonw app.py [WxH+X+Y]
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import tkinter as tk

from cards import CardDb
from parser import BgGame

POLL_SECONDS = 1.0

BG = "#1e1f22"
BG_ROW = "#26272b"
BG_HOVER = "#33353a"
FG = "#e8e6e3"
FG_DIM = "#9a978f"
ACCENT = "#e0a94a"  # next-opponent highlight
GOLD = "#ffd75e"


def find_logs_dir() -> Path | None:
    candidates = []
    try:
        import winreg

        for hive_path in (
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Hearthstone",
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Hearthstone",
        ):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, hive_path) as key:
                    loc, _ = winreg.QueryValueEx(key, "InstallLocation")
                    candidates.append(Path(loc) / "Logs")
            except OSError:
                pass
    except ImportError:
        pass
    candidates += [
        Path(r"C:\Program Files (x86)\Hearthstone\Logs"),
        Path(r"C:\Program Files\Hearthstone\Logs"),
    ]
    for c in candidates:
        if c.is_dir():
            return c
    return None


def newest_power_log(logs_dir: Path) -> Path | None:
    logs = sorted(logs_dir.glob("Hearthstone_*/Power.log"), key=lambda p: p.stat().st_mtime)
    return logs[-1] if logs else None


class LogTailer(threading.Thread):
    """Follows the newest Power.log, feeding lines to the game state."""

    def __init__(self, logs_dir: Path, game: BgGame, lock: threading.Lock, on_change):
        super().__init__(daemon=True)
        self.logs_dir = logs_dir
        self.game = game
        self.lock = lock
        self.on_change = on_change
        self.current: Path | None = None
        self._fh = None

    def run(self):
        while True:
            try:
                self._tick()
            except Exception:
                self._close()
            time.sleep(POLL_SECONDS)

    def _tick(self):
        newest = newest_power_log(self.logs_dir)
        if newest is None:
            return
        if newest != self.current:
            self._close()
            self.current = newest
            self._fh = open(newest, encoding="utf-8", errors="replace")
            self._fh.seek(self._last_game_offset(newest))
        if self._fh is None:
            return
        # Handle log truncation (new game session reusing the file).
        pos = self._fh.tell()
        size = self.current.stat().st_size
        if size < pos:
            self._fh.seek(0)
        fed = False
        with self.lock:
            for line in self._fh:
                if not line.endswith("\n"):
                    # Partial line still being written; rewind and retry later.
                    self._fh.seek(self._fh.tell() - len(line))
                    break
                self.game.feed_line(line.rstrip("\n"))
                fed = True
        if fed:
            self.on_change()

    @staticmethod
    def _last_game_offset(path: Path) -> int:
        """Byte offset of the most recent game's CREATE_GAME, so we don't
        replay a whole multi-hour session log on startup."""
        marker = b"GameState.DebugPrintPower() - CREATE_GAME"
        chunk_size = 1 << 20
        try:
            with open(path, "rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                end = size
                overlap = len(marker)
                while end > 0:
                    start = max(0, end - chunk_size)
                    fh.seek(start)
                    chunk = fh.read(min(end - start + overlap, size - start))
                    idx = chunk.rfind(marker)
                    if idx != -1:
                        line_start = chunk.rfind(b"\n", 0, idx)
                        return start + (line_start + 1 if line_start != -1 else 0)
                    end = start
        except OSError:
            pass
        return 0

    def _close(self):
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
        self._fh = None
        self.current = None


class TrackerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cards = CardDb()
        self.cards.ensure_downloaded()
        self.lock = threading.Lock()
        self.game = BgGame()
        self.dirty = threading.Event()
        self.hovered_pid: int | None = None
        self.pinned_pid: int | None = None

        # Render caches — widgets are built once per roster and then updated
        # in place; rebuilding every tick makes the window flicker.
        self._structure = None
        self._rows: dict[int, dict] = {}
        self._caps: list[tuple[tk.Label, str]] = []
        self._detail_key = None
        self._detail_sel: tuple[int, int] | None = None  # (pid, history index)
        self._highlight_pid: int | None = None
        self._view = None  # latest data pulled from the parser

        root.title("BG Tracker")
        root.configure(bg=BG)
        root.geometry("340x520")
        root.minsize(280, 320)
        root.attributes("-topmost", True)

        self._build_ui()

        logs_dir = find_logs_dir()
        if logs_dir is None:
            self.status.set("Hearthstone Logs folder not found")
        else:
            self.status.set(f"Watching {logs_dir}")
            LogTailer(logs_dir, self.game, self.lock, self.dirty.set).start()

        self._poll()

    # -------------------------------------------------------------------- UI

    def _build_ui(self):
        top = tk.Frame(self.root, bg=BG)
        top.pack(fill="x", padx=8, pady=(8, 4))
        tk.Label(top, text="Opponents", bg=BG, fg=FG, font=("Segoe UI", 11, "bold")).pack(
            side="left"
        )
        self.topmost_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            top,
            text="on top",
            variable=self.topmost_var,
            command=lambda: self.root.attributes("-topmost", self.topmost_var.get()),
            bg=BG,
            fg=FG_DIM,
            selectcolor=BG,
            activebackground=BG,
            activeforeground=FG,
            highlightthickness=0,
            font=("Segoe UI", 8),
        ).pack(side="right")

        self.rows_frame = tk.Frame(self.root, bg=BG)
        self.rows_frame.pack(fill="x", padx=8)

        sep = tk.Frame(self.root, bg=BG_HOVER, height=1)
        sep.pack(fill="x", padx=8, pady=6)

        self.detail_title = tk.Label(
            self.root, text="Hover a hero to see their last board",
            bg=BG, fg=FG_DIM, font=("Segoe UI", 10, "bold"), anchor="w"
        )
        self.detail_title.pack(fill="x", padx=10)
        self.detail = tk.Frame(self.root, bg=BG)
        self.detail.pack(fill="both", expand=True, padx=8, pady=(2, 4))

        self.status = tk.StringVar(value="starting…")
        tk.Label(
            self.root, textvariable=self.status, bg=BG, fg=FG_DIM,
            font=("Segoe UI", 8), anchor="w"
        ).pack(fill="x", side="bottom", padx=8, pady=(0, 6))

    def _poll(self):
        if self.dirty.is_set():
            self.dirty.clear()
            self.refresh()
        self.root.after(400, self._poll)

    # ------------------------------------------------------------- data pull

    def refresh(self):
        with self.lock:
            view = {
                "heroes": dict(self.game.lobby_heroes()),
                "snapshots": dict(self.game.snapshots),
                "history": {p: list(s) for p, s in self.game.history.items()},
                "statuses": self.game.player_status(),
                "names": dict(self.game.player_names),
                "next_opp": self.game.next_opponent_id,
                "friendly": self.game.friendly_controller,
                "teammate": self.game.teammate_id,
                "teams": self.game.duo_teams(),
                "own_board": self.game.friendly_board(),
                "own_tier": self.game.friendly_tech_level(),
                "in_bg": self.game.is_battlegrounds,
                "game_over": self.game.game_over,
            }
            self.cards.learn_all(self.game.learned_names)
        self._view = view

        if not view["in_bg"] or not view["heroes"]:
            self.status.set("Waiting for a Battlegrounds game…")
        elif view["game_over"]:
            place = view["statuses"].get(view["friendly"], {}).get("place", 0)
            self.status.set(f"Game over — you placed #{place}" if place else "Game over")
        else:
            self.status.set("In game")

        teams = view["teams"]
        own_team = teams.get(view["friendly"], 0)
        if teams:
            order = sorted(
                view["heroes"],
                key=lambda p: (teams.get(p, 99) != own_team, teams.get(p, 99), p),
            )
        else:
            order = sorted(view["heroes"])
        view["order"] = order

        structure = (tuple(order), tuple(teams.get(p) for p in order))
        if structure != self._structure:
            self._structure = structure
            self._build_rows(view)
        self._update_rows(view)
        self._update_highlight()
        self._update_detail()

    # ---------------------------------------------------------- row building

    def _build_rows(self, view):
        for w in self.rows_frame.winfo_children():
            w.destroy()
        self._rows.clear()
        self._caps = []
        self._highlight_pid = None

        teams = view["teams"]
        prev_team = None
        first = True
        for pid in view["order"]:
            team = teams.get(pid)
            if teams and team != prev_team:
                cap = tk.Frame(self.rows_frame, bg=BG)
                cap.pack(fill="x", pady=((0, 0) if first else (6, 0)))
                lbl = tk.Label(cap, text="", bg=BG, fg=FG_DIM,
                               font=("Segoe UI", 8), anchor="e")
                lbl.pack(side="right", padx=2)
                self._caps.append((lbl, team))
            prev_team = team

            row = tk.Frame(self.rows_frame, bg=BG_ROW, padx=6, pady=3)
            row.pack(fill="x", pady=1)
            left = tk.Label(row, text="", bg=BG_ROW, fg=FG,
                            font=("Segoe UI", 10), anchor="w")
            left.pack(side="left")
            right = tk.Label(row, text="", bg=BG_ROW, fg=FG_DIM, font=("Segoe UI", 8))
            right.pack(side="right")

            if pid != view["friendly"]:
                for w in (row, left, right):
                    w.bind("<Enter>", lambda _e, p=pid: self._hover(p))
                    w.bind("<Button-1>", lambda _e, p=pid: self._pin(p))
            self._rows[pid] = {"row": row, "left": left, "right": right, "cache": None}
            first = False

    def _team_sum(self, view, team) -> str:
        atk = hp = 0
        seen = False
        missing_mate = False
        for p in view["order"]:
            if view["teams"].get(p) != team:
                continue
            minions = None
            if p == view["friendly"]:
                minions = view["own_board"]
            elif p in view["snapshots"]:
                minions = view["snapshots"][p].minions
            elif p == view["teammate"]:
                missing_mate = True
            if minions:
                seen = True
                atk += sum(m.attack for m in minions)
                hp += sum(m.health for m in minions)
        if not seen:
            return "Σ —"
        note = "  (your side only)" if missing_mate else ""
        return f"Σ {atk:,} / {hp:,}{note}"

    def _update_rows(self, view):
        for lbl, team in self._caps:
            text = self._team_sum(view, team)
            if lbl.cget("text") != text:
                lbl.configure(text=text)

        for pid, refs in self._rows.items():
            hero = view["heroes"].get(pid)
            if hero is None:
                continue
            is_self = pid == view["friendly"]
            is_teammate = view["teammate"] and pid == view["teammate"]
            is_opponent = not is_self and not is_teammate
            is_next = pid == view["next_opp"] and is_opponent
            snap = view["snapshots"].get(pid)

            hero_name = hero.name or self.cards.name(hero.card_id)
            suffix = " (you)" if is_self else (" (teammate)" if is_teammate else "")
            marker = "⚔ " if is_next else "   "
            left_text = f"{marker}{hero_name}{suffix}"

            st = view["statuses"].get(pid, {})
            place = st.get("place", 0)
            ehp = st.get("hp", 0) + st.get("armor", 0)  # effective hp
            dead = ehp <= 0 and place > 0

            parts = []
            if dead:
                parts.append(f"out #{place}")
            else:
                if view["game_over"] and place:
                    parts.append(f"#{place}")
                board = view["own_board"] if is_self else (snap.minions if snap else None)
                tribe = self.cards.tribe_label(board) if board else ""
                if tribe:
                    parts.append(tribe)
                if ehp > 0:
                    parts.append(f"{ehp}hp")
                if is_self:
                    if view["own_tier"]:
                        parts.append(f"T{view['own_tier']}")
                elif snap:
                    if snap.tech_level:
                        parts.append(f"T{snap.tech_level}")
                    parts.append(f"r{snap.round_num}")
                elif not is_teammate:
                    parts.append("—")
            right_text = " · ".join(parts)

            fg = ACCENT if is_next else (FG_DIM if dead else FG)
            font = ("Segoe UI", 10, "bold" if is_next else "normal")

            cache = (left_text, fg, font, right_text)
            if refs["cache"] != cache:
                refs["cache"] = cache
                refs["left"].configure(text=left_text, fg=fg, font=font)
                refs["right"].configure(text=right_text)

    # ------------------------------------------------------- hover and detail

    def _hover(self, pid: int):
        if self.hovered_pid != pid:
            self.hovered_pid = pid
            if self.pinned_pid is None:
                self._detail_sel = None
                self._update_highlight()
                self._update_detail()

    def _pin(self, pid: int):
        self.pinned_pid = None if self.pinned_pid == pid else pid
        self._detail_sel = None
        self._update_highlight()
        self._update_detail()

    def _select_round(self, pid: int, idx: int):
        self._detail_sel = (pid, idx)
        self._update_detail()

    def _shown_pid(self):
        return self.pinned_pid if self.pinned_pid is not None else self.hovered_pid

    def _update_highlight(self):
        shown = self._shown_pid()
        if shown == self._highlight_pid:
            return
        for pid in (self._highlight_pid, shown):
            refs = self._rows.get(pid)
            if refs:
                color = BG_HOVER if pid == shown else BG_ROW
                refs["row"].configure(bg=color)
                for child in refs["row"].winfo_children():
                    child.configure(bg=color)
        self._highlight_pid = shown

    def _update_detail(self):
        view = self._view
        if view is None:
            return
        pid = self._shown_pid()
        hist = view["history"].get(pid, []) if pid is not None else []
        if self._detail_sel and self._detail_sel[0] == pid and self._detail_sel[1] < len(hist):
            idx = self._detail_sel[1]
        else:
            idx = len(hist) - 1
        snap = hist[idx] if hist else None
        key = (pid, idx, id(snap), snap.result if snap else "", self.pinned_pid)
        if key == self._detail_key:
            return
        self._detail_key = key

        for w in self.detail.winfo_children():
            w.destroy()

        if pid is None or pid not in view["heroes"]:
            self.detail_title.configure(text="Hover a hero to see their last board")
            return

        hero = view["heroes"][pid]
        hero_name = hero.name or self.cards.name(hero.card_id)
        pin_mark = " 📌" if self.pinned_pid == pid else ""
        if snap is None:
            self.detail_title.configure(text=f"{hero_name}{pin_mark} — no board seen yet")
            return
        title = f"{hero_name}{pin_mark} — round {snap.round_num}"
        tag = view["names"].get(pid, "")
        if tag:
            title += f"  ({tag})"
        if snap.tech_level:
            title += f"  T{snap.tech_level}"
        self.detail_title.configure(text=title)

        meta = []
        if snap.result == "win":
            meta.append(f"you won (+{snap.result_dmg})")
        elif snap.result == "loss":
            meta.append(f"you lost (-{snap.result_dmg})")
        elif snap.result == "tie":
            meta.append("tied")
        if snap.hero_power:
            meta.append("⚡ " + self.cards.name(snap.hero_power))
        if snap.trinkets:
            meta.append("🎁 " + ", ".join(self.cards.name(t) for t in snap.trinkets))
        if meta:
            tk.Label(
                self.detail, text="   ·   ".join(meta), bg=BG, fg=FG_DIM,
                font=("Segoe UI", 9), anchor="w", wraplength=330, justify="left",
            ).pack(fill="x", padx=4, pady=(0, 2))

        if len(hist) > 1:
            rounds = tk.Frame(self.detail, bg=BG)
            rounds.pack(fill="x", padx=4, pady=(0, 3))
            for i, s in enumerate(hist):
                mark = {"win": "＋", "loss": "－", "tie": "＝"}.get(s.result, "")
                lbl = tk.Label(
                    rounds,
                    text=f"r{s.round_num}{mark}",
                    bg=BG_HOVER if i == idx else BG_ROW,
                    fg=FG if i == idx else FG_DIM,
                    font=("Segoe UI", 8, "bold" if i == idx else "normal"),
                    padx=5, pady=1,
                )
                lbl.pack(side="left", padx=(0, 3))
                lbl.bind("<Button-1>", lambda _e, p=pid, j=i: self._select_round(p, j))

        if not snap.minions:
            tk.Label(
                self.detail, text="(empty board)", bg=BG, fg=FG_DIM,
                font=("Segoe UI", 9), anchor="w"
            ).pack(fill="x", padx=4)
            return
        for m in snap.minions:
            name = self.cards.name(m.card_id, m.name)
            line = tk.Frame(self.detail, bg=BG)
            line.pack(fill="x", padx=4, pady=1)
            tk.Label(
                line,
                text=f"{m.attack}/{m.health}",
                bg=BG, fg=GOLD if m.golden else FG_DIM,
                font=("Consolas", 10), width=9, anchor="e",
            ).pack(side="left")
            label = name + (" ★" if m.golden else "")
            if m.keywords:
                label += "  ·  " + ", ".join(m.keywords)
            tk.Label(
                line, text=label, bg=BG,
                fg=GOLD if m.golden else FG,
                font=("Segoe UI", 10), anchor="w",
            ).pack(side="left", padx=(8, 0))


def main():
    import sys

    root = tk.Tk()
    TrackerApp(root)
    # Optional geometry override, e.g.:  pythonw app.py 340x520+1940+60
    if len(sys.argv) > 1:
        root.geometry(sys.argv[1])
    root.mainloop()


if __name__ == "__main__":
    main()
