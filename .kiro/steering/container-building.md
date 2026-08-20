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

2b. **Gate: is the limitation you are designing around actually verified?** If a workaround, an extra dependency, a different architecture, a dropped feature, **or a choice of one mechanism over another** is being justified by "the platform cannot do X", find the evidence for X before building on it. Preferring mechanism B because A "can't work" is the same failure as building a workaround for a limitation that does not exist, and it is easier to miss because nothing about the resulting design looks like a workaround — search for the capability before ruling it out. Claims in this repo's own docs are marked UNVERIFIED where no test exists — read the evidence column, not just the claim. Writing a single-purpose probe container to settle it is cheap: most of it verifies on the development machine, and it converts an assumption into a fact that benefits every later build. Never restate an untested claim as established, in docs or in conversation.

   **Probe on the router, not on the development machine, for anything kernel- or engine-dependent.** A local experiment cannot answer what the router's kernel supports or what its engine grants (see Phase 2b step 1b for the three buckets), and running one anyway produces a confident answer about the wrong machine. If the question is "does this kernel have X" or "does the engine allow Y", the probe belongs on the router from the start.

   **Try the cheapest probe first.** Some questions are answerable by reading state in a container that is *already deployed* for an unrelated purpose — one `container exec` and no build at all. Reserve a purpose-built probe container for questions that need a capability, a device mapping, or a package the existing containers do not have. When a result comes back: ask for the **device model and firmware** and record them with it, or the observation cannot be scoped and the next device needs the probe re-run anyway. Write down what the result does **not** establish alongside what it does — name the mechanism the probe actually exercised, because a bare "confirmed" is read as clearance for every adjacent question. Then sweep the repo for every place that framed the question as open: a leftover "this is unverified, probe it first" paragraph reads as current and invites someone to re-run settled work or design around a disproved limitation. Grep for the *concept* (headings, a distinctive phrase from the claim), not only the identifier — prose often refers to a setting by description rather than by name, so a token grep returns a false all-clear.

2c. **Gate: does the consumer already have direct access to the resource?** Before designing a mediating service (an adapter, proxy, or bridge process) so that "app A can access X", check whether A already has direct access to X in the deployment shape actually being discussed — e.g. a same-container process reaching `cs.sock` directly needs no `cp.py`-wrapping adapter at all, since the socket is already in that container's filesystem namespace and most languages can speak its small line-based protocol themselves. A mediating layer is justified only when there's a concrete barrier: a genuinely separate container or external host, or a need to centralize auth/allowlisting across multiple untrusted consumers. Ask this before scaffolding anything.

2d. **When a requirement changes, re-derive earlier recommendations instead of appending to them.** Advice is a function of the requirements that produced it, so changing an input can flip the output with no new information about the platform — and quietly swapping a recommendation leaves a contradiction in the conversation with no explanation. State plainly which earlier answers reverse and why. Watch especially for changes that *widen scope* (a subset becoming everything, a specific selector becoming a catch-all, one consumer becoming all consumers): these do not merely add load, they can change which mechanism is correct and activate failure modes that were previously unreachable, so the verification done for the narrow case does not carry over.

2e. **If the request is a feasibility question rather than a build request, still verify by executing.** "Can the router support a container that does X" answered from reasoning alone produces a confident, plausible, partly-wrong answer — the failure mode this repo has recorded more than any other. Build a throwaway probe in a scratch directory **outside the workspace** (`/tmp`), run it, quote measured numbers and real log lines in the answer, then delete it and confirm `git status` is clean. "Don't write code" constrains the deliverable, not the verification. Say explicitly which parts of the answer were executed and which remain inference — for a feasibility answer that distinction is most of the value. Where the design would depend on a capability this repo marks UNVERIFIED, name the one-command probe the user can run on their own router rather than leaving the question open.

3. If the user is asking what to build rather than naming a specific app, inventory the patterns the existing samples already cover (see Phase 4) and propose something that fills a gap. Recommend, then confirm before writing code.

4. Clarify with the user:
   - What the container should do
   - Which router model(s) it targets (determines architecture and memory)
   - Whether it needs Config Store access (cp.py / cs.sock)
   - Whether it needs a LAN IP address (custom network) or just port mapping on the default bridge
   - Whether it needs USB devices or shared volumes
   - Whether it only reads router state or also writes config via `cp.put()` (writes need an allowlist and an off-by-default appdata flag)
   - Any specific NCOS version requirements
   - **If the container integrates with third-party equipment, ask for that equipment's actual configuration dump before writing the integration** — not a description of it. Every negotiated parameter (authentication method, identities, proposals, selectors, address assignment, timers) is in there, and each one guessed is a silent failure at connect time. Ask also for **a working client configuration for the same peer** if one exists: a profile that is already known to connect is a specification, and it independently confirms the parameters the peer's own config leaves ambiguous.
   - **Track which of your open questions are still open.** Confirming one item from a list of several does not answer the rest, and a partial answer reads as agreement with the whole question. Re-state what remains unanswered rather than proceeding as if silence were confirmation.

   Note on port mapping: mapped ports are exposed on WAN as well as LAN and the router firewall does not filter them. Flag this whenever the user picks port mapping for a service that should not be internet-facing, and offer the Local IP Network alternative.

## Phase 2: Build the Container

Follow these conventions established in this repo:

1. **Dockerfile**:
   - Use a **pinned** Alpine tag (`alpine:3.18`, matching the existing samples), not `alpine:latest`. Router deployments are rebuilt and redeployed months apart, and an unpinned base silently changes the Python version and package set between builds
   - **Package presence is not feature presence.** Where the design needs a specific plugin, backend or optional feature of an off-the-shelf daemon, verify that *feature* exists in Alpine's build of the package before committing to the base image — `apk add` it in a throwaway container and list the module directory. The image builds and starts either way, so this is invisible until runtime. If the feature only exists on another base or via compiling from source, treat that as a decision with a measured size cost to report, not something to switch silently or to quietly drop the feature over
   - Install only necessary packages with `--no-cache`
   - Copy application files to `/opt/<app_name>/`
   - Set `PYTHONPATH` if using cp.py
   - Set `ENV CP_APP_NAME=<service>` whenever cp.py is used. `APP_NAME` falls back to `basename(cwd)`, which is empty at `/`, so without it every log line is prefixed with the generic `container:` — and `alert()` sends the value as a protocol field, so it should not depend on the working directory
   - Use an `entrypoint.sh` script for initialization logic
   - **Preflight the platform grants the container depends on, in the container's own entrypoint** — non-default capabilities, device mappings, kernel state. One `PREFLIGHT ok`/`FAILED` line per check naming the compose key that would fix it, and refuse to start when one fails rather than proceeding into a half-working data path. The first deployment then answers the platform questions from `container logs` without a separate probe project, and the same output later distinguishes a withdrawn grant from an application fault
   - **Where the container installs the rules enforcing its own safety property, install them before starting the process whose traffic they govern**, or startup has a window in which traffic takes the path those rules exist to prevent. Netfilter accepts rules naming an interface that does not exist yet, so rules referring to an interface the daemon creates later can go in up front
   - **Record what each safety rule is keyed on**, because a later implementation swap can void it silently. Rules keyed on an artifact of one implementation (an interface name, a device path, a process name, a log format) match nothing under an implementation that does not produce that artifact — and the failure is asymmetric: the primary function keeps working while the safety property quietly stops applying. When evaluating an alternative implementation, the question is not "does it work" but "does it still produce everything my rules are keyed on"
   - **Validate required configuration first and exit naming everything unset.** An absent setting must never be able to present itself as a runtime failure
   - Expose only necessary ports with protocol (e.g., `EXPOSE 1161/udp`)
   - If the main process comes from a downloaded release binary rather than an `apk`/`pip` package, select the asset from buildx's `TARGETARCH`/`TARGETVARIANT` build args with an explicit failing default case — never hardcode one architecture's asset URL. `apk`/`pip` already resolve architecture for you; this only applies to hand-rolled downloads. See "Vendoring a Prebuilt Binary" in `docs/container-development-guide.md`
   - Not every container needs `cp.py` or `$CONFIG_STORE` — only include them if the container actually reads or writes router state. Decide this from Phase 1's clarifying questions, not from what other samples in this repo happen to do

2. **Python applications using cp.py**:
   - Copy `cp.py` from the repo root (canonical copy; each sample keeps an identical copy because the Docker build context cannot reach outside the service directory). It is standard library only — do not add `py3-requests`
   - Use `cp.get()`, `cp.put()`, `cp.log()` etc. for router communication
   - Use `cp.get_appdata()` for user-configurable settings
   - Use `cp.wait_for_uptime()` and `cp.wait_for_wan_connection()` at startup if needed, and **pass them the `stop` event** (`cp.wait_for_uptime(60, stop=stop)`). Use a `threading.Event` as the shutdown flag rather than a bare boolean: `Event.wait(interval)` is both the poll sleep and the flag check, and the readiness helpers accept the same event. Without it, a SIGTERM during a startup wait is not acted on until that call's own timeout (300s) expires, which outlasts the container stop grace period and ends in SIGKILL
   - When feeding Config Store data to an off-the-shelf daemon, use a loopback TCP/UDP socket rather than a FIFO or pty posing as a device file, and emit the target protocol's explicit invalid/no-data value when router state goes stale instead of repeating the last known value
   - `cp.py` swallows its own errors: reads return `None` and writes return normally whether or not they succeeded. Use `cp.config_store_available()` to tell "no Config Store" apart from "no data", verify any write that gets reported to a user by reading it back, and read a `config/...` path before writing it. See the Error Handling Contract in `docs/ncos-sdk-reference.md`
   - **Prefer "attempt the read, then explain a `None`" over "ask whether the backend is up, then decide whether to read".** `cp.config_store_available()` is reliable again (it re-probes a failed backend on a 30s cooldown, and a hung Config Store now counts as a failure rather than a success — both fixed 2026-08-17 and regression-tested), but a gate consulted before every read still adds a way to be wrong without adding information, and inside the cooldown window it answers from cache anyway. Call it when a `None` needs explaining to a human, not to decide whether to try
   - Treat a documented behaviour of a shared module as a claim to verify, not a fact, whenever a design depends on it. This repo's own docs have twice described `cp.py` behaviour the module did not have. Executing it against a mock socket settles the question in minutes; reading it does not — `tests/test_cp.py` is the harness to extend
   - `cp.py` can also drive a *remote* router over HTTP (`cp.use_rest()`), which is for development machines: it lets an application's logic be exercised against a real router before it is containerised. **On the router it is refused** — `use_rest()` raises when the Config Store socket exists, since local access is already available and REST would only add credentials and the risk of addressing the wrong device — and it never engages on its own, so a missing `$CONFIG_STORE` volume still fails visibly. Never put router credentials in an image; the socket needs none. `force=True` exists only for a container deliberately reaching a *different* router, which is a decision to weigh, not a convenience
   - Before writing any `config/...` field, confirm its type and meaning in the DTD (`docs/ncos-api/dtd-usage.md`), not from example code. Semantics are per-path — the same field name can mean the opposite thing in another section — and when a DTD comment is ambiguous the shipped defaults are the strongest available evidence
   - **Never pipe an exploratory config-tree search through `head`.** A truncated listing reads as a complete one, and concluding "the platform has no X" from output that was cut off is indistinguishable from having searched properly. `PATHS.md` groups a subtree's entries across hundreds of lines, so the capability that answers the question is frequently below the cut. Count matches first (`grep -c`), then read all of them, or narrow the pattern until the full result set is small enough to read.
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

1. Build for **both** architectures. A package available for arm64 is not guaranteed to exist for arm/v7, and the smallest routers are arm/v7. This applies to pip packages as much as apk packages — compiled-extension Python packages (numpy, opencv-python-headless, PyAV, TFLite runtimes) frequently ship arm64 wheels but no armv7 wheel at all for a given pinned version. Don't conclude this from a failed `pip download`/`pip install`: its error is identical whether the wheel truly doesn't exist or the platform/Python-version tag in the command was wrong. Check `https://pypi.org/simple/<package>/` directly and grep for the actual wheel filenames — the absence of any `armv7l`-tagged wheel for that exact pinned version across the whole listing is the real evidence. Re-verify this for any inherited Dockerfile comment claiming an architecture limitation before building automation (a CI matrix, a per-sample override) around it, rather than trusting the comment.
1b. **Know what a local run cannot decide, before running it.** The development machine is not the router: Docker Desktop runs a linuxkit VM kernel and applies no user namespace remapping, while the router runs Cradlepoint's kernel under `cpdockerengine`. Sort every local result before quoting it. *Transfers:* image contents, package availability per architecture, application logic, generated config parsing, PID 1 signal handling. *Does not transfer — kernel configuration:* which subsystems, modules, device types and virtual interface types exist (anything behind a `CONFIG_*` option — kernel IPsec/XFRM, tunnel drivers, netfilter modules, `ip link add ... type <x>`). *Does not transfer — engine and namespace policy:* what `cap_add` actually grants, whether `devices:`/`sysctls:` are honoured, restart and health check internals, resource limits, and anything userns remapping affects. **A local kernel-feature result is unsafe in both directions** — absent locally does not mean absent on the router (you would drop a viable design), present locally does not mean present on the router (you would ship one that fails at deploy). Questions in the last two buckets are answered by `container exec` on the router and nowhere else; do not run a local experiment to settle them and do not report one as if it had.
2. Run the arm64 image locally (native on an ARM Mac, qemu elsewhere) and check startup, the endpoints, malformed input, and `docker stop` returning promptly rather than timing out to SIGKILL. For anything with a Python polling loop or a startup call to `cp.wait_for_uptime()`/`wait_for_ntp()`/`wait_for_wan_connection()`, stop it *while inside the sleep or the wait*, not just at idle — a signal handler firing does not make a blocked `time.sleep()` return early (PEP 475), so a flag-setting handler alone still waits out the current sleep or the wait function's own timeout (300s by default) before shutdown proceeds. See "Signal Handling in Polling Loops" in `docs/ncos-sdk-reference.md` for the interruptible-sleep pattern.
3. Exercise the no-Config-Store path deliberately: locally there is no `cs.sock`, which is exactly what a deployment missing the `$CONFIG_STORE` volume looks like. It should degrade visibly instead of resembling an absence of data.
4. Unit-test pure logic (coordinate conversion, geometry, protocol formatting, state machines) directly, and validate any generated wire format by feeding it to the real consumer rather than only checking it for well-formedness. When the consumer is third-party equipment nobody has on the desk, run an open-source implementation of the **peer** role in a second local container, and state which half of the question that settles — it shows this container can do it, not that the vendor's box will accept it, since both ends are then the same implementation. For anything that forwards or routes on behalf of other hosts, use a three-container topology (client, container under test, peer): a two-container test only exercises traffic the container originates itself, which takes a different kernel path and misses routing, NAT and MTU behaviour.
4b. **Test the dependency-down state, and assert at the endpoint rather than the middlebox.** For anything forwarding, proxying or relaying other hosts' traffic through a conditional path, verify behaviour with that path down as well as up — the usual result is a silent fallback that egresses somewhere it should not, in the clear, with nothing looking broken. Prefer explicit fail-closed (default-deny forwarding, allow only the intended egress plus the conntrack return direction) and re-test the happy path afterwards, since a fail-closed ruleset that also blocks the working case is an easy mistake. Separately, assert success at the endpoint that consumes the service: a middlebox's own counters can show traffic in both directions while the client sees nothing. Reset counters between phases or reason about deltas — an absolute count says nothing about which phase produced it. Also re-test the **reverse** direction whenever a selector, route, NAT or firewall rule is broadened to a catch-all, since a wildcard installed for one direction usually matches the return path too.
4c. **Test recovery by inducing a failure outside the daemon's own trigger set.** A daemon that advertises reconnection usually has several mechanisms, each firing on a specific trigger (config load, peer-initiated close, liveness-check failure). Tear the session down administratively instead — a route none of those cover — and see whether anything re-establishes. If nothing does, add a watchdog keyed on observed session state; do not rely on a health check to trigger a restart, since whether `cpdockerengine` restarts an unhealthy container is UNVERIFIED (plain Docker Engine does not). Recovery belongs in the container, where it does not depend on engine behaviour.
4d. **Use a separate throwaway container for test tooling rather than adding it to the image.** A production image should not ship `ping`, `nc` or similar just so a test can use them. Run a minimal image for the client role, and attach a tool container to another container's network namespace with `--network container:<name>` when a listener or capture is needed inside a namespace under test. Keeping the tools out means the thing verified is the thing shipped.
4e. **Assert on the data plane, not just the control plane, and on loaded modules, not just installed files.** A session reported as established says nothing about whether payload transits — send real traffic, read byte/packet counters at both ends, exercise TCP rather than only ICMP (only a stateful protocol tests connection tracking, only a real payload tests MTU), and where addresses are translated, verify the source the *peer* observes. Separately, for any daemon with optional plugins: a module can be present as a real file with `load = yes` and silently fail to load on an unmet dependency, while the daemon's own `failed to load` lines name a dozen unrelated modules. Capture the runtime line that enumerates loaded modules and confirm the ones your feature needs appear there. Installed is not loaded, and loaded is not working.
5. Report measured numbers — image size per architecture **and resident memory with the workload running**, plus what was and was not verified. Do not estimate what can be measured. These are different constraints: image size is a flash and pull-time cost, RSS is what competes with router services for the memory allowance, and they can be orders of magnitude apart. A large image does not by itself rule a design out on memory grounds.
6. Never run entrypoints or config-generation scripts on the host — they write absolute paths like `/etc/<daemon>/`. Run them inside the built image with `--entrypoint sh` so the writes are contained.
7. Config Store logic can be tested without a router by binding a mock `AF_UNIX` socket and overriding `cp.SOCKET_PATH`. See "Verifying Before Deployment" in `docs/container-development-guide.md`, and `tests/test_cp.py` for a worked example (`python3 -m unittest discover -s tests`). That harness is worth the hundred lines it costs: written to review `cp.py` on 2026-08-17, it found six behavioural bugs in the shared client itself. `cp.py` is covered by it now; a sample's own logic is not.
8. **Induce the failures your health and status code exists to detect, and watch it report them.** Confirming a probe says "healthy" while things are healthy tests almost nothing — a probe can be wrong only in the red direction. For anything touching `cs.sock`, exercise three distinct states, since only the first happens for free locally: socket absent, socket present but never answering (a hung Config Store, which is what a wedged container engine looks like), and socket answering with a truncated or non-JSON body. Any cached "unavailable" state needs an explicit path back to available, or one transient startup failure becomes a permanent one in a container that still looks alive.
9. **Check paths case-exactly, against `git ls-files` rather than the filesystem.** The image is Linux and case-sensitive; macOS is not, so a `COPY`, `PYTHONPATH`, `import` or documented path whose case is wrong builds fine locally and fails on a Linux builder. `os.path.exists()` and `ls` will happily confirm a path a fresh clone does not have. This applies to the dangling-reference sweep as well — that checker gives false negatives on macOS unless it consults git. Keep the extractor strict when sweeping docs (markdown links, plus backticked tokens that end in a real file extension or start with a known top-level directory): a looser one flags every path-shaped token that is not a path — API prefixes like `status/`, URL schemes, generic filenames, sample nicknames — and a noisy report gets ignored, which is worse than no report. Do this check at the **directory** level too, not only per-file: compare `git ls-files containers/ | sed -E 's#^(containers/[^/]+)/.*#\1#' | sort -u` against `find containers -maxdepth 1 -type d`, since a whole sample directory can drift this way and the symptom then shows up in CI as an OCI naming error (repository names must be lowercase) rather than a file-not-found, which is easy to misdiagnose as a workflow bug instead of a case mismatch. Any script that derives a name from a directory listing (a CI matrix, an image tag, a generated service name) should lowercase it defensively rather than trust the filesystem's reported case.
10. **Verify scripted multi-site edits by match count, not by exit status.** A bulk `str.replace()` across a file needs an asserted occurrence count or a bounded target section, then a read of each changed site. A short pattern intended for one place routinely matches three, and the script reports success either way.
11. Clean up test artifacts, temporary scripts, local images and `__pycache__` before finishing.
12. Report what was verified in the chat response, not in the sample's README. A README describes the container to someone deploying it; build-time verification notes (image sizes, what was run locally vs. not verifiable without a router, docker stop timing) are a different audience and go stale the moment the container changes. Keep the README to what it does, its files, configuration, building, and deployment.

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
- **Linux capabilities are UNVERIFIED** — whether `cap_add` (`NET_ADMIN`, `NET_RAW`) and `devices:` mappings like `/dev/net/tun` are honoured under `cpdockerengine` is not confirmed anywhere in this repo. Probe on the router before designing around any of them; the probes are one command each and are listed in `docs/container-development-guide.md`. Where a daemon offers both a kernel-facility implementation and a userspace one needing only a TUN/TAP device, prefer the userspace path — it scopes the request to the container's own namespace and degrades predictably rather than failing with `EPERM` inside third-party code
- **Namespaced sysctls are readable but not writable** — `/proc/sys` is mounted read-only, so `sysctl -w` fails even with `CAP_NET_ADMIN`. The only lever is a Compose `sysctls:` entry, and whether `cpdockerengine` honours it is UNVERIFIED. That makes the *current* value the thing that matters for anything routing or NATing other hosts' traffic. **`net.ipv4.ip_forward` was observed as `1`** on a production router, read via `container exec` in an ordinary container with no `cap_add` and no `sysctls:` entry — so forwarding appears enabled by default and the read-only `/proc/sys` is not a blocker. Model/firmware were not captured, so re-read it on the target device (one command) rather than assuming it fleet-wide. It says nothing about whether `cap_add` or `devices:` are honoured
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

### snmp_agent/ — Simple daemon pattern

The `snmp_agent/` directory is a reference for simple long-running daemons:
- `containers/snmp_agent/Dockerfile` — Alpine base, minimal packages, entrypoint pattern
- `containers/snmp_agent/entrypoint.sh` — Config generation then exec into main process
- `containers/snmp_agent/gen_conf.py` — Reading router config via cp.py to generate app config
- `containers/snmp_agent/ncos_snmp.py` — Long-running daemon using cp.py for data
- `containers/snmp_agent/cp.py` — The SDK module (copy into new containers)

### edge_ai/ — Computer Vision / AI pattern

The `edge_ai/` directory is a reference for complex multi-threaded applications with video processing, AI inference, and web UIs:
- `containers/edge_ai/cp.py` — the minimal Config Store client (identical to the canonical copy at the repo root)
- `containers/edge_ai/src/main.py` — Entry point: signal handlers, component initialization, thread orchestration, graceful shutdown
- `containers/edge_ai/src/config.py` — Configuration via `cp.get_appdata()` / `cp.put_appdata()` with full validation and self-provisioning defaults
- `containers/edge_ai/src/capture.py` — RTSP capture via PyAV with TCP transport, frame skipping, disconnect detection, and exponential-backoff reconnection
- `containers/edge_ai/src/inference.py` — TFLite inference engine supporting SSD MobileNet V2 and YOLOv5n, pre-allocated buffers, NMS, thread-safe threshold updates
- `containers/edge_ai/src/annotation.py` — OpenCV-based bounding box drawing with confidence color-coding, FPS overlay, rolling FPS calculator
- `containers/edge_ai/src/processor.py` — Pipeline orchestrator: capture→infer→annotate with adaptive rate control, inference frame skipping, double-buffer frame sharing
- `containers/edge_ai/src/web_server.py` — MJPEG streaming, REST API (stats/config/control), multi-user session control, static file serving
- `containers/edge_ai/src/models.py` — Dataclasses: Detection, AppConfig, RuntimeStats
- `containers/edge_ai/src/templates/index.html` — Self-contained web UI (no CDN dependencies)
- `containers/edge_ai/models/` — TFLite model files (INT8 quantized for ARM64 XNNPACK)

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
- `containers/rtsp_viewer/Dockerfile` — picks the go2rtc release binary matching `TARGETARCH`/
  `TARGETVARIANT` at build time, so one Dockerfile covers both architectures
  instead of hardcoding a single platform's asset URL
- `containers/rtsp_viewer/entrypoint.sh` — generates config from environment variables only if no
  config file is already present, so the same image supports both a
  bind-mounted config (local dev) and an env-var-only deployment (NCOS, which
  cannot bind-mount host files)
- Two compose files: `containers/rtsp_viewer/docker-compose.yml` for local
  build-and-run, and `containers/rtsp_viewer/docker-compose.cradlepoint.yml` as the NCOS deployment example — a pattern
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
