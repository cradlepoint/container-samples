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
