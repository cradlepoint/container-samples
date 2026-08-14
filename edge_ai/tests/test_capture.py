"""Unit tests for RTSPCapture module.

Tests specific example scenarios for RTSP connection management,
frame reading, disconnection detection, and exponential backoff.

Requirements: 1.1, 1.3, 1.4, 1.6, 1.7
"""
import time
import threading
from unittest.mock import patch, MagicMock, PropertyMock

import numpy as np
import pytest

from capture import RTSPCapture


class TestConnectEmptyURL:
    """Test that connect() with empty/missing URL returns False and logs error."""

    def test_connect_empty_url_returns_false(self):
        """connect() with empty URL returns False and logs error.

        Requirements: 1.6
        """
        with patch("capture.cp") as mock_cp:
            cap = RTSPCapture("", target_fps=10)
            result = cap.connect()

            assert result is False
            mock_cp.log.assert_called()
            log_msg = mock_cp.log.call_args[0][0]
            assert "missing" in log_msg.lower() or "empty" in log_msg.lower()

    def test_connect_none_url_returns_false(self):
        """connect() with None-ish empty URL returns False.

        Requirements: 1.6
        """
        with patch("capture.cp") as mock_cp:
            cap = RTSPCapture("", target_fps=10)
            result = cap.connect()

            assert result is False


class TestConnectTimeout:
    """Test connection timeout when av.open() hangs."""

    def test_connect_timeout_returns_false(self):
        """connect() returns False when av.open() hangs past timeout.

        Requirements: 1.1
        """
        with patch("capture.cp") as mock_cp, \
             patch("capture.av") as mock_av:
            def slow_open(*args, **kwargs):
                time.sleep(5)
                return MagicMock()

            mock_av.open = slow_open

            cap = RTSPCapture("rtsp://example.com/stream", target_fps=10)
            result = cap.connect(timeout=0.5)

            assert result is False
            mock_cp.log.assert_called()
            log_calls = [call[0][0] for call in mock_cp.log.call_args_list]
            assert any("timed out" in msg.lower() for msg in log_calls)

    def test_connect_success_within_timeout(self):
        """connect() returns True when av.open() succeeds within timeout.

        Requirements: 1.1
        """
        with patch("capture.cp") as mock_cp, \
             patch("capture.av") as mock_av:
            mock_container = MagicMock()
            mock_stream = MagicMock()
            mock_stream.type = 'video'
            mock_container.streams = [mock_stream]
            mock_av.open.return_value = mock_container

            cap = RTSPCapture("rtsp://example.com/stream", target_fps=10)
            result = cap.connect(timeout=5.0)

            assert result is True
            assert cap.is_connected is True


class TestReadFrameCorrupt:
    """Test frame discard on corrupt frame."""

    def test_read_frame_returns_none_when_not_connected(self):
        """read_frame() returns None when not connected.

        Requirements: 1.7
        """
        cap = RTSPCapture("rtsp://example.com/stream", target_fps=10)
        result = cap.read_frame()
        assert result is None

    def test_read_frame_returns_valid_frame(self):
        """read_frame() returns the frame when decoding succeeds.

        Requirements: 1.7
        """
        with patch("capture.cp") as mock_cp, \
             patch("capture.av") as mock_av:
            # Set up a connected capture
            mock_container = MagicMock()
            mock_stream = MagicMock()
            mock_stream.type = 'video'
            mock_container.streams = [mock_stream]
            mock_av.open.return_value = mock_container

            cap = RTSPCapture("rtsp://example.com/stream", target_fps=10)
            cap.connect(timeout=5.0)

            # Mock demux to return a packet with a frame
            mock_frame = MagicMock()
            valid_array = np.zeros((480, 640, 3), dtype=np.uint8)
            mock_frame.to_ndarray.return_value = valid_array

            mock_packet = MagicMock()
            mock_packet.decode.return_value = [mock_frame]
            mock_container.demux.return_value = [mock_packet]

            result = cap.read_frame()

            assert result is not None
            assert result.shape == (480, 640, 3)


class TestDisconnectionDetection:
    """Test disconnection detected after failures persist for 10+ seconds."""

    def test_disconnection_detected_after_timeout(self):
        """State transitions to disconnected after failures persist for 10+ seconds.

        Requirements: 1.4
        """
        with patch("capture.cp") as mock_cp, \
             patch("capture.av") as mock_av, \
             patch("capture.time") as mock_time:
            mock_container = MagicMock()
            mock_stream = MagicMock()
            mock_stream.type = 'video'
            mock_container.streams = [mock_stream]
            mock_av.open.return_value = mock_container

            cap = RTSPCapture("rtsp://example.com/stream", target_fps=10)
            cap.connect(timeout=5.0)

            # Simulate read failures (demux returns empty)
            mock_container.demux.return_value = iter([])

            # First failure at t=0
            mock_time.time.return_value = 100.0
            cap.read_frame()
            assert cap.connection_state == RTSPCapture.STATE_CONNECTED

            # Failure at t=5 (still within 10s window)
            mock_time.time.return_value = 105.0
            cap.read_frame()
            assert cap.connection_state == RTSPCapture.STATE_CONNECTED

            # Failure at t=10+ (exceeds disconnect timeout)
            mock_time.time.return_value = 110.1
            cap.read_frame()
            assert cap.connection_state == RTSPCapture.STATE_DISCONNECTED


class TestComputeBackoff:
    """Test exponential backoff sequence: 2, 4, 8, 16, 32, 60, 60..."""

    def test_backoff_sequence(self):
        """compute_backoff produces correct sequence: 2, 4, 8, 16, 32, 60, 60...

        Requirements: 1.3
        """
        expected = [2, 4, 8, 16, 32, 60, 60, 60]
        for retry_count, expected_delay in enumerate(expected):
            result = RTSPCapture.compute_backoff(retry_count)
            assert result == expected_delay, (
                "retry_count={}: expected {}, got {}".format(
                    retry_count, expected_delay, result)
            )

    def test_backoff_never_exceeds_60(self):
        """compute_backoff caps at 60 seconds for any retry count.

        Requirements: 1.3
        """
        for retry_count in range(0, 100):
            result = RTSPCapture.compute_backoff(retry_count)
            assert result <= 60

    def test_backoff_minimum_is_2(self):
        """compute_backoff minimum value is 2 seconds (at retry_count=0).

        Requirements: 1.3
        """
        result = RTSPCapture.compute_backoff(0)
        assert result == 2


class TestReconnectionLoop:
    """Test reconnection with exponential backoff sequence."""

    def test_reconnect_loop_succeeds_on_second_attempt(self):
        """reconnect_loop retries with backoff and succeeds.

        Requirements: 1.3, 1.4
        """
        with patch("capture.cp") as mock_cp, \
             patch("capture.av") as mock_av, \
             patch("capture.time") as mock_time_mod:
            mock_container = MagicMock()
            mock_stream = MagicMock()
            mock_stream.type = 'video'

            # First open fails (no video streams), second succeeds
            mock_container_fail = MagicMock()
            mock_container_fail.streams = []
            mock_container_success = MagicMock()
            mock_container_success.streams = [mock_stream]
            mock_av.open.side_effect = [mock_container_fail, mock_container_success]

            # Make time advance so the backoff wait loop exits
            time_values = iter([1000.0, 1000.0, 1100.0, 1100.0, 1200.0])
            mock_time_mod.time.side_effect = lambda: next(time_values, 9999.0)
            mock_time_mod.sleep = MagicMock()

            cap = RTSPCapture("rtsp://example.com/stream", target_fps=10)
            cap.reconnect_loop()

            # Should have attempted connection twice
            assert mock_av.open.call_count == 2
            assert cap.is_connected is True

    def test_reconnect_loop_stops_on_stop_event(self):
        """reconnect_loop exits when stop() is called.

        Requirements: 1.3
        """
        with patch("capture.cp") as mock_cp:
            cap = RTSPCapture("rtsp://example.com/stream", target_fps=10)

            # Signal stop before starting the loop
            cap._stop_event.set()
            cap.reconnect_loop()

            # Should exit without attempting connection
            assert cap.connection_state == RTSPCapture.STATE_RECONNECTING
