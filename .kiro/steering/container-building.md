---
inclusion: auto
description: Workflow and conventions for building Docker containers for Cradlepoint NCOS routers
---

# Container Building for Cradlepoint NCOS Routers

When a user asks you to build, create, or develop a container for a Cradlepoint/Ericsson router, follow this workflow:

## Phase 1: Understand Requirements

1. Read the relevant documentation before writing any code:
   - `#[[file:docs/container-development-guide.md]]` — practical development guide
   - `#[[file:docs/ncos-sdk-reference.md]]` — cp.py SDK API reference
   - `#[[file:docs/memory-resources.md]]` — memory and storage constraints
   - `#[[file:docs/containers-quick-start.md]]` — deployment and compose format
   - `#[[file:docs/containers-advanced-config.md]]` — networking, volumes, devices, health checks

2. Clarify with the user:
   - What the container should do
   - Which router model(s) it targets (determines architecture and memory)
   - Whether it needs Config Store access (cp.py / cs.sock)
   - Whether it needs a LAN IP address (custom network) or just port mapping on the default bridge
   - Whether it needs USB devices or shared volumes
   - Any specific NCOS version requirements

## Phase 2: Build the Container

Follow these conventions established in this repo:

1. **Dockerfile**:
   - Use `alpine:latest` as the base image unless there's a specific reason not to
   - Install only necessary packages with `--no-cache`
   - Copy application files to `/opt/<app_name>/`
   - Set `PYTHONPATH` if using cp.py
   - Use an `entrypoint.sh` script for initialization logic
   - Expose only necessary ports with protocol (e.g., `EXPOSE 1161/udp`)

2. **Python applications using cp.py**:
   - Copy `cp.py` from the repo root or SNMP_agent example
   - Use `cp.get()`, `cp.put()`, `cp.log()` etc. for router communication
   - Use `cp.get_appdata()` for user-configurable settings
   - Use `cp.wait_for_uptime()` and `cp.wait_for_wan_connection()` at startup if needed

3. **Entrypoint script**:
   - Use `#!/bin/sh` (Alpine uses ash, not bash)
   - Perform any config generation or initialization
   - Use `exec` for the final command to ensure proper signal handling

4. **Architecture**:
   - ARMv7 32-bit: AER2200, IBR1700
   - ARMv8 64-bit: E300, E3000, R920, R980, R1900, R2100

## Phase 3: Key Constraints to Remember

- **Bridge networking only** — Host networking is not supported. Containers use bridge mode by default. To give a container its own IP on a LAN, define a custom Compose network bound to a Local IP Network via `com.cradlepoint.network.bridge.uuid` in `driver_opts`, with matching `subnet`/`gateway` in `ipam`. The container can then be assigned a static IP via `networks.<name>.ipv4_address`.
- **No host filesystem mounts** — only named volumes, Config Store (`$CONFIG_STORE`), and USB storage (`$USB_STORAGE`)
- **User namespace remapping** is active — file ownership can change to `nobody:nobody`
- **Memory is limited** — especially on AER2200/IBR1700 (as low as 135 MB)
- **Flash storage is limited** — 6-14 GB total, keep images small
- **Compose version 2.4** is the standard format (use `mem_limit` for memory, not `deploy.resources`)
- **Config Store** access requires the `$CONFIG_STORE` volume in Compose YAML (bare, no mount path). Without it, all `cp.py` calls return `None`.
- **Volumes are not updated** when a new image is deployed — new project needed for fresh volumes
- **FAT32** for USB storage — 32GB max partition, 4GB max file

## Phase 4: Review the Reference Examples

### SNMP_agent/ — Simple daemon pattern

The `SNMP_agent/` directory is a reference for simple long-running daemons:
- `Dockerfile` — Alpine base, minimal packages, entrypoint pattern
- `entrypoint.sh` — Config generation then exec into main process
- `gen_conf.py` — Reading router config via cp.py to generate app config
- `ncos_snmp.py` — Long-running daemon using cp.py for data
- `cp.py` — The SDK module (copy into new containers)

### edge-ai/ — Computer Vision / AI pattern

The `edge-ai/` directory is a reference for complex multi-threaded applications with video processing, AI inference, and web UIs:
- `cp.py` — Full NCOS SDK (singleton CSClient, EventingCSClient, appdata helpers, logging)
- `src/main.py` — Entry point: signal handlers, component initialization, thread orchestration, graceful shutdown
- `src/config.py` — Configuration via `cp.get_appdata()` / `cp.put_appdata()` with full validation and self-provisioning defaults
- `src/capture.py` — RTSP capture via PyAV with TCP transport, frame skipping, disconnect detection, and exponential-backoff reconnection
- `src/inference.py` — TFLite inference engine supporting SSD MobileNet V2 and YOLOv5n, pre-allocated buffers, NMS, thread-safe threshold updates
- `src/annotation.py` — OpenCV-based bounding box drawing with confidence color-coding, FPS overlay, rolling FPS calculator
- `src/processor.py` — Pipeline orchestrator: capture→infer→annotate with adaptive rate control, inference frame skipping, double-buffer frame sharing
- `src/web_server.py` — MJPEG streaming, REST API (stats/config/control), multi-user session control, static file serving
- `src/models.py` — Dataclasses: Detection, AppConfig, RuntimeStats
- `src/templates/index.html` — Self-contained web UI (no CDN dependencies)
- `models/` — TFLite model files (INT8 quantized for ARM64 XNNPACK)

Key patterns to study in edge-ai:
1. **Multi-threaded pipeline** with `threading.Event` for shutdown coordination
2. **Appdata-driven config** that self-provisions defaults on first run
3. **RTSP reconnection** with exponential backoff (2→4→8→...→60s cap)
4. **Performance optimization**: pre-allocated buffers, NEON SIMD via OpenCV, frame skipping, annotation skipping when no clients connected
5. **Adaptive rate control**: auto-reduces FPS when inference is too slow, restores when latency recovers
6. **MJPEG streaming** via Python's built-in http.server with ThreadingMixIn
7. **Primary-user session** control for multi-viewer scenarios
