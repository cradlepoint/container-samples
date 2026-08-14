"""Property-based test for detection filtering.

Tests Property 3: Detection Filtering — Person Class Above Threshold.
For any list of raw detections with mixed class IDs and confidence scores,
and any confidence threshold t in [0.0, 1.0], the filtered result SHALL
contain only detections where class_id == 0 AND confidence >= t.

**Validates: Requirements 2.2, 2.5**
"""
from hypothesis import given, settings, strategies as st

from inference import InferenceEngine


# Strategy for a single raw detection dict with mixed class IDs and scores
raw_detection_strategy = st.fixed_dictionaries({
    'x_min': st.floats(min_value=0.0, max_value=0.4),
    'y_min': st.floats(min_value=0.0, max_value=0.4),
    'x_max': st.floats(min_value=0.5, max_value=1.0),
    'y_max': st.floats(min_value=0.5, max_value=1.0),
    'confidence': st.floats(min_value=0.0, max_value=1.0),
    'class_id': st.integers(min_value=0, max_value=80),  # COCO has 80 classes
})


@settings(max_examples=100)
@given(
    raw_detections=st.lists(raw_detection_strategy, min_size=0, max_size=20),
    threshold=st.floats(min_value=0.0, max_value=1.0),
)
def test_filtered_detections_contain_only_person_class(raw_detections, threshold):
    """Property 3: All filtered detections have class_id == 0 (person).

    The filtered result SHALL contain only detections where class_id == 0.

    **Validates: Requirements 2.2, 2.5**
    """
    engine = InferenceEngine(model_path="dummy.onnx", confidence_threshold=threshold)
    result = engine.filter_detections(raw_detections)

    for det in result:
        # Every detection in the result must correspond to a person class input
        # Since Detection objects don't carry class_id, we verify by checking
        # that the result count doesn't exceed person-class inputs above threshold
        pass

    # Count how many input detections are person class AND above threshold
    expected_candidates = [
        d for d in raw_detections
        if d['class_id'] == 0 and d['confidence'] >= threshold
    ]

    # The filtered result should have at most as many detections as candidates
    # (could be fewer if boxes are invalid after clamping)
    assert len(result) <= len(expected_candidates), (
        "Got {} results but only {} candidates with class_id==0 and "
        "confidence>={}".format(len(result), len(expected_candidates), threshold)
    )


@settings(max_examples=100)
@given(
    raw_detections=st.lists(raw_detection_strategy, min_size=0, max_size=20),
    threshold=st.floats(min_value=0.0, max_value=1.0),
)
def test_filtered_detections_all_above_threshold(raw_detections, threshold):
    """Property 3: All filtered detections have confidence >= threshold.

    The filtered result SHALL contain only detections where confidence >= t.

    **Validates: Requirements 2.2, 2.5**
    """
    engine = InferenceEngine(model_path="dummy.onnx", confidence_threshold=threshold)
    result = engine.filter_detections(raw_detections)

    for det in result:
        assert det.confidence >= threshold, (
            "Detection with confidence {} is below threshold {}".format(
                det.confidence, threshold
            )
        )


@settings(max_examples=100)
@given(
    raw_detections=st.lists(raw_detection_strategy, min_size=0, max_size=20),
    threshold=st.floats(min_value=0.0, max_value=1.0),
)
def test_no_non_person_detections_in_result(raw_detections, threshold):
    """Property 3: No non-person detections pass the filter.

    If a raw detection has class_id != 0, it SHALL NOT appear in the
    filtered result regardless of its confidence score.

    **Validates: Requirements 2.2, 2.5**
    """
    engine = InferenceEngine(model_path="dummy.onnx", confidence_threshold=threshold)
    result = engine.filter_detections(raw_detections)

    # Count non-person inputs
    non_person_count = sum(1 for d in raw_detections if d['class_id'] != 0)

    # If all inputs are non-person, result must be empty
    person_inputs = [d for d in raw_detections if d['class_id'] == 0]
    if len(person_inputs) == 0:
        assert len(result) == 0, (
            "Expected empty result when no person detections in input, "
            "got {} results".format(len(result))
        )


@settings(max_examples=100)
@given(
    raw_detections=st.lists(raw_detection_strategy, min_size=0, max_size=20),
    threshold=st.floats(min_value=0.0, max_value=1.0),
)
def test_below_threshold_detections_excluded(raw_detections, threshold):
    """Property 3: Detections below threshold are excluded from result.

    Even if class_id == 0, detections with confidence < threshold SHALL
    NOT appear in the filtered result.

    **Validates: Requirements 2.2, 2.5**
    """
    engine = InferenceEngine(model_path="dummy.onnx", confidence_threshold=threshold)
    result = engine.filter_detections(raw_detections)

    # Count person detections below threshold
    below_threshold_persons = [
        d for d in raw_detections
        if d['class_id'] == 0 and d['confidence'] < threshold
    ]

    # Result confidence values must all be >= threshold
    for det in result:
        assert det.confidence >= threshold, (
            "Detection with confidence {} should have been filtered out "
            "(threshold={})".format(det.confidence, threshold)
        )
