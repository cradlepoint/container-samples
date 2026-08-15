"""NMEA 0183 sentence synthesis from a Config Store fix.

The router reports position as degrees/minutes/seconds with the sign carried on
the degree component. cp.dec() converts that to signed decimal degrees; NMEA
then wants it back as ``ddmm.mmmm`` plus a separate hemisphere character, so the
sign has to be stripped and re-expressed. Getting this wrong flips positions
across the equator or the prime meridian, which is why it lives in one place
with tests around it.

When the fix is not valid this module emits well-formed sentences that say so
(GGA quality 0, RMC status V, empty coordinate fields) rather than repeating the
last known position. Consumers can then distinguish "no data" from "not moving".
"""

import time
from typing import List, Optional, Tuple

from models import Fix


def checksum(body: str) -> str:
    """XOR of every character between '$' and '*', as two uppercase hex digits."""
    value = 0
    for char in body:
        value ^= ord(char)
    return f"{value:02X}"


def sentence(body: str) -> str:
    """Wrap a sentence body with '$', its checksum and CRLF."""
    return f"${body}*{checksum(body)}\r\n"


def _format_angle(value: Optional[float], degree_width: int, positive: str, negative: str) -> Tuple[str, str]:
    """Return (ddmm.mmmm, hemisphere) for a signed decimal degree value."""
    if value is None:
        return "", ""
    hemisphere = positive if value >= 0 else negative
    magnitude = abs(value)
    degrees = int(magnitude)
    minutes = (magnitude - degrees) * 60.0
    # Rounding minutes to 4dp can carry to 60.0000; promote it to the next degree.
    if round(minutes, 4) >= 60.0:
        degrees += 1
        minutes = 0.0
    return f"{degrees:0{degree_width}d}{minutes:07.4f}", hemisphere


def format_latitude(latitude: Optional[float]) -> Tuple[str, str]:
    return _format_angle(latitude, 2, "N", "S")


def format_longitude(longitude: Optional[float]) -> Tuple[str, str]:
    return _format_angle(longitude, 3, "E", "W")


def _utc_parts(epoch: float) -> Tuple[str, str]:
    """Return (hhmmss.ss, ddmmyy) in UTC."""
    stamp = time.gmtime(epoch)
    fractional = epoch - int(epoch)
    hhmmss = f"{stamp.tm_hour:02d}{stamp.tm_min:02d}{stamp.tm_sec:02d}.{int(fractional * 100):02d}"
    ddmmyy = f"{stamp.tm_mday:02d}{stamp.tm_mon:02d}{stamp.tm_year % 100:02d}"
    return hhmmss, ddmmyy


def _number(value: Optional[float], fmt: str) -> str:
    return "" if value is None else format(value, fmt)


def gga(fix: Fix, epoch: Optional[float] = None) -> str:
    """Global positioning system fix data."""
    epoch = time.time() if epoch is None else epoch
    hhmmss, _ = _utc_parts(epoch)
    lat, lat_hemisphere = format_latitude(fix.latitude if fix.valid else None)
    lon, lon_hemisphere = format_longitude(fix.longitude if fix.valid else None)
    quality = "1" if fix.valid else "0"
    satellites = f"{max(0, int(fix.satellites or 0)):02d}"
    # HDOP is not reported by the Config Store. Accuracy in metres is a
    # different quantity, so the field is left empty rather than faked.
    hdop = ""
    altitude = _number(fix.altitude_m, ".1f") if fix.valid else ""
    body = (
        f"GPGGA,{hhmmss},{lat},{lat_hemisphere},{lon},{lon_hemisphere},"
        f"{quality},{satellites},{hdop},{altitude},M,,M,,"
    )
    return sentence(body)


def rmc(fix: Fix, epoch: Optional[float] = None) -> str:
    """Recommended minimum navigation information."""
    epoch = time.time() if epoch is None else epoch
    hhmmss, ddmmyy = _utc_parts(epoch)
    lat, lat_hemisphere = format_latitude(fix.latitude if fix.valid else None)
    lon, lon_hemisphere = format_longitude(fix.longitude if fix.valid else None)
    status = "A" if fix.valid else "V"
    speed = _number(fix.speed_knots, ".2f") if fix.valid else ""
    heading = _number(fix.heading, ".1f") if fix.valid else ""
    mode = "A" if fix.valid else "N"
    body = (
        f"GPRMC,{hhmmss},{status},{lat},{lat_hemisphere},{lon},{lon_hemisphere},"
        f"{speed},{heading},{ddmmyy},,,{mode}"
    )
    return sentence(body)


def vtg(fix: Fix) -> str:
    """Course over ground and ground speed."""
    heading = _number(fix.heading, ".1f") if fix.valid else ""
    knots = _number(fix.speed_knots, ".2f") if fix.valid else ""
    kph = _number(None if fix.speed_knots is None else fix.speed_knots * 1.852, ".2f") if fix.valid else ""
    mode = "A" if fix.valid else "N"
    body = f"GPVTG,{heading},T,,M,{knots},N,{kph},K,{mode}"
    return sentence(body)


def sentences_for(fix: Fix, epoch: Optional[float] = None) -> List[str]:
    """The sentence set emitted once per interval.

    RMC and GGA together give gpsd position, speed, course, date and fix
    quality, which is everything needed to produce a TPV report. VTG is
    included because some older clients read course only from VTG.
    """
    epoch = time.time() if epoch is None else epoch
    return [rmc(fix, epoch), gga(fix, epoch), vtg(fix)]
