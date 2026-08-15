"""Location history with optional persistence.

Two guards keep flash and memory bounded, which matters on routers with 6-8 GB
of flash shared with everything else:

- The in-memory track is a fixed-length deque, so it can never grow without
  limit no matter how long the container runs.
- Points are only recorded when the receiver has actually moved far enough, or
  when the time floor is reached. A parked vehicle produces one point every
  ``min_interval_s`` instead of one per poll.

Persistence is optional. If the named volume is absent the store falls back to
/tmp and keeps working, non-persistently, rather than failing to start.
"""

import json
import os
import tempfile
import threading
import time
from collections import deque
from typing import Deque, Dict, List, Optional

import cp

from models import Fix, TrackPoint, haversine_m

_PREFERRED_DIR = "/data"
_FALLBACK_DIR = "/tmp"
_FILENAME = "track.json"


def resolve_data_dir() -> str:
    """Use the mounted volume when present, otherwise /tmp."""
    if os.path.isdir(_PREFERRED_DIR) and os.access(_PREFERRED_DIR, os.W_OK):
        return _PREFERRED_DIR
    cp.log(f"history: {_PREFERRED_DIR} not writable, falling back to {_FALLBACK_DIR} (not persistent)")
    return _FALLBACK_DIR


class TrackStore:
    """Bounded in-memory track with periodic atomic snapshots to disk."""

    def __init__(
        self,
        max_points: int = 2000,
        min_move_m: float = 10.0,
        min_interval_s: float = 30.0,
        data_dir: Optional[str] = None,
        save_interval_s: float = 60.0,
    ) -> None:
        self._lock = threading.Lock()
        self._points: Deque[TrackPoint] = deque(maxlen=max_points)
        self._min_move_m = min_move_m
        self._min_interval_s = min_interval_s
        self._data_dir = data_dir if data_dir is not None else resolve_data_dir()
        self._path = os.path.join(self._data_dir, _FILENAME)
        self._save_interval_s = save_interval_s
        self._last_save = 0.0
        self._dirty = False
        self._persistent = self._data_dir == _PREFERRED_DIR

    @property
    def path(self) -> str:
        return self._path

    @property
    def persistent(self) -> bool:
        return self._persistent

    def apply_config(self, min_move_m: float, min_interval_s: float, max_points: int) -> None:
        with self._lock:
            self._min_move_m = min_move_m
            self._min_interval_s = min_interval_s
            if max_points != self._points.maxlen:
                self._points = deque(self._points, maxlen=max_points)

    def load(self) -> int:
        """Restore a previous track. Returns the number of points loaded."""
        try:
            if not os.path.isfile(self._path):
                return 0
            with open(self._path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            if not isinstance(raw, list):
                cp.log("history: saved track is not a list, ignoring")
                return 0
            restored = [point for point in (TrackPoint.from_list(entry) for entry in raw) if point]
            with self._lock:
                self._points.extend(restored)
                count = len(self._points)
            cp.log(f"history: restored {count} points from {self._path}")
            return count
        except (OSError, ValueError) as exc:
            cp.log(f"history: could not load saved track: {exc}")
            return 0

    def on_fix(self, fix: Fix) -> None:
        """Record a breadcrumb if it clears the movement or time threshold."""
        if not fix.valid:
            return
        with self._lock:
            should_record = True
            if self._points:
                last = self._points[-1]
                moved = haversine_m(last.latitude, last.longitude, fix.latitude, fix.longitude)
                elapsed = fix.sampled_at - last.timestamp
                should_record = moved >= self._min_move_m or elapsed >= self._min_interval_s
            if not should_record:
                return
            self._points.append(
                TrackPoint(
                    timestamp=fix.sampled_at,
                    latitude=fix.latitude,
                    longitude=fix.longitude,
                    speed_knots=fix.speed_knots,
                    heading=fix.heading,
                )
            )
            self._dirty = True

    def points(self, since: Optional[float] = None, limit: Optional[int] = None) -> List[List]:
        with self._lock:
            selected = [
                point for point in self._points if since is None or point.timestamp > since
            ]
        if limit is not None and len(selected) > limit:
            selected = selected[-limit:]
        return [point.to_list() for point in selected]

    def stats(self) -> Dict[str, object]:
        with self._lock:
            return {
                "points": len(self._points),
                "max_points": self._points.maxlen,
                "persistent": self._persistent,
                "path": self._path,
                "min_move_m": self._min_move_m,
                "min_interval_s": self._min_interval_s,
            }

    def clear(self) -> None:
        with self._lock:
            self._points.clear()
            self._dirty = True
        self.save(force=True)

    def save(self, force: bool = False) -> bool:
        """Write a snapshot via temp file plus rename so a crash mid-write
        cannot leave a truncated track behind."""
        now = time.time()
        with self._lock:
            if not force and (not self._dirty or (now - self._last_save) < self._save_interval_s):
                return False
            payload = [point.to_list() for point in self._points]
            self._last_save = now
            self._dirty = False
        try:
            directory = os.path.dirname(self._path) or "."
            handle = tempfile.NamedTemporaryFile(
                mode="w", dir=directory, prefix=".track-", suffix=".tmp", delete=False, encoding="utf-8"
            )
            try:
                json.dump(payload, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                handle.close()
            os.replace(handle.name, self._path)
            return True
        except OSError as exc:
            cp.log(f"history: save failed: {exc}")
            return False

    def run_saver(self, stop_event: threading.Event) -> None:
        """Periodic snapshot loop; run in a daemon thread."""
        while not stop_event.is_set():
            stop_event.wait(self._save_interval_s)
            if stop_event.is_set():
                break
            self.save()
        self.save(force=True)
