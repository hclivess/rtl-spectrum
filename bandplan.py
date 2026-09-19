"""
Band plans by country.

A listening guide, not a legal reference. Allocations are set nationally and
change; what is here is the internationally coordinated core (ICAO airband,
ITU marine, IARU amateur, the ISM bands) plus the national bits that actually
differ enough to send you to the wrong part of the spectrum -- FM broadcast in
Japan, licence-free UHF, weather radio, emergency services.

Each entry is (name, start MHz, stop MHz, demodulator, note). `demod` is what
to listen with; "Off" means the signal is digital and will not resolve to
audio, so the entry exists to label the spectrum rather than to tune it.
"""
from collections import namedtuple

Band = namedtuple("Band", "name lo hi demod note")

# --- coordinated worldwide ------------------------------------------------
COMMON = [
    Band("VOR / ILS navigation", 108.0, 117.975, "AM",
         "navigation beacons, tone-modulated"),
    Band("Airband voice", 118.0, 136.975, "AM",
         "ICAO worldwide; 25 kHz, and 8.33 kHz across Europe"),
    Band("Emergency guard", 121.4, 121.6, "AM",
         "121.500 MHz international distress"),
    Band("ACARS datalink", 131.4, 131.9, "AM",
         "aircraft data; 131.525 common in Europe, 131.550 worldwide"),
    Band("Weather satellites", 137.0, 138.0, "NFM",
         "NOAA APT and Meteor-M, polar orbiters"),
    Band("UHF military air", 225.0, 400.0, "AM",
         "NATO / military aviation, AM like the civil band"),
    Band("Marine VHF", 156.0, 162.025, "NFM",
         "ITU channel grid, same worldwide; ch16 = 156.800 distress"),
    Band("ADS-B aircraft", 1087.0, 1093.0, "Off",
         "1090 MHz; use the Aircraft tab, not audio"),
    Band("GPS L1", 1574.0, 1577.0, "Off",
         "spread spectrum, below the noise floor"),
]

# --- ITU regions ----------------------------------------------------------
REGION_1 = [                                    # Europe, Africa, Middle East
    Band("FM broadcast", 87.5, 108.0, "WFM", "87.5-108 across the region"),
    Band("6 m amateur", 50.0, 52.0, "NFM", "IARU Region 1"),
    Band("2 m amateur", 144.0, 146.0, "NFM", "IARU Region 1"),
    Band("70 cm amateur", 430.0, 440.0, "NFM", "IARU Region 1"),
    Band("DAB / VHF III", 174.0, 240.0, "Off", "digital radio, not audio"),
    Band("TETRA emergency", 380.0, 400.0, "Off", "digital trunked, encrypted"),
    Band("ISM 433", 433.05, 434.79, "NFM", "remotes, sensors, telemetry"),
    Band("PMR446 licence-free", 446.0, 446.2, "NFM", "handhelds, 16 channels"),
    Band("Business UHF", 440.0, 470.0, "NFM", "licensed land mobile"),
]

REGION_2 = [                                    # the Americas
    Band("FM broadcast", 88.0, 108.0, "WFM", "88-108 in North America"),
    Band("6 m amateur", 50.0, 54.0, "NFM", "IARU Region 2"),
    Band("2 m amateur", 144.0, 148.0, "NFM", "IARU Region 2"),
    Band("70 cm amateur", 420.0, 450.0, "NFM", "IARU Region 2"),
    Band("MURS licence-free", 151.82, 154.60, "NFM", "5 channels, US"),
    Band("NOAA weather radio", 162.40, 162.55, "NFM", "continuous forecasts"),
    Band("FRS / GMRS", 462.55, 467.72, "NFM", "licence-free handhelds"),
    Band("ISM 915", 902.0, 928.0, "Off", "telemetry, LoRa"),
]

REGION_3 = [                                    # Asia-Pacific
    Band("FM broadcast", 87.5, 108.0, "WFM", "87.5-108 in most of the region"),
    Band("6 m amateur", 50.0, 54.0, "NFM", "IARU Region 3"),
    Band("2 m amateur", 144.0, 148.0, "NFM", "IARU Region 3"),
    Band("70 cm amateur", 430.0, 440.0, "NFM", "varies nationally"),
    Band("ISM 433", 433.05, 434.79, "NFM", "remotes, sensors"),
]

REGIONS = {1: REGION_1, 2: REGION_2, 3: REGION_3}

# --- national differences worth knowing ----------------------------------
# "replace" swaps an entry of the same name; "extra" adds one.
COUNTRIES = {
    "CZ": dict(name="Czechia", region=1, extra=[
        Band("Czech railway", 150.0, 160.0, "NFM", "shunting and station radio"),
    ]),
    "SK": dict(name="Slovakia", region=1),
    "PL": dict(name="Poland", region=1),
    "DE": dict(name="Germany", region=1, extra=[
        Band("Freenet licence-free", 149.0125, 149.1125, "NFM", "German only"),
    ]),
    "AT": dict(name="Austria", region=1),
    "FR": dict(name="France", region=1),
    "IT": dict(name="Italy", region=1),
    "ES": dict(name="Spain", region=1),
    "NL": dict(name="Netherlands", region=1),
    "GB": dict(name="United Kingdom", region=1, extra=[
        Band("UK marine ch M", 157.75, 157.85, "NFM", "UK yacht clubs / marinas"),
    ], warn="In the UK it is an offence to listen to anything other than "
            "broadcast and amateur transmissions."),
    "SE": dict(name="Sweden", region=1),
    "NO": dict(name="Norway", region=1),
    "FI": dict(name="Finland", region=1),
    "RU": dict(name="Russia", region=1, extra=[
        Band("OIRT FM (legacy)", 65.8, 74.0, "WFM", "older sets and relays"),
    ]),
    "UA": dict(name="Ukraine", region=1, extra=[
        Band("OIRT FM (legacy)", 65.8, 74.0, "WFM", "largely retired"),
    ]),
    "US": dict(name="United States", region=2),
    "CA": dict(name="Canada", region=2),
    "BR": dict(name="Brazil", region=2, replace=[
        Band("FM broadcast", 76.0, 108.0, "WFM", "extended band, ex-TV ch 5-6"),
    ]),
    "JP": dict(name="Japan", region=3, replace=[
        Band("FM broadcast", 76.0, 95.0, "WFM",
             "76-95 MHz, not 87.5-108; includes FM complementary relays"),
    ]),
    "AU": dict(name="Australia", region=3, extra=[
        Band("UHF CB", 476.4, 477.4, "NFM", "80 channels, licence-free"),
    ]),
    "NZ": dict(name="New Zealand", region=3, extra=[
        Band("UHF CB", 476.4, 477.4, "NFM", "licence-free"),
    ]),
    "IN": dict(name="India", region=3),
    "CN": dict(name="China", region=3),
    "ZA": dict(name="South Africa", region=1),
}

DEFAULT_COUNTRY = "CZ"


def country_names():
    """[(code, 'Czechia'), ...] sorted by display name."""
    return sorted(((c, d["name"]) for c, d in COUNTRIES.items()),
                  key=lambda t: t[1])


def bands_for(code):
    """The full ordered band list for a country, national entries applied."""
    spec = COUNTRIES.get(code) or COUNTRIES[DEFAULT_COUNTRY]
    bands = list(REGIONS[spec["region"]]) + list(COMMON)
    for repl in spec.get("replace", []):
        bands = [repl if b.name == repl.name else b for b in bands]
        if not any(b.name == repl.name for b in bands):
            bands.append(repl)
    bands += list(spec.get("extra", []))
    return sorted(bands, key=lambda b: b.lo)


def warning_for(code):
    return (COUNTRIES.get(code) or {}).get("warn", "")


def label_for(freq_hz, code=DEFAULT_COUNTRY):
    """Name of the band a frequency falls in, or ''."""
    mhz = freq_hz / 1e6
    hits = [b for b in bands_for(code) if b.lo <= mhz <= b.hi]
    if not hits:
        return ""
    return min(hits, key=lambda b: b.hi - b.lo).name      # most specific wins


def note_for(freq_hz, code=DEFAULT_COUNTRY):
    mhz = freq_hz / 1e6
    hits = [b for b in bands_for(code) if b.lo <= mhz <= b.hi]
    if not hits:
        return ""
    return min(hits, key=lambda b: b.hi - b.lo).note


def demod_for(freq_hz, code=DEFAULT_COUNTRY):
    """Sensible demodulator for a frequency, falling back to NFM."""
    mhz = freq_hz / 1e6
    hits = [b for b in bands_for(code) if b.lo <= mhz <= b.hi]
    if hits:
        best = min(hits, key=lambda b: b.hi - b.lo)
        if best.demod != "Off":
            return best.demod
    return "NFM"
