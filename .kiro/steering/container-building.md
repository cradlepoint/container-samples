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
   - `#[[file:docs/cs-sock-protocol.md]]` — the raw Config Store wire protocol, for a non-Python client talking to `cs.sock` directly (only needed when the request involves a language other than Python accessing the Config Store)

2. **Gate: does NCOS already do this natively?** Check before designing anything — see "Before You Build: Check for Native NCOS Capability" in `docs/container-development-guide.md` for the method and the running list of confirmed native services. Packaging a service NCOS already provides wastes memory and flash, bypasses NCM configuration, and produces a sample nobody should copy. If there is overlap, either reframe the container around the delta (translation, a defect workaround, buffering the native feature lacks) or pick something else. State the native-capability assumptions the recommendation depends on so the user can correct them before code is written, and confirmed-native findings are added to the table in the dev guide.

2b. **Gate: is the limitation you are designing around actually verified?** If a workaround, an extra dependency, a different architecture, or a dropped feature is being justified by "the platform cannot do X", find the evidence for X before building on it. Claims in this repo's own docs are marked UNVERIFIED where no test exists — read the evidence column, not just the claim. Writing a single-purpose probe container to settle it is cheap: most of it verifies on the development machine, and it converts an assumption into a fact that benefits every later build. Never restate an untested claim as established, in docs or in conversation.

2c. **Gate: does the consumer already have direct access to the resource?** Before designing a mediating service (an adapter, proxy, or bridge process) so that "app A can access X", check whether A already has direct access to X in the deployment shape actually being discussed — e.g. a same-container process reaching `cs.sock` directly needs no `cp.py`-wrapping adapter at all, since the socket is already in that container's filesystem namespace and most languages can speak its small line-based protocol themselves. A mediating layer is justified only when there's a concrete barrier: a genuinely separate container or external host, or a need to centralize auth/allowlisting across multiple untrusted consumers. Ask this before scaffolding anything.

3. If the user is asking what to build rather than naming a specific app, inventory the patterns the existing samples already cover (see Phase 4) and propose something that fills a gap. Recommend, then confirm before writing code.

4. Clarify with the user:
   - What the container should do
   - Which router model(s) it targets (determines architecture and memory)
   - Whether it needs Config Store access (cp.py / cs.sock)
   - Whether it needs a LAN IP address (custom network) or just port mapping on the default bridge
   - Whether it needs USB devices or shared volumes
   - Whether it only reads router state or also writes config via `cp.put()` (writes need an allowlist and an off-by-default appdata flag)
   - Any specific NCOS version requirements

   Note on port mapping: mapped ports are exposed on WAN as well as LAN and the router firewall does not filter them. Flag this whenever the user picks port mapping for a service that should not be internet-facing, and offer the Local IP Network alternative.

## Phase 2: Build the Container

Follow these conventions established in this repo:

1. **Dockerfile**:
   - Use a **pinned** Alpine tag (`alpine:3.18`, matching the existing samples), not `alpine:latest`. Router deployments are rebuilt and redeployed months apart, and an unpinned base silently changes the Python version and package set between builds
   - Install only necessary packages with `--no-cache`
   - Copy application files to `/opt/<app_name>/`
   - Set `PYTHONPATH` if using cp.py
   - Set `ENV CP_APP_NAME=<service>` whenever cp.py is used. `APP_NAME` falls back to `basename(cwd)`, which is empty at `/`, so without it every log line is prefixed with the generic `container:` — and `alert()` sends the value as a protocol field, so it should not depend on the working directory
   - Use an `entrypoint.sh` script for initialization logic
   - Expose only necessary ports with protocol (e.g., `EXPOSE 1161/udp`)
   - If the main process comes from a downloaded release binary rather than an `apk`/`pip` package, select the asset from buildx's `TARGETARCH`/`TARGETVARIANT` build args with an explicit failing default case — never hardcode one architecture's asset URL. `apk`/`pip` already resolve architecture for you; this only applies to hand-rolled downloads. See "Vendoring a Prebuilt Binary" in `docs/container-development-guide.md`
   - Not every container needs `cp.py` or `$CONFIG_STORE` — only include them if the container actually reads or writes router state. Decide this from Phase 1's clarifying questions, not from what other samples in this repo happen to do

2. **Python applications using cp.py**:
   - Copy `cp.py` from the repo root (canonical copy; each sample keeps an identical copy because the Docker build context cannot reach outside the service directory). It is standard library only — do not add `py3-requests`
   - Use `cp.get()`, `cp.put()`, `cp.log()` etc. for router communication
   - Use `cp.get_appdata()` for user-configurable settings
   - Use `cp.wait_for_uptime()` and `cp.wait_for_wan_connection()` at startup if needed
   - When feeding Config Store data to an off-the-shelf daemon, use a loopback TCP/UDP socket rather than a FIFO or pty posing as a device file, and emit the target protocol's explicit invalid/no-data value when router state goes stale instead of repeating the last known value
   - `cp.py` swallows its own errors: reads return `None` and writes return normally whether or not they succeeded. Use `cp.config_store_available()` to tell "no Config Store" apart from "no data", verify any write that gets reported to a user by reading it back, and read a `config/...` path before writing it. See the Error Handling Contract in `docs/ncos-sdk-reference.md`
   - Before writing any `config/...` field, confirm its type and meaning in the DTD (`docs/ncos-api/dtd-usage.md`), not from example code. Semantics are per-path — the same field name can mean the opposite thing in another section — and when a DTD comment is ambiguous the shipped defaults are the strongest available evidence
   - **Resolve a config path before reading it, do not guess.** Search `docs/ncos-api/config/PATHS.md` (or walk the DTD) for the distinctive leaf token, e.g. `container`, not a full guessed path like `config/system/container`. Reading a nonexistent path returns `null`, exactly like a path that exists and is empty, so guessing produces a confident wrong conclusion instead of an error. UI menu structure is not a guide to tree structure: the NCM UI shows containers under SYSTEM, while the config path is `config/container`

3. **Entrypoint script**:
   - Use `#!/bin/sh` (Alpine uses ash, not bash)
   - Perform any config generation or initialization
   - Use `exec` for the final command to ensure proper signal handling
   - If two processes must share the container, use a POSIX polling supervisor (`ash` has no `wait -n`) that exits non-zero when either child dies, plus a `trap` for TERM/INT. Never background one process and `exec` the other — silent death of the backgrounded process leaves the container looking healthy.

4. **Compose YAML**:
   - Escape literal `$` as `$$`; the platform interpolates Compose values, which is how `$CONFIG_STORE` resolves
   - Use exec-form `["CMD", ...]` health checks only for single binaries, `["CMD-SHELL", "..."]` when shell operators are needed, and confirm the test binary is actually installed in the image
   - Add `/udp` to port mappings for UDP services
   - **Pick one separator (hyphen or underscore) for the sample's name and use it verbatim everywhere the name appears**: the directory, the Dockerfile's `CP_APP_NAME`, the compose `service:`/`container_name:`, the built and pushed image name in every code block across the README, and any `container logs <name>` example. A pushed tag that differs from the deployed `image:` value by nothing more than `-` vs `_` produces `unauthorized`/`denied` pull errors that look exactly like a registry permissions problem, with nothing in the error text pointing at the name mismatch. When a README shows the image name in more than one place (Building and Deployment sections, for example), grep the finished file for the name and confirm every occurrence is byte-identical before finishing — this is a cheap, mechanical check worth doing every time, not just when something breaks.
   - The NCOS-deployment example's `image:` must reference this sample's own build, e.g. `yourregistry/<sample>:latest` — never a third party's public registry image, even one that happens to already exist and work. A vendored example is copied verbatim more often than it is read carefully, so a stray third-party reference propagates further than a one-off mistake would
   - When the container wraps an off-the-shelf binary or daemon that has its own security features (auth, TLS), wire those through as environment variables or config rather than adding a second layer in front of it. Read that binary's own documentation for security-relevant defaults, not just feature flags — e.g. an auth bypass for requests it treats as coming from localhost is a real gap for anything else in the same network namespace, and would be missed by testing only the published port

5. **Architecture**:
   - ARMv7 32-bit: AER2200, IBR1700
   - ARMv8 64-bit: E300, E3000, R920, R980, R1900, R2100

## Phase 2a: Modifying, Simplifying or Renaming an Existing Sample

Removing a feature is not the inverse of adding one, and the surface is wider than it looks. A single feature removal can touch backend modules, frontend markup, CSS, JS, build files, compose, and shared docs.

1. **Grep twice: once for the feature name, once for the project name.** A feature grep misses things named after the sample — an HTTP `server_version` string, a compose volume name, an env-file name, entrypoint echo strings, an image tag.
2. **Check dead references in both directions**, which grep cannot do. For UI, cross-check CSS selectors against markup and JS, *and* the element ids the JS caches against the markup. One direction finds dead styling, the other finds missing elements. Watch for false positives (hex colour literals look like id selectors) and confirm the checker before trusting it.
3. **Re-examine the abstractions the removed feature justified.** Scaffolding that was proportionate before is over-engineering afterwards — a hand-written deep-copy that existed only because one field was mutable, a lock guarding state nothing shares any more, a listener hook with one caller. Deleting the feature is half the job.
4. **Read the container's own startup log after a rename.** Operator-facing strings — banners, log prefixes, server headers, error text — are invisible to behavioural tests, which assert what the code does and not what it calls itself.
5. **Check whether the removed code was the only demonstration of a shared capability.** In a sample repo the code is the documentation, so losing the sole example of an SDK function is a real coverage gap. Say so rather than letting it disappear silently.
6. **Do not re-add scope while simplifying.** If a simplification leaves a gap, report it and let the user decide instead of quietly reintroducing what was just removed.
7. Finish by updating the sample's README in the same change, and re-grep the whole repo including `docs/` for the old name.

## Phase 2b: Verify Before Declaring Done

Most of this runs on the development machine. Do it before claiming the container works — a build exiting cleanly is not evidence that it runs. Full detail in "Verifying Before Deployment" in `docs/container-development-guide.md`.

1. Build for **both** architectures. A package available for arm64 is not guaranteed to exist for arm/v7, and the smallest routers are arm/v7.
2. Run the arm64 image locally (native on an ARM Mac, qemu elsewhere) and check startup, the endpoints, malformed input, and `docker stop` returning promptly rather than timing out to SIGKILL. For anything with a Python polling loop or a startup call to `cp.wait_for_uptime()`/`wait_for_ntp()`/`wait_for_wan_connection()`, stop it *while inside the sleep or the wait*, not just at idle — a signal handler firing does not make a blocked `time.sleep()` return early (PEP 475), so a flag-setting handler alone still waits out the current sleep or the wait function's own timeout (300s by default) before shutdown proceeds. See "Signal Handling in Polling Loops" in `docs/ncos-sdk-reference.md` for the interruptible-sleep pattern.
3. Exercise the no-Config-Store path deliberately: locally there is no `cs.sock`, which is exactly what a deployment missing the `$CONFIG_STORE` volume looks like. It should degrade visibly instead of resembling an absence of data.
4. Unit-test pure logic (coordinate conversion, geometry, protocol formatting, state machines) directly, and validate any generated wire format by feeding it to the real consumer rather than only checking it for well-formedness.
5. Report measured numbers — image size per architecture, and what was and was not verified. Do not estimate sizes that can be measured.
6. Never run entrypoints or config-generation scripts on the host — they write absolute paths like `/etc/<daemon>/`. Run them inside the built image with `--entrypoint sh` so the writes are contained.
7. Config Store logic can be tested without a router by binding a mock `AF_UNIX` socket and overriding `cp.SOCKET_PATH`. See "Verifying Before Deployment" in `docs/container-development-guide.md`.
8. Clean up test artifacts, temporary scripts, local images and `__pycache__` before finishing.
9. Report what was verified in the chat response, not in the sample's README. A README describes the container to someone deploying it; build-time verification notes (image sizes, what was run locally vs. not verifiable without a router, docker stop timing) are a different audience and go stale the moment the container changes. Keep the README to what it does, its files, configuration, building, and deployment.

## Phase 2c: Verify On the Development Router

Local verification cannot cover Config Store behaviour, image pulls, or anything the router itself does. When a development router is available, the whole loop is scriptable — see `tools/README.md` and `docs/ncos-api/config/container.md`.

1. `python3 tools/dev_router.py check` first. It reports model (which fixes the architecture), firmware, and container projects, and fails loudly when `.env` is not configured.
2. **Confirm the registry namespace and image visibility with the user before pushing.** A push writes to a shared, often public registry account; a public repo is world-visible and effectively permanent. Read `config/container/registry` and any existing project's `image:` to see what the router already pulls from, rather than assuming a registry needs configuring — an empty `registry` array means anonymous Docker Hub pulls, which is all a public image needs.
3. Build for the architecture the connected router reports, push, then create the project by writing `config/container/projects` rather than clicking through the UI for every iteration.
4. **Read an existing project's compose string first.** It is direct evidence of what this firmware accepts, and better than any example.
5. Verify every config write by reading it back. REST writes use form-encoded `data=`, not a JSON body (see `docs/ncos-api/config/README.md`).
6. Watch `container list` for the project's containers to appear — but remember it reads project *config*, so it answers normally even when the engine is dead. A project listed with no containers under it means the pull is running, the engine is wedged, or the engine is not running at all; discriminate with `status/container` (data / hangs / prompt `null`) and whether `status/log` contains any `containers`-facility lines. Full table in `docs/containers-quick-start.md`. **Verify the platform subsystem is alive before debugging your own compose or image** — when the platform is the variable, time spent on your artifact is wasted. This applies doubly right after a firmware upgrade, which can leave the engine down while project config and entitlement both survive intact.
7. **Do not trust the router's own diagnosis of a resource problem.** The container engine logs `High system CPU load?` as a guess whenever a call times out; verify against `status/system` (`cpu`, `load_avg`, `memory`) before acting. Acting on that message unverified leads to disabling unrelated workloads for no reason.
8. Retrieve output with `dev_router.py ssh container logs <name>`. `container list`, `container logs` and `container exec` are CLI-only and have no REST equivalent, so any host-side workflow needs the SSH path as well as HTTP.
9. Reversibility: creating or updating a project changes a router someone may be using. Prefer adding a new project over modifying an existing one, and ask before disabling or removing a workload that is not yours.

## Phase 3: Key Constraints to Remember

- **Bridge networking only** — Host networking is not supported. Containers use bridge mode by default. To give a container its own IP on a LAN, define a custom Compose network bound to a Local IP Network via `com.cradlepoint.network.bridge.uuid` in `driver_opts`, with matching `subnet`/`gateway` in `ipam`. The container can then be assigned a static IP via `networks.<name>.ipv4_address`.
- **No host filesystem mounts** — only named volumes, Config Store (`$CONFIG_STORE`), and USB storage (`$USB_STORAGE`)
- **User namespace remapping** is active — file ownership can change to `nobody:nobody`
- **Memory is limited** — especially on AER2200/IBR1700 (as low as 135 MB)
- **Flash storage is limited** — 6-14 GB total, keep images small
- **Compose version 2.4** is the standard format (use `mem_limit` for memory, not `deploy.resources`)
- **Config Store** access requires the `$CONFIG_STORE` volume in Compose YAML (bare, no mount path). Without it, all `cp.py` calls return `None`.
- **NCM custom alerts DO work from a container** — verified end-to-end on R980 / NCOS 7.26.21: a container with only `$CONFIG_STORE` and no SDK app registration sent the `alert` verb and the alerts appeared in the NCM console as `Custom Alert` rows. `cp.alert('text')` is implemented and returns `True` when accepted. The repo's former claim that alerts need the on-router SDK app context was never tested and was wrong. Two traps: NCM does **not** display the `name` field, so put everything in the value; and an empty or malformed alert still creates an NCM entry reading `Router NCOS App Generated Alert` with none of your text, so never send unvalidated alert text. Alerts are a shared, human-facing, rate-limited channel — send transitions and exceptions, debounced, not periodic samples.
- **Config store events (`cp.register()`) remain UNVERIFIED** — said to need the event socket, no test on record. Default to polling and comparing against the previous sample. Do not justify a workaround, an extra dependency or a dropped feature with that claim without testing it first — see the Phase 1 gate.
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

### edge_ai/ — Computer Vision / AI pattern

The `edge_ai/` directory is a reference for complex multi-threaded applications with video processing, AI inference, and web UIs:
- `cp.py` — the minimal Config Store client (identical to the canonical copy at the repo root)
- `edge_ai/src/main.py` — Entry point: signal handlers, component initialization, thread orchestration, graceful shutdown
- `edge_ai/src/config.py` — Configuration via `cp.get_appdata()` / `cp.put_appdata()` with full validation and self-provisioning defaults
- `edge_ai/src/capture.py` — RTSP capture via PyAV with TCP transport, frame skipping, disconnect detection, and exponential-backoff reconnection
- `edge_ai/src/inference.py` — TFLite inference engine supporting SSD MobileNet V2 and YOLOv5n, pre-allocated buffers, NMS, thread-safe threshold updates
- `edge_ai/src/annotation.py` — OpenCV-based bounding box drawing with confidence color-coding, FPS overlay, rolling FPS calculator
- `edge_ai/src/processor.py` — Pipeline orchestrator: capture→infer→annotate with adaptive rate control, inference frame skipping, double-buffer frame sharing
- `edge_ai/src/web_server.py` — MJPEG streaming, REST API (stats/config/control), multi-user session control, static file serving
- `edge_ai/src/models.py` — Dataclasses: Detection, AppConfig, RuntimeStats
- `edge_ai/src/templates/index.html` — Self-contained web UI (no CDN dependencies)
- `edge_ai/models/` — TFLite model files (INT8 quantized for ARM64 XNNPACK)

Key patterns to study in edge_ai:
1. **Multi-threaded pipeline** with `threading.Event` for shutdown coordination
2. **Appdata-driven config** that self-provisions defaults on first run
3. **RTSP reconnection** with exponential backoff (2→4→8→...→60s cap)
4. **Performance optimization**: pre-allocated buffers, NEON SIMD via OpenCV, frame skipping, annotation skipping when no clients connected
5. **Adaptive rate control**: auto-reduces FPS when inference is too slow, restores when latency recovers
6. **MJPEG streaming** via Python's built-in http.server with ThreadingMixIn
7. **Primary-user session** control for multi-viewer scenarios

### rtsp_viewer/ — Wrapping an off-the-shelf binary, no cp.py

The `rtsp_viewer/` directory is a reference for containers that have no
Config Store integration at all — not every sample needs `cp.py`:
- `Dockerfile` — picks the go2rtc release binary matching `TARGETARCH`/
  `TARGETVARIANT` at build time, so one Dockerfile covers both architectures
  instead of hardcoding a single platform's asset URL
- `entrypoint.sh` — generates config from environment variables only if no
  config file is already present, so the same image supports both a
  bind-mounted config (local dev) and an env-var-only deployment (NCOS, which
  cannot bind-mount host files)
- Two compose files: `docker-compose.yml` for local build-and-run, and
  `docker-compose.cradlepoint.yml` as the NCOS deployment example — a pattern
  worth reusing whenever a sample's local and router configuration diverge
  enough that one file would need contradictory comments

Key patterns to study in rtsp_viewer:
1. **Multi-arch binary selection from buildx `TARGETARCH`** instead of one
   Dockerfile per architecture
2. **Config-file-or-environment-variables**, picked at container startup based
   on which is present
3. **Wrapping a third-party binary's own auth**, rather than adding a proxy —
   go2rtc has Basic auth built in, so the container's job is to plumb
   credentials through as environment variables, not reimplement auth
4. A concrete instance of the standing rule: **never point a Cradlepoint
   compose file's `image:` at someone else's public registry image.** Build
   and push this sample's own Dockerfile instead
