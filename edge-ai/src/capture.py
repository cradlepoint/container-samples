"""RTSP Video Capture module for Edge AI Person Detection.

Manages RTSP connection, frame decoding, disconnection detection,
and automatic reconnection with exponential backoff.

Uses PyAV (ffmpeg wrapper) for RTSP streaming instead of OpenCV
for a smaller container footprint.

Requirements: 1.1, 1.2, 1.3, 1.4, 1.6, 1.7
"""
import sys
import os
import time
import threading

try:
    import av
except ImportError:
    av = None

try:
    import numpy
except ImportError:
    numpy = None

# Add parent directory to path so cp module is importable
_parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)

import cp


class RTSPCapture(object):
    """Manages RTSP connection and frame capture.

    Thread-safe class that handles RTSP stream connection, frame reading,
    disconnection detection, and automatic reconnection with exponential
    backoff. Designed to run in a dedicated capture thread.

    Attributes:
        url: The RTSP stream URL.
        target_fps: Target frames per second for capture rate.
    """

    # Connection states
    STATE_DISCONNECTED = "disconnected"
    STATE_CONNECTED = "connected"
    STATE_RECONNECTING = "reconnecting"

    # Time window for disconnection detection (seconds)
    _DISCONNECT_TIMEOUT = 10.0

    def __init__(self, url, target_fps=10):
        # type: (str, int) -> None
        """Initialize RTSPCapture.

        Args:
            url: RTSP stream URL (must start with "rtsp://").
            target_fps: Target capture frame rate (1-60).
        """
        self.url = url
        self.target_fps = target_fps

        self._lock = threading.Lock()
        self._state = self.STATE_DISCONNECTED
        self._container = None
        self._stream = None
        self._stop_event = threading.Event()

        # Disconnection detection tracking
        self._first_failure_time = None  # type: float or None
        self._consecutive_failures = 0

        # Frame skipping: track source FPS and skip ratio
        self._source_fps = 30  # Assume 30 FPS source until known
        self._frame_counter = 0
        self._skip_ratio = 1  # Process every Nth frame

    @property
    def is_connected(self):
        # type: () -> bool
        """Return True if currently connected to the RTSP stream."""
        with self._lock:
            return self._state == self.STATE_CONNECTED

    @property
    def connection_state(self):
        # type: () -> str
        """Return current connection state string."""
        with self._lock:
            return self._state

    def _set_state(self, new_state):
        # type: (str) -> None
        """Update connection state (thread-safe) and log transitions."""
        with self._lock:
            old_state = self._state
            self._state = new_state
        if old_state != new_state:
            cp.log("RTSP connection state: {} -> {}".format(old_state, new_state))

    def connect(self, timeout=30.0):
        # type: (float) -> bool
        """Attempt RTSP connection within timeout.

        Uses a background thread to open the container since
        av.open() can block indefinitely on unreachable streams.

        Args:
            timeout: Maximum seconds to wait for connection (default 30).

        Returns:
            True if connection succeeded, False otherwise.
        """
        if av is None:
            cp.log("ERROR: PyAV (av) not available")
            return False

        if not self.url:
            cp.log("ERROR: RTSP URL is missing or empty")
            return False

        result = [None]  # Use list to allow mutation from thread

        def _open_container():
            try:
                options = {
                    'rtsp_transport': 'tcp',
                    'stimeout': str(int(timeout * 1000000)),  # microseconds
                    # Reduce buffer to minimize latency and prevent stale frames
                    'buffer_size': '524288',
                    # Flush packets immediately to avoid decode lag
                    'fflags': 'nobuffer',
                    # Reduce analysis duration for faster connection
                    'analyzeduration': '1000000',
                    'probesize': '1000000',
                }
                container = av.open(self.url, options=options, timeout=timeout)
                # Get the video stream
                streams = [s for s in container.streams if s.type == 'video']
                if streams:
                    result[0] = (container, streams[0])
                else:
                    container.close()
                    cp.log("ERROR: No video stream found in RTSP source")
            except Exception as e:
                cp.log("ERROR: Failed to open RTSP stream: {}".format(e))

        connect_thread = threading.Thread(target=_open_container)
        connect_thread.daemon = True
        connect_thread.start()
        connect_thread.join(timeout=timeout)

        if connect_thread.is_alive():
            # Connection timed out
            cp.log("ERROR: RTSP connection timed out after {} seconds".format(timeout))
            return False

        if result[0] is not None:
            container, stream = result[0]
            with self._lock:
                # Release any existing container
                if self._container is not None:
                    try:
                        self._container.close()
                    except Exception:
                        pass
                self._container = container
                self._stream = stream

            # Detect source FPS and compute skip ratio
            try:
                src_fps = float(stream.average_rate)
                if src_fps > 0:
                    self._source_fps = src_fps
                    # Skip frames if source is faster than target
                    self._skip_ratio = max(1, int(round(src_fps / self.target_fps)))
                    if self._skip_ratio > 1:
                        cp.log("Source FPS={:.0f}, target={}, skipping every {} frames".format(
                            src_fps, self.target_fps, self._skip_ratio))
            except (TypeError, ValueError, ZeroDivisionError):
                self._skip_ratio = 1

            self._frame_counter = 0
            self._reset_failure_tracking()
            self._set_state(self.STATE_CONNECTED)
            return True
        else:
            cp.log("ERROR: Failed to connect to RTSP stream: {}".format(self.url))
            return False

    def read_frame(self):
        # type: () -> numpy.ndarray or None
        """Read and decode the next frame from the RTSP stream.

        Implements frame skipping: if the source FPS is higher than
        target_fps, only every Nth frame is decoded and returned.
        Intermediate frames are discarded at the packet level to avoid
        unnecessary decode overhead.

        Returns:
            The decoded frame as a numpy array (BGR, HWC), or None if
            reading failed (disconnected, corrupt frame, etc.).
        """
        with self._lock:
            container = self._container
            stream = self._stream

        if container is None or stream is None:
            return None

        try:
            # Decode frames, skipping based on skip_ratio
            for packet in container.demux(stream):
                for frame in packet.decode():
                    self._frame_counter += 1

                    # Skip frames to match target FPS
                    if self._skip_ratio > 1 and (self._frame_counter % self._skip_ratio) != 0:
                        continue

                    # Convert to numpy array in BGR format
                    img = frame.to_ndarray(format='bgr24')

                    # Validate frame is not corrupt
                    if img.size == 0 or len(img.shape) < 2:
                        cp.log("WARNING: Corrupt frame discarded (invalid dimensions)")
                        self._record_failure()
                        return None

                    # Successful read - reset failure tracking
                    self._reset_failure_tracking()
                    return img

            # No frames decoded from packet
            self._record_failure()
            return None

        except Exception as e:
            if 'EOF' in str(type(e).__name__):
                pass  # Normal end-of-stream
            else:
                cp.log("WARNING: Exception reading frame: {}".format(e))
            self._record_failure()
            return None

    def _record_failure(self):
        # type: () -> None
        """Record a frame read failure for disconnection detection.

        If failures persist for more than _DISCONNECT_TIMEOUT seconds,
        the stream is considered disconnected.
        """
        now = time.time()
        if self._first_failure_time is None:
            self._first_failure_time = now
        self._consecutive_failures += 1

        elapsed = now - self._first_failure_time
        if elapsed >= self._DISCONNECT_TIMEOUT:
            self._set_state(self.STATE_DISCONNECTED)

    def _reset_failure_tracking(self):
        # type: () -> None
        """Reset failure tracking counters after a successful read."""
        self._first_failure_time = None
        self._consecutive_failures = 0

    @staticmethod
    def compute_backoff(retry_count):
        # type: (int) -> float
        """Compute exponential backoff delay.

        Formula: min(2^(retry_count + 1), 60)
        Produces values: 2, 4, 8, 16, 32, 60, 60, ...

        Args:
            retry_count: Number of retries attempted (0-based).

        Returns:
            Backoff delay in seconds, always in range [2, 60].
        """
        delay = 2 ** (retry_count + 1)
        return min(delay, 60)

    def reconnect_loop(self):
        # type: () -> None
        """Reconnection loop with exponential backoff.

        Attempts to reconnect to the RTSP stream with exponential backoff
        starting at 2 seconds and capping at 60 seconds. Continues until
        connection succeeds or stop() is called.
        """
        self._set_state(self.STATE_RECONNECTING)
        retry_count = 0

        while not self._stop_event.is_set():
            delay = self.compute_backoff(retry_count)
            cp.log("RTSP reconnecting in {} seconds (attempt {})".format(
                delay, retry_count + 1))

            # Wait for the backoff delay, checking stop event periodically
            wait_end = time.time() + delay
            while time.time() < wait_end:
                if self._stop_event.is_set():
                    return
                time.sleep(0.5)

            # Attempt reconnection
            if self.connect():
                cp.log("RTSP reconnection successful after {} attempts".format(
                    retry_count + 1))
                return

            retry_count += 1

    def release(self):
        # type: () -> None
        """Release the video capture resource."""
        with self._lock:
            if self._container is not None:
                try:
                    self._container.close()
                except Exception:
                    pass
                self._container = None
                self._stream = None
        self._set_state(self.STATE_DISCONNECTED)

    def stop(self):
        # type: () -> None
        """Signal the capture to stop (used to break reconnect_loop)."""
        self._stop_event.set()
        self.release()
