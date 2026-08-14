"""Unit tests for InferenceEngine.

Tests specific example scenarios for inference engine behavior:
- Model load failure prevents inference attempts
- Inference failure on frame logs error and continues
- Threshold update applies to subsequent detections

Validates: Requirements 2.7, 2.8, 6.2
"""
import sys
from unittest.mock import patch, MagicMock

import numpy
import pytest

from src.inference import InferenceEngine


class TestModelLoadFailurePreventsInference:
    """Test that model load failure prevents inference attempts.

    Validates: Requirement 2.7
    """

    @patch('cp.log')
    def test_missing_model_file_returns_false(self, mock_log):
        """load_model() returns False when model file does not exist."""
        engine = InferenceEngine('/nonexistent/model.tflite', confidence_threshold=0.4)
        result = engine.load_model()
        assert result is False

    @patch('cp.log')
    def test_missing_model_file_logs_error(self, mock_log):
        """load_model() logs an error when model file is missing."""
        engine = InferenceEngine('/nonexistent/model.tflite', confidence_threshold=0.4)
        engine.load_model()

        log_calls = [str(c) for c in mock_log.call_args_list]
        assert any('model' in c.lower() and 'not found' in c.lower()
                   for c in log_calls), (
            "Expected error log about missing model, got: {}".format(log_calls)
        )

    @patch('cp.log')
    def test_detect_returns_empty_when_model_not_loaded(self, mock_log):
        """detect() returns empty list when model has not been loaded."""
        engine = InferenceEngine('/nonexistent/model.tflite', confidence_threshold=0.4)
        # Do not call load_model — model is not loaded
        frame = numpy.zeros((480, 640, 3), dtype=numpy.uint8)

        detections = engine.detect(frame)

        assert detections == []

    @patch('cp.log')
    def test_detect_returns_empty_after_failed_load(self, mock_log):
        """detect() returns empty list after load_model() fails."""
        engine = InferenceEngine('/nonexistent/model.tflite', confidence_threshold=0.4)
        result = engine.load_model()
        assert result is False

        frame = numpy.zeros((480, 640, 3), dtype=numpy.uint8)
        detections = engine.detect(frame)
        assert detections == []

    @patch('cp.log')
    def test_tflite_exception_prevents_inference(self, mock_log):
        """If TFLite raises during load, model stays unloaded and detect returns empty."""
        engine = InferenceEngine('/some/model.tflite', confidence_threshold=0.4)

        with patch('os.path.exists', return_value=True), \
             patch('src.inference.tflite') as mock_tflite:
            mock_tflite.Interpreter.side_effect = RuntimeError("corrupt model")
            result = engine.load_model()

        assert result is False

        frame = numpy.zeros((480, 640, 3), dtype=numpy.uint8)
        detections = engine.detect(frame)
        assert detections == []

    @patch('cp.log')
    def test_tflite_unavailable_prevents_load(self, mock_log):
        """If tflite is None (not installed), load_model returns False."""
        engine = InferenceEngine('/some/model.tflite', confidence_threshold=0.4)

        with patch('src.inference.tflite', None):
            result = engine.load_model()

        assert result is False

        # Verify error was logged
        log_calls = [str(c) for c in mock_log.call_args_list]
        assert any('tflite' in c.lower() or 'not available' in c.lower()
                   for c in log_calls), (
            "Expected error log about tflite unavailability, got: {}".format(log_calls)
        )


class TestInferenceFailureLogsAndContinues:
    """Test that inference failure on a frame logs error and continues.

    Validates: Requirement 2.8
    """

    @patch('cp.log')
    def test_inference_exception_returns_empty_list(self, mock_log):
        """detect() returns empty list when interpreter.invoke() raises."""
        engine = InferenceEngine('/some/model.tflite', confidence_threshold=0.4)

        # Manually set up engine as if model loaded successfully
        engine._model_loaded = True
        engine._input_shape = (300, 300)
        engine._input_dtype = numpy.uint8
        engine._input_details = [{'index': 0, 'shape': [1, 300, 300, 3], 'dtype': numpy.uint8}]
        engine._output_details = [
            {'index': 1}, {'index': 2}, {'index': 3}, {'index': 4}
        ]

        mock_interpreter = MagicMock()
        mock_interpreter.invoke.side_effect = RuntimeError("inference failed")
        engine._interpreter = mock_interpreter

        frame = numpy.zeros((480, 640, 3), dtype=numpy.uint8)
        detections = engine.detect(frame)

        assert detections == []

    @patch('cp.log')
    def test_inference_exception_logs_error(self, mock_log):
        """detect() logs error when interpreter.invoke() raises."""
        engine = InferenceEngine('/some/model.tflite', confidence_threshold=0.4)

        engine._model_loaded = True
        engine._input_shape = (300, 300)
        engine._input_dtype = numpy.uint8
        engine._input_details = [{'index': 0, 'shape': [1, 300, 300, 3], 'dtype': numpy.uint8}]
        engine._output_details = [
            {'index': 1}, {'index': 2}, {'index': 3}, {'index': 4}
        ]

        mock_interpreter = MagicMock()
        mock_interpreter.invoke.side_effect = RuntimeError("inference failed")
        engine._interpreter = mock_interpreter

        frame = numpy.zeros((480, 640, 3), dtype=numpy.uint8)
        engine.detect(frame)

        log_calls = [str(c) for c in mock_log.call_args_list]
        assert any('inference' in c.lower() and 'failed' in c.lower()
                   for c in log_calls), (
            "Expected error log about inference failure, got: {}".format(log_calls)
        )

    @patch('cp.log')
    def test_engine_continues_after_inference_failure(self, mock_log):
        """After a failed inference, subsequent detect() calls still work."""
        engine = InferenceEngine('/some/model.tflite', confidence_threshold=0.4)

        engine._model_loaded = True
        engine._input_shape = (300, 300)
        engine._input_dtype = numpy.uint8
        engine._input_details = [{'index': 0, 'shape': [1, 300, 300, 3], 'dtype': numpy.uint8}]
        engine._output_details = [
            {'index': 0}, {'index': 1}, {'index': 2}, {'index': 3}
        ]

        mock_interpreter = MagicMock()
        # First invoke fails, second succeeds
        call_count = [0]

        def invoke_side_effect():
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("inference failed")

        mock_interpreter.invoke.side_effect = invoke_side_effect
        # Return empty detections on second call
        mock_interpreter.get_tensor.return_value = numpy.zeros((1, 20, 4), dtype=numpy.float32)

        # Override get_tensor to return proper outputs for second call
        def get_tensor_side_effect(index):
            if index == 0:
                return numpy.zeros((1, 20, 4), dtype=numpy.float32)  # boxes
            elif index == 1:
                return numpy.zeros((1, 20), dtype=numpy.float32)  # class_ids
            elif index == 2:
                return numpy.zeros((1, 20), dtype=numpy.float32)  # scores
            elif index == 3:
                return numpy.array([0], dtype=numpy.float32)  # num_detections
            return numpy.array([])

        mock_interpreter.get_tensor.side_effect = get_tensor_side_effect
        engine._interpreter = mock_interpreter

        frame = numpy.zeros((480, 640, 3), dtype=numpy.uint8)

        # First call: fails
        detections1 = engine.detect(frame)
        assert detections1 == []

        # Second call: succeeds (empty detections)
        detections2 = engine.detect(frame)
        assert detections2 == []

        # Verify model is still considered loaded
        assert engine._model_loaded is True


class TestThresholdUpdateAppliesToSubsequentDetections:
    """Test that threshold update applies to subsequent detections.

    Validates: Requirement 6.2
    """

    def test_set_threshold_updates_value(self):
        """set_threshold() updates the confidence_threshold property."""
        engine = InferenceEngine('/some/model.tflite', confidence_threshold=0.4)

        assert engine.confidence_threshold == 0.4

        engine.set_threshold(0.7)

        assert engine.confidence_threshold == 0.7

    def test_filter_detections_uses_updated_threshold(self):
        """filter_detections() uses the new threshold after set_threshold()."""
        engine = InferenceEngine('/some/model.tflite', confidence_threshold=0.8)

        raw_detections = [
            {'x_min': 0.1, 'y_min': 0.1, 'x_max': 0.5, 'y_max': 0.5,
             'confidence': 0.6, 'class_id': 0},
            {'x_min': 0.2, 'y_min': 0.2, 'x_max': 0.6, 'y_max': 0.6,
             'confidence': 0.9, 'class_id': 0},
        ]

        # With threshold 0.8, only the 0.9 detection passes
        detections = engine.filter_detections(raw_detections)
        assert len(detections) == 1
        assert detections[0].confidence == 0.9

        # Lower threshold to 0.5
        engine.set_threshold(0.5)

        # Now both detections pass
        detections = engine.filter_detections(raw_detections)
        assert len(detections) == 2

    def test_threshold_change_does_not_affect_already_filtered(self):
        """Changing threshold only affects subsequent filter calls."""
        engine = InferenceEngine('/some/model.tflite', confidence_threshold=0.3)

        raw_detections = [
            {'x_min': 0.1, 'y_min': 0.1, 'x_max': 0.4, 'y_max': 0.4,
             'confidence': 0.35, 'class_id': 0},
            {'x_min': 0.5, 'y_min': 0.5, 'x_max': 0.9, 'y_max': 0.9,
             'confidence': 0.5, 'class_id': 0},
        ]

        # With threshold 0.3, both pass
        detections_before = engine.filter_detections(raw_detections)
        assert len(detections_before) == 2

        # Raise threshold to 0.4
        engine.set_threshold(0.4)

        # Now only the 0.5 detection passes (0.35 < 0.4)
        detections_after = engine.filter_detections(raw_detections)
        assert len(detections_after) == 1
        assert detections_after[0].confidence == 0.5

    @patch('cp.log')
    def test_threshold_update_applies_in_detect_pipeline(self, mock_log):
        """set_threshold() affects the full detect() pipeline on subsequent calls."""
        engine = InferenceEngine('/some/model.tflite', confidence_threshold=0.9)

        engine._model_loaded = True
        engine._input_shape = (300, 300)
        engine._input_dtype = numpy.uint8
        engine._input_details = [{'index': 0, 'shape': [1, 300, 300, 3], 'dtype': numpy.uint8}]
        engine._output_details = [
            {'index': 0}, {'index': 1}, {'index': 2}, {'index': 3}
        ]
        engine._input_buffer = numpy.zeros((1, 300, 300, 3), dtype=numpy.uint8)

        # Mock interpreter that returns a person detection with confidence 0.7
        mock_interpreter = MagicMock()

        def get_tensor_side_effect(index):
            if index == 0:
                # boxes: [ymin, xmin, ymax, xmax] normalized
                return numpy.array([[[0.2, 0.2, 0.8, 0.8]]], dtype=numpy.float32)
            elif index == 1:
                # class_ids: person = 0
                return numpy.array([[0.0]], dtype=numpy.float32)
            elif index == 2:
                # scores
                return numpy.array([[0.7]], dtype=numpy.float32)
            elif index == 3:
                # num_detections
                return numpy.array([1.0], dtype=numpy.float32)
            return numpy.array([])

        mock_interpreter.get_tensor.side_effect = get_tensor_side_effect
        engine._interpreter = mock_interpreter

        frame = numpy.zeros((300, 300, 3), dtype=numpy.uint8)

        # With threshold 0.9, the 0.7 detection should be filtered out
        detections = engine.detect(frame)
        assert len(detections) == 0

        # Lower threshold to 0.5
        engine.set_threshold(0.5)

        # Reset side_effect so it can be called again
        mock_interpreter.get_tensor.side_effect = get_tensor_side_effect

        # Now the 0.7 detection should pass
        detections = engine.detect(frame)
        assert len(detections) == 1
        assert detections[0].confidence == pytest.approx(0.7, abs=0.01)
