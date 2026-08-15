"""Property-based tests for Detection output normalization.

Tests Property 4: Detection Output Normalization Invariant.
For any Detection, all bounding box coordinates are in [0.0, 1.0],
x_min < x_max, y_min < y_max, confidence in [0.0, 1.0].

**Validates: Requirements 2.3**
"""
from hypothesis import given, settings, strategies as st

from models import Detection, validate_detection


# Strategy for valid normalized coordinates where x_min < x_max and y_min < y_max
def valid_detection_strategy():
    """Generate Detection objects that satisfy all normalization invariants."""
    return st.builds(
        Detection,
        x_min=st.floats(min_value=0.0, max_value=0.99, allow_nan=False, allow_infinity=False),
        y_min=st.floats(min_value=0.0, max_value=0.99, allow_nan=False, allow_infinity=False),
        x_max=st.floats(min_value=0.01, max_value=1.0, allow_nan=False, allow_infinity=False),
        y_max=st.floats(min_value=0.01, max_value=1.0, allow_nan=False, allow_infinity=False),
        confidence=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
    ).filter(lambda d: d.x_min < d.x_max and d.y_min < d.y_max)


def invalid_detection_strategy():
    """Generate Detection objects that violate at least one normalization invariant."""
    # Strategy: generate detections with at least one invalid property
    return st.one_of(
        # x_min out of range (negative)
        st.builds(
            Detection,
            x_min=st.floats(max_value=-0.001, allow_nan=False, allow_infinity=False),
            y_min=st.floats(min_value=0.0, max_value=0.5, allow_nan=False, allow_infinity=False),
            x_max=st.floats(min_value=0.5, max_value=1.0, allow_nan=False, allow_infinity=False),
            y_max=st.floats(min_value=0.6, max_value=1.0, allow_nan=False, allow_infinity=False),
            confidence=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
        ),
        # x_max out of range (> 1.0)
        st.builds(
            Detection,
            x_min=st.floats(min_value=0.0, max_value=0.5, allow_nan=False, allow_infinity=False),
            y_min=st.floats(min_value=0.0, max_value=0.5, allow_nan=False, allow_infinity=False),
            x_max=st.floats(min_value=1.001, allow_nan=False, allow_infinity=False).filter(lambda x: x <= 1e10),
            y_max=st.floats(min_value=0.6, max_value=1.0, allow_nan=False, allow_infinity=False),
            confidence=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
        ),
        # x_min >= x_max (invalid ordering)
        st.builds(
            Detection,
            x_min=st.floats(min_value=0.5, max_value=1.0, allow_nan=False, allow_infinity=False),
            y_min=st.floats(min_value=0.0, max_value=0.5, allow_nan=False, allow_infinity=False),
            x_max=st.floats(min_value=0.0, max_value=0.5, allow_nan=False, allow_infinity=False),
            y_max=st.floats(min_value=0.6, max_value=1.0, allow_nan=False, allow_infinity=False),
            confidence=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
        ),
        # y_min >= y_max (invalid ordering)
        st.builds(
            Detection,
            x_min=st.floats(min_value=0.0, max_value=0.4, allow_nan=False, allow_infinity=False),
            y_min=st.floats(min_value=0.5, max_value=1.0, allow_nan=False, allow_infinity=False),
            x_max=st.floats(min_value=0.5, max_value=1.0, allow_nan=False, allow_infinity=False),
            y_max=st.floats(min_value=0.0, max_value=0.5, allow_nan=False, allow_infinity=False),
            confidence=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
        ),
        # confidence out of range (> 1.0)
        st.builds(
            Detection,
            x_min=st.floats(min_value=0.0, max_value=0.4, allow_nan=False, allow_infinity=False),
            y_min=st.floats(min_value=0.0, max_value=0.4, allow_nan=False, allow_infinity=False),
            x_max=st.floats(min_value=0.5, max_value=1.0, allow_nan=False, allow_infinity=False),
            y_max=st.floats(min_value=0.5, max_value=1.0, allow_nan=False, allow_infinity=False),
            confidence=st.floats(min_value=1.001, allow_nan=False, allow_infinity=False).filter(lambda c: c <= 1e10),
        ),
        # confidence out of range (negative)
        st.builds(
            Detection,
            x_min=st.floats(min_value=0.0, max_value=0.4, allow_nan=False, allow_infinity=False),
            y_min=st.floats(min_value=0.0, max_value=0.4, allow_nan=False, allow_infinity=False),
            x_max=st.floats(min_value=0.5, max_value=1.0, allow_nan=False, allow_infinity=False),
            y_max=st.floats(min_value=0.5, max_value=1.0, allow_nan=False, allow_infinity=False),
            confidence=st.floats(max_value=-0.001, allow_nan=False, allow_infinity=False),
        ),
    )


@settings(max_examples=100)
@given(detection=valid_detection_strategy())
def test_valid_detection_passes_validation(detection):
    """Property 4: Valid detections with all coordinates in [0.0, 1.0],
    x_min < x_max, y_min < y_max, and confidence in [0.0, 1.0] must
    pass validation.

    **Validates: Requirements 2.3**
    """
    assert validate_detection(detection) is True


@settings(max_examples=100)
@given(detection=invalid_detection_strategy())
def test_invalid_detection_fails_validation(detection):
    """Property 4: Detections violating any normalization invariant
    (coordinates outside [0.0, 1.0], x_min >= x_max, y_min >= y_max,
    or confidence outside [0.0, 1.0]) must fail validation.

    **Validates: Requirements 2.3**
    """
    assert validate_detection(detection) is False
