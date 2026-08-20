---
inclusion: auto
description: General lessons learned from building containers for Cradlepoint NCOS routers
---

# Lessons Learned

This file captures general lessons learned from building containers for Cradlepoint NCOS routers. It is updated after each container build via the reflection hook. Only general-purpose improvements are recorded here, not project-specific details.

## Initial Lessons

- Alpine Linux `ash` shell does not support bash-isms like arrays or `[[ ]]`. Use POSIX-compatible shell syntax in entrypoint scripts.
- ~~The `py3-requests` Alpine package is needed if cp.py is used~~ **Superseded 2026-08-14:** `cp.py` was rewritten as a standard-library-only Config Store client. Do not add `py3-requests` for it.
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
## 2026-08-14 — Design-phase lessons (no build performed)

These came out of scoping a new sample rather than running one on hardware. Items marked UNVERIFIED have not been confirmed against a router and should be tested before being relied on.

### Multi-process containers

- Alpine's `ash` has no `wait -n`, so the common "block until any child exits" idiom does not work in entrypoint scripts. When a container must run two processes (an apk daemon plus a Python helper, for example), use a POSIX polling supervisor: `while kill -0 "$A" 2>/dev/null && kill -0 "$B" 2>/dev/null; do sleep 5; done` followed by a non-zero exit, paired with `restart: unless-stopped`.
- Never background one process and `exec` the other. The backgrounded process can die silently while the container still reports running, so the restart policy never fires and the failure is invisible.
- Add a `trap term TERM INT` handler that kills both children, otherwise container stop waits out the timeout and ends in `SIGKILL`.
- Default to one process per container. Reach for a supervisor only when the processes must share a loopback interface or filesystem.

### Compose interpolation

- The platform interpolates `$` in Compose values — that is the mechanism behind `$CONFIG_STORE` and `$USB_STORAGE`. Any other `$` is treated as a variable reference and expands to an empty string. Escape literal dollar signs by doubling them (`$$SYS/broker/uptime`, `$$USER`). This affects `command`, `entrypoint`, `environment`, and `healthcheck` alike.

### Health checks

- `test: ["CMD", ...]` is exec form: no shell, so `||`, `&&`, pipes, and redirects become literal arguments to the binary. Use `["CMD-SHELL", "cmd || exit 1"]` when shell operators are needed.
- The health check binary must be present in the image. `curl` is not in `alpine:latest`; either `apk add --no-cache curl` or write the check using tools the image already ships (many network packages include their own client, e.g. `mosquitto-clients`, `net-snmp-tools`).

### Choosing what to build

- Before proposing a new sample, inventory the patterns the existing samples already cover and pick one that fills a gap. As of this entry the repo covers "apk daemon with a generated config file read from the Config Store" (`SNMP_agent/`) and "multi-threaded Python app with appdata config and a web UI" (`edge_ai/`). Neither demonstrates `cp.put()` writing router config, so that is the highest-value addition.
- ~~Polling with `cp.get()` and event registration with `cp.register()` are complementary~~ **WRONG, corrected 2026-08-14:** `cp.register()` does not work from a container at all — config store event subscriptions need the event socket, which containers cannot access. Polling is the only option. Do not design around event registration, and do not recommend it as a differentiator for a sample.
- When a sample writes to the Config Store, gate it behind an allowlist of permitted paths and an appdata flag defaulting to off. A sample that can be driven to `cp.put()` arbitrary paths from the network is not a sample worth copying.

### Open question (UNVERIFIED)

- Whether a multi-service Compose project gets service-name DNS on a project default network is unconfirmed. Every example in the docs sets `network_mode: bridge` per service, which places the container on the default Docker bridge and would defeat name resolution between services. Until this is tested, assume services cannot resolve each other by name and either co-locate cooperating processes in one container or communicate over an explicit custom network bound to a Local IP Network UUID.
## 2026-08-14 (second entry) — Check native capability before designing

### The mistake worth not repeating

- A container recommendation was made and fully sketched before establishing whether NCOS already provided the service natively. It did (MQTT broker / mosquitto runs on the router), so the design was wasted. **Confirming native capability is a Phase 1 gate, not a detail to check later.** See the "Before You Build" section in `docs/container-development-guide.md` for how to check and for the running list of confirmed native services.
- "NCOS already does X" reframes an idea rather than always killing it. A container is still justified when it fills a genuine gap, translates a native capability into a protocol the native feature does not speak, works around a specific defect in the native implementation (this is why `SNMP_agent/` exists), or adds behavior the native feature lacks entirely such as buffering through a WAN outage. When a sample overlaps a native feature, its README must state what the native feature cannot do.
- When a recommendation rests on an assumption about platform capability, state the assumption explicitly and invite correction before writing code. Surfacing "this dies if X is native" costs one sentence; discovering it after implementation costs the whole build.

### Architecture smell

- If a design requires a multi-process supervisor, re-examine whether every process belongs in the container. In this case the need for a supervisor was a symptom of bundling a daemon that should not have been there at all. The supervisor pattern is still correct when genuinely needed, but it should prompt the question rather than being reached for reflexively.

### Adapting Config Store data for off-the-shelf daemons

- The recurring container shape in this repo is an unmodified apk daemon plus a Python adapter that supplies it data from `cs.sock`. Connect them with a **loopback TCP/UDP socket**, not a synthetic device file. Most daemons accept a `tcp://` or `udp://` source, no privileges are required, and each side can restart independently. FIFO and pty imitation of serial devices is fragile — daemons reject things that fail their device probe, and FIFO blocking-open semantics create startup-order deadlocks.
- Adapters must propagate validity rather than fabricate it. When router state goes stale or unavailable, emit the target protocol's explicit invalid/no-data representation instead of repeating the last known value. Silent stale data is worse than a visible error for anything consuming it downstream. This applies to any re-serving of router state — location, WAN counters, client tables.

### Documentation hygiene

- Use generic placeholders (`mydaemon`, `/etc/mydaemon/`) in documentation examples rather than a real application name. A specific name in an example reads as an endorsed choice and propagates it into future work. Two doc snippets written earlier the same day had to be genericized for exactly this reason.

### Security framing for exposed services

- Weigh port mapping against data sensitivity, not just reachability. Mapped ports are exposed on WAN with no firewall filtering, so any service publishing location, credentials, telemetry, or client inventory should be placed on a Local IP Network instead. Say why in the sample's compose comments so the reader does not "simplify" it back to a `ports:` mapping.

## 2026-08-14 (third entry) — First build actually run and verified

Findings from building a container and running it, rather than from reasoning about one. Everything below was observed.

### cp.py error handling (the big one)

- **`cp.py` swallows its own errors.** Reads log and return `None`; writes log and return normally regardless of outcome. Nothing raises. Any code that relies on exceptions to surface Config Store problems will silently misreport success. Full contract now documented in `docs/ncos-sdk-reference.md`.
- A missing `$CONFIG_STORE` volume is **indistinguishable from empty data** — both give `None`. Probe a path that must always exist (`status/product_info`) at startup and periodically, log the difference, and surface it in any status API or UI. Without this, a deployment missing the volume looks exactly like a working application whose data source is idle. This cost real debugging time to notice even while running the container locally.
- **Verify writes by reading back** whenever the outcome is reported to a user or relied on for persistence. `cp.put_appdata()` returned cleanly with no Config Store attached, so the first version of the API reported `persisted: true` for a write that could not possibly have landed.
- Before `cp.put()` to a `config/...` path, read it first. `None` means the path is absent on this firmware, and a blind write there is undetectable afterwards.
- `cp.py` has a cosmetic defect in several error paths: plain strings where f-strings were intended, so logs show literal `{name}` and `{e}` placeholders. Harmless, hides the real error text, appears in volume when the Config Store is unreachable, and exists in every vendored copy. Do not chase it as an application bug.

### Verification is cheaper than expected

- The whole container can be built and run on a development machine before it ever reaches a router. `docker buildx --platform linux/arm64` plus `--load` then `docker run` works, natively on an ARM Mac. This caught nothing catastrophic but confirmed package availability, startup order, signal handling and endpoint behaviour in minutes.
- **Build both architectures every time.** A package present for arm64 is not guaranteed to exist for arm/v7, and the most constrained routers (AER2200, IBR1700) are arm/v7.
- Test `docker stop` timing. A prompt exit proves PID 1 forwards signals; taking the full timeout means the container is being SIGKILLed and the entrypoint's trap or `exec` is wrong.
- Running locally exercises the "router unreachable" path for free, since there is no `cs.sock`. Treat that as a required test case rather than an artifact of the dev environment.
- **Validate generated wire formats against the real consumer**, not just against a checksum or a unit test. Formatting can be well-formed and still wrong; the consuming daemon is the only authority on whether the output means what was intended.
- **When a check fails, verify the check before suspecting the code.** Two failures during this build were bugs in the test itself: a hand-computed expected value for a concave polygon, and shell escaping that mangled a JSON payload into something the daemon rejected. Both would have sent me looking for non-existent application bugs.

### Container behaviour under NCOS constraints

- Many network daemons **bind `127.0.0.1` by default** and need an explicit flag to listen on all interfaces. Symptom is a published port that appears completely dead while the process is running and the mapping is correct. Confirm with `netstat -ltn` inside the container. The inverse is a feature: internal seams between co-located processes should bind loopback so they can never be reached from the network.
- In a supervised multi-process container, the **health check must cover the process that is not PID 1**, for example by having the application's health endpoint connect to the daemon's port. Otherwise the daemon can die while the container reports healthy, which defeats the point of the supervisor.
- **User namespace remapping affects SysV IPC, not just file ownership.** Daemons using shared memory may fail to remove their segments at shutdown, logging `shmctl(...) for IPC_RMID failed, Operation not permitted`. Benign; verify the daemon's actual service instead of chasing it.
- `python3 -c` makes a good health check binary in Alpine images, since `curl` is absent but Python is usually already installed for `cp.py`: `["CMD", "python3", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=5).status == 200 else 1)"]`.

### Measured sizes

- Alpine 3.18 with `python3`, `py3-requests`, a small network daemon plus its client tools, and application source: **60.6 MB (arm64) / 48.1 MB (arm/v7)**, 62 and 50 MiB installed respectively, running comfortably under `mem_limit: 64M`. Recorded in `docs/memory-resources.md` as a planning baseline. Measure and report sizes rather than estimating them.

### Base image pinning

- The workflow previously said to use `alpine:latest`, which contradicted both existing samples (`alpine:3.18`) and reproducibility. Router images get rebuilt months apart, and an unpinned base silently changes the Python version and package set between builds. Steering now says to pin.

### Browser-facing UIs on routers

- Anything the browser fetches from a third party (map tiles, fonts, scripts from a CDN) travels over the **client's** internet path through the router, not the router's own. It works when the LAN has WAN access and fails silently otherwise. Vendor all assets the page needs, make external asset URLs configurable, and design a degraded mode that still functions when they fail.
- Prefer writing a small amount of vanilla JS over vendoring a minified library into a sample repo. Bundled third-party code cannot be reviewed by the reader and obscures the part of the sample that is meant to be instructive.

## 2026-08-14 (fourth entry) — Rewriting the vendored SDK

### Corrections to earlier entries in this file

- **`cp.register()` does not work from a container.** Config store event subscriptions require the event socket, which containers cannot access, and `cp.alert()` requires the on-router SDK application context. Earlier entries recommended event registration as both a design pattern and a differentiator for a new sample; that advice was wrong and has been struck out above. A container cannot be event-driven off router state — every "react when X changes" behaviour must be built from polling plus comparison against the previous sample. Recorded as a platform constraint in `docs/container-development-guide.md` and in Phase 3 of the workflow.
- **`py3-requests` is no longer needed.** It was only ever required because the vendored `cp.py` imported `requests` for its HTTP transport. The rewritten module is standard library only. Struck out above.
- The lesson-file convention: when an entry turns out to be wrong, strike it through in place with a dated correction rather than deleting it. A future reader needs to know the advice was tried and failed, otherwise the same conclusion gets re-derived. Appending a correction at the bottom is not enough — the wrong line is what gets read.

### Fixing a shared dependency vs documenting around it

- When a vendored dependency's error handling is the repeated source of debugging pain, fixing the dependency can be cheaper than documenting the workaround in every caller. The prior entry's advice (probe `status/product_info`, verify writes by read-back) was correct but had to be re-implemented per container; folding it into the module made every caller correct by default.
- **Fix bugs, preserve contracts.** Converting `None`-returning accessors into raising ones would have been a breaking change across every sample and any copied code, and it is the wrong contract for a long-running poller where a transient socket timeout should not kill the process. The valuable changes were additive or bug fixes: make diagnostics available, verify writes internally, repair error messages. Resist the urge to "improve" a shared API's semantics while fixing its defects.
- Two defects in this class are worth looking for in any wrapper around a socket protocol: chained attribute access on a result that can be `None` (`_dispatch(cmd).get('data')` raised `AttributeError` on failure, which an outer handler then reported as something unrelated), and regex `.group(0)` called without checking the match (a truncated response raised instead of reporting). Both destroy the real error before it reaches a log.
- Throttle repeated failure logs in anything that polls. Logging every failure means a missing volume emits one line per poll forever; log the first, then every Nth.

### Testing a socket protocol client without hardware

- A mock `AF_UNIX` server plus an overridable socket path constant makes Config Store logic fully testable on a development machine: exact command bytes per verb, response unwrapping, appdata round-trips, malformed and truncated replies, receive timeouts, and the missing-socket path. Worth building before hand-verifying anything against a router.
- Make the socket path a module-level constant rather than a hardcoded literal, specifically so it can be overridden in tests.
- Test against real crypto and real published values where they exist, not self-generated expectations. A checksum computed against a documented example sentence and a PBKDF2 hash generated with the documented parameters both catch mistakes that self-consistent tests cannot.

### Vendored copies

- A Docker build cannot reach outside its build context, so each service directory needs its own copy of shared modules. Keep one canonical copy at the repo root and make the per-sample copies byte-identical; verify with a checksum rather than assuming. Two of three copies had already drifted before this change.
- After replacing a shared module, check every consumer's call sites resolve against the new API. An AST walk for `module.attr` references compared against `dir(module)` catches this across a repo in seconds and is more reliable than grep.

### Documentation that describes code

- When replacing a module, rewrite the documents that describe its API in the same change. A reference doc listing functions that no longer exist is worse than no doc. Grep for the module name across `docs/` and steering before declaring the work finished.
- Audit why each apk package is installed when touching a Dockerfile. A dependency inherited from a vendored file may have no other consumer; dropping one here removed 10 packages and ~2.5 MB from every image.
- When wrapping a documented API path, cross-check the different doc pages describing it. Contradictions between pages are common (one page's "higher is preferred" against another's example implying the opposite). Surface the contradiction rather than silently picking one interpretation and encoding it in code.

## 2026-08-14 (fifth entry) — Resolving a documentation contradiction

Small task, correspondingly small set of lessons, but the investigation technique generalises to any container that writes router config.

### Resolve config semantics from the DTD, not from prose

- The DTD is the authoritative source for a config field's type, constraints and meaning. `docs/ncos-api/config/dtd/` holds offline snapshots per model and NCOS version, so this can be settled without a router.
- **When a DTD comment is ambiguous or absent, the shipped defaults are the strongest available evidence.** They encode the behaviour the firmware actually intends. In this case a field documented only as "Failover and failback priority" was resolved by reading the default set and noticing it was ordered wired-before-cellular and 5G-before-3G, which only works one way round.
- **Field semantics are per-path, not global.** The same field name can mean the opposite thing in a different config section, confirmed by the DTD's own comments running in both directions for `priority`. Never carry a field's meaning from one section to another; check the comment and defaults for the exact path being written. Recorded in `docs/ncos-api/dtd-usage.md` with a checklist.

### Grep loses the context that matters in nested JSON

- Grepping a large JSON schema for a field name returns matches without telling you which section they belong to. My first search surfaced comments that looked authoritative but belonged to unrelated sections, and taking them at face value would have produced the opposite (wrong) conclusion.
- Walk the structure with a path-tracking generator and filter on the full path instead. It answers "what does this field mean *here*" in seconds, which grep cannot. Snippet in `docs/ncos-api/dtd-usage.md`.

### When two documents disagree

- Do not resolve a contradiction by picking the reading that matches code already written. That is the path of least effort and it silently encodes a guess as fact. Find independent evidence first, and state what the evidence was so the next reader can check the reasoning rather than trusting the conclusion.
- A "fix the doc" request usually means fix the *claim*, wherever it appears. This one was stated or implied in four places: the reference page, an overview README example, an SDK reference table, and a docstring in shared code. Grep for the claim, not just the file that was reported, then re-sync vendored copies and verify checksums match.
- Contradictions between doc pages are worth actively hunting when wrapping an API path in code, because writing the wrapper is the moment the ambiguity has to be resolved anyway. This one surfaced only because a helper needed to decide a sort order.

## 2026-08-14 (sixth entry) — Testing a claimed limitation, and dev-host credentials

Two pieces of work: an experiment to settle whether a documented platform
limitation is real, and a credentials system so a development router can be
driven from a workstation. The lessons about *evidence* below are the important
ones.

### Unverified claims presented as fact (the big one)

- **This file, and two documents, asserted a platform limitation that nobody had
  ever tested.** The claim carried a specific mechanism ("requires the on-router
  SDK application context"), which made it read as established fact. No test was
  cited anywhere. A feature request was then scoped as a *workaround* for that
  limitation — an extra outbound delivery mechanism — before anyone asked whether
  the limitation existed. That is the expensive failure mode: an untested claim
  in your own docs silently redirects a design toward a workaround.
- **A dated correction needs evidence too.** The claim was recorded in this file
  as a struck-through correction of an earlier entry. Striking out a wrong line
  and replacing it with another untested assertion does not improve the file, it
  launders a guess into an authoritative-looking one. Corrections earn authority
  from evidence, not from being newer.
- **Separate observed from assumed when writing docs.** Anything not actually
  run should say so. Marking a claim UNVERIFIED costs one word and preserves the
  ability to re-examine it; stating it flatly destroys that. Both affected doc
  pages are now annotated.
- **New gate: when a design is shaped by a limitation, check the limitation was
  tested.** If a workaround, an extra dependency, a different architecture, or a
  dropped feature is being justified by "the platform cannot do X", find the
  evidence for X before building around it. A single-purpose probe container is
  cheap — a few hours from empty directory to deployable, most of it verifiable
  on the development machine.
- **Do not infer a causal mechanism from a plausible-looking field name.** While
  investigating, a config field whose name matched the hypothesis was found, with
  a default that fit the story, and it was nearly built into a design as the
  explanation. Field existence plus a suggestive name is not evidence of a
  causal role in a subsystem you have not traced. The DTD is authoritative for
  what a field *is* (type, constraints, default); it says nothing about what
  other behaviour that field gates.

### Designing a probe rather than a feature

When the deliverable is "does X work", the artifact is an experiment, and
experiments have their own requirements. These generalise to any future probe:

- **Do not test through a wrapper that is already stubbed out.** Calling the
  library function that logs "not supported" and returns `None` tests nothing.
  Go at the underlying protocol directly.
- **Do not reuse the shared dispatch helper.** Ours returns `{}` on failure and
  swallows the exception text, which is correct for a long-running poller and
  useless for a probe. A probe needs raw bytes, raw status, and the verbatim
  error. Write a local transport for it.
- **One unique marker per variant.** When trying several candidate message
  layouts, each must carry a distinct identifier. Identical payloads mean a
  single hit in the external system cannot be attributed to a specific variant,
  which wastes the whole run.
- **Include a known-good baseline through the same transport.** Sending a verb
  you know works, alongside the one under test, separates "the verb was
  rejected" from "the transport is broken". Without it an empty response is
  uninterpretable.
- **Record the preconditions that make a negative result meaningful.** If the
  router is not currently connected to the management plane, "nothing arrived"
  proves nothing. Capture and log that state next to the result, and warn when
  it invalidates the run.
- **A probe with external side effects must not restart-loop.** Use
  `restart: "no"`, do the work once, then idle so logs stay retrievable. A
  restart policy on a container that emits to an external system floods it.
- **Emit a human summary and a machine-readable line.** The summary is for the
  person correlating with another system; the single JSON line is what gets
  pasted back for analysis.
- **State which half of the result you cannot see.** When confirmation lives in
  someone else's console, say exactly what to look for and what each possible
  outcome would mean, before they go looking.

### Verification technique

- **To exercise signal handling with a stubbed dependency in-container, keep the
  application as PID 1:** `sh -c 'helper & sleep 2; exec app'`. If the harness
  or the mock is PID 1, the `docker stop` timing test measures the harness's
  signal handling, not the application's, and passes while the real entrypoint is
  broken.
- **Running a mock dependency inside the image is more reliable than bind-mounting
  one from the host.** It follows the existing "run it inside the built image"
  rule, and avoids depending on host-to-container Unix socket bind mounts, whose
  behaviour varies by platform (UNVERIFIED that they fail; deliberately avoided).
- **Host-side tests of sample code need the vendored module directory on
  `PYTHONPATH` as well as `src/`.** Shared modules live at the sample root and
  only land beside the source at image build time, so a test that imports the
  application will fail with `ModuleNotFoundError` until both directories are on
  the path. Cost a wasted test run.
- **`cp.APP_NAME` is derived from `basename(os.getcwd())`**, which is empty at
  `/`, so every log line in an image without a `WORKDIR` is prefixed with the
  generic `container:`. Set `CP_APP_NAME` in the Dockerfile. It also matters
  whenever the value is used as protocol data rather than just a log prefix,
  because then the payload depends on the working directory. Now documented in
  `docs/ncos-sdk-reference.md`.

### Secrets in a repo

- **Fix and verify the ignore rule before creating the file that will hold the
  secret.** Ordering is the whole control. Verify with `git check-ignore -v` and
  a throwaway file containing a dummy value, not by reading the pattern and
  assuming.
- **Verify the committed template is *not* ignored.** A broad pattern such as
  `.env.*` silently swallows `.env.example`, so the template never gets
  committed and the next person has nothing to copy. Needs an explicit negation
  (`!.env.example`), and needs checking.
- **Never put credentials in a command line.** `curl -u user:pass` and
  `sshpass -p secret` are visible to every local user via `ps`. Do HTTP
  in-process instead, and pass SSH passwords through the environment
  (`sshpass -e`). If a tool cannot avoid it, print the command for the operator
  rather than handling the secret carelessly.
- **Redact `__repr__` on any object holding a secret.** A default dataclass repr
  prints the password into every traceback, debugger frame and stray `print`
  that touches the object. Also give it a `describe()` that reports `set` /
  `NOT SET` so status output is useful without being dangerous.
- **Let real environment variables override the file.** It costs a few lines and
  lets a password manager or CI inject the value without it ever reaching disk.
  Report which source each value came from, so a stale file that is shadowing an
  export is obvious.
- **Create the file mode 600 and warn when it is broader.** Include the exact
  `chmod` in the warning.
- **Do not strip inline comments when parsing dotenv files.** `#` is an ordinary
  password character; treating it as a comment mid-line truncates the secret and
  surfaces as a baffling auth failure. Treat `#` as a comment only at the start
  of a line, and strip at most one matching outer quote pair. Avoid
  `configparser` for this, since its interpolation mangles `%` and `$`.

### Configuration that fails silently is a recurring bug class

- **A tool that falls back to hardcoded defaults when unconfigured converts
  "you have not set this up" into "the target is unreachable".** One inherited
  script defaulted to a hardcoded IP and an empty password, so a missing config
  file produced a connection error against a router the operator never intended
  to contact. Fail loudly, name the variables that are unset, and state the fix.
- This is the same family as the Config Store lesson already in this file
  (a missing volume being indistinguishable from absent data). Worth naming as a
  general rule: **an absent configuration must never be able to masquerade as a
  runtime failure.** Whenever a default is supplied for something that identifies
  a target or authenticates to it, that default is a bug.

### Inherited references are not conventions

- **A reference to a file is not evidence the file exists.** Two places in the
  repo referenced a settings file that was not present anywhere. That was read
  as an established convention and taken as the basis for a design; it was
  actually leftover from an unrelated project. Confirm a referenced path exists
  before building on it — one `ls`.
- **When something is confirmed to be leftover, remove the references rather
  than adding a second mechanism beside them.** Two competing config mechanisms
  is worse than either. Grep for the old name and finish the job, including in
  sample READMEs.
- Related self-inflicted cost: hardening around the leftover name (adding it to
  `.gitignore`) before confirming it was part of the system, which then had to be
  undone. Confirm first, harden second.

### Host-side access to a development router

- The repo now has `tools/dev_router.py` plus a gitignored `.env`
  (see `.env.example` and `tools/README.md`) for driving a dev router from a
  workstation: REST for `config/`, `status/` and `control/`, appdata read/write
  with read-back verification, and SSH for the CLI-only `container` commands.
  Use it instead of hand-rolling curl, and extend it rather than adding another
  client.
- **REST wraps replies as `{"success": true, "data": ...}` while the SDK returns
  data directly.** Host-side tooling should unwrap so call sites match container
  code, and should raise on `success: false` rather than returning `None`, so a
  rejected path cannot be mistaken for an empty one.
- `container list`, `container logs` and `container exec` have no REST
  equivalent, so any host-side workflow that inspects a running container needs
  an SSH path as well as an HTTP one.
- TLS: routers ship self-signed certificates, so certificate verification has to
  be off by default for a LAN dev router. Say so explicitly in the tool and the
  docs, rather than leaving the reader to assume HTTPS means authenticated.

## 2026-08-14 (seventh entry) — First deployment driven from a workstation

Deploying to a real router for the first time, rather than reasoning about it.
Everything below was observed. The first item is a mistake I made twice in one
session in slightly different forms, so it leads.

### A zero-result search is evidence about your query, not about the corpus

- I assumed a config path (`config/system/container`), grepped the docs for that
  exact string, got no hits, and concluded the path index was incomplete. Then I
  probed four variants over REST, each returning `null`, which reinforced the
  wrong conclusion. **The path was documented all along** — it is
  `config/container`, and the index had all sixteen of its paths. Grepping for
  the distinctive leaf token (`container`) would have found it in one search and
  saved the entire detour.
- Generalised: when a search for a name you guessed comes back empty, the
  cheapest explanation is that the guess is wrong. Search for the **leaf token**,
  not the full constructed path, before concluding anything is missing. This is a
  variant of the "verify the check before suspecting the code" lesson already in
  this file, and it recurred because a *negative* result feels like information
  about the thing being searched rather than about the search.
- I nearly committed a "fix" adding paths that were already present, and a
  lessons entry claiming the generator was buggy. Re-running the generator and
  diffing took thirty seconds and prevented both. **Verify a documentation defect
  before fixing it**, exactly as you would a code defect.
- **UI structure is not tree structure.** The NCM UI presents containers under
  SYSTEM, but the config path is `config/container`. Menu hierarchy is not a
  reliable guide to the config tree.

### `null` is as ambiguous as `None`

- Over REST, reading a nonexistent config path returns `null`, indistinguishable
  from a path that exists and holds nothing. Guessing paths therefore yields
  confident wrong conclusions rather than errors. Resolve the path from
  `docs/ncos-api/config/PATHS.md` or the DTD first. Same failure family as the
  Config Store `None` ambiguity already recorded here — the platform never
  distinguishes "absent" from "empty" anywhere, so never let a design or a
  diagnosis depend on telling them apart from a single read.

### The deploy loop is fully scriptable

- **Compose YAML is a plain string field in a config array**, so container
  projects can be created and updated over REST or the SDK without touching the
  NCM UI. The whole build → push → deploy → read-logs cycle can be scripted,
  which is dramatically faster than the UI for iteration. Documented in
  `docs/ncos-api/config/container.md`; the workflow now has a Phase 2c for it.
- **Read an existing project's compose string before writing your own.** It is
  direct evidence of what the firmware has already accepted, and beats any
  example in documentation.
- The form-encoded `data=` REST write format documented in
  `docs/ncos-api/config/README.md` is correct — writes landed and verified by
  read-back. Read-back verification remains the only trustworthy confirmation.
- An **empty registry array means anonymous Docker Hub pulls**, which is all a
  public image needs. Check `config/container/registry` and an existing project's
  `image:` before assuming a registry must be configured.
- Compose details confirmed on hardware: quote `restart: "no"` (bare `no` is
  boolean false in YAML 1.1), and set `container_name` so `container logs <name>`
  is predictable rather than a derived guess.

### Do not trust a system's own diagnosis of itself

- The container engine logged `High system CPU load? Unable to get container
  stats` while the router was idle — CPU around 8%, one-minute load average 0.16,
  1.25 GB memory available. That message is a **guess the engine makes whenever a
  call times out**, not a measurement. Taken at face value it points at load
  contention that does not exist, and the obvious "fix" is disabling an unrelated
  workload for no reason.
- Always verify a resource claim against `status/system` (`cpu`, `load_avg`,
  `memory`) before acting on it. More generally: a component reporting on the
  cause of its own failure is a hypothesis, not evidence.
- Symptoms of a wedged container engine, distinct from resource exhaustion:
  a `containerd` `DeadlineExceeded` line in `status/log` (observed on this
  router's firmware naming its containerd process with a `balena-` prefix —
  the on-router engine is `cpdockerengine`, historically balena-derived, but
  whether that ancestry still holds on current firmware is unconfirmed and
  shouldn't be assumed for a different router or firmware version), a project
  listed in `container list` with no containers under it, and a
  `status/container` read that hangs rather than returning. A hanging status
  read is a health signal, not a slow path or a wrong one.

### Reading router state

- Prefer REST for structured status reads and SSH for CLI-only commands. `cat
  /status/log` over SSH returns content that tooling treats as binary; the same
  data over REST parses as JSON cleanly (entries are
  `[timestamp, facility, level, message]`).
- Verify licensed entitlements from `status/feature` before debugging why
  something will not deploy. Container Orchestration is licensed separately, and
  confirming it is present is faster than inferring it from a failure.

### Pushing images is a shared-system action

- A registry push writes to an account someone else owns, and a public repository
  is world-visible and effectively permanent. **Confirm the namespace and
  intended visibility before pushing**, even when the target is obvious from an
  existing deployment. I inferred the namespace from a container already on the
  router and pushed on that basis; it was almost certainly right, but it was an
  inference about someone else's account, and it should have been a question.
- Same principle for the router: creating a project is additive and low risk, but
  modifying or disabling an existing workload affects something the owner may be
  using. Add alongside; ask before touching what is not yours.

## 2026-08-14 (eighth entry) — Two failure signatures behind one symptom

Short session, one substantive lesson. It refines the previous entry rather than
adding to it: what I recorded there as "the" symptom of an unhealthy container
engine turns out to be shared by two distinct failures with different fixes.

### Discriminate before diagnosing

- A project listed in `container list` with no containers under it has at least
  three causes: the pull is still running, the engine is **wedged**, or the engine
  is **not running at all**. The previous entry described only the wedged case.
- The discriminators are cheap and worth using every time:
  - **Wedged** — a `status/container` read hangs or times out, and `status/log`
    carries `containers`-facility lines including
    `daemon is not responding ... DeadlineExceeded`. A reboot clears it.
  - **Not running** — `status/container` returns `null` *promptly*, and there are
    **zero** `containers`-facility lines in the log.
  - A prompt `null` and a hang look similar in a script that only checks for
    truthy data. They mean opposite things about the subsystem.
- **Absence of log output from a facility is a positive signal, not missing
  information.** Counting facilities in `status/log` shows which subsystems are
  alive, and a facility that produced hundreds of lines yesterday and none today
  is strong evidence in itself. This generalises well beyond containers.
- **A CLI command answering normally is not evidence the subsystem is healthy.**
  `container list` reads project config, so it reports projects perfectly while
  the engine is dead. Check what a command's data source actually is before
  treating its success as a health signal.

### Verify the platform before debugging your artifact

- I polled for containers for three minutes and was about to re-examine the
  compose file when the real finding was that nothing was attempting the pull.
  **When the platform is the variable, time spent on your own artifact is
  wasted.** Establish that the subsystem is alive first, then debug your build.
- Config and entitlement surviving an event does not imply the subsystem did.
  After a firmware upgrade, project config and the licensed feature entry were
  both intact while the engine was absent — so neither is evidence of health.
  Check the thing you actually depend on, not its neighbours.
- Cause of the post-upgrade engine absence is UNVERIFIED and recorded in
  `docs/containers-quick-start.md` as a hypothesis only. Resisting the urge to
  write down a plausible mechanism as fact is the whole point of this session's
  earlier lessons; a guess in a troubleshooting table is read as an answer.

### Deployment cost on a metered WAN

- Check the primary WAN before pulling an image to a dev router:
  `status/wan/primary_device` beginning `mdm-` means the pull runs over cellular,
  where tens of megabytes is both slow and billable. Worth knowing before
  wondering why a pull is taking a long time, and worth factoring into how often
  a rebuild gets pushed during iteration.

### Shell note for this repo's environment

- zsh performs brace and glob expansion inside `python3 -c "..."`, which mangled a
  one-liner containing `{...}` and `!r` into an error about "no matches found".
  Use a quoted heredoc (`python3 - <<'PY'`) for any inline script that contains
  braces, `!`, or globs. Cost one wasted command; the heredoc form has been
  reliable throughout.

## 2026-08-15 — RESULT: the alert verb works from a container

The probe ran on an R980-5GD, NCOS 7.26.21, NCM connected and managed. **The
claim this repo asserted in three places is wrong at the socket level.** A
container holding only the `$CONFIG_STORE` volume, with no SDK app registration
of any kind, sent the `alert` verb and the Config Store replied `Alert added(...)`.

### Observed protocol

```
alert\n<name>\n<value>\n     ->  status: ok, body: Alert added('<name>: <value>')
alert\n\n<value>\n           ->  status: ok, body: Alert added('<value>')
alert\n<value>\n             ->  no reply; socket blocks awaiting the third field
```

- Exactly three fields. `<name>` is a **prefix** on the alert text, not a
  separate field, and may be empty.
- A trailing extra newline is ignored.
- The body is a **plain string, not JSON**.
- **A missing field hangs rather than erroring.** Any client sending this verb
  needs a receive timeout, or one malformed command blocks it forever. The
  variant that "failed" in the probe failed by timing out, which is what
  identified the strict field count — a negative result carrying real
  information.

Still open: whether an accepted alert reaches the NCM console. `Alert added` is
local acceptance, not delivery. `cp.alert()` remains a stub pending that answer.

### What this cost, and the rule that would have prevented it

- The claim named a specific, plausible mechanism ("requires the on-router SDK
  application context"). Plausibility is what made it survive unchallenged
  through multiple documents and a dated "correction" in this very file.
- It then nearly justified building an outbound webhook delivery path as a
  workaround for a limitation that does not exist. **The cost of an untested
  limitation is not the wrong sentence in a doc, it is the unnecessary
  architecture built on top of it.**
- The probe took a few hours, most of it verifiable on a development machine, and
  converted a repo-wide assumption into a documented protocol. That ratio is why
  the Phase 1 gate on unverified limitations is worth keeping.
- Reinforced: a stub that logs "not supported" is indistinguishable from a real
  limitation to everyone downstream. If a capability is stubbed on the strength of
  an assumption, say so **in the stub's own message**, or someone will read the
  message as evidence.

### Probe design elements that paid off

Each of these earned its place; the run would have been ambiguous without them.

- **The known-good baseline** (`get` through the same raw transport) meant the
  successful `alert` replies could not be dismissed as a transport artefact.
- **Distinct markers per variant** made A, C and D separable when three of four
  layouts succeeded — a single shared payload would have made the comparison that
  revealed the name-prefix behaviour impossible.
- **Logging raw bytes verbatim** surfaced details no summary would have: the
  non-JSON body, the `<name>: <value>` composition, the ignored trailing newline.
- **Capturing preconditions** (NCM `state`/`managed`/`sync`) means the result is
  interpretable rather than a bare "it printed ok".
- **Not testing through the stub.** Calling `cp.alert()` would have logged "not
  available" and proven nothing; the whole result came from going at the protocol
  directly.

### Addendum to the result above — two details, and one outstanding fix

Recorded separately because they are easy to lose in the headline finding.

- **Config Store wire format, observed precisely:** header fields are separated
  by **bare LF** while the block is terminated by **CRLFCRLF**, e.g.
  `status: ok\ncontent-length: 90\n\r\n\r\n<body>`. Splitting the header on
  `\r\n` does not yield fields. `content-length` is accurate and counts the body
  only. Some verbs return a **plain string body, not JSON**, so a client must
  fall back to raw text. Now in `docs/container-development-guide.md`; relevant
  to anyone writing or mocking this protocol.
- **Outstanding fix, deliberately not yet made:** the `alert()` stub in `cp.py`
  logs "NCM alerts require the on-router SDK app context", which is now known to
  be false, and it printed during the probe run looking like fact. That message
  is wrong regardless of how the NCM-delivery question resolves. It is being left
  until the implementation lands so the four vendored copies and their checksums
  are touched once rather than twice — but it must not be forgotten, because a
  false explanation shipped inside the code is worse than one in a doc: it is
  read as a runtime observation.
- Confirmed on hardware: a compose service without `container_name` produced the
  derived name `a_a_1`, which nobody would guess when reaching for
  `container logs <name>`. Set it explicitly, as already noted above.

## 2026-08-15 (second entry) — Removing a feature and renaming a sample

Different shape of work from the rest of this file: subtraction rather than
construction. It has its own failure modes, now captured as Phase 2a in the
workflow.

### A reflection entry claimed work that was never done

- The previous entry stated `CP_APP_NAME` was "**Now documented in
  `docs/ncos-sdk-reference.md`**". It was not. The advice was right, the pointer
  was fiction, and it survived because nobody checks a citation in a lessons
  file. Corrected by actually writing the documentation.
- This is the same failure this whole session has been about, turned inward:
  **an unverified claim about your own work is as damaging as one about the
  platform**, and a confident cross-reference is *more* damaging than none,
  because the next reader stops looking. When an entry says something is
  documented, fixed or verified, that must describe a completed action.
- Cheap guard: after writing any "now documented in X" or "recorded in Y",
  grep X for the thing. It takes one command.

### Removal has a wider surface than the feature

- **Grep twice: once for the feature name, once for the project name.** A feature
  grep found the obvious code, but several leftovers were named after the sample
  rather than the feature — an HTTP `server_version` header, a compose volume
  name, an env-file name, entrypoint banner strings. Only the second grep found
  them.
- **Check dead references in both directions, because grep only does one.** For
  frontend work, compare CSS selectors against markup and JS *and* the element
  ids the JS caches against the markup: the first direction finds dead styling
  left behind, the second finds elements the JS still expects. Two dead utility
  classes surfaced only this way.
- Beware false positives in such a checker — hex colour literals (`#e2564d`)
  parse as id selectors. Recognise them before "fixing" anything, per the
  standing rule about verifying the check before suspecting the code.
- Finish in the shared docs. A removed concept usually survives as an example in
  a reference page, which is worse than dead code because it reads as current.

### Removing a feature can obsolete the abstractions that served it

- A hand-written deep copy of a config object existed in two places purely
  because one field was a mutable list. With that field gone every field was a
  scalar, and both copies collapsed into `dataclasses.replace()` — about thirty
  lines and a standing bug class (forgetting to add a field in two places).
- Generalised: **after removing a feature, re-examine the scaffolding it
  justified.** Locks guarding state nothing shares any more, listener hooks with
  a single caller, defensive copies of now-immutable data. Machinery that was
  proportionate before becomes over-engineering afterwards, and deleting the
  feature's own code is only half the job.

### Operator-facing strings are invisible to tests

- A full behavioural suite passed while the container still announced the old
  project name in its startup banner and prefixed every log line with the
  generic fallback. Tests assert what code *does*, not what it *calls itself*.
- **After any rename, run the container and read its log from the first line.**
  Banners, log prefixes, server headers and error messages only show up there.
- Confirmed for a second time in a second container: `cp.APP_NAME` falls back to
  `basename(cwd)`, empty at `/`, so it becomes `container:` unless
  `CP_APP_NAME` is set in the Dockerfile. Hitting this twice makes it a default
  every sample needs, so it is now in the Phase 2 Dockerfile conventions rather
  than only in this file.

### Simplifying a sample can silently drop capability coverage

- The removed feature was the only caller of a shared SDK function in the entire
  repo, so the repo lost its sole working demonstration of it. In a sample repo
  the code *is* the documentation, which makes that a real gap even though no
  functionality broke.
- **Check whether what you are deleting is the only example of something, and
  report the gap rather than letting it vanish.** Equally: do not re-add scope to
  close it during a simplification — surface it and let the user choose.

### Sweeping for dangling references is worth automating

Checking my own false citation was cheap enough to generalise, so I ran it across
every backticked file path in the steering files and `docs/`. It found four more
dangling references that had nothing to do with this task:

- Three docs pages pointed at a steering file that does not exist in this repo,
  and one cited a "reference implementation" from an unrelated project — the same
  class of leftover as the settings file removed earlier in this session. Both
  read as authoritative to anyone who does not try the path.
- The workflow described a sample directory by the wrong name (hyphen instead of
  underscore), which silently defeats a grep for it, and listed that sample's
  files as bare `src/...` paths that resolve nowhere from the repo root.

The check is a few lines: extract every backticked path ending in a known
extension, test `os.path.exists`, report the misses. Worth running whenever docs
or steering are edited, because a confident-looking cross-reference is worse than
no reference — the reader stops looking rather than searching for themselves.
Relative paths need qualifying or they are indistinguishable from broken ones.


## 2026-08-15 (third entry) — Integrating a user-supplied sample

The user dropped a working `rtsp_viewer/` (go2rtc-based RTSP-to-browser
viewer) directly into `containers/`, rather than asking for one to be
designed from scratch. Integrating an already-written sample has a different
failure surface than building one, worth naming separately from Phase 2a
(which covers removing/renaming) and Phase 1 (which covers designing new).

### A sample without cp.py is still a valid sample

- Every existing sample in this repo talks to the Config Store. This one has
  no reason to — it wraps a third-party media server and has nothing to read
  from or write to router config. **Do not add a `$CONFIG_STORE` volume or a
  `cp.py` copy just for consistency with other samples.** The steering doc's
  Phase 4 now records this as its own pattern so a future reader doesn't
  assume Config Store integration is mandatory.

### Hardcoded architecture in a Dockerfile is a bug even when it "builds"

- The Dockerfile as supplied downloaded a single hardcoded
  `go2rtc_linux_arm64` release asset. It built and ran fine — for arm64 only —
  which is exactly the failure mode Phase 2b's "build both architectures"
  step exists to catch, and it would have stayed invisible until someone
  tried it on an AER2200 or IBR1700. Fixed by branching on buildx's
  `TARGETARCH`/`TARGETVARIANT` build args, which are set automatically from
  `--platform` and need no per-build editing. Confirmed both `linux/arm64`
  and `linux/arm/v7` build and run afterward. Go's own release matrix names
  the arm/v7 asset `go2rtc_linux_arm` (`GOARM=7` in upstream's build
  workflow), not `..._armv7` — verified against upstream's CI config rather
  than guessed from the asset filename pattern, since `go2rtc_linux_armv6` is
  also a real asset and picking the wrong one silently gets a binary that
  starts up but was never tested on this router class.

### A vendored compose file pointed at someone else's registry image

- `docker-compose.cradlepoint.yml` shipped with `image: phate999/rtsp-viewer`
  — a real, currently-pullable Docker Hub image (confirmed via the registry
  API, arm64 only, so it would have failed silently on arm/v7 too), but not
  this repo's own build. Every other sample here builds its own Dockerfile
  and instructs the reader to push to *their* registry. Shipping a reference
  to a stranger's account as the example is the same class of problem as
  pointing at someone else's registry namespace during a live push (already a
  standing rule in Phase 2c) — except baked into a file instead of a one-time
  action, so it doesn't just risk one push, it teaches the pattern to every
  reader who copies the file. Fixed to build the local Dockerfile and push to
  `yourregistry/...`, consistent with the rest of the repo.

### An off-the-shelf binary's own auth is worth wiring through, not re-implementing

- go2rtc ships Basic auth for both its API and RTSP listeners already. The
  supplied entrypoint didn't expose it. Rather than adding a reverse proxy or
  a second auth layer, the fix was to plumb `API_USERNAME`/`API_PASSWORD`/
  `RTSP_USERNAME`/`RTSP_PASSWORD` env vars straight into the generated
  go2rtc.yaml — the minimum-code path, and it reuses a mechanism already
  tested upstream instead of adding a new one to this repo. Verified end to
  end locally: unauthenticated request to the published port gets `401`,
  authenticated gets `200`.
- **Read the wrapped binary's own docs for security-relevant defaults before
  writing the README's Security section**, not just for feature flags. go2rtc
  documents that it skips Basic auth entirely for requests it treats as
  coming from localhost, *even when auth is configured*. That's a real gap
  for anything sharing a network namespace with the container, and it would
  have been missed by only testing the external published port. Confirmed
  directly: a request made with `docker exec ... wget 127.0.0.1:1984` got a
  full response with no credentials while the same request through the
  published port was rejected. Documented explicitly in the README rather
  than left implicit, since a security note that only covers the case that
  was tested first is worse than no note.

### Image size is a real number, not "seems fine"

- Measured 116 MB (arm64) / 75.9 MB (arm/v7), noticeably larger than every
  other sample (45-60 MB) because of `ffmpeg`'s shared library closure on
  Alpine (about 110 packages pulled in transitively). Recorded as a measured
  number with the reason, rather than silently accepting it or silently
  trying to slim it without being asked — the user supplied ffmpeg's presence
  deliberately (go2rtc uses it for transcoding codecs browsers can't play
  natively), so removing it would drop capability, which Phase 2a says to
  surface rather than do quietly.

## 2026-08-15 (fourth entry) — A signal handler running does not unblock time.sleep()

### The bug, and why "confirm signal handling" didn't catch it the first time

- Built a container whose startup path called a stop-aware wait already, but
  the very same pattern was initially written using the library's
  `cp.wait_for_uptime(60)` directly plus a single `time.sleep(interval)` per
  loop iteration, guarded only by a flag-setting `signal.signal()` handler.
  Locally, with no router, `wait_for_uptime()` runs its full internal wait
  before giving up (uptime never appears without a Config Store), so this was
  exactly the condition where the bug would bite hardest.
- **Confirmed directly, not inferred:** a `SIGTERM` handler that sets a module
  flag runs immediately when the signal arrives, but the `time.sleep(N)` call
  it interrupted still returns only after the full `N` seconds. Python retries
  the underlying syscall to honor the originally requested duration (PEP 475)
  unless the handler itself raises. A minimal repro (handler prints a
  timestamp, then the sleep's return is timestamped separately) showed the
  handler firing at t+1s and the 10s sleep still not returning until t+10s.
- This means the existing "confirm signal handling" step in
  `docs/container-development-guide.md` — timing `docker stop` at idle — proves
  PID 1 forwards the signal, but says nothing about whether an in-process
  polling loop reacts to it. **These are two different failure modes and the
  first one does not test the second.** A container can pass the PID-1 check
  and still take up to a full poll interval, or up to a `wait_for_*` function's
  300-second default timeout, to actually stop.
- **General rule for any future container with a polling loop or a startup
  readiness wait: test `docker stop` while the process is inside the sleep or
  the wait, not just when it's idle between iterations.** A single flag check
  before the next `time.sleep()` call is not enough on its own — the sleep
  itself has to be broken into short steps that recheck the flag, and any call
  to `cp.wait_for_uptime()` / `wait_for_ntp()` / `wait_for_wan_connection()`
  needs its own stop-aware wrapper if the container must shut down promptly
  during that window, since none of those three take a stop flag or an event.
- Checked every other cp.py-using sample in the repo for this pattern before
  writing it up as a general lesson, since a one-off mistake isn't a repo-wide
  finding. `edge_ai` and `gpsd_server` already sleep in short steps against a
  `threading.Event`, and `SNMP_agent`'s only startup wait is inside its own
  short-interval cache refresh loop — so this was a gap in this build, not an
  existing pattern that had been silently wrong elsewhere. Worth checking
  either way before generalizing a lesson from a single fix.
- Fixed in `docs/ncos-sdk-reference.md` (new "Signal Handling in Polling Loops"
  section, and the Readiness table and Usage Pattern now show the interruptible
  form) and in `docs/container-development-guide.md`'s verification steps,
  rather than only in this file, since the previous version of both examples
  demonstrated the bug as the canonical pattern for anyone copying them.

## 2026-08-15 (fifth entry) — Verification and simplification, proportionality lessons

A single sample went through several rounds of "make this simpler" from the
user, each round catching something the previous round should have caught
too. The lessons are about proportionality and where information belongs,
not about the sample itself.

### Verification effort should match what the design actually needs, not what is technically knowable

- While the sample still had a signal handler and an interruptible-sleep
  wrapper, I went deep on characterizing exact PID-1 SIGTERM semantics —
  whether an unhandled signal to PID 1 is default-ignored by the kernel,
  whether `docker stop` was waiting out a sleep or waiting out its own
  timeout, testing multiple variants to pin down the mechanism precisely.
  That investigation was correct on its own terms but disproportionate: the
  container had nothing to flush on shutdown, and the user had already asked
  for something "very simple." **The point where verification depth should
  stop is set by what the design actually needs to guarantee, not by how much
  more there is to learn about the mechanism.** A stateless polling loop that
  drops signal handling entirely does not need its shutdown timing
  characterized to the second.
- The general check before going deep on a verification question: does this
  sample's own design depend on the answer? If a container has no state to
  preserve on exit, exactly how fast `docker stop` returns is not a finding
  worth investing in — note that it isn't instant and move on.

### Simplification requests should remove the narrative scaffolding along with the code

- Asked to strip signal handling and appdata from a sample, I removed the
  named things but initially left behind material that existed only to
  support them: a "Verified Before Deployment" section describing signal
  handling behavior that no longer existed, and a multi-step NCM deployment
  walkthrough where the rest of the file had already been cut down to a
  single compose block's worth of content. Each of these needed its own
  follow-up correction from the user instead of coming out in one pass.
- **When removing a feature at a user's request, look for every place that
  feature was *described*, not just where it was *implemented*, in the same
  pass.** This is the same principle as the existing "removing a feature can
  obsolete the abstractions that served it" lesson in this file, applied to
  prose instead of code — a README section can be scaffolding for a design
  decision exactly the way a lock or a deep-copy helper can.

### Verification narrative does not belong in a sample's README

- Three samples in this repo (not just the one being actively worked on) had
  accumulated a "Verified Before Deployment" section in their README:
  measured image sizes, what was tested locally against a mock Config Store,
  signal-handling timing, what could not be verified without a router. This
  is real information, but it is aimed at whoever is building or reviewing
  the container, not at whoever is deploying it — and it goes stale the
  moment the container's internals change, since nothing forces it to be
  re-verified alongside a later edit.
- Moved this out of every README it appeared in. It belongs in the chat
  response at build time, which is naturally scoped to that specific change
  and does not need to stay accurate indefinitely. A README should describe
  what the container does, its files, configuration, building and
  deployment — not the history of how it was checked. Added as an explicit
  step in Phase 2b of the workflow so future builds do not reintroduce it.
- Finding this required grepping the whole repo for the pattern (`^##
  Verified`) once one instance was flagged, rather than only fixing the file
  open in the editor. Consistent with the standing "sweep for the pattern
  repo-wide once one instance is found" lesson already in this file — worth
  re-noting because the trigger this time was a user correction on one file,
  not something I noticed myself.

### Don't infer platform identity from an artifact string

- An earlier lesson in this file stated the on-router container engine was
  "balena-derived" based on a log line naming a process
  `balena-engine-containerd`. The user corrected this: the engine is
  `cpdockerengine`, it may or may not still be balena underneath, and that
  lineage should not be assumed current just because a component was once
  named after it. **A process name, log string, or CLI tool name is evidence
  of what something was called at some point, not necessarily of its current
  implementation.** Naming survives rewrites more often than architecture
  does. Corrected in `docs/ncos-api/status/container.md` and the relevant
  lessons-learned entry to state the engine's current name
  (`cpdockerengine`) as the fact and its balena ancestry as an unconfirmed,
  possibly-outdated historical note rather than as an ongoing description of
  the runtime.
- This is a specific instance of the general "unverified claims presented as
  fact" failure mode already tracked in this file, arriving from a different
  direction: not a claim about platform *capability* that nobody tested, but
  a claim about platform *identity* inferred from a naming artifact rather
  than from documentation or a direct check. Worth watching for both forms.

## 2026-08-15 (sixth entry) — A log line that looks like the error is a red herring

### The misleading claim, and where it had spread

- A user hit a pull failure on a real router: `container logs` showed `No
  matching registry auth information for url https://index.docker.io/v1/`
  immediately followed by `unauthorized: authentication required` and
  `denied: requested access to the resource is denied`. Two places in this
  repo's own docs (`docs/ncos-api/control/container.md`, in two separate
  spots) asserted that the "No matching registry auth" line is "usually
  harmless for public images." That claim is misleading in exactly the case
  that matters: it is true that the line *itself* appears on every anonymous
  pull including successful ones, but the docs stated it as if the whole
  situation were benign, when the lines immediately following it are the
  actual failure and are not harmless at all.
- Root cause in this case, and the most likely one in general: **a newly
  created Docker Hub repository defaults to private.** An anonymous pull
  against a private repo gets rejected with the identical `unauthorized`/
  `denied` pair that a pull against a nonexistent repo would get — Docker Hub
  deliberately does not distinguish "private" from "doesn't exist" in the
  error, so the fix (make it public, or add registry credentials on the
  router) can't be read off the error text alone. Confirmed by checking Docker
  Hub's own default-visibility behavior and by the error text matching the
  documented Docker Hub v2 registry error format exactly.
- **General rule for reading any log line that's labeled "usually harmless" or
  similar in existing docs: check what immediately follows it before treating
  the situation as benign.** A single line's harmlessness does not imply the
  lines around it are harmless too, and a log message that is unconditionally
  emitted (here, on every anonymous pull attempt regardless of outcome) is
  never itself diagnostic — only a change in what follows it is.
- Fixed both occurrences in `docs/ncos-api/control/container.md` and added an
  explicit FAQ entry plus a note in the registry-configuration section of
  `docs/containers-quick-start.md`, since a reader troubleshooting a pull
  failure is more likely to land in the quick-start doc than the low-level API
  reference. Did not touch anything specific to the container that triggered
  this — the fix is a documentation correction, not a code change.

### This is a registry-visibility problem, not a router-side problem

- Worth naming as its own category distinct from the "image architecture
  doesn't match the router" and "manifest unknown" pull failures already
  documented: those are about what was built, this one is about the pushed
  image's *reachability*, and it is entirely reproducible off the router. The
  fastest diagnostic is an anonymous pull from a workstation
  (`docker pull namespace/repo:tag` while logged out, or with
  `docker logout` first) — if that fails identically, the router is not the
  variable at all, which is the same "verify the platform before debugging
  your artifact" principle already in this file, just applied to a registry
  instead of the container engine.

## 2026-08-15 (seventh entry) — The real cause was a name mismatch, not registry auth

### Diagnosed the wrong layer first

- A user's pull failed with the classic `No matching registry auth` /
  `unauthorized` / `denied` sequence. The previous entry in this file (and the
  doc fixes that went with it) treated this as a Docker Hub visibility
  problem — a very plausible read of that exact error text, and one worth
  keeping in the docs, but it was not the actual cause here. The real problem
  was that the pushed image was named with a hyphen
  (`yourregistry/gps-logger`) while the compose file the router was told to
  deploy had drifted to a different string in one section, an artifact of the
  README having the image name written out separately in a Building section
  and a Deployment section that had gone out of sync during earlier edits.
  Docker Hub's `denied`/`unauthorized` pair does not distinguish "wrong name",
  "private repo", and "doesn't exist" from each other, so the error text alone
  never pointed at the actual cause.
- **The cheapest diagnostic step — comparing the pushed tag string against the
  deployed `image:` string character for character — should be step one, not
  a fallback after ruling out registry-side explanations.** It costs nothing,
  it's mechanical, and it doesn't require touching the router, Docker Hub, or
  any credentials. Registry visibility and typo'd/nonexistent repositories are
  real and worth documenting, but they're strictly more expensive to check
  (visibility requires the user to look at Docker Hub settings; a genuine typo
  requires re-reading both strings anyway) than a direct diff of two strings
  the user can paste immediately. Re-ordered the troubleshooting entries in
  `docs/containers-quick-start.md` and `docs/ncos-api/control/container.md` so
  the name-match check leads.
- **When multiple explanations are consistent with the same symptom, ask for
  the one fact that discriminates between them before writing any docs.** Here
  that fact was "what exact string did you push, and what exact string is in
  the compose file" — a single, cheap question that would have found the real
  cause immediately, versus three rounds of plausible-but-wrong theorizing
  about registry semantics (which was accurate about Docker Hub's own
  behavior, just not the actual problem in this case).

### A README with the same value in two places is a latent bug, independent of any specific typo

- The immediate trigger was traced to something this file already has a
  general lesson about — a value duplicated across a doc going out of sync
  after an edit — but it's worth restating in the specific form that bit this
  build: **any time a sample's image name is written out more than once in a
  README (once in a Building section, once in a Deployment section, for
  example), that duplication is a standing risk of exactly this failure mode,
  independent of whether a specific edit has already desynced them.** The fix
  applied here was to make every occurrence of the name use the same
  underscore-separated form as the directory and `CP_APP_NAME`, and to grep
  the finished README for the name to confirm every instance matches. Recorded
  as a Phase 2 Compose convention so future builds check this before finishing
  rather than after a user hits the failure.

## 2026-08-15 (eighth entry) — Checking my own cross-reference before it shipped

Small, self-contained lesson from a doc-only session (no build performed): a
just-written edit had the same defect this file has repeatedly found in
*inherited* material.

### A dangling cross-reference written in the same turn is just as real as an old one

- While adding a new subsection to `docs/container-development-guide.md`
  about scoping Config Store access to multiple consumers, I wrote "See
  'Security Framing for Exposed Services' below" — a heading that does not
  exist anywhere in `docs/`. The framing it pointed at was real, but it only
  lived as prose in this steering file's Phase 1 notes, never promoted into
  the docs. The 2026-08-15 (second entry) lesson already in this file
  ("sweep for dangling backticked paths") was written for references
  inherited from past edits or user-supplied material; this one shows the
  same check applies to a citation added in the *current* turn, before
  anything is presented as finished. A cross-reference is not exempt from
  verification just because you just wrote it yourself.
- Caught by rereading the edit before treating the section as done, not by a
  separate sweep. **Any time a doc edit adds "see X below" or "see the X
  section", grep the same file (or the target file) for that literal heading
  text before moving on.** It's a few seconds and it is cheaper than a reader
  hitting a dead reference later. Fixed by inlining the actual point (mapped
  ports are exposed on WAN with no firewall filtering) instead of pointing at
  a section that isn't there.
- Generalizes past cross-references specifically: any claim of the form "see
  X" or "documented in Y" written during the current edit deserves the same
  skepticism as one inherited from history. The 2026-08-15 (second entry)
  lesson already names this pattern for a *stated-but-not-done* claim
  ("now documented in..."); this is the same failure at the moment of
  writing, not at the moment of retrospective audit.

## 2026-08-15 (ninth entry) — Naming a pattern without checking the repo's existing vocabulary first

### Introduced a term that collides with an unrelated, already-loaded word

- Asked for a same-container process that wraps `cp.py` behind a local HTTP
  API for a different-language app to consume, I called it a "bridge" in
  chat. This repo already has a name for close variants of this shape —
  "adapter", used in `docs/container-development-guide.md`'s "Feeding
  Config Store Data to an Off-the-Shelf Daemon" section for the mirror-image
  case (a daemon consuming Config Store data via a loopback socket). Worse,
  "bridge" is not a neutral synonym here: `network_mode: bridge` and
  "Default Bridge Network" are established, frequently-referenced Docker
  networking terms in the same document set, with a completely different
  meaning. A reader skimming both concepts in the same doc would have two
  unrelated things both called "bridge."
- The docs edit made in the same session used "adapter" correctly and did not
  repeat the mistake — this was confined to a chat response, but it would
  have propagated into a doc or sample code if the user had asked to
  scaffold it under that name, since a name coined in conversation tends to
  get carried forward into filenames and headings without being
  re-examined.
- **Before coining a name for a new pattern, grep the docs/steering for
  existing terminology covering the same or an adjacent shape**, and
  separately check whether the candidate word is already claimed by an
  unrelated concept in the same document set. Two failure modes, one check:
  reinventing a name that already exists, and picking a name that collides
  with something else. This is the naming-side analogue of the existing
  "grep before fixing a suspected documentation defect" lesson — verify
  against what's already there before adding something new, whether the
  addition is code, a doc claim, or a name.

## 2026-08-15 (tenth entry) — Label which parts of an example are fixed and which are a choice

### Conflating a platform constraint with an implementation choice in the same explanation

- When describing a same-container adapter wrapping `cp.py`, I gave one
  concrete implementation (HTTP over a loopback TCP socket) without
  distinguishing it from the actual fixed constraint in the same system: the
  Config Store side (`cs.sock`) *is* a genuine Unix domain socket speaking a
  fixed line-based protocol that `cp.py` must match exactly, but the
  adapter-to-other-app side is a second, independent socket that I designed,
  where transport (TCP vs. Unix domain socket) and wire format (HTTP vs.
  anything else) were both arbitrary choices, not requirements. The user
  reasonably assumed "socket" meant the same kind of socket throughout and
  had to ask a follow-up to find out only one of the two hops was fixed.
- **When an explanation involves two chained pieces and only one is
  constrained by the platform, say explicitly which is which before
  presenting a concrete implementation for either.** "This side is fixed by
  the platform; this other side is my design choice, here are the
  alternatives" costs one sentence and prevents a wrong generalization from
  the one example given. This is a documentation-writing/explanation
  discipline, not a code defect, but it generalizes the same way as the
  cross-reference lessons already in this file: unstated context invites the
  reader to draw a boundary in the wrong place.
- This also generalizes beyond sockets: any time an answer presents "the"
  implementation of something that actually has multiple independent axes of
  choice (transport, format, framing, naming), name the axes and state which
  are fixed by something external versus freely chosen, rather than
  presenting a single concrete answer as if it were the only shape available.

## 2026-08-15 (eleventh entry) — Started building the adapter before questioning whether it was needed

### Same-container cross-language access to cs.sock doesn't need an adapter at all

- A user asked for an adapter process (Python wrapping `cp.py` behind a local
  HTTP or socket API) so a different-language app in the *same* container
  could read/write the Config Store. I started building it — created the
  sample directory, vendored `cp.py` into it — before it occurred to either
  of us that the adapter was solving a problem that doesn't exist in the
  same-container case. `/var/tmp/cs.sock` is already mounted into that
  container via `$CONFIG_STORE`; it's a plain Unix domain socket speaking a
  small line-based protocol. Any language with Unix socket support (which is
  most mainstream ones) can connect to it directly and skip the adapter, the
  second process, the multi-process supervisor it would need (`ash` has no
  `wait -n`), and the health-check complication that comes with supervising
  two processes. The user, not me, asked "wouldn't it be simpler to just
  document cs.sock usage" — I should have asked that question myself before
  writing any code.
- **An adapter/proxy/wrapper process is only justified when there's a
  concrete reason the consumer can't reach the underlying resource
  directly.** In the same-container case there wasn't one — the socket is
  already in the same filesystem namespace. The adapter earns its place in
  the *other* two scenarios from this same conversation (a separate
  container, or a genuinely external/non-container consumer), where there is
  a real barrier (needing `cp.py`'s own protocol handling in another
  language across a process boundary, or needing auth/allowlisting for a
  network-facing caller). Conflating "another consumer wants this data" with
  "therefore build a mediating service" skips the step of checking whether a
  mediator is actually required for that specific scenario.
- This is a distinct failure from the existing "Architecture smell" lesson
  above (multi-process supervisor as a symptom of a daemon that shouldn't be
  bundled) — that one was about a bundled *service*; this one is about
  building a *mediating layer* in front of a resource the consumer already
  has direct access to. Same family (reach for a supervisor/adapter
  reflexively instead of first asking if it's needed), different trigger.
  Worth keeping both since they catch different designs.
- **General rule: before scaffolding a new sample/service to solve "app A
  needs access to X", check whether A already has direct access to X in the
  deployment shape actually being discussed.** Ask this before writing any
  code, not just when a design feels effortful. It costs one question;
  building the unneeded layer costs a whole sample plus its own docs and
  vendored dependencies. Caught here only because the user asked directly —
  worth internalizing so the same check happens without being prompted next
  time.
- Cleaned up the partially-scaffolded `containers/cp_adapter/` directory
  (just a vendored `cp.py`, nothing built on it yet) rather than leaving a
  dead sample directory in the repo.

## 2026-08-15 (twelfth entry) — Generalizing a tested fact to untested siblings while writing a spec

### One verb's confirmed behavior isn't automatically every verb's confirmed behavior

- Writing `docs/cs-sock-protocol.md`, I stated "field count is exact per verb
  and strict, sending too few fields hangs the socket" as a flat fact
  covering every verb. Only `alert`'s strict-field-count hang was ever
  actually tested against a live router (the 2026-08-15 probe recorded
  earlier in this file). For `get`/`put`/`post`/`delete`/`decrypt`, the same
  behavior is a reasonable inference — same protocol, same dispatch
  mechanism in `cp.py` — but inference is not the same evidence class as a
  direct test, and the draft didn't distinguish them.
- This is the same failure family this file has tracked repeatedly (claims
  presented as fact when they were actually untested extrapolations from a
  plausible mechanism), just caught at first-draft time instead of after
  the doc shipped and someone relied on it. Caught by rereading the new
  document specifically looking for claims that generalize a single
  confirmed observation across a whole category, not by a separate process
  step — worth making it one: **when a spec/reference doc states a behavior
  that was confirmed for one specific case, check whether the doc is
  presenting it as confirmed for the whole category it's grouped under, and
  narrow the wording to say so if the broader claim is inference, not test.**
- Practical phrasing pattern that keeps the distinction without weakening the
  practical guidance: state the safe assumption to build against ("treat
  every verb this way regardless"), but separately and explicitly name which
  instance is actually tested versus inferred. A reader building a client
  gets the same actionable guidance either way; a reader auditing claims can
  tell which sentence to trust as fact.
- Generalizes beyond this doc: any time a new reference document consolidates
  scattered facts of different confidence levels into one section (as this
  one did, pulling from the alert-specific probe, the general error-handling
  contract, and the wire-format testing notes), the act of consolidating
  flattens their original evidence markers unless each claim is re-checked
  against its source before being merged into prose that reads uniformly
  confident.

## 2026-08-16 — A capability question surfaced by scoping, not by building

No container was built this session; a use-case discussion (synthetic traffic
generation with IMIX-style packet-size distributions) surfaced a documentation
gap worth recording on its own, since it will recur for any future sample.

### "No root access" is not the same claim as "these capabilities are available"

- The docs state user namespace remapping and no root access to NetCloud OS,
  but never say which Linux capabilities (`CAP_NET_RAW` specifically, needed
  for raw sockets, packet crafting, and some ping implementations) a container
  receives under `cpdockerengine`, or whether Compose `cap_add`/`cap_drop` has
  any effect there. This is a different question from root access — a
  non-root process can still hold `CAP_NET_RAW` — and conflating the two would
  have led to either wrongly assuming a raw-socket tool will work, or wrongly
  ruling one out, without evidence either way.
- Recorded as an explicit UNVERIFIED item in `docs/container-development-guide.md`
  rather than guessed at, consistent with the standing rule in this file: when
  a design would be shaped by a platform limitation, find the evidence before
  building around it. A cheap probe container (attempt a raw socket, report
  success/failure) would settle this the same way the `alert()` and
  `cp.register()` questions were settled elsewhere in this file — worth doing
  before, not during, a build that actually needs the answer.

## 2026-08-17 — Reviewing the shared client instead of building a container

No container was built. The task was a code review of the vendored `cp.py`,
which found nine defects — six of them confirmed by execution against a mock
`AF_UNIX` socket rather than by reading. The lessons are about how shared code
and the docs describing it drift apart, and they apply to any future build that
depends on either.

### Reading a module is not verifying it, and a doc that describes code is not evidence about that code

- Two documents made claims about `cp.py`'s behaviour that the module does not
  have. `docs/ncos-sdk-reference.md` stated that its connectivity probe
  "re-prob[es] periodically" so a socket appearing later is picked up without a
  restart; the probe actually runs only while the cached state is `None`, so
  after one failure it returns a cached `False` forever. `docs/cs-sock-protocol.md`
  stated that a mock-socket harness "is exactly how `cp.py` itself is tested";
  no test suite for it exists anywhere in the repo. Both read as settled fact,
  both had survived multiple sessions, and both are the same failure this file
  has now recorded several times from different directions — except this time the
  unverified claim was about **our own code**, which feels far more trustworthy
  than a claim about the platform and is therefore less likely to be challenged.
- **Anything that reasons about a shared module's behaviour — a design decision,
  a doc sentence, a review finding — should be settled by running the module, not
  by reading it or by reading its documentation.** Reading found the suspicious
  lines; executing was what turned each one from "this looks wrong" into a
  reproducible fact with observed output, and one suspicion did not survive
  execution. The cost was about a hundred lines of throwaway harness for nine
  findings.
- Corollary for review work specifically: **write the harness before writing the
  findings.** Presenting a read-only review as fact is the same act as recording
  an untested platform claim as fact.

### A spec and the implementation it was derived from diverge silently

- `docs/cs-sock-protocol.md` says it was "cross-checked against `cp.py`'s
  implementation", and its parsing algorithm explicitly instructs clients to
  "treat a timeout here as 'malformed or hung response', not as success."
  `cp.py` does the opposite: it returns a synthetic `timeout` status as an
  ordinary value and then records the exchange as a transport *success*. A spec
  written *from* an implementation reads as though it certifies that
  implementation, and nobody re-checks the direction of the claim.
- **When a document specifies behaviour and also claims to have been checked
  against a reference implementation, those are two separate claims.** The spec
  can be right and the implementation wrong. Say which one was actually
  executed, and when a divergence is found, annotate the *implementation's* doc
  rather than quietly relaxing the spec to match the code.

### Health and diagnostic code needs a test that the unhealthy state reports unhealthy

- `cp.py`'s whole transport-health layer exists to distinguish "no Config Store"
  from "no data" — advice this file has given repeatedly and that every sample
  now follows. Against a socket that accepts the connection and never replies
  (the signature of a wedged container engine, already documented here), it
  reported `available: True`, `last_error: None`, `failures: 0`. The one failure
  mode most worth detecting was the one it declared healthy.
- **General rule: a health check, status endpoint, or availability probe is only
  verified once you have induced the failure it exists to detect and watched it
  report that failure.** Confirming it says "healthy" when things are healthy
  tests almost nothing. This extends the existing multi-process lesson (a health
  check must cover the process that is not PID 1) from *coverage* to *polarity* —
  cover the right thing, and prove it can actually go red.
- Three failure states are worth inducing deliberately for anything that talks
  to `cs.sock`: socket absent, socket present but never answering, and socket
  answering with a truncated or non-JSON body. Only the first happens for free
  by running locally.

### Cached "unavailable" state is a latch unless something clears it

- The re-probe bug's real damage is structural, not cosmetic: a long-running
  poller that gates its read on a cached availability flag converts one
  transient startup failure into a permanent outage, and the container looks
  alive and logs cheerfully the whole time. The socket being absent at second
  zero — e.g. because the container started before the Config Store was
  ready — is exactly the case that then never recovers.
- **Any cached negative state in a long-running container needs an explicit path
  back to positive**: a re-probe on a cooldown, an unconditional retry of the
  real operation, or clearing the cache when the underlying condition
  (`os.path.exists` on the socket) changes. Prefer attempting the real operation
  and diagnosing its failure over consulting a cached flag to decide whether to
  attempt it at all — the flag adds a way to be wrong without adding
  information.

### Audit which lines sit inside a broad `except Exception`

- Two mirror-image defects around the same try block. Command encoding is
  *inside* it, so a non-ASCII path is caught, logged as "config store
  unreachable", and counted as a transport failure — a caller-side argument
  error misattributed to the router, which then latches the availability flag
  described above. JSON encoding of the value is *outside* it, so a
  non-serialisable value raises straight past the module's documented
  "accessors do not raise" contract.
- **In any wrapper whose contract is "never raise, survive anything", the try
  block's boundaries are part of the contract.** Walk them line by line: work
  that can fail for *caller* reasons should be validated and reported as a
  caller error before the remote call, and everything that can fail for *remote*
  reasons must be inside. A broad handler is not a substitute for knowing which
  is which, because it silently relabels one as the other.

### Fix a hazard in every sibling code path, not just the one that prompted it

- `alert()` sanitises newlines out of its fields, with an accurate comment
  explaining that the protocol is newline-delimited and that interpolated data
  makes injection a real hazard rather than a theoretical one. The reasoning is
  entirely generic, yet `get`/`put`/`post`/`delete` interpolate paths and
  queries into the same newline-delimited protocol with no sanitisation at all —
  a path containing a newline sends more protocol fields than the verb takes.
- **When a fix lands with a comment explaining a general hazard, that comment is
  a to-do list for the module's other call paths.** Grep for the sibling paths in
  the same change. Harmless today only because every caller happens to hardcode
  its paths — which is a property of the callers, not a property of the module,
  and the repo already documents an adapter pattern that would forward
  request-supplied paths.

### Symmetric operations need symmetric matching rules

- `get_appdata()` matches names case-insensitively; `put_appdata()` and
  `delete_appdata()` match case-sensitively. So `put_appdata('Poll_Interval', x)`
  against an existing `poll_interval` creates a **duplicate** config entry, then
  its own read-back finds the older entry and returns `False` — a write that
  half-happened and reported failure. Observed on the wire.
- **Any get/set/delete trio over the same keyspace must share one key-matching
  rule, ideally one helper.** Where a read is deliberately lenient (case
  folding, whitespace trimming, aliases), the write and delete must fold
  identically or they address a different record than the read does. This is
  worth checking whenever a read-back is used as write verification, since the
  mismatch makes the verification itself lie.

### Do not manufacture a value that looks valid out of missing parts

- An identity accessor built a version string by interpolating three fields it
  never checked, returning the string `'None.None.None'` when the payload lacked
  the expected keys, while every sibling accessor returns `None` on an
  unexpected shape. A string like that flows into logs, comparisons and status
  APIs looking like data.
- **When assembling a value from several fetched parts, validate the parts, and
  return the module's own "no data" value if any are missing.** Since every
  `cp.get()` can return `None` for a path this firmware or model does not have
  (already a standing rule in this file), composite accessors are where that
  `None` most easily gets laundered into a plausible-looking result.

### A timeout parameter should bound the wall clock, not the intent

- `wait_for_uptime(min_uptime_seconds=60, timeout=3.0)` returned after 10.0
  seconds and then logged "timed out after 3.0s". The internal sleep is chosen
  from how long the wait is expected to need and is never clamped to the
  remaining deadline.
- **Clamp every sleep inside a bounded wait to the time left before the
  deadline.** This matters beyond tidiness for containers: `docker stop` timing
  is already the standing shutdown trap in this repo, and a readiness helper
  that overshoots its own stated timeout by 3x during startup is
  indistinguishable from an entrypoint that ignores signals.

### Reviewing shared code means locating every copy first

- Vendored copies mean a review's blast radius is not the file in the editor.
  Checksumming all copies before starting cost one command, confirmed all five
  were byte-identical, and made every finding attributable to the canonical copy
  rather than to the sample that happened to be open. Worth doing at the *start*
  of a review, not at the end when a fix is being written, because it determines
  whether findings are local or repo-wide.

### An analysis request is not an implementation request

- The ask was "analyze for bugs and improvements", and nine confirmed defects
  across five vendored copies plus their reference docs is a large,
  cross-cutting change. Reporting the findings with a proposed fix order and
  waiting was the right stopping point. **Volume of findings is not consent to
  act on them** — and for shared code that every sample vendors, the decision
  about scope and sequencing genuinely belongs to the user.

## 2026-08-17 (second entry) — Fixing the shared client, and two self-inflicted errors

Implemented the nine `cp.py` defects from the morning's review, added an opt-in
HTTP/REST transport, and wrote the module's first test suite. The two most
valuable lessons are the mistakes I made and caught in final verification, so
they lead.

### A case-insensitive filesystem makes path checks lie, and macOS is one

- I renamed a sample directory reference across three documents because `ls`,
  `find` and `os.path.exists` all agreed the on-disk name was lowercase. The
  rename was wrong: **git's index and HEAD both recorded the capitalised name.**
  Someone had done a case-only rename on disk, and git never noticed because
  `core.ignorecase=true` on macOS. So every check I ran passed locally and would
  have failed the moment anyone cloned the repo on Linux.
- The dangling-path checker this file already recommends is exactly the tool that
  gave the false negative. **`os.path.exists()` is not a case-exact test on
  macOS or Windows.** Check paths against `git ls-files` instead, which is
  case-exact and is also the authority for what a fresh clone actually contains.
  One command, and it makes the checker trustworthy rather than reassuring.
- **This is a container-build hazard, not just a docs hygiene one.** The image is
  Linux and case-sensitive; the development machine usually is not. A
  `COPY containers/My_Sample/app.py`, a `PYTHONPATH` entry, an `import`, or a
  config path that differs from the real name only by case will build and run
  fine on a Mac and fail on a Linux builder or CI — and the error (`file not
  found` for a file you can see) is genuinely baffling if you don't know to
  suspect case. Now recorded in `docs/container-development-guide.md`.
- Generalises to the whole class: **when the development machine is more
  permissive than the target, a passing local check is weaker evidence than it
  appears.** This is the same shape as the already-recorded lesson that
  Docker Desktop is not `cpdockerengine`, arriving through the filesystem
  instead of the container engine. Name which direction the permissiveness runs
  before trusting a green result.
- Worth noting the working tree and the index can disagree indefinitely without
  anyone noticing, and that a case-only rename needs `git mv` through a
  temporary name to be recorded at all. Report the discrepancy rather than
  silently picking a side: renaming a sample directory touches image tags,
  README references and everyone's checkout, so it is the user's call.

### Scripted multi-site edits need a match-count assertion, not just a successful run

- I replaced a set of short patterns (`` - `Dockerfile` — ``, `` - `entrypoint.sh` — ``)
  across a steering file to qualify some bare paths. Each pattern occurred in
  **three** different sample sections, not one, so the script silently relabelled
  another two samples' file lists with the first sample's directory. The script
  reported success; the file was wrong.
- **A bulk `str.replace()` with no count check is an unverified edit.** Either
  assert the expected number of occurrences before replacing, bound the
  replacement to a specific section of the file, or make each pattern unique
  enough to only match its intended site. Then read the result at each site — the
  script exiting cleanly says nothing about whether it changed the right lines.
- This is the standing "verify the check before suspecting the code" rule pointed
  at my own tooling: a scripted edit is a tool, and it needs the same
  verification as any test. Both of this session's errors were caught only
  because I re-ran a full verification pass at the end rather than trusting the
  intermediate steps.

### Adding a second backend proves whether an abstraction was earning its keep

- Asked whether unwrapping responses (`get()` returning the payload rather than
  `{'status', 'data'}`) was a mistake, the strongest answer came from the feature
  being added in the same change: the REST API wraps replies as
  `{"success": …, "data": …}` while the socket does not, so the normalisation
  layer is precisely what lets every accessor, and every caller, work over both
  transports unchanged. Without it each call site would have to know which
  transport it was on.
- **When a design question about an abstraction is hard to answer in the
  abstract, ask what a second implementation behind it would need.** An
  abstraction with one implementation always looks like ceremony; the second one
  is where it either pays for itself or is revealed as noise.

### "Is this design bad?" usually wants a documented rationale, not a refactor

- Two of the three requests this session were questions about existing design
  decisions (returning `None` instead of raising; unwrapping responses). Both
  resolved to "keep it", and the deliverable was the *reasoning*, written into
  the reference doc where the next person to wonder will look — not a change.
- Worth separating the two things such a question can mean: the design may be
  wrong, or the design may be right and its consequences under-documented. Here
  the ambiguity that prompted the question was real (`None` genuinely does not
  distinguish "no data" from "no router") but the fix was better diagnostics, not
  a different error-handling contract. **Find the real defect near the question
  before agreeing to the framing in the question.**
- Reinforces the existing "fix bugs, preserve contracts" lesson from a different
  angle: the pressure to change a shared contract can come from a reasonable
  question rather than from a bug, and it should be resisted the same way.

### A dual-mode client where one mode carries credentials must never fall back

- The new transport points the same module a container imports at a *remote*
  router with admin credentials. It is **explicit-only, with no automatic
  fallback**: a missing `$CONFIG_STORE` volume must fail visibly rather than
  switching. There is a test asserting the remote host is never contacted in
  that situation.
- ~~The property that makes that safe to ship inside every image is that it is
  explicit-only ... not quietly redirect writes to whichever router a leftover
  environment variable names.~~ **Corrected same day, by the user:** two things
  wrong with that framing. The scenario was implausible — the credentials file is
  a gitignored development-host artifact that never enters an image, so those
  variables are not present in a container unless someone deliberately adds them
  to compose or the Dockerfile — and "no fallback" was not actually sufficient
  for the invariant that matters, which is *on the router, never REST*. Nothing
  stopped an explicit opt-in call from succeeding inside a container. Fixed by
  refusing outright when the local socket exists, with an explicit `force=True`
  for the one legitimate cross-device case.
- Two lessons from that, both general:
  - **Justify a guard with the real threat model, not the most alarming story
    available.** An overstated rationale is not harmless: it makes the guard look
    sufficient, so nobody asks what it fails to cover. Here the dramatic version
    ("a stray variable could redirect a container") crowded out the plain
    question "what stops this being used on the router at all?" — which had a
    much better answer available for a few lines of code. State the preconditions
    an exposure actually requires; if the honest list is long, say so, and let the
    guard be judged on what it adds rather than on the scare.
  - **Where an invariant can be enforced in code, do not settle for documenting
    it as a convention.** "This transport is for development hosts" was in four
    documents and a docstring; it was still only advice. A single check against a
    condition already available locally (does the local socket exist?) converted
    it into something that cannot be got wrong by accident. Reach for the cheap
    local signal before writing another paragraph telling the reader not to do
    the thing.
- Generalises to any client with a local and a remote mode: **a fallback path is
  a feature only when both targets are equivalent.** When one is "the machine I
  am running on" and the other is "some other machine I have credentials for",
  a fallback turns a local misconfiguration into an action against a remote
  system. Related standing rule already in this file: a default target is a bug.
  A fallback target is the same bug with a longer fuse.
- Two smaller points that generalise: import the optional transport's
  dependencies lazily inside the functions that need them, so the common path
  does not pay the memory on a router with a 135 MB floor; and give any object
  holding a credential a redacted `__repr__` plus a `describe()` that reports
  `set`/`NOT SET`, so status output stays useful without being dangerous.
- When adding a second mechanism that overlaps an existing one, state the
  division of labour where both are documented. Two overlapping mechanisms with
  no stated boundary is worse than either alone — the same conclusion this file
  already reached about competing config mechanisms.

### Tests are the right place to pin down a limitation, not just a behaviour

- Some findings from a review are not fixable in the module: a payload that
  cannot represent a signed zero, for instance, loses information before any code
  sees it. Writing that as a **passing test that asserts the limited behaviour
  and explains why in its docstring** is much stronger than a comment: it states
  the current answer, and it makes a future "fix" that silently changes the
  behaviour fail loudly enough to force reading the reasoning.
- Related, for any review-driven work: tie each regression test to the defect it
  encodes in its docstring (a `regression:` prefix works). Six months on, the
  test name says what it checks but only the note says why anyone thought to
  check it, which is what stops the assertion being "simplified" away.
- **Weight coverage towards failure paths for anything that talks to a backend.**
  Six of the nine defects were only observable when the backend misbehaved —
  hung, truncated its reply, or was absent — rather than when it answered
  correctly. Tests that only exercise good responses would have found three.

### Testing a module that holds process-wide state

- A client module with module-level state (a transport mode, a cached health
  dict, memoised warnings) is a singleton, so tests must save and restore that
  state around every case or they leak into each other in order-dependent ways.
  A base `TestCase` that snapshots the module's tunables and resets its state
  dict in `setUp` is enough, and it is worth writing before the first test rather
  than after the first confusing failure.
- Restore captured output *last* in that teardown, and prefer a silent reset over
  calling the module's own public "reset" function, which usually logs — a
  cleanup that emits log lines makes a passing suite look broken.
- Make anything time-based (a poll cooldown, a receive timeout) a module-level
  constant specifically so a test can shorten it. Same reasoning as already
  recorded for the socket path.

### Prefer the stdlib primitive over the hand-rolled loop this repo had been recommending

- This repo's documented interruptible-sleep pattern was a hand-written
  `_sleep_interruptibly()` that slept in one-second steps checking a boolean
  flag. It works, but `threading.Event.wait(timeout)` is stdlib, returns the
  instant the event is set rather than at the next step boundary, needs no helper,
  and doubles as the flag check — so the recommended pattern was strictly worse
  than what the standard library already provides. Corrected in
  `docs/ncos-sdk-reference.md`, and the readiness helpers now accept the same
  event so a startup wait is covered by one mechanism.
- **When a doc recommends a hand-rolled pattern, check whether the standard
  library already solves it before propagating the pattern further.** The
  hand-rolled version was written to illustrate a real constraint (PEP 475), and
  illustrating a constraint correctly is not the same as demonstrating the best
  response to it.

### An `if __name__ == '__main__'` block cannot be tested, so extract it

- Adding the refusal above needed a test that `python3 cp.py --rest` exits
  cleanly with an explanation rather than raising a traceback. The CLI body was
  an `if __name__ == '__main__':` block, which is unreachable from a test: it
  only runs when the file is executed as a script, and by then the module's
  state cannot be arranged. **Move the body into a `_main(argv=None) -> int`
  that returns an exit code, and leave only `sys.exit(_main())` in the guard.**
  Exit codes and error paths then get the same coverage as everything else, for
  no change in behaviour.
- Generalises to any script-shaped entry point in a container: the exit code *is*
  the interface (a restart policy and a health check both read it), so it
  deserves a test, and it cannot have one while it lives in a module-level
  block.
- **`runpy.run_path()` is not a way around this.** My first attempt patched
  `cp.SOCKET_PATH` on the imported module and then ran the file with `runpy`,
  which loads a *second, independent* module instance whose state is the
  default — so the patch had no effect and the check reported a failure that did
  not exist. I nearly went looking for a bug in the guard. Worth adding to the
  standing "verify the check before suspecting the code" rule as a specific trap:
  **when a module carries process-wide state, any test that re-imports or re-runs
  it is talking to a different object than the one it configured.** Symptoms look
  exactly like the feature not working.

### Check the boolean logic when enumerating what an exposure requires

- Having just corrected an *overstated* risk, I wrote its replacement as a
  precondition list joined by "and": credentials present, *and* an explicit call,
  *and* `force=True`, *and* no local socket. The last two are **alternatives**,
  not both required — the override only matters when the socket exists. Presenting
  an `OR` as an `AND` inflates the number of things that must go wrong, which
  overstates safety exactly as much as the dramatic version overstated risk, and
  it is harder to spot because the sentence reads as rigour.
- **When documenting what an exposure requires, write the conditions as a
  numbered list and check each connective.** Prose hides the difference between
  "all of these" and "any of these"; a list makes it obvious, and it forces the
  question of whether each item is independent.
- Same lesson, said generally: **a precise-sounding claim is not a checked
  claim.** This file already has that for platform behaviour and for citations.
  It applies to the logic of one's own sentences too, and a security note is the
  worst place for it, because a reader counting four barriers will not verify
  that all four are real.

### A guard is only as good as the case it admits it does not cover

- The signal used to enforce "on the router, always the socket" is a local
  condition the module already checked for *reporting*: does the Config Store
  socket exist. **A condition already collected for diagnostics is often
  available as a policy input**, which is worth remembering before concluding an
  invariant cannot be enforced and writing another paragraph of advice instead.
- But it is a heuristic, and it does not cover a container on a router with the
  `$CONFIG_STORE` volume *missing* — there is no socket to detect then. The
  honest response was to say so in the reference doc and name what covers that
  case instead, rather than presenting the guard as total. Resisting the urge to
  invent a router-detection heuristic matters here: nothing in the container
  namespace is a confirmed marker, and inferring platform identity from an
  artifact is a mistake already recorded in this file.
- **State which case a partial guard closes and which it does not.** A guard
  documented without limits is read as complete, and the next person to extend
  the feature will assume the boundary is already defended.

## 2026-08-18 — Answering a feasibility question by executing it

No sample was built. The task was "can the router support a container that does
X", followed by design questions about routing LAN traffic through such a
container. Everything below was observed by running containers locally, not
reasoned about, and the lessons are about verifying platform capability and
testing containers that carry other hosts' traffic.

### An assessment request still deserves an executed probe, built outside the repo

- The user's framing was explicitly "don't write code". Answering from reasoning
  alone would have produced a confident, plausible, partly-wrong answer — the
  exact failure mode this file has recorded repeatedly. Building a throwaway
  probe in `/tmp`, running it, reporting measured results, and deleting it
  satisfies both: no code lands in the repo, and every claim in the answer has
  an observation behind it.
- **"Don't write code" is a constraint on the deliverable, not permission to
  skip verification.** Work in a scratch directory outside the workspace, quote
  real numbers and real log lines, then clean up and confirm `git status` is
  clean. Say in the answer which parts were executed and which remain
  inference — a feasibility answer's value is almost entirely in that
  distinction.
- Two rounds of this cost maybe forty minutes of container builds and converted
  four separate "probably works" guesses into observations, including one
  (`/proc/sys` being read-only) that changes the design.

### Emulate the far end with a second container when the real peer is third-party equipment

- For a container whose job is to speak a protocol to hardware nobody has on the
  desk, standing up an open-source implementation of the **peer** role in a
  second local container verifies the whole local stack end to end: module
  loading, privileges, negotiation, and the data path. It costs one more compose
  service and no hardware.
- **State plainly which half of the interop question this settles.** It proves
  "our container can do this"; it says nothing about "the vendor's box will
  accept it", because both ends are then the same implementation. Presenting a
  same-implementation test as interop evidence would be a new instance of the
  standing "unverified claims presented as fact" failure. The residual risk
  belongs in the answer as a list of specific settings to confirm on the peer.
- For anything that forwards or routes on behalf of other hosts, use a
  **three-container topology**: a client, the container under test, and the peer.
  A two-container test only exercises traffic the container originates itself,
  which is a different code path in the kernel (output vs forward) and misses
  routing, NAT, and MTU behaviour entirely.

### Control plane up is not data plane working

- A session reported as `ESTABLISHED` proves negotiation and authentication
  succeeded. It proves nothing about whether payload traffic transits. Both are
  worth asserting separately: send real traffic, then read the byte and packet
  counters on both sides to confirm the path taken is the one intended.
- Exercise **TCP, not only ICMP**. ICMP passing shows routing and encapsulation
  work; only a stateful protocol exercises connection tracking, and only a real
  payload exercises MTU. Both bugs are invisible to `ping`.
- Also verify the identity the far end *observes*, not just that it answered. In
  a NAT design the peer sees a rewritten source address, and reading that on the
  peer is what confirms the translation actually happened rather than being
  bypassed by a route you did not notice.

### A daemon's module can be present in the image and silently not load

- A plugin shipped as a real `.so`, with a config file setting `load = yes`,
  simply did not appear in the daemon's runtime module list. Nothing named it:
  the daemon's own `failed to load` lines listed a dozen *other* modules that
  were merely absent from the build, so the log looked noisy-but-normal. The
  cause was an unmet transitive crypto dependency that another package provides,
  and the symptom would have been an authentication failure at run time, long
  after the build "succeeded".
- **For any modular daemon, assert on the runtime loaded-module list, not on the
  presence of files in the image.** Start it once during verification, capture
  the line where it enumerates what it loaded, and check the modules the feature
  needs are actually named there. `ls` on the plugin directory is not the same
  test and will pass while the feature is dead.
- Generalises to the family: **installed is not loaded, and loaded is not
  working.** Each step needs its own check when a feature depends on optional
  modules.

### Package presence is not feature presence, and Alpine's build may omit what you need

- The convention in this repo is a pinned Alpine base, and it is a good default.
  But a package existing in Alpine does not mean Alpine's build of it includes
  the optional module a design depends on — verified here by installing the same
  package on three Alpine releases and finding the needed module absent from all
  of them, while another distro shipped it in a separate package.
- **Check the specific feature, not the package name, before committing to a
  base image.** `apk add` the package in a throwaway container and list the
  module directory. Doing this during design costs minutes; discovering it after
  a sample is written costs the base-image decision.
- When the feature genuinely only exists elsewhere, the options are a different
  base or compiling from source with the module enabled. Either is a real
  decision with a size cost — **measure both architectures and report the
  numbers** rather than switching silently or quietly dropping the feature (the
  standing Phase 2a rule about surfacing dropped capability applies to design
  choices, not only to deletions).

### Image size is a flash number; RSS is the memory number

- Measured in the same run: a non-Alpine image at 121-132 MB (arm64) / 69 MB
  (arm/v7), with the daemon's resident set at **4.4 MiB** with a session up. Two
  orders of magnitude apart, and they answer different questions — the image
  competes for the 6-14 GB of flash, the RSS competes for the 135 MB-1.84 GB
  container memory allowance.
- **Report both, and do not let an image size rule out a design on memory
  grounds.** A 130 MB image that runs in single-digit megabytes is fine on the
  smallest routers as far as memory is concerned; it is flash and pull time that
  the size actually costs. `docs/memory-resources.md` previously presented image
  size as the planning number for both.

### Containers that forward traffic depend on a sysctl they may not be able to set

- `/proc/sys` is mounted **read-only** in the container, so `sysctl -w` fails
  even with `CAP_NET_ADMIN` — observed as `permission denied` on a namespaced key
  whose value the container can read perfectly well. The only lever is a compose
  `sysctls:` entry, and whether `cpdockerengine` honours that is unverified in
  this repo.
- The value it happens to default to therefore decides whether a forwarding
  design is possible at all, which makes it a **go/no-go to probe first**, not a
  detail to handle during implementation. Reading it costs one command in any
  container already deployed.
- **Answered same day, on hardware: `net.ipv4.ip_forward` is `1`.** The user ran
  `container exec <name> cat /proc/sys/net/ipv4/ip_forward` against an ordinary
  deployed container — no `cap_add`, no `sysctls:` entry — so forwarding is
  enabled by default under `cpdockerengine` and the read-only `/proc/sys` does not
  block a forwarding design. Model and firmware were not captured alongside the
  result, which is the one thing that would have made it fleet-general rather than
  "seen on at least one router"; **capture device model and firmware whenever a
  probe result is recorded**, since without them the observation cannot be scoped
  and has to be re-run on the next device anyway.
- Worth noting how cheap this was: an open platform question that would otherwise
  have been designed around got closed by one command the user could run in an
  unrelated, already-deployed container. When a question is answerable that way,
  ask for it early — it does not need a purpose-built probe container, and it beats
  designing for both outcomes.
- General shape worth recognising: when a design depends on kernel state the
  container cannot modify, the question is not "how do we set it" but "what is it
  already, and is that survivable". Answer that before designing around either
  outcome.

### Prefer the implementation with the smallest privilege surface

- Where a daemon offers both a kernel-facility implementation and a userspace one
  needing only a TUN/TAP device, the userspace path is the better default on this
  platform: it asks only for capabilities scoped to the container's own network
  namespace, it fails predictably (slower) rather than mysteriously (`EPERM` deep
  inside third-party code), and it sidesteps the whole unverified question of what
  kernel state a remapped namespace can touch.
- A userspace data path also tends to encapsulate in UDP, which independently
  matters here: containers on the default bridge sit behind the router's SNAT, and
  a protocol that is neither TCP nor UDP has no translatable header.
- Treat the kernel-facility path as an optimisation to confirm on hardware, not as
  the design.

### MTU and PMTUD deserve an explicit test whenever a container carries other hosts' traffic

- A tunnel interface came up with a reduced MTU set automatically, and path MTU
  discovery worked: the container emitted the ICMP that tells the sender to back
  off, and the sender cached it. That is the good case, and it is worth confirming
  rather than assuming, because the failure mode is small packets working and
  large ones vanishing — which reads as an application bug, not a network one.
- Test it deliberately with sizes above and below the tunnel MTU, with and
  without the don't-fragment bit, and check whether the sender learns the path
  MTU. Then clamp MSS anyway: the mechanism depends on ICMP that is filtered
  often enough in real networks that relying on it is optimistic.

### Search the config tree for the field you need, not the feature name you expect

- Looking for static routes, a grep for the obvious path returned nothing, which
  briefly read as "the platform lacks this" — the exact trap already recorded in
  this file. The real path nests the concept under a differently-named parent.
  Grepping for the **leaf field** rather than the feature name found it
  immediately.
- Refinement worth adding to that standing lesson: search for the *field* the
  design needs (`gw`, `options`) rather than the *feature* it belongs to
  (`static`, `dhcp_option`). Field names are stable across firmware and
  vocabulary; the names of features and UI sections are not.
- Also reconfirmed, in the direction the DTD *can* answer: a field's type tells
  you a value is expressible, and nothing more. A permissive type is worth
  reporting as "the config model allows this, untested on hardware" — never as
  "this works".

## 2026-08-18 (second entry) — Widening a requirement invalidated an earlier answer

Continuation of the same feasibility work. The user narrowed one requirement and
broadened another, and the broadened version exposed two failure modes the
narrower design never touches. Everything below was observed by running it.

### A requirement change can invalidate an earlier recommendation, and the reversal has to be said out loud

- I had recommended one mechanism over another with a sound reason. When the scope
  of the requirement widened, the reason evaporated and the rejected option became
  the correct one. Carrying the earlier recommendation forward would have been
  wrong, and quietly swapping it would have been worse — the user would have had a
  contradiction in the transcript with no explanation.
- **When a requirement changes, re-derive the earlier recommendations rather than
  appending to them, and state plainly which ones reverse and why.** The reasoning
  that produced advice is a function of the requirements it was given; changing an
  input can flip the output without any new information about the platform.
- Watch specifically for changes that **widen scope**: a subset becoming
  everything, a narrow selector becoming a catch-all, one consumer becoming all
  consumers. These do not just add load, they can change which mechanism is
  correct and can activate whole failure modes that were previously unreachable.

### A catch-all route in the container's own namespace captures its own return traffic

- Widening a routing selector to a catch-all produced a policy rule of the form
  "from all, look up this table", whose default route then also matched replies
  destined for the *local* network the container serves. Return traffic was
  routed into the upstream path instead of back to the requesting host. The
  narrow-selector version of the same configuration cannot hit this, because the
  table then contains only specific destinations.
- **Any container that installs a catch-all route or a broad policy rule in its
  own namespace should be tested in the reverse direction**, not only outbound.
  The symptom is one-way traffic, and the natural reading is that the far end is
  broken.
- Generalises past routing: a wildcard rule added for one direction of traffic —
  a redirect, a proxy rule, a NAT rule, a firewall default — usually also matches
  the return path, and the return path is the one nobody tests.

### Test the dependency-down state, and prefer fail-closed

- With the upstream path deliberately torn down, the container's routing fell
  back to its ordinary default route and it forwarded other hosts' traffic
  straight out in cleartext. Counted it: real packets, real egress interface,
  while everything still looked healthy.
- Worse than the leak itself is how it interacts with a later well-meaning
  change: those packets get no replies only because the NAT rule is scoped to the
  intended egress interface. Add a broader NAT rule during troubleshooting — an
  entirely natural thing to do — and the traffic starts *working*, silently
  bypassing the path it was supposed to take. **A leak that works is far more
  dangerous than a leak that fails**, because nothing looks wrong.
- **For any container that forwards, proxies or relays other hosts' traffic
  through a conditional path, verify the behaviour with that path down.** Then
  make the failure explicit: default-deny forwarding and allow only the intended
  egress plus the conntrack return direction. Verify in *both* states — down (no
  leak) and up (still works) — because a fail-closed ruleset that also blocks the
  working case is an easy mistake and it is only caught by re-testing the happy
  path afterwards.
- This is a distinct gate from a health check. A health check notices the
  dependency is gone; it does nothing about what the data path does in the
  meantime. Both are needed and neither substitutes for the other.

### Assert at the endpoint that matters, not at the middlebox

- The intermediate component's own counters showed traffic flowing in both
  directions while the client at the edge saw 100% loss. Both readings were
  accurate: packets really did traverse the middle and really did come back, then
  went somewhere other than the client. Trusting the middlebox's counters would
  have sent me looking at the far end for a fault that was one hop away.
- **A component's own statistics are evidence about that component, not about the
  end-to-end path.** Assert success at the endpoint that actually consumes the
  service, and use intermediate counters to localise a failure once the endpoint
  says something is wrong. This is the same lesson as "do not trust a system's
  own diagnosis of itself", arriving through counters instead of log messages.

### Counters are cumulative; zero them or compare deltas

- I briefly read a rule's packet counter as evidence of a new leak when the count
  was left over from the previous phase of the same test. **When counters are the
  evidence, reset them at the start of each phase or record the value before and
  after and reason about the delta.** An absolute number carries no information
  about which phase produced it.
- Belongs to the standing "verify the check before suspecting the code" family:
  the measurement instrument was fine, my reading of it was not, and it would have
  produced a confidently wrong finding.

### A compound shell block can silently not run while printing plausible output

- A multi-step verification block containing `#` comment lines was mangled by the
  shell: none of the steps ran, and the output looked like a replay of the
  previous command, which read as though the steps had executed. I only caught it
  by inspecting the resulting state (`ip route`, `iptables -S`) and finding
  nothing had changed.
- **Keep inline comments out of compound shell blocks, and make each step echo a
  unique marker**, so a missing marker proves a step was skipped. Then verify the
  *state* the block was supposed to produce rather than reading its output as
  proof.
- Same family as the existing rule about scripted multi-site edits needing a
  match-count assertion: a script's output is not evidence that its steps ran, and
  plausible-looking output is the worst case because it stops you checking.

## 2026-08-18 (third entry) — Recording a result the user obtained

Tiny task: the user ran one probe command on their router and pasted the result,
which closed a platform question this file had opened the same day. The result
itself is recorded above, next to the entry that framed it as unknown. What
follows is about handling an incoming result, which is its own small discipline.

### A positive result invites over-reading, so enumerate what it does not close

- The observation settled one specific question. Adjacent questions that feel like
  the same question — whether the engine grants the capability the design also
  needs, whether a device mapping appears, whether related rules can be
  installed — were untouched by it, because they are decided by a different
  mechanism (what the engine grants) than the one the probe exercised (what the
  kernel permits in that namespace).
- **When a probe comes back positive, write down what it does not establish in the
  same breath as what it does.** A bare "confirmed" gets read as clearance for
  everything nearby, and the next reader has no way to tell which adjacent claims
  were checked. This is the mirror of the already-recorded trap of generalising one
  verb's tested behaviour to its untested siblings — same failure, arriving through
  a success rather than through a doc consolidation.
- Practical form: name the mechanism the probe actually exercised. If the remaining
  questions turn on a *different* mechanism, that is the sentence that stops the
  over-generalisation.

### Capture device model and firmware with every probe result

- The result arrived without them, and that is the single thing that would have
  made it general rather than "seen on at least one router". Without model and
  firmware the observation cannot be scoped, so the next device needs the probe
  re-run regardless — which wastes most of the value of having run it.
- This repo already does it well elsewhere (an earlier probe result is recorded as
  R980 / NCOS 7.26.21, and that is why it is still quotable). **Ask for model and
  firmware alongside any result before recording it**, or record the gap explicitly
  so nobody reads the entry as fleet-wide.

### When a claim's status changes, sweep for every place that framed it as open

- Converting an UNVERIFIED item to observed means editing more than the page that
  stated it. A leftover paragraph saying "this is unknown, probe it first" reads as
  current, and its consequence is worse than a stale wrong fact: it invites someone
  to re-run settled work, or to design around a limitation that has been
  disproved — the exact cost this file has already documented for untested claims.
- **The token grep is not enough.** Searching for the setting's name did not match
  the paragraph that was entirely about that setting, because the prose referred to
  it by description rather than by name. I found it only by searching for a phrase
  from the surrounding argument. So sweep for the *concept* — headings, synonyms, a
  distinctive phrase from the claim — not just the identifier, or the sweep returns
  a false all-clear. Same family as the standing rule that a zero-result search is
  evidence about the query rather than the corpus.

### The cheapest probe is often reading state somewhere that already exists

- Recorded in full next to the result above, but worth stating on its own because
  it applies before any probe container gets written: an open question about the
  platform may be answerable by reading state in a container that is *already
  deployed* for some unrelated purpose. That is one command and no build, versus
  authoring, pushing and deploying a probe project.
- Check that first. A purpose-built probe container is the right tool when the
  question needs a capability, a device mapping, or a package the existing
  containers do not have — not when it needs a file read.

## 2026-08-18 (fourth entry) — Testing a kernel-feature question on the wrong machine

User correction, and a real category error on my part. Recorded because the
reasoning that led to it is written down in this repo and reads as permission.

### Kernel feature availability does not transfer from the development machine

- Asked whether a kernel networking facility was usable from a container, I
  probed it **locally under Docker Desktop** and reported the result. One feature
  was reported as unavailable ("Unknown device type") and two as available. All
  three findings are facts about Docker Desktop's **linuxkit VM kernel** and say
  nothing whatever about the router's kernel, which is a different build with a
  different `CONFIG_*` set.
- **A local kernel result is unsafe in both directions**, which is what makes this
  worse than a merely useless test: absent locally invites dropping a design that
  the router would support, present locally invites shipping one that fails at
  deploy. I was one step away from telling the user a facility "isn't available"
  on the strength of a VM kernel's config.
- The class is wider than one subsystem: anything reached through
  `ip link add ... type <x>`, kernel IPsec/XFRM, tunnel drivers, netfilter
  modules — anything whose existence depends on a kernel build option. **These
  questions are answerable only by `container exec` on the router.**

### The misleading sentence was in this repo's own guide, and it was mine to notice

- `docs/container-development-guide.md` said engine differences matter, then added
  that "kernel-level behaviour such as how PID 1 receives signals inside a
  container also transfers, since that's a property of Linux namespaces rather
  than of Docker Engine specifically." That is true and it is also exactly the
  sentence I over-generalised: from *namespace semantics transfer* to *kernel
  behaviour transfers*, and from there to *kernel feature availability transfers*.
- Fixed by replacing the prose with three explicit buckets — transfers (image
  contents, application logic, namespace semantics), does not transfer (kernel
  configuration), does not transfer (engine and namespace policy: what `cap_add`
  grants, whether `devices:`/`sysctls:` are honoured, userns remapping effects).
  A reader now has to place a result in a bucket rather than infer from an example.
- General lesson about documentation of this kind: **a doc that says "X transfers"
  with one example invites the reader to extend the category by resemblance.**
  Where the boundary matters, enumerate what is *outside* it too. One worked
  example plus a general-sounding justification is how a correct sentence becomes
  a wrong inference.

### Momentum is not a reason to keep using the same instrument

- The honest cause was not ignorance of the distinction — this file already
  contains "the engine itself differs" and "when the development machine is more
  permissive than the target, a passing local check is weaker evidence than it
  appears". It was that several previous questions in the same session *were*
  legitimately answerable locally (image contents, plugin loading, application
  behaviour, netfilter rule syntax), so reaching for the same harness again
  required no thought.
- **When a new question arrives mid-session, re-ask which machine can answer it
  rather than reusing the harness that answered the last one.** The cost of
  getting this wrong is not a wasted test, it is a confident answer about the
  wrong system, delivered with measured output attached — which is far more
  persuasive than a guess and therefore far more damaging.
- Cheap guard, worth applying before any local run: name the thing the result
  would be a property of. If the answer is "this kernel" or "this engine" rather
  than "this image" or "this code", the development machine cannot answer it.

### Retract, do not soften, a result obtained the wrong way

- The right response to the correction was to withdraw the findings outright
  rather than keep them with a caveat attached. A caveated wrong-machine result
  still anchors the design discussion, and in a transcript it will be read later
  as evidence with a footnote. Withdrawing it and naming the one-command probe
  that *would* settle the question leaves the reader in a better position than a
  hedge.

### Addendum to the fourth entry — two things the correction exposed downstream

Recorded separately because both are about handling a correction rather than about
the mistake itself, and both apply to any future session.

- **A repeated caveat stops working as a warning, including for the person writing
  it.** Several answers in that session carried "verified locally, not on
  `cpdockerengine`" and the qualifier was accurate every time. That is what made it
  dangerous: attaching it began to feel like managing the risk, when for one
  question the caveat's real job was to stop the local test being run at all. **A
  standing disclaimer is not a substitute for deciding, per question, which machine
  can answer it.** If a caveat appears in every answer, it has stopped carrying
  information — treat that as a signal to re-derive the judgement it was standing
  in for.
- **After writing down a new distinction, sweep your own earlier claims in the same
  session against it.** Applying the new three-bucket rule retroactively caught two
  claims I had already made and presented as settled: that netfilter/NAT rules
  install and behave correctly, and that a TUN device can be created — both of
  which are kernel-configuration and capability-policy questions, not properties of
  the image. The design reasoning built on them survives; the platform claims do
  not. Fixed by adding the netfilter probes to the router probe list and marking
  the fail-closed pattern's availability as unprobed where it is documented.
  Writing a rule and leaving your own prior output unaudited against it is the
  documentation equivalent of fixing a bug without checking the sibling call paths.

## 2026-08-18 (fifth entry) — First build of a container that carries other hosts' traffic

A sample was actually written and run this time. The findings below came out of
testing it rather than designing it, and the first one is a defect I shipped into
the first build and only found by inducing a failure the daemon's own recovery did
not cover.

### An off-the-shelf daemon's reconnection is several mechanisms, each with its own trigger

- The daemon offered three recovery options and I enabled all three, which read as
  comprehensive. They are not the same mechanism: one fires at configuration load,
  one only when the **peer** closes the session, one only when a liveness check
  concludes the peer is dead. Tearing the session down administratively — none of
  those three routes — left it down **permanently**, with the container reporting
  perfectly healthy because its main process was alive and its config was loaded.
- For a container whose entire purpose is maintaining that session, and especially
  one that fails closed, that is an indefinite outage waiting for a human.
- **Enumerate which trigger each recovery option responds to, then test by inducing
  a teardown outside that set.** Blocking traffic or killing the peer exercises the
  liveness path, which is the one most likely to already work; an administrative
  teardown is the case that finds the gap. Fix by adding a watchdog keyed on
  **observed session state** rather than on the daemon's notion of failure.
- Generalises to anything holding a long-lived session — broker connections,
  replication streams, media pullers. "It reconnects" is a claim about specific
  triggers, not about all failures.

### Do not make an unverified engine behaviour part of the recovery design

- The obvious alternative to a watchdog was to let the health check mark the
  container unhealthy and have the engine restart it. `docs/container-development-guide.md`
  stated flatly that after `retries` failures "the container is restarted" — with
  no evidence cited, and plain Docker Engine does **not** do this (it only marks
  the container unhealthy; restart-on-unhealthy is Swarm behaviour). Building
  recovery on that sentence would have produced a container that silently never
  recovers.
- Corrected the doc to mark it UNVERIFIED and to say what a health check is good
  for: a status signal, not a recovery mechanism. **Recovery belongs inside the
  container**, where it depends only on code we control. Keep the health check for
  visibility, and keep exiting non-zero when the main process dies, since the
  restart *policy* is documented behaviour.
- General rule: before a design leans on a platform behaviour, check whether the
  claim describing it cites evidence. An unattributed flat assertion in this repo's
  own docs is exactly the failure mode already recorded here several times, and it
  is most dangerous where it silently substitutes for something you would otherwise
  have built.

### Put the platform probes in the real container, not a separate probe project

- I had been offering to build a throwaway probe container to settle which
  capabilities and devices the engine grants. Putting those checks in the real
  container's entrypoint was strictly better: one artifact instead of two, the
  first deployment answers every open platform question from `container logs`, and
  the checks keep paying off afterwards — the same output distinguishes "a grant
  was withdrawn by a firmware upgrade" from an application fault.
- Shape that worked: one line per check, `PREFLIGHT ok` / `PREFLIGHT FAILED`,
  each failure naming the compose key that would fix it, then **refuse to start**
  rather than proceeding into a half-working data path. A container that half-works
  is harder to diagnose than one that will not start and says why.
- A separate probe container is still right when the answer decides whether the
  real container gets written at all. It is the wrong tool once you are writing the
  real thing anyway.

### Install a safety property before the thing it protects exists

- The fail-closed firewall goes in **before** the daemon starts, not after it
  connects. Installing it afterwards leaves a startup window in which forwarded
  traffic takes exactly the path the rules exist to prevent. Netfilter accepts
  rules naming an interface that does not exist yet, so rules referring to an
  interface the daemon will create later can be installed up front — which is what
  makes the safe ordering possible at all.
- General: when a container installs the rules enforcing its own guarantee, the
  guarantee holds only from the moment the rules exist. Order the entrypoint so
  that moment precedes any traffic.

### Keep test tooling out of the image under test

- The image deliberately ships no `ping` or `nc`, which is correct and also meant
  the obvious test commands were unavailable. Rather than adding tools to the
  production image, use a minimal separate image for the client role, and attach a
  tool container **into another container's network namespace** with
  `--network container:<name>` when a listener or capture is needed inside the
  namespace under test.
- Worth knowing as a technique: it gives full tooling inside a namespace without
  changing the artifact being verified, so what was tested is what ships.

### Verify a reused harness is running what you think it is

- A test runner reused an existing background process for a new container run
  (`isReused: true`) after I had removed the container that process was managing.
  The run therefore executed nothing, and my next check reported "no SA" — which
  read exactly like the watchdog failing. I nearly went looking for a bug in code
  that had never run.
- **When a harness reuses a session, process or cached environment, confirm it is
  running the current command before reading its output as a result.** Same family
  as the standing rule about verifying the check before suspecting the code, and as
  the earlier finding that a compound shell block can silently not run while
  printing plausible output. A false negative from a harness is more expensive than
  from a test, because nothing about it looks like a harness problem.

### One-name rule: grep for both separators, not just the one you chose

- The sample is named with underscores throughout, and I still nearly shipped a
  generated file inside the image whose name used a hyphen. Caught by grepping for
  both separator variants and counting occurrences, which took one command and
  turned "I was consistent" into a number.
- Cheap, mechanical, and worth doing before finishing any sample:
  `grep -rho 'name[_-]variant' . | sort | uniq -c` should show a single row.

## 2026-08-18 (sixth entry) — A truncated search became a platform limitation

The user asked a clarifying question about which of two mechanisms the design
used. Answering it properly meant re-reading the config tree, which produced a
capability I had spent several turns implicitly asserting did not exist. Nothing
was built; the lesson is about how the wrong answer got established.

### `head` on an exploratory search manufactures platform limitations

- Earlier in the session I listed a config subtree with
  `grep -n '^- `config/<subtree>' PATHS.md | head -40`, read the 40 lines, and
  reasoned from them as though they were the subtree. The entry that answered the
  question sat a few hundred lines further down, inside the same subtree, and was
  removed by the pipe. I then told the user, across more than one turn, that the
  platform could not do something it can do.
- **A truncated result reads exactly like a complete one.** There is no marker in
  the output saying "there was more", so the conclusion drawn from it feels as
  well-founded as one drawn from a full read. This is the sibling of the standing
  lesson that a zero-result search is evidence about the query rather than the
  corpus: a *truncated* result is evidence about the pager, not the corpus.
- Practical rule, now in the workflow: **count before reading** (`grep -c`), then
  read every match, or narrow the pattern until the whole result set fits. Reserve
  `head` for output you already know the shape of, never for "does this exist".
  Config path indexes are the worst case, because one subtree's entries can span
  hundreds of lines and the interesting leaf is rarely near the top.

### Choosing mechanism B because A "cannot work" is a limitation claim in disguise

- The repo already gates workarounds justified by "the platform cannot do X". This
  was the same claim wearing different clothes: not a workaround, just a
  recommendation of one mechanism over another, resting entirely on an untested
  assertion that the other could not be scoped the way the design needed.
- It is easier to miss than a workaround, because the result looks like an ordinary
  design choice. There is no extra dependency, no odd architecture, nothing that
  invites "why is this here?". The gate has been widened to name it explicitly.
- Tell of this failure worth watching for in your own output: a sentence of the
  form "X can't express Y, so we use Z". That is a capability claim, and it needs
  a search behind it before it goes in a recommendation, a README, or a comment
  someone will copy.

### A reversal has more than one trigger, and each needs saying out loud

- A rule was added earlier in this session to re-derive recommendations when a
  *requirement* changes. This reversal had a different cause: the requirements were
  unchanged and **new information about the platform** arrived. Both produce the
  same hazard — advice in the transcript that is no longer correct — and both need
  the same handling: state which earlier answer reverses and why, rather than
  quietly substituting the new one.
- Worth generalising the trigger list rather than the rule: requirements changed,
  platform knowledge changed, or a measurement came back different. Any of the
  three invalidates conclusions downstream of it, and the older and more confident
  the original statement, the more important it is to name it as superseded.

### A clarifying question is a prompt to re-verify the excluded option

- The user's question was neutral — "is it A or B?" — and the useful response was
  not to restate the choice but to re-check the basis on which the other option had
  been dropped. That re-check is what surfaced the error.
- **Treat "so is it A or B?" as an invitation to re-audit why the loser lost.** A
  question about a decision is the cheapest moment to catch a bad premise
  underneath it, and it costs one search. Restating the conclusion confidently is
  the failure mode, because the question sounds like it wants a summary.

## 2026-08-18 (seventh entry) — Swapping implementations can void a safety rule silently

A question-only turn: whether an alternative implementation of a capability was
usable instead of the one the sample uses. Nothing was built and nothing was
tested — the answer depended on kernel configuration and namespace policy, which
only a probe on the router can settle. One genuinely new general lesson came out of
working through what switching would actually cost.

### A safety rule keyed on one implementation's artifact matches nothing under another

- The container's whole data plane — the fail-closed forwarding accepts, the NAT
  rule, the MSS clamp — is keyed on a **named interface** that the current
  implementation happens to create. The alternative implementation of the same
  capability is policy-based and produces **no interface at all**. Every one of
  those rules would match nothing.
- What makes this dangerous is the asymmetry: **the primary function keeps working
  while the safety property silently stops applying.** Traffic still flows, the
  session still establishes, logs look normal — and the guarantee the rules existed
  to provide is gone. Nobody would notice until the failure the guarantee was for
  actually happened.
- **Record what each safety rule is keyed on**, alongside the rule. Interface
  names, device paths, process names, log message formats are all artifacts of a
  particular implementation, and any of them can disappear in a swap that looks
  like a pure performance change.
- Generalises beyond firewalls: a health check keyed on a process name, monitoring
  keyed on a log string, a supervisor keyed on a PID file. Same shape — the thing
  being observed is incidental to the implementation, not intrinsic to the
  capability.

### "Does the alternative work?" is usually the wrong question

- The useful question is **"does the alternative still produce everything my design
  is keyed on?"** A capability comparison naturally focuses on whether the
  alternative provides the capability, which is the part most likely to be fine,
  and skips the edges where the design has accumulated incidental dependencies.
- Where it does not, there are two honest options: re-key the rules onto something
  the alternative does provide, or pick the *variant* of the alternative that
  restores the artifact. Note that the variant is often a further dependency on top
  of the base capability rather than a free choice — so a swap that looked like one
  unknown turns out to be several, and saying so is more useful than a yes.
- Practical habit: before evaluating a swap, grep the entrypoint and compose for
  the artifact's name. The count of hits is the size of the change nobody costed.

### Scope a performance claim by what it is not

- Moving a data path from userspace into the kernel removes a per-packet round
  trip. It is **not** hardware offload — the work still happens in software in the
  container's own namespace. Stating the bound explicitly matters because a reader
  will otherwise assume the most favourable interpretation available, and "kernel"
  reads as "fast" to most people.
- Same discipline as the standing rule about enumerating what a positive probe
  result does *not* establish, applied to performance instead of capability.

### The guard from the fourth entry fired

- That entry's rule — before running a local experiment, name the thing the result
  would be a property of, and if the answer is "this kernel" or "this engine" then
  the development machine cannot answer it — worked as written. The question this
  turn was of exactly that shape and the rule stopped the test before it was run.
- Worth recording that a rule in this file has now been exercised rather than only
  written. A guard that has actually caught the case it was written for is worth
  more than one that has only ever been asserted, and knowing which is which helps
  the next reader decide how much weight to give an entry.

## 2026-08-18 (eighth entry) — Built an integration against an assumed peer configuration

The user pasted the real configuration of the equipment the container integrates
with, and it contradicted an assumption the sample had been built on. Everything
below is about how that assumption survived long enough to be written into code.

### Ask for the peer's actual configuration before writing the integration

- The sample hardcoded one authentication method for the remote end. The real
  device uses a different one entirely — there was no shared secret to use, so the
  container as shipped could not have completed authentication against it.
- Nothing exotic caused this. **The configuration was available for the asking the
  entire time and I never asked for it.** Several turns were spent reasoning about
  what the peer probably required, when one request would have settled every
  parameter at once: authentication method, identities, proposals, traffic
  selectors, address assignment, timers.
- **For any container that integrates with third-party equipment, obtaining that
  equipment's configuration dump is a Phase 1 input, not a nice-to-have.** Ask for
  the config itself, not a description of it — a description carries the sender's
  own summary of what matters, and the parameter that breaks the integration is
  usually one neither party thought to mention.
- Corollary for local testing: a peer emulator configured from *my* assumptions
  verifies the container against those assumptions. That looks like end-to-end
  verification and is worth far less, because it passes while the real integration
  cannot connect. Configure the emulator from the real peer's config.

### A working client config for the same peer is a specification

- Alongside the device config came a working phone profile for the same gateway.
  It independently confirmed the authentication shape and the expected peer
  identity, and one of its properties settled a question the device config left
  ambiguous — the profile carried no trust anchor of its own, which could only mean
  the peer's certificate chains to a publicly trusted CA.
- **Ask whether a client that already connects to this peer exists, and get its
  configuration.** It has been proven against the live system, which no amount of
  reading the server config gives you, and it is usually shorter and more explicit
  about the client-side choices you actually have to make.
- Read it for what it *omits* as well as what it sets. An absent section is
  frequently the most informative part, because it means the default applied and
  the peer accepted it.

### A partial answer to a multi-part question is not confirmation of the whole

- Earlier I listed several things to confirm about the peer. The user confirmed
  one of them, precisely and helpfully. I then proceeded as though the list had
  been answered, and built on the remaining assumptions — including the one that
  turned out to be wrong.
- **The confirmed item is the confirmed item.** A partial answer reads as agreement
  with the whole question, especially when the answer arrives in the affirmative
  and the question was phrased as a bundle. Restate what remains open rather than
  letting silence become consent.
- Practical habit: keep the open-questions list explicit across turns and re-state
  the remainder each time one is closed. It costs a sentence and it is the same
  discipline already recorded for probe results — enumerate what an answer does
  *not* establish. This entry is that lesson arriving from a human answer instead
  of a machine one, which is the direction that is easier to miss, because a person
  answering feels like the question was handled.

### Exercise the PKI path, not just the shared-secret path

- Adding certificate support meant the certificate code path needed a real chain to
  run against. Generating one locally — a CA and a server certificate with the
  right identity, using the daemon's own tooling in a throwaway container — took a
  few commands and turned an unexecuted branch into a verified one, including trust
  anchor loading, chain validation and identity matching.
- **Where an integration supports both a secret-based and a certificate-based mode,
  test both.** The secret path is the easy one to stand up locally, so it is the one
  that gets tested, and the certificate path then ships never having run. Issuing a
  throwaway chain is cheap enough that there is no excuse for the asymmetry.

### Defaults copied from a working example can encode the example's assumptions

- One parameter had to be *absent* to match the peer, not present — the peer had a
  feature disabled that most examples enable, and including the corresponding
  option makes the second negotiation stage fail while the first still succeeds.
- Two general points. **A partially successful handshake localises the fault**: the
  stage that failed names the configuration section to look at, so "stage one fine,
  stage two fails" is information, not just a failure. And **a default lifted from a
  working example carries that example's peer assumptions**; where a setting must
  match the other side, say so in the comment next to the default, because the
  failure it causes does not look like a proposal mismatch.
