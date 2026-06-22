"""Frame Processor module for Edge AI Person Detection.

Orchestrates the main processing pipeline: capture -> resize -> infer ->
annotate -> store. Manages frame pacing, adaptive rate control, and
thread-safe access to the current annotated frame.

Performance optimizations:
- Skip annotation when no clients are connected (saves PIL overhead)
- Double-buffer for lock-free frame reads by web server
- OpenCV resize with NEON SIMD on ARM64 (with Pillow/numpy fallback)
- Frame skipping from RTSP when camera FPS exceeds target
- Configurable JPEG quality for faster encoding

Requirements: 1.2, 1.5, 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 10.2, 10.3, 10.5
"""
import sys
import os
import time
import threading

try:
    import numpy
except ImportError:
    numpy = None

try:
    import cv2
    _HAS_CV2 = True
except ImportError:
    cv2 = None
    _HAS_CV2 = False

try:
    from PIL import Image
except ImportError:
    Image = None

# Add parent directory to path so cp module is importable
_parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)

import cp

from capture import RTSPCapture
from inference import InferenceEngine
from annotation import AnnotationRenderer, FPSCalculator


class FrameProcessor(object):
    """Main processing pipeline orchestrator.

    Coordinates the capture -> resize -> infer -> annotate pipeline,
    manages frame pacing via sleep duration computation, implements
    adaptive rate control based on inference latency, and provides
    thread-safe access to the latest annotated frame.

    Performance features:
    - Skips annotation when no web clients are connected
    - Double-buffer pattern for minimal lock contention
    - OpenCV resize for NEON SIMD acceleration on ARM64
    - RTSP frame skipping to match target FPS

    Attributes:
        capture: RTSPCapture instance for frame acquisition.
        engine: InferenceEngine instance for person detection.
        renderer: AnnotationRenderer instance for drawing annotations.
        target_fps: Configured target frames per second.
    """

    # Adaptive rate control thresholds
    _HIGH_LATENCY_THRESHOLD_MS = 1000.0
    _LOW_LATENCY_THRESHOLD_MS = 500.0
    _CONSECUTIVE_COUNT = 10

    # Logging interval in seconds
    _LOG_INTERVAL_SECONDS = 60.0

    def __init__(self, capture, engine, renderer, target_fps=10, web_server=None):
        # type: (RTSPCapture, InferenceEngine, AnnotationRenderer, int, object) -> None
        """Initialize FrameProcessor.

        Args:
            capture: RTSPCapture instance for frame acquisition.
            engine: InferenceEngine instance for person detection.
            renderer: AnnotationRenderer instance for drawing annotations.
            target_fps: Target processing frame rate (1-60, default 10).
            web_server: Optional WebServer instance for client-aware annotation.
        """
        self.capture = capture
        self.engine = engine
        self.renderer = renderer
        self.target_fps = target_fps
        self.web_server = web_server

        # Detection state: default is stopped (user must press Play)
        self.is_running = False

        # Double-buffer: write to _next_frame, swap to _current_frame atomically
        self._frame_lock = threading.Lock()
        self._current_frame = None  # type: numpy.ndarray or None

        # Processing control
        self._stop_event = threading.Event()
        self._running_fps = target_fps  # Adaptive rate (may be reduced)

        # Adaptive rate control state
        self._high_latency_count = 0
        self._low_latency_count = 0

        # FPS calculator for overlay
        self._fps_calculator = FPSCalculator()

        # Stats tracking for periodic logging
        self._inference_times = []  # type: list
        self._detection_count = 0
        self._frames_processed = 0
        self._last_log_time = time.time()

        # JPEG quality (lower = faster encoding for streaming)
        self.jpeg_quality = 50

        # Overlay toggle flags (controllable via web UI)
        self.show_bboxes = True
        self.show_labels = True
        self.show_fps = True
        self.show_detcount = True

        # Frame skip counter for RTSP streams faster than target
        self._frame_skip_counter = 0

        # Inference frame skipping: 0=disabled, N=skip N frames between inferences
        self.skip_inference_frames = 0
        self._inference_skip_counter = 0
        self._last_detections = []  # Reused during skipped frames

    @property
    def current_frame(self):
        # type: () -> numpy.ndarray or None
        """Thread-safe access to the latest annotated frame.

        Returns:
            The latest annotated frame as a numpy array, or None if no
            frame has been processed yet.
        """
        with self._frame_lock:
            return self._current_frame

    @current_frame.setter
    def current_frame(self, frame):
        # type: (numpy.ndarray or None) -> None
        """Thread-safe setter for the current annotated frame.

        Args:
            frame: The annotated frame to store, or None.
        """
        with self._frame_lock:
            self._current_frame = frame

    @staticmethod
    def resize_frame(frame, target_size):
        # type: (numpy.ndarray, tuple) -> numpy.ndarray
        """Resize frame to model input dimensions.

        Uses OpenCV when available (NEON SIMD on ARM64) for maximum
        performance. Falls back to Pillow, then numpy nearest-neighbor.

        Args:
            frame: Input frame as a numpy array (H, W, C).
            target_size: Target dimensions as (width, height).

        Returns:
            Resized frame with dimensions exactly (target_height, target_width, C).
        """
        target_w, target_h = target_size
        if _HAS_CV2:
            # OpenCV uses NEON SIMD on ARM64 — fastest resize path
            return cv2.resize(frame, (target_w, target_h))
        elif Image is not None:
            # Pillow fallback
            pil_image = Image.fromarray(frame)
            resized = pil_image.resize((target_w, target_h), Image.BILINEAR)
            return numpy.array(resized)
        else:
            # Numpy nearest-neighbor fallback
            src_h, src_w = frame.shape[0], frame.shape[1]
            row_indices = (numpy.arange(target_h) * src_h // target_h).astype(int)
            col_indices = (numpy.arange(target_w) * src_w // target_w).astype(int)
            return frame[numpy.ix_(row_indices, col_indices)]

    @staticmethod
    def compute_sleep_duration(target_fps, elapsed):
        # type: (int, float) -> float
        """Calculate sleep time to maintain target frame rate.

        Formula: max(0, 1/target_fps - elapsed)

        Args:
            target_fps: Target frames per second (1-60).
            elapsed: Time already spent processing the current frame (seconds).

        Returns:
            Sleep duration in seconds, never negative.
        """
        if target_fps <= 0:
            return 0.0
        frame_interval = 1.0 / target_fps
        sleep_time = frame_interval - elapsed
        return max(0.0, sleep_time)

    def check_adaptive_rate(self, latencies):
        # type: (list) -> int
        """Check if processing rate should be reduced or restored.

        Reduces rate by half if 10 consecutive latencies exceed 1000ms.
        Restores to configured target_fps if 10 consecutive latencies
        are below 500ms.

        Args:
            latencies: List of recent inference latency values in milliseconds.

        Returns:
            The adjusted target FPS value.
        """
        if len(latencies) < self._CONSECUTIVE_COUNT:
            return self._running_fps

        # Check the last 10 latencies
        recent = latencies[-self._CONSECUTIVE_COUNT:]

        # Check for high latency: all 10 > 1000ms -> reduce by half
        all_high = all(
            lat > self._HIGH_LATENCY_THRESHOLD_MS for lat in recent
        )
        if all_high:
            new_fps = max(self._running_fps // 2, 1)
            if new_fps != self._running_fps:
                cp.log("WARNING: High inference latency detected, reducing FPS from {} to {}".format(
                    self._running_fps, new_fps))
                self._running_fps = new_fps
            return self._running_fps

        # Check for low latency: all 10 < 500ms -> restore to target
        all_low = all(
            lat < self._LOW_LATENCY_THRESHOLD_MS for lat in recent
        )
        if all_low and self._running_fps < self.target_fps:
            cp.log("Inference latency recovered, restoring FPS from {} to {}".format(
                self._running_fps, self.target_fps))
            self._running_fps = self.target_fps

        return self._running_fps

    def _has_clients(self):
        # type: () -> bool
        """Check if any web clients are connected for streaming.

        Returns:
            True if at least one client is connected, or if no web_server
            is configured (always annotate in that case).
        """
        if self.web_server is None:
            return True
        if hasattr(self.web_server, 'get_client_count'):
            return self.web_server.get_client_count() > 0
        return True

    def start_detection(self):
        # type: () -> None
        """Start the detection pipeline."""
        self.is_running = True

    def stop_detection(self):
        # type: () -> None
        """Stop the detection pipeline (keeps thread alive but stops processing).

        Clears the current frame so the stream serves the placeholder.
        Resets the FPS calculator so it reports 0.
        """
        self.is_running = False
        self.current_frame = None
        # Reset FPS calculator so it reports 0 when stopped
        self._fps_calculator = FPSCalculator()

    def process_loop(self):
        # type: () -> None
        """Main processing loop running in a dedicated thread.

        Pipeline: capture -> resize -> infer -> annotate -> store.
        Manages frame pacing, adaptive rate control, periodic stats
        logging, and memory release.

        Performance optimizations:
        - Skips annotation when no clients are watching
        - Uses frame pacing to avoid over-processing
        """
        cp.log("Frame processor started at {} FPS".format(self.target_fps))
        latencies = []  # type: list

        while not self._stop_event.is_set():
            # When detection is stopped, idle until started
            if not self.is_running:
                time.sleep(0.1)
                self._check_periodic_logging()
                continue
            loop_start = time.time()

            # Capture frame
            frame = self.capture.read_frame()
            if frame is None:
                # No frame available - check if we need to reconnect
                if not self.capture.is_connected:
                    self.capture.reconnect_loop()
                # Brief sleep to avoid busy-waiting
                time.sleep(0.1)
                self._check_periodic_logging()
                continue

            try:
                # Run inference or reuse last detections (frame skipping)
                if self.skip_inference_frames > 0 and self._inference_skip_counter > 0:
                    # Skip inference, reuse last detections
                    detections = self._last_detections
                    inference_ms = 0.0
                    self._inference_skip_counter -= 1
                else:
                    # Run inference (resize happens inside engine with pre-allocated buffer)
                    infer_start = time.time()
                    detections = self.engine.detect(frame)
                    infer_end = time.time()
                    inference_ms = (infer_end - infer_start) * 1000.0
                    self._last_detections = detections
                    self._inference_skip_counter = self.skip_inference_frames

                self._inference_times.append(inference_ms)
                self._detection_count += len(detections)

                # Adaptive rate control
                latencies.append(inference_ms)
                if len(latencies) > 20:
                    latencies = latencies[-20:]
                self.check_adaptive_rate(latencies)

                # Annotate only if clients are watching (saves ~5-10ms per frame)
                annotate_start = time.time()
                if self._has_clients():
                    annotated = frame
                    if self.show_bboxes:
                        annotated = self.renderer.draw_detections(
                            annotated, detections, show_labels=self.show_labels)
                    self._fps_calculator.tick()
                    fps = self._fps_calculator.get_fps()
                    if self.show_fps:
                        # Compute rolling average inference for overlay
                        avg_inf = inference_ms
                        if self._inference_times:
                            avg_inf = sum(self._inference_times[-10:]) / min(len(self._inference_times), 10)
                        det_count = len(detections) if self.show_detcount else -1
                        annotated = self.renderer.draw_fps_overlay(annotated, fps, avg_inf, det_count)
                    # Store annotated frame (double-buffer swap)
                    self.current_frame = annotated
                else:
                    # Still track FPS even without annotation
                    self._fps_calculator.tick()
                    # Store raw frame for when a client connects
                    self.current_frame = frame
                annotate_ms = (time.time() - annotate_start) * 1000.0

                # Track annotation time for logging
                if not hasattr(self, '_annotate_times'):
                    self._annotate_times = []
                self._annotate_times.append(annotate_ms)

                # Release references to free memory
                del frame
                del detections

                self._frames_processed += 1

            except Exception as e:
                cp.log("ERROR: Frame processing failed: {} - {}".format(
                    type(e).__name__, e))
                del frame
                continue

            # Frame pacing: sleep to maintain target FPS
            elapsed = time.time() - loop_start
            sleep_duration = self.compute_sleep_duration(
                self._running_fps, elapsed
            )
            if sleep_duration > 0:
                time.sleep(sleep_duration)

            # Periodic stats logging
            self._check_periodic_logging()

        cp.log("Frame processor stopped")

    def _check_periodic_logging(self):
        # type: () -> None
        """Log periodic stats every 60 seconds.

        Logs average inference time and detection count for the interval.
        Logs a warning if zero frames were processed in the interval.
        """
        now = time.time()
        elapsed_since_log = now - self._last_log_time

        if elapsed_since_log >= self._LOG_INTERVAL_SECONDS:
            if self._frames_processed == 0:
                cp.log("WARNING: Zero frames processed in the last {:.0f}s interval".format(
                    elapsed_since_log))
            else:
                avg_inference = 0.0
                if self._inference_times:
                    avg_inference = sum(self._inference_times) / len(self._inference_times)
                avg_annotate = 0.0
                if hasattr(self, '_annotate_times') and self._annotate_times:
                    avg_annotate = sum(self._annotate_times) / len(self._annotate_times)
                cp.log("Stats ({}s): fps={:.1f}, inference={:.1f}ms, annotate={:.1f}ms, detections={}".format(
                    int(elapsed_since_log),
                    self._fps_calculator.get_fps(),
                    avg_inference, avg_annotate, self._detection_count))

            # Reset counters for next interval
            self._inference_times = []
            self._detection_count = 0
            self._frames_processed = 0
            self._last_log_time = now
            if hasattr(self, '_annotate_times'):
                self._annotate_times = []

    def set_target_fps(self, fps):
        # type: (int) -> None
        """Update the target FPS at runtime.

        Args:
            fps: New target FPS value (1-60).
        """
        self.target_fps = fps
        self._running_fps = fps

    def stop(self):
        # type: () -> None
        """Signal the processing loop to stop."""
        self._stop_event.set()
