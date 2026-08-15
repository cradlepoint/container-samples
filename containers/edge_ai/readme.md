# Edge AI Person Detection

Real-time person detection for Cradlepoint ARM64 routers. Ingests an RTSP video feed, runs TensorFlow Lite inference on-device, annotates detections with color-coded bounding boxes, and serves the annotated stream through a web interface accessible from any browser.

## How It Works

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│ RTSP Camera │────▶│ Frame Decode │────▶│  Inference   │────▶│  Annotation  │────▶│ MJPEG Stream│
│  (PyAV)     │     │ (BGR24)      │     │ (TFLite/     │     │ (OpenCV draw)│     │ (HTTP/JPEG) │
│             │     │              │     │  XNNPACK)    │     │              │     │             │
└─────────────┘     └──────────────┘     └─────────────┘     └──────────────┘     └─────────────┘
                                                                                          │
                                                                                          ▼
                                                                                   ┌─────────────┐
                                                                                   │   Browser   │
                                                                                   │  (Web UI)   │
                                                                                   └─────────────┘
```

### Pipeline Stages

1. **RTSP Capture** — PyAV connects to the camera's RTSP stream via TCP. Frames are decoded to BGR24 numpy arrays. Frame skipping reduces decode overhead when the source FPS exceeds the target.

2. **Preprocessing** — The full-resolution frame is resized to the model's input dimensions (300x300 or 320x320) using OpenCV's NEON-accelerated resize. Color conversion from BGR to RGB is applied.

3. **Inference** — TensorFlow Lite runs the detection model using the XNNPACK delegate with all available CPU cores. The model outputs bounding boxes, class IDs, and confidence scores.

4. **Post-processing** — Only person detections (COCO class 0) above the confidence threshold are kept. For YOLOv5n, Non-Maximum Suppression removes overlapping boxes.

5. **Annotation** — Bounding boxes are drawn on the original full-resolution frame using OpenCV (in-place, no format conversion). Color coding indicates confidence level. FPS, inference time, and detection count overlays are added.

6. **Streaming** — The annotated frame is JPEG-encoded (OpenCV imencode) and served as an MJPEG stream over HTTP. A threaded server handles multiple concurrent clients.

## Available Models

Both models are included in the Docker image and can be switched at runtime via the web UI:

| Model | Input | Size | Speed | Accuracy |
|-------|-------|------|-------|----------|
| **SSD MobileNet V2** | 300x300 uint8 | 5.9 MB | Faster (~10 FPS) | Good |
| **YOLOv5n INT8** | 320x320 float32 | 2.7 MB | Slightly slower | Better |

- SSD MobileNet V2: Pre-trained COCO model with built-in post-processing (boxes, scores, classes output directly)
- YOLOv5n: Ultralytics export with single-tensor output, requires transpose + NMS in post-processing

## Confidence Color Coding

| Confidence | Color | Meaning |
|-----------|-------|---------|
| < 0.50 | Red | Low confidence |
| 0.50 – 0.65 | Orange | Below average |
| 0.65 – 0.80 | Yellow | Moderate |
| ≥ 0.80 | Green | High confidence |

## Web Interface

The web UI is served on port 8080 (configurable) and provides:

- **Live video stream** — Full-resolution MJPEG with detection overlays (16:9 aspect ratio)
- **Start/Stop control** — Begin or pause detection processing; shows "Detection Stopped" placeholder when idle
- **Configuration panel** — RTSP URL, confidence threshold, target FPS, frame skipping, JPEG quality
- **Overlay toggles** — Enable/disable bounding boxes, confidence labels, FPS overlay, detection count (2x2 grid)
- **Model selector** — Switch between SSD MobileNet V2 and YOLOv5n at runtime (immediate, no restart needed)
- **Resource monitoring** — Rolling 30-second CPU and memory usage chart (collapsible panel)
- **Model info** — Current model details, input/output dimensions, runtime info (collapsible panel)
- **Collapsible panels** — All configuration panels can be collapsed/expanded via header click
- **Session control** — Primary user has configuration control; secondary users can view the stream but cannot change settings
- **Primary user indicator** — Header badge shows "Primary User" (green) or "Viewer Only" (amber)
- **Dark mode** — Toggle between light and dark themes
- **Toast notifications** — Non-intrusive slide-in feedback for configuration changes
- **Form auto-population** — All form fields load current server values on page load via `GET /config`

### Multi-User Behavior

- Only one detection pipeline runs regardless of how many users are viewing
- The first user to connect becomes the "primary" user with full control
- Secondary users see the same stream but their configuration controls are locked
- All users can independently start/stop their view of the stream
- When the primary user disconnects (10s timeout), the next user is promoted

## Configuration (Appdata)

All configuration is stored in the router's appdata and persists across container restarts:

| Field | Default | Range | Description |
|-------|---------|-------|-------------|
| `rtsp_input_url` | `rtsp://192.168.0.33:8554/stream` | Valid RTSP URL | Camera stream URL |
| `confidence_threshold` | `0.35` | 0.0 – 1.0 | Minimum detection confidence |
| `target_fps` | `10` | 1 – 60 | Target processing frame rate |
| `web_port` | `8080` | 1024 – 65535 | Web server port |
| `skip_inference_frames` | `0` | 0 – 10 | Frames to skip between inferences (0=disabled) |
| `model_name` | `ssd_mobilenet_v2` | `ssd_mobilenet_v2`, `yolov5n` | Active detection model |
| `jpeg_quality` | `70` | 1 – 100 | MJPEG stream quality (lower = faster, less bandwidth) |

On first boot, any missing appdata entries are created with default values. Changes made via the web UI are persisted to appdata immediately.

## Performance Optimizations

- **Multi-threaded TFLite** — Uses all available CPU cores for inference
- **OpenCV NEON SIMD** — ARM64-optimized resize and color conversion
- **In-place annotation** — OpenCV draws directly on numpy arrays (no PIL conversion)
- **OpenCV JPEG encoding** — Faster than PIL for MJPEG streaming
- **Pre-allocated input buffer** — Avoids per-frame numpy allocation
- **Frame skipping** — Skip N frames between inferences, reuse last detections
- **Client-aware annotation** — Skips drawing when no clients are connected
- **RTSP frame decimation** — Discards frames at the demux level when source FPS exceeds target
- **Adaptive rate control** — Reduces FPS automatically under sustained high latency
- **RTSP buffer management** — Reduced buffer size and `nobuffer` flag to prevent stale frame buildup and decode artifacts
- **Stream frame-drop protection** — Skips re-encoding unchanged frames to prevent backpressure artifacts on slower hardware
- **Configurable JPEG quality** — Lower quality reduces encoding time and bandwidth for resource-constrained devices

## Project Structure

```
edge_ai/
├── src/
│   ├── main.py              # Application entry point, thread orchestration
│   ├── config.py            # Configuration loader (reads/writes router appdata)
│   ├── capture.py           # RTSP capture via PyAV with reconnection
│   ├── inference.py         # TFLite inference engine (SSD + YOLO support)
│   ├── annotation.py        # OpenCV-based bounding box and overlay rendering
│   ├── processor.py         # Frame processing pipeline orchestrator
│   ├── web_server.py        # Threaded HTTP server (MJPEG, API, static files)
│   ├── models.py            # Data models (Detection, AppConfig)
│   ├── templates/
│   │   └── index.html       # Web UI (ES5 JavaScript, Font Awesome)
│   └── static/
│       ├── css/             # Stylesheets + Font Awesome + webfonts
│       └── js/app.js        # Client-side logic
├── models/
│   ├── ssd_mobilenet_v2.tflite   # SSD MobileNet V2 (5.9 MB)
│   └── yolov5n_int8.tflite       # YOLOv5n INT8 (2.7 MB)
├── tests/                   # Unit + property-based tests (167 tests)
├── cp.py                    # Router config store access module
├── Dockerfile               # Multi-stage Python 3.12 slim build
├── docker-compose.yml       # Compose v2.4 deployment config
├── requirements.txt         # Production dependencies
└── requirements-dev.txt     # Test dependencies
```

## Dependencies

| Package | Purpose | Size |
|---------|---------|------|
| numpy | Array operations | ~42 MB |
| opencv-python-headless | Resize, draw, JPEG encode | ~74 MB |
| ai-edge-litert | TFLite inference runtime | ~37 MB |
| av (PyAV) | RTSP capture via ffmpeg | ~45 MB |
| Pillow | Placeholder image generation | ~3 MB |
| requests | Router API communication (cp.py) | ~1 MB |

## Deployment

### Build

```bash
docker build --platform linux/arm64 -t jongaudu/edge-ai:latest edge_ai/
```

### Push

```bash
docker push jongaudu/edge-ai:latest
```

### Docker Compose

```yaml
version: "2.4"
services:
  edge-ai:
    image: jongaudu/edge-ai:latest
    restart: unless-stopped
    ports:
      - "8080:8080"
    mem_limit: 1g
    cpus: 4
```

### Router Deployment

Deploy via the router's container management interface (REST API or NCM). The router exposes its config store socket to the container, enabling `cp.py` to read/write appdata without volume mounts.

## Development

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

# Run tests
pytest tests/ -v

# Run locally (requires an RTSP source; no Config Store, so appdata reads return None)
python src/main.py
```

## Resource Constraints

| Resource | Limit |
|----------|-------|
| Container memory | 1 GB |
| CPU cores | 4 |
| Docker image size | ~500 MB |
| Model memory (loaded) | ~30-50 MB |
| Typical FPS (ARM64) | 8-12 FPS |
| Inference latency | 80-120 ms/frame |
