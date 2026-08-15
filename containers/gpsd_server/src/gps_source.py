"""Poll the router's Config Store for GPS fixes.

Polling was chosen over consuming the router's native NMEA stream so the data
path through cs.sock stays explicit and the sample works on any model without
first configuring a GPS connection. ``status/gps`` is common across models.

The poller emits a Fix on every cycle, valid or not, and marks the fix invalid
once it exceeds the configured staleness window. Downstream consumers get an
honest signal instead of a frozen position.
"""

import threading
import time
from typing import Callable, Dict, List, Optional

import cp

from models import Fix

# The only router config path this container is ever allowed to write.
# Any write attempt outside this set is refused, so a bug or a malicious request
# cannot reconfigure the router through the container.
_WRITE_ALLOWLIST = ("config/system/gps/enabled",)


def _coerce_float(value: object) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _dms_to_decimal(component: object) -> Optional[float]:
    """Convert one ``{degree, minute, second}`` dict to signed decimal degrees.

    The sign lives on the degree component, which cp.dec() already handles.
    """
    if not isinstance(component, dict):
        return None
    degree = component.get("degree")
    if degree is None:
        return None
    try:
        return cp.dec(degree, component.get("minute", 0) or 0, component.get("second", 0) or 0)
    except Exception:  # noqa: BLE001
        return None


def parse_fix(raw: Optional[Dict], sampled_at: Optional[float] = None) -> Fix:
    """Build a Fix from a ``status/gps/fix`` payload.

    Returns an invalid Fix rather than raising when the payload is missing or
    malformed; the router legitimately has no GPS data before first lock.
    """
    sampled_at = time.time() if sampled_at is None else sampled_at
    if not isinstance(raw, dict):
        return Fix(sampled_at=sampled_at)

    return Fix(
        latitude=_dms_to_decimal(raw.get("latitude")),
        longitude=_dms_to_decimal(raw.get("longitude")),
        altitude_m=_coerce_float(raw.get("altitude_meters")),
        speed_knots=_coerce_float(raw.get("ground_speed_knots")),
        heading=_coerce_float(raw.get("heading")),
        accuracy_m=_coerce_float(raw.get("accuracy")),
        satellites=int(_coerce_float(raw.get("satellites")) or 0),
        lock=bool(raw.get("lock", False)),
        age_s=_coerce_float(raw.get("age")),
        sampled_at=sampled_at,
    )


def ensure_gps_enabled(allowed: bool) -> None:
    """Optionally turn GPS on if the router shipped with it disabled.

    Off by default. Even when enabled, the write is attempted only if the path
    already exists and reads as a boolean False -- proving it is present and
    the expected shape on this firmware. If the read returns None the path is
    absent on this model and the write is skipped with a log line rather than
    blindly writing into an unknown tree.
    """
    path = "config/system/gps/enabled"
    if path not in _WRITE_ALLOWLIST:  # pragma: no cover - defensive
        cp.log(f"gps: refusing write to non-allowlisted path {path}")
        return
    try:
        current = cp.get(path)
    except Exception as exc:  # noqa: BLE001
        cp.log(f"gps: could not read {path}: {exc}")
        return

    if current is None:
        cp.log(f"gps: {path} not present on this firmware, leaving GPS configuration alone")
        return
    if current is True:
        cp.log("gps: GPS already enabled on the router")
        return
    if not allowed:
        cp.log(
            "gps: GPS is disabled on the router and enable_gps_if_disabled is false. "
            "No fixes will be produced until GPS is enabled in NCM."
        )
        return
    try:
        cp.put(path, True)
        cp.log(f"gps: enabled GPS via {path}")
    except Exception as exc:  # noqa: BLE001
        cp.log(f"gps: failed to enable GPS: {exc}")


class GpsSource:
    """Background poller holding the most recent Fix."""

    def __init__(self, stop_event: threading.Event, poll_interval: float, stale_after: float) -> None:
        self._stop = stop_event
        self._poll_interval = poll_interval
        self._stale_after = stale_after
        self._lock = threading.Lock()
        self._fix = Fix()
        self._last_valid: Optional[Fix] = None
        self._listeners: List[Callable[[Fix], None]] = []
        self._thread: Optional[threading.Thread] = None
        self._poll_count = 0
        self._error_count = 0
        self._consecutive_errors = 0

    def add_listener(self, callback: Callable[[Fix], None]) -> None:
        """Register a callback invoked once per poll with the new Fix."""
        self._listeners.append(callback)

    def apply_config(self, poll_interval: float, stale_after: float) -> None:
        with self._lock:
            self._poll_interval = poll_interval
            self._stale_after = stale_after

    def current(self) -> Fix:
        """The latest Fix, downgraded to invalid if it has gone stale."""
        with self._lock:
            fix, stale_after = self._fix, self._stale_after
        if fix.valid and (time.time() - fix.sampled_at) > stale_after:
            stale = Fix(
                satellites=fix.satellites,
                lock=False,
                age_s=fix.age_s,
                sampled_at=fix.sampled_at,
            )
            return stale
        return fix

    def last_valid(self) -> Optional[Fix]:
        """Last known good fix, for display purposes only.

        Never fed to the NMEA output -- the map can usefully show where the
        vehicle was last seen, but a GPS client must not be told that position
        is current.
        """
        with self._lock:
            return self._last_valid

    def stats(self) -> Dict[str, object]:
        with self._lock:
            return {
                "polls": self._poll_count,
                "errors": self._error_count,
                "consecutive_errors": self._consecutive_errors,
                "poll_interval": self._poll_interval,
                "stale_after": self._stale_after,
            }

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="gps-poller", daemon=True)
        self._thread.start()

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self) -> None:
        cp.log("gps: poller started")
        while not self._stop.is_set():
            started = time.time()
            try:
                raw = cp.get("status/gps/fix")
                fix = parse_fix(raw, started)
                with self._lock:
                    self._fix = fix
                    if fix.valid:
                        self._last_valid = fix
                    self._poll_count += 1
                    self._consecutive_errors = 0
            except Exception as exc:  # noqa: BLE001
                fix = Fix(sampled_at=started)
                with self._lock:
                    self._fix = fix
                    self._error_count += 1
                    self._consecutive_errors += 1
                    consecutive = self._consecutive_errors
                # Log the first failure, then back off to every 60th so a
                # persistent Config Store problem cannot flood the log.
                if consecutive == 1 or consecutive % 60 == 0:
                    cp.log(f"gps: poll failed ({consecutive} in a row): {exc}")

            for listener in self._listeners:
                try:
                    listener(fix)
                except Exception as exc:  # noqa: BLE001
                    cp.log(f"gps: listener error: {exc}")

            with self._lock:
                interval = self._poll_interval
            elapsed = time.time() - started
            self._stop.wait(max(0.05, interval - elapsed))
        cp.log("gps: poller stopped")
