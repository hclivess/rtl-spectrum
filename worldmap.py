"""
Aircraft map view.

Draws real slippy-map basemap tiles with aircraft overlaid. The plot works in
Web Mercator degrees (see tiles.py) so tiles are axis-aligned squares and the
aspect ratio is a plain 1:1, which keeps shapes conformal. Latitude ticks are
converted back for display, so the axis still reads in real degrees.

If tiles cannot be fetched -- no network, blocked, or turned off -- the view
falls back to bundled vector country outlines and stays usable offline.
"""
import os
import json
import math

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QPainterPath, QTransform

import theme
from tiles import TileLayer, SOURCES, merc_y, inv_merc_y, MAX_MERC_LAT

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
KM_PER_DEG_LAT = 111.32


# ------------------------------------------------------------- topojson ----
def _decode_arcs(topo):
    sx, sy = topo["transform"]["scale"]
    tx, ty = topo["transform"]["translate"]
    arcs = []
    for arc in topo["arcs"]:
        x = y = 0
        pts = np.empty((len(arc), 2), dtype=np.float64)
        for i, (dx, dy) in enumerate(arc):
            x += dx
            y += dy
            pts[i, 0] = x * sx + tx
            pts[i, 1] = y * sy + ty
        arcs.append(pts)
    return arcs


def _ring(arcs, indices):
    parts = []
    for idx in indices:
        a = arcs[~idx][::-1] if idx < 0 else arcs[idx]
        parts.append(a[1:] if parts else a)
    return np.vstack(parts) if parts else np.empty((0, 2))


def load_borders(name="countries-110m.json", obj="countries"):
    """(x, y) in Mercator degrees with NaN separators, for one curve item."""
    path = os.path.join(DATA_DIR, name)
    if not os.path.isfile(path):
        return np.array([]), np.array([])
    with open(path, "r", encoding="utf-8") as fh:
        topo = json.load(fh)
    arcs = _decode_arcs(topo)
    chunks = []
    for geom in topo["objects"][obj]["geometries"]:
        gt = geom.get("type")
        polys = [geom["arcs"]] if gt == "Polygon" else (
            geom["arcs"] if gt == "MultiPolygon" else [])
        for poly in polys:
            for ring in poly:
                pts = _ring(arcs, ring)
                if len(pts) > 1:
                    chunks.append(pts)
                    chunks.append(np.array([[np.nan, np.nan]]))
    if not chunks:
        return np.array([]), np.array([])
    allpts = np.vstack(chunks)
    lat = np.clip(allpts[:, 1], -MAX_MERC_LAT, MAX_MERC_LAT)
    with np.errstate(invalid="ignore"):
        y = np.degrees(np.log(np.tan(np.pi/4 + np.radians(lat)/2)))
    y[np.isnan(allpts[:, 1])] = np.nan
    return allpts[:, 0], y


# ------------------------------------------------------------- geodesy -----
def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1)*math.sin(p2) - math.sin(p1)*math.cos(p2)*math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def radio_horizon_km(alt_ft, rx_height_m=10.0):
    """
    Line-of-sight range to an aircraft at `alt_ft`. 1090 MHz does not bend
    round the earth, so a position much beyond this is not a distant
    aircraft -- it is a bad CPR solve.
    """
    h_ac = max(alt_ft or 0, 0) * 0.3048
    return 3.57 * (math.sqrt(max(rx_height_m, 0.0)) + math.sqrt(h_ac))


class LatAxis(pg.AxisItem):
    """Ticks placed at round latitudes, positioned in Mercator space."""

    STEPS = (30, 20, 10, 5, 2, 1, 0.5, 0.2, 0.1, 0.05, 0.02, 0.01)

    def tickValues(self, minVal, maxVal, size):
        lo, hi = sorted((inv_merc_y(minVal), inv_merc_y(maxVal)))
        span = hi - lo
        step = self.STEPS[-1]
        for s in self.STEPS:
            if span / s >= 3:
                step = s
                break
        vals = []
        v = math.floor(lo / step) * step
        while v <= hi + step:
            if lo <= v <= hi:
                vals.append(merc_y(v))
            v += step
        return [(step, vals)]

    def tickStrings(self, values, scale, spacing):
        return [f"{inv_merc_y(v):.2f}" for v in values]


# ------------------------------------------------------------------ map ----
class MapWidget(pg.PlotWidget):
    aircraftClicked = Signal(str)
    followChanged = Signal(bool)
    status = Signal(str)

    RINGS_KM = (100, 200, 300, 400)
    MIN_SPAN_KM = 8.0
    MAX_SPAN_DEG = 170.0

    def __init__(self, parent=None):
        super().__init__(parent, axisItems={"left": LatAxis(orientation="left")})
        self.setBackground(theme.PANEL)
        self.showGrid(x=True, y=True, alpha=0.10)
        self.setLabel("bottom", "Longitude", units="deg")
        self.setLabel("left", "Latitude", units="deg")
        self.setMenuEnabled(False)

        self._rx = None
        self._labels = []
        self._index = {}
        self._follow = True

        bx, by = load_borders()
        self.borders = pg.PlotCurveItem(
            bx, by, pen=pg.mkPen("#33445a", width=1), connect="finite")
        self.addItem(self.borders, ignoreBounds=True)
        self.has_borders = len(bx) > 0

        self.ring_item = pg.PlotCurveItem(
            pen=pg.mkPen("#5b6b7d", width=1, style=Qt.DotLine), connect="finite")
        self.addItem(self.ring_item, ignoreBounds=True)

        self.rx_item = pg.ScatterPlotItem(
            size=15, symbol="+", pen=pg.mkPen(theme.WARN, width=2), brush=None)
        self.addItem(self.rx_item, ignoreBounds=True)

        self.ac_item = pg.ScatterPlotItem(size=14, pen=pg.mkPen("#05080c", width=1))
        self.ac_item.sigClicked.connect(self._on_click)
        self.addItem(self.ac_item, ignoreBounds=True)

        self.setAspectLocked(True, ratio=1.0)        # Mercator: square tiles
        vb = self.getViewBox()
        vb.setLimits(xMin=-180, xMax=180, yMin=-180, yMax=180,
                     minYRange=0.02, maxYRange=self.MAX_SPAN_DEG)
        vb.sigRangeChangedManually.connect(self._on_manual_range)
        vb.setRange(xRange=(4, 28), yRange=(merc_y(45), merc_y(56)), padding=0)

        self.tiles = TileLayer(self, "osm", parent=self)
        self.tiles.statusChanged.connect(self.status)
        self.borders.setVisible(False)               # tiles draw the coastlines
        self.tiles.refresh()

    # ------------------------------------------------------------ tiles --
    def set_tiles(self, on):
        self.tiles.set_enabled(on)
        self.borders.setVisible(not on)

    def set_tile_source(self, key):
        self.tiles.set_source(key)

    def set_dark_map(self, on):
        self.tiles.set_dark(on)

    def attribution(self):
        return self.tiles.source.attribution if self.tiles.enabled else \
            "Natural Earth (bundled outlines)"

    # --------------------------------------------------------- receiver --
    def set_receiver(self, lat, lon):
        self._rx = (lat, lon) if lat is not None else None
        if self._rx is None:
            self.rx_item.setData([], [])
            self.ring_item.setData([], [])
            return
        self.rx_item.setData([lon], [merc_y(lat)])
        xs, ys = [], []
        t = np.linspace(0, 2 * np.pi, 181)
        for km in self.RINGS_KM:
            dlat = km / KM_PER_DEG_LAT
            dlon = km / (KM_PER_DEG_LAT * max(math.cos(math.radians(lat)), 0.2))
            ring_lat = lat + dlat * np.sin(t)
            xs.append(lon + dlon * np.cos(t))
            ys.append(np.array([merc_y(v) for v in ring_lat]))
            xs.append(np.array([np.nan]))
            ys.append(np.array([np.nan]))
        self.ring_item.setData(np.concatenate(xs), np.concatenate(ys))

    # --------------------------------------------------------- aircraft --
    def set_aircraft(self, aircraft):
        for t in self._labels:
            self.removeItem(t)
        self._labels.clear()
        self._index.clear()

        spots, lats, lons = [], [], []
        for a in aircraft:
            if a.lat is None or a.lon is None:
                continue
            self._index[a.icao] = a
            lats.append(a.lat)
            lons.append(a.lon)
            frac = max(0.0, min(1.0, (a.altitude or 0) / 42000.0))
            col = pg.mkBrush(int(250 - 180*frac), int(120 + 80*frac), int(60 + 180*frac))
            spots.append({"pos": (a.lon, merc_y(a.lat)), "brush": col,
                          "data": a.icao, "symbol": self._arrow(a.track), "size": 15})
            label = a.callsign or a.icao
            if a.altitude:
                label += f"\n{a.altitude:,} ft"
            t = pg.TextItem(label, color=theme.TEXT, anchor=(0.5, 1.3),
                            fill=pg.mkBrush(5, 8, 12, 170))
            t.setFont(QFont("Consolas", 8))
            t.setPos(a.lon, merc_y(a.lat))
            self.addItem(t, ignoreBounds=True)
            self._labels.append(t)

        self.ac_item.setData(spots)
        if self._follow and lats:
            if self._rx:
                lats.append(self._rx[0])
                lons.append(self._rx[1])
            self._fit(lats, lons)

    @staticmethod
    def _arrow(track):
        p = QPainterPath()
        p.moveTo(0, -1.0)
        p.lineTo(0.62, 0.85)
        p.lineTo(0, 0.4)
        p.lineTo(-0.62, 0.85)
        p.closeSubpath()
        if track is not None:
            p = QTransform().rotate(float(track)).map(p)
        return p

    def _on_click(self, _item, points):
        if points:
            self.aircraftClicked.emit(points[0].data())

    def info_for(self, icao):
        a = self._index.get(icao)
        if a is None or a.lat is None or self._rx is None:
            return None
        return (haversine_km(self._rx[0], self._rx[1], a.lat, a.lon),
                bearing_deg(self._rx[0], self._rx[1], a.lat, a.lon))

    # ------------------------------------------------------------- view --
    def set_follow(self, on):
        self._follow = bool(on)
        if self._follow:
            self.fit_now()

    def _on_manual_range(self, *_):
        if self._follow:
            self._follow = False
            self.followChanged.emit(False)

    def fit_now(self):
        lats = [a.lat for a in self._index.values() if a.lat is not None]
        lons = [a.lon for a in self._index.values() if a.lon is not None]
        if self._rx:
            lats.append(self._rx[0])
            lons.append(self._rx[1])
        if lats:
            self._fit(lats, lons)

    def _fit(self, lats, lons):
        ys = [merc_y(v) for v in lats]
        cy = (min(ys) + max(ys)) / 2.0
        cx = (min(lons) + max(lons)) / 2.0
        # floor the span in km so a single aircraft does not zoom to nothing
        lat_c = inv_merc_y(cy)
        min_deg = self.MIN_SPAN_KM / KM_PER_DEG_LAT
        span_y = min(max((max(ys) - min(ys)) * 1.35, min_deg), self.MAX_SPAN_DEG)
        span_x = min(max((max(lons) - min(lons)) * 1.35, min_deg), self.MAX_SPAN_DEG)
        self.getViewBox().setRange(
            xRange=(cx - span_x/2, cx + span_x/2),
            yRange=(cy - span_y/2, cy + span_y/2), padding=0)

    def zoom(self, factor):
        vb = self.getViewBox()
        (x0, x1), (y0, y1) = vb.viewRange()
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        sx, sy = (x1 - x0) * factor, (y1 - y0) * factor
        if not (0.02 <= sy <= self.MAX_SPAN_DEG):
            return
        vb.setRange(xRange=(cx - sx/2, cx + sx/2),
                    yRange=(cy - sy/2, cy + sy/2), padding=0)

    def span_km(self):
        (x0, x1), (y0, y1) = self.getViewBox().viewRange()
        lat = inv_merc_y((y0 + y1) / 2.0)
        return (x1 - x0) * KM_PER_DEG_LAT * max(math.cos(math.radians(lat)), 0.01)

    def center_on(self, lat, lon, span_km=120.0):
        self._follow = False
        self.followChanged.emit(False)
        d = span_km / KM_PER_DEG_LAT
        y = merc_y(lat)
        self.getViewBox().setRange(xRange=(lon - d/2, lon + d/2),
                                   yRange=(y - d/2, y + d/2), padding=0)

    def shutdown(self):
        self.tiles.shutdown()
