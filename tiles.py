"""
Slippy-map raster tile layer for a pyqtgraph ViewBox.

Tiles are published in Web Mercator, so the plot works in "Mercator degrees":
x is plain longitude, y is the Gudermannian of latitude scaled to the same
range. In that space a tile is an axis-aligned square and the aspect ratio is
a plain 1:1, which is also what makes shapes come out conformal.

Tiles are cached on disk and in memory, fetches are debounced and capped, and
every request carries a real User-Agent, because tile servers are donated
infrastructure and the usage policies ask for exactly that.
"""
import os
import math
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QObject, Signal, QTimer
from PySide6.QtGui import QImage

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "tiles")
USER_AGENT = "rtlsdr-spectrum-explorer/1.0 (hobby ADS-B viewer)"
TILE_PX = 256
MAX_TILES_PER_VIEW = 48


class TileSource:
    def __init__(self, key, label, url, attribution, max_zoom=18, allow_dark=True):
        self.key = key
        self.label = label
        self.url = url
        self.attribution = attribution
        self.max_zoom = max_zoom
        self.allow_dark = allow_dark          # inverting imagery looks wrong


# Only key-free providers. CARTO's basemaps now stamp "API KEY REQUIRED"
# across every tile, so they are deliberately not offered here.
SOURCES = [
    TileSource("osm", "OpenStreetMap",
               "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
               "© OpenStreetMap contributors", 18),
    TileSource("esri_imagery", "Satellite (Esri)",
               "https://server.arcgisonline.com/ArcGIS/rest/services/"
               "World_Imagery/MapServer/tile/{z}/{y}/{x}",
               "Imagery © Esri, Maxar, Earthstar Geographics", 18, False),
    TileSource("esri_topo", "Topographic (Esri)",
               "https://server.arcgisonline.com/ArcGIS/rest/services/"
               "World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
               "© Esri, contributors", 18),
    TileSource("opentopo", "OpenTopoMap",
               "https://a.tile.opentopomap.org/{z}/{x}/{y}.png",
               "© OpenStreetMap contributors, SRTM · OpenTopoMap (CC-BY-SA)",
               16),
]
SOURCES_BY_KEY = {s.key: s for s in SOURCES}


def invert_lightness(arr):
    """
    Turn a light basemap dark while keeping hues: invert luminance and rescale
    each channel by the same factor. A plain 255-x inversion would flip the
    colours too, turning greenery magenta.
    """
    rgb = arr[:, :, :3].astype(np.float32)
    lum = rgb @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    new = 255.0 - lum
    safe = np.maximum(lum, 1.0)
    out = rgb * (new / safe)[..., None]
    dark = lum <= 1.0
    if dark.any():
        out[dark] = new[dark][..., None]
    # pull a little saturation out so labels stay readable on the overlay
    grey = (out @ np.array([0.299, 0.587, 0.114], dtype=np.float32))[..., None]
    out = out * 0.75 + grey * 0.25
    res = arr.copy()
    res[:, :, :3] = np.clip(out, 0, 255).astype(np.uint8)
    return res


# ------------------------------------------------------------ projection ---
MAX_MERC_LAT = 85.05112878


def merc_y(lat):
    lat = max(-MAX_MERC_LAT, min(MAX_MERC_LAT, lat))
    return math.degrees(math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)))


def inv_merc_y(y):
    return math.degrees(2 * math.atan(math.exp(math.radians(y))) - math.pi / 2)


def tile_bounds(z, xt, yt):
    """(x0, y0, x1, y1) of a tile in Mercator degrees, y increasing upward."""
    span = 360.0 / (2 ** z)
    x0 = -180.0 + xt * span
    y1 = 180.0 - yt * span
    return x0, y1 - span, x0 + span, y1


def tiles_for_view(x0, x1, y0, y1, z):
    n = 2 ** z
    span = 360.0 / n
    xa = int(math.floor((x0 + 180.0) / span))
    xb = int(math.floor((x1 + 180.0) / span))
    ya = int(math.floor((180.0 - y1) / span))
    yb = int(math.floor((180.0 - y0) / span))
    out = []
    for xt in range(xa, xb + 1):
        for yt in range(ya, yb + 1):
            if 0 <= yt < n:
                out.append((z, xt % n, yt))
    return out


def zoom_for(view_width_deg, widget_px, max_zoom):
    if view_width_deg <= 0 or widget_px <= 0:
        return 3
    z = math.log2(widget_px * 360.0 / (TILE_PX * view_width_deg))
    return max(0, min(max_zoom, int(round(z))))


def _decode(data):
    img = QImage()
    if not img.loadFromData(data):
        return None
    img = img.convertToFormat(QImage.Format_RGBA8888)
    w, h, bpl = img.width(), img.height(), img.bytesPerLine()
    buf = np.frombuffer(img.constBits(), dtype=np.uint8, count=bpl * h)
    arr = buf.reshape(h, bpl)[:, : w * 4].reshape(h, w, 4)
    return np.ascontiguousarray(arr[::-1])          # plot y is up, image y is down


# ----------------------------------------------------------------- layer ---
class TileLayer(QObject):
    """Fetches and draws basemap tiles beneath everything else in a ViewBox."""

    tileReady = Signal(object, object)               # key, RGBA array
    statusChanged = Signal(str)

    def __init__(self, plot, source_key="osm", parent=None):
        super().__init__(parent)
        self.plot = plot
        self.vb = plot.getViewBox()
        self.source = SOURCES_BY_KEY[source_key]
        self.enabled = True
        self.dark = True

        self._items = {}                             # key -> ImageItem
        self._mem = {}                               # key -> array
        self._inflight = set()
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=6)
        self._failed = 0

        self.tileReady.connect(self._place)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(220)
        self._debounce.timeout.connect(self.refresh)
        self.vb.sigRangeChanged.connect(lambda *_: self._debounce.start())

    # ------------------------------------------------------------------
    def set_source(self, key):
        if key not in SOURCES_BY_KEY or key == self.source.key:
            return
        self.source = SOURCES_BY_KEY[key]
        self.clear()
        self.refresh()

    def set_dark(self, on):
        on = bool(on)
        if on == self.dark:
            return
        self.dark = on
        self.clear()            # cached arrays carry the old filter
        self.refresh()

    def set_enabled(self, on):
        self.enabled = bool(on)
        if not self.enabled:
            self.clear()
        else:
            self.refresh()

    def clear(self):
        for it in self._items.values():
            self.vb.removeItem(it)
        self._items.clear()
        self._mem.clear()

    # ------------------------------------------------------------------
    def _cache_path(self, z, x, y):
        return os.path.join(CACHE_DIR, self.source.key, str(z), str(x), f"{y}.png")

    def _fetch(self, key):
        z, x, y = key
        path = self._cache_path(z, x, y)
        data = None
        if os.path.isfile(path):
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError:
                data = None
        if data is None:
            url = self.source.url.format(z=z, x=x, y=y)
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=12) as r:
                    data = r.read()
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as fh:
                    fh.write(data)
            except Exception:
                with self._lock:
                    self._inflight.discard(key)
                self._failed += 1
                if self._failed in (1, 10):
                    self.statusChanged.emit(
                        "map tiles unavailable (offline?) - showing outlines only")
                return
        arr = _decode(data)
        if arr is not None and self.dark and self.source.allow_dark:
            arr = invert_lightness(arr)
        with self._lock:
            self._inflight.discard(key)
        if arr is not None:
            self._failed = 0
            self.tileReady.emit(key, arr)

    def _place(self, key, arr):
        if not self.enabled or key in self._items:
            return
        z, x, y = key
        self._mem[key] = arr
        item = pg.ImageItem(arr, axisOrder="row-major")
        x0, y0, x1, y1 = tile_bounds(z, x, y)
        item.setRect(pg.QtCore.QRectF(x0, y0, x1 - x0, y1 - y0))
        item.setZValue(-1000 + z)                    # always under the overlays
        self.vb.addItem(item, ignoreBounds=True)
        self._items[key] = item

    # ------------------------------------------------------------------
    def refresh(self):
        if not self.enabled:
            return
        (x0, x1), (y0, y1) = self.vb.viewRange()
        px = max(self.plot.width(), 200)
        z = zoom_for(x1 - x0, px, self.source.max_zoom)
        want = tiles_for_view(x0, x1, y0, y1, z)
        if len(want) > MAX_TILES_PER_VIEW:
            z = max(0, z - 1)
            want = tiles_for_view(x0, x1, y0, y1, z)
        if len(want) > MAX_TILES_PER_VIEW:
            want = want[:MAX_TILES_PER_VIEW]
        wanted = set(want)

        # drop tiles from other zoom levels once this level has arrived
        for key in list(self._items):
            if key[0] != z:
                self.vb.removeItem(self._items.pop(key))

        for key in want:
            if key in self._items:
                continue
            arr = self._mem.get(key)
            if arr is not None:
                self._place(key, arr)
                continue
            with self._lock:
                if key in self._inflight:
                    continue
                self._inflight.add(key)
            self._pool.submit(self._fetch, key)

        if len(self._mem) > 600:                     # bound the memory cache
            for k in list(self._mem)[:300]:
                if k not in wanted:
                    self._mem.pop(k, None)

    def shutdown(self):
        self._pool.shutdown(wait=False)
