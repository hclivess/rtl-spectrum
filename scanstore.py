"""
Saved scans.

A sweep of a wide range costs real time, and the answer barely changes between
runs: the broadcast stations in a town are the same tomorrow. Every auto-find
is written here with its trace, so the result can be brought back and looked
at without tuning the dongle at all -- useful when the radio is unplugged, or
busy doing something else.

The trace is stored decimated to TRACE_POINTS, which is plenty to redraw and
keeps a file of twenty scans to a few hundred kB.
"""
import os
import json
import time

import numpy as np

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
STORE = os.path.join(DATA_DIR, "scans.json")
TRACE_POINTS = 2048
MAX_SCANS = 20


def _read():
    try:
        with open(STORE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _write(scans):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STORE + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(scans[:MAX_SCANS], fh, separators=(",", ":"))
    os.replace(tmp, STORE)          # a scan file is never half written


def _decimate(freqs, db, points=TRACE_POINTS):
    """Keep the peaks: a plain stride would drop the narrow carriers."""
    n = len(db)
    if n <= points:
        return list(map(float, freqs)), [round(float(v), 2) for v in db]
    edges = np.linspace(0, n, points + 1).astype(int)
    f_out, d_out = [], []
    for a, b in zip(edges[:-1], edges[1:]):
        if b <= a:
            continue
        i = a + int(np.argmax(db[a:b]))
        f_out.append(float(freqs[i]))
        d_out.append(round(float(db[i]), 2))
    return f_out, d_out


def save(f_start, f_stop, country, hits, freqs=None, db=None):
    """Record one completed scan and return it."""
    entry = {
        "when": time.time(),
        "f_start": float(f_start),
        "f_stop": float(f_stop),
        "country": country or "",
        "hits": [[round(float(f), 4), round(float(s), 2)] for f, s in hits],
    }
    if freqs is not None and db is not None and len(db):
        tf, td = _decimate(np.asarray(freqs), np.asarray(db))
        entry["trace_freqs"] = tf
        entry["trace_db"] = td

    scans = [s for s in _read()
             if not (abs(s.get("f_start", 0) - entry["f_start"]) < 1.0
                     and abs(s.get("f_stop", 0) - entry["f_stop"]) < 1.0)]
    scans.insert(0, entry)
    _write(scans)
    return entry


def all_scans():
    return sorted(_read(), key=lambda s: -s.get("when", 0))


def latest():
    scans = all_scans()
    return scans[0] if scans else None


def delete(index):
    scans = all_scans()
    if 0 <= index < len(scans):
        del scans[index]
        _write(scans)


def clear():
    try:
        os.remove(STORE)
    except OSError:
        pass


def age_text(when):
    secs = max(0.0, time.time() - float(when))
    if secs < 90:
        return "just now"
    if secs < 5400:
        return f"{int(secs // 60)} min ago"
    if secs < 172800:
        return f"{int(secs // 3600)} h ago"
    return f"{int(secs // 86400)} days ago"


def label(scan):
    return (f"{scan['f_start'] / 1e6:.1f}-{scan['f_stop'] / 1e6:.1f} MHz  "
            f"{len(scan.get('hits', []))} signals  "
            f"{age_text(scan.get('when', 0))}")
