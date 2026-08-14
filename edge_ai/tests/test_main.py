"""Unit tests for main.py initialization.

Tests specific example scenarios for application entry point behavior:
- Startup with missing RTSP URL serves config page (web server only)
- Startup logs version, model name, RTSP URL, and threshold
- Model load failure exits non-zero

Validates: Requirements 8.2, 10.1, 7.7
"""
import sys
import threading
from unittest.mock import patch, MagicMock, call

import pytest


# We need to patch modules before importing main, so we use patch decorators
# targeting the main module's namespace.


class TestMissingRTSPURLServesConfigPage:
    """Test startup with missing RTSP URL serves config page.

    When config.rtsp_input_url is empty, main() starts WebServer with
    processor=None and doesn't start capture/processing.

    Validates: Requirement 8.2
    """

    @patch('main._shutdown_event')
    @patch('main.WebServer')
    @patch('main.ConfigLoader')
    @patch('main.cp')
    def test_missing_url_starts_web_server_only(
        self, mock_cp, mock_config_loader_cls, mock_web_server_cls, mock_shutdown
    ):
        """When RTSP URL is empty, main() starts WebServer without processor."""
        from main import main

        # Configure mock config loader to return config with empty URL
        mock_config = MagicMock()
        mock_config.rtsp_input_url = ""
        mock_config.confidence_threshold = 0.4
        mock_config.web_port = 8080
        mock_config.target_fps = 10
        mock_config_loader_cls.return_value.load_config.return_value = mock_config

        # Make shutdown event trigger immediately to exit the wait loop
        mock_shutdown.is_set.side_effect = [False, True]
        mock_shutdown.wait.return_value = None

        mock_web = MagicMock()
        mock_web_server_cls.return_value = mock_web

        main()

        # WebServer should be created with processor=None
        mock_web_server_cls.assert_called_once_with(mock_config.web_port, None)
        mock_web.start.assert_called_once()
        mock_web.stop.assert_called_once()

    @patch('main._shutdown_event')
    @patch('main.WebServer')
    @patch('main.ConfigLoader')
    @patch('main.cp')
    def test_missing_url_does_not_create_capture_or_engine(
        self, mock_cp, mock_config_loader_cls, mock_web_server_cls, mock_shutdown
    ):
        """When RTSP URL is empty, RTSPCapture and InferenceEngine are not created."""
        from main import main

        mock_config = MagicMock()
        mock_config.rtsp_input_url = ""
        mock_config.confidence_threshold = 0.4
        mock_config.web_port = 8080
        mock_config.target_fps = 10
        mock_config_loader_cls.return_value.load_config.return_value = mock_config

        mock_shutdown.is_set.side_effect = [False, True]
        mock_shutdown.wait.return_value = None

        with patch('main.RTSPCapture') as mock_capture_cls, \
             patch('main.InferenceEngine') as mock_engine_cls:
            main()

            mock_capture_cls.assert_not_called()
            mock_engine_cls.assert_not_called()

    @patch('main._shutdown_event')
    @patch('main.WebServer')
    @patch('main.ConfigLoader')
    @patch('main.cp')
    def test_missing_url_logs_warning(
        self, mock_cp, mock_config_loader_cls, mock_web_server_cls, mock_shutdown
    ):
        """When RTSP URL is empty, a warning is logged via cp.log()."""
        from main import main

        mock_config = MagicMock()
        mock_config.rtsp_input_url = ""
        mock_config.confidence_threshold = 0.4
        mock_config.web_port = 8080
        mock_config.target_fps = 10
        mock_config_loader_cls.return_value.load_config.return_value = mock_config

        mock_shutdown.is_set.side_effect = [False, True]
        mock_shutdown.wait.return_value = None

        main()

        # Check that cp.log was called with a message about missing RTSP URL
        log_calls = [str(c) for c in mock_cp.log.call_args_list]
        assert any('no rtsp url' in c.lower() or 'not configured' in c.lower()
                   for c in log_calls), (
            "Expected warning log about missing RTSP URL, got: {}".format(log_calls)
        )


class TestStartupLogsVersionModelURLThreshold:
    """Test startup logs version, model name, RTSP URL, and threshold.

    Validates: Requirement 10.1
    """

    @patch('main._shutdown_event')
    @patch('main.WebServer')
    @patch('main.FrameProcessor')
    @patch('main.AnnotationRenderer')
    @patch('main.RTSPCapture')
    @patch('main.InferenceEngine')
    @patch('main.ConfigLoader')
    @patch('main.cp')
    def test_logs_version_on_startup(
        self, mock_cp, mock_config_loader_cls, mock_engine_cls,
        mock_capture_cls, mock_renderer_cls, mock_processor_cls,
        mock_web_server_cls, mock_shutdown
    ):
        """main() logs the application version via cp.log()."""
        from main import main, APP_VERSION

        mock_config = MagicMock()
        mock_config.rtsp_input_url = "rtsp://192.168.1.100/stream"
        mock_config.confidence_threshold = 0.5
        mock_config.web_port = 8080
        mock_config.target_fps = 10
        mock_config_loader_cls.return_value.load_config.return_value = mock_config

        mock_engine = MagicMock()
        mock_engine.load_model.return_value = True
        mock_engine_cls.return_value = mock_engine

        mock_capture = MagicMock()
        mock_capture.connect.return_value = True
        mock_capture_cls.return_value = mock_capture

        mock_shutdown.is_set.side_effect = [False, True]
        mock_shutdown.wait.return_value = None

        main()

        log_calls = [str(c) for c in mock_cp.log.call_args_list]
        assert any(APP_VERSION in c for c in log_calls), (
            "Expected version '{}' in log calls, got: {}".format(APP_VERSION, log_calls)
        )

    @patch('main._shutdown_event')
    @patch('main.WebServer')
    @patch('main.FrameProcessor')
    @patch('main.AnnotationRenderer')
    @patch('main.RTSPCapture')
    @patch('main.InferenceEngine')
    @patch('main.ConfigLoader')
    @patch('main.cp')
    def test_logs_model_name_on_startup(
        self, mock_cp, mock_config_loader_cls, mock_engine_cls,
        mock_capture_cls, mock_renderer_cls, mock_processor_cls,
        mock_web_server_cls, mock_shutdown
    ):
        """main() logs the model name via cp.log()."""
        from main import main

        mock_config = MagicMock()
        mock_config.rtsp_input_url = "rtsp://192.168.1.100/stream"
        mock_config.confidence_threshold = 0.5
        mock_config.web_port = 8080
        mock_config.target_fps = 10
        mock_config_loader_cls.return_value.load_config.return_value = mock_config

        mock_engine = MagicMock()
        mock_engine.load_model.return_value = True
        mock_engine_cls.return_value = mock_engine

        mock_capture = MagicMock()
        mock_capture.connect.return_value = True
        mock_capture_cls.return_value = mock_capture

        mock_shutdown.is_set.side_effect = [False, True]
        mock_shutdown.wait.return_value = None

        main()

        log_calls = [str(c) for c in mock_cp.log.call_args_list]
        assert any('ssd_mobilenet_v2' in c.lower() for c in log_calls), (
            "Expected model name in log calls, got: {}".format(log_calls)
        )

    @patch('main._shutdown_event')
    @patch('main.WebServer')
    @patch('main.FrameProcessor')
    @patch('main.AnnotationRenderer')
    @patch('main.RTSPCapture')
    @patch('main.InferenceEngine')
    @patch('main.ConfigLoader')
    @patch('main.cp')
    def test_logs_rtsp_url_on_startup(
        self, mock_cp, mock_config_loader_cls, mock_engine_cls,
        mock_capture_cls, mock_renderer_cls, mock_processor_cls,
        mock_web_server_cls, mock_shutdown
    ):
        """main() logs the RTSP URL via cp.log()."""
        from main import main

        mock_config = MagicMock()
        mock_config.rtsp_input_url = "rtsp://10.0.0.5/live"
        mock_config.confidence_threshold = 0.4
        mock_config.web_port = 8080
        mock_config.target_fps = 10
        mock_config_loader_cls.return_value.load_config.return_value = mock_config

        mock_engine = MagicMock()
        mock_engine.load_model.return_value = True
        mock_engine_cls.return_value = mock_engine

        mock_capture = MagicMock()
        mock_capture.connect.return_value = True
        mock_capture_cls.return_value = mock_capture

        mock_shutdown.is_set.side_effect = [False, True]
        mock_shutdown.wait.return_value = None

        main()

        log_calls = [str(c) for c in mock_cp.log.call_args_list]
        assert any('rtsp://10.0.0.5/live' in c for c in log_calls), (
            "Expected RTSP URL in log calls, got: {}".format(log_calls)
        )

    @patch('main._shutdown_event')
    @patch('main.WebServer')
    @patch('main.FrameProcessor')
    @patch('main.AnnotationRenderer')
    @patch('main.RTSPCapture')
    @patch('main.InferenceEngine')
    @patch('main.ConfigLoader')
    @patch('main.cp')
    def test_logs_threshold_on_startup(
        self, mock_cp, mock_config_loader_cls, mock_engine_cls,
        mock_capture_cls, mock_renderer_cls, mock_processor_cls,
        mock_web_server_cls, mock_shutdown
    ):
        """main() logs the confidence threshold via cp.log()."""
        from main import main

        mock_config = MagicMock()
        mock_config.rtsp_input_url = "rtsp://192.168.1.100/stream"
        mock_config.confidence_threshold = 0.65
        mock_config.web_port = 8080
        mock_config.target_fps = 10
        mock_config_loader_cls.return_value.load_config.return_value = mock_config

        mock_engine = MagicMock()
        mock_engine.load_model.return_value = True
        mock_engine_cls.return_value = mock_engine

        mock_capture = MagicMock()
        mock_capture.connect.return_value = True
        mock_capture_cls.return_value = mock_capture

        mock_shutdown.is_set.side_effect = [False, True]
        mock_shutdown.wait.return_value = None

        main()

        log_calls = [str(c) for c in mock_cp.log.call_args_list]
        assert any('0.65' in c for c in log_calls), (
            "Expected threshold '0.65' in log calls, got: {}".format(log_calls)
        )


class TestModelLoadFailureExitsNonZero:
    """Test model load failure exits non-zero.

    When engine.load_model() returns False, main() calls sys.exit(1).

    Validates: Requirement 7.7
    """

    @patch('main._shutdown_event')
    @patch('main.WebServer')
    @patch('main.InferenceEngine')
    @patch('main.ConfigLoader')
    @patch('main.cp')
    def test_model_load_failure_calls_sys_exit(
        self, mock_cp, mock_config_loader_cls, mock_engine_cls,
        mock_web_server_cls, mock_shutdown
    ):
        """When load_model() returns False, main() exits with code 1."""
        from main import main

        mock_config = MagicMock()
        mock_config.rtsp_input_url = "rtsp://192.168.1.100/stream"
        mock_config.confidence_threshold = 0.4
        mock_config.web_port = 8080
        mock_config.target_fps = 10
        mock_config_loader_cls.return_value.load_config.return_value = mock_config

        mock_engine = MagicMock()
        mock_engine.load_model.return_value = False
        mock_engine_cls.return_value = mock_engine

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1

    @patch('main._shutdown_event')
    @patch('main.WebServer')
    @patch('main.InferenceEngine')
    @patch('main.ConfigLoader')
    @patch('main.cp')
    def test_model_load_failure_logs_error(
        self, mock_cp, mock_config_loader_cls, mock_engine_cls,
        mock_web_server_cls, mock_shutdown
    ):
        """When load_model() returns False, an error is logged before exit."""
        from main import main

        mock_config = MagicMock()
        mock_config.rtsp_input_url = "rtsp://192.168.1.100/stream"
        mock_config.confidence_threshold = 0.4
        mock_config.web_port = 8080
        mock_config.target_fps = 10
        mock_config_loader_cls.return_value.load_config.return_value = mock_config

        mock_engine = MagicMock()
        mock_engine.load_model.return_value = False
        mock_engine_cls.return_value = mock_engine

        with pytest.raises(SystemExit):
            main()

        log_calls = [str(c) for c in mock_cp.log.call_args_list]
        assert any('failed' in c.lower() and 'model' in c.lower()
                   for c in log_calls), (
            "Expected error log about model load failure, got: {}".format(log_calls)
        )

    @patch('main._shutdown_event')
    @patch('main.WebServer')
    @patch('main.InferenceEngine')
    @patch('main.ConfigLoader')
    @patch('main.cp')
    def test_model_load_failure_does_not_start_web_server(
        self, mock_cp, mock_config_loader_cls, mock_engine_cls,
        mock_web_server_cls, mock_shutdown
    ):
        """When load_model() fails, WebServer is not started."""
        from main import main

        mock_config = MagicMock()
        mock_config.rtsp_input_url = "rtsp://192.168.1.100/stream"
        mock_config.confidence_threshold = 0.4
        mock_config.web_port = 8080
        mock_config.target_fps = 10
        mock_config_loader_cls.return_value.load_config.return_value = mock_config

        mock_engine = MagicMock()
        mock_engine.load_model.return_value = False
        mock_engine_cls.return_value = mock_engine

        with pytest.raises(SystemExit):
            main()

        # WebServer should not have been instantiated for the full pipeline
        # (it may be instantiated in the no-URL path, but not here)
        mock_web_server_cls.return_value.start.assert_not_called()
