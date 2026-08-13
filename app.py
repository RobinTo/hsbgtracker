"""BG Tracker — lightweight Hearthstone Battlegrounds opponent-board tracker.

Tails Power.log, remembers each opponent's board from the last combat you
fought against them, and shows it when you hover their row.

Run:  pythonw app.py [WxH+X+Y]
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import asdict
from pathlib import Path

import tkinter as tk

from cards import CardDb
from images import ArtStore, TILE_H, TILE_W
from parser import BgGame

POLL_SECONDS = 1.0
CONFIG_FILE = Path(__file__).with_name("tracker_config.json")
HISTORY_FILE = Path(__file__).with_name("games_history.jsonl")

SKIN_RE = __import__("re").compile(r"_SKIN_.*$")


def base_hero(card_id: str) -> str:
    """Skins are the same hero: BG20_HERO_202_SKIN_D -> BG20_HERO_202."""
    return SKIN_RE.sub("", card_id or "")


VERSION_RE = __import__("re").compile(r"BattleNet version: Product = ([\d.]+)")


def read_game_patch(log_dir: Path | None) -> str:
    """Client patch (e.g. '36.2.0') from the session's Hearthstone.log."""
    if log_dir is None:
        return ""
    try:
        with open(log_dir / "Hearthstone.log", encoding="utf-8", errors="replace") as fh:
            head = fh.read(65536)
        m = VERSION_RE.search(head)
        if m:
            v = m.group(1)
            return v[:-2] if v.endswith(".0") and v.count(".") == 3 else v
    except OSError:
        pass
    return ""


def load_career() -> dict:
    """Per-hero career from games_history.jsonl:
    {base_card: {"name": str, "places": [int, ...]}}"""
    import json as _json

    out: dict = {}
    try:
        for line in HISTORY_FILE.read_text(encoding="utf-8").splitlines():
            try:
                g = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            own = g.get("history", {}).get(str(g.get("own_pid")), [])
            card = ""
            for s in own:
                if s.get("hero_card_id"):
                    card = base_hero(s["hero_card_id"])
                    break
            if not card:
                continue
            hero_name = g.get("heroes", {}).get(str(g.get("own_pid")), "")
            rec = out.setdefault(card, {"name": hero_name, "places": []})
            if g.get("place"):
                rec["places"].append(g["place"])
    except OSError:
        pass
    return out

TRIBE_ICONS = {
    "Beast": "🐾",
    "Demon": "😈",
    "Dragon": "🐉",
    "Elemental": "🔥",
    "Mech": "⚙",
    "Murloc": "🐟",
    "Naga": "🐍",
    "Pirate": "☠",
    "Quilboar": "🐗",
    "Undead": "💀",
    "Amalgam": "🧬",
    "Mixed": "✚",
}


def set_app_identity(root: tk.Tk):
    """Own taskbar identity + icon, so the tracker doesn't group with other
    pythonw windows and a pinned shortcut points at it cleanly."""
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("hstracker.bg")
    except Exception:
        pass
    ico = Path(__file__).with_name("icon.ico")
    if ico.exists():
        try:
            root.iconbitmap(str(ico))
        except tk.TclError:
            pass


def enable_dark_titlebar(root: tk.Tk):
    """Ask DWM for a dark title bar (Windows 10 1809+; silently no-op elsewhere)."""
    try:
        import ctypes

        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        value = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE old/new builds
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(value), 4
            ) == 0:
                break
    except Exception:
        pass

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


class Tooltip:
    """Card popup next to the tracker window: full render when the art CDN
    has one, otherwise name + tier/tribe + effect text."""

    def __init__(self, root: tk.Tk, cards: CardDb, art: ArtStore):
        self.root = root
        self.cards = cards
        self.art = art
        self.win: tk.Toplevel | None = None
        self._pending: tuple | None = None  # (widget, after_id)

    def attach(self, widget, card_id: str, extra: str = ""):
        widget.bind("<Enter>", lambda _e: self._schedule(widget, card_id, extra), add="+")
        widget.bind("<Leave>", lambda _e: self.hide(), add="+")

    def _schedule(self, widget, card_id, extra):
        self.hide()
        after_id = widget.after(250, lambda: self._show(widget, card_id, extra))
        self._pending = (widget, after_id)

    def hide(self):
        if self._pending is not None:
            widget, after_id = self._pending
            try:
                widget.after_cancel(after_id)
            except tk.TclError:
                pass
            self._pending = None
        if self.win is not None:
            try:
                self.win.destroy()
            except tk.TclError:
                pass
            self.win = None

    def _show(self, widget, card_id, extra):
        self._pending = None
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg="#0c0c0e")
        inner = tk.Frame(win, bg="#0c0c0e", padx=6, pady=6)
        inner.pack()

        render = self.art.get_render(card_id)
        if render is not None:
            lbl = tk.Label(inner, image=render, bg="#0c0c0e", bd=0)
            lbl.image = render
            lbl.pack()
        else:
            name = self.cards.name(card_id)
            tk.Label(inner, text=name, bg="#0c0c0e", fg=FG,
                     font=("Segoe UI", 10, "bold"), anchor="w").pack(fill="x")
            sub = []
            tier = self.cards.tech_level(card_id)
            if tier:
                sub.append(f"Tier {tier}")
            races = self.cards.races(card_id)
            if races:
                sub.append("/".join(r.title() for r in races))
            if sub:
                tk.Label(inner, text=" · ".join(sub), bg="#0c0c0e", fg=FG_DIM,
                         font=("Segoe UI", 8), anchor="w").pack(fill="x")
            text = self.cards.text(card_id)
            if text:
                tk.Label(inner, text=text, bg="#0c0c0e", fg=FG,
                         font=("Segoe UI", 9), wraplength=230,
                         justify="left", anchor="w").pack(fill="x", pady=(3, 0))
        if extra:
            tk.Label(inner, text=extra, bg="#0c0c0e", fg=GOLD,
                     font=("Segoe UI", 8), wraplength=230,
                     justify="left", anchor="w").pack(fill="x", pady=(3, 0))

        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        x = self.root.winfo_rootx() - w - 8
        if x < 0:
            x = self.root.winfo_rootx() + self.root.winfo_width() + 8
        y = min(widget.winfo_rooty(), self.root.winfo_screenheight() - h - 10)
        win.geometry(f"+{x}+{y}")
        self.win = win


class TrackerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cards = CardDb()
        self.cards.ensure_downloaded()
        self.lock = threading.Lock()
        self.game = BgGame()
        self.dirty = threading.Event()
        self.art_dirty = threading.Event()
        self.art = ArtStore(on_new=self.art_dirty.set)
        self.tooltip = Tooltip(root, self.cards, self.art)
        self.hovered_pid: int | None = None
        self.pinned_pid: int | None = None

        # Render caches — widgets are built once per roster and then updated
        # in place; rebuilding every tick makes the window flicker.
        self._structure = None
        self._rows: dict[int, dict] = {}
        self._caps: list[tuple[tk.Label, str]] = []
        self._detail_key = None
        self._detail_sel: tuple[int, int] | None = None  # (pid, history index)
        self._highlight_pids: set[int] = set()
        self._view = None  # latest data pulled from the parser
        self._last_click = 0.0  # manual pin/round clicks pause auto-select
        self._prev_next_opp = 0
        self._recap_shown = False
        self._career = load_career()
        self.tailer: LogTailer | None = None
        self._patch_cache: tuple[Path | None, str] = (None, "")

        root.title("BG Tracker")
        root.configure(bg=BG)
        root.geometry(self._load_geometry() or "520x600")
        root.minsize(360, 320)
        root.attributes("-topmost", True)
        set_app_identity(root)
        enable_dark_titlebar(root)

        self._build_ui()
        self._geom_save_job = None
        root.bind("<Configure>", self._on_configure)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.bind("<Escape>", lambda _e: self._clear_selection())
        root.bind("<Up>", lambda _e: self._nav_player(-1))
        root.bind("<Down>", lambda _e: self._nav_player(1))
        root.bind("<Left>", lambda _e: self._nav_round(-1))
        root.bind("<Right>", lambda _e: self._nav_round(1))

        logs_dir = find_logs_dir()
        if logs_dir is None:
            self.status.set("Hearthstone Logs folder not found")
        else:
            self.status.set(f"Watching {logs_dir}")
            self.tailer = LogTailer(logs_dir, self.game, self.lock, self.dirty.set)
            self.tailer.start()

        import live_server
        import stats_page

        self._server = live_server.start(
            self._live_state, lambda: stats_page.build_html(self.cards)
        )

        self._poll()

    def _live_state(self):
        with self.lock:
            return {
                "in_game": self.game.is_battlegrounds and not self.game.game_over,
                "round": self.game.turn,
                "mode": "duos" if self.game.is_duos else "solo",
                "game_over": self.game.game_over,
            }

    # ------------------------------------------------------- window geometry

    @staticmethod
    def _load_geometry() -> str | None:
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return cfg.get("geometry")
        except (OSError, json.JSONDecodeError):
            return None

    def _save_geometry(self):
        self._geom_save_job = None
        try:
            CONFIG_FILE.write_text(
                json.dumps({"geometry": self.root.geometry()}), encoding="utf-8"
            )
        except OSError:
            pass

    def _on_configure(self, event):
        if event.widget is not self.root:
            return
        if self._geom_save_job is not None:
            self.root.after_cancel(self._geom_save_job)
        self._geom_save_job = self.root.after(1500, self._save_geometry)

    def _on_close(self):
        self._save_geometry()
        self.root.destroy()

    # ---------------------------------------------------- keyboard navigation

    def _nav_player(self, delta: int):
        view = self._view
        if not view or not view.get("order"):
            return
        order = view["order"]
        cur = self._shown_pid()
        try:
            i = order.index(cur)
        except ValueError:
            i = -delta if delta > 0 else 0
        pid = order[(i + delta) % len(order)]
        self._last_click = time.time()
        self.pinned_pid = pid
        self._detail_sel = None
        self._update_highlight()
        self._update_detail()

    def _nav_round(self, delta: int):
        view = self._view
        pid = self._shown_pid()
        if not view or pid is None:
            return
        hist = view["history"].get(pid, [])
        if len(hist) < 2:
            return
        if self._detail_sel and self._detail_sel[0] == pid:
            idx = self._detail_sel[1]
        else:
            idx = len(hist) - 1
        self._select_round(pid, max(0, min(len(hist) - 1, idx + delta)))

    # -------------------------------------------------------------------- UI

    def _build_ui(self):
        top = tk.Frame(self.root, bg=BG)
        top.pack(fill="x", padx=8, pady=(8, 4))
        tk.Label(top, text="Opponents", bg=BG, fg=FG, font=("Segoe UI", 11, "bold")).pack(
            side="left"
        )
        self.recap_btn = tk.Label(top, text="📊 recap", bg=BG, fg=FG_DIM,
                                  font=("Segoe UI", 8, "underline"), cursor="hand2")
        self.recap_btn.bind("<Button-1>", lambda _e: self._clear_selection())
        self._recap_btn_shown = False
        stats_btn = tk.Label(top, text="📈 stats", bg=BG, fg=FG_DIM,
                             font=("Segoe UI", 8, "underline"), cursor="hand2")
        stats_btn.bind("<Button-1>", lambda _e: self._open_stats())
        stats_btn.pack(side="right", padx=(0, 8))
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

        holder = tk.Canvas(self.root, bg=BG, highlightthickness=0)
        scroll = tk.Scrollbar(self.root, orient="vertical", command=holder.yview,
                              width=8)
        holder.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y", pady=(2, 4))
        holder.pack(fill="both", expand=True, padx=(8, 0), pady=(2, 4))
        self.detail = tk.Frame(holder, bg=BG)
        win = holder.create_window((0, 0), window=self.detail, anchor="nw")
        self.detail.bind(
            "<Configure>",
            lambda _e: holder.configure(scrollregion=holder.bbox("all")),
        )
        holder.bind(
            "<Configure>", lambda e: holder.itemconfigure(win, width=e.width)
        )
        self._detail_canvas = holder
        self.root.bind_all("<MouseWheel>", self._on_wheel)

        self.status = tk.StringVar(value="starting…")
        tk.Label(
            self.root, textvariable=self.status, bg=BG, fg=FG_DIM,
            font=("Segoe UI", 8), anchor="w"
        ).pack(fill="x", side="bottom", padx=8, pady=(0, 6))

    def _on_wheel(self, event):
        c = self._detail_canvas
        # Only scroll when the content actually overflows the viewport.
        _, _, _, content_h = c.bbox("all") or (0, 0, 0, 0)
        if content_h > c.winfo_height():
            c.yview_scroll(-1 if event.delta > 0 else 1, "units")

    def _poll(self):
        if self.art_dirty.is_set():
            # New card art landed on disk — force the detail pane to rebuild.
            self.art_dirty.clear()
            self._detail_key = None
            self.dirty.set()
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
                "own_extras": self.game._side_extras(self.game.friendly_controller),
                "in_bg": self.game.is_battlegrounds,
                "game_over": self.game.game_over,
                "turn": self.game.turn,
                "hp_track": list(self.game.hp_track),
                "anomaly": self.game.anomaly_dbf,
                "choices": self.game.hero_choices(),
                "choosing": self.game.is_choosing(),
                "econ": dict(self.game.econ),
                "tier_ups": list(self.game.tier_ups),
            }
            self.cards.learn_all(self.game.learned_names)
        self._view = view

        if not view["in_bg"] or not view["heroes"]:
            self.status.set("Waiting for a Battlegrounds game…")
        elif view["game_over"]:
            place = view["statuses"].get(view["friendly"], {}).get("place", 0)
            self.status.set(f"Game over — you placed #{place}" if place else "Game over")
            if not self._recap_shown:
                # Jump to the recap once per game and persist the game.
                self._recap_shown = True
                self.pinned_pid = None
                self.hovered_pid = None
                self._persist_game(view)
            if not self._recap_btn_shown:
                self._recap_btn_shown = True
                self.recap_btn.pack(side="right", padx=(0, 8))
        else:
            self._recap_shown = False
            if self._recap_btn_shown:
                self._recap_btn_shown = False
                self.recap_btn.pack_forget()
            txt = f"In game · round {view['turn']}" if view["turn"] else "In game"
            if view["anomaly"]:
                aid = self.cards.card_by_dbf(view["anomaly"])
                if aid:
                    txt += f" · anomaly: {self.cards.name(aid)}"
            self.status.set(txt)

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

        # Auto-select the upcoming opponent when it changes, unless the user
        # clicked something in the last couple of seconds.
        nxt = view["next_opp"]
        if (
            nxt
            and nxt != self._prev_next_opp
            and nxt in view["heroes"]
            and nxt != view["friendly"]
            and not (view["teammate"] and nxt == view["teammate"])
            and time.time() - self._last_click > 2
        ):
            self.pinned_pid = nxt
            self._detail_sel = None
        if nxt:
            self._prev_next_opp = nxt

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
        self._highlight_pids = set()

        teams = view["teams"]
        if teams:
            self._build_rows_duos(view)
            return
        for pid in view["order"]:
            row = tk.Frame(self.rows_frame, bg=BG_ROW, padx=6, pady=3)
            row.pack(fill="x", pady=1)
            self._hero_art(row, view, pid).pack(side="left", padx=(0, 6))
            left = tk.Label(row, text="", bg=BG_ROW, fg=FG,
                            font=("Segoe UI", 10), anchor="w")
            left.pack(side="left")
            right = tk.Label(row, text="", bg=BG_ROW, fg=FG_DIM, font=("Segoe UI", 8))
            right.pack(side="right")
            self._bind_row(pid, view, row, left, right)
            self._rows[pid] = {"row": row, "left": left, "right": right, "cache": None}

    HERO_ART_W = 52

    def _hero_art(self, parent, view, pid):
        """Small crop of the hero's tile art."""
        h = 29
        c = tk.Canvas(parent, width=self.HERO_ART_W, height=h, bg=BG_ROW,
                      highlightthickness=0)
        hero = view["heroes"].get(pid)
        if hero is not None and hero.card_id:
            img = self.art.get_tile_small(hero.card_id)
            if img is not None:
                c.create_image(self.HERO_ART_W, h // 2, image=img, anchor="e")
                c.image = img
                for width, stipple in ((10, "gray25"), (5, "gray50")):
                    c.create_rectangle(0, 0, width, h, fill=BG_ROW, width=0,
                                       stipple=stipple)
        return c

    def _build_rows_duos(self, view):
        """One block per team: Σ caption on top, both heroes side by side."""
        teams = view["teams"]
        seen = []
        for pid in view["order"]:
            team = teams.get(pid)
            if team not in seen:
                seen.append(team)
        for i, team in enumerate(seen):
            cap = tk.Frame(self.rows_frame, bg=BG)
            cap.pack(fill="x", pady=((0, 0) if i == 0 else (5, 0)))
            lbl = tk.Label(cap, text="", bg=BG, fg=FG_DIM,
                           font=("Segoe UI", 8), anchor="e")
            lbl.pack(side="right", padx=2)
            self._caps.append((lbl, team))

            block = tk.Frame(self.rows_frame, bg=BG)
            block.pack(fill="x", pady=1)
            block.columnconfigure(0, weight=1, uniform="duo")
            block.columnconfigure(1, weight=1, uniform="duo")
            members = [p for p in view["order"] if teams.get(p) == team]
            for j, pid in enumerate(members):
                cell = tk.Frame(block, bg=BG_ROW, padx=6, pady=3)
                cell.grid(row=0, column=j, sticky="nsew",
                          padx=((0, 2) if j == 0 else (2, 0)))
                art = self._hero_art(cell, view, pid)
                art.pack(side="left", padx=(0, 6))
                texts = tk.Frame(cell, bg=BG_ROW)
                texts.pack(side="left", fill="both", expand=True)
                left = tk.Label(texts, text="", bg=BG_ROW, fg=FG,
                                font=("Segoe UI", 10), anchor="w")
                left.pack(fill="x")
                right = tk.Label(texts, text="", bg=BG_ROW, fg=FG_DIM,
                                 font=("Segoe UI", 8), anchor="w")
                right.pack(fill="x")
                self._bind_row(pid, view, cell, texts, art, left, right)
                self._rows[pid] = {"row": cell, "left": left, "right": right, "cache": None}

    def _bind_row(self, pid, view, *widgets):
        for w in widgets:
            w.bind("<Enter>", lambda _e, p=pid: self._hover(p))
            w.bind("<Button-1>", lambda _e, p=pid: self._pin(p))

    # ------------------------------------------------------- derived numbers

    def _threat(self, tech_level: int, minions) -> int:
        """Rough damage this board deals on a full win: hero tier plus the
        tier of every surviving minion."""
        return tech_level + sum(self.cards.tech_level(m.card_id) or 1 for m in minions)

    def _buff_delta(self, minions) -> tuple[int, int]:
        da = dh = 0
        for m in minions:
            bs = self.cards.base_stats(m.card_id)
            if bs is None:
                continue
            da += m.attack - bs[0]
            dh += m.health - bs[1]
        return da, dh

    def _team_sum(self, view, team) -> str:
        atk = hp = 0
        seen = False
        missing_mate = False
        members = [p for p in view["order"] if view["teams"].get(p) == team]
        for p in members:
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
        rec = ""
        if view["friendly"] not in members:
            # Head-to-head record: one entry per combat round vs this team.
            fights: dict[int, str] = {}
            for p in members:
                for s in view["history"].get(p, []):
                    if s.result:
                        fights[s.round_num] = s.result
            w = sum(1 for r in fights.values() if r == "win")
            l = sum(1 for r in fights.values() if r == "loss")
            t = sum(1 for r in fights.values() if r == "tie")
            if fights:
                rec = f" · {w}W {l}L" + (f" {t}T" if t else "")
        return f"Σ {atk:,} / {hp:,}{rec}{note}"

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
            nxt = view["next_opp"]
            teams = view["teams"]
            # In duos you fight the whole team — mark both members.
            is_next = is_opponent and (
                pid == nxt
                or (bool(teams) and nxt in teams and teams.get(pid) == teams[nxt])
            )
            snap = view["snapshots"].get(pid)

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
                    parts.append(TRIBE_ICONS.get(tribe, tribe))
                if ehp > 0:
                    parts.append(f"{ehp}hp")
                if is_self:
                    if view["own_tier"]:
                        parts.append(f"T{view['own_tier']}")
                elif snap:
                    if snap.tech_level:
                        parts.append(f"T{snap.tech_level}")
                    age = view["turn"] - snap.round_num if view["turn"] else 0
                    parts.append(
                        f"r{snap.round_num} ({age} old)" if age >= 2 else f"r{snap.round_num}"
                    )
                elif not is_teammate:
                    parts.append("—")
            right_text = " · ".join(parts)

            hero_name = hero.name or self.cards.name(hero.card_id)
            suffix = " (you)" if is_self else (" (teammate)" if is_teammate else "")
            marker = ("👻 " if dead else "⚔ ") if is_next else ""
            left_text = f"{marker}{hero_name}{suffix}"

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
        self._last_click = time.time()
        self.pinned_pid = None if self.pinned_pid == pid else pid
        self._detail_sel = None
        self._update_highlight()
        self._update_detail()

    def _select_round(self, pid: int, idx: int):
        self._last_click = time.time()
        self._detail_sel = (pid, idx)
        self._update_detail()

    def _open_stats(self):
        def work():
            try:
                if self._server is not None:
                    import webbrowser

                    from live_server import PORT

                    webbrowser.open(f"http://127.0.0.1:{PORT}/")
                else:
                    import stats_page

                    stats_page.build(open_browser=True, cards=self.cards)
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    def _clear_selection(self):
        """Back to the default view (the recap, once a game is over)."""
        self._last_click = time.time()
        self.pinned_pid = None
        self.hovered_pid = None
        self._detail_sel = None
        self._update_highlight()
        self._update_detail()

    # ------------------------------------------------------ game persistence

    def _persist_game(self, view):
        """Append the finished game to games_history.jsonl (deduplicated, so
        re-parsing the log after an app restart doesn't double-write)."""
        try:
            sig_src = json.dumps(
                [sorted(view["names"].items()), view["hp_track"]], default=str
            )
            sig = hashlib.md5(sig_src.encode()).hexdigest()[:12]
            if HISTORY_FILE.exists():
                with open(HISTORY_FILE, "rb") as fh:
                    fh.seek(0, 2)
                    fh.seek(max(0, fh.tell() - 65536))
                    if sig.encode() in fh.read():
                        return
            # Duos teams share placement — fall back to the teammate's if our
            # own hero's status was lost (e.g. it left PLAY on death).
            place = view["statuses"].get(view["friendly"], {}).get("place", 0)
            if not place and view["teammate"]:
                place = view["statuses"].get(view["teammate"], {}).get("place", 0)
            log_dir = self.tailer.current.parent if (
                self.tailer and self.tailer.current
            ) else None
            if self._patch_cache[0] != log_dir:
                self._patch_cache = (log_dir, read_game_patch(log_dir))
            rec = {
                "sig": sig,
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "patch": self._patch_cache[1],
                "mode": "duos" if view["teams"] else "solo",
                "own_pid": view["friendly"],
                "teammate": view["teammate"],
                "place": place,
                "choices": [
                    {"card": c, "n": n or self.cards.name(c)}
                    for c, n, _p in view.get("choices", [])
                ],
                "econ": view.get("econ", {}),
                "tierUps": view.get("tier_ups", []),
                "teams": view["teams"],
                "names": view["names"],
                "heroes": {
                    p: (h.name or h.card_id) for p, h in view["heroes"].items()
                },
                "statuses": view["statuses"],
                "hp_track": view["hp_track"],
                "history": {
                    p: [asdict(s) for s in snaps]
                    for p, snaps in view["history"].items()
                },
            }
            with open(HISTORY_FILE, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._career = load_career()
        except Exception:
            pass  # never let stats bookkeeping break the tracker

    def _shown_pid(self):
        return self.pinned_pid if self.pinned_pid is not None else self.hovered_pid

    def _update_highlight(self):
        shown = self._shown_pid()
        group: set[int] = set()
        if shown is not None:
            teams = self._view["teams"] if self._view else {}
            if teams and shown in teams:
                group = {p for p in self._rows if teams.get(p) == teams[shown]}
            else:
                group = {shown}
        if group == self._highlight_pids:
            return
        for pid in self._highlight_pids | group:
            refs = self._rows.get(pid)
            if refs:
                color = BG_HOVER if pid in group else BG_ROW
                refs["row"].configure(bg=color)
                for child in refs["row"].winfo_children():
                    child.configure(bg=color)
                    for grandchild in child.winfo_children():
                        grandchild.configure(bg=color)
        self._highlight_pids = group

    def _update_detail(self):
        view = self._view
        if view is None:
            return
        pid = self._shown_pid()
        if (
            view["in_bg"]
            and not view["game_over"]
            and view["choices"]
            and view["choosing"]
        ):
            self._update_detail_choices(view)
            return
        if pid is None and view["game_over"] and view["hp_track"]:
            self._update_detail_recap(view)
            return
        teams = view["teams"]
        if pid is not None and teams and pid in teams:
            members = [p for p in view["order"] if teams.get(p) == teams[pid]]
            if len(members) >= 2:
                self._update_detail_team(view, members)
                return
        if pid is not None and pid == view["friendly"]:
            self._update_detail_self(view)
            return
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

        self.tooltip.hide()
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
        age = view["turn"] - snap.round_num if view["turn"] else 0
        if age >= 2:
            title += f" ({age} old)"
        tag = view["names"].get(pid, "")
        if tag:
            title += f"  ({tag})"
        if snap.tech_level:
            title += f"  T{snap.tech_level}"
        self.detail_title.configure(text=title)

        result_txt = {
            "win": f"you won (+{snap.result_dmg})",
            "loss": f"you lost (-{snap.result_dmg})",
            "tie": "tied",
        }.get(snap.result, "")
        meta = tk.Frame(self.detail, bg=BG)
        meta.pack(fill="x", padx=4, pady=(0, 2))
        plain = []
        if result_txt:
            plain.append(result_txt)
        plain.append(f"~{self._threat(snap.tech_level, snap.minions)} dmg")
        da, dh = self._buff_delta(snap.minions)
        if da > 0 or dh > 0:
            plain.append(f"+{da}/+{dh} buffs")
        tk.Label(meta, text=" · ".join(plain), bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 9)).pack(side="left", padx=(0, 8))
        hoverables = []
        if snap.hero_power:
            hoverables.append(("⚡ " + self.cards.name(snap.hero_power), snap.hero_power))
        for t in snap.trinkets:
            hoverables.append(("🎁 " + self.cards.name(t), t))
        if snap.buddy_dbf:
            bid = self.cards.card_by_dbf(snap.buddy_dbf)
            if bid:
                hoverables.append(("🤝 " + self.cards.name(bid), bid))
        for text, cid in hoverables:
            lbl = tk.Label(meta, text=text, bg=BG, fg=FG_DIM,
                           font=("Segoe UI", 9, "underline"))
            lbl.pack(side="left", padx=(0, 8))
            self.tooltip.attach(lbl, cid)

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
            self._minion_row(self.detail, m)

    # -------------------------------------------------------- hero pick panel

    def _update_detail_choices(self, view):
        key = ("choices", tuple(c[0] for c in view["choices"]))
        if key == self._detail_key:
            return
        self._detail_key = key
        self.tooltip.hide()
        for w in self.detail.winfo_children():
            w.destroy()
        self.detail_title.configure(text="Hero pick — your record with each option")

        for cid, name, power_dbf in view["choices"]:
            row = tk.Frame(self.detail, bg=BG_ROW, height=36)
            row.pack(fill="x", padx=4, pady=2)
            row.pack_propagate(False)

            c = tk.Canvas(row, width=64, height=30, bg=BG_ROW, highlightthickness=0)
            c.pack(side="left", padx=(4, 8), pady=3)
            img = self.art.get_tile_small(cid)
            if img is not None:
                c.create_image(64, 15, image=img, anchor="e")
                c.image = img

            career = self._career.get(base_hero(cid))
            base = base_hero(cid)
            # Base-card name, so skins ("Tavish Drizzle Pike") display as
            # the hero they are ("Tavish Stormpike").
            if self.cards.known(base):
                display = self.cards.name(base)
            else:
                display = name or self.cards.name(cid)
            tk.Label(row, text=display, bg=BG_ROW, fg=FG,
                     font=("Segoe UI", 10, "bold"), anchor="w").pack(side="left")
            if career and career["places"]:
                ps = career["places"]
                stat = f"{len(ps)} game(s) · avg #{sum(ps) / len(ps):.1f}"
                if 1 in ps:
                    stat += f" · {ps.count(1)}× first"
            else:
                stat = "never played"
            tk.Label(row, text=stat, bg=BG_ROW, fg=FG_DIM,
                     font=("Segoe UI", 9)).pack(side="right", padx=8)

            tip_cid = self.cards.card_by_dbf(power_dbf) if power_dbf else ""
            for w in (row, c):
                self.tooltip.attach(w, tip_cid or cid)

    # ---------------------------------------------------- self view and recap

    def _update_detail_self(self, view):
        """Our own live board (solo games; duos shows it in the team view)."""
        minions = view["own_board"]
        key = ("self", tuple((m.card_id, m.attack, m.health, m.golden) for m in minions))
        if key == self._detail_key:
            return
        self._detail_key = key
        self.tooltip.hide()
        for w in self.detail.winfo_children():
            w.destroy()
        self.detail_title.configure(text="Your board — live")

        trinkets, hero_power = view["own_extras"]
        meta = tk.Frame(self.detail, bg=BG)
        meta.pack(fill="x", padx=4, pady=(0, 2))
        plain = [f"~{self._threat(view['own_tier'], minions)} dmg"]
        da, dh = self._buff_delta(minions)
        if da > 0 or dh > 0:
            plain.append(f"+{da}/+{dh} buffs")
        tk.Label(meta, text=" · ".join(plain), bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 9)).pack(side="left", padx=(0, 8))
        hoverables = [("⚡ " + self.cards.name(hero_power), hero_power)] if hero_power else []
        hoverables += [("🎁 " + self.cards.name(t), t) for t in trinkets]
        for text, cid in hoverables:
            lbl = tk.Label(meta, text=text, bg=BG, fg=FG_DIM,
                           font=("Segoe UI", 9, "underline"))
            lbl.pack(side="left", padx=(0, 8))
            self.tooltip.attach(lbl, cid)

        if not minions:
            tk.Label(self.detail, text="(empty board)", bg=BG, fg=FG_DIM,
                     font=("Segoe UI", 9), anchor="w").pack(fill="x", padx=4)
        for m in minions:
            self._minion_row(self.detail, m)

    def _update_detail_recap(self, view):
        track = view["hp_track"]
        place = view["statuses"].get(view["friendly"], {}).get("place", 0)
        key = ("recap", len(track), place)
        if key == self._detail_key:
            return
        self._detail_key = key
        self.tooltip.hide()
        for w in self.detail.winfo_children():
            w.destroy()
        self.detail_title.configure(
            text=f"Recap — you placed #{place}" if place else "Recap"
        )

        w, h, pad = 330, 70, 10
        c = tk.Canvas(self.detail, width=w, height=h, bg=BG, highlightthickness=0)
        c.pack(padx=4, pady=(2, 4), anchor="w")
        hps = [hp for _, hp in track]
        top = max(max(hps), 1)
        pts = []
        for i, (_rnd, hp) in enumerate(track):
            x = pad + i * (w - 2 * pad) / max(len(track) - 1, 1)
            y = h - pad - (max(hp, 0) / top) * (h - 2 * pad)
            pts.append((x, y))
        for a, b in zip(pts, pts[1:]):
            c.create_line(*a, *b, fill=ACCENT, width=2)
        for (x, y), (rnd, hp) in zip(pts, track):
            c.create_oval(x - 2, y - 2, x + 2, y + 2, fill=FG, outline="")
        first_r, last_r = track[0][0], track[-1][0]
        c.create_text(pad, h - 2, text=f"r{first_r}", fill=FG_DIM,
                      font=("Segoe UI", 7), anchor="sw")
        c.create_text(w - pad, h - 2, text=f"r{last_r}", fill=FG_DIM,
                      font=("Segoe UI", 7), anchor="se")
        c.create_text(pad, 2, text=f"{top}hp", fill=FG_DIM,
                      font=("Segoe UI", 7), anchor="nw")

        placed = sorted(
            (st.get("place", 9), p) for p, st in view["statuses"].items()
        )
        for pl, p in placed:
            if not pl or pl > 8 or p not in view["heroes"]:
                continue
            hero = view["heroes"][p]
            name = hero.name or self.cards.name(hero.card_id)
            you = "  (you)" if p == view["friendly"] else ""
            tk.Label(
                self.detail, text=f"#{pl}  {name}{you}", bg=BG,
                fg=FG if p == view["friendly"] else FG_DIM,
                font=("Segoe UI", 9), anchor="w",
            ).pack(fill="x", padx=6)

    # ------------------------------------------------------- duos team detail

    def _update_detail_team(self, view, members):
        """Both boards of a duos team side by side."""
        cols = []
        for p in members:
            if p == view["friendly"]:
                cols.append((p, None, -1))  # live board, no history
                continue
            hist = view["history"].get(p, [])
            if self._detail_sel and self._detail_sel[0] == p and self._detail_sel[1] < len(hist):
                idx = self._detail_sel[1]
            else:
                idx = len(hist) - 1
            cols.append((p, hist[idx] if hist else None, idx))
        key = (
            "team",
            self.pinned_pid,
            tuple((p, idx, id(s), s.result if s else "") for p, s, idx in cols),
            len(view["own_board"]) if any(p == view["friendly"] for p, _, _ in cols) else -1,
        )
        if key == self._detail_key:
            return
        self._detail_key = key
        self.tooltip.hide()
        for w in self.detail.winfo_children():
            w.destroy()

        names = " & ".join(
            (view["heroes"][p].name or self.cards.name(view["heroes"][p].card_id))
            for p, _, _ in cols
            if p in view["heroes"]
        )
        pin_mark = " 📌" if self.pinned_pid in [p for p, _, _ in cols] else ""
        self.detail_title.configure(text=names + pin_mark)

        grid = tk.Frame(self.detail, bg=BG)
        grid.pack(fill="both", expand=True)
        grid.columnconfigure(0, weight=1, uniform="boards")
        grid.columnconfigure(1, weight=1, uniform="boards")
        for j, (p, snap, idx) in enumerate(cols):
            col = tk.Frame(grid, bg=BG)
            col.grid(row=0, column=j, sticky="new", padx=((0, 3) if j == 0 else (3, 0)))
            self._board_column(col, view, p, snap, idx)

    def _board_column(self, col, view, pid, snap, idx):
        hero = view["heroes"].get(pid)
        hero_name = (hero.name if hero else "") or (
            self.cards.name(hero.card_id) if hero else "?"
        )
        is_self = pid == view["friendly"]
        tk.Label(
            col, text=hero_name + (" (you)" if is_self else ""), bg=BG, fg=FG,
            font=("Segoe UI", 9, "bold"), anchor="w",
        ).pack(fill="x")

        if is_self:
            trinkets, hero_power = view["own_extras"]
            minions = view["own_board"]
            sub = "live board"
        elif snap is None:
            tk.Label(col, text="no board seen yet", bg=BG, fg=FG_DIM,
                     font=("Segoe UI", 8), anchor="w").pack(fill="x")
            return
        else:
            trinkets, hero_power = snap.trinkets, snap.hero_power
            minions = snap.minions
            age = view["turn"] - snap.round_num if view["turn"] else 0
            bits = [f"r{snap.round_num}" + (f" ({age} old)" if age >= 2 else "")]
            if snap.tech_level:
                bits.append(f"T{snap.tech_level}")
            bits.append(f"~{self._threat(snap.tech_level, minions)} dmg")
            da, dh = self._buff_delta(minions)
            if da > 0 or dh > 0:
                bits.append(f"+{da}/+{dh}")
            bits.append({
                "win": f"won (+{snap.result_dmg})",
                "loss": f"lost (-{snap.result_dmg})",
                "tie": "tied",
            }.get(snap.result, ""))
            sub = " · ".join(b for b in bits if b)
        tk.Label(col, text=sub, bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 8), anchor="w").pack(fill="x")

        extras = tk.Frame(col, bg=BG)
        extras.pack(fill="x")
        hoverables = []
        if hero_power:
            hoverables.append(("⚡ " + self.cards.name(hero_power), hero_power))
        for t in trinkets:
            hoverables.append(("🎁 " + self.cards.name(t), t))
        if not is_self and snap is not None and snap.buddy_dbf:
            bid = self.cards.card_by_dbf(snap.buddy_dbf)
            if bid:
                hoverables.append(("🤝 " + self.cards.name(bid), bid))
        for text, cid in hoverables:
            lbl = tk.Label(extras, text=text, bg=BG, fg=FG_DIM,
                           font=("Segoe UI", 8, "underline"), anchor="w")
            lbl.pack(fill="x")
            self.tooltip.attach(lbl, cid)

        if not is_self:
            hist = view["history"].get(pid, [])
            if len(hist) > 1:
                chips = tk.Frame(col, bg=BG)
                chips.pack(fill="x", pady=(1, 2))
                for i, s in enumerate(hist):
                    mark = {"win": "＋", "loss": "－", "tie": "＝"}.get(s.result, "")
                    lbl = tk.Label(
                        chips, text=f"r{s.round_num}{mark}",
                        bg=BG_HOVER if i == idx else BG_ROW,
                        fg=FG if i == idx else FG_DIM,
                        font=("Segoe UI", 7, "bold" if i == idx else "normal"),
                        padx=3, pady=0,
                    )
                    lbl.pack(side="left", padx=(0, 2))
                    lbl.bind("<Button-1>", lambda _e, p=pid, j=i: self._select_round(p, j))

        if not minions:
            tk.Label(col, text="(empty board)", bg=BG, fg=FG_DIM,
                     font=("Segoe UI", 8), anchor="w").pack(fill="x")
        for m in minions:
            self._minion_row(col, m, art_w=105)

    ART_W = 150  # right part of the tile shown; the left fade is clipped off

    def _minion_row(self, parent, m, art_w=None):
        aw = art_w or self.ART_W
        h = TILE_H + 2
        row = tk.Frame(parent, bg=BG_ROW, height=h)
        row.pack(fill="x", padx=(2 if art_w else 4), pady=1)
        row.pack_propagate(False)
        if m.golden:
            row.configure(
                highlightbackground=GOLD, highlightcolor=GOLD, highlightthickness=1
            )

        c = tk.Canvas(row, width=aw, height=h, bg=BG_ROW, highlightthickness=0)
        c.pack(side="right")
        tile = self.art.get_tile(m.card_id)
        if tile is not None:
            # Right-anchored: the tile's white left fade hangs past the
            # canvas edge and gets clipped.
            c.create_image(aw, h // 2, image=tile, anchor="e")
            c.image = tile  # keep a reference or tk garbage-collects it
            # Screen-door blend so the art edge doesn't cut hard.
            for width, stipple in ((26, "gray25"), (18, "gray50"), (9, "gray75")):
                c.create_rectangle(
                    0, 0, width, h, fill=BG_ROW, width=0, stipple=stipple
                )
        stats = f"{m.attack}/{m.health}"
        sw = 7 * len(stats) + 10
        c.create_rectangle(
            aw - sw - 2, h - 18, aw - 2, h - 3,
            fill="#101014", outline="#000",
        )
        c.create_text(
            aw - 2 - sw / 2, h - 10, text=stats,
            fill=GOLD if m.golden else "#ffffff", font=("Consolas", 9, "bold"),
        )
        tier = self.cards.tech_level(m.card_id)
        if tier:
            c.create_oval(aw - 16, 2, aw - 2, 16, fill="#1d3050", outline="#0a0a10")
            c.create_text(aw - 9, 9, text=str(tier), fill="#e8e6e3",
                          font=("Segoe UI", 7, "bold"))

        texts = tk.Frame(row, bg=BG_ROW)
        texts.pack(side="left", fill="both", expand=True, padx=(8, 0))
        name = self.cards.name(m.card_id, m.name)
        if m.golden:
            name += " ★"
        tk.Label(
            texts, text=name, bg=BG_ROW, fg=GOLD if m.golden else FG,
            font=("Segoe UI", 9, "bold"), anchor="w",
        ).pack(fill="x", pady=(5, 0))
        if m.keywords:
            tk.Label(
                texts, text=", ".join(m.keywords), bg=BG_ROW, fg=FG_DIM,
                font=("Segoe UI", 7), anchor="w",
            ).pack(fill="x")

        extra = ", ".join(m.keywords)
        if m.golden:
            extra = ("Golden · " + extra) if extra else "Golden"
        for w in (row, c, texts, *texts.winfo_children()):
            self.tooltip.attach(w, m.card_id, extra)


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
