---
inclusion: auto
description: General lessons learned from building containers for Cradlepoint NCOS routers
---

# Lessons Learned

This file captures general lessons learned from building containers for Cradlepoint NCOS routers. It is updated after each container build via the reflection hook. Only general-purpose improvements are recorded here, not project-specific details.

## Initial Lessons

- Alpine Linux `ash` shell does not support bash-isms like arrays or `[[ ]]`. Use POSIX-compatible shell syntax in entrypoint scripts.
- The `py3-requests` Alpine package is needed if cp.py is used, since it imports `requests`.
- When using `pass_persist` or similar long-running stdin/stdout protocols, always flush stdout after each write.
- Container logging to stdout is captured by the container runtime. Use `cp.log()` or write to `/dev/stdout` for visibility.
- The router's Config Store socket is at `/var/tmp/cs.sock` — this path is fixed and must not be changed.
- In Compose YAML, use `$CONFIG_STORE` as a bare volume (no mount target path). The platform resolves it automatically. Writing `$CONFIG_STORE:/var/tmp` is incorrect.
- Always handle the case where `cp.get()` returns `None` (router may not have the requested data).
- The router's GPS fix at `status/gps/fix` uses DMS format: `{"latitude": {"degree": int, "minute": int, "second": float}, "longitude": {...}}`. When writing GPS data back via `cp.put()`, convert decimal degrees to this DMS structure. The degree component carries the sign (negative for south/west).
- For periodic sampling of router state (GPS, WAN stats, etc.), use a background thread with its own timer inside the container rather than driving sampling from HTTP request handlers or frontend polling. HTTP-driven sampling is subject to browser throttling, network jitter, and stops entirely when no client is connected. A daemon thread (`threading.Thread(target=..., daemon=True).start()`) in `main()` gives consistent cadence independent of client behavior.
- For optional persistence, check at startup whether a mount point exists (e.g., `os.path.isdir('/data')`) and fall back to `/tmp` if it doesn't. This lets the container work with or without the named volume, so missing the volume degrades gracefully (non-persistent) instead of crashing.
- Compose networks on NCOS bind to NCM Local IP Networks via the `com.cradlepoint.network.bridge.uuid` driver opt under `driver_opts`. The UUID must match an existing Local IP Network, and the declared `subnet`/`gateway` must match that network's configuration. Attach a service to multiple networks by listing them under the service's `networks:` key.
- Host networking (`network_mode: host`) is not supported on these routers. Only bridge networking is available. To place a container directly on a LAN (reachable by its own IP without port mapping), use a custom Compose network bound to a Local IP Network UUID and assign a static IP via `networks.<name>.ipv4_address`. The default bridge (`172.17.0.x`) is used when no custom network is specified — services on it are exposed externally via `ports:` mappings only.
- UDP services (SNMP, syslog, DNS, etc.) must specify the `/udp` suffix in Compose port mappings, e.g. `- '1161:1161/udp'`. Without the suffix, Docker publishes TCP only, so UDP requests are silently dropped at the router with no error in the container logs. Symptom: the service appears to work when assigned its own IP on a Local IP Network (no port publishing involved) but fails when reached via the router IP with a `ports:` mapping.

## Computer Vision & AI Lessons (from edge-ai example)

### Architecture & Threading

- Structure CV/AI applications as a multi-threaded pipeline: capture thread → processing thread → web server thread. Each stage is a separate module (`capture.py`, `processor.py`, `inference.py`, `annotation.py`, `web_server.py`). The main thread only handles signal registration and shutdown coordination.
- Use `threading.Event` for graceful shutdown coordination across threads. Register `signal.SIGTERM` and `signal.SIGINT` handlers that set the event, then have each thread's main loop check `event.is_set()` periodically.
- Use daemon threads (`thread.daemon = True`) for processing and web server threads so the process exits cleanly when the main thread completes.
- For frame sharing between producer (processor) and consumer (web server), use a double-buffer pattern with a simple `threading.Lock`. The processor writes to `current_frame` under the lock; the web server reads it under the same lock. This minimizes lock contention compared to a queue.

### RTSP Video Capture

- Use PyAV (`import av`) instead of OpenCV's VideoCapture for RTSP streaming. PyAV wraps ffmpeg directly, has a smaller footprint, and gives more control over connection options (transport protocol, buffer sizes, timeouts).
- Always use TCP transport for RTSP (`'rtsp_transport': 'tcp'`). UDP is unreliable on router networks and causes frame corruption.
- Set low-latency RTSP options: `'buffer_size': '524288'`, `'fflags': 'nobuffer'`, `'analyzeduration': '1000000'`, `'probesize': '1000000'`. This reduces connection time and prevents stale frame buildup.
- Implement RTSP connection in a background thread with a timeout, since `av.open()` can block indefinitely on unreachable streams. Use a `threading.Thread` with `.join(timeout=N)` to bound the wait.
- Implement exponential backoff for RTSP reconnection: `delay = min(2^(retry+1), 60)` gives 2, 4, 8, 16, 32, 60, 60... seconds. Always check the stop event during the wait to allow clean shutdown.
- Implement frame skipping at the capture level when the source FPS exceeds the target FPS. Detect source FPS from `stream.average_rate` and compute a skip ratio: `skip_ratio = max(1, round(source_fps / target_fps))`. Only decode every Nth frame to reduce CPU load.
- Track disconnection state with a timeout window (e.g., 10 seconds of consecutive failures) rather than disconnecting on a single failed read. RTSP streams commonly drop individual frames without being fully disconnected.

### TFLite Inference on ARM64

- Use TensorFlow Lite (tflite_runtime) for inference on ARM64 routers. It's lightweight (~5 MB) and uses XNNPACK with NEON SIMD for optimized execution.
- Import tflite with a fallback chain: try `tflite_runtime.interpreter`, then `ai_edge_litert.interpreter`, then `tensorflow.lite`. This handles different package naming across versions.
- Use `num_threads=os.cpu_count()` (typically 4) when creating the TFLite interpreter for full CPU utilization on multi-core ARM64 chips.
- Pre-allocate the input buffer (`numpy.zeros(...)`) once at model load time and reuse it every frame. This avoids per-frame memory allocation which causes GC pressure on memory-constrained routers.
- Use INT8 quantized models (uint8 input/output) for edge deployment. They're ~4x smaller and ~2-4x faster than float32 models on ARM64 with XNNPACK.
- Standard model input sizes for edge: 300x300 (SSD MobileNet V2) or 320x320 (YOLOv5n). Both work well on router hardware.
- Detect model type from output tensor structure: single output = YOLO format, multiple outputs (4 tensors) = SSD format. This allows supporting multiple model architectures with one inference engine.
- Apply Non-Maximum Suppression (NMS) for YOLO models (they don't include post-processing). SSD MobileNet V2 includes NMS in the model itself.
- Support runtime threshold updates via a `threading.Lock` around the confidence threshold value, since the web UI may update it while inference is running.

### Frame Processing & Performance

- Implement "inference frame skipping": run inference every (N+1)th frame and reuse the previous detections for skipped frames. This dramatically reduces CPU usage while maintaining smooth video display. Make N configurable (0-10, where 0 = disabled).
- Use OpenCV (`cv2.resize`) for frame resizing — it uses NEON SIMD on ARM64 and is significantly faster than Pillow or numpy. Provide a fallback chain: OpenCV → Pillow → numpy nearest-neighbor.
- Implement adaptive rate control: if inference latency exceeds 1000ms for 10 consecutive frames, halve the target FPS. If latency drops below 500ms for 10 consecutive frames, restore the original target. This prevents the pipeline from falling hopelessly behind.
- Skip annotation entirely when no web clients are connected. Annotation (drawing bounding boxes, text overlays) costs 5-10ms per frame and is pure waste without viewers.
- Use `time.time()` based frame pacing: `sleep_duration = max(0, 1/target_fps - elapsed)`. This maintains consistent output rate regardless of variable inference time.
- Log periodic stats (every 60 seconds): average inference time, FPS achieved, detection count, annotation time. Warn if zero frames were processed in an interval.

### Annotation & Streaming

- Use OpenCV drawing functions (`cv2.rectangle`, `cv2.putText`) directly on numpy arrays for annotation. This avoids the expensive numpy→PIL→numpy round-trip and is 10-50x faster.
- Color-code bounding boxes by confidence: Red (<50%), Orange (50-65%), Yellow (65-80%), Green (≥80%). This gives users instant visual feedback on detection quality.
- Use a rolling window FPS calculator (deque of timestamps over 2 seconds) rather than instantaneous frame-to-frame timing. Formula: `fps = (N-1) / (last - first)`.
- Serve video as MJPEG over HTTP (`multipart/x-mixed-replace`). It works in all browsers without JavaScript or WebSocket complexity. Boundary format: `--frame\r\nContent-Type: image/jpeg\r\nContent-Length: N\r\n\r\n<jpeg bytes>\r\n`.
- Use Python's built-in `http.server` with `ThreadingMixIn` for the web server. No need for Flask/FastAPI on resource-constrained routers.
- Make JPEG quality configurable (default 70, range 1-100). Lower quality (40-50) significantly reduces encoding time and bandwidth on slower hardware.
- Limit concurrent streaming clients (default 4). Each MJPEG connection holds an open HTTP response, consuming a thread and bandwidth.
- Implement primary-user session control for multi-user scenarios: first user gets control, others are view-only. Promote the next viewer if the primary disconnects (heartbeat timeout of 10 seconds).

### Configuration via Appdata

- Use `cp.get_appdata(field)` and `cp.put_appdata(field, value)` for all user-configurable settings. This integrates with NCM's device configuration system.
- All appdata values are stored as strings. Parse them to the appropriate type (int, float) with full validation and defaults for invalid values.
- On first run, check if each appdata field exists. If `cp.get_appdata()` returns `None`, create it with `cp.put_appdata(field, default_value)`. This provides self-configuring behavior.
- Validate all config inputs with explicit range checks and meaningful error messages via `cp.log()`. Never crash on bad config — fall back to defaults.
- Handle missing RTSP URL gracefully: start the web server anyway so the user can configure the URL via the web UI, rather than crashing.

### Web UI for Container Applications

- Serve a single-page web UI from `templates/index.html` with supporting `static/css/` and `static/js/` files. Keep the UI self-contained (no CDN dependencies — the router may not have internet access).
- Provide REST API endpoints alongside the stream: `/stream` (MJPEG), `/stats` (JSON metrics), `/config` (GET/POST settings), `/control` (start/stop), `/session` (primary user), `/heartbeat` (keepalive).
- Implement client-side polling for stats (CPU%, memory%, FPS, detection count) and display as a rolling chart. Poll interval of 1-2 seconds is sufficient.
- All configuration changes via the API should take effect immediately without restarting the container. Use thread-safe setters (locks) for runtime parameter updates.

### Data Models

- Use Python `dataclasses` for structured data (Detection, AppConfig, RuntimeStats). They provide clear field definitions and are lighter than full ORM classes.
- Keep detection coordinates normalized [0.0, 1.0] throughout the pipeline. Only convert to pixel coordinates at the annotation stage. This decouples inference resolution from display resolution.
- Include a `validate_detection()` function that checks invariants (coordinates in range, positive width/height, valid confidence). Use it in tests and debug builds.

### Container Image Optimization

- For CV/AI containers, the base image needs more than Alpine. Use a Python slim image or Alpine with compiled wheels for numpy, opencv-python-headless, tflite-runtime, and av (PyAV).
- Install `opencv-python-headless` (not `opencv-python`) to avoid pulling in GUI dependencies (Qt, GTK) that add hundreds of MB and are useless in containers.
- Pin exact package versions in requirements to ensure reproducible builds on ARM64. Cross-compilation for ARM64 from x86 may need `--platform linux/arm64` and qemu or native builders.
- Keep TFLite model files in a `/app/models/` directory. Include a `README.md` in the models directory documenting model architecture, input/output format, quantization, and source URL.
