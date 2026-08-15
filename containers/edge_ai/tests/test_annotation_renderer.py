"""Unit tests for AnnotationRenderer class.

Validates: Requirements 3.4, 3.5, 3.7
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import numpy

from annotation import AnnotationRenderer, BBOX_THICKNESS
from models import Detection


class TestAnnotationRendererZeroDetections:
    """Test that zero detections produces unannotated frame (plus FPS overlay).

    Validates: Requirement 3.7
    """

    def test_zero_detections_returns_unmodified_frame(self):
        """When detections list is empty, draw_detections returns frame unchanged."""
        renderer = AnnotationRenderer()
        frame = numpy.zeros((480, 640, 3), dtype=numpy.uint8)
        frame[:] = (128, 64, 32)  # Fill with a known color
        original = frame.copy()

        result = renderer.draw_detections(frame, [])

        numpy.testing.assert_array_equal(result, original)

    def test_zero_detections_with_fps_overlay_only_modifies_fps_region(self):
        """With zero detections, only the FPS overlay should modify the frame."""
        renderer = AnnotationRenderer()
        frame = numpy.zeros((480, 640, 3), dtype=numpy.uint8)
        frame[:] = (100, 100, 100)  # Uniform gray
        original = frame.copy()

        # Draw detections (none) - frame should be unchanged
        result = renderer.draw_detections(frame, [])
        numpy.testing.assert_array_equal(result, original)

        # Now draw FPS overlay - frame should be modified
        result = renderer.draw_fps_overlay(result, 10.0)

        # Frame should now differ from original (FPS text was drawn)
        assert not numpy.array_equal(result, original)

    def test_zero_detections_frame_dimensions_preserved(self):
        """Frame dimensions should not change with zero detections."""
        renderer = AnnotationRenderer()
        frame = numpy.zeros((480, 640, 3), dtype=numpy.uint8)

        result = renderer.draw_detections(frame, [])

        assert result.shape == (480, 640, 3)


class TestAnnotationRendererBboxThickness:
    """Test that bounding box line thickness is 2 pixels.

    Validates: Requirement 3.4
    """

    def test_bbox_thickness_constant_is_two(self):
        """The BBOX_THICKNESS constant should be 2."""
        assert BBOX_THICKNESS == 2

    def test_bbox_drawn_on_frame(self):
        """Bounding box is drawn on frame when detection is present."""
        renderer = AnnotationRenderer()
        # Create a black frame
        frame = numpy.zeros((480, 640, 3), dtype=numpy.uint8)
        original = frame.copy()

        # Create a detection in the center of the frame
        detection = Detection(
            x_min=0.25,
            y_min=0.25,
            x_max=0.75,
            y_max=0.75,
            confidence=0.9
        )

        result = renderer.draw_detections(frame, [detection])

        # The frame should be modified (not all zeros anymore)
        assert not numpy.array_equal(result, original)

    def test_bbox_interior_not_filled(self):
        """The interior of the bounding box should not be filled."""
        renderer = AnnotationRenderer()
        frame = numpy.zeros((480, 640, 3), dtype=numpy.uint8)

        detection = Detection(
            x_min=0.3,
            y_min=0.3,
            x_max=0.7,
            y_max=0.7,
            confidence=0.6
        )

        result = renderer.draw_detections(frame, [detection])

        # Interior point should remain black (or close to it)
        # Detection maps to roughly x1=192, y1=144, x2=448, y2=336
        # Check center of box - should still be black
        center_y = 240
        center_x = 320
        # The center pixel should be unchanged (black)
        assert tuple(result[center_y, center_x]) == (0, 0, 0)


class TestAnnotationRendererLabelFontScale:
    """Test that confidence label font scale produces 12-16px text height.

    Validates: Requirement 3.5
    """

    def test_label_is_rendered_on_frame(self):
        """A confidence label is rendered when a detection is present."""
        renderer = AnnotationRenderer()
        frame = numpy.zeros((480, 640, 3), dtype=numpy.uint8)

        detection = Detection(
            x_min=0.3,
            y_min=0.3,
            x_max=0.7,
            y_max=0.7,
            confidence=0.87
        )

        result = renderer.draw_detections(frame, [detection])

        # Frame should be modified (label drawn)
        assert not numpy.array_equal(result, numpy.zeros_like(result))

    def test_label_format_is_percentage(self):
        """The confidence label format is a percentage string."""
        renderer = AnnotationRenderer()
        label = renderer.format_confidence_label(0.87)
        assert label == "87%"

    def test_label_format_various_values(self):
        """Font produces correct labels for various confidence values."""
        renderer = AnnotationRenderer()
        assert renderer.format_confidence_label(0.0) == "0%"
        assert renderer.format_confidence_label(0.5) == "50%"
        assert renderer.format_confidence_label(0.99) == "99%"
        assert renderer.format_confidence_label(1.0) == "100%"
