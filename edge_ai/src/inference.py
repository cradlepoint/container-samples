"""Inference Engine for Edge AI Person Detection.

Uses TensorFlow Lite with XNNPACK delegate for optimized ARM64 CPU
inference. Loads a quantized SSD MobileNet V2 model and filters
detections to person class (COCO class 0) above a configurable
confidence threshold.

Performance optimizations:
- Multi-threaded inference (num_threads=4) for full CPU utilization
- Pre-allocated input buffer to avoid per-frame allocation
- OpenCV resize for NEON SIMD acceleration on ARM64 (with Pillow fallback)

Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8
"""
import sys
import os
import threading

try:
    import numpy
except ImportError:
    numpy = None

try:
    import cv2
    _HAS_CV2 = True
except ImportError:
    cv2 = None
    _HAS_CV2 = False

try:
    import tflite_runtime.interpreter as tflite
except ImportError:
    try:
        from ai_edge_litert import interpreter as _litert
        tflite = _litert
    except ImportError:
        try:
            import tensorflow.lite as _tfl
            tflite = _tfl
        except ImportError:
            tflite = None

# Add parent directory to path so cp module is importable
_parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)

import cp

from models import Detection


# Number of CPU threads for TFLite inference
_NUM_THREADS = 4


class InferenceEngine(object):
    """Runs person detection inference using TensorFlow Lite.

    Loads a quantized SSD MobileNet V2 TFLite model and performs inference
    on preprocessed frames. Filters results to person class only (COCO
    class 0) and applies a configurable confidence threshold.

    Performance features:
    - Uses num_threads=4 for multi-core inference
    - Pre-allocates input buffer to avoid per-frame numpy allocation
    - Uses OpenCV resize (NEON SIMD on ARM64) when available

    Thread-safe threshold updates are supported via set_threshold().

    Attributes:
        model_path: Path to the TFLite model file.
        confidence_threshold: Minimum confidence for detections.
    """

    # COCO class index for person (0-indexed in this model's output)
    _PERSON_CLASS_ID = 0

    def __init__(self, model_path, confidence_threshold=0.4, num_threads=_NUM_THREADS):
        # type: (str, float, int) -> None
        """Initialize InferenceEngine.

        Args:
            model_path: Path to the quantized TFLite model file.
            confidence_threshold: Minimum confidence score for detections
                (default 0.4, range [0.0, 1.0]).
            num_threads: Number of CPU threads for inference (default 4).
        """
        self.model_path = model_path
        self._num_threads = num_threads
        self._threshold_lock = threading.Lock()
        self._confidence_threshold = confidence_threshold

        self._interpreter = None
        self._input_details = None
        self._output_details = None
        self._input_shape = None  # (height, width)
        self._input_dtype = None
        self._model_loaded = False
        self._model_type = 'ssd'  # 'ssd' or 'yolo'

        # Pre-allocated input buffer (set after model load)
        self._input_buffer = None  # type: numpy.ndarray or None

    @property
    def confidence_threshold(self):
        # type: () -> float
        """Return current confidence threshold (thread-safe)."""
        with self._threshold_lock:
            return self._confidence_threshold

    @property
    def input_size(self):
        # type: () -> tuple
        """Return model's expected input dimensions as (width, height).

        Returns:
            Tuple of (width, height) if model is loaded, otherwise (300, 300)
            as a default fallback.
        """
        if self._input_shape is not None:
            # _input_shape is (height, width)
            return (self._input_shape[1], self._input_shape[0])
        return (300, 300)

    def load_model(self):
        # type: () -> bool
        """Load the TFLite model with multi-threaded execution.

        Returns:
            True if model loaded successfully, False otherwise.
        """
        if tflite is None:
            cp.log("ERROR: TFLite runtime is not available, cannot load model")
            self._model_loaded = False
            return False

        if not os.path.exists(self.model_path):
            cp.log("ERROR: Model file not found: {}".format(self.model_path))
            self._model_loaded = False
            return False

        try:
            # Create interpreter with multi-threading for full CPU utilization
            self._interpreter = tflite.Interpreter(
                model_path=self.model_path,
                num_threads=self._num_threads
            )
            self._interpreter.allocate_tensors()

            # Extract input metadata
            self._input_details = self._interpreter.get_input_details()
            self._output_details = self._interpreter.get_output_details()

            input_shape = self._input_details[0]['shape']
            # Shape is [batch, height, width, channels]
            self._input_shape = (int(input_shape[1]), int(input_shape[2]))
            self._input_dtype = self._input_details[0]['dtype']

            # Pre-allocate input buffer to avoid per-frame allocation
            self._input_buffer = numpy.zeros(
                (1, self._input_shape[0], self._input_shape[1], 3),
                dtype=self._input_dtype
            )

            # Detect model type from output structure
            if len(self._output_details) == 1:
                self._model_type = 'yolo'  # Single output tensor = YOLO format
            else:
                self._model_type = 'ssd'   # Multiple outputs = SSD format

            self._model_loaded = True
            cp.log("Model loaded: {} ({}x{}, {}, {} threads, type={})".format(
                os.path.basename(self.model_path),
                self._input_shape[1], self._input_shape[0],
                self._input_dtype.__name__ if hasattr(self._input_dtype, '__name__') else str(self._input_dtype),
                self._num_threads,
                self._model_type))
            return True

        except Exception as e:
            cp.log("ERROR: Failed to load model {}: {} - {}".format(
                self.model_path, type(e).__name__, e))
            self._model_loaded = False
            return False

    def detect(self, frame):
        # type: (numpy.ndarray) -> list
        """Run inference on a frame and return filtered person detections.

        Preprocesses the frame (resize to model input size, convert dtype),
        runs inference, and post-processes results to return only person
        detections above the confidence threshold.

        Args:
            frame: Input frame as a numpy array (BGR, HWC format).

        Returns:
            List of Detection objects. Returns empty list if model is not
            loaded or inference fails.
        """
        if not self._model_loaded:
            return []

        if numpy is None:
            return []

        try:
            # Preprocess: resize and convert to model input format
            input_h, input_w = self._input_shape
            self._preprocess_into_buffer(frame, input_w, input_h)

            # Set input tensor (uses pre-allocated buffer)
            input_index = self._input_details[0]['index']
            self._interpreter.set_tensor(input_index, self._input_buffer)

            # Run inference (multi-threaded via num_threads)
            self._interpreter.invoke()

            # Branch on model type for output parsing
            if self._model_type == 'yolo':
                output = self._interpreter.get_tensor(
                    self._output_details[0]['index'])
                raw_detections = self._parse_yolo_output(output)
            else:
                raw_detections = self._parse_ssd_output()

            # Filter and validate (clamping, box validity)
            detections = self.filter_detections(raw_detections)
            return detections

        except Exception as e:
            cp.log("ERROR: Inference failed: {} - {}".format(
                type(e).__name__, e))
            return []

    def filter_detections(self, raw_detections):
        # type: (list) -> list
        """Filter raw detections to person class above confidence threshold.

        Keeps only detections where class_id == 0 (person) and confidence
        is at or above the current threshold.

        Args:
            raw_detections: List of dicts with keys: x_min, y_min, x_max,
                y_max, confidence, class_id.

        Returns:
            List of Detection objects passing the filter criteria.
        """
        threshold = self.confidence_threshold
        filtered = []

        for det in raw_detections:
            if det['class_id'] != self._PERSON_CLASS_ID:
                continue
            if det['confidence'] < threshold:
                continue

            # Clamp coordinates to [0.0, 1.0]
            x_min = max(0.0, min(1.0, det['x_min']))
            y_min = max(0.0, min(1.0, det['y_min']))
            x_max = max(0.0, min(1.0, det['x_max']))
            y_max = max(0.0, min(1.0, det['y_max']))

            # Ensure valid box (positive width and height)
            if x_min >= x_max or y_min >= y_max:
                continue

            # Filter out boxes wider than tall (not a person shape)
            box_width = x_max - x_min
            box_height = y_max - y_min
            if box_width > box_height:
                continue

            confidence = max(0.0, min(1.0, det['confidence']))

            filtered.append(Detection(
                x_min=x_min,
                y_min=y_min,
                x_max=x_max,
                y_max=y_max,
                confidence=confidence,
            ))

        return filtered

    def set_threshold(self, threshold):
        # type: (float) -> None
        """Update confidence threshold at runtime (thread-safe).

        Args:
            threshold: New confidence threshold in [0.0, 1.0].
        """
        with self._threshold_lock:
            self._confidence_threshold = threshold

    def _parse_ssd_output(self):
        # type: () -> list
        """Parse SSD MobileNet V2 output tensors into detection dicts.

        SSD MobileNet V2 outputs:
          0: boxes [1, N, 4] - normalized [ymin, xmin, ymax, xmax]
          1: class_ids [1, N] - class indices
          2: scores [1, N] - confidence scores
          3: num_detections [1] - number of valid detections

        Returns:
            List of detection dicts with keys: x_min, y_min, x_max, y_max,
            confidence, class_id.
        """
        boxes = self._interpreter.get_tensor(self._output_details[0]['index'])[0]
        class_ids = self._interpreter.get_tensor(self._output_details[1]['index'])[0]
        scores = self._interpreter.get_tensor(self._output_details[2]['index'])[0]
        num_detections = int(self._interpreter.get_tensor(self._output_details[3]['index'])[0])

        threshold = self.confidence_threshold
        raw_detections = []
        for i in range(num_detections):
            score = float(scores[i])
            if score < threshold:
                continue
            class_id = int(class_ids[i])
            if class_id != self._PERSON_CLASS_ID:
                continue
            raw_detections.append({
                'y_min': float(boxes[i][0]),
                'x_min': float(boxes[i][1]),
                'y_max': float(boxes[i][2]),
                'x_max': float(boxes[i][3]),
                'confidence': score,
                'class_id': class_id,
            })
        return raw_detections

    def _parse_yolo_output(self, output):
        # type: (numpy.ndarray) -> list
        """Parse YOLOv5/v8 output tensor [1, 84, 2100] into detection dicts.

        Transposes from [1, 84, 2100] to [2100, 84], extracts bounding boxes
        and class scores, filters by confidence and person class, then applies
        Non-Maximum Suppression.

        Args:
            output: Raw output tensor from the model, shape [1, 84, 2100].

        Returns:
            List of detection dicts with keys: x_min, y_min, x_max, y_max,
            confidence, class_id.
        """
        # Transpose from [1, 84, 2100] to [2100, 84]
        predictions = output[0].transpose()  # Now [2100, 84]

        threshold = self.confidence_threshold

        raw_detections = []
        for i in range(predictions.shape[0]):
            # First 4: x_center, y_center, width, height (already normalized [0, 1])
            x_c = predictions[i][0]
            y_c = predictions[i][1]
            w = predictions[i][2]
            h = predictions[i][3]

            # Class scores start at index 4
            class_scores = predictions[i][4:]
            class_id = int(numpy.argmax(class_scores))
            confidence = float(class_scores[class_id])

            if confidence < threshold:
                continue
            if class_id != self._PERSON_CLASS_ID:
                continue

            # Coordinates are already normalized [0, 1]
            x_min = x_c - w / 2.0
            y_min = y_c - h / 2.0
            x_max = x_c + w / 2.0
            y_max = y_c + h / 2.0

            raw_detections.append({
                'x_min': float(x_min),
                'y_min': float(y_min),
                'x_max': float(x_max),
                'y_max': float(y_max),
                'confidence': confidence,
                'class_id': class_id,
            })

        # Apply NMS
        raw_detections = self._apply_nms(raw_detections)
        return raw_detections

    def _apply_nms(self, detections, iou_threshold=0.45):
        # type: (list, float) -> list
        """Apply Non-Maximum Suppression.

        Sorts detections by confidence descending and removes overlapping
        detections that exceed the IoU threshold.

        Args:
            detections: List of detection dicts.
            iou_threshold: IoU threshold above which a detection is suppressed.

        Returns:
            Filtered list of detection dicts after NMS.
        """
        if not detections:
            return []
        # Sort by confidence descending
        detections = sorted(detections, key=lambda d: d['confidence'], reverse=True)
        kept = []
        for det in detections:
            overlap = False
            for k in kept:
                iou = self._compute_iou(det, k)
                if iou > iou_threshold:
                    overlap = True
                    break
            if not overlap:
                kept.append(det)
        return kept

    @staticmethod
    def _compute_iou(a, b):
        # type: (dict, dict) -> float
        """Compute IoU between two detection dicts.

        Args:
            a: First detection dict with x_min, y_min, x_max, y_max.
            b: Second detection dict with x_min, y_min, x_max, y_max.

        Returns:
            Intersection over Union value in [0.0, 1.0].
        """
        x1 = max(a['x_min'], b['x_min'])
        y1 = max(a['y_min'], b['y_min'])
        x2 = min(a['x_max'], b['x_max'])
        y2 = min(a['y_max'], b['y_max'])
        inter = max(0, x2 - x1) * max(0, y2 - y1)
        area_a = (a['x_max'] - a['x_min']) * (a['y_max'] - a['y_min'])
        area_b = (b['x_max'] - b['x_min']) * (b['y_max'] - b['y_min'])
        union = area_a + area_b - inter
        if union <= 0:
            return 0.0
        return inter / union

    def _preprocess_into_buffer(self, frame, target_w, target_h):
        # type: (numpy.ndarray, int, int) -> None
        """Preprocess frame directly into the pre-allocated input buffer.

        Uses OpenCV resize (NEON SIMD on ARM64) when available for maximum
        performance. Falls back to Pillow or numpy.

        Args:
            frame: Input BGR frame (HWC numpy array).
            target_w: Target width for model input.
            target_h: Target height for model input.
        """
        if _HAS_CV2:
            # OpenCV resize uses NEON SIMD on ARM64 — fastest path
            resized = cv2.resize(frame, (target_w, target_h))
            rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        else:
            try:
                from PIL import Image as PILImage
                pil_image = PILImage.fromarray(frame[:, :, ::-1])  # BGR to RGB
                resized = pil_image.resize((target_w, target_h), PILImage.BILINEAR)
                rgb = numpy.array(resized)
            except ImportError:
                resized = self._resize_numpy(frame, target_w, target_h)
                rgb = resized[:, :, ::-1]  # BGR to RGB

        # Write directly into pre-allocated buffer
        if self._input_dtype == numpy.uint8:
            self._input_buffer[0] = rgb.astype(numpy.uint8)
        else:
            self._input_buffer[0] = rgb.astype(numpy.float32) / 255.0

    def _resize_numpy(self, frame, target_w, target_h):
        # type: (numpy.ndarray, int, int) -> numpy.ndarray
        """Nearest-neighbor resize using only numpy (fallback).

        Args:
            frame: Input frame (H, W, C).
            target_w: Target width.
            target_h: Target height.

        Returns:
            Resized frame (target_h, target_w, C).
        """
        src_h, src_w = frame.shape[0], frame.shape[1]
        row_indices = (numpy.arange(target_h) * src_h // target_h).astype(int)
        col_indices = (numpy.arange(target_w) * src_w // target_w).astype(int)
        return frame[numpy.ix_(row_indices, col_indices)]
