"""Data models for the gpsd server.

Kept as plain dataclasses so the whole application stays dependency-free
beyond the standard library and cp.py.
"""

import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Fix:
    """A single GPS fix sampled from the router's Config Store.

    A Fix is always produced by every poll cycle, even when the router has no
    lock. Callers must check ``valid`` rather than assuming coordinates are
    present -- serving a stale position as though it were current is the
    failure mode that matters most in tracking applications.
    """

    latitude: Optional[float] = None
    longitude: Optional[float] = None
    altitude_m: Optional[float] = None
    speed_knots: Optional[float] = None
    heading: Optional[float] = None
    accuracy_m: Optional[float] = None
    satellites: int = 0
    lock: bool = False
    age_s: Optional[float] = None
    # Wall-clock time at which this sample was taken by the container.
    sampled_at: float = field(default_factory=time.time)

    @property
    def valid(self) -> bool:
        """True only when the router reports a lock and usable coordinates."""
        return bool(
            self.lock
            and self.latitude is not None
            and self.longitude is not None
            and -90.0 <= self.latitude <= 90.0
            and -180.0 <= self.longitude <= 180.0
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "altitude_m": self.altitude_m,
            "speed_knots": self.speed_knots,
            "speed_kph": None if self.speed_knots is None else round(self.speed_knots * 1.852, 2),
            "heading": self.heading,
            "accuracy_m": self.accuracy_m,
            "satellites": self.satellites,
            "lock": self.lock,
            "age_s": self.age_s,
            "sampled_at": self.sampled_at,
            "valid": self.valid,
        }


@dataclass
class TrackPoint:
    """One recorded breadcrumb in the location history."""

    timestamp: float
    latitude: float
    longitude: float
    speed_knots: Optional[float] = None
    heading: Optional[float] = None

    def to_list(self) -> List[Any]:
        """Compact list form. History payloads can hold thousands of points,
        so the wire format avoids repeating key names for every entry."""
        return [
            round(self.timestamp, 1),
            self.latitude,
            self.longitude,
            self.speed_knots,
            self.heading,
        ]

    @staticmethod
    def from_list(raw: List[Any]) -> Optional["TrackPoint"]:
        try:
            return TrackPoint(
                timestamp=float(raw[0]),
                latitude=float(raw[1]),
                longitude=float(raw[2]),
                speed_knots=None if raw[3] is None else float(raw[3]),
                heading=None if raw[4] is None else float(raw[4]),
            )
        except (TypeError, ValueError, IndexError):
            return None


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two decimal-degree points."""
    earth_radius_m = 6371008.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2.0) ** 2
    return 2.0 * earth_radius_m * math.asin(min(1.0, math.sqrt(a)))
