"""Property-based test for rate restoration on recovered latency.

Tests Property 12: Rate Restoration on Recovered Latency.
For any reduced operating FPS and configured target FPS, when 10 consecutive
inference latencies are all below 500ms, the operating FPS SHALL be restored
to the configured target FPS value.

**Validates: Requirements 9.5**
"""
from unittest.mock import MagicMock

from hypothesis import given, settings, strategies as st

from src.processor import FrameProcessor


def _make_processor(target_fps, running_fps):
    """Create a FrameProcessor with mocked dependencies and reduced FPS.

    Args:
        target_fps: The configured target FPS.
        running_fps: The current reduced operating FPS (must be < target_fps).

    Returns:
        A FrameProcessor instance with _running_fps set to running_fps.
    """
    capture = MagicMock()
    engine = MagicMock()
    renderer = MagicMock()
    processor = FrameProcessor(capture, engine, renderer, target_fps)
    processor._running_fps = running_fps
    return processor


@settings(max_examples=100)
@given(
    target_fps=st.integers(min_value=2, max_value=60),
    latency=st.floats(min_value=0.0, max_value=499.9),
)
def test_rate_restored_to_target_on_low_latency(target_fps, latency):
    """Property 12: For any reduced operating FPS, when 10 consecutive
    latencies are all below 500ms, the operating FPS is restored to the
    configured target FPS.

    **Validates: Requirements 9.5**
    """
    # running_fps must be less than target_fps to be in a "reduced" state
    running_fps = max(1, target_fps // 2)
    processor = _make_processor(target_fps, running_fps)

    # Create 10 consecutive low latencies (all below 500ms)
    latencies = [latency] * 10

    result = processor.check_adaptive_rate(latencies)

    assert result == target_fps, (
        "target_fps={}, running_fps={}, latency={}: "
        "expected restoration to {}, got {}".format(
            target_fps, running_fps, latency, target_fps, result
        )
    )


@settings(max_examples=100)
@given(
    target_fps=st.integers(min_value=2, max_value=60),
    running_fps_divisor=st.integers(min_value=2, max_value=10),
    latencies=st.lists(
        st.floats(min_value=0.0, max_value=499.9),
        min_size=10,
        max_size=10,
    ),
)
def test_rate_restored_for_various_reduction_levels(target_fps, running_fps_divisor, latencies):
    """Property 12: For any level of FPS reduction, 10 consecutive low
    latencies restore to the configured target FPS.

    **Validates: Requirements 9.5**
    """
    # Simulate various levels of reduction
    running_fps = max(1, target_fps // running_fps_divisor)
    # Only test when actually reduced
    if running_fps >= target_fps:
        running_fps = max(1, target_fps - 1)

    processor = _make_processor(target_fps, running_fps)

    result = processor.check_adaptive_rate(latencies)

    assert result == target_fps, (
        "target_fps={}, running_fps={}, latencies={}: "
        "expected restoration to {}, got {}".format(
            target_fps, running_fps, latencies, target_fps, result
        )
    )
