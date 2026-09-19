"""
Signal processing for rtl-spectrum. Imports no Qt, so it is usable from a CLI
or a test without a display.
"""
import math
import threading

import numpy as np

from config import AUDIO_FS

try:
    import sounddevice as _sd
except Exception:
    _sd = None


# ------------------------------------------------------------------ DSP ----
def fft_decimate(x, decim):
    """Stateless brick-wall low-pass and decimate. Use FftDecimator on streams."""
    decim = int(decim)
    if decim <= 1:
        return x
    n = (len(x) // decim) * decim
    if n == 0:
        return x[:0]
    spec = np.fft.fftshift(np.fft.fft(x[:n]))
    keep = n // decim
    lo = (n - keep) // 2
    return np.fft.ifft(np.fft.ifftshift(spec[lo:lo + keep])) / decim


def design_lowpass(cutoff, ntaps):
    """Windowed-sinc low-pass. `cutoff` in cycles/sample (0 .. 0.5)."""
    n = np.arange(ntaps) - (ntaps - 1) / 2.0
    h = np.sinc(2 * cutoff * n) * np.hamming(ntaps)
    return (h / h.sum()).astype(np.float64)


def _factor(d, limit=10):
    """Split a decimation factor into stages no larger than `limit`."""
    stages = []
    for p in (2, 3, 5, 7):
        while d % p == 0:
            stages.append(p)
            d //= p
    if d > 1:
        stages.append(d)
    out = []
    for s in sorted(stages, reverse=True):
        for i, cur in enumerate(out):
            if cur * s <= limit:
                out[i] = cur * s
                break
        else:
            out.append(s)
    return out


class FirDecimator:
    """
    Streaming decimator built from stateful linear convolution.

    An FFT filter applied per block is a *circular* convolution: every block
    wraps its own edges, so the joins click at the block rate. Carrying the
    filter's input tail and convolving in 'valid' mode is genuinely linear and
    leaves no seam. Large factors are cascaded so the cost stays low.
    """

    def __init__(self, decim, atten_taps=8):
        self.decim = max(int(decim), 1)
        self.stages = []
        if self.decim > 1:
            for d in _factor(self.decim):
                ntaps = max(15, atten_taps * d | 1)
                self.stages.append({
                    "d": d,
                    "h": design_lowpass(0.5 / d * 0.90, ntaps),
                    "tail": None,
                    "phase": 0,
                })

    def __call__(self, x):
        for st in self.stages:
            h, d = st["h"], st["d"]
            if st["tail"] is None or st["tail"].dtype != x.dtype:
                st["tail"] = np.zeros(len(h) - 1, dtype=x.dtype)
            buf = np.concatenate([st["tail"], x])
            if len(buf) < len(h):
                st["tail"] = buf
                return x[:0]
            y = np.convolve(buf, h.astype(x.dtype.type if np.iscomplexobj(x)
                                          else np.float32), mode="valid")
            st["tail"] = buf[-(len(h) - 1):].copy()
            start = (-st["phase"]) % d
            st["phase"] = (st["phase"] + len(x)) % d
            x = y[start::d]
        return x


def design_bandpass(lo, hi, ntaps):
    """Windowed-sinc band-pass; cutoffs in cycles/sample (0 .. 0.5)."""
    n = np.arange(ntaps) - (ntaps - 1) / 2.0
    h = (2 * hi * np.sinc(2 * hi * n) - 2 * lo * np.sinc(2 * lo * n))
    h *= np.hamming(ntaps)
    # normalise to unity gain at the middle of the passband
    mid = (lo + hi) / 2.0
    gain = np.abs((h * np.exp(-2j * np.pi * mid * n)).sum())
    if gain > 1e-9:
        h = h / gain
    return h.astype(np.float32)


class FirFilter:
    """Streaming FIR with carried tail, so blocks join without a seam."""

    def __init__(self, taps):
        self.h = np.asarray(taps, dtype=np.float32)
        self.tail = np.zeros(len(self.h) - 1, dtype=np.float32)

    def __call__(self, x):
        x = np.asarray(x, dtype=np.float32)
        buf = np.concatenate([self.tail, x])
        if len(buf) < len(self.h):
            self.tail = buf
            return x[:0]
        y = np.convolve(buf, self.h, mode="valid")
        self.tail = buf[-(len(self.h) - 1):].copy()
        return y.astype(np.float32)


class Agc:
    """
    Slow automatic gain with a ceiling.

    Normalising each block to its own peak is what makes a quiet band as loud
    as speech: silence gets multiplied up until the hiss is at full scale.
    Capping the gain keeps the noise floor down where it belongs and still
    brings a weak transmission up.
    """

    def __init__(self, target=0.30, max_gain=25.0, attack=0.15, release=0.008):
        self.target = target
        self.max_gain = max_gain
        self.attack = attack
        self.release = release
        self.env = 0.0
        self.gain = 1.0

    def __call__(self, x):
        if not len(x):
            return x
        peak = float(np.abs(x).max())
        a = self.attack if peak > self.env else self.release
        self.env = (1 - a) * self.env + a * peak
        want = self.target / max(self.env, 1e-5)
        self.gain = min(want, self.max_gain)
        return np.clip(x * self.gain, -1.0, 1.0).astype(np.float32)


class Demodulator:
    """Stateful demodulation chain: decimators, discriminator and de-emphasis."""

    def __init__(self, fs, mode):
        self.fs = fs
        self.mode = mode
        if mode == "WFM":
            self.d1 = FirDecimator(max(int(fs // 240000), 1))
            self.d2 = FirDecimator(max(int((fs / self.d1.decim) // AUDIO_FS), 1))
        else:
            self.d1 = FirDecimator(max(int(fs // AUDIO_FS), 1))
            self.d2 = None
        # NFM broadcast carries 750 us pre-emphasis; without the matching
        # de-emphasis the top of the band is lifted and it hisses.
        self.deemph = Deemphasis(tau=50e-6 if mode == "WFM" else 750e-6)
        self.prev = None            # last IQ sample, for discriminator continuity
        self.dc = 0.0

        # Audio-band filter. Voice needs 300-3400 Hz; everything above that in
        # a 24 kHz-wide output is hiss and nothing else.
        if mode == "WFM":
            self.post = FirFilter(design_bandpass(30.0 / AUDIO_FS,
                                                  15000.0 / AUDIO_FS, 127))
        else:
            self.post = FirFilter(design_bandpass(300.0 / AUDIO_FS,
                                                  3400.0 / AUDIO_FS, 255))
        self.agc = Agc(max_gain=40.0 if mode == "AM" else 25.0)

    def _discriminate(self, x):
        if len(x) == 0:
            return np.zeros(0, dtype=np.float32)
        if self.prev is not None:
            x = np.concatenate([[self.prev], x])
        self.prev = x[-1]
        return np.angle(x[1:] * np.conj(x[:-1])).astype(np.float32)

    def __call__(self, iq, normalise=True):
        if self.mode == "WFM":
            aud = self._discriminate(self.d1(iq))
            aud = self.d2(aud.astype(np.complex64)).real
            aud = self.deemph(aud.astype(np.float32))
        elif self.mode == "NFM":
            aud = self._discriminate(self.d1(iq))
        elif self.mode == "AM":
            env = np.abs(self.d1(iq)).astype(np.float32)
            # track the carrier slowly instead of removing each block's own
            # mean, which would step the level at every block boundary
            self.dc = 0.95 * self.dc + 0.05 * float(env.mean()) if self.dc else \
                float(env.mean())
            aud = env - self.dc
        else:
            return np.zeros(0, dtype=np.float32)

        aud = np.asarray(aud, dtype=np.float32)
        if self.mode != "WFM":
            aud = self.deemph(aud)
        aud = self.post(aud)
        if normalise:
            aud = self.agc(aud)
        return aud


def fm_discriminate(x):
    if len(x) < 2:
        return np.zeros(0, dtype=np.float32)
    return np.angle(x[1:] * np.conj(x[:-1])).astype(np.float32)


class Deemphasis:
    """
    One-pole de-emphasis (50 us Europe) expressed as a short FIR with carried
    state, so it stays vectorised instead of looping per audio sample.
    """

    def __init__(self, tau=50e-6, fs=AUDIO_FS, taps=64):
        a = math.exp(-1.0 / (fs * tau))
        self.kernel = ((1 - a) * a ** np.arange(taps)).astype(np.float32)
        self.tail = np.zeros(taps - 1, dtype=np.float32)

    def __call__(self, x):
        if len(x) == 0:
            return x
        y = np.convolve(x, self.kernel)
        y[:len(self.tail)] += self.tail
        self.tail = y[len(x):].astype(np.float32).copy()
        return y[:len(x)].astype(np.float32)


def demodulate(iq, fs, mode, deemph, normalise=True):
    """Demodulate one IQ block to float32 audio near AUDIO_FS."""
    if mode == "WFM":
        d1 = max(int(fs // 240000), 1)
        base = fft_decimate(iq, d1)
        aud = fm_discriminate(base)
        aud = fft_decimate(aud.astype(np.complex64),
                           max(int((fs / d1) // AUDIO_FS), 1)).real
        aud = deemph(aud.astype(np.float32))
    elif mode == "NFM":
        aud = fm_discriminate(fft_decimate(iq, max(int(fs // AUDIO_FS), 1)))
    elif mode == "AM":
        base = fft_decimate(iq, max(int(fs // AUDIO_FS), 1))
        aud = np.abs(base).astype(np.float32)
        aud -= aud.mean()
    else:
        return np.zeros(0, dtype=np.float32)

    aud = np.asarray(aud, dtype=np.float32)
    if normalise:
        peak = float(np.abs(aud).max()) if len(aud) else 0.0
        if peak > 1e-9:
            aud = aud / peak * 0.7
    return aud


class AudioSink:
    """
    Ring-buffered output. The producer is an SDR worker thread and the
    consumer is PortAudio's own callback thread, so a busy GUI repainting a
    waterfall can no longer starve playback -- which is what a blocking
    write() from the GUI thread did.
    """

    def __init__(self, fs=AUDIO_FS, seconds=2.0, prefill=0.35):
        self.fs = fs
        self.size = int(fs * seconds)
        self.buf = np.zeros(self.size, dtype=np.float32)
        self.w = 0
        self.r = 0
        self.lock = threading.Lock()
        self.stream = None
        self.underruns = 0
        # hold playback until this much is banked, so block-to-block jitter
        # is absorbed instead of being heard
        self.prefill = int(fs * prefill)
        self.primed = False

    def _available(self):
        return (self.w - self.r) % self.size

    def start(self):
        if _sd is None:
            raise RuntimeError("sounddevice not installed")
        self.stream = _sd.OutputStream(
            samplerate=self.fs, channels=1, dtype="float32",
            blocksize=1024, latency="high", callback=self._callback)
        self.stream.start()

    def _callback(self, outdata, frames, time_info, status):
        with self.lock:
            avail = self._available()
            if not self.primed:
                if avail < self.prefill:
                    outdata[:] = 0.0
                    return
                self.primed = True
            n = min(frames, avail)
            if n:
                idx = (self.r + np.arange(n)) % self.size
                outdata[:n, 0] = self.buf[idx]
                self.r = (self.r + n) % self.size
            if n < frames:
                outdata[n:, 0] = 0.0
                self.underruns += 1
                self.primed = False          # rebuild the cushion before resuming

    def write(self, data):
        data = np.asarray(data, dtype=np.float32).ravel()
        if not len(data):
            return
        with self.lock:
            free = self.size - 1 - self._available()
            if len(data) > free:              # prefer fresh audio over stale
                data = data[-free:] if free > 0 else data[:0]
            n = len(data)
            if n:
                idx = (self.w + np.arange(n)) % self.size
                self.buf[idx] = data
                self.w = (self.w + n) % self.size

    def stop(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None


def find_signals(freqs, db, min_snr=8.0, max_peaks=60, min_sep_hz=50e3):
    """
    Pick carriers out of a trace. The noise floor is estimated blockwise, not
    globally, so a strong band cannot mask a weak one elsewhere in a sweep.
    """
    n = len(db)
    if n < 64:
        return []
    blk = max(n // 128, 64)
    floor = np.empty(n, dtype=np.float32)
    for a in range(0, n, blk):
        b = min(a + blk, n)
        floor[a:b] = np.median(db[a:b])
    snr = db - floor

    cand = np.where(snr > min_snr)[0]
    if len(cand) == 0:
        return []
    picked = []
    for i in cand[np.argsort(snr[cand])[::-1]]:
        f = float(freqs[i])
        if any(abs(f - pf) < min_sep_hz for pf, _ in picked):
            continue
        picked.append((f, float(snr[i])))
        if len(picked) >= max_peaks:
            break
    picked.sort(key=lambda t: t[0])
    return picked


def channel_snr(iq, fs, half_bw=8000.0, noise_lo=20000.0, noise_hi=60000.0):
    """dB of in-channel power over the out-of-channel noise density."""
    n = len(iq)
    if n < 256:
        return 0.0
    p = np.abs(np.fft.fftshift(np.fft.fft(iq * np.hanning(n)))) ** 2
    f = (np.arange(n) - n // 2) * (fs / n)
    sig_m = np.abs(f) <= half_bw
    noi_m = (np.abs(f) >= noise_lo) & (np.abs(f) <= noise_hi)
    if not sig_m.any() or not noi_m.any():
        return 0.0
    sig = p[sig_m].mean()
    noi = p[noi_m].mean()
    if noi <= 0 or sig <= 0:
        return 0.0
    return float(10 * np.log10(sig / noi))


def suggest_demod(f_hz):
    mhz = f_hz / 1e6
    if 87.0 <= mhz <= 108.1:
        return "WFM"
    if 118.0 <= mhz <= 137.0:
        return "AM"
    return "NFM"


def band_label(f_hz):
    mhz = f_hz / 1e6
    for lo, hi, name in (
        (87.0, 108.1, "FM broadcast"), (108.1, 117.99, "VOR / ILS"),
        (118.0, 137.0, "airband"), (137.0, 138.0, "weather sat"),
        (144.0, 146.0, "2 m ham"), (156.0, 163.0, "marine"),
        (174.0, 240.0, "DAB / VHF III"), (380.0, 400.0, "TETRA"),
        (430.0, 440.0, "70 cm ham"), (440.0, 470.0, "UHF PMR"),
        (470.0, 790.0, "UHF TV"), (791.0, 960.0, "cellular"),
        (1087.0, 1093.0, "ADS-B"), (1500.0, 1620.0, "GPS / Iridium"),
    ):
        if lo <= mhz <= hi:
            return name
    return ""


def fmt_age(seconds):
    if seconds < 60:
        return f"{int(seconds)}s"
    return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"


