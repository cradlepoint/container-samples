"""Web UI and JSON API.

Built on the standard library's ThreadingHTTPServer. Flask or FastAPI would add
tens of megabytes of dependencies for endpoints this simple, which is not a
trade worth making on a router.

SECURITY: there is no authentication on this server. It is published with a
Compose port mapping, and mapped ports on NCOS are reachable on WAN as well as
LAN with no firewall filtering. The API exposes the router's physical location.
On anything other than a trusted network, put this on a Local IP Network instead
of a port mapping, or front it with an authenticating proxy. See the README.
"""

import json
import os
import posixpath
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional

import cp

_STATIC_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
_TEMPLATE_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

# Config payloads are small. Anything larger is rejected outright so a large
# POST cannot exhaust memory on a router with 135 MB to spare.
_MAX_BODY_BYTES = 256 * 1024


class AppContext:
    """Everything the request handlers need, passed in rather than imported,
    so the handler has no global state."""

    def __init__(
        self,
        config_store,
        gps_source,
        nmea_server,
        track_store,
        stop_event: threading.Event,
        started_at: float,
        gpsd_port_provider: Callable[[], int],
        config_store_ok_provider: Callable[[], bool],
    ) -> None:
        self.config = config_store
        self.gps = gps_source
        self.nmea = nmea_server
        self.track = track_store
        self.stop_event = stop_event
        self.started_at = started_at
        self.gpsd_port_provider = gpsd_port_provider
        self.config_store_ok_provider = config_store_ok_provider


def _gpsd_reachable(port: int, timeout: float = 2.0) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = "ncos-gpsd-server/1.0"
    protocol_version = "HTTP/1.1"

    # Injected by make_server.
    context: AppContext = None  # type: ignore[assignment]

    def log_message(self, fmt: str, *args: Any) -> None:
        """Suppress per-request access logging.

        The default implementation writes every request to stderr. The UI polls
        once a second, so that buries the application's own log lines in noise.
        Errors are still logged explicitly by the handlers below.
        """
        return

    # ---------------------------------------------------------------- helpers

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json({"error": message}, status=status)

    def _send_file(self, path: str) -> None:
        try:
            with open(path, "rb") as handle:
                body = handle.read()
        except OSError:
            self._send_error_json(404, "not found")
            return
        extension = os.path.splitext(path)[1].lower()
        self.send_response(200)
        self.send_header("Content-Type", _CONTENT_TYPES.get(extension, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        # The UI is served from the container; never cache it, so a redeployed
        # image does not leave stale assets in the browser.
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> Optional[Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send_error_json(400, "invalid Content-Length")
            return None
        if length <= 0:
            self._send_error_json(400, "empty body")
            return None
        if length > _MAX_BODY_BYTES:
            self._send_error_json(413, "body too large")
            return None
        try:
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            self._send_error_json(400, f"invalid JSON: {exc}")
            return None

    @staticmethod
    def _safe_static_path(url_path: str) -> Optional[str]:
        """Resolve a /static/... URL inside the static root.

        Rejects traversal attempts by normalising first and then confirming the
        result is still under the root.
        """
        relative = posixpath.normpath(url_path.lstrip("/"))
        if relative.startswith("..") or relative.startswith("/"):
            return None
        if not relative.startswith("static/"):
            return None
        candidate = os.path.normpath(os.path.join(os.path.dirname(_STATIC_ROOT), relative))
        if not candidate.startswith(_STATIC_ROOT + os.sep):
            return None
        return candidate if os.path.isfile(candidate) else None

    # ------------------------------------------------------------------ verbs

    def do_GET(self) -> None:  # noqa: N802 - required name
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)

        try:
            if route == "/":
                self._send_file(os.path.join(_TEMPLATE_ROOT, "index.html"))
            elif route.startswith("/static/"):
                resolved = self._safe_static_path(parsed.path)
                if resolved is None:
                    self._send_error_json(404, "not found")
                else:
                    self._send_file(resolved)
            elif route == "/health":
                self._handle_health()
            elif route == "/api/status":
                self._handle_status()
            elif route == "/api/history":
                self._handle_history(query)
            elif route == "/api/config":
                self._send_json(self.context.config.snapshot().to_dict())
            else:
                self._send_error_json(404, "not found")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            cp.log(f"web: GET {route} failed: {exc}")
            try:
                self._send_error_json(500, "internal error")
            except OSError:
                pass

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        try:
            if route == "/api/config":
                self._handle_post_config()
            elif route == "/api/history/clear":
                self.context.track.clear()
                self._send_json({"ok": True, "points": 0})
            else:
                self._send_error_json(404, "not found")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            cp.log(f"web: POST {route} failed: {exc}")
            try:
                self._send_error_json(500, "internal error")
            except OSError:
                pass

    # --------------------------------------------------------------- handlers

    def _handle_health(self) -> None:
        """Health for the container as a whole, covering both processes.

        The poller and NMEA listener are threads in this process; gpsd is a
        separate process, so it is checked by connecting to its port. A
        supervised two-process container needs its health check to cover both,
        otherwise gpsd can die while the container still reports healthy.
        """
        gpsd_port = self.context.gpsd_port_provider()
        checks = {
            "gps_poller": self.context.gps.is_alive(),
            "nmea_listener": self.context.nmea.is_alive(),
            "gpsd": _gpsd_reachable(gpsd_port),
        }
        healthy = all(checks.values())
        self._send_json(
            {"healthy": healthy, "checks": checks, "uptime_s": round(time.time() - self.context.started_at, 1)},
            status=200 if healthy else 503,
        )

    def _handle_status(self) -> None:
        fix = self.context.gps.current()
        last_valid = self.context.gps.last_valid()
        cfg = self.context.config.snapshot()
        self._send_json(
            {
                "fix": fix.to_dict(),
                "last_valid": last_valid.to_dict() if last_valid else None,
                "gps": self.context.gps.stats(),
                "nmea": self.context.nmea.stats(),
                "track": self.context.track.stats(),
                "gpsd": {"port": cfg.gpsd_port, "reachable": _gpsd_reachable(cfg.gpsd_port, timeout=1.0)},
                "config_store_ok": self.context.config_store_ok_provider(),
                "tile_url": cfg.tile_url,
                "uptime_s": round(time.time() - self.context.started_at, 1),
                "server_time": time.time(),
            }
        )

    def _handle_history(self, query: Dict[str, List[str]]) -> None:
        since = None
        limit = None
        if "since" in query:
            try:
                since = float(query["since"][0])
            except (TypeError, ValueError):
                self._send_error_json(400, "since must be a number")
                return
        if "limit" in query:
            try:
                limit = max(1, min(20000, int(query["limit"][0])))
            except (TypeError, ValueError):
                self._send_error_json(400, "limit must be an integer")
                return
        points = self.context.track.points(since=since, limit=limit)
        self._send_json({"fields": ["t", "lat", "lon", "knots", "heading"], "points": points})

    def _handle_post_config(self) -> None:
        payload = self._read_body()
        if payload is None:
            return
        if not isinstance(payload, dict):
            self._send_error_json(400, "expected an object")
            return
        applied = self.context.config.update_runtime(payload)
        self._send_json({"ok": True, "applied": applied, "config": self.context.config.snapshot().to_dict()})


def make_server(port: int, context: AppContext) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"context": context})
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    server.daemon_threads = True
    return server


def start(server: ThreadingHTTPServer) -> threading.Thread:
    """Serve in a daemon thread. Stop it with ``stop(server, thread)``."""
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.5}, name="web-server", daemon=True
    )
    thread.start()
    return thread


def stop(server: ThreadingHTTPServer, thread: Optional[threading.Thread] = None) -> None:
    try:
        server.shutdown()
    except Exception:  # noqa: BLE001
        pass
    try:
        server.server_close()
    except OSError:
        pass
    if thread is not None:
        thread.join(timeout=5.0)
