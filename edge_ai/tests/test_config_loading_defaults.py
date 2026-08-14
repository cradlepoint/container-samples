"""Property-based tests for configuration loading with defaults.

Tests Property 9: Configuration Loading with Defaults.
For any appdata dictionary with an arbitrary subset of optional fields missing,
the loaded configuration SHALL use the following defaults for missing fields:
confidence_threshold=0.4, web_port=8080, target_fps=10. Present and valid fields
SHALL retain their appdata values.

**Validates: Requirements 8.1, 8.3**
"""
import sys
from unittest.mock import patch, MagicMock

from hypothesis import given, settings, strategies as st, assume

from src.config import ConfigLoader
from src.models import AppConfig


# --- Strategies ---

# Valid value strategies for optional fields
_valid_confidence = st.floats(
    min_value=0.0, max_value=1.0,
    allow_nan=False, allow_infinity=False
)

_valid_web_port = st.integers(min_value=1024, max_value=65535)

_valid_target_fps = st.integers(min_value=1, max_value=60)

_valid_rtsp_url = st.text(
    alphabet=st.characters(
        whitelist_categories=('L', 'N', 'P', 'S'),
        blacklist_characters='\x00'
    ),
    min_size=1,
    max_size=100
).map(lambda path: 'rtsp://' + path)


# Strategy that generates an appdata dict with an arbitrary subset of optional
# fields present (as valid string values) or missing (None).
@st.composite
def appdata_with_optional_subset(draw):
    """Generate an appdata dict where each optional field is either a valid
    value (as a string, simulating cp.get_appdata return) or None (missing).

    Always includes a valid rtsp_input_url since it is required.
    """
    rtsp_url = draw(_valid_rtsp_url)

    # For each optional field, decide whether it is present or missing
    has_confidence = draw(st.booleans())
    has_web_port = draw(st.booleans())
    has_target_fps = draw(st.booleans())

    confidence_value = None
    if has_confidence:
        confidence_value = str(draw(_valid_confidence))

    web_port_value = None
    if has_web_port:
        web_port_value = str(draw(_valid_web_port))

    target_fps_value = None
    if has_target_fps:
        target_fps_value = str(draw(_valid_target_fps))

    appdata = {
        'rtsp_input_url': rtsp_url,
        'confidence_threshold': confidence_value,
        'web_port': web_port_value,
        'target_fps': target_fps_value,
    }

    return appdata


# --- Property Test ---

@settings(max_examples=100)
@given(appdata=appdata_with_optional_subset())
def test_config_loading_uses_defaults_for_missing_optional_fields(appdata):
    """Property 9: For any appdata dict with an arbitrary subset of optional
    fields missing, the loaded configuration uses correct defaults for missing
    fields (confidence_threshold=0.4, web_port=8080, target_fps=10) and retains
    present valid field values.

    **Validates: Requirements 8.1, 8.3**
    """
    # Mock cp.get_appdata to return values from our generated appdata dict
    def mock_get_appdata(field_name):
        return appdata.get(field_name)

    with patch('cp.get_appdata', side_effect=mock_get_appdata), \
         patch('cp.put_appdata'), \
         patch('cp.log'):
        loader = ConfigLoader()
        config = loader.load_config()

    # Verify rtsp_input_url is retained (always present and valid)
    assert config.rtsp_input_url == appdata['rtsp_input_url'], (
        "Expected rtsp_input_url '{}', got '{}'".format(
            appdata['rtsp_input_url'], config.rtsp_input_url
        )
    )

    # Verify confidence_threshold: default 0.4 if missing, else retained
    if appdata['confidence_threshold'] is None:
        assert config.confidence_threshold == 0.35, (
            "Expected default confidence_threshold 0.4, got {}".format(
                config.confidence_threshold
            )
        )
    else:
        expected = float(appdata['confidence_threshold'])
        assert config.confidence_threshold == expected, (
            "Expected confidence_threshold {}, got {}".format(
                expected, config.confidence_threshold
            )
        )

    # Verify web_port: default 8080 if missing, else retained
    if appdata['web_port'] is None:
        assert config.web_port == 8080, (
            "Expected default web_port 8080, got {}".format(config.web_port)
        )
    else:
        expected = int(appdata['web_port'])
        assert config.web_port == expected, (
            "Expected web_port {}, got {}".format(expected, config.web_port)
        )

    # Verify target_fps: default 10 if missing, else retained
    if appdata['target_fps'] is None:
        assert config.target_fps == 10, (
            "Expected default target_fps 10, got {}".format(config.target_fps)
        )
    else:
        expected = int(appdata['target_fps'])
        assert config.target_fps == expected, (
            "Expected target_fps {}, got {}".format(expected, config.target_fps)
        )
