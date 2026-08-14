"""Shared data models for Edge AI Person Detection.

Defines the core dataclasses used across the application:
- Detection: A single person detection result with normalized coordinates
- AppConfig: Application configuration loaded from router appdata
- RuntimeStats: Operational statistics for monitoring
- validate_detection: Validates Detection invariants
"""
from dataclasses import dataclass, field


@dataclass
class Detection:
    """Single person detection with normalized coordinates.

    All bounding box coordinates are normalized to [0.0, 1.0] relative
    to frame dimensions. Confidence is in [0.0, 1.0].
    """

    x_min: float  # Left edge, normalized [0.0, 1.0]
    y_min: float  # Top edge, normalized [0.0, 1.0]
    x_max: float  # Right edge, normalized [0.0, 1.0]
    y_max: float  # Bottom edge, normalized [0.0, 1.0]
    confidence: float  # Detection confidence [0.0, 1.0]


@dataclass
class AppConfig:
    """Application configuration loaded from router appdata via cp.get_appdata().

    Required fields:
        rtsp_input_url: Must start with "rtsp://"

    Optional fields with defaults:
        confidence_threshold: Range [0.0, 1.0], default 0.4
        web_port: Range [1024, 65535], default 8080
        target_fps: Range [1, 60], default 10
        skip_inference_frames: Range [0, 10], default 0 (disabled)
        model_name: One of 'ssd_mobilenet_v2', 'yolov5n'
    """

    rtsp_input_url: str
    confidence_threshold: float = 0.4
    web_port: int = 8080
    target_fps: int = 10
    skip_inference_frames: int = 0
    model_name: str = 'ssd_mobilenet_v2'
    jpeg_quality: int = 70


@dataclass
class RuntimeStats:
    """Operational statistics for monitoring.

    Provides real-time metrics about the application's performance
    and connection state.
    """

    current_fps: float
    total_detections: int
    avg_inference_ms: float
    connection_status: str  # "connected", "disconnected", "reconnecting"
    uptime_seconds: float


def validate_detection(detection):
    # type: (Detection) -> bool
    """Validate that a Detection satisfies the normalization invariant.

    Checks:
    - All bounding box coordinates (x_min, y_min, x_max, y_max) are in [0.0, 1.0]
    - x_min < x_max (positive width)
    - y_min < y_max (positive height)
    - confidence is in [0.0, 1.0]

    Returns True if all invariants hold, False otherwise.
    """
    # Check coordinate ranges
    if not (0.0 <= detection.x_min <= 1.0):
        return False
    if not (0.0 <= detection.y_min <= 1.0):
        return False
    if not (0.0 <= detection.x_max <= 1.0):
        return False
    if not (0.0 <= detection.y_max <= 1.0):
        return False

    # Check ordering constraints
    if not (detection.x_min < detection.x_max):
        return False
    if not (detection.y_min < detection.y_max):
        return False

    # Check confidence range
    if not (0.0 <= detection.confidence <= 1.0):
        return False

    return True
