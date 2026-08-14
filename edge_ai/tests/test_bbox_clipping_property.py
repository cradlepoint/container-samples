"""Property-based test for bounding box clipping to frame boundaries.

Tests Property 6: Bounding Box Clipping to Frame Boundaries.
For any bounding box with pixel coordinates (x1, y1, x2, y2) -- including
values outside frame dimensions -- and any frame of size (W, H) where W > 0
and H > 0, the clipped bounding box SHALL have all coordinates within
[0, W) x [0, H), and the clipped box SHALL have non-negative width and height.

**Validates: Requirements 3.6**
"""
from hypothesis import given, settings, strategies as st

from annotation import AnnotationRenderer


@settings(max_examples=100)
@given(
    x1=st.integers(min_value=-1000, max_value=2000),
    y1=st.integers(min_value=-1000, max_value=2000),
    x2=st.integers(min_value=-1000, max_value=2000),
    y2=st.integers(min_value=-1000, max_value=2000),
    frame_width=st.integers(min_value=1, max_value=4096),
    frame_height=st.integers(min_value=1, max_value=4096),
)
def test_clipped_bbox_within_frame_boundaries(x1, y1, x2, y2, frame_width, frame_height):
    """Property 6: All clipped coordinates are within [0, W) x [0, H).

    **Validates: Requirements 3.6**
    """
    renderer = AnnotationRenderer()
    cx1, cy1, cx2, cy2 = renderer.clip_bbox((x1, y1, x2, y2), frame_width, frame_height)

    # All coordinates within [0, frame_width-1] x [0, frame_height-1]
    # which is equivalent to [0, W) x [0, H) for integer coordinates
    assert 0 <= cx1 <= frame_width - 1, (
        "cx1={} not in [0, {}]".format(cx1, frame_width - 1)
    )
    assert 0 <= cy1 <= frame_height - 1, (
        "cy1={} not in [0, {}]".format(cy1, frame_height - 1)
    )
    assert 0 <= cx2 <= frame_width - 1, (
        "cx2={} not in [0, {}]".format(cx2, frame_width - 1)
    )
    assert 0 <= cy2 <= frame_height - 1, (
        "cy2={} not in [0, {}]".format(cy2, frame_height - 1)
    )


@st.composite
def bbox_strategy(draw):
    """Generate a bounding box where x1 <= x2 and y1 <= y2.

    Coordinates may extend outside frame boundaries (negative or very large)
    but maintain valid ordering as produced by Detection model conversion.
    """
    a = draw(st.integers(min_value=-1000, max_value=2000))
    b = draw(st.integers(min_value=-1000, max_value=2000))
    c = draw(st.integers(min_value=-1000, max_value=2000))
    d = draw(st.integers(min_value=-1000, max_value=2000))
    x1, x2 = min(a, b), max(a, b)
    y1, y2 = min(c, d), max(c, d)
    return (x1, y1, x2, y2)


@settings(max_examples=100)
@given(
    bbox=bbox_strategy(),
    frame_width=st.integers(min_value=1, max_value=4096),
    frame_height=st.integers(min_value=1, max_value=4096),
)
def test_clipped_bbox_has_non_negative_dimensions(bbox, frame_width, frame_height):
    """Property 6: Clipped box has non-negative width and height.

    Uses valid bounding boxes (x1 <= x2, y1 <= y2) as produced by
    Detection model coordinate conversion.

    **Validates: Requirements 3.6**
    """
    x1, y1, x2, y2 = bbox
    renderer = AnnotationRenderer()
    cx1, cy1, cx2, cy2 = renderer.clip_bbox((x1, y1, x2, y2), frame_width, frame_height)

    width = cx2 - cx1
    height = cy2 - cy1
    assert width >= 0, (
        "Clipped width={} is negative for bbox=({},{},{},{}), frame={}x{}".format(
            width, x1, y1, x2, y2, frame_width, frame_height
        )
    )
    assert height >= 0, (
        "Clipped height={} is negative for bbox=({},{},{},{}), frame={}x{}".format(
            height, x1, y1, x2, y2, frame_width, frame_height
        )
    )
