"""Card art download + cache (HearthstoneJSON art CDN).

Two kinds of art:
- tiles:   256x59 deck-list bar crops   -> board rows
- renders: 256x388 full card images     -> hover tooltips (patchy for BG
           cards; callers fall back to card text when missing)

Downloads happen on a background thread and only write files; PhotoImages
are created lazily on the tk thread by get_tile/get_render. A .404 marker
file remembers cards the CDN doesn't have.
"""

from __future__ import annotations

import queue
import re
import threading
import urllib.request
from pathlib import Path

import tkinter as tk

CACHE_DIR = Path(__file__).with_name("images_cache")
TILE_URL = "https://art.hearthstonejson.com/v1/tiles/{}.png"
RENDER_URL = "https://art.hearthstonejson.com/v1/render/latest/enUS/256x/{}.png"
ORIG_URL = "https://art.hearthstonejson.com/v1/orig/{}.png"  # 512x512 art
BGS_URL = "https://art.hearthstonejson.com/v1/bgs/latest/enUS/256x/{}.png"

# Card-browser art served through the local server: kind -> (subdir, url).
# "render" shares the tooltip cache; "bgs" are the BG-styled renders.
ART_PROXY = {
    "render": ("renders", RENDER_URL),
    "bgs": ("bgs", BGS_URL),
}


def fetch_art(kind: str, card_id: str) -> bytes | None:
    """Synchronous cached fetch for the card-browser page. Downloads each
    image from the CDN at most once (a .404 marker remembers misses, cleared
    on game patches); afterwards it is served from images_cache."""
    entry = ART_PROXY.get(kind)
    aid = _art_id(card_id)
    if entry is None or not re.fullmatch(r"[A-Za-z0-9_]{1,64}", aid):
        return None
    subdir, url_tpl = entry
    d = CACHE_DIR / subdir
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{aid}.png"
    miss = d / f"{aid}.404"
    try:
        if path.exists():
            return path.read_bytes()
        if miss.exists():
            return None
        req = urllib.request.Request(
            url_tpl.format(aid),
            headers={"User-Agent": "hstracker/1.0 (+local BG tracker)"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        path.write_bytes(data)
        return data
    except Exception:
        try:
            miss.touch()
        except OSError:
            pass
        return None

# Tile display scale: 256x59 * 3/4 -> 192x44
TILE_ZOOM, TILE_SUB = 3, 4
TILE_W, TILE_H = 256 * TILE_ZOOM // TILE_SUB, 59 * TILE_ZOOM // TILE_SUB


def _art_id(card_id: str) -> str:
    return card_id.removesuffix("_G")  # golden variants share base art


def _crop_center(img: tk.PhotoImage, w: int, h: int) -> tk.PhotoImage:
    """Exact-size center crop (drops the white margins painted into art)."""
    iw, ih = img.width(), img.height()
    cw, ch = min(w, iw), min(h, ih)
    if cw == iw and ch == ih:
        return img
    x1, y1 = (iw - cw) // 2, (ih - ch) // 2
    out = tk.PhotoImage(width=cw, height=ch)
    out.tk.call(str(out), "copy", str(img), "-from", x1, y1, x1 + cw, y1 + ch)
    return out


class ArtStore:
    def __init__(self, on_new=None):
        self.on_new = on_new  # called (from worker thread) when a file lands
        self._photos: dict[tuple, tk.PhotoImage | None] = {}
        self._queued: set = set()
        self._q: queue.Queue = queue.Queue()
        (CACHE_DIR / "tiles").mkdir(parents=True, exist_ok=True)
        (CACHE_DIR / "renders").mkdir(parents=True, exist_ok=True)
        (CACHE_DIR / "orig").mkdir(parents=True, exist_ok=True)
        (CACHE_DIR / "bgs").mkdir(parents=True, exist_ok=True)
        threading.Thread(target=self._worker, daemon=True).start()

    # ------------------------------------------------------------ tk thread

    def get_tile(self, card_id: str) -> tk.PhotoImage | None:
        return self._get("tiles", TILE_URL, card_id, scale=(TILE_ZOOM, TILE_SUB))

    def get_tile_small(self, card_id: str) -> tk.PhotoImage | None:
        """Half-size tile (128x29) for roster rows."""
        return self._get("tiles", TILE_URL, card_id, scale=(1, 2), variant="s")

    def get_render(self, card_id: str) -> tk.PhotoImage | None:
        return self._get("renders", RENDER_URL, card_id, scale=None)

    def get_portrait(self, card_id: str) -> tk.PhotoImage | None:
        """512x512 original art at ~170px — oversized so the canvas crop cuts
        off the white margins painted into the source art."""
        return self._get("orig", ORIG_URL, card_id, scale=(1, 3), variant="p")

    def get_thumb(self, card_id: str) -> tk.PhotoImage | None:
        """Lobby board thumb: art scaled to ~73px, center-cropped to 48x60
        so the source's painted margins are gone."""
        return self._get("orig", ORIG_URL, card_id, scale=(1, 7), variant="t",
                         crop=(48, 60))

    def get_face(self, card_id: str) -> tk.PhotoImage | None:
        """Hero face for lobby rows: ~30px art cropped to 26x26."""
        return self._get("orig", ORIG_URL, card_id, scale=(1, 17), variant="f",
                         crop=(26, 26))

    def _get(self, kind, url_tpl, card_id, scale, variant="", crop=None):
        aid = _art_id(card_id)
        key = (kind + variant, aid)
        if key in self._photos:
            return self._photos[key]
        path = CACHE_DIR / kind / f"{aid}.png"
        if (CACHE_DIR / kind / f"{aid}.404").exists():
            self._photos[key] = None
            return None
        if path.exists():
            try:
                img = tk.PhotoImage(file=str(path))
                if scale:
                    img = img.zoom(scale[0]).subsample(scale[1])
                if crop:
                    img = _crop_center(img, *crop)
                self._photos[key] = img
            except tk.TclError:
                self._photos[key] = None
            return self._photos[key]
        if key not in self._queued:
            self._queued.add(key)
            self._q.put((kind, url_tpl, aid))
        return None  # caller shows placeholder; on_new fires when ready

    def clear_misses(self):
        """Forget remembered 404s (a new patch usually means the CDN gained
        art for cards it lacked). Cached art itself stays valid."""
        for kind in ("tiles", "renders", "orig", "bgs"):
            for f in (CACHE_DIR / kind).glob("*.404"):
                try:
                    f.unlink()
                except OSError:
                    pass
        self._photos = {k: v for k, v in self._photos.items() if v is not None}
        self._queued.clear()

    # -------------------------------------------------------- worker thread

    def _worker(self):
        while True:
            kind, url_tpl, aid = self._q.get()
            path = CACHE_DIR / kind / f"{aid}.png"
            miss = CACHE_DIR / kind / f"{aid}.404"
            try:
                req = urllib.request.Request(
                    url_tpl.format(aid),
                    headers={"User-Agent": "hstracker/1.0 (+local BG tracker)"},
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = resp.read()
                path.write_bytes(data)
            except Exception:
                try:
                    miss.touch()
                except OSError:
                    pass
            if self.on_new:
                self.on_new()
