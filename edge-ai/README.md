# Edge AI Person Detection

## Overview

This application performs real-time person detection from an RTSP video feed using a lightweight AI model optimized for ARM64 processors. Detected persons are highlighted with color-coded bounding boxes indicating confidence levels, and the annotated video is streamed to your browser.

## Getting Started

1. Click the **Start** button to begin detection
2. The application connects to the configured RTSP camera and begins processing frames
3. Detected persons appear with colored bounding boxes
4. Click **Stop** to pause detection

## Pipeline

The detection pipeline processes each frame through these stages:

1. **Capture** — Connects to the RTSP camera stream via TCP
2. **Resize** — Scales the frame to the model's input size (300x300 or 320x320)
3. **Inference** — Runs the TFLite detection model using all available CPU cores
4. **Filter** — Keeps only person detections above the confidence threshold
5. **Annotate** — Draws bounding boxes and overlays on the full-resolution frame
6. **Stream** — Encodes as JPEG and serves via MJPEG to your browser

## Available Models

You can switch between models at any time using the Model Info panel:

- **SSD MobileNet V2** (300x300) — Faster inference, good for real-time monitoring
- **YOLOv5n** (320x320) — More accurate detections, slightly higher CPU usage

Model changes take effect immediately without restarting.

## Confidence Color Coding

Bounding box colors indicate detection confidence:

- **Red** — Low confidence (below 50%)
- **Orange** — Below average (50% to 65%)
- **Yellow** — Moderate (65% to 80%)
- **Green** — High confidence (80% and above)

## Configuration Options

### RTSP URL
The camera stream address. Must start with `rtsp://`.

### Confidence Threshold
Minimum confidence score (0.1 to 1.0) for a detection to be displayed. Lower values show more detections but may include false positives. Default: 0.35.

### Target FPS
How many frames per second to process (1 to 30). Higher values use more CPU. Default: 10.

### Skip Inference Frames
Number of frames to skip between inferences (0 to 10). When set to N, inference runs every (N+1)th frame and the previous detections are reused for skipped frames. This increases display FPS without proportionally increasing CPU usage. Default: 0 (disabled).

### Stream Quality
JPEG encoding quality for the video stream (1 to 100). Lower values reduce bandwidth and encoding time but decrease image quality. Recommended: 50-70 for normal use, 30-50 for slower hardware. Default: 70.

## Overlay Toggles

- **Bounding Boxes** — Show/hide detection rectangles
- **Confidence Labels** — Show/hide percentage labels on boxes
- **FPS Overlay** — Show/hide the FPS and inference time display
- **Detection Count** — Show/hide the current number of detected persons

## Resource Usage Chart

The chart shows a rolling 30-second history of:

- **Blue line** — CPU utilization (%)
- **Green line** — Memory utilization (%)
- **Orange line** — FPS as percentage of target (100% = hitting target)

## Multi-User Access

- The first user to connect becomes the **Primary User** (shown in green in the header)
- The primary user has full control over all settings and start/stop
- Additional users connect as **Viewers** (shown in amber) — they can see the stream but cannot change settings
- If the primary user disconnects for more than 10 seconds, the next viewer is automatically promoted
- Only one detection pipeline runs regardless of how many users are viewing

## Tips for Best Performance

- Start with the SSD MobileNet V2 model for faster processing
- Set Target FPS to match your needs (5-10 is usually sufficient for monitoring)
- Use Skip Inference Frames = 1 to double display FPS with minimal accuracy impact
- Lower Stream Quality to 40-50 on slower hardware to reduce encoding overhead
- The FPS overlay shows real-time performance — if it's consistently below target, reduce Target FPS or increase frame skipping

## Troubleshooting

- **Black screen after pressing Start** — Check that the RTSP URL is correct and the camera is accessible from the router's network
- **Low FPS** — Try reducing Target FPS, increasing Skip Inference Frames, or lowering Stream Quality
- **No detections showing** — Lower the Confidence Threshold (try 0.2-0.3)
- **Too many false detections** — Raise the Confidence Threshold (try 0.5-0.6)
- **Controls locked** — Another user has primary control. Wait for them to disconnect or ask them to close their browser tab
