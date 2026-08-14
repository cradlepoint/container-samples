# Model Files

## Included Model

**ssd_mobilenet_v2.tflite** — SSD MobileNet V2 quantized for TensorFlow Lite

| Property | Value |
|----------|-------|
| Architecture | SSD MobileNet V2 |
| Format | TensorFlow Lite (FlatBuffer) |
| Quantization | INT8 (uint8 input) |
| Input size | 300x300x3 (RGB, uint8) |
| Output | Up to 20 detections per frame |
| Classes | 90 COCO classes (person = class 0) |
| File size | ~6 MB |
| Optimized for | ARM64 CPU via XNNPACK (NEON SIMD) |

## How Detection Works

1. The input frame (any resolution from the RTSP stream) is resized to 300x300 using OpenCV's NEON-accelerated resize
2. The resized frame is converted from BGR to RGB (uint8)
3. TFLite runs inference using 4 CPU threads with XNNPACK delegate
4. Only person detections (COCO class 0) above the confidence threshold are returned
5. Bounding box coordinates are normalized [0.0, 1.0] and mapped back to the original frame resolution for annotation

## Note on Display

The web viewer displays the **full-resolution, full-color** frame from the RTSP stream with detection overlays drawn on top. The 300x300 downscaled version is only used internally for inference — the user always sees the original quality image with annotations.

## Source

Downloaded from the Google Coral test data repository:
```
https://raw.githubusercontent.com/google-coral/test_data/master/ssd_mobilenet_v2_coco_quant_postprocess.tflite
```

This is a pre-trained, post-processed model ready for edge deployment without additional conversion steps.
