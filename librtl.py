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

# Where to look for the DLL. RTLSDR_DLL_DIR wins; otherwise try the directory
# beside this file, then the usual unpack location of the rtl-sdr-blog build.
_CANDIDATE_DIRS = [
    os.environ.get("RTLSDR_DLL_DIR", ""),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "rtl"),
    os.path.dirname(os.path.abspath(__file__)),
    r"C:\tmp\rtlsdr\rtl\x64",
    r"C:\Program Files\rtl-sdr",
]
DLL_DIR = next((d for d in _CANDIDATE_DIRS
                if d and os.path.isfile(os.path.join(d, "rtlsdr.dll"))),
               _CANDIDATE_DIRS[3])

TUNERS = {
    0: "UNKNOWN", 1: "E4000", 2: "FC0012", 3: "FC0013",
    4: "FC2580", 5: "R820T", 6: "R828D",
}


def _load():
    if os.path.isdir(DLL_DIR):
        os.environ["PATH"] = DLL_DIR + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(DLL_DIR)
            except OSError:
                pass
    last = None
    for cand in (os.path.join(DLL_DIR, "rtlsdr.dll"), "rtlsdr.dll", "librtlsdr.dll"):
        try:
            return ctypes.CDLL(cand)
        except OSError as exc:
            last = exc
    raise OSError(f"could not load librtlsdr (tried rtlsdr.dll): {last}")


_lib = _load()

_lib.rtlsdr_get_device_count.restype = c_uint32
_lib.rtlsdr_get_device_name.restype = c_char_p
_lib.rtlsdr_get_device_name.argtypes = [c_uint32]
_lib.rtlsdr_open.argtypes = [POINTER(c_void_p), c_uint32]
_lib.rtlsdr_close.argtypes = [c_void_p]
_lib.rtlsdr_set_sample_rate.argtypes = [c_void_p, c_uint32]
_lib.rtlsdr_set_center_freq.argtypes = [c_void_p, c_uint32]
_lib.rtlsdr_get_center_freq.argtypes = [c_void_p]
_lib.rtlsdr_get_center_freq.restype = c_uint32
_lib.rtlsdr_set_freq_correction.argtypes = [c_void_p, c_int]
_lib.rtlsdr_set_tuner_gain_mode.argtypes = [c_void_p, c_int]
_lib.rtlsdr_set_tuner_gain.argtypes = [c_void_p, c_int]
_lib.rtlsdr_get_tuner_gains.argtypes = [c_void_p, POINTER(c_int)]
_lib.rtlsdr_get_tuner_type.argtypes = [c_void_p]
_lib.rtlsdr_set_agc_mode.argtypes = [c_void_p, c_int]
_lib.rtlsdr_reset_buffer.argtypes = [c_void_p]
_lib.rtlsdr_read_sync.argtypes = [c_void_p, c_void_p, c_int, POINTER(c_int)]


def device_count():
    return int(_lib.rtlsdr_get_device_count())


def device_name(index=0):
    n = _lib.rtlsdr_get_device_name(c_uint32(index))
    return n.decode(errors="replace") if n else "?"


class RtlSdrError(OSError):
    pass


class RtlSdr:
    """Thin synchronous wrapper. read_bytes() returns native uint8 I/Q."""

    # librtlsdr requires transfer sizes that are a multiple of 512 bytes and
    # rejects anything past 256 * 16384 in one shot.
    MAX_XFER = 256 * 16384

    def __init__(self, index=0):
        if device_count() == 0:
            raise RtlSdrError("no RTL-SDR device found (is WinUSB bound to Interface 0?)")
        self._dev = c_void_p()
        rc = _lib.rtlsdr_open(byref(self._dev), c_uint32(index))
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
            _lib.rtlsdr_close(self._dev)
            self._dev = None

    def set_sample_rate(self, hz):
        if _lib.rtlsdr_set_sample_rate(self._dev, c_uint32(int(hz))) != 0:
            raise RtlSdrError(f"set_sample_rate({hz}) failed")

    def set_center_freq(self, hz):
        hz = int(hz)
        if _lib.rtlsdr_set_center_freq(self._dev, c_uint32(hz)) != 0:
            raise RtlSdrError(f"set_center_freq({hz}) failed")

    def get_center_freq(self):
        return int(_lib.rtlsdr_get_center_freq(self._dev))

    def set_freq_correction(self, ppm):
        # returns -2 when the value is already set; that is not an error
        rc = _lib.rtlsdr_set_freq_correction(self._dev, c_int(int(ppm)))
        if rc not in (0, -2):
            raise RtlSdrError(f"set_freq_correction({ppm}) failed (rc={rc})")

    def get_gains(self):
        n = _lib.rtlsdr_get_tuner_gains(self._dev, None)
        if n <= 0:
            return []
        arr = (c_int * n)()
        _lib.rtlsdr_get_tuner_gains(self._dev, arr)
        return [g / 10.0 for g in arr]

    def set_gain(self, db):
        """db=None or 'auto' hands gain control to the tuner."""
        if db is None or db == "auto":
            _lib.rtlsdr_set_tuner_gain_mode(self._dev, 0)
            return
        _lib.rtlsdr_set_tuner_gain_mode(self._dev, 1)
        gains = self.get_gains()
        target = float(db)
        if gains:                      # snap to nearest supported step
            target = min(gains, key=lambda g: abs(g - target))
        _lib.rtlsdr_set_tuner_gain(self._dev, c_int(int(round(target * 10))))
        return target

    def set_agc(self, on):
        _lib.rtlsdr_set_agc_mode(self._dev, c_int(1 if on else 0))

    def tuner_type(self):
        return TUNERS.get(int(_lib.rtlsdr_get_tuner_type(self._dev)), "UNKNOWN")

    def reset_buffer(self):
        _lib.rtlsdr_reset_buffer(self._dev)

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
            rc = _lib.rtlsdr_read_sync(self._dev, self._buf, c_int(chunk), byref(got))
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
