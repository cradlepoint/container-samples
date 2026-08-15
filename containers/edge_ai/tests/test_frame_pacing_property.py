"""Property-based test for frame pacing sleep duration.

Tests Property 10: Frame Pacing Sleep Duration.
For any target_fps in [1, 60] and any elapsed processing time e >= 0,
the computed sleep duration SHALL equal max(0, (1.0 / target_fps) - e).
The sleep duration SHALL never be negative.

**Validates: Requirements 1.2, 9.2**
"""
from hypothesis import given, settings, strategies as st

from processor import FrameProcessor


@settings(max_examples=100)
@given(
    target_fps=st.integers(min_value=1, max_value=60),
    elapsed=st.floats(min_value=0.0, max_value=10.0, allow_nan=False, allow_infinity=False)
)
def test_frame_pacing_equals_formula(target_fps, elapsed):
    """Property 10: For any target_fps in [1,60] and elapsed e>=0,
    compute_sleep_duration returns max(0, 1/target_fps - e).

    **Validates: Requirements 1.2, 9.2**
    """
    result = FrameProcessor.compute_sleep_duration(target_fps, elapsed)
    expected = max(0.0, (1.0 / target_fps) - elapsed)
    assert abs(result - expected) < 1e-9, (
        "target_fps={}, elapsed={}: expected {}, got {}".format(
            target_fps, elapsed, expected, result)
    )


@settings(max_examples=100)
@given(
    target_fps=st.integers(min_value=1, max_value=60),
    elapsed=st.floats(min_value=0.0, max_value=10.0, allow_nan=False, allow_infinity=False)
)
def test_frame_pacing_never_negative(target_fps, elapsed):
    """Property 10: The sleep duration SHALL never be negative.

    **Validates: Requirements 1.2, 9.2**
    """
    result = FrameProcessor.compute_sleep_duration(target_fps, elapsed)
    assert result >= 0.0, (
        "target_fps={}, elapsed={}: sleep duration {} is negative".format(
            target_fps, elapsed, result)
    )
