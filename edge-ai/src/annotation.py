"""Annotation module for Edge AI Person Detection.

Provides:
- AnnotationRenderer: Draws color-coded bounding boxes, confidence labels,
  and FPS overlay on video frames
- FPSCalculator: Calculates rolling average FPS over a 2-second window

Uses OpenCV for drawing operations directly on numpy arrays — no image
format conversion overhead. This is significantly faster than PIL-based
drawing on ARM64.
"""
import collections
import time

import numpy

try:
    import cv2
    _HAS_CV2 = True
except ImportError:
    cv2 = None
    _HAS_CV2 = False

from models import Detection

# BGR color constants
COLOR_RED = (0, 0, 255)
COLOR_ORANGE = (0, 128, 255)
COLOR_YELLOW = (0, 255, 255)
COLOR_GREEN = (0, 255, 0)
COLOR_WHITE = (255, 255, 255)
COLOR_BLACK = (0, 0, 0)

# Drawing constants
BBOX_THICKNESS = 2
LABEL_FONT_SCALE = 0.5
LABEL_THICKNESS = 1
FPS_FONT_SCALE = 0.7
FPS_THICKNESS = 2
FPS_POSITION = (10, 25)


class AnnotationRenderer(object):
    """Draws detection annotations and FPS overlay on frames.

    Uses OpenCV drawing functions which operate directly on numpy arrays
    without any image format conversion. This eliminates the expensive
    numpy→PIL→numpy round-trip that was the annotation bottleneck.
    """

    def __init__(self):
        pass

    def confidence_to_color(self, confidence):
        # type: (float) -> tuple
        """Map confidence score to BGR color.

        Args:
            confidence: Detection confidence in [0.0, 1.0].

        Returns:
            BGR color tuple:
            - Red (0, 0, 255) if confidence < 0.50
            - Orange (0, 128, 255) if 0.50 <= confidence < 0.65
            - Yellow (0, 255, 255) if 0.65 <= confidence < 0.80
            - Green (0, 255, 0) if confidence >= 0.80
        """
        if confidence < 0.50:
            return COLOR_RED
        elif confidence < 0.65:
            return COLOR_ORANGE
        elif confidence < 0.80:
            return COLOR_YELLOW
        else:
            return COLOR_GREEN

    def format_confidence_label(self, confidence):
        # type: (float) -> str
        """Format confidence score as a percentage string.

        Args:
            confidence: Detection confidence in [0.0, 1.0].

        Returns:
            Percentage string, e.g. "87%" for confidence=0.87.
        """
        return str(int(round(confidence * 100))) + "%"

    def clip_bbox(self, bbox, frame_width, frame_height):
        # type: (tuple, int, int) -> tuple
        """Clip bounding box pixel coordinates to frame boundaries.

        Args:
            bbox: Tuple of (x1, y1, x2, y2) pixel coordinates.
            frame_width: Frame width in pixels.
            frame_height: Frame height in pixels.

        Returns:
            Clipped (x1, y1, x2, y2) with all coordinates within
            [0, frame_width-1] x [0, frame_height-1].
        """
        x1, y1, x2, y2 = bbox
        x1 = max(0, min(x1, frame_width - 1))
        y1 = max(0, min(y1, frame_height - 1))
        x2 = max(0, min(x2, frame_width - 1))
        y2 = max(0, min(y2, frame_height - 1))
        return (x1, y1, x2, y2)

    def draw_detections(self, frame, detections, show_labels=True):
        # type: (numpy.ndarray, list, bool) -> numpy.ndarray
        """Draw all detection bounding boxes and confidence labels on frame.

        Uses OpenCV drawing functions directly on the numpy array — no
        image format conversion needed. This is ~10-50x faster than PIL.

        When detections is empty, the frame is returned unmodified.

        Args:
            frame: BGR image as numpy array (H, W, 3).
            detections: List of Detection objects with normalized coordinates.
            show_labels: Whether to draw confidence labels (default True).

        Returns:
            Annotated frame (same array, modified in place).
        """
        if not detections:
            return frame

        if not _HAS_CV2:
            return frame

        frame_height, frame_width = frame.shape[:2]

        for detection in detections:
            # Convert normalized coordinates to pixel coordinates
            x1 = int(detection.x_min * frame_width)
            y1 = int(detection.y_min * frame_height)
            x2 = int(detection.x_max * frame_width)
            y2 = int(detection.y_max * frame_height)

            # Clip to frame boundaries
            x1, y1, x2, y2 = self.clip_bbox(
                (x1, y1, x2, y2), frame_width, frame_height
            )

            # Skip degenerate boxes after clipping
            if x2 <= x1 or y2 <= y1:
                continue

            # Get color based on confidence
            color = self.confidence_to_color(detection.confidence)

            # Draw bounding box directly on numpy array
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, BBOX_THICKNESS)

            # Draw confidence label with filled background
            if show_labels:
                label = self.format_confidence_label(detection.confidence)
                (text_w, text_h), baseline = cv2.getTextSize(
                    label, cv2.FONT_HERSHEY_SIMPLEX, LABEL_FONT_SCALE, LABEL_THICKNESS
                )

                # Position label at top-left corner of bbox
                label_x = x1
                label_y = y1

                # Reposition label to stay within frame
                if label_y - text_h - baseline < 0:
                    label_y = y1 + text_h + baseline + 4
                if label_x + text_w > frame_width:
                    label_x = frame_width - text_w

                # Background rectangle for label
                bg_y1 = label_y - text_h - baseline
                bg_y2 = label_y + baseline
                cv2.rectangle(frame, (label_x, bg_y1), (label_x + text_w, bg_y2), color, -1)

                # Draw text
                cv2.putText(
                    frame, label, (label_x, label_y - baseline),
                    cv2.FONT_HERSHEY_SIMPLEX, LABEL_FONT_SCALE,
                    COLOR_BLACK, LABEL_THICKNESS
                )

        return frame

    def draw_fps_overlay(self, frame, fps, inference_ms=0.0, det_count=-1):
        # type: (numpy.ndarray, float, float, int) -> numpy.ndarray
        """Draw FPS and inference time overlay at fixed position (10, 25).

        Uses OpenCV putText directly on the numpy array.

        Args:
            frame: BGR image as numpy array (H, W, 3).
            fps: Current frames per second value.
            inference_ms: Average inference time in milliseconds.
            det_count: Number of detections to display. If >= 0, appended
                       to the overlay text.

        Returns:
            Frame with overlay drawn (same array, modified in place).
        """
        if not _HAS_CV2:
            return frame

        text = "FPS: {:.1f} | Inf: {:.0f}ms".format(fps, inference_ms)
        if det_count >= 0:
            text = text + " | Det: {}".format(det_count)

        # Get text size for background rectangle
        (text_w, text_h), baseline = cv2.getTextSize(
            text, cv2.FONT_HERSHEY_SIMPLEX, FPS_FONT_SCALE, FPS_THICKNESS
        )

        # Background rectangle behind text
        bg_x1 = FPS_POSITION[0] - 2
        bg_y1 = FPS_POSITION[1] - text_h - 2
        bg_x2 = FPS_POSITION[0] + text_w + 2
        bg_y2 = FPS_POSITION[1] + baseline + 2

        # Draw white background rectangle
        cv2.rectangle(frame, (bg_x1, bg_y1), (bg_x2, bg_y2), COLOR_WHITE, -1)

        # Draw black text
        cv2.putText(
            frame, text, FPS_POSITION,
            cv2.FONT_HERSHEY_SIMPLEX, FPS_FONT_SCALE,
            COLOR_BLACK, FPS_THICKNESS
        )

        return frame


class FPSCalculator(object):
    """Calculates rolling average FPS over a 2-second window.

    Uses a deque of timestamps to maintain a sliding window.
    FPS is computed as (N-1) / (last - first) where N is the number
    of timestamps in the window.
    """

    def __init__(self, window_seconds=2.0):
        # type: (float) -> None
        """Initialize FPSCalculator with a rolling window size.

        Args:
            window_seconds: Size of the rolling window in seconds (default 2.0).
        """
        self._window_seconds = window_seconds
        self._timestamps = collections.deque()  # type: collections.deque

    def tick(self, timestamp=None):
        # type: (float) -> None
        """Record a frame timestamp.

        Adds the timestamp to the rolling window and removes any
        timestamps that have fallen outside the window.

        Args:
            timestamp: The frame timestamp in seconds. If None, uses
                       the current time from time.time().
        """
        if timestamp is None:
            timestamp = time.time()

        self._timestamps.append(timestamp)

        # Remove timestamps outside the rolling window
        cutoff = timestamp - self._window_seconds
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

    def get_fps(self):
        # type: () -> float
        """Calculate average FPS from timestamps in the rolling window.

        Returns:
            The average FPS rounded to 1 decimal place.
            Returns 0.0 if fewer than 2 timestamps are in the window.
        """
        if len(self._timestamps) < 2:
            return 0.0

        first = self._timestamps[0]
        last = self._timestamps[-1]
        duration = last - first

        if duration <= 0.0:
            return 0.0

        fps = (len(self._timestamps) - 1) / duration
        return round(fps, 1)
