"""
Mode S / ADS-B frame decoding.

Frames arrive as the hex strings rtl_adsb prints (``*8d4840d6...;``). Every
frame is CRC-checked before it is believed -- at realistic signal levels most
of what a cheap dongle emits is corrupt, and an unchecked decoder invents
aircraft that do not exist.
"""
import math
import time

# 25-bit Mode S generator polynomial (0x1FFF409)
_GENERATOR = "1111111111111010000001001"

_CHARSET = ("#ABCDEFGHIJKLMNOPQRSTUVWXYZ#####_"
            "###############0123456789######")

# ICAO 24-bit address blocks -> country. Abbreviated to the ranges a European
# receiver actually sees; unknown addresses simply report "".
_ICAO_BLOCKS = [
    (0x004000, 0x0043FF, "Zimbabwe"), (0x008000, 0x00FFFF, "South Africa"),
    (0x010000, 0x017FFF, "Egypt"), (0x024000, 0x0243FF, "Ethiopia"),
    (0x04C000, 0x04C3FF, "Kenya"), (0x0A0000, 0x0A7FFF, "Morocco"),
    (0x0C0000, 0x0C0FFF, "Tunisia"), (0x200000, 0x27FFFF, "Africa (other)"),
    (0x300000, 0x33FFFF, "Italy"), (0x340000, 0x37FFFF, "Spain"),
    (0x380000, 0x3BFFFF, "France"), (0x3C0000, 0x3FFFFF, "Germany"),
    (0x400000, 0x43FFFF, "United Kingdom"), (0x440000, 0x447FFF, "Austria"),
    (0x448000, 0x44FFFF, "Belgium"), (0x450000, 0x457FFF, "Bulgaria"),
    (0x458000, 0x45FFFF, "Denmark"), (0x460000, 0x467FFF, "Finland"),
    (0x468000, 0x46FFFF, "Greece"), (0x470000, 0x477FFF, "Hungary"),
    (0x478000, 0x47FFFF, "Norway"), (0x480000, 0x487FFF, "Netherlands"),
    (0x488000, 0x48FFFF, "Poland"), (0x490000, 0x497FFF, "Portugal"),
    (0x498000, 0x49FFFF, "Czechia"), (0x4A0000, 0x4A7FFF, "Romania"),
    (0x4A8000, 0x4AFFFF, "Sweden"), (0x4B0000, 0x4B7FFF, "Switzerland"),
    (0x4B8000, 0x4BFFFF, "Turkey"), (0x4C0000, 0x4C7FFF, "Serbia"),
    (0x4C8000, 0x4C83FF, "Cyprus"), (0x4CA000, 0x4CAFFF, "Ireland"),
    (0x4CC000, 0x4CCFFF, "Iceland"), (0x4D0000, 0x4D03FF, "Luxembourg"),
    (0x4D2000, 0x4D23FF, "Malta"), (0x500000, 0x5FFFFF, "Europe (other)"),
    (0x600000, 0x6FFFFF, "Middle East / Asia"),
    (0x700000, 0x7FFFFF, "Asia"), (0x800000, 0x8FFFFF, "India / Asia"),
    (0x900000, 0x9FFFFF, "Asia-Pacific"),
    (0xA00000, 0xAFFFFF, "United States"), (0xC00000, 0xC3FFFF, "Canada"),
    (0xC80000, 0xC87FFF, "New Zealand"), (0x7C0000, 0x7CFFFF, "Australia"),
    (0xE00000, 0xEFFFFF, "South America"),
]


def icao_country(icao_hex):
    try:
        v = int(icao_hex, 16)
    except ValueError:
        return ""
    for lo, hi, name in _ICAO_BLOCKS:
        if lo <= v <= hi:
            return name
    return ""


def crc_residual(hexmsg):
    """Zero for an intact frame (DF17 carries plain parity, no address overlay)."""
    bits = list(bin(int(hexmsg, 16))[2:].zfill(len(hexmsg) * 4))
    for i in range(len(bits) - 24):
        if bits[i] == "1":
            for j in range(25):
                bits[i + j] = "1" if bits[i + j] != _GENERATOR[j] else "0"
    return int("".join(bits[-24:]), 2)


def _bits(hexmsg):
    return bin(int(hexmsg, 16))[2:].zfill(len(hexmsg) * 4)


def _altitude(field12):
    """AC12 -> feet, or None when the Q bit says metric/unavailable."""
    if len(field12) != 12 or field12 == "0" * 12:
        return None
    if field12[7] != "1":
        return None
    return int(field12[:7] + field12[8:], 2) * 25 - 1000


def decode_squawk(bits13):
    """
    13-bit ID field -> 4-digit octal Mode A code. The bits are interleaved
    C1 A1 C2 A2 C4 A4 X B1 D1 B2 D2 B4 D4, not in digit order.
    """
    b = [int(c) for c in bits13]
    if len(b) != 13:
        return None
    c1, a1, c2, a2, c4, a4, _x, b1, d1, b2, d2, b4, d4 = b
    a = a4 * 4 + a2 * 2 + a1
    bb = b4 * 4 + b2 * 2 + b1
    c = c4 * 4 + c2 * 2 + c1
    d = d4 * 4 + d2 * 2 + d1
    return f"{a}{bb}{c}{d}"


SQUAWK_MEANING = {
    "7500": "UNLAWFUL INTERFERENCE (hijack)",
    "7600": "RADIO FAILURE",
    "7700": "GENERAL EMERGENCY",
    "7777": "military interceptor",
    "2000": "no code assigned / oceanic entry",
    "1200": "VFR (US)",
    "7000": "VFR conspicuity (Europe)",
}


def decode_ac13(bits13):
    """13-bit AC altitude field (DF4 / DF20) -> feet, or None."""
    if len(bits13) != 13 or bits13 == "0" * 13:
        return None
    m_bit = bits13[6]          # metric
    q_bit = bits13[8]          # 25 ft increments
    if m_bit == "1":
        return None            # metric encoding, rare, not handled
    if q_bit == "1":
        n = int(bits13[:6] + bits13[7] + bits13[9:], 2)
        return n * 25 - 1000
    return None                # Gillham-coded, 100 ft, not handled


def _nl(lat):
    if abs(lat) >= 87:
        return 1
    if lat == 0:
        return 59
    return math.floor(2 * math.pi / math.acos(
        1 - (1 - math.cos(math.pi / 30)) / math.cos(math.radians(abs(lat))) ** 2))


def cpr_position(even, odd, even_is_newer):
    """
    Globally unambiguous airborne CPR. `even`/`odd` are raw (lat, lon) 17-bit
    fields. Returns (lat, lon) or None when the two frames straddle a latitude
    band boundary, where the solution is not unique.
    """
    ye, xe = even[0] / 131072.0, even[1] / 131072.0
    yo, xo = odd[0] / 131072.0, odd[1] / 131072.0
    j = math.floor(59 * ye - 60 * yo + 0.5)
    lat_e = 6.0 * ((j % 60) + ye)
    lat_o = (360.0 / 59) * ((j % 59) + yo)
    if lat_e >= 270:
        lat_e -= 360
    if lat_o >= 270:
        lat_o -= 360
    if _nl(lat_e) != _nl(lat_o):
        return None
    lat = lat_e if even_is_newer else lat_o
    nl = _nl(lat)
    m = math.floor(xe * (nl - 1) - xo * nl + 0.5)
    ni = max(nl, 1) if even_is_newer else max(nl - 1, 1)
    lon = (360.0 / ni) * ((m % ni) + (xe if even_is_newer else xo))
    if lon > 180:
        lon -= 360
    if not (-90 <= lat <= 90):
        return None
    return lat, lon


class Aircraft:
    __slots__ = ("icao", "callsign", "altitude", "speed", "track", "vrate",
                 "lat", "lon", "pos_time", "squawk", "messages", "df_counts",
                 "first_seen", "last_seen",
                 "_even", "_odd", "_even_t", "_odd_t")

    def __init__(self, icao):
        self.icao = icao
        self.callsign = None
        self.altitude = None
        self.speed = None
        self.track = None
        self.vrate = None
        self.lat = None
        self.lon = None
        self.pos_time = None
        self.squawk = None
        self.df_counts = {}
        self.messages = 0
        self.first_seen = time.time()
        self.last_seen = self.first_seen
        self._even = self._odd = None
        self._even_t = self._odd_t = -1e9

    @property
    def country(self):
        return icao_country(self.icao)

    @property
    def squawk_meaning(self):
        return SQUAWK_MEANING.get(self.squawk or "", "")

    @property
    def emergency(self):
        return self.squawk in ("7500", "7600", "7700")

    @property
    def df_summary(self):
        return " ".join(f"DF{k}:{v}" for k, v in sorted(self.df_counts.items()))

    @property
    def maps_url(self):
        if self.lat is None:
            return ""
        return f"https://www.google.com/maps?q={self.lat:.5f},{self.lon:.5f}"

    @property
    def track_url(self):
        return f"https://globe.adsbexchange.com/?icao={self.icao.lower()}"


class AdsbDecoder:
    """
    Accumulates aircraft state from a stream of hex frames.

    Note on replay: position validity is judged from frame *arrival* time,
    which is correct live but meaningless when a saved frame log is replayed,
    since every line then arrives at once. Positions decoded from a file are
    not trustworthy unless the caller supplies real per-frame timestamps.
    """

    MAX_CPR_GAP = 10.0          # seconds between the even/odd frames

    def __init__(self):
        self.aircraft = {}
        self.total = 0
        self.valid = 0
        self._seq = 0

    def stats(self):
        pct = (100.0 * self.valid / self.total) if self.total else 0.0
        return self.total, self.valid, pct

    def prune(self, max_age=300):
        now = time.time()
        for k in [k for k, a in self.aircraft.items() if now - a.last_seen > max_age]:
            del self.aircraft[k]

    def _touch(self, icao, df, create=False):
        ac = self.aircraft.get(icao)
        if ac is None:
            if not create:
                return None
            ac = self.aircraft[icao] = Aircraft(icao)
        ac.messages += 1
        ac.df_counts[df] = ac.df_counts.get(df, 0) + 1
        ac.last_seen = time.time()
        self.valid += 1
        return ac

    def feed(self, line):
        """
        Feed one rtl_adsb output line. Returns the Aircraft it updated.

        Three families are handled:
          DF17            ADS-B squitter, plain parity, self-validating
          DF11            all-call reply, carries the address directly
          DF0/4/5/16/20/21 surveillance replies whose parity is XORed with the
                          aircraft address, so the CRC syndrome *is* the
                          address. These are only accepted for an aircraft
                          already known from DF17/DF11, which is what stops
                          noise from inventing traffic.
        """
        frame = line.strip().strip("*;").strip()
        if not frame:
            return None
        self.total += 1
        if len(frame) not in (14, 28):
            return None
        try:
            residual = crc_residual(frame)
            df = int(frame[:2], 16) >> 3
        except ValueError:
            return None

        if df == 11:
            if residual != 0:
                return None
            return self._touch(frame[2:8].upper(), 11, create=True)

        if df in (0, 4, 5, 16, 20, 21):
            ac = self._touch(f"{residual:06X}", df)
            if ac is None:
                return None
            bits = _bits(frame)
            field = bits[19:32]
            if df in (0, 4, 16, 20):
                alt = decode_ac13(field)
                if alt is not None:
                    ac.altitude = alt
            else:
                sq = decode_squawk(field)
                if sq:
                    ac.squawk = sq
            return ac

        if df != 17 or residual != 0 or len(frame) != 28:
            return None

        self._seq += 1
        ac = self._touch(frame[2:8].upper(), 17, create=True)
        me = _bits(frame)[32:88]
        tc = int(me[:5], 2)

        if 1 <= tc <= 4:
            cs = "".join(_CHARSET[int(me[8 + 6 * i:14 + 6 * i], 2)] for i in range(8))
            cs = cs.replace("#", "").strip("_ ").strip()
            if cs:
                ac.callsign = cs

        elif 9 <= tc <= 18:
            alt = _altitude(me[8:20])
            if alt is not None:
                ac.altitude = alt
            pair = (int(me[22:39], 2), int(me[39:56], 2))
            now = ac.last_seen
            if me[21] == "1":
                ac._odd, ac._odd_t = pair, now
            else:
                ac._even, ac._even_t = pair, now
            # Global CPR is only unambiguous while the two frames describe
            # nearly the same place. Pairing frames minutes apart yields a
            # confident-looking position that can be hundreds of km wrong.
            if (ac._even and ac._odd
                    and abs(ac._even_t - ac._odd_t) <= self.MAX_CPR_GAP):
                pos = cpr_position(ac._even, ac._odd, ac._even_t > ac._odd_t)
                if pos:
                    ac.lat, ac.lon = pos
                    ac.pos_time = now

        elif tc == 19:
            sub = int(me[5:8], 2)
            if sub in (1, 2):
                ew, ns = int(me[14:24], 2), int(me[25:35], 2)
                if ew and ns:
                    vx = (ew - 1) * (-1 if me[13] == "1" else 1)
                    vy = (ns - 1) * (-1 if me[24] == "1" else 1)
                    if sub == 2:
                        vx *= 4
                        vy *= 4
                    ac.speed = round(math.hypot(vx, vy))
                    ac.track = round(math.degrees(math.atan2(vx, vy)) % 360)
                vr_raw = int(me[37:46], 2)
                if vr_raw:
                    ac.vrate = (vr_raw - 1) * 64 * (-1 if me[36] == "1" else 1)
        return ac
