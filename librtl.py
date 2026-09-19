"""
Minimal ctypes binding to librtlsdr.

pyrtlsdr is avoided deliberately: it binds rtlsdr_set_dithering at import time,
which the rtl-sdr-blog V1.4.0 Windows DLL does not export, so importing it dies
before a device is ever opened. Only the calls this application actually needs
are bound here, all of which are confirmed present in that DLL.
"""
import os
import ctypes
from ctypes import c_int, c_uint32, c_char_p, c_void_p, POINTER, byref

import sys

# Library file names by platform. Windows ships rtlsdr.dll; Linux and macOS
# use the usual so/dylib spellings and the system loader finds them on its own.
if sys.platform == "win32":
    _NAMES = ("rtlsdr.dll", "librtlsdr.dll")
elif sys.platform == "darwin":
    _NAMES = ("librtlsdr.dylib", "librtlsdr.0.dylib")
else:
    _NAMES = ("librtlsdr.so.0", "librtlsdr.so")

# RTLSDR_DLL_DIR wins; otherwise look beside this file, then where the
# rtl-sdr-blog build is usually unpacked.
_CANDIDATE_DIRS = [
    os.environ.get("RTLSDR_DLL_DIR", ""),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "rtl"),
    os.path.dirname(os.path.abspath(__file__)),
    os.path.join("C:" + os.sep, "tmp", "rtlsdr", "rtl", "x64"),
    os.path.join("C:" + os.sep, "Program Files", "rtl-sdr"),
]
DLL_DIR = next((d for d in _CANDIDATE_DIRS
                if d and any(os.path.isfile(os.path.join(d, n)) for n in _NAMES)),
               "")

TUNERS = {
    0: "UNKNOWN", 1: "E4000", 2: "FC0012", 3: "FC0013",
    4: "FC2580", 5: "R820T", 6: "R828D",
}

_lib = None
_load_error = None


def _bind(lib):
    lib.rtlsdr_get_device_count.restype = c_uint32
    lib.rtlsdr_get_device_name.restype = c_char_p
    lib.rtlsdr_get_device_name.argtypes = [c_uint32]
    lib.rtlsdr_open.argtypes = [POINTER(c_void_p), c_uint32]
    lib.rtlsdr_close.argtypes = [c_void_p]
    lib.rtlsdr_set_sample_rate.argtypes = [c_void_p, c_uint32]
    lib.rtlsdr_set_center_freq.argtypes = [c_void_p, c_uint32]
    lib.rtlsdr_get_center_freq.argtypes = [c_void_p]
    lib.rtlsdr_get_center_freq.restype = c_uint32
    lib.rtlsdr_set_freq_correction.argtypes = [c_void_p, c_int]
    lib.rtlsdr_set_tuner_gain_mode.argtypes = [c_void_p, c_int]
    lib.rtlsdr_set_tuner_gain.argtypes = [c_void_p, c_int]
    lib.rtlsdr_get_tuner_gains.argtypes = [c_void_p, POINTER(c_int)]
    lib.rtlsdr_get_tuner_type.argtypes = [c_void_p]
    lib.rtlsdr_set_agc_mode.argtypes = [c_void_p, c_int]
    lib.rtlsdr_reset_buffer.argtypes = [c_void_p]
    lib.rtlsdr_read_sync.argtypes = [c_void_p, c_void_p, c_int, POINTER(c_int)]
    return lib


def load():
    """
    Load librtlsdr on first use and remember the outcome.

    Loading at import time meant the whole application refused to start
    without the library present - no window, no message, and a frozen build
    that could not even run its own self-test on a machine with no dongle.
    """
    global _lib, _load_error
    if _lib is not None or _load_error is not None:
        return _lib
    if DLL_DIR and os.path.isdir(DLL_DIR):
        os.environ["PATH"] = DLL_DIR + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(DLL_DIR)
            except OSError:
                pass
    tried, last = [], None
    for name in _NAMES:
        for cand in ([os.path.join(DLL_DIR, name)] if DLL_DIR else []) + [name]:
            tried.append(cand)
            try:
                _lib = _bind(ctypes.CDLL(cand))
                return _lib
            except OSError as exc:
                last = exc
    _load_error = (f"librtlsdr not found (tried {', '.join(_NAMES)}). "
                   f"Put it beside the application or set RTLSDR_DLL_DIR. "
                   f"Last error: {last}")
    return None


def available():
    return load() is not None


def load_error():
    load()
    return _load_error or ""




def device_count():
    lib = load()
    return int(lib.rtlsdr_get_device_count()) if lib else 0


def device_name(index=0):
    lib = load()
    if not lib:
        return "no driver"
    n = lib.rtlsdr_get_device_name(c_uint32(index))
    return n.decode(errors="replace") if n else "?"


class RtlSdrError(OSError):
    pass


class RtlSdr:
    """Thin synchronous wrapper. read_bytes() returns native uint8 I/Q."""

    # librtlsdr requires transfer sizes that are a multiple of 512 bytes and
    # rejects anything past 256 * 16384 in one shot.
    MAX_XFER = 256 * 16384

    def __init__(self, index=0):
        if not available():
            raise RtlSdrError(load_error())
        if device_count() == 0:
            raise RtlSdrError("no RTL-SDR device found (is WinUSB bound to Interface 0?)")
        self._dev = c_void_p()
        rc = load().rtlsdr_open(byref(self._dev), c_uint32(index))
        if rc != 0 or not self._dev:
            raise RtlSdrError(
                f"rtlsdr_open failed (rc={rc}). The dongle is almost certainly "
                f"held by another process - most often a stray rtl_adsb.exe "
                f"left behind by a crash. Close it, or use 'Release device' "
                f"on the Aircraft tab.")
        self.name = device_name(index)
        self._buf = None
        self._buf_len = 0

    # ---------------------------------------------------------- config --
    def close(self):
        if getattr(self, "_dev", None):
            load().rtlsdr_close(self._dev)
            self._dev = None

    def set_sample_rate(self, hz):
        if load().rtlsdr_set_sample_rate(self._dev, c_uint32(int(hz))) != 0:
            raise RtlSdrError(f"set_sample_rate({hz}) failed")

    def set_center_freq(self, hz):
        hz = int(hz)
        if load().rtlsdr_set_center_freq(self._dev, c_uint32(hz)) != 0:
            raise RtlSdrError(f"set_center_freq({hz}) failed")

    def get_center_freq(self):
        return int(load().rtlsdr_get_center_freq(self._dev))

    def set_freq_correction(self, ppm):
        # returns -2 when the value is already set; that is not an error
        rc = load().rtlsdr_set_freq_correction(self._dev, c_int(int(ppm)))
        if rc not in (0, -2):
            raise RtlSdrError(f"set_freq_correction({ppm}) failed (rc={rc})")

    def get_gains(self):
        n = load().rtlsdr_get_tuner_gains(self._dev, None)
        if n <= 0:
            return []
        arr = (c_int * n)()
        load().rtlsdr_get_tuner_gains(self._dev, arr)
        return [g / 10.0 for g in arr]

    def set_gain(self, db):
        """db=None or 'auto' hands gain control to the tuner."""
        if db is None or db == "auto":
            load().rtlsdr_set_tuner_gain_mode(self._dev, 0)
            return
        load().rtlsdr_set_tuner_gain_mode(self._dev, 1)
        gains = self.get_gains()
        target = float(db)
        if gains:                      # snap to nearest supported step
            target = min(gains, key=lambda g: abs(g - target))
        load().rtlsdr_set_tuner_gain(self._dev, c_int(int(round(target * 10))))
        return target

    def set_agc(self, on):
        load().rtlsdr_set_agc_mode(self._dev, c_int(1 if on else 0))

    def tuner_type(self):
        return TUNERS.get(int(load().rtlsdr_get_tuner_type(self._dev)), "UNKNOWN")

    def reset_buffer(self):
        load().rtlsdr_reset_buffer(self._dev)

    # ------------------------------------------------------------ read --
    def read_bytes(self, nbytes):
        nbytes = (int(nbytes) // 512) * 512
        if nbytes <= 0:
            return b""
        if self._buf_len < nbytes:
            self._buf = ctypes.create_string_buffer(nbytes)
            self._buf_len = nbytes
        out = bytearray()
        remaining = nbytes
        got = c_int(0)
        while remaining > 0:
            chunk = min(remaining, self.MAX_XFER)
            rc = load().rtlsdr_read_sync(self._dev, self._buf, c_int(chunk), byref(got))
            if rc != 0:
                raise RtlSdrError(f"read_sync failed (rc={rc})")
            if got.value <= 0:
                raise RtlSdrError("read_sync returned no data")
            out += self._buf.raw[:got.value]
            remaining -= got.value
        return bytes(out)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
