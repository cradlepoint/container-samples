"""Property-based tests for configuration validation.

Tests Property 8: Configuration Validation.
For any configuration value, validate acceptance/rejection against defined ranges:
- confidence_threshold is valid iff numeric and in [0.0, 1.0]
- web_port is valid iff integer in [1024, 65535]
- target_fps is valid iff integer in [1, 60]
- rtsp_input_url is valid iff starts with "rtsp://" and length <= 2048

**Validates: Requirements 5.1, 5.2, 6.5, 6.6, 8.4**
"""
import sys
from unittest.mock import MagicMock, patch

from hypothesis import given, settings, strategies as st

from src.config import ConfigLoader


# --- Strategies ---

def valid_confidence_threshold_strategy():
    """Generate valid confidence_threshold values as strings: numeric in [0.0, 1.0]."""
    return st.floats(
        min_value=0.0, max_value=1.0,
        allow_nan=False, allow_infinity=False
    ).map(str)


def invalid_confidence_threshold_strategy():
    """Generate invalid confidence_threshold values."""
    return st.one_of(
        # Numeric but below 0.0
        st.floats(max_value=-0.001, allow_nan=False, allow_infinity=False)
        .filter(lambda x: -1e10 <= x)
        .map(str),
        # Numeric but above 1.0
        st.floats(min_value=1.001, allow_nan=False, allow_infinity=False)
        .filter(lambda x: x <= 1e10)
        .map(str),
        # Non-numeric strings
        st.text(min_size=1).filter(lambda s: not _is_numeric(s)),
    )


def valid_web_port_strategy():
    """Generate valid web_port values as strings: integer in [1024, 65535]."""
    return st.integers(min_value=1024, max_value=65535).map(str)


def invalid_web_port_strategy():
    """Generate invalid web_port values."""
    return st.one_of(
        # Integer below 1024
        st.integers(min_value=0, max_value=1023).map(str),
        # Integer above 65535
        st.integers(min_value=65536, max_value=100000).map(str),
        # Non-integer strings
        st.text(min_size=1).filter(lambda s: not _is_integer(s)),
        # Float strings (not valid integers)
        st.floats(min_value=1024.1, max_value=65535.9,
                  allow_nan=False, allow_infinity=False)
        .filter(lambda x: x != int(x))
        .map(str),
    )


def valid_target_fps_strategy():
    """Generate valid target_fps values as strings: integer in [1, 60]."""
    return st.integers(min_value=1, max_value=60).map(str)


def invalid_target_fps_strategy():
    """Generate invalid target_fps values."""
    return st.one_of(
        # Integer below 1
        st.integers(min_value=-100, max_value=0).map(str),
        # Integer above 60
        st.integers(min_value=61, max_value=1000).map(str),
        # Non-integer strings
        st.text(min_size=1).filter(lambda s: not _is_integer(s)),
    )


def valid_rtsp_url_strategy():
    """Generate valid rtsp_input_url values: starts with 'rtsp://' and length <= 2048."""
    # "rtsp://" is 7 chars, so path can be up to 2041 chars
    return st.text(
        alphabet=st.characters(whitelist_categories=('L', 'N', 'P', 'S'),
                               blacklist_characters='\x00'),
        min_size=1,
        max_size=2041
    ).map(lambda path: 'rtsp://' + path)


def invalid_rtsp_url_strategy():
    """Generate invalid rtsp_input_url values."""
    return st.one_of(
        # Does not start with "rtsp://"
        st.text(min_size=1, max_size=100).filter(
            lambda s: not s.startswith('rtsp://')
        ),
        # Starts with "rtsp://" but exceeds 2048 chars
        st.text(
            alphabet=st.characters(whitelist_categories=('L', 'N'),
                                   blacklist_characters='\x00'),
            min_size=2042,
            max_size=2100
        ).map(lambda path: 'rtsp://' + path),
    )


def valid_jpeg_quality_strategy():
    """Generate valid jpeg_quality values as strings: integer in [1, 100]."""
    return st.integers(min_value=1, max_value=100).map(str)


def invalid_jpeg_quality_strategy():
    """Generate invalid jpeg_quality values."""
    return st.one_of(
        # Integer below 1
        st.integers(min_value=-100, max_value=0).map(str),
        # Integer above 100
        st.integers(min_value=101, max_value=1000).map(str),
        # Non-integer strings
        st.text(min_size=1).filter(lambda s: not _is_integer(s)),
    )


# --- Helper functions ---

def _is_numeric(s):
    """Check if a string can be converted to float."""
    try:
        float(s)
        return True
    except (ValueError, TypeError):
        return False


def _is_integer(s):
    """Check if a string can be converted to int."""
    try:
        int(s)
        return True
    except (ValueError, TypeError):
        return False


# --- Property Tests ---

@settings(max_examples=100)
@given(value=valid_confidence_threshold_strategy())
def test_valid_confidence_threshold_accepted(value):
    """Property 8: Valid confidence_threshold values (numeric in [0.0, 1.0])
    are accepted and returned as the float conversion of the input.

    **Validates: Requirements 5.1, 5.2, 6.5, 6.6, 8.4**
    """
    loader = ConfigLoader()
    result = loader._validate_confidence_threshold(value)
    expected = float(value)
    assert result == expected, (
        "Expected {}, got {} for input '{}'".format(expected, result, value)
    )


@settings(max_examples=100)
@given(value=invalid_confidence_threshold_strategy())
def test_invalid_confidence_threshold_rejected(value):
    """Property 8: Invalid confidence_threshold values (non-numeric or outside
    [0.0, 1.0]) are rejected and the default 0.4 is returned.

    **Validates: Requirements 5.1, 5.2, 6.5, 6.6, 8.4**
    """
    loader = ConfigLoader()
    result = loader._validate_confidence_threshold(value)
    assert result == 0.35, (
        "Expected default 0.4, got {} for invalid input '{}'".format(result, value)
    )


@settings(max_examples=100)
@given(value=valid_web_port_strategy())
def test_valid_web_port_accepted(value):
    """Property 8: Valid web_port values (integer in [1024, 65535])
    are accepted and returned as the int conversion of the input.

    **Validates: Requirements 5.1, 5.2, 6.5, 6.6, 8.4**
    """
    loader = ConfigLoader()
    result = loader._validate_web_port(value)
    expected = int(value)
    assert result == expected, (
        "Expected {}, got {} for input '{}'".format(expected, result, value)
    )


@settings(max_examples=100)
@given(value=invalid_web_port_strategy())
def test_invalid_web_port_rejected(value):
    """Property 8: Invalid web_port values (non-integer or outside [1024, 65535])
    are rejected and the default 8080 is returned.

    **Validates: Requirements 5.1, 5.2, 6.5, 6.6, 8.4**
    """
    loader = ConfigLoader()
    result = loader._validate_web_port(value)
    assert result == 8080, (
        "Expected default 8080, got {} for invalid input '{}'".format(result, value)
    )


@settings(max_examples=100)
@given(value=valid_target_fps_strategy())
def test_valid_target_fps_accepted(value):
    """Property 8: Valid target_fps values (integer in [1, 60])
    are accepted and returned as the int conversion of the input.

    **Validates: Requirements 5.1, 5.2, 6.5, 6.6, 8.4**
    """
    loader = ConfigLoader()
    result = loader._validate_target_fps(value)
    expected = int(value)
    assert result == expected, (
        "Expected {}, got {} for input '{}'".format(expected, result, value)
    )


@settings(max_examples=100)
@given(value=invalid_target_fps_strategy())
def test_invalid_target_fps_rejected(value):
    """Property 8: Invalid target_fps values (non-integer or outside [1, 60])
    are rejected and the default 10 is returned.

    **Validates: Requirements 5.1, 5.2, 6.5, 6.6, 8.4**
    """
    loader = ConfigLoader()
    result = loader._validate_target_fps(value)
    assert result == 10, (
        "Expected default 10, got {} for invalid input '{}'".format(result, value)
    )


@settings(max_examples=100)
@given(value=valid_rtsp_url_strategy())
def test_valid_rtsp_url_accepted(value):
    """Property 8: Valid rtsp_input_url values (starts with 'rtsp://' and
    length <= 2048) are accepted and returned as-is.

    **Validates: Requirements 5.1, 5.2, 6.5, 6.6, 8.4**
    """
    loader = ConfigLoader()
    result = loader._validate_rtsp_url(value)
    assert result == value, (
        "Expected '{}', got '{}' for valid URL".format(value, result)
    )


@settings(max_examples=100)
@given(value=invalid_rtsp_url_strategy())
def test_invalid_rtsp_url_rejected(value):
    """Property 8: Invalid rtsp_input_url values (does not start with 'rtsp://'
    or exceeds 2048 chars) are rejected and empty string is returned.

    **Validates: Requirements 5.1, 5.2, 6.5, 6.6, 8.4**
    """
    loader = ConfigLoader()
    result = loader._validate_rtsp_url(value)
    assert result == '', (
        "Expected empty string, got '{}' for invalid URL '{}'".format(
            result, value[:50]
        )
    )


@settings(max_examples=100)
@given(value=valid_jpeg_quality_strategy())
def test_valid_jpeg_quality_accepted(value):
    """Property 8: Valid jpeg_quality values (integer in [1, 100])
    are accepted and returned as the int conversion of the input.

    **Validates: Requirements 5.1, 5.2, 6.5, 6.6, 8.4**
    """
    loader = ConfigLoader()
    result = loader._validate_jpeg_quality(value)
    expected = int(value)
    assert result == expected, (
        "Expected {}, got {} for input '{}'".format(expected, result, value)
    )


@settings(max_examples=100)
@given(value=invalid_jpeg_quality_strategy())
def test_invalid_jpeg_quality_rejected(value):
    """Property 8: Invalid jpeg_quality values (non-integer or outside [1, 100])
    are rejected and the default 70 is returned.

    **Validates: Requirements 5.1, 5.2, 6.5, 6.6, 8.4**
    """
    loader = ConfigLoader()
    result = loader._validate_jpeg_quality(value)
    assert result == 70, (
        "Expected default 70, got {} for invalid input '{}'".format(result, value)
    )
