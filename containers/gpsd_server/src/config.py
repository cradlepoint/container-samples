"""Configuration via NCOS appdata.

Every setting is read with ``cp.get_appdata()`` and self-provisions its default
with ``cp.put_appdata()`` on first run, so a fresh deployment produces a
complete, editable set of fields in NCM without the user guessing key names.

All appdata values are strings. Everything here parses defensively: a bad value
logs a warning and falls back to the default rather than crashing the container.
"""

import threading
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, List, Optional

import cp

_DEFAULTS: Dict[str, str] = {
    "gps_poll_interval": "1.0",
    "gps_stale_after": "10",
    "nmea_port": "10110",
    "gpsd_port": "2947",
    "web_port": "8080",
    "history_points": "2000",
    "history_min_move_m": "10",
    "history_min_interval_s": "30",
    "tile_url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    "enable_gps_if_disabled": "false",
}


def _as_float(name: str, raw: Optional[str], default: float, low: float, high: float) -> float:
    if raw is None:
        return default
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        cp.log(f"config: {name}='{raw}' is not a number, using {default}")
        return default
    if not (low <= value <= high):
        cp.log(f"config: {name}={value} outside {low}..{high}, using {default}")
        return default
    return value


def _as_int(name: str, raw: Optional[str], default: int, low: int, high: int) -> int:
    return int(_as_float(name, raw, float(default), float(low), float(high)))


def _as_bool(name: str, raw: Optional[str], default: bool) -> bool:
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in ("1", "true", "yes", "on", "enabled"):
        return True
    if text in ("0", "false", "no", "off", "disabled"):
        return False
    cp.log(f"config: {name}='{raw}' is not a boolean, using {default}")
    return default


@dataclass
class AppConfig:
    """Every field is an immutable scalar, which is what makes copying a
    snapshot as simple as ``dataclasses.replace()``."""

    gps_poll_interval: float = 1.0
    gps_stale_after: float = 10.0
    nmea_port: int = 10110
    gpsd_port: int = 2947
    web_port: int = 8080
    history_points: int = 2000
    history_min_move_m: float = 10.0
    history_min_interval_s: float = 30.0
    tile_url: str = _DEFAULTS["tile_url"]
    enable_gps_if_disabled: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "gps_poll_interval": self.gps_poll_interval,
            "gps_stale_after": self.gps_stale_after,
            "nmea_port": self.nmea_port,
            "gpsd_port": self.gpsd_port,
            "web_port": self.web_port,
            "history_points": self.history_points,
            "history_min_move_m": self.history_min_move_m,
            "history_min_interval_s": self.history_min_interval_s,
            "tile_url": self.tile_url,
            "enable_gps_if_disabled": self.enable_gps_if_disabled,
        }


def provision_defaults() -> None:
    """Create any appdata field that does not exist yet.

    Runs before the first read so NCM shows the full set of knobs immediately
    after deployment.
    """
    created, failed = 0, 0
    for name, default in _DEFAULTS.items():
        try:
            if cp.get_appdata(name) is None:
                # put_appdata verifies by reading back, so this reports what
                # actually happened rather than assuming the write landed.
                if cp.put_appdata(name, default):
                    created += 1
                    cp.log(f"config: provisioned appdata {name}={default}")
                else:
                    failed += 1
        except Exception as exc:  # noqa: BLE001 - never let config kill startup
            failed += 1
            cp.log(f"config: could not provision {name}: {exc}")
    if failed:
        cp.log(
            f"config: {failed} appdata field(s) could not be written "
            f"({created} succeeded). Settings will fall back to built-in defaults."
        )


def load() -> AppConfig:
    """Read the full configuration from appdata."""
    get = cp.get_appdata
    cfg = AppConfig()
    try:
        cfg.gps_poll_interval = _as_float("gps_poll_interval", get("gps_poll_interval"), 1.0, 0.2, 60.0)
        cfg.gps_stale_after = _as_float("gps_stale_after", get("gps_stale_after"), 10.0, 1.0, 3600.0)
        cfg.nmea_port = _as_int("nmea_port", get("nmea_port"), 10110, 1024, 65535)
        cfg.gpsd_port = _as_int("gpsd_port", get("gpsd_port"), 2947, 1024, 65535)
        cfg.web_port = _as_int("web_port", get("web_port"), 8080, 1024, 65535)
        cfg.history_points = _as_int("history_points", get("history_points"), 2000, 10, 20000)
        cfg.history_min_move_m = _as_float("history_min_move_m", get("history_min_move_m"), 10.0, 0.0, 10000.0)
        cfg.history_min_interval_s = _as_float(
            "history_min_interval_s", get("history_min_interval_s"), 30.0, 1.0, 3600.0
        )
        cfg.enable_gps_if_disabled = _as_bool(
            "enable_gps_if_disabled", get("enable_gps_if_disabled"), False
        )
        tile_url = get("tile_url")
        cfg.tile_url = _DEFAULTS["tile_url"] if tile_url is None else str(tile_url).strip()
    except Exception as exc:  # noqa: BLE001
        cp.log(f"config: load failed ({exc}), using defaults")
    return cfg


class ConfigStore:
    """Thread-safe holder for the live configuration.

    The web server updates settings while the poller and web handlers read them,
    so all access goes through one lock. Runtime changes take effect without
    restarting the container.
    """

    def __init__(self, cfg: AppConfig) -> None:
        self._cfg = cfg
        self._lock = threading.Lock()
        self._listeners: List[Callable[[AppConfig], None]] = []

    def snapshot(self) -> AppConfig:
        """A detached copy, safe to read without holding the lock.

        ``replace()`` with no changes copies every field, so adding a setting
        never requires updating this method -- the previous hand-written copy
        listed each field twice and was a standing invitation to miss one.
        """
        with self._lock:
            return replace(self._cfg)

    def on_change(self, callback: Callable[[AppConfig], None]) -> None:
        self._listeners.append(callback)

    def update_runtime(self, changes: Dict[str, Any]) -> Dict[str, Any]:
        """Apply a subset of settings at runtime and mirror them to appdata.

        Only fields that can be changed without restarting a listener are
        accepted; ports are deliberately excluded because the sockets are
        already bound.
        """
        applied: Dict[str, Any] = {}
        with self._lock:
            for key, value in changes.items():
                if key == "gps_poll_interval":
                    self._cfg.gps_poll_interval = _as_float(key, value, self._cfg.gps_poll_interval, 0.2, 60.0)
                    applied[key] = self._cfg.gps_poll_interval
                elif key == "gps_stale_after":
                    self._cfg.gps_stale_after = _as_float(key, value, self._cfg.gps_stale_after, 1.0, 3600.0)
                    applied[key] = self._cfg.gps_stale_after
                elif key == "history_min_move_m":
                    self._cfg.history_min_move_m = _as_float(key, value, self._cfg.history_min_move_m, 0.0, 10000.0)
                    applied[key] = self._cfg.history_min_move_m
                elif key == "history_min_interval_s":
                    self._cfg.history_min_interval_s = _as_float(
                        key, value, self._cfg.history_min_interval_s, 1.0, 3600.0
                    )
                    applied[key] = self._cfg.history_min_interval_s
                elif key == "tile_url":
                    self._cfg.tile_url = str(value).strip()
                    applied[key] = self._cfg.tile_url
            updated = replace(self._cfg)

        for key, value in applied.items():
            try:
                cp.put_appdata(key, "true" if value is True else "false" if value is False else str(value))
            except Exception as exc:  # noqa: BLE001
                cp.log(f"config: could not persist {key}: {exc}")

        for listener in self._listeners:
            try:
                listener(updated)
            except Exception as exc:  # noqa: BLE001
                cp.log(f"config: change listener failed: {exc}")
        return applied
