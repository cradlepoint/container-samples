"""Web Server module for Edge AI Person Detection.

Provides MJPEG streaming and configuration API via Python's built-in
http.server module. Handles connection limiting, placeholder images,
frame encoding, configuration validation, stats reporting, and
primary-user session control.

Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8
"""
import sys
import os
import io
import time
import threading

try:
    import numpy
except ImportError:
    numpy = None

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = None
    ImageDraw = None
    ImageFont = None

# Add parent directory to path so cp module is importable
_parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)

import cp

try:
    from http.server import HTTPServer, BaseHTTPRequestHandler
    from socketserver import ThreadingMixIn

    class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
        """Handle each request in a new thread."""
        daemon_threads = True

except ImportError:
    from BaseHTTPServer import HTTPServer, BaseHTTPRequestHandler
    ThreadedHTTPServer = HTTPServer


# MIME boundary for multipart MJPEG stream
MJPEG_BOUNDARY = "frame"


class MJPEGStreamHandler(object):
    """Handles MJPEG frame encoding and formatting for streaming.

    Encodes numpy frames as JPEG with configurable quality and formats
    them as multipart chunks suitable for MJPEG streaming.

    Attributes:
        quality: JPEG encoding quality (1-100).
    """

    def __init__(self, quality=70):
        # type: (int) -> None
        """Initialize MJPEGStreamHandler.

        Args:
            quality: JPEG encoding quality, 1-100 (default 70).
        """
        self.quality = quality

    def stream_frame(self, frame, quality=None):
        # type: (numpy.ndarray, int) -> bytes
        """Encode frame as JPEG and format as multipart chunk.

        Uses OpenCV imencode for fast JPEG encoding directly from numpy.

        Args:
            frame: Input frame as a numpy array (H, W, C) in BGR format.
            quality: Optional override for JPEG quality (1-100).
                     Uses instance quality if not specified.

        Returns:
            Bytes containing the formatted multipart JPEG chunk with
            boundary, content-type header, content-length, and JPEG data.
            Returns empty bytes if encoding fails.
        """
        if frame is None:
            return b""

        q = quality if quality is not None else self.quality
        q = max(1, min(100, q))

        try:
            import cv2
            encode_params = [cv2.IMWRITE_JPEG_QUALITY, q]
            success, jpeg_data = cv2.imencode('.jpg', frame, encode_params)
            if not success:
                return b""
            jpeg_bytes = jpeg_data.tobytes()
        except ImportError:
            # Fallback to PIL if cv2 not available
            if Image is None:
                return b""
            try:
                rgb_array = frame[:, :, ::-1]
                pil_image = Image.fromarray(rgb_array)
                buffer = io.BytesIO()
                pil_image.save(buffer, format='JPEG', quality=q)
                jpeg_bytes = buffer.getvalue()
            except Exception:
                return b""

        # Format as multipart chunk
        chunk = b"--" + MJPEG_BOUNDARY.encode("ascii") + b"\r\n"
        chunk += b"Content-Type: image/jpeg\r\n"
        chunk += b"Content-Length: " + str(len(jpeg_bytes)).encode("ascii") + b"\r\n"
        chunk += b"\r\n"
        chunk += jpeg_bytes
        chunk += b"\r\n"

        return chunk


def _create_placeholder_image():
    # type: () -> numpy.ndarray
    """Create a placeholder image indicating detection is stopped.

    Returns:
        A 1920x1080 black BGR numpy array with centered white text.
    """
    if numpy is None:
        return None

    # Create a black 1920x1080 image
    img = numpy.zeros((1080, 1920, 3), dtype=numpy.uint8)

    try:
        import cv2
        text = "Detection Stopped"
        font = cv2.FONT_HERSHEY_SIMPLEX
        font_scale = 2.0
        thickness = 3
        (text_w, text_h), baseline = cv2.getTextSize(text, font, font_scale, thickness)
        text_x = (1920 - text_w) // 2
        text_y = (1080 + text_h) // 2
        cv2.putText(img, text, (text_x, text_y), font, font_scale,
                    (255, 255, 255), thickness)
    except ImportError:
        pass

    return img


# Paths for serving static files and templates
_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
_TEMPLATES_DIR = os.path.join(_SRC_DIR, 'templates')
_STATIC_DIR = os.path.join(_SRC_DIR, 'static')

# MIME types for static file serving
_MIME_TYPES = {
    '.html': 'text/html; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.js': 'application/javascript; charset=utf-8',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.gif': 'image/gif',
    '.svg': 'image/svg+xml',
    '.ico': 'image/x-icon',
    '.woff': 'font/woff',
    '.woff2': 'font/woff2',
    '.ttf': 'font/ttf',
}


def _read_template(filename):
    # type: (str) -> str
    """Read an HTML template file from the templates directory.

    Args:
        filename: Name of the template file (e.g. 'index.html').

    Returns:
        The file contents as a string, or a fallback error page if not found.
    """
    filepath = os.path.join(_TEMPLATES_DIR, filename)
    try:
        with open(filepath, 'r') as f:
            return f.read()
    except (IOError, OSError):
        return '<html><body><h1>Edge AI</h1><p>Template not found.</p></body></html>'


def _build_index_html():
    # type: () -> str
    """Load the Edge AI web interface from the templates/index.html file.

    Returns ES5-compatible HTML with MJPEG video display, configuration
    controls, client-side validation, and stats display.

    Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7
    """
    return _read_template('index.html')


class WebServer(object):
    """HTTP server for MJPEG video streaming and configuration API.

    Serves annotated video frames as an MJPEG stream, enforces connection
    limits, provides configuration validation, and reports runtime stats.

    Attributes:
        port: HTTP server port number.
        processor: FrameProcessor instance providing current_frame.
        max_clients: Maximum simultaneous streaming connections.
        jpeg_quality: JPEG encoding quality (1-100).
    """

    def __init__(self, port, processor, max_clients=4, jpeg_quality=70):
        # type: (int, object, int, int) -> None
        """Initialize WebServer.

        Args:
            port: HTTP port to listen on (1024-65535).
            processor: FrameProcessor instance with current_frame property.
            max_clients: Maximum simultaneous streaming clients (default 4).
            jpeg_quality: JPEG encoding quality 1-100 (default 70).
        """
        self.port = port
        self.processor = processor
        self.max_clients = max_clients
        self.jpeg_quality = jpeg_quality

        self._stream_handler = MJPEGStreamHandler(quality=jpeg_quality)
        self._placeholder_image = _create_placeholder_image()
        self._client_count = 0
        self._client_lock = threading.Lock()
        self._server = None
        self._start_time = time.time()

        # Stats tracking
        self._total_detections = 0
        self._inference_times = []  # type: list
        self._stats_lock = threading.Lock()

        # Session control: primary user tracking
        self._primary_session_id = None
        self._primary_lock = threading.Lock()
        self._session_last_seen = {}  # type: dict  # session_id -> timestamp
        self._session_counter = 0
        self._SESSION_TIMEOUT = 10.0  # seconds before a session is considered dead

    def _increment_clients(self):
        # type: () -> bool
        """Attempt to increment the client count.

        Returns:
            True if a client slot was available and count was incremented,
            False if the maximum client limit has been reached.
        """
        with self._client_lock:
            if self._client_count >= self.max_clients:
                return False
            self._client_count += 1
            return True

    def _decrement_clients(self):
        # type: () -> None
        """Decrement the client count when a client disconnects."""
        with self._client_lock:
            self._client_count = max(0, self._client_count - 1)

    def get_client_count(self):
        # type: () -> int
        """Get the current number of connected streaming clients.

        Returns:
            Current client count.
        """
        with self._client_lock:
            return self._client_count

    def create_session(self):
        # type: () -> tuple
        """Create a new session and determine if it becomes primary.

        Returns:
            A tuple (session_id, is_primary) where session_id is a unique
            string identifier and is_primary indicates whether this session
            has control.
        """
        with self._primary_lock:
            self._session_counter += 1
            session_id = "{0}_{1}".format(
                int(time.time() * 1000), self._session_counter)
            self._session_last_seen[session_id] = time.time()

            if self._primary_session_id is None:
                self._primary_session_id = session_id
                return (session_id, True)
            return (session_id, False)

    def heartbeat(self, session_id):
        # type: (str) -> bool
        """Process a heartbeat from a session.

        Updates the session's last-seen timestamp. If the current primary
        session has timed out, promotes this session to primary.

        Args:
            session_id: The session identifier.

        Returns:
            True if this session is now the primary, False otherwise.
        """
        now = time.time()
        with self._primary_lock:
            self._session_last_seen[session_id] = now

            # Check if primary has timed out
            if self._primary_session_id is not None:
                primary_last = self._session_last_seen.get(
                    self._primary_session_id, 0)
                if now - primary_last > self._SESSION_TIMEOUT:
                    # Primary timed out, promote this session
                    self._primary_session_id = session_id
                    return True

            # If no primary exists, promote this session
            if self._primary_session_id is None:
                self._primary_session_id = session_id
                return True

            return self._primary_session_id == session_id

    def is_primary_session(self, session_id):
        # type: (str) -> bool
        """Check if the given session_id is the current primary.

        Args:
            session_id: The session identifier to check.

        Returns:
            True if session_id is the primary session.
        """
        with self._primary_lock:
            return self._primary_session_id == session_id

    def get_frame(self):
        # type: () -> numpy.ndarray
        """Get the current frame to serve.

        Returns the placeholder when detection is stopped, or the
        processor's current annotated frame when running.

        Returns:
            Frame as a numpy array (BGR), or placeholder if stopped/unavailable.
        """
        # Always show placeholder when detection is stopped
        if self.processor is not None:
            if hasattr(self.processor, 'is_running') and not self.processor.is_running:
                return self._placeholder_image

        frame = None
        if self.processor is not None:
            frame = self.processor.current_frame
        if frame is None:
            frame = self._placeholder_image
        return frame

    @staticmethod
    def validate_config_input(field, value):
        # type: (str, str) -> tuple
        """Validate a configuration input value.

        Validates the following fields:
        - confidence_threshold: numeric, range [0.1, 1.0]
        - target_fps: integer, range [1, 30]
        - rtsp_url: string, must start with "rtsp://", max 2048 chars
        - input_resolution: must be one of "320x240", "640x480", "1280x720"

        Args:
            field: The configuration field name to validate.
            value: The string value to validate.

        Returns:
            A tuple (valid, error_message) where valid is True if the value
            is acceptable, and error_message describes the validation failure
            (empty string if valid).
        """
        if field == "confidence_threshold":
            try:
                v = float(value)
            except (ValueError, TypeError):
                return (False, "confidence_threshold must be a numeric value")
            if v < 0.1 or v > 1.0:
                return (False, "confidence_threshold must be between 0.1 and 1.0")
            return (True, "")

        elif field == "target_fps":
            try:
                v = int(value)
            except (ValueError, TypeError):
                return (False, "target_fps must be an integer value")
            if v < 1 or v > 30:
                return (False, "target_fps must be between 1 and 30")
            return (True, "")

        elif field == "rtsp_url":
            if not isinstance(value, str):
                return (False, "rtsp_url must be a string")
            if len(value) > 2048:
                return (False, "rtsp_url must not exceed 2048 characters")
            if not value.startswith("rtsp://"):
                return (False, "rtsp_url must start with 'rtsp://'")
            return (True, "")

        elif field == "input_resolution":
            valid_resolutions = ["320x240", "640x480", "1280x720"]
            if value not in valid_resolutions:
                return (False, "input_resolution must be one of: 320x240, 640x480, 1280x720")
            return (True, "")

        else:
            return (False, "Unknown configuration field: {}".format(field))

    def get_stats(self):
        # type: () -> dict
        """Return current operational statistics.

        Returns:
            Dictionary with keys:
            - current_fps: Current processing FPS (float)
            - total_detections: Total detections since start (int)
            - avg_inference_ms: Average inference time in ms (float)
            - connection_status: RTSP connection state (str)
            - stream_resolution: Current stream frame dimensions (str)
            - model_input: Model input dimensions (str)
            - model_name: Model filename (str)
        """
        # Get FPS from processor's FPS calculator if available
        current_fps = 0.0
        if hasattr(self.processor, '_fps_calculator'):
            current_fps = self.processor._fps_calculator.get_fps()

        # Get total detections from processor
        total_detections = 0
        if hasattr(self.processor, '_detection_count'):
            total_detections = self.processor._detection_count

        # Get average inference time
        avg_inference_ms = 0.0
        if hasattr(self.processor, '_inference_times') and self.processor._inference_times:
            times = self.processor._inference_times
            if len(times) > 0:
                avg_inference_ms = sum(times) / len(times)

        # Get connection status from capture
        connection_status = "disconnected"
        if hasattr(self.processor, 'capture'):
            capture = self.processor.capture
            if hasattr(capture, 'is_connected'):
                if capture.is_connected:
                    connection_status = "connected"
                elif hasattr(capture, '_reconnecting') and capture._reconnecting:
                    connection_status = "reconnecting"

        # Get stream resolution from current frame
        stream_resolution = "--"
        if self.processor is not None:
            try:
                frame = self.processor.current_frame
                if frame is not None and hasattr(frame, 'shape') and len(frame.shape) >= 2:
                    h, w = frame.shape[0], frame.shape[1]
                    stream_resolution = "{}x{}".format(w, h)
            except (TypeError, AttributeError):
                pass

        # Get model info from engine
        model_input = "300x300"
        model_name = "SSD MobileNet V2"
        model_key = "ssd_mobilenet_v2"
        if hasattr(self.processor, 'engine'):
            engine = self.processor.engine
            if hasattr(engine, 'input_size'):
                try:
                    size = engine.input_size
                    if size and len(size) == 2:
                        iw, ih = size
                        model_input = "{}x{}".format(iw, ih)
                except (TypeError, ValueError):
                    pass
            if hasattr(engine, 'model_path'):
                import os
                try:
                    model_name = os.path.basename(engine.model_path)
                    if 'yolov5n' in engine.model_path:
                        model_key = 'yolov5n'
                    else:
                        model_key = 'ssd_mobilenet_v2'
                except (TypeError, AttributeError):
                    pass

        return {
            "current_fps": current_fps,
            "total_detections": total_detections,
            "avg_inference_ms": round(avg_inference_ms, 1),
            "connection_status": connection_status,
            "stream_resolution": stream_resolution,
            "model_input": model_input,
            "model_name": model_name,
            "model_key": model_key,
            "detection_state": "running" if (
                self.processor is not None and
                hasattr(self.processor, 'is_running') and
                self.processor.is_running
            ) else "stopped",
            "cpu_percent": self._get_cpu_percent(),
            "mem_percent": self._get_mem_percent(),
            "mem_used_mb": self._get_mem_used_mb(),
            "source_fps": self._get_source_fps(),
            "fps_percent": self._get_fps_percent(),
        }

    def _get_cpu_percent(self):
        # type: () -> float
        """Get current process CPU usage percentage.

        Returns:
            CPU usage as a percentage (0-100), or 0.0 if unavailable.
        """
        try:
            import resource
            # Use /proc/stat for container CPU on Linux
            with open('/proc/stat', 'r') as f:
                line = f.readline()
                parts = line.split()
                # cpu user nice system idle iowait irq softirq
                idle = int(parts[4])
                total = sum(int(p) for p in parts[1:8])
                if not hasattr(self, '_last_cpu_idle'):
                    self._last_cpu_idle = idle
                    self._last_cpu_total = total
                    return 0.0
                idle_delta = idle - self._last_cpu_idle
                total_delta = total - self._last_cpu_total
                self._last_cpu_idle = idle
                self._last_cpu_total = total
                if total_delta == 0:
                    return 0.0
                return round((1.0 - float(idle_delta) / float(total_delta)) * 100.0, 1)
        except Exception:
            return 0.0

    def _get_mem_percent(self):
        # type: () -> float
        """Get current process memory usage percentage.

        Returns:
            Memory usage as a percentage (0-100), or 0.0 if unavailable.
        """
        try:
            with open('/proc/meminfo', 'r') as f:
                lines = f.readlines()
            mem_total = 0
            mem_available = 0
            for line in lines:
                if line.startswith('MemTotal:'):
                    mem_total = int(line.split()[1])
                elif line.startswith('MemAvailable:'):
                    mem_available = int(line.split()[1])
            if mem_total == 0:
                return 0.0
            used = mem_total - mem_available
            return round(float(used) / float(mem_total) * 100.0, 1)
        except Exception:
            return 0.0

    def _get_mem_used_mb(self):
        # type: () -> float
        """Get current process memory usage in MB.

        Returns:
            Memory used in MB, or 0.0 if unavailable.
        """
        try:
            with open('/proc/self/status', 'r') as f:
                for line in f:
                    if line.startswith('VmRSS:'):
                        # VmRSS is in kB
                        kb = int(line.split()[1])
                        return round(kb / 1024.0, 1)
            return 0.0
        except Exception:
            return 0.0

    def _get_source_fps(self):
        # type: () -> float
        """Get the RTSP source stream frame rate.

        Returns:
            Source FPS as reported by the capture module, or 0.0.
        """
        try:
            if self.processor is not None and hasattr(self.processor, 'capture'):
                capture = self.processor.capture
                if hasattr(capture, '_source_fps'):
                    return round(float(capture._source_fps), 1)
        except (TypeError, ValueError):
            pass
        return 0.0

    def _get_fps_percent(self):
        # type: () -> float
        """Get current FPS as a percentage of target FPS.

        Returns:
            FPS percentage (0-100+), or 0.0 if stopped or unavailable.
        """
        try:
            if self.processor is not None:
                # Return 0 if detection is stopped
                if hasattr(self.processor, 'is_running') and not self.processor.is_running:
                    return 0.0
                if hasattr(self.processor, '_fps_calculator'):
                    current = self.processor._fps_calculator.get_fps()
                    target = self.processor.target_fps
                    if target > 0:
                        return round((current / target) * 100.0, 1)
        except (TypeError, ValueError, ZeroDivisionError):
            pass
        return 0.0

    def _make_handler_class(self):
        # type: () -> type
        """Create a request handler class with access to this WebServer instance.

        Returns:
            A BaseHTTPRequestHandler subclass bound to this server.
        """
        server_ref = self

        class EdgeAIRequestHandler(BaseHTTPRequestHandler):
            """HTTP request handler for MJPEG streaming and API."""

            def log_message(self, format, *args):
                """Suppress default HTTP logging to avoid noise."""
                pass

            def do_GET(self):
                """Handle GET requests for stream, stats, session, static files, and pages."""
                if self.path == "/stream" or self.path == "/stream.mjpeg":
                    self._handle_stream()
                elif self.path == "/stats":
                    self._handle_stats()
                elif self.path == "/config":
                    self._handle_config_get()
                elif self.path == "/session":
                    self._handle_session()
                elif self.path == "/help":
                    self._handle_help()
                elif self.path.startswith("/static/"):
                    self._handle_static()
                elif self.path == "/":
                    self._handle_index()
                else:
                    self.send_error(404)

            def do_POST(self):
                """Handle POST requests for configuration updates, heartbeat, and control."""
                if self.path == "/config":
                    self._handle_config_post()
                elif self.path == "/heartbeat":
                    self._handle_heartbeat()
                elif self.path == "/control":
                    self._handle_control()
                else:
                    self.send_error(404)

            def _handle_config_post(self):
                """Handle configuration update POST request."""
                import json

                content_length = int(self.headers.get("Content-Length", 0))
                if content_length == 0:
                    self._send_json_response(400, {"error": "Empty request body"})
                    return

                body = self.rfile.read(content_length)
                try:
                    data = json.loads(body.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    self._send_json_response(400, {"error": "Invalid JSON"})
                    return

                # Session control: if session_id is provided and non-empty, verify it is primary.
                # If no session_id is provided, allow for backward compatibility.
                session_id = data.get("session_id", "")
                if session_id:
                    if not server_ref.is_primary_session(session_id):
                        self._send_json_response(403, {
                            "error": "Controls locked - another user has control"
                        })
                        return

                errors = {}
                applied = {}

                # Validate and apply each field present in the request
                if "confidence_threshold" in data:
                    val = str(data["confidence_threshold"])
                    valid, err = server_ref.validate_config_input(
                        "confidence_threshold", val)
                    if valid:
                        threshold = float(val)
                        if hasattr(server_ref.processor, 'engine'):
                            server_ref.processor.engine.set_threshold(threshold)
                        applied["confidence_threshold"] = threshold
                    else:
                        errors["confidence_threshold"] = err

                if "target_fps" in data:
                    val = str(data["target_fps"])
                    valid, err = server_ref.validate_config_input(
                        "target_fps", val)
                    if valid:
                        fps = int(val)
                        if hasattr(server_ref.processor, '_target_fps'):
                            server_ref.processor._target_fps = fps
                        if hasattr(server_ref.processor, '_running_fps'):
                            server_ref.processor._running_fps = fps
                        applied["target_fps"] = fps
                    else:
                        errors["target_fps"] = err

                if "input_resolution" in data:
                    val = str(data["input_resolution"])
                    valid, err = server_ref.validate_config_input(
                        "input_resolution", val)
                    if valid:
                        applied["input_resolution"] = val
                    else:
                        errors["input_resolution"] = err

                if "rtsp_url" in data:
                    val = str(data["rtsp_url"])
                    valid, err = server_ref.validate_config_input(
                        "rtsp_url", val)
                    if valid:
                        # Attempt reconnection with new URL
                        if hasattr(server_ref.processor, 'capture'):
                            capture = server_ref.processor.capture
                            if hasattr(capture, 'url'):
                                old_url = capture.url
                                capture.url = val
                                # Try to connect with 10s timeout
                                connected = False
                                if hasattr(capture, 'connect'):
                                    connected = capture.connect(timeout=10.0)
                                if not connected:
                                    capture.url = old_url
                                    errors["rtsp_url"] = (
                                        "Failed to connect to new RTSP URL "
                                        "within 10 seconds")
                                else:
                                    applied["rtsp_url"] = val
                            else:
                                applied["rtsp_url"] = val
                        else:
                            applied["rtsp_url"] = val
                    else:
                        errors["rtsp_url"] = err

                if "show_bboxes" in data:
                    if hasattr(server_ref.processor, 'show_bboxes'):
                        server_ref.processor.show_bboxes = bool(data["show_bboxes"])
                        applied["show_bboxes"] = bool(data["show_bboxes"])
                if "show_labels" in data:
                    if hasattr(server_ref.processor, 'show_labels'):
                        server_ref.processor.show_labels = bool(data["show_labels"])
                        applied["show_labels"] = bool(data["show_labels"])
                if "show_fps" in data:
                    if hasattr(server_ref.processor, 'show_fps'):
                        server_ref.processor.show_fps = bool(data["show_fps"])
                        applied["show_fps"] = bool(data["show_fps"])

                if "show_detcount" in data:
                    if hasattr(server_ref.processor, 'show_detcount'):
                        server_ref.processor.show_detcount = bool(data["show_detcount"])
                        applied["show_detcount"] = bool(data["show_detcount"])

                if "skip_inference_frames" in data:
                    try:
                        skip_val = int(data["skip_inference_frames"])
                        if 0 <= skip_val <= 10:
                            if hasattr(server_ref.processor, 'skip_inference_frames'):
                                server_ref.processor.skip_inference_frames = skip_val
                            applied["skip_inference_frames"] = skip_val
                        else:
                            errors["skip_inference_frames"] = "Must be between 0 and 10"
                    except (ValueError, TypeError):
                        errors["skip_inference_frames"] = "Must be an integer"

                if "jpeg_quality" in data:
                    try:
                        jpeg_val = int(data["jpeg_quality"])
                        if 1 <= jpeg_val <= 100:
                            server_ref.jpeg_quality = jpeg_val
                            server_ref._stream_handler.quality = jpeg_val
                            applied["jpeg_quality"] = jpeg_val
                        else:
                            errors["jpeg_quality"] = "Must be between 1 and 100"
                    except (ValueError, TypeError):
                        errors["jpeg_quality"] = "Must be an integer"

                if "model_name" in data:
                    val = str(data["model_name"])
                    valid_models = ['ssd_mobilenet_v2', 'yolov5n']
                    if val in valid_models:
                        # Reload model if changed
                        model_paths = {
                            'ssd_mobilenet_v2': '/app/models/ssd_mobilenet_v2.tflite',
                            'yolov5n': '/app/models/yolov5n_int8.tflite',
                        }
                        if hasattr(server_ref.processor, 'engine'):
                            engine = server_ref.processor.engine
                            if engine.model_path != model_paths[val]:
                                engine.model_path = model_paths[val]
                                engine.load_model()
                                cp.log("Model switched to: {}".format(val))
                        applied["model_name"] = val
                    else:
                        errors["model_name"] = "Must be 'ssd_mobilenet_v2' or 'yolov5n'"

                if errors:
                    self._send_json_response(400, {
                        "errors": errors,
                        "applied": applied
                    })
                else:
                    # Persist applied config changes to appdata
                    _appdata_fields = {
                        'confidence_threshold', 'target_fps',
                        'rtsp_url', 'rtsp_input_url', 'model_name'
                    }
                    for key, value in applied.items():
                        appdata_key = key
                        if key == 'rtsp_url':
                            appdata_key = 'rtsp_input_url'
                        if appdata_key in ('confidence_threshold', 'target_fps',
                                           'rtsp_input_url', 'web_port',
                                           'skip_inference_frames', 'model_name',
                                           'jpeg_quality'):
                            try:
                                cp.put_appdata(appdata_key, str(value))
                            except Exception:
                                pass
                    self._send_json_response(200, {
                        "success": True,
                        "applied": applied
                    })

            def _send_json_response(self, status_code, data):
                """Send a JSON response with the given status code."""
                import json
                body = json.dumps(data).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle_session(self):
                """Handle GET /session to register a new session."""
                import json
                session_id, is_primary = server_ref.create_session()
                self._send_json_response(200, {
                    "session_id": session_id,
                    "is_primary": is_primary
                })

            def _handle_heartbeat(self):
                """Handle POST /heartbeat to keep session alive."""
                import json

                content_length = int(self.headers.get("Content-Length", 0))
                if content_length == 0:
                    self._send_json_response(400, {"error": "Empty request body"})
                    return

                body = self.rfile.read(content_length)
                try:
                    data = json.loads(body.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    self._send_json_response(400, {"error": "Invalid JSON"})
                    return

                session_id = data.get("session_id", "")
                if not session_id:
                    self._send_json_response(400, {
                        "error": "session_id is required"
                    })
                    return

                is_primary = server_ref.heartbeat(session_id)
                self._send_json_response(200, {"is_primary": is_primary})

            def _handle_control(self):
                """Handle POST /control to start/stop detection."""
                import json

                content_length = int(self.headers.get("Content-Length", 0))
                if content_length == 0:
                    self._send_json_response(400, {"error": "Empty request body"})
                    return

                body = self.rfile.read(content_length)
                try:
                    data = json.loads(body.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    self._send_json_response(400, {"error": "Invalid JSON"})
                    return

                # Session control: verify primary user
                session_id = data.get("session_id", "")
                if session_id:
                    if not server_ref.is_primary_session(session_id):
                        self._send_json_response(403, {
                            "error": "Controls locked - another user has control"
                        })
                        return

                action = data.get("action", "")
                if action not in ("start", "stop"):
                    self._send_json_response(400, {
                        "error": "action must be 'start' or 'stop'"
                    })
                    return

                if server_ref.processor is None:
                    self._send_json_response(400, {
                        "error": "No processor available"
                    })
                    return

                if action == "start":
                    # Connect capture if not connected
                    if hasattr(server_ref.processor, 'capture'):
                        capture = server_ref.processor.capture
                        if hasattr(capture, 'is_connected') and not capture.is_connected:
                            if hasattr(capture, 'connect'):
                                cp.log("Starting detection: connecting to {}".format(
                                    capture.url))
                                connected = capture.connect()
                                if connected:
                                    cp.log("RTSP connection established")
                                else:
                                    cp.log("WARNING: RTSP connection failed, will retry in process loop")
                    server_ref.processor.start_detection()
                    self._send_json_response(200, {"status": "running"})
                else:
                    server_ref.processor.stop_detection()
                    # Release capture connection
                    if hasattr(server_ref.processor, 'capture'):
                        capture = server_ref.processor.capture
                        if hasattr(capture, 'release'):
                            capture.release()
                    cp.log("Detection stopped by user")
                    self._send_json_response(200, {"status": "stopped"})

            def _handle_stream(self):
                """Serve MJPEG stream to client with frame-drop protection.

                If encoding/sending takes longer than the frame interval,
                the latest frame is used (dropping intermediate frames)
                rather than queuing stale frames that cause artifacting.
                """
                # Check connection limit
                if not server_ref._increment_clients():
                    self.send_response(503)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(b"Maximum client connections exceeded")
                    return

                try:
                    self.send_response(200)
                    self.send_header(
                        "Content-Type",
                        "multipart/x-mixed-replace; boundary=" + MJPEG_BOUNDARY
                    )
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "keep-alive")
                    self.end_headers()

                    while True:
                        frame = server_ref.get_frame()
                        if frame is None:
                            time.sleep(0.1)
                            continue

                        # Encode with configured quality
                        chunk = server_ref._stream_handler.stream_frame(
                            frame, quality=server_ref.jpeg_quality)
                        if not chunk:
                            time.sleep(0.05)
                            continue

                        try:
                            self.wfile.write(chunk)
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError, OSError):
                            break

                        # Pace the stream — use actual elapsed time to avoid drift
                        target_fps = 10
                        if hasattr(server_ref.processor, '_running_fps'):
                            target_fps = server_ref.processor._running_fps
                        if target_fps > 0:
                            time.sleep(1.0 / target_fps)

                finally:
                    server_ref._decrement_clients()

            def _handle_stats(self):
                """Serve JSON stats endpoint."""
                import json
                stats = server_ref.get_stats()
                body = json.dumps(stats).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(body)

            def _handle_config_get(self):
                """Serve current configuration values as JSON."""
                import json
                config = {}
                if server_ref.processor is not None:
                    if hasattr(server_ref.processor, 'engine'):
                        config['confidence_threshold'] = server_ref.processor.engine.confidence_threshold
                    if hasattr(server_ref.processor, 'target_fps'):
                        config['target_fps'] = server_ref.processor.target_fps
                    if hasattr(server_ref.processor, 'skip_inference_frames'):
                        config['skip_inference_frames'] = server_ref.processor.skip_inference_frames
                    if hasattr(server_ref.processor, 'capture') and hasattr(server_ref.processor.capture, 'url'):
                        config['rtsp_url'] = server_ref.processor.capture.url
                    if hasattr(server_ref.processor, 'show_bboxes'):
                        config['show_bboxes'] = server_ref.processor.show_bboxes
                    if hasattr(server_ref.processor, 'show_labels'):
                        config['show_labels'] = server_ref.processor.show_labels
                    if hasattr(server_ref.processor, 'show_fps'):
                        config['show_fps'] = server_ref.processor.show_fps
                    if hasattr(server_ref.processor, 'show_detcount'):
                        config['show_detcount'] = server_ref.processor.show_detcount
                config['jpeg_quality'] = server_ref.jpeg_quality
                body = json.dumps(config).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(body)

            def _handle_index(self):
                """Serve the full Edge AI web interface page."""
                body = _build_index_html().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle_static(self):
                """Serve static files (CSS, JS, images) from the static directory."""
                # Strip /static/ prefix and resolve the file path
                rel_path = self.path[len("/static/"):]
                # Prevent directory traversal
                rel_path = rel_path.replace("..", "")
                file_path = os.path.join(_STATIC_DIR, rel_path)

                if not os.path.isfile(file_path):
                    self.send_error(404)
                    return

                # Determine MIME type from extension
                _, ext = os.path.splitext(file_path)
                content_type = _MIME_TYPES.get(ext.lower(), "application/octet-stream")

                try:
                    mode = "rb"
                    with open(file_path, mode) as f:
                        body = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                    self.end_headers()
                    self.wfile.write(body)
                except (IOError, OSError, BrokenPipeError, ConnectionResetError):
                    pass

            def _handle_help(self):
                """Serve help.md content as plain text for the help modal."""
                help_path = os.path.join(os.path.dirname(os.path.dirname(_SRC_DIR)), 'help.md')
                if not os.path.exists(help_path):
                    help_path = '/app/help.md'
                if not os.path.exists(help_path):
                    # Fallback to readme
                    help_path = '/app/readme.md'
                try:
                    with open(help_path, 'r') as f:
                        content = f.read()
                except (IOError, OSError):
                    content = "Help documentation not available."
                body = content.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return EdgeAIRequestHandler

    def start(self):
        # type: () -> None
        """Start the HTTP server in a background thread."""
        handler_class = self._make_handler_class()
        try:
            self._server = ThreadedHTTPServer(("0.0.0.0", self.port), handler_class)
            self._server.timeout = 1.0
            cp.log("Web server started on port {}".format(self.port))
        except OSError as e:
            cp.log("ERROR: Failed to start web server on port {}: {}".format(
                self.port, e))
            return

        thread = threading.Thread(target=self._serve_forever, name="WebServerThread")
        thread.daemon = True
        thread.start()

    def _serve_forever(self):
        # type: () -> None
        """Serve HTTP requests until stopped."""
        if self._server is not None:
            self._server.serve_forever()

    def stop(self):
        # type: () -> None
        """Stop the HTTP server."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            cp.log("Web server stopped")
