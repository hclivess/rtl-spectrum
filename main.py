#!/usr/bin/env python3
"""
rtl-spectrum - a wideband receiver for RTL2832U dongles.

Three things you can do to a frequency: listen to it, sweep across it, or scan
it and record whatever opens the squelch. Plus ADS-B on 1090 MHz, decoded and
plotted on a map.

The DVB-T demodulator in an RTL2832U cannot be upgraded to DVB-T2 -- that is a
different chip, not a firmware level -- but the tuner still hands over raw IQ
from 24 MHz to 1.766 GHz, which is what this uses.
"""
import os
import sys
import csv
import math
import time
import wave
import queue
import shutil
import threading
import json
import webbrowser
import subprocess
from datetime import datetime

import numpy as np
import pyqtgraph as pg
import pyqtgraph.exporters  # noqa: F401  -- registers ImageExporter
from PySide6.QtCore import (Qt, QThread, Signal, Slot, QRectF, QTimer,
                            QSettings)
from PySide6.QtGui import QFont, QColor, QBrush, QIcon, QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QGroupBox, QLabel, QPushButton, QComboBox, QDoubleSpinBox, QSpinBox,
    QRadioButton, QCheckBox, QFileDialog, QMessageBox, QSplitter, QStatusBar,
    QButtonGroup, QProgressBar, QListWidget, QTabWidget, QTableWidget,
    QTableWidgetItem, QPlainTextEdit, QHeaderView, QAbstractItemView,
    QScrollArea, QFrame, QInputDialog,
)

import theme
from config import (APP_NAME, APP_VERSION, APP_REPO, AUDIO_FS, SCAN_FS, BANDS,
                    TUNER_MIN_HZ, TUNER_MAX_HZ, DEFAULT_REC_DIR,
                    DEFAULT_SETTINGS, BUILTIN_PRESETS)
from dsp import (Demodulator, Deemphasis, FirDecimator, AudioSink, fft_decimate,
                 decimate_peak,
                 find_signals, channel_snr, suggest_demod, band_label, fmt_age)
from librtl import RtlSdr, RtlSdrError, device_count, device_name
import airports
import bandplan as bp
import scanstore
from adsb import AdsbDecoder
from worldmap import MapWidget, radio_horizon_km
from tiles import SOURCES as TILE_SOURCES
from utils import childproc

try:
    import sounddevice as _sd
except Exception:
    _sd = None

SELFTEST = os.environ.get("RTLSPECTRUM_SELFTEST")


def _find_rtl_adsb():
    """rtl_adsb does the 1090 MHz demodulation; locate it or fall back to PATH."""
    env = os.environ.get("RTL_ADSB")
    if env and os.path.isfile(env):
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (os.path.join(here, "rtl"), here,
              os.path.join("C:" + os.sep, "tmp", "rtlsdr", "rtl", "x64")):
        p = os.path.join(d, "rtl_adsb.exe" if os.name == "nt" else "rtl_adsb")
        if os.path.isfile(p):
            return p
    return shutil.which("rtl_adsb") or "rtl_adsb"


RTL_ADSB = _find_rtl_adsb()


def release_device():
    """
    Kill any stray rtl_adsb left by a hard kill. childproc normally takes the
    child down with us, so this is a belt-and-braces path for a process that
    predates the current run.
    """
    if os.name != "nt":
        return False
    try:
        r = subprocess.run(["taskkill", "/F", "/IM", "rtl_adsb.exe"],
                           capture_output=True, text=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        return r.returncode == 0
    except Exception:
        return False


# --------------------------------------------------------- spectrum work ---
class SdrWorker(QThread):
    spectrum = Signal(object, object)
    audio = Signal(object)
    status = Signal(str)
    progress = Signal(int)
    failed = Signal(str)
    deviceInfo = Signal(str)
    signalsFound = Signal(object)

    def __init__(self):
        super().__init__()
        self.sdr = None
        self._run = False
        self.find_mode = False
        self.mode = "live"
        self.center = 94.6e6
        self.f_start = 24.0e6
        self.f_stop = 1766.0e6
        self.fs = 2.4e6
        self.gain = "49.6"
        self.ppm = 65
        self.device = 0
        self.nfft = 4096
        self.demod = "Off"
        self.iq_path = None
        self.wav_path = None
        self._iq_fh = None
        self._wav_fh = None
        self._demod = None
        self._last_spec = 0.0
        self._wav_tmp = None
        self._iq_tmp = None

    def _finish_wav(self):
        # a file under its final name is always complete (STANDARD 1b)
        if self._wav_tmp and os.path.exists(self._wav_tmp):
            os.replace(self._wav_tmp, self._wav_tmp[:-5])
        self._wav_tmp = None

    def _finish_iq(self):
        if self._iq_tmp and os.path.exists(self._iq_tmp):
            os.replace(self._iq_tmp, self._iq_tmp[:-5])
        self._iq_tmp = None

    def start_iq(self, p): self.iq_path = p
    def stop_iq(self): self.iq_path = None
    def start_wav(self, p): self.wav_path = p
    def stop_wav(self): self.wav_path = None
    def stop(self): self._run = False

    def _open(self):
        sdr = RtlSdr(self.device)
        sdr.set_sample_rate(self.fs)
        sdr.set_freq_correction(self.ppm)
        applied = sdr.set_gain(None if self.gain == "auto" else self.gain)
        sdr.reset_buffer()
        g = "auto" if applied is None else f"{applied:.1f} dB"
        self.deviceInfo.emit(f"{sdr.tuner_type()}  |  {g}  |  {self.ppm} ppm")
        return sdr

    def _read(self, nsamp):
        raw = np.frombuffer(self.sdr.read_bytes(nsamp * 2), dtype=np.uint8)
        if self._iq_fh is not None:
            self._iq_fh.write(raw.tobytes())
        f = raw.astype(np.float32) - 127.5
        return ((f[0::2] + 1j * f[1::2]) / 127.5).astype(np.complex64)

    def _sync_files(self):
        if self.iq_path and self._iq_fh is None:
            self._iq_tmp = self.iq_path + ".part"
            self._iq_fh = open(self._iq_tmp, "wb")
            self.status.emit(f"recording IQ to {os.path.basename(self.iq_path)}")
        elif not self.iq_path and self._iq_fh is not None:
            self._iq_fh.close(); self._iq_fh = None
            self._finish_iq()
            self.status.emit("IQ recording stopped")
        if self.wav_path and self._wav_fh is None:
            self._wav_tmp = self.wav_path + ".part"
            fh = wave.open(self._wav_tmp, "wb")
            fh.setnchannels(1); fh.setsampwidth(2); fh.setframerate(AUDIO_FS)
            self._wav_fh = fh
            self.status.emit(f"recording audio to {os.path.basename(self.wav_path)}")
        elif not self.wav_path and self._wav_fh is not None:
            self._wav_fh.close(); self._wav_fh = None
            self._finish_wav()
            self.status.emit("audio recording stopped")

    def run(self):
        try:
            self.sdr = self._open()
        except Exception as exc:
            self.failed.emit(str(exc)); return
        self._run = True
        try:
            self._run_live() if self.mode == "live" else self._run_sweep()
        except Exception as exc:
            if self._run:
                self.failed.emit(f"{type(exc).__name__}: {exc}")
        finally:
            for fh in (self._iq_fh, self._wav_fh):
                if fh:
                    fh.close()
            self._iq_fh = self._wav_fh = None
            self._finish_wav()
            self._finish_iq()
            try: self.sdr.close()
            except Exception: pass
            self.sdr = None
            self.status.emit("stopped")

    def _reader(self, nsamp, q):
        """
        Pull from the dongle in its own thread.

        read_bytes already takes a full block-time to return, so doing the
        FFTs and demodulation between calls pushes each iteration past the
        block period and librtlsdr silently drops the overflow. Reading in a
        dedicated thread keeps the device drained while processing runs in
        parallel -- numpy and the USB call both release the GIL.
        """
        while self._run:
            try:
                raw = self.sdr.read_bytes(nsamp * 2)
            except Exception:
                break
            try:
                q.put_nowait(raw)
            except queue.Full:
                try:
                    q.get_nowait()          # drop the oldest, keep audio fresh
                    q.put_nowait(raw)
                except (queue.Empty, queue.Full):
                    pass

    def _run_live(self):
        self.sdr.set_center_freq(self.center)
        win = np.hanning(self.nfft).astype(np.float32)
        freqs = self.center + (np.arange(self.nfft) - self.nfft // 2) * (self.fs / self.nfft)
        self.status.emit(f"live at {self.center/1e6:.4f} MHz")

        nsamp = max(self.nfft * 8, 65536)
        q = queue.Queue(maxsize=6)
        reader = threading.Thread(target=self._reader, args=(nsamp, q), daemon=True)
        reader.start()
        try:
            self._live_loop(q, win, freqs)
        finally:
            self._run = False
            reader.join(timeout=2.0)

    def _live_loop(self, q, win, freqs):
        while self._run:
            self._sync_files()
            try:
                raw = q.get(timeout=1.0)
            except queue.Empty:
                continue
            arr = np.frombuffer(raw, dtype=np.uint8)
            if self._iq_fh is not None:
                self._iq_fh.write(raw)
            f = arr.astype(np.float32) - 127.5
            iq = ((f[0::2] + 1j * f[1::2]) / 127.5).astype(np.complex64)
            frames = len(iq) // self.nfft
            if not frames:
                continue
            acc = np.zeros(self.nfft)
            for k in range(frames):
                acc += np.abs(np.fft.fftshift(
                    np.fft.fft(iq[k*self.nfft:(k+1)*self.nfft] * win))) ** 2
            db = 10 * np.log10(acc / frames / self.nfft**2 + 1e-20)
            db[self.nfft // 2] = db[self.nfft // 2 - 2]        # notch DC spike
            # cap plot updates: repainting a waterfall per block starves audio
            now = time.time()
            if now - self._last_spec >= 0.066:
                self._last_spec = now
                self.spectrum.emit(freqs, db.astype(np.float32))
            if self.demod != "Off":
                if self._demod is None or self._demod.mode != self.demod:
                    self._demod = Demodulator(self.fs, self.demod)
                aud = self._demod(iq)
                if len(aud):
                    self.audio.emit(aud)
                    if self._wav_fh is not None:
                        self._wav_fh.writeframes((aud * 32767).astype(np.int16).tobytes())

    def _run_sweep(self):
        usable = self.fs * 0.8
        centers = np.arange(self.f_start + usable/2, self.f_stop + usable/2, usable)
        centers = centers[(centers >= TUNER_MIN_HZ) & (centers <= TUNER_MAX_HZ)]
        if not len(centers):
            self.failed.emit("that span produced no tuning steps"); return
        n, reps = 2048, 4
        win = np.hanning(n).astype(np.float32)
        keep = (int(n * 0.8) // 2) * 2
        lo = (n - keep) // 2
        bin_hz = self.fs / n
        self.status.emit(f"sweeping {self.f_start/1e6:.1f}-{self.f_stop/1e6:.1f} MHz, "
                         f"{len(centers)} steps")
        while self._run:
            all_f, all_d, t0 = [], [], time.time()
            for i, fc in enumerate(centers):
                if not self._run:
                    break
                self._sync_files()
                try:
                    self.sdr.set_center_freq(float(fc))
                except RtlSdrError:
                    continue
                self._read(4096)                       # discard, let PLL settle
                iq = self._read(n * reps)
                acc = np.zeros(n)
                for k in range(reps):
                    acc += np.abs(np.fft.fftshift(
                        np.fft.fft(iq[k*n:(k+1)*n] * win))) ** 2
                db = 10 * np.log10(acc / reps / n**2 + 1e-20)
                db[n // 2] = db[n // 2 - 2]
                all_f.append(fc + (np.arange(lo, lo + keep) - n//2) * bin_hz)
                all_d.append(db[lo:lo + keep])
                if i % 8 == 0 or i == len(centers) - 1:
                    self.progress.emit(int(100 * (i+1) / len(centers)))
                    self.spectrum.emit(np.concatenate(all_f),
                                       np.concatenate(all_d).astype(np.float32))
            if all_f and self._run:
                freqs = np.concatenate(all_f)
                db = np.concatenate(all_d).astype(np.float32)
                self.progress.emit(100)
                self.spectrum.emit(freqs, db)
                self.status.emit(f"sweep done: {len(freqs)} bins in {time.time()-t0:.1f} s")
                if self.find_mode:
                    hits = find_signals(freqs, db)
                    self.signalsFound.emit(hits)
                    self.status.emit(f"auto-find: {len(hits)} signals")
                    self._run = False
                    break


# ------------------------------------------------------ voice scanner ------
class ScannerWorker(QThread):
    """
    Wideband squelch scanner. Rather than visiting 25 kHz channels one at a
    time, it FFTs a whole 1.2 MHz slice and tests every channel in it at once,
    so a 19 MHz band is swept in roughly a second. Any channel above squelch
    is tuned and recorded until it goes quiet for `hang` seconds.
    """
    activity = Signal(float, float)          # freq_hz, snr_db
    logline = Signal(str)
    recorded = Signal(str, float, float)     # path, freq_hz, seconds
    status = Signal(str)
    failed = Signal(str)
    nowPlaying = Signal(float)
    audio = Signal(object)
    spectrum = Signal(object, object)        # the slice or channel on screen
    marks = Signal(object)                   # [(freq_hz, snr_db), ...]

    # The dongle generates its own carriers: harmonics of the 28.8 MHz
    # reference and the 24 MHz USB clock. They are strong, permanent and
    # unmodulated, so a squelch locks onto them and records forever.
    # 120.000 MHz (24 x 5) is the one that bites in the airband.
    SPUR_CLOCKS = (28.8e6, 24.0e6, 48.0e6)
    SPUR_GUARD = 6e3
    MAX_CAPTURE_S = 45.0          # nothing legitimate holds a channel this long

    def __init__(self):
        super().__init__()
        self._run = False
        self.f_start = 118.0e6
        self.f_stop = 137.0e6
        self.step = 25e3
        self.squelch = 8.0
        self.hang = 2.0
        self.min_len = 0.7
        self.skip_spurs = True
        self.monitor = True
        self._learned_spurs = []
        self._last_spec = 0.0
        self._mon_dm = None
        self._mon_ch = None

    def _monitor_audio(self, iq, off_hz, ch):
        """
        Demodulate one channel out of the slice we already captured by mixing
        it to DC, so the band can be heard while the scan hunts without
        costing a retune.
        """
        if self.demod == "Off":
            return
        t = np.arange(len(iq), dtype=np.float64)
        mix = (iq * np.exp(-2j * np.pi * off_hz * t / SCAN_FS)).astype(np.complex64)
        if (self._mon_dm is None or self._mon_ch != ch
                or self._mon_dm.mode != self.demod):
            self._mon_dm = Demodulator(SCAN_FS, self.demod)
            self._mon_ch = ch
        aud = self._mon_dm(mix, normalise=False)
        if not len(aud):
            return
        peak = float(np.abs(aud).max())
        if peak > 1e-9:
            aud = aud / max(peak, 0.02) * 0.45
        self.audio.emit(aud)
        self.gain = "49.6"
        self.ppm = 65
        self.demod = "AM"
        self.device = 0
        self.out_dir = DEFAULT_REC_DIR

    def stop(self):
        self._run = False

    def is_spur(self, freq):
        if not self.skip_spurs:
            return False
        for clk in self.SPUR_CLOCKS:
            n = round(freq / clk)
            if n >= 1 and abs(freq - n * clk) <= self.SPUR_GUARD:
                return True
        return any(abs(freq - f) <= self.SPUR_GUARD for f in self._learned_spurs)

    def _learn_spur(self, freq, why):
        if not any(abs(freq - f) <= self.SPUR_GUARD for f in self._learned_spurs):
            self._learned_spurs.append(freq)
            self.logline.emit(f"{datetime.now():%H:%M:%S}  BLOCKED {freq/1e6:9.4f} MHz "
                              f"- {why}")

    def _read(self, sdr, nsamp):
        raw = np.frombuffer(sdr.read_bytes(nsamp * 2), dtype=np.uint8)
        f = raw.astype(np.float32) - 127.5
        return ((f[0::2] + 1j * f[1::2]) / 127.5).astype(np.complex64)

    def run(self):
        os.makedirs(self.out_dir, exist_ok=True)
        try:
            sdr = RtlSdr(self.device)
            sdr.set_sample_rate(SCAN_FS)
            sdr.set_freq_correction(self.ppm)
            sdr.set_gain(None if self.gain == "auto" else self.gain)
            sdr.reset_buffer()
        except Exception as exc:
            self.failed.emit(str(exc)); return

        self._run = True
        usable = SCAN_FS * 0.75
        slices = np.arange(self.f_start + usable/2, self.f_stop + usable/2, usable)
        n = 8192
        win = np.hanning(n).astype(np.float32)
        self.status.emit(f"scanning {self.f_start/1e6:.2f}-{self.f_stop/1e6:.2f} MHz "
                         f"in {len(slices)} slices, squelch {self.squelch:.0f} dB")
        self.logline.emit(f"scan started: {len(slices)} slices, "
                          f"{self.demod}, squelch {self.squelch:.0f} dB")
        try:
            while self._run:
                for fc in slices:
                    if not self._run:
                        break
                    try:
                        sdr.set_center_freq(float(fc))
                    except RtlSdrError:
                        continue
                    self._read(sdr, 4096)
                    # average several FFTs: one 7 ms snapshot has enough
                    # variance to throw 10 dB outliers and chase ghosts
                    reps = 4
                    iq = self._read(sdr, n * reps)
                    p = np.zeros(n)
                    for k in range(reps):
                        p += np.abs(np.fft.fftshift(
                            np.fft.fft(iq[k*n:(k+1)*n] * win))) ** 2
                    p /= reps
                    f_off = (np.arange(n) - n // 2) * (SCAN_FS / n)

                    # channel grid inside the usable part of this slice
                    lo_f = fc - usable / 2
                    hi_f = fc + usable / 2
                    chans = np.arange(math.ceil(lo_f / self.step) * self.step,
                                      hi_f, self.step)
                    chans = chans[(chans >= self.f_start) & (chans <= self.f_stop)]
                    if not len(chans):
                        continue
                    powers = []
                    for ch in chans:
                        m = np.abs(f_off - (ch - fc)) <= 8000.0
                        powers.append(p[m].mean() if m.any() else 0.0)
                    powers = np.asarray(powers)
                    good = powers > 0
                    if good.sum() < 4:
                        continue
                    floor = np.median(powers[good])
                    snr = 10 * np.log10(np.maximum(powers, 1e-30) / max(floor, 1e-30))

                    # show the slice being examined, so the scan is visible
                    now = time.time()
                    if now - self._last_spec > 0.08:
                        self._last_spec = now
                        db = 10 * np.log10(p / n ** 2 + 1e-20)
                        db[n // 2] = db[n // 2 - 2]
                        self.spectrum.emit((fc + f_off).astype(np.float64),
                                           db.astype(np.float32))
                        self.marks.emit([(float(c), float(v))
                                         for c, v in zip(chans, snr) if v >= 3.0])

                    # monitor: hear the band while it hunts, not only during a
                    # capture. The channel is mixed down from the slice we
                    # already have, so this costs no extra tuning time.
                    if self.monitor:
                        best = int(np.argmax(snr))
                        ch = float(chans[best])
                        if not self.is_spur(ch):
                            self._monitor_audio(iq, ch - fc, ch)

                    for ch, s in sorted(zip(chans, snr), key=lambda t: -t[1]):
                        if not self._run:
                            break
                        if s < self.squelch:
                            break
                        if self.is_spur(float(ch)):
                            continue
                        self.activity.emit(float(ch), float(s))
                        self._capture(sdr, float(ch))
                    # retune the slice after a capture moved us away
        except Exception as exc:
            if self._run:
                self.failed.emit(f"{type(exc).__name__}: {exc}")
        finally:
            try: sdr.close()
            except Exception: pass
            self.status.emit("scanner stopped")
            self.logline.emit("scan stopped")

    def _capture(self, sdr, freq):
        """Tune `freq` and record while it stays above squelch."""
        try:
            sdr.set_center_freq(freq)
        except RtlSdrError:
            return
        self._read(sdr, 4096)
        blk = 65536                                   # ~55 ms at 1.2 MS/s
        iq = self._read(sdr, blk)
        if channel_snr(iq, SCAN_FS) < self.squelch:
            return                                    # gone already, keep scanning

        self.nowPlaying.emit(freq)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self.out_dir, f"{freq/1e6:.4f}MHz_{stamp}.wav")
        fh = wave.open(path, "wb")
        fh.setnchannels(1); fh.setsampwidth(2); fh.setframerate(AUDIO_FS)
        t0 = time.time()
        last_active = t0
        stuck = False
        self.logline.emit(f"{datetime.now():%H:%M:%S}  OPEN  {freq/1e6:9.4f} MHz  "
                          f"{band_label(freq)}")
        try:
            dm = Demodulator(SCAN_FS, self.demod)
            while self._run:
                aud = dm(iq, normalise=False)
                if len(aud):
                    peak = float(np.abs(aud).max())
                    if peak > 1e-9:
                        aud = aud / max(peak, 0.05) * 0.6
                    fh.writeframes((aud * 32767).astype(np.int16).tobytes())
                    self.audio.emit(aud)
                iq = self._read(sdr, blk)
                s = channel_snr(iq, SCAN_FS)
                self.activity.emit(freq, s)
                # follow onto the channel being recorded
                now2 = time.time()
                if now2 - self._last_spec > 0.08:
                    self._last_spec = now2
                    nf = 4096
                    seg = iq[:nf] * np.hanning(nf).astype(np.float32)
                    pw = np.abs(np.fft.fftshift(np.fft.fft(seg))) ** 2
                    dbc = 10 * np.log10(pw / nf ** 2 + 1e-20)
                    dbc[nf // 2] = dbc[nf // 2 - 2]
                    fr = freq + (np.arange(nf) - nf // 2) * (SCAN_FS / nf)
                    self.spectrum.emit(fr, dbc.astype(np.float32))
                    self.marks.emit([(freq, float(s))])
                now = time.time()
                if s >= self.squelch:
                    last_active = now
                elif now - last_active > self.hang:
                    break
                if now - t0 > self.MAX_CAPTURE_S:
                    # a carrier that never drops is the dongle, not a person
                    self._learn_spur(freq, f"held squelch for {self.MAX_CAPTURE_S:.0f}s")
                    stuck = True
                    break
        finally:
            fh.close()
            dur = time.time() - t0
            if stuck:
                try: os.remove(path)
                except OSError: pass
                self.nowPlaying.emit(0.0)
                return
            self.nowPlaying.emit(0.0)
            if dur < self.min_len:
                try: os.remove(path)
                except OSError: pass
                self.logline.emit(f"{datetime.now():%H:%M:%S}  drop  {freq/1e6:9.4f} MHz  "
                                  f"({dur:.1f}s, too short)")
            else:
                self.recorded.emit(path, freq, dur)
                self.logline.emit(f"{datetime.now():%H:%M:%S}  SAVED {freq/1e6:9.4f} MHz  "
                                  f"{dur:5.1f}s  {os.path.basename(path)}")


# ---------------------------------------------------------- ADS-B work -----
class AdsbWorker(QThread):
    """
    Drives rtl_adsb as a child process and decodes its frames. rtl_adsb owns
    the dongle while it runs, so this mode is exclusive with the others.
    """
    updated = Signal(object, int, int, float)
    logline = Signal(str)
    failed = Signal(str)

    def __init__(self):
        super().__init__()
        self._run = False
        self.gain = "49.6"
        self.ppm = 65
        self.device = 0
        self.proc = None
        self.decoder = AdsbDecoder()

    def stop(self):
        self._run = False
        if self.proc and self.proc.poll() is None:
            childproc.kill(self.proc)

    def run(self):
        if not os.path.exists(RTL_ADSB):
            self.failed.emit(f"rtl_adsb.exe not found at {RTL_ADSB}")
            return
        cmd = [RTL_ADSB, "-d", str(int(self.device)), "-p", str(int(self.ppm))]
        if self.gain != "auto":
            cmd += ["-g", str(self.gain)]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            # childproc puts it in a job object that dies with us, so a hard
            # kill of the app cannot leave rtl_adsb holding the USB device
            self.proc = childproc.popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1, creationflags=flags)
        except Exception as exc:
            self.failed.emit(f"cannot start rtl_adsb: {exc}")
            return

        self._run = True
        self.logline.emit("listening on 1090 MHz (ADS-B extended squitter)")
        last_emit = 0.0
        try:
            for line in self.proc.stdout:
                if not self._run:
                    break
                ac = self.decoder.feed(line)
                if ac is not None and ac.messages == 1:
                    self.logline.emit(
                        f"{datetime.now():%H:%M:%S}  new aircraft {ac.icao}"
                        + (f"  {ac.country}" if ac.country else ""))
                now = time.time()
                if now - last_emit > 1.0:
                    last_emit = now
                    self.decoder.prune()
                    t, v, pct = self.decoder.stats()
                    self.updated.emit(list(self.decoder.aircraft.values()), t, v, pct)
        except Exception as exc:
            if self._run:
                self.failed.emit(f"{type(exc).__name__}: {exc}")
        finally:
            self.stop()
            t, v, pct = self.decoder.stats()
            self.updated.emit(list(self.decoder.aircraft.values()), t, v, pct)
            self.logline.emit("ADS-B stopped")


# ------------------------------------------------------------------ GUI ----
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("RTL-SDR Spectrum Explorer")
        self.resize(1480, 940)

        self.worker = None
        self.scanner = None
        self.adsb = None
        self.peak_hold = None
        self.last_trace = None
        self.found = []
        self.wf_rows = 240
        self.wf = None
        self._audio_out = None
        self._emergency_seen = {}
        self._updating_table = False
        self._ac_index = {}

        pg.setConfigOptions(antialias=False, background=theme.PANEL,
                            foreground=theme.MUTED)

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(10, 10, 10, 6)
        outer.setSpacing(8)

        self.header = theme.Header("Spectrum Explorer", "RTL2832U / R820T")
        outer.addWidget(self.header)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._tab_radio(), "Radio")
        self.tabs.addTab(self._tab_aircraft(), "Aircraft")
        outer.addWidget(self.tabs, 1)

        self.setStatusBar(QStatusBar())
        self.prog = QProgressBar()
        self.prog.setMaximumWidth(200)
        self.statusBar().addPermanentWidget(self.prog)

        self._populate_devices()
        if device_count():
            self.header.pill_dev.set(device_name(0), theme.GOOD)
            self.statusBar().showMessage("device ready")
        else:
            self.header.pill_dev.set("no device", theme.BAD)
            self.statusBar().showMessage("no RTL-SDR found - check the WinUSB driver")

        self._build_menus()
        self._install_shortcuts()
        self._refresh_saved_list()
        self._wire_live_controls()
        self._restore_settings()

        threading.Thread(target=self._prune_caches, daemon=True).start()

        self.age_timer = QTimer(self)
        self.age_timer.timeout.connect(self._refresh_ages)
        self.age_timer.start(1000)

    # ------------------------------------------------------ spectrum tab --
    def _tab_radio(self):
        """
        One tab for everything the receiver does to a frequency: listen to it,
        sweep across it, or scan it and record what opens. These were two tabs
        with two sets of frequency, demodulator, listen and start controls,
        which only invited picking the wrong one.
        """
        page = QWidget()
        lay = QHBoxLayout(page)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(8)

        panel = QWidget()
        pl = QVBoxLayout(panel)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(8)

        # -------------------------------------------------------- tuning --
        # One frequency, or a range. That is the only thing that changes what
        # the receiver does; recording is a separate switch, because wanting
        # to record is orthogonal to what you are listening to.
        gb_tune = QGroupBox("Frequency")
        gt = QGridLayout(gb_tune)
        self.rb_live = QRadioButton("One frequency")
        self.rb_range = QRadioButton("A range")
        self.rb_live.setChecked(True)
        grp = QButtonGroup(self)
        row_modes = QWidget()
        rm = QHBoxLayout(row_modes)
        rm.setContentsMargins(0, 0, 0, 0)
        for rb in (self.rb_live, self.rb_range):
            grp.addButton(rb)
            rm.addWidget(rb)
            rb.toggled.connect(self._on_mode_change)
        gt.addWidget(row_modes, 0, 0, 1, 2)
        self.lbl_center = QLabel("Tune to MHz")
        gt.addWidget(self.lbl_center, 1, 0)
        self.sp_center = QDoubleSpinBox()
        self.sp_center.setRange(24, 1766)
        self.sp_center.setDecimals(4)
        self.sp_center.setSingleStep(0.025)
        self.sp_center.setValue(94.6)
        gt.addWidget(self.sp_center, 1, 1)
        self.lbl_from = QLabel("From MHz")
        gt.addWidget(self.lbl_from, 2, 0)
        self.sp_start = QDoubleSpinBox()
        self.sp_start.setRange(24, 1766)
        self.sp_start.setDecimals(3)
        self.sp_start.setValue(87.5)
        gt.addWidget(self.sp_start, 2, 1)
        self.lbl_to = QLabel("To MHz")
        gt.addWidget(self.lbl_to, 3, 0)
        self.sp_stop = QDoubleSpinBox()
        self.sp_stop.setRange(24, 1766)
        self.sp_stop.setDecimals(3)
        self.sp_stop.setValue(108.0)
        gt.addWidget(self.sp_stop, 3, 1)
        self.cb_country = QComboBox()
        for code, nm in bp.country_names():
            self.cb_country.addItem(nm, code)
        i = self.cb_country.findData(bp.DEFAULT_COUNTRY)
        if i >= 0:
            self.cb_country.setCurrentIndex(i)
        self.cb_country.setToolTip("Band plans differ by country: FM is 76-95 in "
                                   "Japan, licence-free UHF is different everywhere")
        self.cb_country.currentIndexChanged.connect(self._on_country_change)
        gt.addWidget(QLabel("Band plan"), 4, 0)
        gt.addWidget(self.cb_country, 4, 1)
        self.cb_band = QComboBox()
        self.cb_band.currentIndexChanged.connect(self._on_band_pick)
        gt.addWidget(self.cb_band, 5, 0, 1, 2)
        self.lbl_band = QLabel("-")
        self.lbl_band.setWordWrap(True)
        self.lbl_band.setFont(QFont("Consolas", 9))
        self.lbl_band.setStyleSheet(f"color: {theme.ACCENT};")
        gt.addWidget(self.lbl_band, 6, 0, 1, 2)
        for wdg in (self.sp_center, self.sp_start, self.sp_stop):
            wdg.valueChanged.connect(self._update_band_label)
        pl.addWidget(gb_tune)

        # --------------------------------------------------------- audio --
        gb_audio = QGroupBox("Audio")
        ga = QGridLayout(gb_audio)
        ga.addWidget(QLabel("Demodulate"), 0, 0)
        self.cb_demod = QComboBox()
        self.cb_demod.addItems(["Off", "WFM", "NFM", "AM"])
        ga.addWidget(self.cb_demod, 0, 1)
        self.ck_listen = QCheckBox("Listen" + ("" if _sd else "  (needs sounddevice)"))
        self.ck_listen.setToolTip("Ctrl+L")
        self.ck_listen.setEnabled(_sd is not None)
        self.ck_listen.setChecked(_sd is not None)
        ga.addWidget(self.ck_listen, 1, 0, 1, 2)
        pl.addWidget(gb_audio)

        # ------------------------------------------------------- scanner --
        self.gb_scan = QGroupBox("")
        gs = QGridLayout(self.gb_scan)
        gs.addWidget(QLabel("Channel step kHz"), 0, 0)
        self.cb_vstep = QComboBox()
        self.cb_vstep.addItems(["25", "8.333", "12.5", "50", "100", "200"])
        self.cb_vstep.setToolTip("8.333 kHz is the European airband grid; "
                                 "25 kHz elsewhere and for most other services")
        gs.addWidget(self.cb_vstep, 0, 1)
        gs.addWidget(QLabel("Squelch dB"), 1, 0)
        self.sp_squelch = QDoubleSpinBox()
        self.sp_squelch.setRange(2, 40)
        self.sp_squelch.setValue(8.0)
        self.sp_squelch.setSingleStep(0.5)
        gs.addWidget(self.sp_squelch, 1, 1)
        gs.addWidget(QLabel("Hang time s"), 2, 0)
        self.sp_hang = QDoubleSpinBox()
        self.sp_hang.setRange(0.2, 30.0)
        self.sp_hang.setValue(2.0)
        self.sp_hang.setSingleStep(0.5)
        gs.addWidget(self.sp_hang, 2, 1)
        gs.addWidget(QLabel("Discard under s"), 3, 0)
        self.sp_minlen = QDoubleSpinBox()
        self.sp_minlen.setRange(0.0, 10.0)
        self.sp_minlen.setValue(0.7)
        self.sp_minlen.setSingleStep(0.1)
        gs.addWidget(self.sp_minlen, 3, 1)
        self.ck_spurs = QCheckBox("Skip dongle spurs (24 / 28.8 MHz harmonics)")
        self.ck_spurs.setChecked(True)
        self.ck_spurs.setToolTip("120.000 MHz is 24 MHz x 5 - a permanent internal "
                                 "carrier that otherwise holds the squelch open")
        gs.addWidget(self.ck_spurs, 4, 0, 1, 2)
        self.b_outdir = QPushButton("Save to: rtlsdr-recordings")
        self.b_outdir.clicked.connect(self.on_pick_outdir)
        gs.addWidget(self.b_outdir, 5, 0, 1, 2)
        self.lbl_now = QLabel("idle")
        f = QFont("Consolas", 13)
        f.setWeight(QFont.DemiBold)
        self.lbl_now.setFont(f)
        self.lbl_now.setStyleSheet(f"color: {theme.MUTED};")
        gs.addWidget(self.lbl_now, 6, 0, 1, 2)
        self.bar_snr = QProgressBar()
        self.bar_snr.setRange(0, 40)
        self.bar_snr.setFormat("%v dB over floor")
        gs.addWidget(self.bar_snr, 7, 0, 1, 2)
        self.sec_scan = theme.Collapsible("Scanner options", self.gb_scan)
        pl.addWidget(self.sec_scan)

        # --------------------------------------------------------- start --
        gb_run = QGroupBox("Run")
        gr2 = QGridLayout(gb_run)
        self.b_start = QPushButton("Start")
        self.b_start.setShortcut(QKeySequence("Ctrl+Return"))
        self.b_start.setObjectName("primary")
        self.b_start.clicked.connect(lambda: self.on_start())
        self.b_stop = QPushButton("Stop")
        self.b_stop.setEnabled(False)
        self.b_stop.clicked.connect(self.on_stop)
        gr2.addWidget(self.b_start, 0, 0)
        gr2.addWidget(self.b_stop, 0, 1)
        pl.addWidget(gb_run)

        # ------------------------------------------------------ receiver --
        gb_rx = QGroupBox("")
        gr = QGridLayout(gb_rx)
        gr.addWidget(QLabel("Sample rate"), 0, 0)
        self.cb_fs = QComboBox()
        self.cb_fs.addItems(["2.4", "2.048", "1.8", "1.024"])
        gr.addWidget(self.cb_fs, 0, 1)
        gr.addWidget(QLabel("Gain dB"), 1, 0)
        self.cb_gain = QComboBox()
        self.cb_gain.addItem("auto")
        for g in [0.0, 8.7, 12.5, 16.6, 20.7, 25.4, 29.7, 32.8, 37.2, 40.2,
                  44.5, 48.0, 49.6]:
            self.cb_gain.addItem(str(g))
        self.cb_gain.setCurrentText("49.6")
        gr.addWidget(self.cb_gain, 1, 1)
        gr.addWidget(QLabel("ppm"), 2, 0)
        self.sp_ppm = QSpinBox()
        self.sp_ppm.setRange(-200, 200)
        self.sp_ppm.setValue(65)
        gr.addWidget(self.sp_ppm, 2, 1)
        gr.addWidget(QLabel("FFT size"), 3, 0)
        self.cb_nfft = QComboBox()
        self.cb_nfft.addItems(["1024", "2048", "4096", "8192", "16384"])
        self.cb_nfft.setCurrentText("4096")
        gr.addWidget(self.cb_nfft, 3, 1)
        gr.addWidget(QLabel("Dongle"), 4, 0)
        self.cb_dev = QComboBox()
        gr.addWidget(self.cb_dev, 4, 1)
        self.ck_peak = QCheckBox("Peak hold on the trace")
        gr.addWidget(self.ck_peak, 5, 0, 1, 2)
        self.sec_rx = theme.Collapsible("Receiver", gb_rx)
        pl.addWidget(self.sec_rx)

        # ------------------------------------------------------ autofind --
        gb_find = QGroupBox("Auto-find")
        gf = QGridLayout(gb_find)
        self.b_find = QPushButton("Scan this range for signals")
        self.b_find.setToolTip("Ctrl+F")
        self.b_find.clicked.connect(self.on_autofind)
        gf.addWidget(self.b_find, 0, 0, 1, 2)
        self.b_find_fm = QPushButton("Find + play the strongest")
        self.b_find_fm.clicked.connect(self.on_find_and_play)
        gf.addWidget(self.b_find_fm, 1, 0, 1, 2)
        self.b_airports = QPushButton("Airport frequencies near me")
        self.b_airports.setToolTip("Published tower, ground, approach and ATIS "
                                   "frequencies, so there is nothing to hunt for")
        self.b_airports.clicked.connect(self.on_airport_freqs)
        gf.addWidget(self.b_airports, 4, 0, 1, 2)
        self.cb_saved = QComboBox()
        self.cb_saved.setToolTip("Scans are kept, so a range you have already "
                                 "swept can be brought back without the radio")
        self.cb_saved.currentIndexChanged.connect(self.on_load_saved)
        gf.addWidget(self.cb_saved, 5, 0, 1, 2)
        self.lst_found = QListWidget()
        self.lst_found.setFont(QFont("Consolas", 9))
        self.lst_found.setMinimumHeight(110)
        self.lst_found.itemDoubleClicked.connect(self.on_tune_found)
        gf.addWidget(self.lst_found, 2, 0, 1, 2)
        self.b_tune = QPushButton("Tune to selected")
        self.b_tune.clicked.connect(
            lambda: self.on_tune_found(self.lst_found.currentItem()))
        self.lst_found.currentItemChanged.connect(self._on_found_selected)
        gf.addWidget(self.b_tune, 3, 0, 1, 2)
        pl.addWidget(gb_find)

        # -------------------------------------------------------- record --
        gb_rec = QGroupBox("")
        gc = QGridLayout(gb_rec)
        self.b_wav = QPushButton("Record audio")
        self.b_wav.setCheckable(True)
        self.b_wav.clicked.connect(self.on_rec_wav)
        self.b_iq = QPushButton("Record IQ")
        self.b_iq.setCheckable(True)
        self.b_iq.clicked.connect(self.on_rec_iq)
        self.b_csv = QPushButton("Trace CSV")
        self.b_csv.clicked.connect(self.on_save_csv)
        self.b_png = QPushButton("Chart PNG")
        self.b_png.clicked.connect(self.on_save_png)
        self.ck_autorec = QCheckBox("Auto-record every channel that opens")
        self.ck_autorec.setToolTip(
            "Over a range: stop on any channel above squelch, record it to its "
            "own file until it goes quiet, then carry on")
        self.ck_autorec.toggled.connect(self._on_mode_change)
        gc.addWidget(self.ck_autorec, 0, 0, 1, 2)
        gc.addWidget(self.b_wav, 1, 0)
        gc.addWidget(self.b_iq, 1, 1)
        gc.addWidget(self.b_csv, 2, 0)
        gc.addWidget(self.b_png, 2, 1)
        self.sec_rec = theme.Collapsible("Record", gb_rec, opened=True)
        pl.addWidget(self.sec_rec)

        self.lbl_cursor = QLabel("-")
        self.lbl_cursor.setFont(QFont("Consolas", 9))
        self.lbl_cursor.setStyleSheet(f"color: {theme.MUTED};")
        pl.addWidget(self.lbl_cursor)
        pl.addStretch(1)
        lay.addWidget(self._scrollable(panel))

        # ---------------------------------------------------------- plots --
        split = QSplitter(Qt.Vertical)
        self.plot = pg.PlotWidget()
        self.plot.setLabel("bottom", "Frequency", units="Hz")
        self.plot.setLabel("left", "Power", units="dB")
        self.plot.showGrid(x=True, y=True, alpha=0.18)
        self.curve = self.plot.plot(pen=pg.mkPen(theme.TRACE, width=1))
        self.curve_pk = self.plot.plot(
            pen=pg.mkPen(theme.PEAK, width=1, style=Qt.DashLine))
        self.vline = pg.InfiniteLine(angle=90, movable=False,
                                     pen=pg.mkPen("#4a5766", width=1))
        self.plot.addItem(self.vline, ignoreBounds=True)
        self.markers = pg.ScatterPlotItem(size=10, pen=pg.mkPen(theme.MARK),
                                          brush=pg.mkBrush(250, 204, 21, 140))
        self.plot.addItem(self.markers, ignoreBounds=True)
        split.addWidget(self.plot)

        self.wf_widget = pg.PlotWidget()
        self.wf_widget.setLabel("bottom", "Frequency", units="Hz")
        self.wf_widget.setLabel("left", "Time")
        self.wf_img = pg.ImageItem()
        self.wf_widget.addItem(self.wf_img)
        self.wf_img.setLookupTable(
            pg.colormap.get("inferno").getLookupTable(0.0, 1.0, 256))
        split.addWidget(self.wf_widget)

        bottom = QTabWidget()
        rec_page = QWidget()
        rv = QVBoxLayout(rec_page)
        rv.setContentsMargins(6, 6, 6, 6)
        self.tbl_rec = QTableWidget(0, 5)
        self.tbl_rec.setHorizontalHeaderLabels(
            ["Time", "Frequency", "Length", "Aircraft", "File"])
        self.tbl_rec.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.tbl_rec.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_rec.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl_rec.doubleClicked.connect(self.on_open_recording)
        rv.addWidget(self.tbl_rec)
        row = QHBoxLayout()
        b_open = QPushButton("Open folder")
        b_open.clicked.connect(
            lambda: os.startfile(self.scan_outdir())
            if os.path.isdir(self.scan_outdir()) else None)
        b_play = QPushButton("Play selected")
        b_play.clicked.connect(self.on_open_recording)
        b_ac = QPushButton("Aircraft for selected")
        b_ac.clicked.connect(self.on_show_aircraft)
        row.addWidget(b_open)
        row.addWidget(b_play)
        row.addWidget(b_ac)
        row.addStretch(1)
        rv.addLayout(row)
        bottom.addTab(rec_page, "Recordings")

        self.txt_log = QPlainTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setFont(QFont("Consolas", 9))
        self.txt_log.setMaximumBlockCount(2000)
        bottom.addTab(self.txt_log, "Activity log")
        split.addWidget(bottom)
        split.setSizes([420, 260, 240])
        lay.addWidget(split, 1)

        self.plot.scene().sigMouseMoved.connect(self._on_mouse)
        self._outdir = DEFAULT_REC_DIR
        self.b_outdir.setText("Save to: " + os.path.basename(self._outdir))
        self._rebuild_band_list()
        self._on_mode_change()
        return page

    def _populate_devices(self):
        n = device_count()
        for cb in (self.cb_dev, self.cb_adev):
            cb.clear()
            for i in range(n):
                cb.addItem(f"{i}: {device_name(i)}", i)
            if not n:
                cb.addItem("no dongle found", 0)
            cb.setEnabled(n > 1)
        if n > 1:
            self.cb_adev.setCurrentIndex(1)     # keep the two modes apart
        self.statusBar().showMessage(
            f"{n} dongle(s) detected" if n else
            "no RTL-SDR found - check the WinUSB driver")

    def _update_band_label(self, *_):
        code = self.country()
        if self.rb_live.isChecked():
            f = self.sp_center.value() * 1e6
            name = bp.label_for(f, code) or "no named band"
            note = bp.note_for(f, code)
            txt = f"{self.sp_center.value():.4f} MHz  ·  {name}"
            self.lbl_band.setText(txt + ("\n" + note if note else ""))
            return
        else:
            lo, hi = self.sp_start.value(), self.sp_stop.value()
            names = [b.name for b in bp.bands_for(code) if not (b.hi < lo or b.lo > hi)]
            covers = ", ".join(names) if names else "no named band"
            self.lbl_band.setText(
                f"{lo:.3f} – {hi:.3f} MHz  ({hi - lo:.3f} wide)\n{covers}")

    def scanning(self):
        """A range with auto-record on is what the scanner is."""
        return self.rb_range.isChecked() and self.ck_autorec.isChecked()

    def _on_mode_change(self, *_):
        """Show only the fields the current choice actually uses."""
        live = self.rb_live.isChecked()
        scan = self.scanning()
        self.lbl_center.setVisible(live)
        self.sp_center.setVisible(live)
        for w in (self.lbl_from, self.sp_start, self.lbl_to, self.sp_stop):
            w.setVisible(not live)
        if hasattr(self, "sec_scan"):
            self.sec_scan.setVisible(scan)
        if hasattr(self, "ck_autorec"):
            self.ck_autorec.setEnabled(not live)
            if live and self.ck_autorec.isChecked():
                self.ck_autorec.setChecked(False)
        # recording by hand is for one frequency; over a range the scanner
        # writes the files itself
        self.b_iq.setEnabled(not scan)
        self.b_wav.setEnabled(not scan)
        # The button is Start. It was relabelled Listen / Sweep / Scan, which
        # contradicted the Listen checkbox sitting right above it: you could
        # untick Listen and the button still said Listen. What it will do goes
        # in the tooltip, where it cannot argue with a control.
        self.b_start.setToolTip(
            "Scan the range and record every channel that opens" if scan else
            ("Sweep the range and draw the spectrum" if self.rb_range.isChecked()
             else "Tune this frequency and start receiving"))
        self._update_band_label()

    def _tab_aircraft(self):
        page = QWidget()
        lay = QHBoxLayout(page)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(8)

        panel = QWidget()
        pl = QVBoxLayout(panel)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(8)

        gb = QGroupBox("ADS-B  1090 MHz")
        g = QGridLayout(gb)
        info = QLabel("Aircraft broadcast identity, altitude and position "
                      "unencrypted for collision avoidance. Frames are "
                      "CRC-checked before being believed.")
        info.setWordWrap(True)
        info.setStyleSheet(f"color: {theme.MUTED};")
        g.addWidget(info, 0, 0, 1, 2)
        self.b_astart = QPushButton("Start receiving")
        self.b_astart.setObjectName("primary")
        self.b_astart.clicked.connect(self.on_adsb_start)
        self.b_astop = QPushButton("Stop"); self.b_astop.setEnabled(False)
        self.b_astop.clicked.connect(self.on_adsb_stop)
        g.addWidget(QLabel("Dongle"), 1, 0)
        self.cb_adev = QComboBox()
        self.cb_adev.setToolTip("With a second dongle, ADS-B and the radio tab "
                                "can run at the same time")
        g.addWidget(self.cb_adev, 1, 1)
        g.addWidget(self.b_astart, 2, 0); g.addWidget(self.b_astop, 2, 1)
        b_rel = QPushButton("Release device (clear stray rtl_adsb)")
        b_rel.setToolTip("Use if the dongle reports usb_open error -3 after a crash")
        b_rel.clicked.connect(self.on_release_device)
        g.addWidget(b_rel, 3, 0, 1, 2)
        pl.addWidget(gb)

        gb2 = QGroupBox("Link quality")
        g2 = QGridLayout(gb2)
        self.lbl_frames = QLabel("0")
        self.lbl_valid = QLabel("0")
        self.lbl_pct = QLabel("0.0 %")
        self.lbl_count = QLabel("0")
        self.lbl_pos = QLabel("0 / 0")
        for i, (k, w) in enumerate((("frames seen", self.lbl_frames),
                                    ("usable", self.lbl_valid),
                                    ("yield", self.lbl_pct),
                                    ("aircraft", self.lbl_count),
                                    ("with position", self.lbl_pos))):
            lab = QLabel(k); lab.setStyleSheet(f"color: {theme.MUTED};")
            w.setFont(QFont("Consolas", 12))
            g2.addWidget(lab, i, 0); g2.addWidget(w, i, 1)
        pl.addWidget(gb2)

        gb3 = QGroupBox("Selected aircraft")
        g3 = QVBoxLayout(gb3)
        b_map = QPushButton("Open position in Google Maps")
        b_map.setObjectName("primary")
        b_map.clicked.connect(lambda: self._open_ac_url("maps"))
        b_track = QPushButton("Open on ADSBexchange")
        b_track.clicked.connect(lambda: self._open_ac_url("track"))
        b_exp = QPushButton("Export table to CSV")
        b_exp.clicked.connect(self.on_export_aircraft)
        for b in (b_map, b_track, b_exp):
            g3.addWidget(b)
        pl.addWidget(gb3)

        gb_rx = QGroupBox("Your position")
        grx = QGridLayout(gb_rx)
        note = QLabel("Set this to draw range rings and get distance and "
                      "bearing to each aircraft. Nothing is sent anywhere.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.MUTED};")
        grx.addWidget(note, 0, 0, 1, 2)
        grx.addWidget(QLabel("Latitude"), 1, 0)
        self.sp_rxlat = QDoubleSpinBox()
        self.sp_rxlat.setRange(-90, 90); self.sp_rxlat.setDecimals(5)
        self.sp_rxlat.setValue(50.0)
        grx.addWidget(self.sp_rxlat, 1, 1)
        grx.addWidget(QLabel("Longitude"), 2, 0)
        self.sp_rxlon = QDoubleSpinBox()
        self.sp_rxlon.setRange(-180, 180); self.sp_rxlon.setDecimals(5)
        self.sp_rxlon.setValue(15.0)
        grx.addWidget(self.sp_rxlon, 2, 1)
        self.ck_showrx = QCheckBox("Show my position on the map")
        self.ck_showrx.toggled.connect(self._apply_receiver)
        grx.addWidget(self.ck_showrx, 3, 0, 1, 2)
        self.sp_rxlat.valueChanged.connect(self._apply_receiver)
        self.sp_rxlon.valueChanged.connect(self._apply_receiver)
        self.ck_follow = QCheckBox("Auto-fit map to aircraft")
        self.ck_follow.setChecked(True)
        self.ck_follow.toggled.connect(lambda v: self.map.set_follow(v))
        self.ck_follow.setToolTip("Turns itself off as soon as you pan or zoom, "
                                  "so the refit cannot fight you")
        grx.addWidget(self.ck_follow, 4, 0, 1, 2)

        zrow = QWidget()
        zl = QHBoxLayout(zrow)
        zl.setContentsMargins(0, 0, 0, 0)
        b_fit = QPushButton("Fit")
        b_fit.clicked.connect(lambda: (self.map.fit_now(),
                                       self.ck_follow.setChecked(True)))
        b_in = QPushButton("Zoom in")
        b_in.clicked.connect(lambda: self.map.zoom(0.7))
        b_out = QPushButton("Zoom out")
        b_out.clicked.connect(lambda: self.map.zoom(1 / 0.7))
        for b in (b_fit, b_in, b_out):
            zl.addWidget(b)
        grx.addWidget(zrow, 5, 0, 1, 2)

        lrow = QWidget()
        ll = QHBoxLayout(lrow)
        ll.setContentsMargins(0, 0, 0, 0)
        for text, mode in (("Map", "map"), ("Split", "split"), ("Table", "table")):
            b = QPushButton(text)
            b.setToolTip("Give the pane more room")
            b.clicked.connect(lambda _=False, m=mode: self.set_ac_layout(m))
            ll.addWidget(b)
        grx.addWidget(lrow, 7, 0, 1, 2)

        self.ck_tiles = QCheckBox("Online map tiles")
        self.ck_tiles.setChecked(True)
        self.ck_tiles.setToolTip("Uncheck to work offline from bundled outlines")
        self.ck_tiles.toggled.connect(self._on_tiles_toggled)
        grx.addWidget(self.ck_tiles, 9, 0, 1, 2)
        self.cb_tilesrc = QComboBox()
        for src in TILE_SOURCES:
            self.cb_tilesrc.addItem(src.label, src.key)
        self.cb_tilesrc.currentIndexChanged.connect(self._on_tile_source)
        grx.addWidget(self.cb_tilesrc, 10, 0, 1, 2)
        self.ck_darkmap = QCheckBox("Darken basemap to match the theme")
        self.ck_darkmap.setChecked(True)
        self.ck_darkmap.toggled.connect(lambda v: self.map.set_dark_map(v))
        grx.addWidget(self.ck_darkmap, 12, 0, 1, 2)
        self.lbl_attrib = QLabel("")
        self.lbl_attrib.setWordWrap(True)
        self.lbl_attrib.setStyleSheet(f"color: {theme.MUTED}; font-size: 10px;")
        grx.addWidget(self.lbl_attrib, 11, 0, 1, 2)
        self.lbl_span = QLabel("-")
        self.lbl_span.setStyleSheet(f"color: {theme.MUTED};")
        self.lbl_span.setFont(QFont("Consolas", 9))
        grx.addWidget(self.lbl_span, 6, 0, 1, 2)
        self.lbl_acinfo = QLabel("no aircraft selected")
        self.lbl_acinfo.setWordWrap(True)
        self.lbl_acinfo.setFont(QFont("Consolas", 9))
        grx.addWidget(self.lbl_acinfo, 8, 0, 1, 2)
        pl.addWidget(gb_rx)

        gb4 = QGroupBox("Log")
        g4 = QVBoxLayout(gb4)
        self.txt_alog = QPlainTextEdit()
        self.txt_alog.setReadOnly(True)
        self.txt_alog.setFont(QFont("Consolas", 9))
        self.txt_alog.setMaximumBlockCount(500)
        g4.addWidget(self.txt_alog)
        pl.addWidget(gb4, 1)
        lay.addWidget(self._scrollable(panel))

        self.tbl_ac = QTableWidget(0, 12)
        self.tbl_ac.setHorizontalHeaderLabels(
            ["ICAO", "Callsign", "Country", "Squawk", "Alt ft", "Speed kt",
             "Track", "V/S fpm", "Latitude", "Longitude", "Dist km", "Msgs"])
        self.tbl_ac.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_ac.setSelectionBehavior(QAbstractItemView.SelectRows)
        # rows are kept in first-seen order and updated in place; re-sorting
        # live data moves the row out from under the user's selection
        self.tbl_ac.setSortingEnabled(False)
        self.tbl_ac.horizontalHeader().setStretchLastSection(True)
        self.tbl_ac.setFont(QFont("Consolas", 10))
        self.tbl_ac.itemSelectionChanged.connect(self._on_ac_selected)

        self.map = MapWidget()
        self.map.aircraftClicked.connect(self._on_map_click)
        self.map.followChanged.connect(self.ck_follow.setChecked)
        self.map.status.connect(self.statusBar().showMessage)
        self.lbl_attrib.setText(self.map.attribution())
        self.map.getViewBox().sigRangeChanged.connect(self._update_span)
        self._update_span()

        right = QSplitter(Qt.Vertical)
        right.addWidget(self.map)
        right.addWidget(self.tbl_ac)
        self.map.setMinimumHeight(200)
        self.tbl_ac.setMinimumHeight(240)
        right.setStretchFactor(0, 1)
        right.setStretchFactor(1, 1)
        right.setSizes([430, 530])          # table is the thing you read
        self._right_split = right
        lay.addWidget(right, 1)
        return page

    def set_ac_layout(self, mode):
        """Collapse the aircraft tab toward the map, the table, or an even split."""
        total = max(sum(self._right_split.sizes()), 600)
        if mode == "map":
            self._right_split.setSizes([total, 0])
        elif mode == "table":
            self._right_split.setSizes([0, total])
        else:
            self._right_split.setSizes([int(total * 0.45), int(total * 0.55)])

    def _on_tiles_toggled(self, on):
        self.map.set_tiles(on)
        self.cb_tilesrc.setEnabled(on)
        self.lbl_attrib.setText(self.map.attribution())

    def _on_tile_source(self, idx):
        key = self.cb_tilesrc.itemData(idx)
        if key:
            self.map.set_tile_source(key)
            self.lbl_attrib.setText(self.map.attribution())

    def _update_span(self, *_):
        km = self.map.span_km()
        self.lbl_span.setText(f"view width  {km:,.0f} km" if km < 5000
                              else f"view width  {km/1000:,.1f} thousand km")

    def _apply_receiver(self, *_):
        if getattr(self, "ck_showrx", None) is None or not hasattr(self, "map"):
            return
        if self.ck_showrx.isChecked():
            self.map.set_receiver(self.sp_rxlat.value(), self.sp_rxlon.value())
        else:
            self.map.set_receiver(None, None)

    @staticmethod
    def _beyond_horizon(ac, dist_km):
        if ac.altitude is None:
            return False
        return dist_km > 1.15 * radio_horizon_km(ac.altitude)

    def _on_map_click(self, icao):
        for r in range(self.tbl_ac.rowCount()):
            it = self.tbl_ac.item(r, 0)
            if it and it.text() == icao:
                self.tbl_ac.selectRow(r)
                self.tbl_ac.scrollToItem(it)
                break

    def _on_ac_selected(self):
        it = self.tbl_ac.currentItem()
        if it is None:
            return
        icao = it.data(Qt.UserRole)
        ac = getattr(self, "_ac_index", {}).get(icao)
        if ac is None:
            return
        lines = [f"{ac.icao}  {ac.callsign or ''}".strip()]
        if ac.country:
            lines.append(ac.country)
        info = self.map.info_for(icao)
        if info:
            d, b = info
            lines.append(f"{d:.0f} km  bearing {b:.0f}°")
        elif ac.lat is None:
            lines.append("no position fix yet")
        else:
            lines.append("set your position for range")
        if ac.squawk:
            lines.append(f"squawk {ac.squawk}" +
                         (f"  {ac.squawk_meaning}" if ac.squawk_meaning else ""))
        self.lbl_acinfo.setText("\n".join(lines))

    # ------------------------------------------------------------ helpers --
    @staticmethod
    def _scrollable(panel, width=320):
        """
        Side panels carry more controls than fit a laptop screen, so they
        scroll rather than running off the bottom edge.
        """
        panel.setFixedWidth(width)
        sa = QScrollArea()
        sa.setWidget(panel)
        sa.setWidgetResizable(True)
        sa.setFixedWidth(width + 14)
        sa.setFrameShape(QFrame.NoFrame)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sa.setStyleSheet("QScrollArea, QScrollArea > QWidget, "
                         "QScrollArea > QWidget > QWidget { background: transparent; }")
        return sa

    def scan_outdir(self):
        return self._outdir

    def country(self):
        return self.cb_country.currentData() or bp.DEFAULT_COUNTRY

    def _rebuild_band_list(self):
        """
        Bands and named channels share one combo: a country's ATC or weather
        frequencies are the thing you actually want to jump to, and giving
        them their own control would be one more box to read.
        """
        self._bands = bp.bands_for(self.country())
        self._channels = bp.channels_for(self.country())
        self.cb_band.blockSignals(True)
        self.cb_band.clear()
        self.cb_band.addItem("- jump to a band or channel -")
        for b in self._bands:
            self.cb_band.addItem(f"{b.name}   {b.lo:g}-{b.hi:g}")
        self.cb_band.addItem("Full tuner range   24-1766")
        self.cb_band.insertSeparator(self.cb_band.count())
        for c in self._channels:
            self.cb_band.addItem(f"{c.mhz:>9.4f}  {c.name}")
        self.cb_band.blockSignals(False)

    def _on_country_change(self, *_):
        self._rebuild_band_list()
        self._update_band_label()
        warn = bp.warning_for(self.country())
        if warn:
            self.statusBar().showMessage(warn, 15000)

    def _on_band_pick(self, idx):
        if idx <= 0:
            return
        n_bands = len(self._bands)
        if idx - 1 < n_bands:                            # a band: set the range
            b = self._bands[idx - 1]
            self.sp_start.setValue(b.lo)
            self.sp_stop.setValue(b.hi)
            self.sp_center.setValue(round((b.lo + b.hi) / 2, 4))
            if b.demod != "Off":
                self.cb_demod.setCurrentText(b.demod)
        elif idx - 1 == n_bands:                         # full tuner range
            self.sp_start.setValue(24.0)
            self.sp_stop.setValue(1766.0)
        else:                                            # a channel: tune it
            ch_i = idx - n_bands - 3                     # skip full-range + separator
            if 0 <= ch_i < len(self._channels):
                c = self._channels[ch_i]
                self.rb_live.setChecked(True)
                self.sp_center.setValue(c.mhz)
                self.cb_demod.setCurrentText(c.demod)
                if c.demod == "Off":
                    # a digital channel: audio would be meaningless here
                    self.statusBar().showMessage(
                        f"{c.name} - {c.mhz:.4f} MHz is digital; "
                        f"use the Aircraft tab for 1090 MHz", 10000)
                else:
                    self.statusBar().showMessage(f"{c.name} - {c.mhz:.4f} MHz")
        self._update_band_label()

    def _log(self, widget, text):
        widget.appendPlainText(text)

    # ------------------------------------------------------ spectrum ctrl --
    def on_start(self, find_mode=False):
        if self._busy():
            return
        if self.scanning() and not find_mode:
            return self.on_scan_start()
        sweep = find_mode or self.rb_range.isChecked()
        if sweep and self.sp_stop.value() <= self.sp_start.value():
            QMessageBox.warning(self, "Bad span", "Sweep stop must be above sweep start.")
            return
        if (not sweep and self.ck_listen.isChecked()
                and self.cb_demod.currentText() == "Off"):
            self.cb_demod.setCurrentText(bp.demod_for(self.sp_center.value() * 1e6, self.country()))
            self.statusBar().showMessage(
                f"Listen was on with no demodulator - selected "
                f"{self.cb_demod.currentText()}")

        w = SdrWorker()
        w.find_mode = find_mode
        w.mode = "sweep" if sweep else "live"
        w.center = self.sp_center.value() * 1e6
        w.f_start = self.sp_start.value() * 1e6
        w.f_stop = self.sp_stop.value() * 1e6
        w.fs = float(self.cb_fs.currentText()) * 1e6
        w.gain = self.cb_gain.currentText()
        w.ppm = self.sp_ppm.value()
        w.device = self.cb_dev.currentData() or 0
        w.nfft = int(self.cb_nfft.currentText())
        w.demod = self.cb_demod.currentText()
        w.spectrum.connect(self.on_spectrum)
        w.status.connect(self.statusBar().showMessage)
        w.progress.connect(self.prog.setValue)
        w.failed.connect(self.on_failed)
        w.audio.connect(self.on_audio)
        w.signalsFound.connect(self.on_signals_found)
        w.deviceInfo.connect(lambda s: self.header.pill_dev.set(s, theme.GOOD))
        w.finished.connect(self._spectrum_finished)
        self.worker = w
        self.peak_hold = None
        self.wf = None
        if self.ck_listen.isChecked():
            self._open_audio()
        w.start()
        self.header.pill_state.set("sweeping" if sweep else "live", theme.ACCENT)
        self.b_start.setEnabled(False)
        self.b_stop.setEnabled(True)
        self.b_find.setEnabled(False)

    def on_stop(self):
        """
        One Stop for every Radio-tab mode. The scanner runs in self.scanner,
        not self.worker, so stopping only the latter left it scanning behind a
        header that said idle and a _busy() that blocked every other mode.
        """
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(5000)
        if self.scanner and self.scanner.isRunning():
            self.scanner.stop()
            self.scanner.wait(8000)
        self._spectrum_finished()
        self._scan_finished()

    def _wire_live_controls(self):
        """
        Controls that take effect while something is running.

        Everything here is read by the worker on each block, so writing the
        attribute is enough; only the mode and the tuning need a restart, and
        those are locked while a job runs.
        """
        self.ck_listen.toggled.connect(self._on_listen_toggled)
        self.cb_demod.currentTextChanged.connect(self._on_demod_changed)
        self.sp_squelch.valueChanged.connect(
            lambda v: self._set_scanner("squelch", v))
        self.sp_hang.valueChanged.connect(
            lambda v: self._set_scanner("hang", v))
        self.sp_minlen.valueChanged.connect(
            lambda v: self._set_scanner("min_len", v))
        self.ck_spurs.toggled.connect(
            lambda v: self._set_scanner("skip_spurs", v))

    def _set_scanner(self, attr, value):
        if self.scanner and self.scanner.isRunning():
            setattr(self.scanner, attr, value)

    def _on_listen_toggled(self, on):
        running = bool((self.worker and self.worker.isRunning()) or
                       (self.scanner and self.scanner.isRunning()))
        if on and running and self._audio_out is None:
            self._open_audio()
        elif not on:
            self._close_audio()
        self._set_scanner("monitor", on)

    def _on_demod_changed(self, mode):
        # the worker rebuilds its Demodulator when the mode name changes
        if self.worker and self.worker.isRunning():
            self.worker.demod = mode
        self._set_scanner("demod", mode)
        if mode == "Off":
            self._close_audio()
        elif self.ck_listen.isChecked():
            self._on_listen_toggled(True)

    def _refresh_state_pill(self):
        """The header reports what is actually running, never what we assumed."""
        if self.scanner and self.scanner.isRunning():
            self.header.pill_state.set("scanning", theme.WARN)
        elif self.adsb and self.adsb.isRunning():
            self.header.pill_state.set("ADS-B", theme.GOOD)
        elif self.worker and self.worker.isRunning():
            self.header.pill_state.set(
                "sweeping" if self.worker.mode == "sweep" else "listening",
                theme.ACCENT)
        else:
            self.header.pill_state.set("idle", theme.MUTED)

    def _spectrum_finished(self):
        self._close_audio()
        self.b_start.setEnabled(True)
        self.b_stop.setEnabled(False)
        self.b_find.setEnabled(True)
        self.b_iq.setChecked(False)
        self.b_wav.setChecked(False)
        self._refresh_state_pill()

    def on_autofind(self):
        self.lst_found.clear()
        self.statusBar().showMessage("scanning for signals...")
        self.on_start(find_mode=True)

    def on_airport_freqs(self):
        """
        List the published frequencies of nearby airports instead of scanning
        for them. Sweeping a 19 MHz band finds a channel only while somebody
        is talking on it; the published list is there whether or not they are.
        """
        lat, lon = self.sp_rxlat.value(), self.sp_rxlon.value()
        if not airports.cached():
            ok = QMessageBox.question(
                self, "Download airport data",
                "Airport frequencies come from the public-domain OurAirports "
                "dataset, about 12 MB to fetch and 2.4 MB once cached.\n\n"
                "Download it now?",
                QMessageBox.Yes | QMessageBox.No)
            if ok != QMessageBox.Yes:
                return
            self.b_airports.setEnabled(False)
            try:
                airports.download(progress=self.statusBar().showMessage)
            except Exception as exc:
                QMessageBox.warning(self, "Download failed",
                                    f"Could not fetch the airport data:\n{exc}")
                return
            finally:
                self.b_airports.setEnabled(True)

        rows = airports.channels_near(lat, lon, radius_km=150.0,
                                      max_airports=12, limit=80)
        self.lst_found.clear()
        self.markers.setData([], [])
        if not rows:
            QMessageBox.information(
                self, "Nothing within range",
                f"No airport with a published frequency within 150 km of "
                f"{lat:.4f}, {lon:.4f}.\n\nSet your position on the Aircraft tab.")
            return
        for mhz, label, dist in rows:
            self.lst_found.addItem(f"{mhz:9.4f} MHz  {dist:4.0f} km  {label}")
        self.lst_found.setCurrentRow(0)
        self.cb_demod.setCurrentText("AM")
        self.statusBar().showMessage(
            f"{len(rows)} published frequencies within 150 km of "
            f"{lat:.4f}, {lon:.4f} - double-click one to tune it")

    def on_find_and_play(self):
        self._play_after_find = True
        self.on_autofind()

    def _refresh_saved_list(self):
        self._saved = scanstore.all_scans()
        self.cb_saved.blockSignals(True)
        self.cb_saved.clear()
        if self._saved:
            self.cb_saved.addItem(f"- {len(self._saved)} saved scans -")
            for sc in self._saved:
                self.cb_saved.addItem(scanstore.label(sc))
        else:
            self.cb_saved.addItem("- no saved scans yet -")
        self.cb_saved.blockSignals(False)

    def on_load_saved(self, idx):
        """Redraw a stored scan: its peaks and its trace, no tuning involved."""
        if idx <= 0 or idx - 1 >= len(getattr(self, "_saved", [])):
            return
        sc = self._saved[idx - 1]
        tf, td = sc.get("trace_freqs"), sc.get("trace_db")
        if tf and td:
            self.on_spectrum(np.asarray(tf, dtype=np.float64),
                             np.asarray(td, dtype=np.float32))
        # hits are stored in Hz, the same units the live path emits
        self._show_hits([(f, snr) for f, snr in sc.get("hits", [])])
        self.statusBar().showMessage(
            f"saved scan {scanstore.label(sc)} - reloaded without tuning")

    def _show_hits(self, hits):
        """Render a set of hits, whether they came from the radio or a file."""
        self.found = hits
        self.lst_found.clear()
        for f, snr in sorted(hits, key=lambda t: -t[1]):
            lbl = bp.label_for(f, self.country())
            self.lst_found.addItem(f"{f/1e6:10.4f} MHz  {snr:5.1f} dB  {lbl}")
        if not hits:
            self.markers.setData([], [])
            return
        self.lst_found.setCurrentRow(0)
        xs = [f for f, _ in hits]
        ys = [0.0] * len(xs)
        if self.last_trace is not None:
            F, D = self.last_trace
            pos = np.searchsorted(F, xs)            # F is ascending by construction
            pos = np.clip(pos, 0, len(D) - 1)
            ys = [float(D[i]) for i in pos]
        self.markers.setData(xs, ys)

    @Slot(object)
    def on_signals_found(self, hits):
        if hits:
            F, D = self.last_trace if self.last_trace is not None else (None, None)
            try:
                scanstore.save(self.sp_start.value() * 1e6,
                               self.sp_stop.value() * 1e6,
                               self.cb_country.currentText(), hits, F, D)
                self._refresh_saved_list()
            except OSError:
                pass
        self._show_hits(hits)
        if getattr(self, "_play_after_find", False):
            self._play_after_find = False
            if hits:
                best = max(hits, key=lambda t: t[1])
                QTimer.singleShot(400, lambda: self._tune_and_play(best[0]))
            else:
                QMessageBox.information(self, "Nothing found",
                                        "No signal cleared the threshold in that range.")

    def _on_found_selected(self, item, _prev=None):
        if item is None:
            self.b_tune.setText("Tune to selected")
            return
        try:
            mhz = float(item.text().split()[0])
        except (ValueError, IndexError):
            return
        self.b_tune.setText(f"Tune to {mhz:.4f} MHz")
        self.markers.setSymbol("o")

    def on_tune_found(self, item):
        if item is None:
            self.statusBar().showMessage("Select a signal in the list first", 4000)
            return
        try:
            mhz = float(item.text().split()[0])
        except (ValueError, IndexError):
            return
        self._tune_and_play(mhz * 1e6)

    def _tune_and_play(self, f_hz):
        self.on_stop()
        self.rb_live.setChecked(True)
        self.sp_center.setValue(f_hz / 1e6)
        self.cb_demod.setCurrentText(bp.demod_for(f_hz, self.country()))
        if _sd is not None:
            self.ck_listen.setChecked(True)
        QTimer.singleShot(250, lambda: self.on_start())

    # ---------------------------------------------------------- plotting --
    MAX_PLOT_POINTS = 8192          # a 4K screen has nothing like this many
    MAX_RECORDING_ROWS = 500        # the log rolls; the WAVs stay on disk

    @Slot(object, object)
    def on_spectrum(self, freqs, db):
        # Keep the full-resolution trace for the cursor and marker lookups,
        # but never hand more than a screenful to the plot or the waterfall.
        self.last_trace = (freqs, db)
        if len(db) > self.MAX_PLOT_POINTS:
            freqs, db = decimate_peak(freqs, db, self.MAX_PLOT_POINTS)
        self.curve.setData(freqs, db)
        if self.ck_peak.isChecked():
            if self.peak_hold is None or len(self.peak_hold) != len(db):
                self.peak_hold = db.copy()
            else:
                np.maximum(self.peak_hold, db, out=self.peak_hold)
            self.curve_pk.setData(freqs, self.peak_hold)
        else:
            self.curve_pk.setData([], [])
            self.peak_hold = None

        if self.wf is None or self.wf.shape[1] != len(db):
            self.wf = np.full((self.wf_rows, len(db)), float(db.min()), dtype=np.float32)
        self.wf[:-1] = self.wf[1:]
        self.wf[-1] = db
        # a sample is indistinguishable here and avoids 357 M elements a frame
        sample = self.wf[::8, ::max(1, self.wf.shape[1] // 2048)]
        lo, hi = np.percentile(sample, 5), np.percentile(sample, 99.5)
        if hi - lo < 1.0:
            hi = lo + 1.0
        self.wf_img.setImage(self.wf.T, autoLevels=False, levels=(lo, hi))
        self.wf_img.setRect(QRectF(float(freqs[0]), 0.0,
                                   float(freqs[-1] - freqs[0]), float(self.wf_rows)))
        self.wf_widget.setXRange(float(freqs[0]), float(freqs[-1]), padding=0)

    def _on_mouse(self, pos):
        if not self.plot.sceneBoundingRect().contains(pos):
            return
        p = self.plot.getPlotItem().vb.mapSceneToView(pos)
        self.vline.setPos(p.x())
        txt = f"cursor {p.x()/1e6:11.4f} MHz"
        if self.last_trace is not None:
            F, D = self.last_trace
            i = int(np.clip(np.searchsorted(F, p.x()), 0, len(D) - 1))
            lbl = bp.label_for(F[i], self.country())
            txt += f"\ntrace  {F[i]/1e6:11.4f} MHz  {D[i]:7.1f} dB"
            if lbl:
                txt += f"\nband   {lbl}"
        self.lbl_cursor.setText(txt)

    # ------------------------------------------------------------- audio --
    def _open_audio(self):
        if _sd is None:
            return
        try:
            self._audio_out = AudioSink()
            self._audio_out.start()
        except Exception as exc:
            self.statusBar().showMessage(f"audio output unavailable: {exc}")
            self._audio_out = None

    def _close_audio(self):
        if self._audio_out is not None:
            self._audio_out.stop()
            self._audio_out = None

    @Slot(object)
    def on_audio(self, aud):
        if self._audio_out is not None:
            self._audio_out.write(aud)

    # -------------------------------------------------------- recording ---
    def _busy(self, want=None):
        """
        A tuner can be at one frequency at a time, so ADS-B at 1090 MHz and
        voice at 118-137 MHz cannot share one dongle. With two plugged in they
        run happily in parallel, so only block on a genuine collision.
        """
        if want is None:
            want = self.cb_dev.currentData() or 0
        for w in (self.worker, self.scanner, self.adsb):
            if w and w.isRunning() and getattr(w, "device", 0) == want:
                QMessageBox.information(
                    self, "Dongle busy",
                    f"Device {want} is already running another mode.\n\n"
                    f"Stop it, or select a second dongle if you have one.")
                return True
        return False

    def on_rec_iq(self, checked):
        if checked and not (self.worker and self.worker.isRunning()):
            self.b_iq.setChecked(False)
            self.statusBar().showMessage("Press Start first, then record", 4000)
            return
        if checked:
            p, _ = QFileDialog.getSaveFileName(self, "Record raw IQ", "capture.bin",
                                               "Raw IQ (*.bin)")
            if not p:
                self.b_iq.setChecked(False); return
            self.worker.start_iq(p)
        elif self.worker:
            self.worker.stop_iq()

    def on_rec_wav(self, checked):
        # shared by the Spectrum tab button and the Listen button
        btn = self.sender() if isinstance(self.sender(), QPushButton) else self.b_wav
        if checked and not (self.worker and self.worker.isRunning()):
            btn.setChecked(False)
            self.statusBar().showMessage("Press Start first, then record", 4000)
            return
        if checked and getattr(self.worker, "demod", "Off") == "Off":
            btn.setChecked(False)
            self.statusBar().showMessage(
                "Choose WFM, NFM or AM before recording audio", 5000)
            return
        if checked:
            default = (f"{self.worker.center/1e6:.4f}MHz.wav"
                       if self.worker else "audio.wav")
            p, _ = QFileDialog.getSaveFileName(self, "Record audio", default,
                                               "WAV (*.wav)")
            if not p:
                btn.setChecked(False); return
            self.worker.start_wav(p)
        elif self.worker:
            self.worker.stop_wav()

    def on_save_csv(self):
        if self.last_trace is None:
            self.statusBar().showMessage("Nothing to save yet - run a sweep first", 4000)
            return
        p, _ = QFileDialog.getSaveFileName(self, "Save trace", "spectrum.csv", "CSV (*.csv)")
        if not p:
            return
        F, D = self.last_trace
        with open(p, "w", newline="") as fh:
            wr = csv.writer(fh)
            wr.writerow(["freq_hz", "power_db"])
            for f, d in zip(F, D):
                wr.writerow([f"{f:.0f}", f"{d:.2f}"])
        self.statusBar().showMessage(f"saved {len(F)} bins to {p}")

    def on_save_png(self):
        p, _ = QFileDialog.getSaveFileName(self, "Save chart", "spectrum.png", "PNG (*.png)")
        if p:
            pg.exporters.ImageExporter(self.plot.getPlotItem()).export(p)
            self.statusBar().showMessage(f"saved chart to {p}")

    def on_failed(self, msg):
        QMessageBox.critical(self, "SDR error", msg)
        self.on_stop()

    # ---------------------------------------------------------- scanner ---
    def on_pick_outdir(self):
        d = QFileDialog.getExistingDirectory(self, "Recordings folder", self._outdir)
        if d:
            self._outdir = d
            self.b_outdir.setText("Save to: " + os.path.basename(d))

    def on_scan_start(self):
        if self._busy():
            return
        if self.sp_stop.value() <= self.sp_start.value():
            QMessageBox.warning(self, "Bad span", "Stop must be above start.")
            return
        s = ScannerWorker()
        s.f_start = self.sp_start.value() * 1e6
        s.f_stop = self.sp_stop.value() * 1e6
        s.step = float(self.cb_vstep.currentText()) * 1e3
        s.squelch = self.sp_squelch.value()
        s.hang = self.sp_hang.value()
        s.min_len = self.sp_minlen.value()
        s.demod = self.cb_demod.currentText()
        s.gain = self.cb_gain.currentText()
        s.ppm = self.sp_ppm.value()
        s.device = self.cb_dev.currentData() or 0
        s.out_dir = self._outdir
        s.skip_spurs = self.ck_spurs.isChecked()
        s.activity.connect(self.on_scan_activity)
        s.logline.connect(lambda t: self._log(self.txt_log, t))
        s.recorded.connect(self.on_scan_recorded)
        s.status.connect(self.statusBar().showMessage)
        s.failed.connect(lambda m: (QMessageBox.critical(self, "Scanner error", m),
                                    self.on_scan_stop()))
        s.nowPlaying.connect(self.on_scan_now)
        s.audio.connect(self.on_audio)
        s.spectrum.connect(self.on_spectrum)
        s.marks.connect(self.on_scan_marks)
        s.monitor = self.ck_listen.isChecked()
        s.finished.connect(self._scan_finished)
        self.scanner = s
        if self.ck_listen.isChecked():
            self._open_audio()
        s.start()
        self.b_start.setEnabled(False)
        self.b_stop.setEnabled(True)
        self.header.pill_state.set("scanning", theme.WARN)

    def on_scan_stop(self):
        if self.scanner:
            self.scanner.stop()
            self.scanner.wait(6000)
        self._scan_finished()

    def _scan_finished(self):
        self._close_audio()
        self.b_start.setEnabled(True)
        self.b_stop.setEnabled(False)
        self.lbl_now.setText("idle")
        self.lbl_now.setStyleSheet(f"color: {theme.MUTED};")
        self.bar_snr.setValue(0)
        self._refresh_state_pill()

    def on_scan_activity(self, freq, snr):
        self.bar_snr.setValue(int(max(0, min(40, snr))))

    @Slot(object)
    def on_scan_marks(self, hits):
        """Dots on the trace for every channel the scanner is considering."""
        if not hits or self.last_trace is None:
            self.markers.setData([], [])
            return
        F, D = self.last_trace
        xs, ys = [], []
        for f, _snr in hits:
            i = int(np.argmin(np.abs(F - f)))
            xs.append(float(F[i]))
            ys.append(float(D[i]))
        self.markers.setData(xs, ys)
        if len(hits) == 1:
            self.vline.setPos(hits[0][0])

    def on_scan_now(self, freq):
        if freq <= 0:
            self.lbl_now.setText("scanning...")
            self.lbl_now.setStyleSheet(f"color: {theme.MUTED};")
        else:
            self.lbl_now.setText(f"{freq/1e6:.4f} MHz")
            self.lbl_now.setStyleSheet(f"color: {theme.GOOD};")

    def _aircraft_snapshot(self):
        """
        Who was overhead when this recording was made.

        ADS-B carries no channel information, so nothing in a transmission
        says which aircraft sent it. What can honestly be recorded is a
        coincidence in time and space: these aircraft were in range at that
        moment. With one dongle the two modes cannot run together, so the
        snapshot is whatever ADS-B last saw and its age is reported with it.
        """
        idx = getattr(self, "_ac_index", {}) or {}
        age = time.time() - getattr(self, "_ac_seen_at", 0.0)
        live = bool(self.adsb and self.adsb.isRunning())
        out = []
        for a in idx.values():
            info = self.map.info_for(a.icao)
            out.append({
                "icao": a.icao,
                "callsign": a.callsign or "",
                "country": a.country or "",
                "altitude_ft": a.altitude,
                "squawk": a.squawk or "",
                "distance_km": round(info[0], 1) if info else None,
                "bearing_deg": round(info[1]) if info else None,
                "lat": a.lat, "lon": a.lon,
            })
        out.sort(key=lambda d: (d["distance_km"] is None, d["distance_km"] or 0))
        return out, age, live

    def on_scan_recorded(self, path, freq, dur):
        aircraft, age, live = self._aircraft_snapshot()
        sidecar = {
            "file": os.path.basename(path),
            "frequency_hz": freq,
            "recorded_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "duration_s": round(dur, 2),
            "band": bp.label_for(freq, self.country()),
            "demodulator": self.cb_demod.currentText(),
            "aircraft_in_range": aircraft,
            "aircraft_data_age_s": round(age, 1) if aircraft else None,
            "aircraft_live": live,
            "note": ("ADS-B running on a second dongle: aircraft were in range "
                     "at the time of this recording"
                     if live else
                     "ADS-B was not receiving during this recording; the list is "
                     "the last state seen, see aircraft_data_age_s"),
        }
        try:
            tmp = path + ".json.part"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(sidecar, fh, indent=2)
            os.replace(tmp, path + ".json")
        except OSError:
            pass

        if aircraft and (live or age < 300):
            near = aircraft[0]
            tag = near["callsign"] or near["icao"]
            if near["distance_km"] is not None:
                tag += f" {near['distance_km']:.0f}km"
            ac_cell = f"{len(aircraft)}  ({tag})"
        elif aircraft:
            ac_cell = f"{len(aircraft)}  (stale {age/60:.0f}m)"
        else:
            ac_cell = "-"

        # the table is a running log; the files themselves are on disk
        while self.tbl_rec.rowCount() >= self.MAX_RECORDING_ROWS:
            self.tbl_rec.removeRow(0)
        r = self.tbl_rec.rowCount()
        self.tbl_rec.insertRow(r)
        for c, v in enumerate((datetime.now().strftime("%H:%M:%S"),
                               f"{freq/1e6:.4f} MHz", f"{dur:.1f} s",
                               ac_cell, os.path.basename(path))):
            it = QTableWidgetItem(v)
            it.setData(Qt.UserRole, path)
            if c == 3 and aircraft:
                it.setForeground(QBrush(QColor(theme.GOOD if live else theme.MUTED)))
                it.setToolTip("\n".join(
                    f"{a['icao']} {a['callsign']} "
                    f"{(str(a['altitude_ft']) + ' ft') if a['altitude_ft'] else ''} "
                    f"{(str(a['distance_km']) + ' km') if a['distance_km'] else ''}"
                    for a in aircraft[:12]))
            self.tbl_rec.setItem(r, c, it)
        self.tbl_rec.scrollToBottom()

    def on_show_aircraft(self):
        """Show the aircraft sidecar for the selected recording."""
        it = self.tbl_rec.currentItem()
        if it is None:
            self.statusBar().showMessage("Select a recording first", 4000)
            return
        path = (it.data(Qt.UserRole) or "") + ".json"
        if not os.path.isfile(path):
            QMessageBox.information(self, "No aircraft data",
                                    "This recording has no aircraft sidecar.")
            return
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        acs = d.get("aircraft_in_range") or []
        lines = [f"{d['file']}", f"{d['frequency_hz']/1e6:.4f} MHz  "
                 f"{d.get('band') or ''}  {d['duration_s']} s", "", d.get("note", ""), ""]
        if acs:
            lines.append(f"{len(acs)} aircraft:")
            for a in acs[:20]:
                bits = [a["icao"], a["callsign"] or "-", a["country"] or ""]
                if a["altitude_ft"]:
                    bits.append(f"{a['altitude_ft']:,} ft")
                if a["distance_km"] is not None:
                    bits.append(f"{a['distance_km']:.0f} km / {a['bearing_deg']}°")
                if a["squawk"]:
                    bits.append(f"sq {a['squawk']}")
                lines.append("  " + "  ".join(b for b in bits if b))
        else:
            lines.append("No aircraft were being decoded at the time.")
        QMessageBox.information(self, "Aircraft in range", "\n".join(lines))

    def on_open_recording(self):
        it = self.tbl_rec.currentItem()
        if it is None:
            return
        path = it.data(Qt.UserRole)
        if path and os.path.exists(path):
            os.startfile(path)

    # ------------------------------------------------------------- ADS-B --
    def on_adsb_start(self):
        if self._busy(self.cb_adev.currentData() or 0):
            return
        if release_device():
            self._log(self.txt_alog, "cleared a stray rtl_adsb that held the device")
        a = AdsbWorker()
        a.gain = self.cb_gain.currentText()
        a.ppm = self.sp_ppm.value()
        a.device = self.cb_adev.currentData() or 0
        a.updated.connect(self.on_adsb_update)
        a.logline.connect(lambda t: self._log(self.txt_alog, t))
        a.failed.connect(lambda m: (QMessageBox.critical(self, "ADS-B error", m),
                                    self.on_adsb_stop()))
        a.finished.connect(self._adsb_finished)
        self.adsb = a
        a.start()
        self.b_astart.setEnabled(False)
        self.b_astop.setEnabled(True)
        self.header.pill_state.set("ADS-B", theme.GOOD)

    def on_adsb_stop(self):
        if self.adsb:
            self.adsb.stop()
            self.adsb.wait(4000)
        self._adsb_finished()

    def _adsb_finished(self):
        self.b_astart.setEnabled(True)
        self.b_astop.setEnabled(False)
        self._refresh_state_pill()

    @Slot(object, int, int, float)
    def on_adsb_update(self, aircraft, total, valid, pct):
        self.lbl_frames.setText(f"{total}")
        self.lbl_valid.setText(f"{valid}")
        self.lbl_pct.setText(f"{pct:.1f} %")
        self.lbl_count.setText(f"{len(aircraft)}")

        # Stable order: first seen stays put. Sorting by message count means
        # rows reorder under the cursor every second.
        keep = sorted(aircraft, key=lambda a: a.first_seen)
        # populate the map first: the distance column is derived from it
        self.map.set_aircraft(keep)

        # remember what the user was looking at
        sel_icao = None
        cur = self.tbl_ac.currentItem()
        if cur is not None:
            sel_icao = cur.data(Qt.UserRole)
        scroll = self.tbl_ac.verticalScrollBar().value()

        self._updating_table = True
        self.tbl_ac.setSortingEnabled(False)

        existing = {}
        for r in range(self.tbl_ac.rowCount()):
            i0 = self.tbl_ac.item(r, 0)
            if i0 is not None:
                existing[i0.text()] = r

        for a in keep:
            info = self.map.info_for(a.icao)
            vals = [
                a.icao,
                a.callsign or "-",
                a.country or "-",
                a.squawk or "-",
                f"{a.altitude:,}" if a.altitude is not None else "-",
                f"{a.speed}" if a.speed is not None else "-",
                f"{a.track}" if a.track is not None else "-",
                f"{a.vrate}" if a.vrate is not None else "-",
                f"{a.lat:.5f}" if a.lat is not None else "-",
                f"{a.lon:.5f}" if a.lon is not None else "-",
                f"{info[0]:.0f}" if info else "-",
                f"{a.messages}",
            ]
            r = existing.pop(a.icao, None)
            if r is None:
                r = self.tbl_ac.rowCount()
                self.tbl_ac.insertRow(r)
                for c in range(self.tbl_ac.columnCount()):
                    self.tbl_ac.setItem(r, c, QTableWidgetItem(""))

            for c, v in enumerate(vals):
                it = self.tbl_ac.item(r, c)
                if it.text() != v:                 # avoid needless repaints
                    it.setText(v)
                it.setData(Qt.UserRole, a.icao)
                colour = None
                tip = ""
                if c == 0:
                    colour = theme.ACCENT
                elif c == 3 and a.squawk:
                    colour = theme.BAD if a.emergency else theme.WARN
                    tip = a.squawk_meaning
                elif c in (8, 9) and a.lat is not None:
                    colour = theme.GOOD
                elif c == 10 and info and self._beyond_horizon(a, info[0]):
                    colour = theme.BAD
                    tip = ("Beyond the radio horizon for this altitude - the "
                           "position is almost certainly a stale CPR pair, "
                           "not a real fix. Altitude and speed are still good.")
                it.setForeground(QBrush(QColor(colour or theme.TEXT)))
                it.setToolTip(tip)

            if a.emergency and self._emergency_seen.get(a.icao) != a.squawk:
                self._emergency_seen[a.icao] = a.squawk
                self._log(self.txt_alog,
                          f"*** {a.icao} {a.callsign or ''} squawking "
                          f"{a.squawk} - {a.squawk_meaning} ***")

        # drop rows for aircraft that aged out, bottom-up so indices hold
        for r in sorted(existing.values(), reverse=True):
            self.tbl_ac.removeRow(r)

        self._ac_index = {a.icao: a for a in keep}
        self._ac_seen_at = time.time()
        with_pos = sum(1 for a in keep if a.lat is not None)
        self.lbl_pos.setText(f"{with_pos} / {len(keep)}")

        # put the user back where they were
        if sel_icao is not None:
            for r in range(self.tbl_ac.rowCount()):
                i0 = self.tbl_ac.item(r, 0)
                if i0 is not None and i0.text() == sel_icao:
                    self.tbl_ac.selectRow(r)
                    break
        self.tbl_ac.verticalScrollBar().setValue(scroll)
        self._updating_table = False

    def on_release_device(self):
        killed = release_device()
        msg = ("cleared a stray rtl_adsb - the dongle is free again"
               if killed else "nothing was holding the device")
        self._log(self.txt_alog, msg)
        self.statusBar().showMessage(msg)

    def _open_ac_url(self, which):
        it = self.tbl_ac.currentItem()
        if it is None:
            self.statusBar().showMessage("Select an aircraft row first", 4000)
            return
        ac = getattr(self, "_ac_index", {}).get(it.data(Qt.UserRole))
        if ac is None:
            return
        url = ac.maps_url if which == "maps" else ac.track_url
        if not url:
            QMessageBox.information(
                self, "No position yet",
                f"{ac.icao} has not sent an even/odd position pair yet, so its "
                f"location cannot be resolved. Altitude and speed are available.")
            return
        webbrowser.open(url)

    def on_export_aircraft(self):
        idx = getattr(self, "_ac_index", {})
        if not idx:
            self.statusBar().showMessage("No aircraft decoded yet", 4000)
            return
        p, _ = QFileDialog.getSaveFileName(self, "Export aircraft", "aircraft.csv",
                                           "CSV (*.csv)")
        if not p:
            return
        with open(p, "w", newline="") as fh:
            wr = csv.writer(fh)
            wr.writerow(["icao", "callsign", "country", "squawk", "alt_ft", "speed_kt",
                         "track_deg", "vrate_fpm", "lat", "lon", "messages"])
            for a in idx.values():
                wr.writerow([a.icao, a.callsign or "", a.country, a.squawk or "", a.altitude,
                             a.speed, a.track, a.vrate, a.lat, a.lon, a.messages])
        self.statusBar().showMessage(f"exported {len(idx)} aircraft to {p}")

    def _prune_caches(self):
        """Housekeeping at startup, in the background: nothing here is urgent."""
        try:
            import tiles as _t
            removed, freed = _t.prune_cache()
            if removed:
                print(f"tile cache: removed {removed} tiles, freed "
                      f"{freed/1e6:.1f} MB", flush=True)
        except Exception:
            pass

    def _refresh_ages(self):
        self._refresh_state_pill()
        running = bool((self.worker and self.worker.isRunning()) or
                       (self.scanner and self.scanner.isRunning()))
        self.b_stop.setEnabled(running)
        self.b_start.setEnabled(not running)
        # Mode and frequency are locked while a job runs: letting them change
        # leaves the panel describing something other than what is on the air.
        for wdg in (self.rb_live, self.rb_range, self.ck_autorec, self.cb_band,
                    self.cb_country, self.sp_center, self.sp_start, self.sp_stop):
            wdg.setEnabled(not running)

    # ---------------------------------------------------------- settings --
    def _widget_map(self):
        """Every setting key -> the widget that owns it. No hidden state."""
        return {
            "mode": (self.rb_live, self.rb_range),
            "auto_record": self.ck_autorec,
            "center_mhz": self.sp_center,
            "start_mhz": self.sp_start,
            "stop_mhz": self.sp_stop,
            "sample_rate": self.cb_fs,
            "gain": self.cb_gain,
            "ppm": self.sp_ppm,
            "fft_size": self.cb_nfft,
            "demod": self.cb_demod,
            "listen": self.ck_listen,
            "peak_hold": self.ck_peak,
            "device": self.cb_dev,
            "scan_step_khz": self.cb_vstep,
            "squelch_db": self.sp_squelch,
            "hang_s": self.sp_hang,
            "min_len_s": self.sp_minlen,
            "skip_spurs": self.ck_spurs,
            "adsb_device": self.cb_adev,
            "rx_lat": self.sp_rxlat,
            "rx_lon": self.sp_rxlon,
            "show_rx": self.ck_showrx,
            "map_tiles": self.ck_tiles,
            "map_dark": self.ck_darkmap,
            "map_source": self.cb_tilesrc,
            "country": self.cb_country,
        }

    def get_settings(self):
        out = {}
        for key, w in self._widget_map().items():
            if key == "mode":
                out[key] = "live" if self.rb_live.isChecked() else "range"
            elif isinstance(w, QComboBox):
                out[key] = w.currentText()
            elif isinstance(w, (QSpinBox, QDoubleSpinBox)):
                out[key] = w.value()
            elif isinstance(w, QCheckBox):
                out[key] = w.isChecked()
        out["output_dir"] = self._outdir
        return out

    def apply_settings(self, data):
        for key, w in self._widget_map().items():
            if key not in data:
                continue
            v = data[key]
            try:
                if key == "mode":
                    {"live": self.rb_live, "range": self.rb_range,
                     "sweep": self.rb_range,
                     "scan": self.rb_range}.get(v, self.rb_live).setChecked(True)
                elif isinstance(w, QComboBox):
                    i = w.findText(str(v))
                    if i < 0:
                        i = w.findData(v)
                    if i >= 0:
                        w.setCurrentIndex(i)
                elif isinstance(w, QSpinBox):
                    w.setValue(int(v))
                elif isinstance(w, QDoubleSpinBox):
                    w.setValue(float(v))
                elif isinstance(w, QCheckBox):
                    w.setChecked(bool(v))
            except (TypeError, ValueError):
                continue
        if data.get("output_dir"):
            self._outdir = data["output_dir"]
            self.b_outdir.setText("Save to: " + os.path.basename(self._outdir))
        self._on_mode_change()
        self._apply_receiver()

    # ----------------------------------------------------------- presets --
    def _preset_dir(self):
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "presets")
        os.makedirs(d, exist_ok=True)
        return d

    def _install_shortcuts(self):
        """
        Everything frequent should be reachable without the mouse. Space is
        the run control because that is what it is on every other transport.
        """
        for keys, slot in (
            ("Space", self._toggle_run),
            ("Ctrl+F", self.on_autofind),
            ("Ctrl+R", lambda: self.b_wav.click() if self.b_wav.isEnabled() else None),
            ("Ctrl+L", lambda: self.ck_listen.setChecked(not self.ck_listen.isChecked())),
            ("Ctrl+1", lambda: self.tabs.setCurrentIndex(0)),
            ("Ctrl+2", lambda: self.tabs.setCurrentIndex(1)),
            ("Esc", self.on_stop),
        ):
            act = QAction(self)
            act.setShortcut(QKeySequence(keys))
            act.triggered.connect(slot)
            self.addAction(act)

    def _toggle_run(self):
        if self.b_stop.isEnabled():
            self.on_stop()
        elif self.b_start.isEnabled():
            self.on_start()

    def _build_menus(self):
        mb = self.menuBar()

        m_file = mb.addMenu("&File")
        a = m_file.addAction("Open recordings folder")
        a.triggered.connect(lambda: os.startfile(self._outdir)
                            if os.path.isdir(self._outdir) else None)
        a = m_file.addAction("Forget saved scans")
        a.triggered.connect(self._forget_scans)
        m_file.addSeparator()
        a = m_file.addAction("E&xit")
        a.triggered.connect(self.close)

        self.m_presets = mb.addMenu("&Presets")
        self._rebuild_preset_menu()

        m_help = mb.addMenu("&Help")
        a = m_help.addAction("&About")
        a.triggered.connect(self._about)

    def _forget_scans(self):
        scanstore.clear()
        self._refresh_saved_list()
        self.statusBar().showMessage("saved scans removed")

    def _rebuild_preset_menu(self):
        self.m_presets.clear()
        for name in BUILTIN_PRESETS:
            act = self.m_presets.addAction(name)
            act.triggered.connect(
                lambda _=False, n=name: self._load_builtin(n))
        saved = sorted(f[:-5] for f in os.listdir(self._preset_dir())
                       if f.endswith(".json"))
        if saved:
            self.m_presets.addSeparator()
            for name in saved:
                act = self.m_presets.addAction(name)
                act.triggered.connect(
                    lambda _=False, n=name: self._load_preset(n))
        self.m_presets.addSeparator()
        for label, fn in (("Save current as preset...", self._save_preset),
                          ("Delete a preset...", self._delete_preset),
                          ("Import preset...", self._import_preset),
                          ("Export current...", self._export_preset)):
            self.m_presets.addAction(label).triggered.connect(fn)
        self.m_presets.addSeparator()
        self.m_presets.addAction(
            "Save current as defaults").triggered.connect(self._save_defaults)
        self.m_presets.addAction(
            "Reset to factory defaults").triggered.connect(self._reset_defaults)

    def _load_builtin(self, name):
        self.apply_settings({**DEFAULT_SETTINGS, **BUILTIN_PRESETS[name]})
        self.statusBar().showMessage(f"preset: {name}")

    def _load_preset(self, name):
        path = os.path.join(self._preset_dir(), name + ".json")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                self.apply_settings(json.load(fh))
            self.statusBar().showMessage(f"preset: {name}")
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Preset", f"Could not read {name}: {exc}")

    def _write_preset(self, path, data):
        # temp name + replace, so a preset file is never half written
        tmp = path + ".part"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)

    def _save_preset(self):
        name, ok = QInputDialog.getText(self, "Save preset", "Name:")
        if not ok or not name.strip():
            return
        safe = "".join(c for c in name.strip() if c.isalnum() or c in " -_").strip()
        if not safe:
            return
        self._write_preset(os.path.join(self._preset_dir(), safe + ".json"),
                           self.get_settings())
        self._rebuild_preset_menu()
        self.statusBar().showMessage(f"saved preset {safe}")

    def _delete_preset(self):
        saved = sorted(f[:-5] for f in os.listdir(self._preset_dir())
                       if f.endswith(".json"))
        if not saved:
            QMessageBox.information(self, "Presets", "No saved presets yet.")
            return
        name, ok = QInputDialog.getItem(self, "Delete preset", "Preset:",
                                        saved, 0, False)
        if ok and name:
            try:
                os.remove(os.path.join(self._preset_dir(), name + ".json"))
            except OSError:
                pass
            self._rebuild_preset_menu()

    def _import_preset(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import preset", "",
                                              "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Preset", str(exc))
            return
        name = os.path.splitext(os.path.basename(path))[0]
        self._write_preset(os.path.join(self._preset_dir(), name + ".json"), data)
        self.apply_settings(data)
        self._rebuild_preset_menu()

    def _export_preset(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export current settings",
                                              "rtl-spectrum.json", "JSON (*.json)")
        if path:
            self._write_preset(path, self.get_settings())
            self.statusBar().showMessage(f"exported to {path}")

    def _save_defaults(self):
        self._qsettings().setValue("defaults", json.dumps(self.get_settings()))
        self.statusBar().showMessage("current settings saved as defaults")

    def _reset_defaults(self):
        self._qsettings().remove("defaults")
        self.apply_settings(DEFAULT_SETTINGS)
        self.statusBar().showMessage("reset to factory defaults")

    def _qsettings(self):
        return QSettings("hclivess", APP_NAME)

    def _restore_settings(self):
        raw = self._qsettings().value("defaults", "")
        data = dict(DEFAULT_SETTINGS)
        if raw:
            try:
                data.update(json.loads(raw))
            except ValueError:
                pass
        self.apply_settings(data)

    def _about(self):
        QMessageBox.about(
            self, f"About {APP_NAME}",
            f"<b>{APP_NAME} {APP_VERSION}</b><br><br>"
            f"A wideband receiver for RTL2832U dongles: listen, sweep, "
            f"scan-and-record, and decode ADS-B aircraft.<br><br>"
            f'<a href="{APP_REPO}">{APP_REPO}</a>')

    def closeEvent(self, ev):
        try:
            self._qsettings().setValue("last", json.dumps(self.get_settings()))
        except Exception:
            pass
        try:
            self.map.shutdown()
        except Exception:
            pass
        for stop in (self.on_stop, self.on_scan_stop, self.on_adsb_stop):
            try:
                stop()
            except Exception:
                pass
        ev.accept()


def icon_path(name):
    """Find an asset beside the frozen exe, inside _MEIPASS, or in the source tree."""
    here = os.path.dirname(os.path.abspath(__file__))
    for base in (os.path.dirname(sys.executable), getattr(sys, "_MEIPASS", ""), here):
        if base:
            p = os.path.join(base, name)
            if os.path.isfile(p):
                return p
    return ""


def selftest():
    """
    Headless end-to-end check, no dongle required: synthesise an AM-modulated
    carrier, push it through the real demodulator, write a real WAV, and
    verify the tone came back. Also decodes a known-good ADS-B frame.
    Exit 0 only if actual output was produced.
    """
    import wave as _wave
    from dsp import Demodulator
    from adsb import AdsbDecoder, crc_residual

    out = SELFTEST if SELFTEST and SELFTEST != "1" else os.path.join(
        os.getcwd(), "selftest.wav")
    fs, secs, tone = SCAN_FS, 2.0, 1000.0
    t = np.arange(int(fs * secs)) / fs
    env = 0.5 * (1.0 + 0.8 * np.sin(2 * np.pi * tone * t))
    iq = (env * np.exp(2j * np.pi * 1200.0 * t)).astype(np.complex64)

    dm = Demodulator(fs, "AM")
    blk = 65536
    chunks = [dm(iq[i:i + blk], normalise=False)
              for i in range(0, len(iq) - blk, blk)]
    aud = np.concatenate(chunks) if chunks else np.zeros(0, np.float32)
    if len(aud) < AUDIO_FS:
        print("SELFTEST FAIL: demodulator produced no audio")
        return 1

    peak = float(np.abs(aud).max())
    if peak <= 1e-6:
        print("SELFTEST FAIL: audio is silent")
        return 1
    spec = np.abs(np.fft.rfft(aud * np.hanning(len(aud))))
    got = float(np.fft.rfftfreq(len(aud), 1 / AUDIO_FS)[int(np.argmax(spec))])
    if abs(got - tone) > 20.0:
        print(f"SELFTEST FAIL: expected {tone:.0f} Hz, recovered {got:.1f} Hz")
        return 1

    tmp = out + ".part"
    with _wave.open(tmp, "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(AUDIO_FS)
        fh.writeframes((aud / peak * 0.8 * 32767).astype(np.int16).tobytes())
    os.replace(tmp, out)
    if os.path.getsize(out) < 1000:
        print("SELFTEST FAIL: wav too small")
        return 1

    frame = "8D4840D6202CC371C32CE0576098"
    if crc_residual(frame) != 0:
        print("SELFTEST FAIL: Mode S CRC rejected a known-good frame")
        return 1
    dec = AdsbDecoder()
    ac = dec.feed("*" + frame + ";")
    if ac is None or ac.callsign != "KLM1023":
        print(f"SELFTEST FAIL: callsign decode gave {ac and ac.callsign!r}")
        return 1

    print(f"SELFTEST OK: {got:.1f} Hz recovered, {os.path.getsize(out)} bytes "
          f"-> {out}; ADS-B callsign {ac.callsign}")
    return 0


def main():
    if SELFTEST:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        sys.exit(selftest())

    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                f"hclivess.{APP_NAME}")
        except Exception:
            pass

    app = QApplication(sys.argv)
    theme.apply(app)
    ico = icon_path("icon.ico") or icon_path("icon.png")
    if ico:
        app.setWindowIcon(QIcon(ico))
    win = MainWindow()
    if ico:
        win.setWindowIcon(QIcon(ico))
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
