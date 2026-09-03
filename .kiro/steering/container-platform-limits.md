---
inclusion: auto
description: Affirmative cheat sheet of NCOS container platform facts — compose rules, networking, volumes, health checks, per-model memory, Config Store behaviour from inside a container, measured image sizes
---

# NCOS Container Platform Facts

Dense reference for what the platform does, so it is not re-derived per session.
Every statement is present tense and affirmative, including the negative
constraints. A claim with no evidence marker is established repo-wide knowledge;
`UNVERIFIED` next to a claim means no test is on record and it must not be
designed around without one. On-router observations carry the device model and
firmware where those were captured, and say so where they were not.

Deeper narrative lives in `docs/container-development-guide.md`,
`docs/cs-sock-protocol.md`, `docs/ncos-sdk-reference.md` and
`docs/memory-resources.md`. `.kiro/steering/lessons-learned.md` is a closed
archive, loaded manually with `#lessons-learned` when the history behind a fact
is needed.

## Target

- The router runs `linux/arm64` (aarch64), or `linux/arm/v7` on AER2200 and
  IBR1700. Always pass `--platform` to `docker buildx` and build both
  architectures; a package existing for arm64 does not mean it exists for
  arm/v7, and the most memory-constrained models are the arm/v7 ones.
- Alpine is the default base and uses musl libc, not glibc. Pin the tag
  (`alpine:3.18`), because router images get rebuilt months apart and an
  unpinned base changes the Python version and package set silently.
- The engine is `cpdockerengine`. It exposes no Docker API and no Docker socket;
  the interfaces are the `container` CLI and `status/container` over REST.
- Containers run user-namespace remapped with no root on NCOS. File ownership
  appears as `nobody:nobody`. Remapping also affects SysV IPC: a daemon using
  shared memory logs `shmctl(...) for IPC_RMID failed, Operation not permitted`
  at shutdown. That message is benign; verify the daemon's actual service
  instead of chasing it.
- `/proc/sys` is mounted read-only, so `sysctl -w` fails even with
  `CAP_NET_ADMIN`. The only lever is a compose `sysctls:` entry, and whether
  `cpdockerengine` honours it is UNVERIFIED. Design against the value the
  kernel already has rather than around setting it.
- The router kernel decides which kernel modules, netfilter tables, device
  types and `ip link` types exist, and the development machine cannot answer
  that. A local kernel result is unsafe in both directions: absent locally kills
  a design the router supports, present locally ships one that fails on deploy.
  Before running a local experiment, name what the result is a property of — "this
  image" or "this code" is answerable locally, "this kernel" or "this engine" is not.

## Compose

- Compose file version is `2.4`. Memory caps use `mem_limit`, not
  `deploy.resources`.
- The router pulls pre-built images from a registry. A `build:` directive is not
  supported on the device; build off-router with `docker buildx` and push.
- Compose YAML is a plain string field in a config array under
  `config/container`, so the whole build → push → deploy → read-logs loop is
  scriptable over REST or SSH without touching the NCM UI. Read an existing
  project's compose string first: it is direct evidence of what the firmware has
  already accepted.
- The platform interpolates `$` in every compose value. That is the mechanism
  behind `$CONFIG_STORE` and `$USB_STORAGE`. Any other `$` expands to an empty
  string, so escape a literal dollar by doubling it (`P@ss$$word`,
  `$$HOSTNAME`). This applies to `command`, `entrypoint`, `environment` and
  `healthcheck` alike, and a truncated secret is the most damaging case because
  it fails silently rather than erroring.
- Quote `restart: "no"`. Bare `no` is boolean false in YAML 1.1. Confirmed on
  hardware (model and firmware not captured).
- Set `container_name` explicitly. Without it the engine derives a name such as
  `a_a_1`, which nobody guesses when reaching for `container logs <name>`.
  Confirmed on hardware (model and firmware not captured).
- Omit the `logging:` block for an NCOS deployment. The engine attaches the
  **syslog** driver with `tag: {{.Name}}` and an empty `LogPath` regardless, so a
  `json-file` request is silently overridden and implies a driver you do not get.
  Observed on hardware (model and firmware not captured). A `json-file` block is
  still useful in a local development compose file, where it works.
- A compose key the engine silently overrides is worse than one it rejects. When
  a compose option matters, read back what the engine actually configured from
  `HostConfig` in `status/container` rather than trusting the file.
- UDP ports need an explicit `/udp` suffix (`- '1161:1161/udp'`). Without it
  Docker publishes TCP only and UDP requests are dropped at the router with no
  error in the container log. The symptom is a service that works when given its
  own IP on a Local IP Network and fails through the router IP with a `ports:`
  mapping.
- `restart: unless-stopped` or `restart: always` for production containers. Use
  `restart: "no"` for a one-shot probe with external side effects, do the work
  once, then idle so the log stays retrievable — a restart policy on a container
  that emits to an external system floods it.
- Default to one process per container. Reach for a supervisor only when the
  processes must share a loopback interface or filesystem.
- Alpine's `ash` has no `wait -n`, so the "block until any child exits" idiom
  does not work in an entrypoint. Use a POSIX polling supervisor
  (`while kill -0 "$A" 2>/dev/null && kill -0 "$B" 2>/dev/null; do sleep 5; done`)
  followed by a non-zero exit, paired with `restart: unless-stopped`.
- Never background one process and `exec` the other. The backgrounded process
  dies silently while the container still reports running, so the restart policy
  never fires. Add `trap term TERM INT` to kill both children, or `container
  stop` waits out the timeout and ends in `SIGKILL`.
- A signal handler that sets a flag does not unblock `time.sleep()`. Python
  retries the syscall to honour the original duration (PEP 475), so the handler
  runs immediately while a 10-second sleep still returns at t+10. Confirmed by
  direct repro. Sleep on `threading.Event.wait(timeout)` instead — it is stdlib,
  returns the instant the event is set, and doubles as the flag check. Pass the
  same event to `cp.wait_for_uptime()` / `wait_for_ntp()` /
  `wait_for_wan_connection()`, whose default timeout is 300 seconds.
- Timing `docker stop` at idle proves only that PID 1 forwards signals. Test it
  while the process is inside a sleep or a readiness wait; those are different
  failure modes and the first does not test the second.
- Service-name DNS between services in one compose project is UNVERIFIED. Every
  documented example sets `network_mode: bridge` per service, which puts each
  container on the default bridge and would defeat name resolution. Until tested,
  co-locate cooperating processes in one container or use an explicit custom
  network bound to a Local IP Network UUID.

## Networking

- Bridge networking only. `network_mode: host` is not supported, so a design
  needing the router's own network namespace does not work; give the container
  its own IP on a Local IP Network instead.
- The default bridge assigns `172.17.0.0/16` starting at `172.17.0.2`, and the
  router is the gateway. Services on it are reachable only through `ports:`
  mappings.
- Custom networks need NCOS 7.2.50+. A compose network binds to an NCM Local IP
  Network via `com.cradlepoint.network.bridge.uuid` under `driver_opts`. The UUID
  must match an existing Local IP Network and the declared `subnet`/`gateway`
  must match that network's configuration. Assign a static address with
  `networks.<name>.ipv4_address`; attach to several by listing them under the
  service's `networks:` key. Let the NCM Compose Builder emit the block rather
  than hand-writing UUIDs.
- Mapped ports are exposed on **both LAN and WAN with no router firewall
  filtering**. Anything publishing location, credentials, telemetry or client
  inventory belongs on a Local IP Network rather than a `ports:` mapping. Say why
  in the compose comments so a reader does not "simplify" it back.
- Port mapping publishes a service; it does not put the container in the traffic
  path. A forwarding container needs an address on a real LAN segment plus a
  router-side mechanism: a static route (`config/routing/tables[name=Main]/routes`
  with `gw` = the container address) for specific destinations, source-based
  policy routing (`config/routing/policies` with `src_ip_network` or `in_dev`) for
  all destinations, or DHCP options (`config/lan/dhcpd/options`, option 3 or 121)
  for DHCP clients only.
- A catch-all static route in the Main table pointing at a container captures the
  router's own management, DNS and upstream traffic, which then loops back into
  the container. Use source-based policy routing, which scopes the redirect to
  LAN-sourced traffic and leaves the router's default alone.
- A catch-all route or broad policy rule inside the container's own namespace
  also matches its **return** traffic, sending replies destined for the local
  network up the tunnel. Test any such container in the reverse direction; the
  symptom is one-way traffic that reads as a broken far end.
- `ip rule add from <served-subnet> lookup <table>` is correct only while the
  container's own address sits outside that subnet. Give a forwarding container
  its own Local IP Network rather than the LAN it serves. That also keeps a
  routing mistake away from your management path — the segment a forwarding
  container serves is usually the segment you manage the router from, and
  management access has been lost this way. Establish an out-of-band path before
  deploying anything that installs a default route, and when reachability drops
  ping a second target on the same subnet before concluding the router is down.
- With its upstream path down, a forwarding container falls back to its ordinary
  default route and forwards other hosts' traffic in cleartext while still
  reporting healthy. Default-deny forwarding, allow only the intended egress plus
  the conntrack return direction, and verify both states — down (no leak) and up
  (still works).
- Confirmed on a router (kernel `5.4.213-coconut+`, aarch64; model and firmware
  not captured, so treat as one router and one firmware and re-probe on the
  target): `cap_add: NET_ADMIN` is honoured, a `devices:` mapping of
  `/dev/net/tun` is honoured, TUN creation works, netfilter `nat`/`filter` rules
  install, and `net.ipv4.ip_forward` is `1`.
- The **nf_tables backend has no usable `nat` table on these routers; the legacy
  `ip_tables` backend works.** Debian 12 and current Alpine both symlink
  `iptables` to the nft implementation, so a container calling bare `iptables`
  gets the backend that fails — the binary runs and the syntax is accepted, only
  rule insertion fails. Probe both and use whichever has a working `nat` table.
  The two backends cannot see each other's rules, so mixing them yields a ruleset
  that looks half-applied.
- The XFRM subsystem is reachable while XFRM **interfaces** are absent
  (`ip link add type xfrm` returns `Error: Unknown device type`). These are two
  separate kernel options; a subsystem being present does not mean the specific
  feature within it is.
- `CAP_NET_RAW` is UNVERIFIED — never probed. "No root access" is a different
  claim from "this capability is absent", since a non-root process can hold
  `CAP_NET_RAW`.
- Where a daemon offers both a kernel-facility implementation and a userspace one
  needing only a TUN/TAP device, prefer the userspace path. It scopes the request
  to the container's own namespace, fails predictably rather than with `EPERM`
  deep inside third-party code, and usually encapsulates in UDP — which matters
  because containers on the default bridge sit behind the router's SNAT and a
  protocol that is neither TCP nor UDP has no translatable header.
- Many network daemons bind `127.0.0.1` by default. The symptom is a published
  port that appears completely dead while the process runs and the mapping is
  correct; confirm with `netstat -ltn` inside the container. The inverse is
  useful: bind internal seams between co-located processes to loopback so they
  cannot be reached from the network.
- A tunnel interface comes up with a reduced MTU and PMTUD works (the container
  emits the ICMP and the sender caches it), observed in a local three-container
  topology. Clamp MSS anyway, because that ICMP is filtered often enough in real
  networks. Test above and below the tunnel MTU, with and without DF; the failure
  mode is small packets working and large ones vanishing, which reads as an
  application bug.
- The router's own IP from inside a container is the default gateway of the
  bridge network, readable from `/proc/net/route`.
- Anything a browser fetches from a third party (map tiles, fonts, CDN scripts)
  travels the **client's** internet path, not the router's. Vendor every asset a
  container's web UI needs and design a degraded mode, or the page fails silently
  on a LAN without WAN access.

## Volumes and storage

- Host filesystem mounts do not work; the container namespace is protected.
  The three available sources are named volumes, `$CONFIG_STORE`, and
  `$USB_STORAGE`.
- `$CONFIG_STORE` goes in `volumes:` as a **bare entry with no mount target**.
  The platform resolves the path itself. `$CONFIG_STORE:/var/tmp` is wrong.
- Named volumes are shared between services in the same project by mounting the
  same name. Volume data is **not** refreshed when a new image is deployed —
  create a new project to get fresh volume data.
- Treat a named volume as optional: check whether the mount point exists
  (`os.path.isdir('/data')`) at startup and fall back to `/tmp`, so a missing
  volume degrades to non-persistent instead of crashing.
- `$USB_STORAGE` needs NCOS 7.23.20+ and mounts at `/var/media`. FAT32 only: 32 GB
  maximum partition, 4 GB maximum file. One USB storage device at a time, no hub.
  If several containers in a project use USB, all of them restart on
  plug/unplug. Avoid writing NCOS logs to USB while containers use it.
- USB serial ports and USB audio pass through `devices:` (`/dev/snd:/dev/snd`
  for audio, NCOS 7.25.20+).

## Health checks

- `test: ["CMD", ...]` is exec form with no shell, so `||`, `&&`, pipes and
  redirects become literal arguments to the binary. Use
  `["CMD-SHELL", "cmd || exit 1"]` when shell operators are needed.
- The test binary must exist in the image. `curl` is absent from Alpine; either
  `apk add --no-cache curl` or use what the image already ships. Python is
  usually present for `cp.py`:
  `["CMD", "python3", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=5).status == 200 else 1)"]`
- Exit 0 is healthy, non-zero unhealthy.
- **Whether `cpdockerengine` restarts an unhealthy container is UNVERIFIED.**
  Plain Docker Engine does not — it only marks the container unhealthy, and
  restart-on-unhealthy is Swarm behaviour. Treat a health check as a status
  signal, never as a recovery mechanism, and put recovery inside the container:
  a watchdog keyed on observed state, plus exiting non-zero when the main process
  dies so the restart policy (which is documented) can act.
- In a supervised multi-process container the health check must cover the process
  that is **not** PID 1, for example by having the app's health endpoint connect
  to the daemon's port. Otherwise the daemon dies while the container reports
  healthy.
- A health check is only verified once the failure it exists to detect has been
  induced and it reported that failure. Confirming it says healthy when things are
  healthy tests almost nothing.
- A socket file existing is not the daemon accepting connections. `[ -S <path> ]`
  tests only the first; test readiness by performing a harmless operation and
  retrying it, with a liveness check on the daemon's PID inside the loop.
  Installed is not loaded, loaded is not working, and listening is not accepting —
  each transition needs its own check.

## Memory and flash per model

Container memory is capped per model so router services keep their allocation.
Disabling Wi-Fi (`put/config/wlan/radio/<n>/enabled false`, plus radio 2 on
IBR1700) or IDS/IPS (`put/config/security/ips/mode "off"`, after disabling
Analytics in NCM) frees memory; both need a reboot.

| Key services | AER2200 | IBR1700 | E300 / E3000 | R1900 | R2100 | R920 | R980 |
|---|---|---|---|---|---|---|---|
| None enabled | 460 MB | 460 MB | 921 MB / 1.84 GB | 1.80 GB | 1.80 GB | 921 MB | 921 MB |
| All enabled | 135 MB | 135 MB | 371 MB / 1.29 GB | 1.45 GB | 1.45 GB | 371 MB | 371 MB |
| Wi-Fi only | 260 MB | 260 MB | 621 MB / 1.54 GB | 1.66 GB | 1.66 GB | 621 MB | 621 MB |
| IDS/IPS only | 335 MB | 335 MB | 671 MB / 1.59 GB | 1.58 GB | 1.58 GB | 671 MB | 671 MB |

Flash: 6 GB on AER2200, IBR1700, E300 and R1900; 8 GB on R2100, R920 and R980;
14 GB on E3000.

## Measured image sizes

Image size is a **flash** and pull-time cost. Resident set is what competes with
router services for the memory allowance above. The two are orders of magnitude
apart, so report both — `docker image ls` and `docker stats` — and judge each
against the constraint it actually consumes. An image size does not by itself
rule a design out on memory grounds.

| Contents | arm64 | arm/v7 | Note |
|---|---|---|---|
| Alpine 3.18 + `python3` + small network daemon + client tools + app source | 60.6 MB | 48.1 MB | 62 / 50 MiB installed; runs comfortably under `mem_limit: 64M` |
| Alpine 3.18 + `python3` + network daemon (34 packages) | 58.1 MB | 45.6 MB | 59 / 47 MiB installed |
| Alpine + `ffmpeg` + Go binary | 116 MB | 75.9 MB | ffmpeg's shared-library closure pulls ~110 packages transitively |
| Debian 12-slim + network daemon + optional plugins | 121–132 MB | 69 MB | daemon RSS with a session established: **4.4 MiB** |

Typical samples land at 45–60 MB. Adding compiled Python wheels (numpy, opencv,
tflite) moves an image into the hundreds of megabytes, which is what makes the
135 MB floor on AER2200 and IBR1700 the real constraint for heavier workloads.
Install `opencv-python-headless`, not `opencv-python`, to avoid pulling Qt and
GTK. Prefer Alpine where it can do the job — the reason is flash and pull time,
not the memory floor. A pull runs over the router's primary WAN; check
`status/wan/primary_device` first, because a name beginning `mdm-` means the pull
is cellular, slow and billable.

## Config Store from inside a container

- The socket is `/var/tmp/cs.sock`. The path is fixed.
- The socket exists only when the service lists `$CONFIG_STORE` in `volumes:`.
  Without it the path does not exist and every `cp.py` accessor returns `None`.
- Family `AF_UNIX`, type `SOCK_STREAM`. Open one connection per request: connect,
  send one command, read one response, close. There is no evidence of support for
  a persistent connection carrying multiple commands.
- A request is newline-terminated ASCII fields in a single send:
  `<verb>\n<field1>\n<field2>\n`. Bare `\n` terminates every field including the
  last. Verbs and their fields: `get`/`decrypt` take `path`, `query`, `tree`;
  `put` takes `path`, `query`, `tree`, `value`; `post` takes `path`, `query`,
  `value` (three fields, no `tree`); `delete` takes `path`, `query`; `alert` takes
  `name`, `value`.
- `value` for `put`/`post` is JSON-encoded even for a bare string —
  `json.dumps("text")` produces `"text"` with the quotes.
- **A command with a missing or extra field hangs the socket rather than
  erroring**, so every client needs a receive timeout. `cp.py` uses 2 seconds and
  applies it to the whole exchange, not per `recv()`. Never build a command with a
  variable number of fields; send every field a verb requires even when it is
  empty. This is confirmed directly for `alert`; for the other verbs it is
  inference from the shared dispatch mechanism, not a test — build against it
  regardless, but do not restate it as confirmed for them.
- The response is `status: <word>\ncontent-length: <bytes>\n\r\n\r\n<body>`.
  Header fields are separated by bare `\n` while the header block is terminated
  by `\r\n\r\n`, so splitting the whole response on `\r\n` does **not** yield the
  header fields — find the `\r\n\r\n` terminator first, then parse the bytes
  before it as `\n`-separated `key: value` lines in any order.
  `content-length` counts the body only and is accurate; loop until you have that
  many bytes rather than waiting for the socket to close.
- Most verbs return a JSON body. Some return a plain string — `alert`'s success
  body and some `put` error bodies. Try a JSON parse and fall back to the raw
  decoded text rather than crashing.
- Do not branch application logic on the exact `status` string for a write's
  success. `put`/`post`/`delete` do not reliably signal failure, so **verify every
  write by reading it back**. A client reporting success straight from the
  response can report success for a write that silently did not happen.
- Reading an absent path and reading a path that exists but holds nothing both
  return a body of `null`. The protocol has no "not found" signal, so guessed
  paths produce confident wrong conclusions rather than errors. Resolve paths from
  `docs/ncos-api/config/PATHS.md` or the DTD in `docs/ncos-api/config/dtd/`
  first, and search for the **leaf field** rather than the feature name — a
  zero-result search is evidence about the query, not about the corpus.
- A missing `$CONFIG_STORE` volume is indistinguishable from empty data; both
  give `None`. Probe a path that always has data (`status/product_info`) at
  startup and periodically, and surface the difference in any status output.
- Read a `config/...` path before writing it. `None` means the path is absent on
  this firmware, and a blind write there is undetectable afterwards.
- `cp.py` is standard library only; `py3-requests` is not needed. Its accessors
  never raise: reads log and return `None`, writes log and return normally. Code
  relying on exceptions to surface Config Store problems silently misreports
  success.
- `cp.APP_NAME` derives from `basename(os.getcwd())`, which is empty at `/`, so
  every log line in an image without a `WORKDIR` is prefixed `container:`. Set
  `CP_APP_NAME` in the Dockerfile. It is also protocol data for `alert()`.
- Throttle repeated failure logs in anything that polls: a missing volume emits
  one line per poll forever. `cp.py` logs the first failure, then at most every
  60 seconds.
- Appdata is a structural convention, not a separate protocol: a JSON list of
  `{"_id_", "name", "value"}` objects at `config/system/sdk/appdata`, addressed
  with the ordinary verbs (`config/system/sdk/appdata/<_id_>/value` updates one
  entry in place). All values are strings — parse and validate them with
  defaults. The list can hold duplicate names, so create-vs-update matters: check
  for an existing entry by name, `put` the `value` field if found, `post` a new
  entry otherwise. Use `cp.get_appdata()` / `put_appdata()` rather than
  reimplementing that branch. Name matching is case-insensitive for read, write
  and delete alike; a get/set/delete trio that folds keys differently addresses
  different records and makes read-back verification lie.
- There is no event-subscription verb. Poll `get()` on an interval and compare
  against the previous sample. Whether `cp.register()` works from a container is
  UNVERIFIED — it is said to need an event socket containers do not have, and no
  test is on record either way. Do not justify a workaround, a dependency or a
  dropped feature with that claim without testing it.
- The socket carries no transport authentication. Anything that can open it has
  full read/write/alert access to router configuration, so do not expose it
  beyond the container it is mounted into. A consumer in the same container needs
  no adapter in any language — the socket is already in that filesystem
  namespace. A consumer in another container gets its own `$CONFIG_STORE` entry
  and its own vendored `cp.py`. Only a genuinely external consumer justifies a
  network-facing adapter, which then needs authentication and an explicit
  allowlist of writable paths, off by default.
- Test a client against a mock `AF_UNIX` listener with `cp.SOCKET_PATH`
  overridden; the constant exists for that. Cover a JSON body, a plain-string
  body, a truncated response, a response that never arrives, and the socket path
  not existing. Only the last happens for free by running locally, and failure
  paths are where the defects are.

### The `alert` verb

**`alert` works from a container.** Verified on an **R980-5GD running NCOS
7.26.21**, from a container holding only the `$CONFIG_STORE` volume with no SDK
app registration of any kind. Any statement in this repo that alerts require the
on-router SDK application context is wrong and was never tested.

```
alert\n<name>\n<value>\n     ->  status: ok, body: Alert added('<name>: <value>')
alert\n\n<value>\n           ->  status: ok, body: Alert added('<value>')
alert\n<value>\n             ->  no reply; the socket blocks waiting for the
                                 missing third field until the client times out
```

- The wire form is exactly three fields. `<name>` is a **prefix on the alert
  text**, not a separate field, and may be empty.
- The response body is a **plain string, not JSON**.
- A trailing extra newline after the value is ignored.
- A missing field hangs the socket rather than erroring, so a client sending this
  verb needs a receive timeout. This is the verb most easily hand-built with a
  variable field count.
- **Whether an accepted alert reaches the NCM console is UNVERIFIED.**
  `Alert added(...)` is local acceptance at the Config Store, not delivery. How
  NCM renders `<name>` and `<value>`, and what an empty value shows there, are
  UNVERIFIED for the same reason.
- Sanitise alert text before sending. The protocol is newline-delimited, so an
  embedded newline injects an extra field and desyncs the command; `cp.alert()`
  collapses newlines and tabs to spaces and ASCII-encodes the result, since
  whether the Config Store accepts UTF-8 is untested.
- Refuse empty alert text at the client. `cp.alert()` does, returning `False`
  without sending.
- Alerts are a shared, human-facing channel. Send transitions and exceptions,
  debounced, rather than periodic samples.
- `alert` exists only on the Config Store socket; there is no REST equivalent, so
  `cp.alert()` returns `False` under `cp.use_rest()`.

## Logs and diagnostics on the router

- `container logs <name>` commonly returns nothing, and that is not a fault. The
  engine attaches the syslog driver, so stdout and stderr go to the router log
  tagged with the container name. Read them with `log show -i -s <container_name>`,
  or `log show -f 200` to follow with history.
- **stdout arrives as `INFO`, stderr as `ERR`.** A third-party daemon writing
  routine chatter to stderr fills the router's *error* log regardless of what it
  considers the severity. Send routine output to stdout.
- The log buffer is shared with the whole router and rolls over fast — a verbose
  daemon evicted a container's own startup diagnostics within about three
  minutes. A wrapped daemon's log level is a resource decision: default it low,
  expose it as an environment variable, and keep status lines sparse. When a
  container branches on platform capability, log the selected branch early and
  unconditionally; that line is what distinguishes diagnosing from guessing.
- `container exec` accepts **no shell pipelines** (`... | head` fails with
  `Invalid command: head`) and returns **no output at all** non-interactively —
  every attempt over a non-interactive SSH invocation returned only
  `<name> exec done.`, including `echo` as a positive control. A TTY is required.
  Run by hand at an interactive prompt on the router it works normally. So a
  probe whose result must be read belongs in the container's own entrypoint with
  the answer logged, not in a scripted `exec`.
- `container list`, `container logs` and `container exec` have no REST
  equivalent, so host-side tooling needs an SSH path as well as HTTP. Prefer REST
  for structured status reads: `cat /status/log` over SSH returns content tooling
  treats as binary, while the same data over REST parses as JSON
  (`[timestamp, facility, level, message]`).
- REST wraps replies as `{"success": true, "data": ...}` while the SDK returns
  data directly. Host-side tooling should unwrap and should raise on
  `success: false`, so a rejected path is not mistaken for an empty one.
- A project listed in `container list` with no containers under it has three
  causes, and the discriminators are cheap. **Pull still running**: wait.
  **Engine wedged**: a `status/container` read hangs, and `status/log` carries
  `containers`-facility lines including `daemon is not responding ...
  DeadlineExceeded`; a reboot clears it. **Engine not running**:
  `status/container` returns `null` *promptly* with zero `containers`-facility
  lines. A prompt `null` and a hang look alike to a script checking for truthy
  data and mean opposite things.
- `container list` reads project config, so it answers normally while the engine
  is dead. A command succeeding is not evidence its subsystem is healthy — check
  what the command's data source actually is.
- The engine logs `High system CPU load? Unable to get container stats` whenever
  a call times out. That is a guess it makes, not a measurement; verify against
  `status/system` (`cpu`, `load_avg`, `memory`) before acting on it.
- Container Orchestration is licensed separately. Check `status/feature` before
  debugging why something will not deploy. Config and a licensed feature entry
  both survive a firmware upgrade that leaves the engine absent, so neither is
  evidence of engine health.
- An empty `config/container/registry` array means anonymous Docker Hub pulls,
  which is all a public image needs.

## Standing UNVERIFIED list

Do not design around any of these without testing first; an untested limitation
costs unnecessary architecture, not just a wrong sentence.

- Whether an accepted `alert` reaches the NCM console.
- Whether `cp.register()` / config store event subscriptions work from a
  container.
- Whether `cpdockerengine` honours a compose `sysctls:` entry.
- Whether `cpdockerengine` restarts a container its health check marks unhealthy.
- Whether `CAP_NET_RAW` is granted, and what `cap_drop` does.
- Whether services in one compose project resolve each other by name.
- Whether the Config Store accepts UTF-8 rather than ASCII.
- Strict field counts for `get`/`put`/`post`/`delete`/`decrypt` (confirmed for
  `alert` only).
- Whether host-to-container Unix socket bind mounts work (deliberately avoided).
- Whether named-volume data survives an image redeploy in the same project (the
  documented rule is that it is not refreshed; the failure direction is untested).

When a probe closes one of these, record the result with the **device model and
firmware version** next to it. Without those the observation cannot be scoped and
has to be re-run on the next device anyway. Then sweep for every place that
framed the question as open, searching for the concept — headings, synonyms, a
distinctive phrase — not just the identifier.
