"""
rtl-spectrum - configuration and constants (the only place APP_NAME / APP_VERSION live)
"""
import os

APP_NAME = "rtl-spectrum"
APP_VERSION = "1.0"
APP_REPO = "https://github.com/hclivess/rtl-spectrum"
WINDOW_MIN_WIDTH = 820
WINDOW_MIN_HEIGHT = 560

# --- radio constants -------------------------------------------------------
TUNER_MIN_HZ = 24e6
TUNER_MAX_HZ = 1766e6
AUDIO_FS = 48000
SCAN_FS = 1.2e6            # divides exactly by 25 to reach 48 kHz

# name, start MHz, stop MHz, demodulator
BANDS = [
    ("FM broadcast", 87.5, 108.0, "WFM"),
    ("Airband voice", 118.0, 137.0, "AM"),
    ("2 m amateur", 144.0, 146.0, "NFM"),
    ("Marine VHF", 156.0, 163.0, "NFM"),
    ("PMR / business UHF", 440.0, 470.0, "NFM"),
    ("70 cm amateur", 430.0, 440.0, "NFM"),
]

DEFAULT_REC_DIR = os.environ.get(
    "RTLSDR_REC_DIR", os.path.join(os.path.expanduser("~"), "rtlsdr-recordings"))

DEFAULT_SETTINGS = {
    "mode": "live",
    "center_mhz": 94.6,
    "start_mhz": 87.5,
    "stop_mhz": 108.0,
    "sample_rate": "2.4",
    "gain": "49.6",
    "ppm": 65,
    "fft_size": "4096",
    "demod": "Off",
    "listen": True,
    "peak_hold": False,
    "device": 0,
    "scan_step_khz": "25",
    "squelch_db": 8.0,
    "hang_s": 2.0,
    "min_len_s": 0.7,
    "skip_spurs": True,
    "output_dir": DEFAULT_REC_DIR,
    "adsb_device": 0,
    "rx_lat": 50.0,
    "rx_lon": 15.0,
    "show_rx": False,
    "map_tiles": True,
    "map_dark": True,
    "map_source": "osm",
    "country": "Czechia",
}


# 2-4 built-ins, each a partial override of DEFAULT_SETTINGS
BUILTIN_PRESETS = {
    "FM radio": {
        "mode": "live", "center_mhz": 94.6, "demod": "WFM",
        "listen": True, "sample_rate": "2.4", "gain": "49.6",
    },
    "Airband scan + record": {
        "mode": "scan", "start_mhz": 118.0, "stop_mhz": 137.0,
        "demod": "AM", "scan_step_khz": "25", "squelch_db": 8.0,
        "hang_s": 2.0, "skip_spurs": True,
    },
    "Whole-band survey": {
        "mode": "sweep", "start_mhz": 24.0, "stop_mhz": 1766.0,
        "demod": "Off", "listen": False, "peak_hold": True,
    },
    "Aircraft (ADS-B)": {
        "mode": "live", "center_mhz": 1090.0, "demod": "Off",
        "listen": False, "show_rx": True,
    },
}
