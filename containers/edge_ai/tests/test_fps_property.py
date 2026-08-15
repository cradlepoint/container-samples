"""Property-based test for FPS calculation over rolling window.

Tests Property 7: FPS Calculation Over Rolling Window.
For any sequence of N >= 2 monotonically increasing timestamps within a
2-second window, the calculated FPS SHALL equal (N-1) / (last - first),
rounded to 1 decimal place. For fewer than 2 timestamps, FPS SHALL be 0.0.

**Validates: Requirements 4.2, 4.3**
"""
from hypothesis import given, settings, assume, strategies as st

from annotation import FPSCalculator


@settings(max_examples=100)
@given(
    start=st.floats(min_value=0.0, max_value=1e6),
    increments=st.lists(
        st.floats(min_value=1e-9, max_value=1.0),
        min_size=1,
        max_size=50,
    ),
)
def test_fps_equals_formula_for_monotonic_timestamps(start, increments):
    """Property 7: For N>=2 monotonically increasing timestamps within a
    2-second window, FPS = (N-1) / (last - first), rounded to 1 decimal.

    **Validates: Requirements 4.2, 4.3**
    """
    # Build monotonically increasing timestamps from start + cumulative increments
    timestamps = [start]
    current = start
    for inc in increments:
        current = current + inc
        timestamps.append(current)

    # Ensure all timestamps fit within a 2-second window
    total_span = timestamps[-1] - timestamps[0]
    assume(total_span > 0.0)
    assume(total_span <= 2.0)

    # N >= 2 is guaranteed since we have start + at least 1 increment
    n = len(timestamps)
    assert n >= 2

    calc = FPSCalculator(window_seconds=2.0)
    for ts in timestamps:
        calc.tick(ts)

    expected_fps = round((n - 1) / (timestamps[-1] - timestamps[0]), 1)
    actual_fps = calc.get_fps()

    assert actual_fps == expected_fps, (
        "N={}, timestamps[0]={}, timestamps[-1]={}: expected FPS={}, got {}".format(
            n, timestamps[0], timestamps[-1], expected_fps, actual_fps
        )
    )


@settings(max_examples=100)
@given(data=st.data())
def test_fps_zero_for_fewer_than_two_timestamps(data):
    """Property 7: For fewer than 2 timestamps, FPS SHALL be 0.0.

    **Validates: Requirements 4.2, 4.3**
    """
    calc = FPSCalculator(window_seconds=2.0)

    # Test with 0 timestamps
    assert calc.get_fps() == 0.0

    # Test with exactly 1 timestamp
    ts = data.draw(st.floats(min_value=0.0, max_value=1e6))
    calc.tick(ts)
    assert calc.get_fps() == 0.0


@settings(max_examples=100)
@given(
    start=st.floats(min_value=0.0, max_value=1e6),
    increments=st.lists(
        st.floats(min_value=1e-9, max_value=0.5),
        min_size=1,
        max_size=100,
    ),
)
def test_fps_is_non_negative(start, increments):
    """Property 7: FPS value is always non-negative.

    **Validates: Requirements 4.2, 4.3**
    """
    calc = FPSCalculator(window_seconds=2.0)

    current = start
    calc.tick(current)
    for inc in increments:
        current = current + inc
        calc.tick(current)

    fps = calc.get_fps()
    assert fps >= 0.0, "FPS should never be negative, got {}".format(fps)
