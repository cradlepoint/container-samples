"""Configuration loader for Edge AI Person Detection.

Reads and validates application configuration from router appdata
via cp.get_appdata(). If appdata entries do not exist, creates them
with default values using cp.put_appdata(). On subsequent starts,
reads the (potentially updated) values from appdata.

All appdata values are stored as strings.
"""
import sys
import os

# Add parent directory to path so cp module can be imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cp

from models import AppConfig


# Valid model names
VALID_MODELS = ('ssd_mobilenet_v2', 'yolov5n')

# Default values for configuration fields (stored as strings in appdata)
_DEFAULTS = {
    'rtsp_input_url': 'rtsp://192.168.0.33:8554/stream',
    'confidence_threshold': '0.35',
    'web_port': '8080',
    'target_fps': '10',
    'jpeg_quality': '70',
    'skip_inference_frames': '0',
    'model_name': 'ssd_mobilenet_v2',
}


class ConfigLoader:
    """Reads and validates appdata configuration via cp.py.

    On first run, creates any missing appdata entries with default values.
    On subsequent runs, reads the (potentially updated) values from appdata.
    All appdata values are stored and retrieved as strings.
    """

    def load_config(self):
        # type: () -> AppConfig
        """Load config from appdata, creating defaults for missing entries.

        For each config field:
        1. Attempt to read from appdata via cp.get_appdata()
        2. If the field does not exist (returns None), create it with the
           default value via cp.put_appdata()
        3. Validate the value and apply it

        Returns:
            AppConfig: Validated configuration instance.
        """
        raw = {}
        for field_name in ['rtsp_input_url', 'confidence_threshold', 'web_port', 'target_fps', 'skip_inference_frames', 'model_name', 'jpeg_quality']:
            value = cp.get_appdata(field_name)
            if value is None:
                # Field doesn't exist in appdata — create it with default
                default_value = _DEFAULTS[field_name]
                cp.put_appdata(field_name, default_value)
                cp.log("Created appdata '{}' with default value '{}'".format(
                    field_name, default_value))
                raw[field_name] = default_value
            else:
                raw[field_name] = value

        return self._validate_config(raw)

    def _validate_config(self, raw):
        # type: (dict) -> AppConfig
        """Validate raw config values and return AppConfig with defaults for invalid fields.

        Logs validation errors via cp.log().

        Args:
            raw: Dictionary of raw string values from appdata (may contain None).

        Returns:
            AppConfig: Validated configuration instance.
        """
        rtsp_input_url = self._validate_rtsp_url(raw.get('rtsp_input_url'))
        confidence_threshold = self._validate_confidence_threshold(
            raw.get('confidence_threshold')
        )
        web_port = self._validate_web_port(raw.get('web_port'))
        target_fps = self._validate_target_fps(raw.get('target_fps'))
        skip_inference_frames = self._validate_skip_inference_frames(
            raw.get('skip_inference_frames')
        )
        model_name = self._validate_model_name(raw.get('model_name'))
        jpeg_quality = self._validate_jpeg_quality(raw.get('jpeg_quality'))

        return AppConfig(
            rtsp_input_url=rtsp_input_url,
            confidence_threshold=confidence_threshold,
            web_port=web_port,
            target_fps=target_fps,
            skip_inference_frames=skip_inference_frames,
            model_name=model_name,
            jpeg_quality=jpeg_quality,
        )

    def _validate_rtsp_url(self, value):
        # type: (object) -> str
        """Validate rtsp_input_url field.

        Must start with "rtsp://". If missing, logs a warning and returns
        empty string (the main app will handle serving the config page).
        If present but invalid, logs an error and returns empty string.

        Args:
            value: Raw value from appdata (string or None).

        Returns:
            str: Validated URL or empty string.
        """
        if value is None or value == '':
            cp.log('WARNING: rtsp_input_url is missing from appdata configuration')
            return ''

        if not isinstance(value, str):
            cp.log(
                'ERROR: rtsp_input_url must be a string, got {}'.format(
                    type(value).__name__
                )
            )
            return ''

        if not value.startswith('rtsp://'):
            cp.log(
                'ERROR: rtsp_input_url must start with "rtsp://", '
                'got "{}"'.format(value)
            )
            return ''

        if len(value) > 2048:
            cp.log(
                'ERROR: rtsp_input_url must be 2048 characters or fewer, '
                'got {} characters'.format(len(value))
            )
            return ''

        return value

    def _validate_confidence_threshold(self, value):
        # type: (object) -> float
        """Validate confidence_threshold field.

        Must be numeric and in [0.0, 1.0]. If invalid, logs error and
        returns default. Creates appdata entry with default if invalid.

        Args:
            value: Raw string value from appdata.

        Returns:
            float: Validated threshold or default.
        """
        if value is None or value == '':
            return float(_DEFAULTS['confidence_threshold'])

        try:
            numeric_value = float(value)
        except (ValueError, TypeError):
            cp.log(
                'ERROR: confidence_threshold must be numeric, '
                'got "{}"'.format(value)
            )
            return float(_DEFAULTS['confidence_threshold'])

        if numeric_value < 0.0 or numeric_value > 1.0:
            cp.log(
                'ERROR: confidence_threshold must be between 0.0 and 1.0, '
                'got {}'.format(numeric_value)
            )
            return float(_DEFAULTS['confidence_threshold'])

        return numeric_value

    def _validate_web_port(self, value):
        # type: (object) -> int
        """Validate web_port field.

        Must be an integer in [1024, 65535]. If invalid, logs error and
        returns default.

        Args:
            value: Raw string value from appdata.

        Returns:
            int: Validated port or default.
        """
        if value is None or value == '':
            return int(_DEFAULTS['web_port'])

        try:
            int_value = int(value)
        except (ValueError, TypeError):
            cp.log(
                'ERROR: web_port must be an integer, '
                'got "{}"'.format(value)
            )
            return int(_DEFAULTS['web_port'])

        if int_value < 1024 or int_value > 65535:
            cp.log(
                'ERROR: web_port must be between 1024 and 65535, '
                'got {}'.format(int_value)
            )
            return int(_DEFAULTS['web_port'])

        return int_value

    def _validate_target_fps(self, value):
        # type: (object) -> int
        """Validate target_fps field.

        Must be an integer in [1, 60]. If invalid, logs error and
        returns default.

        Args:
            value: Raw string value from appdata.

        Returns:
            int: Validated FPS or default.
        """
        if value is None or value == '':
            return int(_DEFAULTS['target_fps'])

        try:
            int_value = int(value)
        except (ValueError, TypeError):
            cp.log(
                'ERROR: target_fps must be an integer, '
                'got "{}"'.format(value)
            )
            return int(_DEFAULTS['target_fps'])

        if int_value < 1 or int_value > 60:
            cp.log(
                'ERROR: target_fps must be between 1 and 60, '
                'got {}'.format(int_value)
            )
            return int(_DEFAULTS['target_fps'])

        return int_value

    def _validate_skip_inference_frames(self, value):
        # type: (object) -> int
        """Validate skip_inference_frames field.

        Must be an integer in [0, 10]. 0 means disabled (run inference
        every frame). N>0 means run inference every (N+1)th frame and
        reuse the last detections for skipped frames.

        Args:
            value: Raw string value from appdata.

        Returns:
            int: Validated skip count or default (0 = disabled).
        """
        if value is None or value == '':
            return int(_DEFAULTS['skip_inference_frames'])

        try:
            int_value = int(value)
        except (ValueError, TypeError):
            cp.log(
                'ERROR: skip_inference_frames must be an integer, '
                'got "{}"'.format(value)
            )
            return int(_DEFAULTS['skip_inference_frames'])

        if int_value < 0 or int_value > 10:
            cp.log(
                'ERROR: skip_inference_frames must be between 0 and 10, '
                'got {}'.format(int_value)
            )
            return int(_DEFAULTS['skip_inference_frames'])

        return int_value

    def _validate_model_name(self, value):
        # type: (object) -> str
        """Validate model_name field.

        Must be one of the valid model identifiers.

        Args:
            value: Raw string value from appdata.

        Returns:
            str: Validated model name or default.
        """
        if value is None or value == '':
            return _DEFAULTS['model_name']

        if not isinstance(value, str):
            cp.log(
                'ERROR: model_name must be a string, '
                'got "{}"'.format(type(value).__name__)
            )
            return _DEFAULTS['model_name']

        if value not in VALID_MODELS:
            cp.log(
                'ERROR: model_name must be one of {}, '
                'got "{}"'.format(VALID_MODELS, value)
            )
            return _DEFAULTS['model_name']

        return value

    def _validate_jpeg_quality(self, value):
        # type: (object) -> int
        """Validate jpeg_quality field.

        Must be an integer in [1, 100]. If invalid, logs error and
        returns default.

        Args:
            value: Raw string value from appdata.

        Returns:
            int: Validated JPEG quality or default.
        """
        if value is None or value == '':
            return int(_DEFAULTS['jpeg_quality'])

        try:
            int_value = int(value)
        except (ValueError, TypeError):
            cp.log(
                'ERROR: jpeg_quality must be an integer, '
                'got "{}"'.format(value)
            )
            return int(_DEFAULTS['jpeg_quality'])

        if int_value < 1 or int_value > 100:
            cp.log(
                'ERROR: jpeg_quality must be between 1 and 100, '
                'got {}'.format(int_value)
            )
            return int(_DEFAULTS['jpeg_quality'])

        return int_value
