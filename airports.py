"""
Airport radio frequencies, so you can tune instead of hunt.

Data is the public-domain OurAirports dataset. It is downloaded on demand
rather than shipped: the two source files are about 12 MB, and most of that is
airports with no radio at all. What is kept is a compact cache of only those
airports that publish a frequency.

Nothing here is authoritative for flight. Frequencies change and the dataset is
community-maintained; the national AIP is the real source.
"""
import os
import csv
import json
import math
import io
import urllib.request

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CACHE = os.path.join(DATA_DIR, "airport-frequencies.json")
USER_AGENT = "rtl-spectrum/1.0 (hobby SDR receiver)"

AIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
FREQS_URL = ("https://davidmegginson.github.io/ourairports-data/"
             "airport-frequencies.csv")

# Types worth listening to, in the order a listener usually wants them.
USEFUL = ("ATIS", "TWR", "TOWER", "GND", "GROUND", "APP", "APPROACH", "DEP",
          "DEPARTURE", "CTR", "CENTER", "CENTRE", "RADIO", "INFO", "AFIS",
          "AUTO-INFO", "DELIVERY", "CLD", "RDO", "A/D", "UNICOM", "CTAF")

EARTH_KM = 6371.0


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", errors="replace")


def cached():
    return os.path.isfile(CACHE)


def cache_info():
    if not cached():
        return "not downloaded"
    try:
        with open(CACHE, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return f"{len(d)} airports, {os.path.getsize(CACHE)//1024} kB"
    except (OSError, ValueError):
        return "cache unreadable"


def download(progress=None):
    """
    Build the cache. `progress` is called with a short status string.
    Returns the number of airports kept.
    """
    def say(msg):
        if progress:
            progress(msg)

    os.makedirs(DATA_DIR, exist_ok=True)

    say("downloading frequencies...")
    freq_rows = list(csv.DictReader(io.StringIO(_fetch(FREQS_URL))))
    say(f"{len(freq_rows)} frequencies; downloading airports...")
    air_rows = list(csv.DictReader(io.StringIO(_fetch(AIRPORTS_URL))))
    say(f"{len(air_rows)} airports; building cache...")

    by_ident = {}
    for r in freq_rows:
        ident = (r.get("airport_ident") or "").strip()
        try:
            mhz = float(r.get("frequency_mhz") or 0)
        except ValueError:
            continue
        if not ident or not (24.0 <= mhz <= 1766.0):
            continue
        kind = (r.get("type") or "").strip().upper()
        by_ident.setdefault(ident, []).append({
            "type": kind,
            "desc": (r.get("description") or "").strip(),
            "mhz": round(mhz, 4),
        })

    out = {}
    for r in air_rows:
        ident = (r.get("ident") or "").strip()
        if ident not in by_ident:
            continue
        try:
            lat = float(r["latitude_deg"])
            lon = float(r["longitude_deg"])
        except (KeyError, TypeError, ValueError):
            continue
        out[ident] = {
            "name": (r.get("name") or "").strip(),
            "country": (r.get("iso_country") or "").strip(),
            "lat": round(lat, 5),
            "lon": round(lon, 5),
            "freqs": sorted(by_ident[ident], key=lambda f: _rank(f["type"])),
        }

    tmp = CACHE + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(out, fh, separators=(",", ":"))
    os.replace(tmp, CACHE)              # never leave a half-written cache
    say(f"cached {len(out)} airports with radio")
    return len(out)


def _rank(kind):
    for i, k in enumerate(USEFUL):
        if kind.startswith(k):
            return i
    return len(USEFUL)


def _load():
    if not cached():
        return {}
    try:
        with open(CACHE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def distance_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(a)))


def near(lat, lon, radius_km=120.0, max_airports=12):
    """[(distance_km, ident, airport dict), ...] sorted by distance."""
    out = []
    for ident, a in _load().items():
        d = distance_km(lat, lon, a["lat"], a["lon"])
        if d <= radius_km:
            out.append((d, ident, a))
    out.sort(key=lambda t: t[0])
    return out[:max_airports]


def channels_near(lat, lon, radius_km=120.0, max_airports=12, limit=120):
    """
    Flattened list of (mhz, label, distance_km) ready to drop into a UI list,
    nearest airport first and the most useful frequency type first within it.
    """
    rows = []
    for d, ident, a in near(lat, lon, radius_km, max_airports):
        for f in a["freqs"]:
            desc = f["desc"] or f["type"]
            label = f"{ident} {f['type']}  {a['name']}"
            if desc and desc.upper() not in (f["type"], ident):
                label += f"  ({desc})"
            rows.append((f["mhz"], label, d))
            if len(rows) >= limit:
                return rows
    return rows
