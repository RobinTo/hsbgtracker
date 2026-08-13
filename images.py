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
import threading
import urllib.request
from pathlib import Path

import tkinter as tk

CACHE_DIR = Path(__file__).with_name("images_cache")
TILE_URL = "https://art.hearthstonejson.com/v1/tiles/{}.png"
RENDER_URL = "https://art.hearthstonejson.com/v1/render/latest/enUS/256x/{}.png"
ORIG_URL = "https://art.hearthstonejson.com/v1/orig/{}.png"  # 512x512 art

# Tile display scale: 256x59 * 3/4 -> 192x44
TILE_ZOOM, TILE_SUB = 3, 4
TILE_W, TILE_H = 256 * TILE_ZOOM // TILE_SUB, 59 * TILE_ZOOM // TILE_SUB


def _art_id(card_id: str) -> str:
    return card_id.removesuffix("_G")  # golden variants share base art


class ArtStore:
    def __init__(self, on_new=None):
        self.on_new = on_new  # called (from worker thread) when a file lands
        self._photos: dict[tuple, tk.PhotoImage | None] = {}
        self._queued: set = set()
        self._q: queue.Queue = queue.Queue()
        (CACHE_DIR / "tiles").mkdir(parents=True, exist_ok=True)
        (CACHE_DIR / "renders").mkdir(parents=True, exist_ok=True)
        (CACHE_DIR / "orig").mkdir(parents=True, exist_ok=True)
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
        """512x512 original art at 128x128 — callers crop via canvas clipping."""
        return self._get("orig", ORIG_URL, card_id, scale=(1, 4), variant="p")

    def get_thumb(self, card_id: str) -> tk.PhotoImage | None:
        """512x512 original art at ~51x51 for lobby board strips."""
        return self._get("orig", ORIG_URL, card_id, scale=(1, 10), variant="t")

    def get_face(self, card_id: str) -> tk.PhotoImage | None:
        """~26x26 hero face for lobby rows."""
        return self._get("orig", ORIG_URL, card_id, scale=(1, 19), variant="f")

    def _get(self, kind, url_tpl, card_id, scale, variant=""):
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
                self._photos[key] = img
            except tk.TclError:
                self._photos[key] = None
            return self._photos[key]
        if key not in self._queued:
            self._queued.add(key)
            self._q.put((kind, url_tpl, aid))
        return None  # caller shows placeholder; on_new fires when ready

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
