"""Property-based test for adaptive rate reduction on high latency.

Tests Property 11: Adaptive Rate Reduction on High Latency.
For any current target FPS f > 1 and a sequence of 10 consecutive
inference latencies all exceeding 1000ms, the adjusted target FPS
SHALL equal max(f // 2, 1).

**Validates: Requirements 9.4**
"""
from unittest.mock import MagicMock, patch

from hypothesis import given, settings, strategies as st

from processor import FrameProcessor


def _make_processor(target_fps):
    """Create a FrameProcessor with mocked dependencies and given target FPS."""
    capture = MagicMock()
    engine = MagicMock()
    renderer = MagicMock()
    processor = FrameProcessor(capture, engine, renderer, target_fps)
    return processor


@settings(max_examples=100)
@given(
    fps=st.integers(min_value=2, max_value=60),
    latency=st.floats(min_value=1000.01, max_value=50000.0),
)
@patch('processor.cp.log')
def test_adaptive_rate_reduction_halves_fps(mock_log, fps, latency):
    """Property 11: For any current FPS f > 1 and 10 consecutive latencies
    > 1000ms, adjusted FPS = max(f // 2, 1).

    **Validates: Requirements 9.4**
    """
    processor = _make_processor(target_fps=fps)
    # Ensure _running_fps starts at the given fps value
    processor._running_fps = fps

    # Build 10 consecutive latencies all exceeding 1000ms
    latencies = [latency] * 10

    result = processor.check_adaptive_rate(latencies)

    expected = max(fps // 2, 1)
    assert result == expected, (
        "fps={}, latency={}: expected adjusted FPS {}, got {}".format(
            fps, latency, expected, result
        )
    )
