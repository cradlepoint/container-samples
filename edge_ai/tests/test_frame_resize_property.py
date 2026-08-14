"""Property-based test for frame resize dimensions.

Tests Property 2: Frame Resize Preserves Target Dimensions.
For any input frame with dimensions (w, h) where w > 0 and h > 0,
and any target model input size (tw, th) where tw > 0 and th > 0,
resizing the frame SHALL produce an output with dimensions exactly (tw, th).

**Validates: Requirements 1.5**
"""
import numpy
from hypothesis import given, settings, strategies as st

from processor import FrameProcessor


@settings(max_examples=100)
@given(
    src_w=st.integers(min_value=1, max_value=4096),
    src_h=st.integers(min_value=1, max_value=4096),
    target_w=st.integers(min_value=1, max_value=2048),
    target_h=st.integers(min_value=1, max_value=2048),
)
def test_frame_resize_preserves_target_dimensions(src_w, src_h, target_w, target_h):
    """Property 2: For any input frame (w, h) and target (tw, th),
    output has dimensions exactly (tw, th).

    **Validates: Requirements 1.5**
    """
    # Create a 3-channel input frame (H, W, C) matching real camera frames
    frame = numpy.zeros((src_h, src_w, 3), dtype=numpy.uint8)

    # Resize to target dimensions
    result = FrameProcessor.resize_frame(frame, (target_w, target_h))

    # Verify output spatial dimensions match target exactly
    assert result.shape[0] == target_h, (
        "Expected height {}, got {}".format(target_h, result.shape[0])
    )
    assert result.shape[1] == target_w, (
        "Expected width {}, got {}".format(target_w, result.shape[1])
    )
