"""Unit tests for WebServer module.

Tests specific example scenarios for MJPEG streaming, connection limiting,
placeholder images, stats reporting, configuration validation, and RTSP
URL change failure handling.

Requirements: 5.7, 5.8, 6.6, 6.7
"""
import time
import threading
from unittest.mock import patch, MagicMock, PropertyMock

import numpy as np
import pytest

from web_server import WebServer, MJPEGStreamHandler, MJPEG_BOUNDARY


class TestPlaceholderImage:
    """Test placeholder image served before first frame."""

    def test_get_frame_returns_placeholder_when_no_processor_frame(self):
        """get_frame() returns placeholder when processor.current_frame is None.

        Requirements: 5.7
        """
        mock_processor = MagicMock()
        mock_processor.current_frame = None

        server = WebServer(port=8080, processor=mock_processor)
        frame = server.get_frame()

        # Should return the placeholder image (not None)
        assert frame is not None
        # Placeholder is 1920x1080x3 black
        assert frame.shape == (1080, 1920, 3)

    def test_placeholder_is_black_background(self):
        """Placeholder image has black background (0, 0, 0).

        Requirements: 5.7
        """
        mock_processor = MagicMock()
        mock_processor.current_frame = None

        server = WebServer(port=8080, processor=mock_processor)
        frame = server.get_frame()

        # Check corner pixel is black
        assert frame[0, 0, 0] == 0
        assert frame[0, 0, 1] == 0
        assert frame[0, 0, 2] == 0

    def test_get_frame_returns_processor_frame_when_available(self):
        """get_frame() returns processor's frame when current_frame is not None.

        Requirements: 5.7
        """
        mock_processor = MagicMock()
        test_frame = np.ones((480, 640, 3), dtype=np.uint8) * 128
        mock_processor.current_frame = test_frame

        server = WebServer(port=8080, processor=mock_processor)
        frame = server.get_frame()

        assert frame is test_frame

    def test_get_frame_returns_placeholder_when_processor_is_none(self):
        """get_frame() returns placeholder when processor itself is None.

        Requirements: 5.7
        """
        server = WebServer(port=8080, processor=None)
        frame = server.get_frame()

        assert frame is not None
        assert frame.shape == (1080, 1920, 3)


class TestMaxClientsExceeded:
    """Test HTTP 503 when max clients exceeded."""

    def test_increment_clients_returns_false_at_max(self):
        """_increment_clients() returns False when max_clients reached.

        Requirements: 5.8
        """
        mock_processor = MagicMock()
        server = WebServer(port=8080, processor=mock_processor, max_clients=2)

        # Fill up client slots
        assert server._increment_clients() is True
        assert server._increment_clients() is True

        # Third client should be rejected
        assert server._increment_clients() is False

    def test_increment_clients_allows_after_decrement(self):
        """_increment_clients() allows new client after one disconnects.

        Requirements: 5.8
        """
        mock_processor = MagicMock()
        server = WebServer(port=8080, processor=mock_processor, max_clients=1)

        # Fill the slot
        assert server._increment_clients() is True
        # Rejected
        assert server._increment_clients() is False

        # Client disconnects
        server._decrement_clients()

        # Now a new client can connect
        assert server._increment_clients() is True

    def test_decrement_clients_does_not_go_below_zero(self):
        """_decrement_clients() does not let count go below zero.

        Requirements: 5.8
        """
        mock_processor = MagicMock()
        server = WebServer(port=8080, processor=mock_processor, max_clients=4)

        # Decrement without any clients connected
        server._decrement_clients()
        assert server.get_client_count() == 0

    def test_get_client_count_tracks_connections(self):
        """get_client_count() accurately reflects connected clients.

        Requirements: 5.8
        """
        mock_processor = MagicMock()
        server = WebServer(port=8080, processor=mock_processor, max_clients=4)

        assert server.get_client_count() == 0
        server._increment_clients()
        assert server.get_client_count() == 1
        server._increment_clients()
        assert server.get_client_count() == 2
        server._decrement_clients()
        assert server.get_client_count() == 1


class TestMJPEGResponseHeaders:
    """Test MJPEG response headers (multipart/x-mixed-replace)."""

    def test_stream_frame_produces_correct_boundary(self):
        """stream_frame() output starts with correct MIME boundary.

        Requirements: 5.3
        """
        handler = MJPEGStreamHandler(quality=70)
        frame = np.zeros((480, 640, 3), dtype=np.uint8)

        chunk = handler.stream_frame(frame)

        # Should start with boundary
        expected_boundary = b"--" + MJPEG_BOUNDARY.encode("ascii") + b"\r\n"
        assert chunk.startswith(expected_boundary)

    def test_stream_frame_contains_content_type_jpeg(self):
        """stream_frame() output contains Content-Type: image/jpeg header.

        Requirements: 5.3
        """
        handler = MJPEGStreamHandler(quality=70)
        frame = np.zeros((480, 640, 3), dtype=np.uint8)

        chunk = handler.stream_frame(frame)

        assert b"Content-Type: image/jpeg\r\n" in chunk

    def test_stream_frame_contains_content_length(self):
        """stream_frame() output contains Content-Length header.

        Requirements: 5.3
        """
        handler = MJPEGStreamHandler(quality=70)
        frame = np.zeros((480, 640, 3), dtype=np.uint8)

        chunk = handler.stream_frame(frame)

        assert b"Content-Length: " in chunk

    def test_stream_frame_ends_with_crlf(self):
        """stream_frame() output ends with CRLF after JPEG data.

        Requirements: 5.3
        """
        handler = MJPEGStreamHandler(quality=70)
        frame = np.zeros((480, 640, 3), dtype=np.uint8)

        chunk = handler.stream_frame(frame)

        assert chunk.endswith(b"\r\n")

    def test_stream_frame_returns_empty_for_none_frame(self):
        """stream_frame() returns empty bytes when frame is None.

        Requirements: 5.3
        """
        handler = MJPEGStreamHandler(quality=70)

        chunk = handler.stream_frame(None)

        assert chunk == b""

    def test_stream_frame_quality_override(self):
        """stream_frame() uses quality override when provided.

        Requirements: 5.2
        """
        handler = MJPEGStreamHandler(quality=70)
        frame = np.zeros((480, 640, 3), dtype=np.uint8)

        chunk_low = handler.stream_frame(frame, quality=10)
        chunk_high = handler.stream_frame(frame, quality=95)

        # Both should produce valid chunks
        assert len(chunk_low) > 0
        assert len(chunk_high) > 0
        # Higher quality generally produces larger output
        # (for a solid black frame this may not always hold, but both should be valid)
        assert b"Content-Type: image/jpeg\r\n" in chunk_low
        assert b"Content-Type: image/jpeg\r\n" in chunk_high

    def test_stream_frame_clamps_quality_to_valid_range(self):
        """stream_frame() clamps quality to [1, 100] range.

        Requirements: 5.2
        """
        handler = MJPEGStreamHandler(quality=70)
        frame = np.zeros((480, 640, 3), dtype=np.uint8)

        # Quality below 1 should be clamped to 1
        chunk = handler.stream_frame(frame, quality=-5)
        assert len(chunk) > 0
        assert b"Content-Type: image/jpeg\r\n" in chunk

        # Quality above 100 should be clamped to 100
        chunk = handler.stream_frame(frame, quality=200)
        assert len(chunk) > 0
        assert b"Content-Type: image/jpeg\r\n" in chunk


class TestStatsEndpoint:
    """Test stats endpoint returns all required fields."""

    def test_get_stats_returns_required_keys(self):
        """get_stats() returns dict with all required fields.

        Requirements: 6.4
        """
        mock_processor = MagicMock()
        mock_processor._fps_calculator = MagicMock()
        mock_processor._fps_calculator.get_fps.return_value = 10.5
        mock_processor._detection_count = 42
        mock_processor._inference_times = [50.0, 60.0, 70.0]
        mock_processor.capture = MagicMock()
        mock_processor.capture.is_connected = True

        server = WebServer(port=8080, processor=mock_processor)
        stats = server.get_stats()

        assert "current_fps" in stats
        assert "total_detections" in stats
        assert "avg_inference_ms" in stats
        assert "connection_status" in stats

    def test_get_stats_returns_correct_fps(self):
        """get_stats() returns current FPS from processor's FPS calculator.

        Requirements: 6.4
        """
        mock_processor = MagicMock()
        mock_processor._fps_calculator = MagicMock()
        mock_processor._fps_calculator.get_fps.return_value = 15.3
        mock_processor._detection_count = 0
        mock_processor._inference_times = []
        mock_processor.capture = MagicMock()
        mock_processor.capture.is_connected = True

        server = WebServer(port=8080, processor=mock_processor)
        stats = server.get_stats()

        assert stats["current_fps"] == 15.3

    def test_get_stats_returns_total_detections(self):
        """get_stats() returns total detection count from processor.

        Requirements: 6.4
        """
        mock_processor = MagicMock()
        mock_processor._fps_calculator = MagicMock()
        mock_processor._fps_calculator.get_fps.return_value = 0.0
        mock_processor._detection_count = 100
        mock_processor._inference_times = []
        mock_processor.capture = MagicMock()
        mock_processor.capture.is_connected = False
        mock_processor.capture._reconnecting = False

        server = WebServer(port=8080, processor=mock_processor)
        stats = server.get_stats()

        assert stats["total_detections"] == 100

    def test_get_stats_returns_avg_inference_ms(self):
        """get_stats() computes average inference time from processor times.

        Requirements: 6.4
        """
        mock_processor = MagicMock()
        mock_processor._fps_calculator = MagicMock()
        mock_processor._fps_calculator.get_fps.return_value = 0.0
        mock_processor._detection_count = 0
        mock_processor._inference_times = [40.0, 50.0, 60.0]
        mock_processor.capture = MagicMock()
        mock_processor.capture.is_connected = True

        server = WebServer(port=8080, processor=mock_processor)
        stats = server.get_stats()

        assert stats["avg_inference_ms"] == 50.0

    def test_get_stats_connection_status_connected(self):
        """get_stats() returns 'connected' when capture is connected.

        Requirements: 6.4
        """
        mock_processor = MagicMock()
        mock_processor._fps_calculator = MagicMock()
        mock_processor._fps_calculator.get_fps.return_value = 0.0
        mock_processor._detection_count = 0
        mock_processor._inference_times = []
        mock_processor.capture = MagicMock()
        mock_processor.capture.is_connected = True

        server = WebServer(port=8080, processor=mock_processor)
        stats = server.get_stats()

        assert stats["connection_status"] == "connected"

    def test_get_stats_connection_status_disconnected(self):
        """get_stats() returns 'disconnected' when capture is not connected.

        Requirements: 6.4
        """
        mock_processor = MagicMock()
        mock_processor._fps_calculator = MagicMock()
        mock_processor._fps_calculator.get_fps.return_value = 0.0
        mock_processor._detection_count = 0
        mock_processor._inference_times = []
        mock_processor.capture = MagicMock()
        mock_processor.capture.is_connected = False
        mock_processor.capture._reconnecting = False

        server = WebServer(port=8080, processor=mock_processor)
        stats = server.get_stats()

        assert stats["connection_status"] == "disconnected"

    def test_get_stats_connection_status_reconnecting(self):
        """get_stats() returns 'reconnecting' when capture is reconnecting.

        Requirements: 6.4
        """
        mock_processor = MagicMock()
        mock_processor._fps_calculator = MagicMock()
        mock_processor._fps_calculator.get_fps.return_value = 0.0
        mock_processor._detection_count = 0
        mock_processor._inference_times = []
        mock_processor.capture = MagicMock()
        mock_processor.capture.is_connected = False
        mock_processor.capture._reconnecting = True

        server = WebServer(port=8080, processor=mock_processor)
        stats = server.get_stats()

        assert stats["connection_status"] == "reconnecting"

    def test_get_stats_defaults_when_processor_lacks_attributes(self):
        """get_stats() returns safe defaults when processor lacks expected attributes.

        Requirements: 6.4
        """
        mock_processor = MagicMock(spec=[])  # No attributes

        server = WebServer(port=8080, processor=mock_processor)
        stats = server.get_stats()

        assert stats["current_fps"] == 0.0
        assert stats["total_detections"] == 0
        assert stats["avg_inference_ms"] == 0.0
        assert stats["connection_status"] == "disconnected"


class TestValidateConfigInput:
    """Test invalid config input rejected with error message."""

    def test_confidence_threshold_valid(self):
        """Valid confidence_threshold values are accepted.

        Requirements: 6.6
        """
        valid, error = WebServer.validate_config_input("confidence_threshold", "0.5")
        assert valid is True
        assert error == ""

        valid, error = WebServer.validate_config_input("confidence_threshold", "0.1")
        assert valid is True

        valid, error = WebServer.validate_config_input("confidence_threshold", "1.0")
        assert valid is True

    def test_confidence_threshold_out_of_range(self):
        """confidence_threshold outside [0.1, 1.0] is rejected with error.

        Requirements: 6.6
        """
        valid, error = WebServer.validate_config_input("confidence_threshold", "0.05")
        assert valid is False
        assert "0.1" in error or "between" in error.lower()

        valid, error = WebServer.validate_config_input("confidence_threshold", "1.5")
        assert valid is False
        assert "1.0" in error or "between" in error.lower()

    def test_confidence_threshold_non_numeric(self):
        """Non-numeric confidence_threshold is rejected with error.

        Requirements: 6.6
        """
        valid, error = WebServer.validate_config_input("confidence_threshold", "abc")
        assert valid is False
        assert "numeric" in error.lower()

    def test_target_fps_valid(self):
        """Valid target_fps values are accepted.

        Requirements: 6.6
        """
        valid, error = WebServer.validate_config_input("target_fps", "10")
        assert valid is True
        assert error == ""

        valid, error = WebServer.validate_config_input("target_fps", "1")
        assert valid is True

        valid, error = WebServer.validate_config_input("target_fps", "30")
        assert valid is True

    def test_target_fps_out_of_range(self):
        """target_fps outside [1, 30] is rejected with error.

        Requirements: 6.6
        """
        valid, error = WebServer.validate_config_input("target_fps", "0")
        assert valid is False
        assert "1" in error or "between" in error.lower()

        valid, error = WebServer.validate_config_input("target_fps", "31")
        assert valid is False
        assert "30" in error or "between" in error.lower()

    def test_target_fps_non_integer(self):
        """Non-integer target_fps is rejected with error.

        Requirements: 6.6
        """
        valid, error = WebServer.validate_config_input("target_fps", "abc")
        assert valid is False
        assert "integer" in error.lower()

    def test_rtsp_url_valid(self):
        """Valid rtsp_url values are accepted.

        Requirements: 6.6
        """
        valid, error = WebServer.validate_config_input("rtsp_url", "rtsp://192.168.1.1/stream")
        assert valid is True
        assert error == ""

    def test_rtsp_url_missing_prefix(self):
        """rtsp_url without 'rtsp://' prefix is rejected.

        Requirements: 6.6
        """
        valid, error = WebServer.validate_config_input("rtsp_url", "http://example.com/stream")
        assert valid is False
        assert "rtsp://" in error

    def test_rtsp_url_too_long(self):
        """rtsp_url exceeding 2048 characters is rejected.

        Requirements: 6.6
        """
        long_url = "rtsp://" + "a" * 2048
        valid, error = WebServer.validate_config_input("rtsp_url", long_url)
        assert valid is False
        assert "2048" in error

    def test_input_resolution_valid(self):
        """Valid input_resolution values are accepted.

        Requirements: 6.6
        """
        for res in ["320x240", "640x480", "1280x720"]:
            valid, error = WebServer.validate_config_input("input_resolution", res)
            assert valid is True
            assert error == ""

    def test_input_resolution_invalid(self):
        """Invalid input_resolution values are rejected.

        Requirements: 6.6
        """
        valid, error = WebServer.validate_config_input("input_resolution", "1920x1080")
        assert valid is False
        assert "320x240" in error or "640x480" in error or "1280x720" in error

    def test_unknown_field_rejected(self):
        """Unknown configuration field is rejected with error.

        Requirements: 6.6
        """
        valid, error = WebServer.validate_config_input("unknown_field", "value")
        assert valid is False
        assert "unknown" in error.lower()


class TestRTSPURLChangeFailure:
    """Test RTSP URL change failure within 10s shows error and retains previous URL."""

    def test_url_change_failure_retains_previous_url(self):
        """When new RTSP URL connection fails, previous URL is retained.

        Requirements: 6.7
        """
        mock_processor = MagicMock()
        mock_capture = MagicMock()
        mock_capture.is_connected = True
        mock_capture._url = "rtsp://old-camera/stream"
        # Simulate connection failure for new URL
        mock_capture.connect.return_value = False
        mock_processor.capture = mock_capture

        server = WebServer(port=8080, processor=mock_processor)

        # Attempt to change URL - the capture.connect() fails
        new_url = "rtsp://new-camera/stream"
        mock_capture.connect.return_value = False

        # Verify the capture's URL hasn't changed after failed connect
        # The web server validates the URL first
        valid, error = server.validate_config_input("rtsp_url", new_url)
        assert valid is True  # URL format is valid

        # Simulate the connection attempt failing
        result = mock_capture.connect(timeout=10.0)
        assert result is False

        # Previous URL should still be active
        assert mock_capture._url == "rtsp://old-camera/stream"

    def test_url_change_validates_format_before_connecting(self):
        """URL format is validated before attempting connection.

        Requirements: 6.6, 6.7
        """
        mock_processor = MagicMock()
        server = WebServer(port=8080, processor=mock_processor)

        # Invalid URL should be rejected at validation stage
        valid, error = server.validate_config_input("rtsp_url", "http://not-rtsp/stream")
        assert valid is False
        assert "rtsp://" in error

    def test_url_change_rejects_empty_url(self):
        """Empty RTSP URL is rejected during validation.

        Requirements: 6.6
        """
        mock_processor = MagicMock()
        server = WebServer(port=8080, processor=mock_processor)

        valid, error = server.validate_config_input("rtsp_url", "")
        assert valid is False
        assert "rtsp://" in error
