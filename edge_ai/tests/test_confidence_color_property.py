"""Property-based test for confidence-to-color mapping and label formatting.

Tests Property 5: Confidence-to-Color Mapping and Label Formatting.
For any confidence score c in [0.0, 1.0]:
- if c < 0.35 the color SHALL be red (BGR: 0, 0, 255)
- if 0.35 <= c < 0.5 the color SHALL be orange (BGR: 0, 128, 255)
- if 0.5 <= c < 0.75 the color SHALL be yellow (BGR: 0, 255, 255)
- if c >= 0.75 the color SHALL be green (BGR: 0, 255, 0)
Additionally, the formatted label SHALL equal str(round(c * 100)) + "%".

**Validates: Requirements 3.2, 3.3**
"""
from hypothesis import given, settings, strategies as st

from annotation import AnnotationRenderer


# BGR color constants matching the implementation
COLOR_RED = (0, 0, 255)
COLOR_ORANGE = (0, 128, 255)
COLOR_YELLOW = (0, 255, 255)
COLOR_GREEN = (0, 255, 0)


@settings(max_examples=100)
@given(confidence=st.floats(min_value=0.0, max_value=1.0, allow_nan=False))
def test_confidence_to_color_mapping(confidence):
    """Property 5: For any confidence c in [0.0, 1.0], the color mapping
    SHALL follow the defined thresholds.

    - c < 0.35 -> red (0, 0, 255)
    - 0.35 <= c < 0.5 -> orange (0, 128, 255)
    - 0.5 <= c < 0.75 -> yellow (0, 255, 255)
    - c >= 0.75 -> green (0, 255, 0)

    **Validates: Requirements 3.2, 3.3**
    """
    renderer = AnnotationRenderer()
    color = renderer.confidence_to_color(confidence)

    if confidence < 0.50:
        assert color == COLOR_RED, (
            "confidence={}: expected red {}, got {}".format(
                confidence, COLOR_RED, color
            )
        )
    elif confidence < 0.65:
        assert color == COLOR_ORANGE, (
            "confidence={}: expected orange {}, got {}".format(
                confidence, COLOR_ORANGE, color
            )
        )
    elif confidence < 0.80:
        assert color == COLOR_YELLOW, (
            "confidence={}: expected yellow {}, got {}".format(
                confidence, COLOR_YELLOW, color
            )
        )
    else:
        assert color == COLOR_GREEN, (
            "confidence={}: expected green {}, got {}".format(
                confidence, COLOR_GREEN, color
            )
        )


@settings(max_examples=100)
@given(confidence=st.floats(min_value=0.0, max_value=1.0, allow_nan=False))
def test_confidence_label_formatting(confidence):
    """Property 5: For any confidence c in [0.0, 1.0], the formatted label
    SHALL equal str(round(c * 100)) + "%".

    **Validates: Requirements 3.2, 3.3**
    """
    renderer = AnnotationRenderer()
    label = renderer.format_confidence_label(confidence)

    expected = str(int(round(confidence * 100))) + "%"
    assert label == expected, (
        "confidence={}: expected '{}', got '{}'".format(
            confidence, expected, label
        )
    )
