"""BG Tracker — lightweight Hearthstone Battlegrounds opponent-board tracker.

"Boards are the interface" layout: the next opponent's boards render big at
the top (portrait tiles with stat bands), every other player in the lobby is
one line with their whole board as thumbnails below.

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
from images import ArtStore
from parser import BgGame

POLL_SECONDS = 1.0
CONFIG_FILE = Path(__file__).with_name("tracker_config.json")
HISTORY_FILE = Path(__file__).with_name("games_history.jsonl")

# ------------------------------------------------------------------ palette
BG = "#141413"        # window
BG_BAR = "#0f0f0e"    # status bar
BG_HERO = "#1b1613"   # next-opponent section (warm tint)
BG_ROW = "#1a1a19"    # lobby row
BG_OWN = "#181a1d"    # own-team lobby row (cool tint)
BG_ELIM = "#171716"
BG_CHIP = "#212120"
FG = "#e9e7e2"
FG2 = "#c9c7c1"
FG_DIM = "#8d8b83"
FAINT = "#6e6c66"
FAINT2 = "#5e5c55"
EDGE = "#2b2b27"
HAIR = "#2c2c28"
HAIR2 = "#262622"
ACCENT = "#d95926"    # next opponent
BLUE = "#3987e5"      # our team
GOLD = "#d8a441"      # golden minions
ATK_C = "#e8c15a"
HP_C = "#e0776a"
GREEN = "#4fb477"
TIER_BG = "#101c2c"
TIER_FG = "#cfe2f5"
BAND = "#0d0d0c"

UI = "Segoe UI"
MONO = "Consolas"

KW_ABBR = {
    "Taunt": "TAUNT",
    "Divine Shield": "DS",
    "Windfury": "WF",
    "Poisonous": "POIS",
    "Venomous": "VEN",
    "Reborn": "REB",
    "Stealth": "ST",
    "Deathrattle": "DR",
}

TRIBE_FG = {
    "Pirate": "#d8a441",
    "Elemental": "#8fb7e8",
    "Dragon": "#e0917a",
    "Mech": "#9fb6c4",
    "Beast": "#a8c48f",
    "Murloc": "#8fd0c4",
    "Demon": "#c48fb6",
    "Undead": "#b0a8d8",
    "Naga": "#8fc4bc",
    "Quilboar": "#c4a88f",
}

SKIN_RE = __import__("re").compile(r"_SKIN_.*$")
VERSION_RE = __import__("re").compile(r"BattleNet version: Product = ([\d.]+)")


def base_hero(card_id: str) -> str:
    """Skins are the same hero: BG20_HERO_202_SKIN_D -> BG20_HERO_202."""
    return SKIN_RE.sub("", card_id or "")


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
    out: dict = {}
    try:
        for line in HISTORY_FILE.read_text(encoding="utf-8").splitlines():
            try:
                g = json.loads(line)
            except json.JSONDecodeError:
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
        pos = self._fh.tell()
        size = self.current.stat().st_size
        if size < pos:
            self._fh.seek(0)
        fed = False
        with self.lock:
            for line in self._fh:
                if not line.endswith("\n"):
                    self._fh.seek(self._fh.tell() - len(line))
                    break
                self.game.feed_line(line.rstrip("\n"))
                fed = True
        if fed:
            self.on_change()

    @staticmethod
    def _last_game_offset(path: Path) -> int:
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
        self._pending: tuple | None = None

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
        win.configure(bg="#0b0b0a")
        inner = tk.Frame(win, bg="#0b0b0a", padx=6, pady=6)
        inner.pack()

        render = self.art.get_render(card_id)
        if render is not None:
            lbl = tk.Label(inner, image=render, bg="#0b0b0a", bd=0)
            lbl.image = render
            lbl.pack()
        else:
            name = self.cards.name(card_id)
            tk.Label(inner, text=name, bg="#0b0b0a", fg=FG,
                     font=(UI, 10, "bold"), anchor="w").pack(fill="x")
            sub = []
            tier = self.cards.tech_level(card_id)
            if tier:
                sub.append(f"Tier {tier}")
            races = self.cards.races(card_id)
            if races:
                sub.append("/".join(r.title() for r in races))
            if sub:
                tk.Label(inner, text=" · ".join(sub), bg="#0b0b0a", fg=FG_DIM,
                         font=(UI, 8), anchor="w").pack(fill="x")
            text = self.cards.text(card_id)
            if text:
                tk.Label(inner, text=text, bg="#0b0b0a", fg=FG,
                         font=(UI, 9), wraplength=230,
                         justify="left", anchor="w").pack(fill="x", pady=(3, 0))
        if extra:
            tk.Label(inner, text=extra, bg="#0b0b0a", fg=GOLD,
                     font=(UI, 8), wraplength=230,
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
        self.pinned_pid: int | None = None

        self._hero_key = None
        self._detail_sel: tuple[int, int] | None = None  # (pid, history index)
        self._lobby_key = None
        self._lobby_rows: dict[int, dict] = {}
        self._view = None
        self._last_click = 0.0
        self._prev_next_opp = 0
        self._recap_shown = False
        self._career = load_career()
        self.tailer: LogTailer | None = None
        self._patch_cache: tuple[Path | None, str] = (None, "")
        self._last_data = 0.0

        root.title("BG Tracker")
        root.configure(bg=BG)
        geom = self._load_geometry() or "760x860"
        # The board layout needs width; widen a remembered narrow window.
        try:
            if int(geom.split("x")[0]) < 720:
                geom = "760x" + geom.split("x", 1)[1]
        except (ValueError, IndexError):
            pass
        root.geometry(geom)
        root.minsize(600, 480)
        root.attributes("-topmost", True)
        set_app_identity(root)
        enable_dark_titlebar(root)

        self._build_shell()
        self._geom_save_job = None
        root.bind("<Configure>", self._on_configure)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.bind("<Escape>", lambda _e: self._clear_selection())
        root.bind("<Up>", lambda _e: self._nav_team(-1))
        root.bind("<Down>", lambda _e: self._nav_team(1))
        root.bind("<Left>", lambda _e: self._nav_round(-1))
        root.bind("<Right>", lambda _e: self._nav_round(1))

        logs_dir = find_logs_dir()
        if logs_dir is None:
            self._status_text = "Hearthstone Logs folder not found"
        else:
            self._status_text = "waiting for a game"
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

    # -------------------------------------------------------------- keyboard

    def _teams_in_order(self):
        view = self._view
        if not view:
            return []
        teams = view["teams"]
        seen, out = set(), []
        for pid in view["order"]:
            key = teams.get(pid, pid)
            if key not in seen:
                seen.add(key)
                out.append([p for p in view["order"] if teams.get(p, p) == key])
        return out

    def _nav_team(self, delta: int):
        groups = self._teams_in_order()
        if not groups:
            return
        shown = set(self._shown_team())
        idx = next((i for i, g in enumerate(groups) if set(g) & shown), -delta)
        group = groups[(idx + delta) % len(groups)]
        self._last_click = time.time()
        self.pinned_pid = group[0]
        self._detail_sel = None
        self.dirty.set()

    def _nav_round(self, delta: int):
        view = self._view
        if not view:
            return
        pids = [p for p in self._shown_team() if p in view["history"]]
        if not pids:
            return
        pid = pids[0]
        hist = view["history"].get(pid, [])
        if len(hist) < 2:
            return
        if self._detail_sel and self._detail_sel[0] == pid:
            idx = self._detail_sel[1]
        else:
            idx = len(hist) - 1
        self._select_round(pid, max(0, min(len(hist) - 1, idx + delta)))

    def _select_round(self, pid: int, idx: int):
        self._last_click = time.time()
        self._detail_sel = (pid, idx)
        self.dirty.set()

    def _pin(self, pid: int):
        self._last_click = time.time()
        self.pinned_pid = None if self.pinned_pid == pid else pid
        self._detail_sel = None
        self.dirty.set()

    def _clear_selection(self):
        self._last_click = time.time()
        self.pinned_pid = None
        self._detail_sel = None
        self.dirty.set()

    # ------------------------------------------------------ game persistence

    def _persist_game(self, view):
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

    # ------------------------------------------------------- derived numbers

    def _threat(self, tech_level: int, minions) -> int:
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

    def _player_minions(self, view, pid):
        if pid == view["friendly"]:
            return view["own_board"]
        snap = view["snapshots"].get(pid)
        return snap.minions if snap else None

    def _team_totals(self, view, pids):
        atk = hp = 0
        seen = False
        for p in pids:
            minions = self._player_minions(view, p)
            if minions:
                seen = True
                atk += sum(m.attack for m in minions)
                hp += sum(m.health for m in minions)
        return (atk, hp) if seen else None

    def _team_record(self, view, pids) -> str:
        if view["friendly"] in pids:
            return ""
        fights: dict[int, str] = {}
        for p in pids:
            for s in view["history"].get(p, []):
                if s.result:
                    fights[s.round_num] = s.result
        if not fights:
            return ""
        w = sum(1 for r in fights.values() if r == "win")
        l = sum(1 for r in fights.values() if r == "loss")
        t = sum(1 for r in fights.values() if r == "tie")
        return f"{w}W {l}L" + (f" {t}T" if t else "")

    # ------------------------------------------------------------------ shell

    def _build_shell(self):
        # Hero section (fixed) at top; lobby scrolls; status bar fixed bottom.
        self.hero = tk.Frame(self.root, bg=BG)
        self.hero.pack(fill="x", side="top")

        status = tk.Frame(self.root, bg=BG_BAR)
        status.pack(fill="x", side="bottom")
        tk.Frame(status, bg=HAIR2, height=1).pack(fill="x", side="top")
        inner = tk.Frame(status, bg=BG_BAR)
        inner.pack(fill="x", padx=14, pady=6)
        self.status_dot = tk.Frame(inner, bg=FAINT, width=7, height=7)
        self.status_dot.pack(side="left")
        self.status_lbl = tk.Label(inner, text="starting…", bg=BG_BAR, fg=FAINT,
                                   font=(MONO, 9), anchor="w")
        self.status_lbl.pack(side="left", padx=(8, 0))
        stats_btn = tk.Label(inner, text="stats", bg=BG_BAR, fg=BLUE,
                             font=(UI, 9), cursor="hand2")
        stats_btn.pack(side="right")
        stats_btn.bind("<Button-1>", lambda _e: self._open_stats())
        self.topmost_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            inner, text="on top", variable=self.topmost_var,
            command=lambda: self.root.attributes("-topmost", self.topmost_var.get()),
            bg=BG_BAR, fg=FG_DIM, selectcolor=BG_BAR, activebackground=BG_BAR,
            activeforeground=FG, highlightthickness=0, font=(UI, 9),
        ).pack(side="right", padx=(0, 12))
        recap_btn = tk.Label(inner, text="recap", bg=BG_BAR, fg=FG_DIM,
                             font=(UI, 9), cursor="hand2")
        recap_btn.pack(side="right", padx=(0, 12))
        recap_btn.bind("<Button-1>", lambda _e: self._clear_selection())

        holder = tk.Canvas(self.root, bg=BG, highlightthickness=0)
        scroll = tk.Scrollbar(self.root, orient="vertical", command=holder.yview, width=8)
        holder.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        holder.pack(fill="both", expand=True)
        self.lobby = tk.Frame(holder, bg=BG)
        win = holder.create_window((0, 0), window=self.lobby, anchor="nw")
        self.lobby.bind(
            "<Configure>", lambda _e: holder.configure(scrollregion=holder.bbox("all"))
        )
        holder.bind("<Configure>", lambda e: holder.itemconfigure(win, width=e.width))
        self._lobby_canvas = holder
        self.root.bind_all("<MouseWheel>", self._on_wheel)

    def _on_wheel(self, event):
        c = self._lobby_canvas
        _, _, _, content_h = c.bbox("all") or (0, 0, 0, 0)
        if content_h > c.winfo_height():
            c.yview_scroll(-1 if event.delta > 0 else 1, "units")

    def _poll(self):
        if self.art_dirty.is_set():
            self.art_dirty.clear()
            self._hero_key = None
            self._lobby_key = None
            self.dirty.set()
        if self.dirty.is_set():
            self.dirty.clear()
            self.refresh()
        self._tick_status()
        self.root.after(400, self._poll)

    def _tick_status(self):
        view = self._view
        if not view or not view["in_bg"] or not view["heroes"]:
            self.status_dot.configure(bg=FAINT)
            self.status_lbl.configure(text=self._status_text)
            return
        if view["game_over"]:
            place = view["statuses"].get(view["friendly"], {}).get("place", 0)
            self.status_dot.configure(bg=FG_DIM)
            self.status_lbl.configure(
                text=f"game over · you placed #{place}" if place else "game over")
            return
        age = max(0, int(time.time() - self._last_data))
        mode = "duos" if view["teams"] else "solo"
        txt = f"in game · {mode} · round {view['turn']} · updated {age}s ago"
        if view["anomaly"]:
            aid = self.cards.card_by_dbf(view["anomaly"])
            if aid:
                txt += f" · anomaly: {self.cards.name(aid)}"
        self.status_dot.configure(bg=GREEN)
        self.status_lbl.configure(text=txt)

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
        self._last_data = time.time()

        if view["game_over"] and view["in_bg"] and view["heroes"]:
            if not self._recap_shown:
                self._recap_shown = True
                self.pinned_pid = None
                self._persist_game(view)
        else:
            self._recap_shown = False

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

        # Auto-select the upcoming opponent when it changes.
        nxt = view["next_opp"]
        if (
            nxt
            and nxt != self._prev_next_opp
            and nxt in view["heroes"]
            and nxt != view["friendly"]
            and not (view["teammate"] and nxt == view["teammate"])
            and time.time() - self._last_click > 2
        ):
            self.pinned_pid = None  # follow the next opponent by default
            self._detail_sel = None
        if nxt:
            self._prev_next_opp = nxt

        self._sync_hero(view)
        self._sync_lobby(view)
        self._tick_status()

    # ------------------------------------------------------------ hero logic

    def _shown_team(self) -> list:
        """Pids whose boards render big at the top."""
        view = self._view
        if not view:
            return []
        teams = view["teams"]
        anchor = self.pinned_pid if self.pinned_pid is not None else view["next_opp"]
        if not anchor or anchor not in view["heroes"]:
            return []
        if teams and anchor in teams:
            return [p for p in view["order"] if teams.get(p) == teams[anchor]]
        return [anchor]

    def _fighter_data(self, view, pid):
        """(snap_or_None, minions, idx, hist) for a shown fighter."""
        if pid == view["friendly"]:
            return None, view["own_board"], -1, []
        hist = view["history"].get(pid, [])
        if self._detail_sel and self._detail_sel[0] == pid and self._detail_sel[1] < len(hist):
            idx = self._detail_sel[1]
        else:
            idx = len(hist) - 1
        snap = hist[idx] if hist else None
        return snap, (snap.minions if snap else []), idx, hist

    def _sync_hero(self, view):
        if not view["in_bg"] or not view["heroes"]:
            key = ("idle",)
            if key != self._hero_key:
                self._hero_key = key
                self._wipe_hero()
                tk.Label(self.hero, text="Waiting for a Battlegrounds game…",
                         bg=BG, fg=FG_DIM, font=(UI, 11), pady=30).pack()
            return
        if view["choosing"] and view["choices"] and not view["game_over"]:
            self._sync_hero_choices(view)
            return
        team = self._shown_team()
        if not team:
            if view["game_over"] and view["hp_track"]:
                self._sync_hero_recap(view)
                return
            key = ("none",)
            if key != self._hero_key:
                self._hero_key = key
                self._wipe_hero()
                tk.Label(self.hero, text="No opponent announced yet",
                         bg=BG, fg=FG_DIM, font=(UI, 10), pady=24).pack()
            return
        if view["game_over"] and self.pinned_pid is None and view["hp_track"]:
            self._sync_hero_recap(view)
            return

        parts = []
        for pid in team:
            snap, minions, idx, _h = self._fighter_data(view, pid)
            parts.append((pid, id(snap), idx, snap.result if snap else "",
                          len(minions) if minions else 0))
        key = ("team", tuple(parts), self.pinned_pid, view["next_opp"])
        if key == self._hero_key:
            return
        self._hero_key = key
        self.tooltip.hide()
        self._wipe_hero()

        is_next = view["next_opp"] in team and self.pinned_pid is None
        statuses = view["statuses"]
        dead_team = all(
            statuses.get(p, {}).get("hp", 1) <= 0 and statuses.get(p, {}).get("place", 0)
            for p in team
        )
        box = tk.Frame(self.hero, bg=BG_HERO if is_next else BG_ROW)
        box.pack(fill="x")
        tk.Frame(self.hero, bg="#3a2a20" if is_next else HAIR, height=1).pack(fill="x")

        head = tk.Frame(box, bg=box["bg"])
        head.pack(fill="x", padx=16, pady=(10, 8))
        label = ("👻 GHOST" if dead_team else "⚔ NEXT") if is_next else "VIEWING"
        tk.Label(head, text=label, bg=box["bg"],
                 fg=ACCENT if is_next else FG_DIM,
                 font=(UI, 11, "bold")).pack(side="left")
        names = " & ".join(
            (view["heroes"][p].name or self.cards.name(view["heroes"][p].card_id))
            for p in team if p in view["heroes"]
        )
        tk.Label(head, text="  " + names, bg=box["bg"], fg=FG,
                 font=(UI, 11, "bold")).pack(side="left")
        rec = self._team_record(view, team)
        if rec:
            tk.Label(head, text=rec, bg=box["bg"], fg=FG_DIM,
                     font=(MONO, 9)).pack(side="right")
        totals = self._team_totals(view, team)
        if totals:
            tk.Label(head, text=f"{totals[0]:,}/{totals[1]:,}  ", bg=box["bg"],
                     fg=FG, font=(MONO, 10, "bold")).pack(side="right")

        for i, pid in enumerate(team):
            if i:
                tk.Frame(box, bg="#2a211c" if is_next else HAIR, height=1).pack(
                    fill="x", padx=8)
            self._fighter_block(box, view, pid)

    def _wipe_hero(self):
        for w in self.hero.winfo_children():
            w.destroy()

    def _fighter_block(self, parent, view, pid):
        bgc = parent["bg"]
        snap, minions, idx, hist = self._fighter_data(view, pid)
        is_self = pid == view["friendly"]
        hero_ent = view["heroes"].get(pid)
        hero_name = (hero_ent.name if hero_ent else "") or (
            self.cards.name(hero_ent.card_id) if hero_ent else "?")

        block = tk.Frame(parent, bg=bgc)
        block.pack(fill="x", padx=16, pady=(6, 10))

        head = tk.Frame(block, bg=bgc)
        head.pack(fill="x", pady=(0, 6))
        face = tk.Canvas(head, width=24, height=24, bg=bgc, highlightthickness=1,
                         highlightbackground=EDGE)
        face.pack(side="left")
        if hero_ent is not None and hero_ent.card_id:
            img = self.art.get_face(hero_ent.card_id)
            if img is not None:
                face.create_image(12, 12, image=img)
                face.image = img
        tk.Label(head, text=hero_name + (" (you)" if is_self else ""), bg=bgc, fg=FG,
                 font=(UI, 10, "bold")).pack(side="left", padx=(8, 6))

        tribe, tcount = self._tribe_count(minions or [])
        if tribe:
            chip_fg = TRIBE_FG.get(tribe, FG_DIM)
            chip = tribe.upper() + (f" ×{tcount}" if tcount else "")
            tk.Label(head, text=chip, bg=BG_CHIP, fg=chip_fg,
                     font=(MONO, 8), padx=5, pady=1).pack(side="left", padx=(0, 6))

        meta = []
        st = view["statuses"].get(pid, {})
        ehp = st.get("hp", 0) + st.get("armor", 0)
        if ehp > 0:
            meta.append(f"{ehp}hp")
        tier = view["own_tier"] if is_self else (snap.tech_level if snap else 0)
        if tier:
            meta.append(f"T{tier}")
        if minions:
            meta.append(f"~{self._threat(tier, minions)} dmg")
        if snap:
            age = view["turn"] - snap.round_num if view["turn"] else 0
            meta.append(f"r{snap.round_num}" + (f" ({age} old)" if age >= 2 else ""))
            meta.append({
                "win": f"won +{snap.result_dmg}",
                "loss": f"lost -{snap.result_dmg}",
                "tie": "tied",
            }.get(snap.result, ""))
        elif is_self:
            meta.append("live")
        tk.Label(head, text=" · ".join(m for m in meta if m), bg=bgc, fg=FG_DIM,
                 font=(MONO, 8)).pack(side="left")

        if is_self:
            trinkets, hero_power = view["own_extras"]
            buddy = 0
        else:
            trinkets = snap.trinkets if snap else []
            hero_power = snap.hero_power if snap else ""
            buddy = snap.buddy_dbf if snap else 0
        extras = tk.Frame(head, bg=bgc)
        extras.pack(side="right")
        items = []
        if hero_power:
            items.append((self.cards.name(hero_power), hero_power))
        for t in trinkets:
            items.append((self.cards.name(t), t))
        if buddy:
            bid = self.cards.card_by_dbf(buddy)
            if bid:
                items.append((self.cards.name(bid), bid))
        for j, (text, cid) in enumerate(items[:3]):
            if len(text) > 18:
                text = text[:17] + "…"
            lbl = tk.Label(extras, text=(" · " if j else "") + text, bg=bgc,
                           fg="#9a9890", font=(MONO, 8), cursor="hand2")
            lbl.pack(side="left")
            self.tooltip.attach(lbl, cid)

        if len(hist) > 1:
            chips = tk.Frame(block, bg=bgc)
            chips.pack(fill="x", pady=(0, 6))
            for i, s in enumerate(hist):
                mark = {"win": "+", "loss": "-", "tie": "="}.get(s.result, "")
                sel = i == idx
                lbl = tk.Label(
                    chips, text=f"r{s.round_num}{mark}",
                    bg=FG2 if sel else BG_ROW, fg=BG if sel else FAINT2,
                    font=(MONO, 8, "bold" if sel else "normal"), padx=5, pady=0,
                )
                lbl.pack(side="left", padx=(0, 3))
                lbl.bind("<Button-1>", lambda _e, p=pid, j=i: self._select_round(p, j))

        board = tk.Frame(block, bg=bgc)
        board.pack(anchor="w")
        if not minions:
            tk.Label(board, text="no board seen yet", bg=bgc, fg=FAINT,
                     font=(UI, 9)).pack(anchor="w", pady=6)
        else:
            for m in minions:
                self._big_tile(board, m)

    def _tribe_count(self, minions):
        counts: dict[str, int] = {}
        for m in minions:
            for race in self.cards.races(m.card_id):
                if race != "ALL":
                    counts[race] = counts.get(race, 0) + 1
        if not counts:
            return "", 0
        from cards import RACE_LABELS

        race, n = max(counts.items(), key=lambda kv: kv[1])
        if n * 2 >= max(len(minions), 1):
            return RACE_LABELS.get(race, race.title()), n
        return "Mixed", 0

    TILE_W, TILE_H = 88, 108

    def _big_tile(self, parent, m):
        w, h = self.TILE_W, self.TILE_H
        cell = tk.Frame(parent, bg=parent["bg"])
        cell.pack(side="left", padx=(0, 5))
        c = tk.Canvas(cell, width=w, height=h, bg="#24241f", highlightthickness=2,
                      highlightbackground=GOLD if m.golden else EDGE)
        c.pack()
        img = self.art.get_portrait(m.card_id)
        if img is not None:
            c.create_image(w // 2, h // 2 - 6, image=img)
            c.image = img
        tier = self.cards.tech_level(m.card_id)
        if tier:
            c.create_rectangle(2, 2, 17, 17, fill=TIER_BG, outline="")
            c.create_text(9, 9, text=str(tier), fill=TIER_FG, font=(MONO, 8, "bold"))
        if m.golden:
            c.create_rectangle(w - 16, 2, w - 2, 17, fill=GOLD, outline="")
            c.create_text(w - 9, 9, text="★", fill="#0b0b0a", font=(MONO, 8, "bold"))
        if m.keywords:
            abbr = " · ".join(KW_ABBR.get(k, k[:2].upper()) for k in m.keywords)
            c.create_rectangle(0, h - 33, w, h - 20, fill="#0c0c0b", outline="")
            c.create_text(w // 2, h - 27, text=abbr, fill="#dfe6ef", font=(MONO, 7))
        c.create_rectangle(0, h - 20, w, h, fill=BAND, outline="")
        c.create_text(4, h - 10, text=str(m.attack), fill=ATK_C,
                      font=(MONO, 11, "bold"), anchor="w")
        c.create_text(w - 4, h - 10, text=str(m.health), fill=HP_C,
                      font=(MONO, 11, "bold"), anchor="e")

        name = self.cards.name(m.card_id, m.name)
        cap = name if len(name) <= 13 else name[:12] + "…"
        tk.Label(cell, text=cap, bg=parent["bg"], fg=FG_DIM,
                 font=(UI, 7), anchor="w").pack(fill="x")

        extra = ", ".join(m.keywords)
        if m.golden:
            extra = ("Golden · " + extra) if extra else "Golden"
        for wdg in (cell, c):
            self.tooltip.attach(wdg, m.card_id, extra)

    # -------------------------------------------------------- hero pick panel

    def _sync_hero_choices(self, view):
        key = ("choices", tuple(c[0] for c in view["choices"]))
        if key == self._hero_key:
            return
        self._hero_key = key
        self.tooltip.hide()
        self._wipe_hero()
        box = tk.Frame(self.hero, bg=BG_HERO)
        box.pack(fill="x")
        tk.Frame(self.hero, bg="#3a2a20", height=1).pack(fill="x")
        tk.Label(box, text="HERO PICK — your record with each option",
                 bg=BG_HERO, fg=ACCENT, font=(UI, 11, "bold"),
                 anchor="w").pack(fill="x", padx=16, pady=(10, 6))
        for cid, name, power_dbf in view["choices"]:
            row = tk.Frame(box, bg=BG_ROW, height=40)
            row.pack(fill="x", padx=16, pady=(0, 4))
            row.pack_propagate(False)
            c = tk.Canvas(row, width=32, height=32, bg=BG_ROW, highlightthickness=1,
                          highlightbackground=EDGE)
            c.pack(side="left", padx=(6, 10), pady=3)
            img = self.art.get_face(cid)
            if img is not None:
                c.create_image(16, 16, image=img)
                c.image = img
            base = base_hero(cid)
            if self.cards.known(base):
                display = self.cards.name(base)
            else:
                display = name or self.cards.name(cid)
            tk.Label(row, text=display, bg=BG_ROW, fg=FG,
                     font=(UI, 10, "bold")).pack(side="left")
            career = self._career.get(base)
            if career and career["places"]:
                ps = career["places"]
                stat = f"{len(ps)} game(s) · avg #{sum(ps) / len(ps):.1f}"
                if 1 in ps:
                    stat += f" · {ps.count(1)}× first"
            else:
                stat = "never played"
            tk.Label(row, text=stat, bg=BG_ROW, fg=FG_DIM,
                     font=(MONO, 9)).pack(side="right", padx=10)
            tip = self.cards.card_by_dbf(power_dbf) if power_dbf else ""
            for wdg in (row, c):
                self.tooltip.attach(wdg, tip or cid)
        tk.Frame(box, bg=BG_HERO, height=6).pack()

    # ------------------------------------------------------------------ recap

    def _sync_hero_recap(self, view):
        track = view["hp_track"]
        place = view["statuses"].get(view["friendly"], {}).get("place", 0)
        if not place and view["teammate"]:
            place = view["statuses"].get(view["teammate"], {}).get("place", 0)
        key = ("recap", len(track), place)
        if key == self._hero_key:
            return
        self._hero_key = key
        self.tooltip.hide()
        self._wipe_hero()
        box = tk.Frame(self.hero, bg=BG_ROW)
        box.pack(fill="x")
        tk.Frame(self.hero, bg=HAIR, height=1).pack(fill="x")
        tk.Label(box, text=f"RECAP — you placed #{place}" if place else "RECAP",
                 bg=BG_ROW, fg=FG, font=(UI, 11, "bold"),
                 anchor="w").pack(fill="x", padx=16, pady=(10, 4))

        w, h, pad = 700, 74, 10
        c = tk.Canvas(box, width=w, height=h, bg=BG_ROW, highlightthickness=0)
        c.pack(padx=16, pady=(0, 4), anchor="w")
        hps = [hp for _, hp in track]
        top = max(max(hps), 1)
        pts = []
        for i, (_rnd, hp) in enumerate(track):
            x = pad + i * (w - 2 * pad) / max(len(track) - 1, 1)
            y = h - pad - (max(hp, 0) / top) * (h - 2 * pad)
            pts.append((x, y))
        for a, b in zip(pts, pts[1:]):
            c.create_line(*a, *b, fill=ACCENT, width=2)
        for x, y in pts:
            c.create_oval(x - 2, y - 2, x + 2, y + 2, fill=FG, outline="")
        c.create_text(pad, h - 2, text=f"r{track[0][0]}", fill=FG_DIM,
                      font=(UI, 7), anchor="sw")
        c.create_text(w - pad, h - 2, text=f"r{track[-1][0]}", fill=FG_DIM,
                      font=(UI, 7), anchor="se")
        c.create_text(pad, 2, text=f"{top}hp", fill=FG_DIM, font=(UI, 7), anchor="nw")

        placed = sorted((st.get("place", 9), p) for p, st in view["statuses"].items())
        row = tk.Frame(box, bg=BG_ROW)
        row.pack(fill="x", padx=16, pady=(0, 10))
        shown = set()
        for pl, p in placed:
            if not pl or pl > 8 or p not in view["heroes"] or pl in shown and view["teams"]:
                continue
            hero = view["heroes"][p]
            nm = hero.name or self.cards.name(hero.card_id)
            you = " (you)" if p == view["friendly"] else ""
            tk.Label(row, text=f"#{pl} {nm}{you}   ", bg=BG_ROW,
                     fg=FG if p == view["friendly"] else FAINT,
                     font=(UI, 9)).pack(side="left")

    # ------------------------------------------------------------------ lobby

    def _sync_lobby(self, view):
        if not view["in_bg"] or not view["heroes"]:
            if self._lobby_key != ("idle",):
                self._lobby_key = ("idle",)
                for w in self.lobby.winfo_children():
                    w.destroy()
                self._lobby_rows = {}
            return
        shown = set(self._shown_team())
        statuses = view["statuses"]
        teams = view["teams"]

        def is_dead(p):
            st = statuses.get(p, {})
            return st.get("place", 0) > 0 and st.get("hp", 1) <= 0

        alive = [p for p in view["order"] if not is_dead(p) and p not in shown]
        dead_groups: dict = {}
        for p in view["order"]:
            if is_dead(p) and p not in shown:
                key = teams.get(p, p)
                dead_groups.setdefault(key, []).append(p)

        structure = (tuple(alive), tuple(shown), tuple(sorted(dead_groups)),
                     tuple(teams.get(p) for p in alive))
        if structure != self._lobby_key:
            self._lobby_key = structure
            self._build_lobby(view, alive, dead_groups)
        self._update_lobby(view, alive)

    def _build_lobby(self, view, alive, dead_groups):
        for w in self.lobby.winfo_children():
            w.destroy()
        self._lobby_rows = {}
        teams = view["teams"]

        head = tk.Frame(self.lobby, bg=BG)
        head.pack(fill="x", padx=16, pady=(12, 6))
        tk.Label(head, text="LOBBY BOARDS", bg=BG, fg=FG_DIM,
                 font=(UI, 9, "bold")).pack(side="left")
        tk.Frame(head, bg=HAIR, height=1).pack(side="left", fill="x", expand=True,
                                               padx=10, pady=8)
        self._lobby_head = tk.Label(head, text="", bg=BG, fg=FAINT, font=(MONO, 9))
        self._lobby_head.pack(side="right")

        for pid in alive:
            own_side = pid == view["friendly"] or (
                view["teammate"] and pid == view["teammate"])
            bgc = BG_OWN if own_side else BG_ROW
            outer = tk.Frame(self.lobby, bg=BG)
            outer.pack(fill="x", padx=16, pady=2)
            edge = tk.Frame(outer, bg=BLUE if own_side else HAIR, width=3)
            edge.pack(side="left", fill="y")
            row = tk.Frame(outer, bg=bgc)
            row.pack(side="left", fill="x", expand=True)

            face = tk.Canvas(row, width=26, height=26, bg=bgc, highlightthickness=1,
                             highlightbackground=EDGE)
            face.pack(side="left", padx=(8, 9), pady=8)
            hero = view["heroes"].get(pid)
            if hero is not None and hero.card_id:
                img = self.art.get_face(hero.card_id)
                if img is not None:
                    face.create_image(13, 13, image=img)
                    face.image = img

            names = tk.Frame(row, bg=bgc, width=132)
            names.pack(side="left", fill="y", pady=6)
            names.pack_propagate(False)
            nm = tk.Label(names, text="", bg=bgc, fg=FG, font=(UI, 9, "bold"),
                          anchor="w")
            nm.pack(fill="x")
            sub = tk.Label(names, text="", bg=bgc, fg=FG_DIM, font=(MONO, 8),
                           anchor="w")
            sub.pack(fill="x")

            thumbs = tk.Canvas(row, width=7 * 50 - 2, height=60, bg=bgc,
                               highlightthickness=0)
            thumbs.pack(side="left", pady=7)

            sigma = tk.Label(row, text="", bg=bgc, fg=FG2, font=(MONO, 9, "bold"))
            sigma.pack(side="right", padx=10)

            for wdg in (row, face, names, nm, sub, thumbs, sigma):
                wdg.bind("<Button-1>", lambda _e, p=pid: self._pin(p))
            self._lobby_rows[pid] = {
                "row": row, "nm": nm, "sub": sub, "thumbs": thumbs,
                "sigma": sigma, "snap_id": None, "cache": None,
            }

        if dead_groups:
            dhead = tk.Frame(self.lobby, bg=BG)
            dhead.pack(fill="x", padx=16, pady=(10, 2))
            tk.Label(dhead, text="ELIMINATED", bg=BG, fg="#4d4b46",
                     font=(UI, 8, "bold")).pack(side="left")
            tk.Frame(dhead, bg="#242420", height=1).pack(side="left", fill="x",
                                                         expand=True, padx=10, pady=6)
            entries = []
            for key, pids in dead_groups.items():
                place = min(
                    view["statuses"].get(p, {}).get("place", 9) for p in pids)
                entries.append((place, pids))
            for place, pids in sorted(entries):
                row = tk.Frame(self.lobby, bg=BG_ELIM)
                row.pack(fill="x", padx=16, pady=1)
                nms = " & ".join(
                    (view["heroes"][p].name or self.cards.name(view["heroes"][p].card_id))
                    for p in pids if p in view["heroes"])
                tk.Label(row, text=f"#{place}", bg=BG_ELIM, fg=FG_DIM,
                         font=(MONO, 9, "bold"), width=3).pack(side="left", padx=(8, 4),
                                                               pady=4)
                lbl = tk.Label(row, text=nms, bg=BG_ELIM, fg=FAINT, font=(UI, 9))
                lbl.pack(side="left")
                rec = self._team_record(view, pids)
                if rec:
                    tk.Label(row, text=rec, bg=BG_ELIM, fg="#4d4b46",
                             font=(MONO, 8)).pack(side="right", padx=10)
                for wdg in (row, lbl):
                    wdg.bind("<Button-1>", lambda _e, p=pids[0]: self._pin(p))
        tk.Frame(self.lobby, bg=BG, height=8).pack()

    def _update_lobby(self, view, alive):
        teams = view["teams"]
        alive_teams = {teams.get(p, p) for p in view["order"]
                       if not (view["statuses"].get(p, {}).get("place", 0) > 0
                               and view["statuses"].get(p, {}).get("hp", 1) <= 0)}
        unit = "teams" if teams else "players"
        self._lobby_head.configure(
            text=f"round {view['turn']} · {len(alive_teams)} {unit} left")

        for pid, refs in self._lobby_rows.items():
            hero = view["heroes"].get(pid)
            if hero is None:
                continue
            is_self = pid == view["friendly"]
            is_mate = view["teammate"] and pid == view["teammate"]
            name = hero.name or self.cards.name(hero.card_id)
            st = view["statuses"].get(pid, {})
            ehp = st.get("hp", 0) + st.get("armor", 0)
            snap = view["snapshots"].get(pid)
            if is_self:
                sub = f"you · {ehp}hp" if ehp > 0 else "you"
            elif is_mate:
                sub = f"teammate · {ehp}hp" if ehp > 0 else "teammate"
            else:
                bits = [f"{ehp}hp"] if ehp > 0 else []
                if snap and snap.tech_level:
                    bits.append(f"T{snap.tech_level}")
                if snap:
                    age = view["turn"] - snap.round_num if view["turn"] else 0
                    if age >= 2:
                        bits.append(f"r{snap.round_num}")
                sub = " · ".join(bits) if bits else "not seen yet"
            minions = self._player_minions(view, pid)
            sigma = f"{sum(m.attack for m in minions):,}/{sum(m.health for m in minions):,}" \
                if minions else "—"

            cache = (name, sub, sigma,
                     BLUE if (is_self or is_mate) else FG_DIM)
            if refs["cache"] != cache:
                refs["cache"] = cache
                refs["nm"].configure(text=name)
                refs["sub"].configure(text=sub, fg=cache[3])
                refs["sigma"].configure(text=sigma)

            snap_key = ("live", len(minions or [])) if is_self else id(snap)
            if refs["snap_id"] != snap_key:
                refs["snap_id"] = snap_key
                self._draw_thumbs(refs["thumbs"], minions)

    THUMB_W, THUMB_H = 48, 60

    def _draw_thumbs(self, c: tk.Canvas, minions):
        tw, th = self.THUMB_W, self.THUMB_H
        step = tw + 2
        c.delete("all")
        c.images = []
        for i in range(7):
            x = i * step
            if minions and i < len(minions):
                m = minions[i]
                img = self.art.get_thumb(m.card_id)
                if img is not None:
                    c.create_image(x + tw // 2, th // 2, image=img)
                    c.images.append(img)
                else:
                    c.create_rectangle(x + 1, 1, x + tw - 1, th - 1,
                                       fill="#24241f", outline="")
                c.create_rectangle(
                    x + 1, 1, x + tw - 1, th - 1,
                    outline=GOLD if m.golden else EDGE,
                    width=2 if m.golden else 1,
                )
            else:
                c.create_rectangle(x + 1, 1, x + tw - 1, th - 1, outline="#2f2f2b",
                                   dash=(2, 2))


def main():
    import sys

    root = tk.Tk()
    TrackerApp(root)
    if len(sys.argv) > 1:
        root.geometry(sys.argv[1])
    root.mainloop()


if __name__ == "__main__":
    main()
