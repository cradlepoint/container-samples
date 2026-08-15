"""Unit tests for FrameProcessor.

Tests specific example scenarios for frame processor behavior:
- resize_frame produces correct output dimensions
- compute_sleep_duration returns correct values
- check_adaptive_rate reduces and restores FPS correctly
- current_frame property is thread-safe
- process_loop orchestrates the pipeline correctly
- Periodic stats logging works as expected

Validates: Requirements 1.2, 1.5, 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 10.2, 10.3, 10.5
"""
import time
import threading
from unittest.mock import patch, MagicMock, PropertyMock

import numpy
import pytest

from src.processor import FrameProcessor
from src.annotation import AnnotationRenderer, FPSCalculator
from src.models import Detection


class TestResizeFrame:
    """Test resize_frame produces correct output dimensions.

    Validates: Requirement 1.5
    """

    def test_resize_to_smaller_dimensions(self):
        """Resizing a 640x480 frame to 320x240 produces (240, 320, 3)."""
        frame = numpy.zeros((480, 640, 3), dtype=numpy.uint8)
        result = FrameProcessor.resize_frame(frame, (320, 240))
        assert result.shape == (240, 320, 3)

    def test_resize_to_larger_dimensions(self):
        """Resizing a 320x240 frame to 640x480 produces (480, 640, 3)."""
        frame = numpy.zeros((240, 320, 3), dtype=numpy.uint8)
        result = FrameProcessor.resize_frame(frame, (640, 480))
        assert result.shape == (480, 640, 3)

    def test_resize_to_square(self):
        """Resizing a rectangular frame to square produces (640, 640, 3)."""
        frame = numpy.zeros((480, 640, 3), dtype=numpy.uint8)
        result = FrameProcessor.resize_frame(frame, (640, 640))
        assert result.shape == (640, 640, 3)

    def test_resize_preserves_dtype(self):
        """Resized frame preserves the original dtype."""
        frame = numpy.zeros((480, 640, 3), dtype=numpy.uint8)
        result = FrameProcessor.resize_frame(frame, (320, 240))
        assert result.dtype == numpy.uint8


class TestComputeSleepDuration:
    """Test compute_sleep_duration returns correct values.

    Validates: Requirements 1.2, 9.2
    """

    def test_no_elapsed_time(self):
        """With zero elapsed time, sleep equals full frame interval."""
        result = FrameProcessor.compute_sleep_duration(10, 0.0)
        assert abs(result - 0.1) < 1e-9

    def test_half_elapsed(self):
        """With half the frame interval elapsed, sleep is half."""
        result = FrameProcessor.compute_sleep_duration(10, 0.05)
        assert abs(result - 0.05) < 1e-9

    def test_elapsed_exceeds_interval(self):
        """When elapsed exceeds frame interval, sleep is 0."""
        result = FrameProcessor.compute_sleep_duration(10, 0.2)
        assert result == 0.0

    def test_never_negative(self):
        """Sleep duration is never negative even with large elapsed."""
        result = FrameProcessor.compute_sleep_duration(1, 100.0)
        assert result == 0.0

    def test_high_fps(self):
        """At 60 FPS with zero elapsed, sleep is ~16.67ms."""
        result = FrameProcessor.compute_sleep_duration(60, 0.0)
        assert abs(result - 1.0 / 60.0) < 1e-9

    def test_zero_fps_returns_zero(self):
        """Zero FPS returns 0 sleep (edge case protection)."""
        result = FrameProcessor.compute_sleep_duration(0, 0.0)
        assert result == 0.0


class TestCheckAdaptiveRate:
    """Test check_adaptive_rate reduces and restores FPS.

    Validates: Requirements 9.4, 9.5
    """

    def _make_processor(self, target_fps=10):
        """Create a FrameProcessor with mocked dependencies."""
        capture = MagicMock()
        engine = MagicMock()
        renderer = MagicMock()
        processor = FrameProcessor(capture, engine, renderer, target_fps)
        return processor

    def test_reduce_fps_on_high_latency(self):
        """10 consecutive latencies > 1000ms reduces FPS by half."""
        processor = self._make_processor(target_fps=10)
        latencies = [1200.0] * 10

        result = processor.check_adaptive_rate(latencies)

        assert result == 5

    def test_reduce_fps_minimum_floor(self):
        """FPS reduction cannot go below 1."""
        processor = self._make_processor(target_fps=2)
        latencies = [1500.0] * 10

        result = processor.check_adaptive_rate(latencies)

        assert result == 1

    def test_restore_fps_on_low_latency(self):
        """10 consecutive latencies < 500ms restores to target FPS."""
        processor = self._make_processor(target_fps=10)
        # First reduce the FPS
        processor._running_fps = 5
        latencies = [200.0] * 10

        result = processor.check_adaptive_rate(latencies)

        assert result == 10

    def test_no_change_with_mixed_latencies(self):
        """Mixed latencies (some high, some low) don't trigger changes."""
        processor = self._make_processor(target_fps=10)
        latencies = [1200.0, 300.0, 1100.0, 400.0, 1300.0,
                     200.0, 1400.0, 600.0, 1500.0, 100.0]

        result = processor.check_adaptive_rate(latencies)

        assert result == 10  # No change

    def test_fewer_than_10_latencies_no_change(self):
        """Fewer than 10 latencies don't trigger any rate change."""
        processor = self._make_processor(target_fps=10)
        latencies = [1500.0] * 5

        result = processor.check_adaptive_rate(latencies)

        assert result == 10

    def test_already_at_target_no_restore(self):
        """If already at target FPS, low latencies don't change anything."""
        processor = self._make_processor(target_fps=10)
        latencies = [200.0] * 10

        result = processor.check_adaptive_rate(latencies)

        assert result == 10


class TestCurrentFrameProperty:
    """Test thread-safe current_frame property.

    Validates: Requirement 9.1
    """

    def test_initial_frame_is_none(self):
        """current_frame is None before any processing."""
        capture = MagicMock()
        engine = MagicMock()
        renderer = MagicMock()
        processor = FrameProcessor(capture, engine, renderer, 10)

        assert processor.current_frame is None

    def test_set_and_get_frame(self):
        """Setting current_frame makes it retrievable."""
        capture = MagicMock()
        engine = MagicMock()
        renderer = MagicMock()
        processor = FrameProcessor(capture, engine, renderer, 10)

        frame = numpy.zeros((480, 640, 3), dtype=numpy.uint8)
        processor.current_frame = frame

        result = processor.current_frame
        assert result is not None
        assert numpy.array_equal(result, frame)

    def test_thread_safe_access(self):
        """Multiple threads can safely read/write current_frame."""
        capture = MagicMock()
        engine = MagicMock()
        renderer = MagicMock()
        processor = FrameProcessor(capture, engine, renderer, 10)

        errors = []

        def writer():
            for i in range(100):
                frame = numpy.full((10, 10, 3), i % 256, dtype=numpy.uint8)
                processor.current_frame = frame

        def reader():
            for _ in range(100):
                frame = processor.current_frame
                if frame is not None:
                    # Just verify it's a valid numpy array
                    if not isinstance(frame, numpy.ndarray):
                        errors.append("Got non-array: {}".format(type(frame)))

        t1 = threading.Thread(target=writer)
        t2 = threading.Thread(target=reader)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert errors == []


class TestProcessLoop:
    """Test process_loop orchestrates the pipeline.

    Validates: Requirements 9.1, 9.3, 10.2, 10.3
    """

    @patch('cp.log')
    def test_process_loop_captures_resizes_infers_annotates(self, mock_log):
        """process_loop runs the full pipeline: capture -> resize -> infer -> annotate."""
        capture = MagicMock()
        engine = MagicMock()
        renderer = MagicMock()

        # Set up capture to return one frame then stop the processor
        frame = numpy.zeros((480, 640, 3), dtype=numpy.uint8)
        processor = FrameProcessor(capture, engine, renderer, 10)
        processor.is_running = True

        call_count = [0]

        def read_frame_side_effect():
            call_count[0] += 1
            if call_count[0] == 1:
                return frame
            # Stop after first frame processed
            processor.stop()
            return None

        capture.read_frame.side_effect = read_frame_side_effect
        capture.is_connected = True

        # Set up engine
        engine.input_size = (640, 640)
        engine.detect.return_value = []

        # Set up renderer
        renderer.draw_detections.return_value = frame
        renderer.draw_fps_overlay.return_value = frame

        processor.process_loop()

        # Verify pipeline was called
        capture.read_frame.assert_called()
        engine.detect.assert_called_once()
        renderer.draw_detections.assert_called_once()
        renderer.draw_fps_overlay.assert_called_once()

    @patch('cp.log')
    def test_process_loop_stores_annotated_frame(self, mock_log):
        """process_loop stores the annotated frame for web server access."""
        capture = MagicMock()
        engine = MagicMock()
        renderer = MagicMock()

        frame = numpy.zeros((480, 640, 3), dtype=numpy.uint8)
        annotated = numpy.ones((480, 640, 3), dtype=numpy.uint8)

        processor = FrameProcessor(capture, engine, renderer, 10)
        processor.is_running = True

        call_count = [0]

        def read_frame_side_effect():
            call_count[0] += 1
            if call_count[0] == 1:
                return frame
            processor.stop()
            return None

        capture.read_frame.side_effect = read_frame_side_effect
        capture.is_connected = True
        engine.input_size = (640, 640)
        engine.detect.return_value = []
        renderer.draw_detections.return_value = annotated
        renderer.draw_fps_overlay.return_value = annotated

        processor.process_loop()

        assert processor.current_frame is not None
        assert numpy.array_equal(processor.current_frame, annotated)

    @patch('cp.log')
    def test_process_loop_handles_exception_gracefully(self, mock_log):
        """process_loop continues after an exception during processing."""
        capture = MagicMock()
        engine = MagicMock()
        renderer = MagicMock()

        frame = numpy.zeros((480, 640, 3), dtype=numpy.uint8)

        processor = FrameProcessor(capture, engine, renderer, 10)
        processor.is_running = True

        call_count = [0]

        def read_frame_side_effect():
            call_count[0] += 1
            if call_count[0] <= 2:
                return frame
            processor.stop()
            return None

        capture.read_frame.side_effect = read_frame_side_effect
        capture.is_connected = True
        engine.input_size = (640, 640)
        # First detect raises, second succeeds
        engine.detect.side_effect = [RuntimeError("test error"), []]
        renderer.draw_detections.return_value = frame
        renderer.draw_fps_overlay.return_value = frame

        processor.process_loop()

        # Should have logged the error
        log_calls = [str(c) for c in mock_log.call_args_list]
        assert any('error' in c.lower() for c in log_calls)


class TestPeriodicLogging:
    """Test periodic stats logging.

    Validates: Requirements 10.2, 10.5
    """

    @patch('cp.log')
    def test_logs_warning_on_zero_frames(self, mock_log):
        """Logs warning if zero frames processed in 60s interval."""
        capture = MagicMock()
        engine = MagicMock()
        renderer = MagicMock()
        processor = FrameProcessor(capture, engine, renderer, 10)

        # Set last log time to 61 seconds ago to trigger logging
        processor._last_log_time = time.time() - 61

        processor._check_periodic_logging()

        log_calls = [str(c) for c in mock_log.call_args_list]
        assert any('zero frames' in c.lower() for c in log_calls)

    @patch('cp.log')
    def test_logs_stats_when_frames_processed(self, mock_log):
        """Logs avg inference time and detection count after 60s."""
        capture = MagicMock()
        engine = MagicMock()
        renderer = MagicMock()
        processor = FrameProcessor(capture, engine, renderer, 10)

        # Simulate some processing
        processor._frames_processed = 50
        processor._inference_times = [20.0, 30.0, 25.0]
        processor._detection_count = 15
        processor._last_log_time = time.time() - 61

        processor._check_periodic_logging()

        log_calls = [str(c) for c in mock_log.call_args_list]
        assert any('inference' in c.lower() and 'detections' in c.lower()
                   for c in log_calls)

    @patch('cp.log')
    def test_resets_counters_after_logging(self, mock_log):
        """Counters are reset after periodic logging."""
        capture = MagicMock()
        engine = MagicMock()
        renderer = MagicMock()
        processor = FrameProcessor(capture, engine, renderer, 10)

        processor._frames_processed = 50
        processor._inference_times = [20.0, 30.0]
        processor._detection_count = 10
        processor._last_log_time = time.time() - 61

        processor._check_periodic_logging()

        assert processor._frames_processed == 0
        assert processor._inference_times == []
        assert processor._detection_count == 0


class TestSetTargetFps:
    """Test set_target_fps updates both target and running FPS."""

    def test_updates_target_fps(self):
        """set_target_fps updates the target_fps attribute."""
        capture = MagicMock()
        engine = MagicMock()
        renderer = MagicMock()
        processor = FrameProcessor(capture, engine, renderer, 10)

        processor.set_target_fps(20)

        assert processor.target_fps == 20
        assert processor._running_fps == 20
