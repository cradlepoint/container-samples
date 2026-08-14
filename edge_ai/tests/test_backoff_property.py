"""Property-based test for exponential backoff computation.

Tests Property 1: Exponential Backoff Computation.
For any retry count n >= 0, the computed backoff delay SHALL equal
min(2^(n+1), 60) seconds, always producing a value in the range [2, 60].

**Validates: Requirements 1.3**
"""
from hypothesis import given, settings, strategies as st

from capture import RTSPCapture


@settings(max_examples=100)
@given(retry_count=st.integers(min_value=0, max_value=1000))
def test_exponential_backoff_equals_formula(retry_count):
    """Property 1: For any retry count n >= 0, compute_backoff returns
    min(2^(n+1), 60).

    **Validates: Requirements 1.3**
    """
    result = RTSPCapture.compute_backoff(retry_count)
    expected = min(2 ** (retry_count + 1), 60)
    assert result == expected, (
        "retry_count={}: expected {}, got {}".format(retry_count, expected, result)
    )


@settings(max_examples=100)
@given(retry_count=st.integers(min_value=0, max_value=1000))
def test_exponential_backoff_always_in_range(retry_count):
    """Property 1: For any retry count n >= 0, compute_backoff always
    produces a value in the range [2, 60].

    **Validates: Requirements 1.3**
    """
    result = RTSPCapture.compute_backoff(retry_count)
    assert 2 <= result <= 60, (
        "retry_count={}: result {} not in [2, 60]".format(retry_count, result)
    )
