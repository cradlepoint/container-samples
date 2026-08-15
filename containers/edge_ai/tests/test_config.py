"""Unit tests for ConfigLoader.

Tests specific example scenarios for configuration loading:
- Missing RTSP URL logs warning and returns empty string
- Invalid config values fall back to defaults
- cp.get_appdata() returns None for missing fields — defaults applied

Validates: Requirements 8.2, 8.3, 8.5
"""
import sys
from unittest.mock import patch, call

import pytest

from src.config import ConfigLoader


class TestConfigLoaderMissingRtspUrl:
    """Test startup with missing RTSP URL logs warning and returns config indicating no URL.

    Validates: Requirement 8.2
    """

    @patch('cp.put_appdata')
    @patch('cp.log')
    @patch('cp.get_appdata')
    def test_missing_rtsp_url_returns_empty_string(self, mock_get_appdata, mock_log, mock_put_appdata):
        """When rtsp_input_url is None, AppConfig.rtsp_input_url should be ''."""
        mock_get_appdata.side_effect = lambda field: {
            'rtsp_input_url': None,
            'confidence_threshold': '0.6',
            'web_port': '9090',
            'target_fps': '15',
        }.get(field)

        loader = ConfigLoader()
        config = loader.load_config()

        assert type(config).__name__ == 'AppConfig'
        assert config.rtsp_input_url == 'rtsp://192.168.0.33:8554/stream'

    @patch('cp.put_appdata')
    @patch('cp.log')
    @patch('cp.get_appdata')
    def test_missing_rtsp_url_logs_creation(self, mock_get_appdata, mock_log, mock_put_appdata):
        """When rtsp_input_url is None, a log about creating the default is emitted."""
        mock_get_appdata.side_effect = lambda field: {
            'rtsp_input_url': None,
            'confidence_threshold': '0.5',
            'web_port': '8080',
            'target_fps': '10',
        }.get(field)

        loader = ConfigLoader()
        loader.load_config()

        # Verify cp.log was called about creating the appdata entry
        log_calls = [str(c) for c in mock_log.call_args_list]
        assert any('rtsp_input_url' in c and 'created' in c.lower()
                   for c in log_calls), (
            "Expected a log about creating rtsp_input_url appdata, "
            "got: {}".format(log_calls)
        )

    @patch('cp.put_appdata')
    @patch('cp.log')
    @patch('cp.get_appdata')
    def test_empty_rtsp_url_returns_empty_string(self, mock_get_appdata, mock_log, mock_put_appdata):
        """When rtsp_input_url is empty string, AppConfig.rtsp_input_url should be ''."""
        mock_get_appdata.side_effect = lambda field: {
            'rtsp_input_url': '',
            'confidence_threshold': None,
            'web_port': None,
            'target_fps': None,
        }.get(field)

        loader = ConfigLoader()
        config = loader.load_config()

        assert config.rtsp_input_url == ''

    @patch('cp.put_appdata')
    @patch('cp.log')
    @patch('cp.get_appdata')
    def test_missing_rtsp_url_other_fields_still_valid(self, mock_get_appdata, mock_log, mock_put_appdata):
        """Other valid fields should still be loaded correctly when RTSP URL is missing."""
        mock_get_appdata.side_effect = lambda field: {
            'rtsp_input_url': None,
            'confidence_threshold': '0.7',
            'web_port': '3000',
            'target_fps': '20',
        }.get(field)

        loader = ConfigLoader()
        config = loader.load_config()

        assert config.rtsp_input_url == 'rtsp://192.168.0.33:8554/stream'
        assert config.confidence_threshold == 0.7
        assert config.web_port == 3000
        assert config.target_fps == 20


class TestConfigLoaderInvalidValues:
    """Test startup with invalid config values falls back to defaults.

    Validates: Requirement 8.5
    """

    @patch('cp.put_appdata')
    @patch('cp.log')
    @patch('cp.get_appdata')
    def test_invalid_confidence_threshold_uses_default(self, mock_get_appdata, mock_log, mock_put_appdata):
        """Non-numeric confidence_threshold falls back to default 0.4."""
        mock_get_appdata.side_effect = lambda field: {
            'rtsp_input_url': 'rtsp://camera.local/stream',
            'confidence_threshold': 'abc',
            'web_port': '8080',
            'target_fps': '10',
        }.get(field)

        loader = ConfigLoader()
        config = loader.load_config()

        assert config.confidence_threshold == 0.35

    @patch('cp.put_appdata')
    @patch('cp.log')
    @patch('cp.get_appdata')
    def test_invalid_confidence_threshold_logs_error(self, mock_get_appdata, mock_log, mock_put_appdata):
        """Non-numeric confidence_threshold logs an error via cp.log."""
        mock_get_appdata.side_effect = lambda field: {
            'rtsp_input_url': 'rtsp://camera.local/stream',
            'confidence_threshold': 'abc',
            'web_port': '8080',
            'target_fps': '10',
        }.get(field)

        loader = ConfigLoader()
        loader.load_config()

        # Verify an error was logged about confidence_threshold
        log_calls = [str(c) for c in mock_log.call_args_list]
        assert any('confidence_threshold' in c for c in log_calls), (
            "Expected error log about confidence_threshold, got: {}".format(log_calls)
        )

    @patch('cp.put_appdata')
    @patch('cp.log')
    @patch('cp.get_appdata')
    def test_out_of_range_confidence_threshold_uses_default(self, mock_get_appdata, mock_log, mock_put_appdata):
        """confidence_threshold > 1.0 falls back to default 0.4."""
        mock_get_appdata.side_effect = lambda field: {
            'rtsp_input_url': 'rtsp://camera.local/stream',
            'confidence_threshold': '2.5',
            'web_port': '8080',
            'target_fps': '10',
        }.get(field)

        loader = ConfigLoader()
        config = loader.load_config()

        assert config.confidence_threshold == 0.35

    @patch('cp.put_appdata')
    @patch('cp.log')
    @patch('cp.get_appdata')
    def test_invalid_web_port_uses_default(self, mock_get_appdata, mock_log, mock_put_appdata):
        """web_port out of range [1024, 65535] falls back to default 8080."""
        mock_get_appdata.side_effect = lambda field: {
            'rtsp_input_url': 'rtsp://camera.local/stream',
            'confidence_threshold': '0.5',
            'web_port': '99999',
            'target_fps': '10',
        }.get(field)

        loader = ConfigLoader()
        config = loader.load_config()

        assert config.web_port == 8080

    @patch('cp.put_appdata')
    @patch('cp.log')
    @patch('cp.get_appdata')
    def test_invalid_web_port_logs_error(self, mock_get_appdata, mock_log, mock_put_appdata):
        """web_port out of range logs an error via cp.log."""
        mock_get_appdata.side_effect = lambda field: {
            'rtsp_input_url': 'rtsp://camera.local/stream',
            'confidence_threshold': '0.5',
            'web_port': '99999',
            'target_fps': '10',
        }.get(field)

        loader = ConfigLoader()
        loader.load_config()

        log_calls = [str(c) for c in mock_log.call_args_list]
        assert any('web_port' in c for c in log_calls), (
            "Expected error log about web_port, got: {}".format(log_calls)
        )

    @patch('cp.put_appdata')
    @patch('cp.log')
    @patch('cp.get_appdata')
    def test_non_numeric_web_port_uses_default(self, mock_get_appdata, mock_log, mock_put_appdata):
        """Non-numeric web_port falls back to default 8080."""
        mock_get_appdata.side_effect = lambda field: {
            'rtsp_input_url': 'rtsp://camera.local/stream',
            'confidence_threshold': '0.5',
            'web_port': 'not_a_port',
            'target_fps': '10',
        }.get(field)

        loader = ConfigLoader()
        config = loader.load_config()

        assert config.web_port == 8080

    @patch('cp.put_appdata')
    @patch('cp.log')
    @patch('cp.get_appdata')
    def test_invalid_target_fps_uses_default(self, mock_get_appdata, mock_log, mock_put_appdata):
        """target_fps out of range [1, 60] falls back to default 10."""
        mock_get_appdata.side_effect = lambda field: {
            'rtsp_input_url': 'rtsp://camera.local/stream',
            'confidence_threshold': '0.5',
            'web_port': '8080',
            'target_fps': '100',
        }.get(field)

        loader = ConfigLoader()
        config = loader.load_config()

        assert config.target_fps == 10

    @patch('cp.put_appdata')
    @patch('cp.log')
    @patch('cp.get_appdata')
    def test_all_invalid_values_use_all_defaults(self, mock_get_appdata, mock_log, mock_put_appdata):
        """When all optional values are invalid, all defaults are applied."""
        mock_get_appdata.side_effect = lambda field: {
            'rtsp_input_url': 'not-an-rtsp-url',
            'confidence_threshold': 'abc',
            'web_port': '99999',
            'target_fps': '-5',
        }.get(field)

        loader = ConfigLoader()
        config = loader.load_config()

        assert config.rtsp_input_url == ''
        assert config.confidence_threshold == 0.35
        assert config.web_port == 8080
        assert config.target_fps == 10


class TestConfigLoaderMissingFields:
    """Test cp.get_appdata() returns None for missing fields — defaults applied.

    Validates: Requirement 8.3
    """

    @patch('cp.put_appdata')
    @patch('cp.log')
    @patch('cp.get_appdata')
    def test_all_optional_fields_none_uses_defaults(self, mock_get_appdata, mock_log, mock_put_appdata):
        """When all optional fields return None, defaults are applied."""
        mock_get_appdata.side_effect = lambda field: {
            'rtsp_input_url': 'rtsp://192.168.1.100/live',
            'confidence_threshold': None,
            'web_port': None,
            'target_fps': None,
        }.get(field)

        loader = ConfigLoader()
        config = loader.load_config()

        assert config.rtsp_input_url == 'rtsp://192.168.1.100/live'
        assert config.confidence_threshold == 0.35
        assert config.web_port == 8080
        assert config.target_fps == 10

    @patch('cp.put_appdata')
    @patch('cp.log')
    @patch('cp.get_appdata')
    def test_confidence_threshold_none_uses_default(self, mock_get_appdata, mock_log, mock_put_appdata):
        """When confidence_threshold is None, default 0.4 is used."""
        mock_get_appdata.side_effect = lambda field: {
            'rtsp_input_url': 'rtsp://camera/stream1',
            'confidence_threshold': None,
            'web_port': '9000',
            'target_fps': '15',
        }.get(field)

        loader = ConfigLoader()
        config = loader.load_config()

        assert config.confidence_threshold == 0.35
        assert config.web_port == 9000
        assert config.target_fps == 15

    @patch('cp.put_appdata')
    @patch('cp.log')
    @patch('cp.get_appdata')
    def test_web_port_none_uses_default(self, mock_get_appdata, mock_log, mock_put_appdata):
        """When web_port is None, default 8080 is used."""
        mock_get_appdata.side_effect = lambda field: {
            'rtsp_input_url': 'rtsp://camera/stream1',
            'confidence_threshold': '0.6',
            'web_port': None,
            'target_fps': '20',
        }.get(field)

        loader = ConfigLoader()
        config = loader.load_config()

        assert config.confidence_threshold == 0.6
        assert config.web_port == 8080
        assert config.target_fps == 20

    @patch('cp.put_appdata')
    @patch('cp.log')
    @patch('cp.get_appdata')
    def test_target_fps_none_uses_default(self, mock_get_appdata, mock_log, mock_put_appdata):
        """When target_fps is None, default 10 is used."""
        mock_get_appdata.side_effect = lambda field: {
            'rtsp_input_url': 'rtsp://camera/stream1',
            'confidence_threshold': '0.8',
            'web_port': '4000',
            'target_fps': None,
        }.get(field)

        loader = ConfigLoader()
        config = loader.load_config()

        assert config.confidence_threshold == 0.8
        assert config.web_port == 4000
        assert config.target_fps == 10

    @patch('cp.put_appdata')
    @patch('cp.log')
    @patch('cp.get_appdata')
    def test_all_fields_none_uses_all_defaults(self, mock_get_appdata, mock_log, mock_put_appdata):
        """When all fields return None (including rtsp_input_url), all defaults are applied."""
        mock_get_appdata.return_value = None

        loader = ConfigLoader()
        config = loader.load_config()

        assert config.rtsp_input_url == 'rtsp://192.168.0.33:8554/stream'
        assert config.confidence_threshold == 0.35
        assert config.web_port == 8080
        assert config.target_fps == 10


# --- Property-Based Tests for Configuration Loading with Defaults (Property 9) ---

from hypothesis import given, settings, strategies as st


# Strategies for valid optional field values (returned as strings from appdata)
_valid_confidence_threshold_st = st.floats(
    min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False
).map(lambda x: str(x))

_valid_web_port_st = st.integers(
    min_value=1024, max_value=65535
).map(lambda x: str(x))

_valid_target_fps_st = st.integers(
    min_value=1, max_value=60
).map(lambda x: str(x))

_valid_rtsp_url_st = st.text(
    alphabet=st.characters(whitelist_categories=('L', 'N', 'P', 'S'),
                           blacklist_characters='\x00'),
    min_size=1, max_size=100
).map(lambda x: 'rtsp://' + x)

# Strategy that produces None (missing) or a valid value for each optional field
_optional_confidence_st = st.one_of(st.none(), _valid_confidence_threshold_st)
_optional_web_port_st = st.one_of(st.none(), _valid_web_port_st)
_optional_target_fps_st = st.one_of(st.none(), _valid_target_fps_st)


@settings(max_examples=100)
@given(
    rtsp_url=_valid_rtsp_url_st,
    confidence=_optional_confidence_st,
    web_port=_optional_web_port_st,
    target_fps=_optional_target_fps_st,
)
def test_config_loading_defaults(rtsp_url, confidence, web_port, target_fps):
    """Property 9: Configuration Loading with Defaults.

    For any appdata dict with arbitrary subset of optional fields missing,
    loaded config uses correct defaults:
    - Missing confidence_threshold -> 0.4
    - Missing web_port -> 8080
    - Missing target_fps -> 10
    Present and valid fields retain their appdata values.

    **Validates: Requirements 8.1, 8.3**
    """
    appdata_values = {
        'rtsp_input_url': rtsp_url,
        'confidence_threshold': confidence,
        'web_port': web_port,
        'target_fps': target_fps,
    }

    def mock_get_appdata(field_name):
        return appdata_values.get(field_name)

    loader = ConfigLoader()

    with patch('cp.get_appdata', side_effect=mock_get_appdata), \
         patch('cp.put_appdata'), \
         patch('cp.log'):
        config = loader.load_config()

    # rtsp_input_url: valid URL should be retained
    assert config.rtsp_input_url == rtsp_url

    # confidence_threshold: if missing (None), default 0.4; if present and valid, retain value
    if confidence is None:
        assert config.confidence_threshold == 0.35
    else:
        assert config.confidence_threshold == float(confidence)

    # web_port: if missing (None), default 8080; if present and valid, retain value
    if web_port is None:
        assert config.web_port == 8080
    else:
        assert config.web_port == int(web_port)

    # target_fps: if missing (None), default 10; if present and valid, retain value
    if target_fps is None:
        assert config.target_fps == 10
    else:
        assert config.target_fps == int(target_fps)
