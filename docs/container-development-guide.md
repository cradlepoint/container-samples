# Container Development Guide for Cradlepoint Routers

This is a practical guide for building Docker containers that run on Ericsson (Cradlepoint) NCOS routers via the NetCloud Container Orchestrator.

## Before You Build: Check for Native NCOS Capability

NCOS ships a substantial set of network services of its own. Packaging one of those services as a container produces a worse result than the built-in feature: it consumes memory and flash the router does not have to spare, it does not appear in NCM's configuration UI, and it needs its own port mapping and lifecycle management for no gain.

Check first, before designing anything. Ways to find out what is already native:

- On the router CLI, inspect the config tree: `get config/system` lists the service subtrees NCOS manages, and `get status/system` shows what is running.
- In NCM, browse the device configuration JSON — a service with a `config/system/<name>` subtree is native and configurable.
- Ask whoever knows the platform. This is cheaper than discovering it after the container is written.

### Known native services

This list is incomplete and grows as capabilities are confirmed. Treat anything not listed as unknown rather than absent, and verify before assuming a gap exists.

| Capability | Native? | Notes |
|------------|---------|-------|
| MQTT broker (mosquitto) | Yes | Runs natively. Do not ship a broker container. A client that publishes into the native broker is still a valid pattern. |
| SNMP agent | Yes | Native agent exists. The `SNMP_agent/` sample is justified only because it fixes unstable ifIndex assignment, not because SNMP is missing. |
| Remote syslog (outbound) | Yes | Sends the router's own logs to a remote collector. |
| NMEA / TAIP GPS forwarding | Yes | Streams sentences to a remote server or local port. |
| RTSP viewing / transcoding | No | No native way to view or re-encode a camera's RTSP stream in a browser. The `rtsp_viewer/` sample fills this gap. |

### When a container is still the right answer

A container earns its place when it does one of these:

- Provides a service NCOS genuinely lacks
- Translates between a native capability and a protocol the native feature does not speak
- Fixes a specific defect or limitation in the native implementation, as `SNMP_agent/` does
- Adds behavior the native feature has no concept of, such as buffering data through a WAN outage and replaying it

"NCOS already does X" does not always kill an idea — but it does mean the container must be reframed around the delta, and the README should state plainly what the native feature cannot do.

## Architecture Constraints

### Target Architectures

| Router           | Architecture     | Docker Platform Flag   |
|------------------|------------------|------------------------|
| AER2200, IBR1700 | ARMv7 32-bit     | `linux/arm/v7`         |
| E300, E3000, R920, R980, R1900, R2100 | ARMv8 64-bit | `linux/arm64` |

Build images for the correct architecture. Use `docker buildx` for cross-compilation:

```bash
# For ARMv8 64-bit routers
docker buildx build --platform linux/arm64 -t myimage:latest .

# For ARMv7 32-bit routers
docker buildx build --platform linux/arm/v7 -t myimage:latest .
```

### Memory Limits

Containers share memory with router services. Keep images and runtime footprint small.

- **Smallest routers** (AER2200, IBR1700): 135-460 MB available
- **Mid-range** (E300, R920, R980): 371-921 MB available
- **Largest** (E3000, R1900, R2100): up to 1.84 GB available

See [memory-resources.md](memory-resources.md) for full details.

### Flash Storage

Container images are stored in flash: 6-14 GB depending on model. Keep images small.

## Dockerfile Best Practices

### Use Alpine Base Images

Alpine Linux is the preferred base for NCOS containers due to its small footprint:

```dockerfile
FROM alpine:3.18
RUN apk add --no-cache python3
```

**Package presence is not feature presence.** A package existing in Alpine does
not mean Alpine's build of it includes the optional module a design depends on,
and the image will build and start perfectly either way. Where a design needs a
specific plugin, backend or optional feature of an off-the-shelf daemon, check for
that feature — not the package name — before committing to the base image:
`apk add` it in a throwaway container and list the module directory. This costs
minutes during design and is the base-image decision, so it is expensive to
discover after a sample is written.

When the feature genuinely only exists on another base, or only via compiling from
source with it enabled, that is a real decision with a size cost. Measure both
architectures and report the numbers rather than switching bases silently or
quietly dropping the feature.

### Keep Images Minimal

- Use `--no-cache` with `apk add` to avoid caching package indexes
- Combine RUN commands to reduce layers
- Remove unnecessary build dependencies after compilation
- Do not install documentation or man pages

### Entrypoint Pattern

Use an entrypoint script for initialization:

```dockerfile
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
```

End the script with `exec` so the application replaces the shell as PID 1 and receives `SIGTERM` directly. Without `exec`, the shell holds PID 1, signals are not forwarded, and container stop falls back to `SIGKILL` after the timeout.

### Running More Than One Process

Prefer one process per container. When an apk daemon and a Python helper genuinely must share a container (for example, the helper talks to the daemon over `127.0.0.1`), note that Alpine's `ash` has no `wait -n`, so the usual "wait for whichever child exits first" idiom is unavailable. Use a POSIX polling supervisor and let the restart policy handle recovery:

```sh
#!/bin/sh
/usr/sbin/mydaemon -f -c /etc/mydaemon/mydaemon.conf &
DAEMON_PID=$!
python3 /opt/app/helper.py &
HELPER_PID=$!

term() {
    kill "$DAEMON_PID" "$HELPER_PID" 2>/dev/null
    exit 0
}
trap term TERM INT

# Exit non-zero as soon as either child dies so the restart policy fires
while kill -0 "$DAEMON_PID" 2>/dev/null && kill -0 "$HELPER_PID" 2>/dev/null; do
    sleep 5
done
echo "ERROR: a supervised process exited, restarting container"
exit 1
```

Pair this with `restart: unless-stopped`. Do not background one process and `exec` the other — the backgrounded process can die silently and the container will look healthy.

Two further points for multi-process containers:

- **The health check must cover the process that is not PID 1.** A check that only exercises the main application will pass while the supervised daemon is dead. If the application serves HTTP, have its health endpoint open a socket to the daemon's port and fail when that connection is refused, so one check covers both.
- **Start order matters for readability, not correctness.** Most daemons retry a refused connection, but starting the dependency first avoids a burst of connection errors that looks like a fault. Poll for the port to open before starting the consumer, and bail out early if the first process dies during the wait.

### Daemons That Bind Loopback By Default

Several common network daemons listen on `127.0.0.1` unless told otherwise, as a
security default. In a container that makes a published port appear completely
dead: the mapping is correct, the process is running, and nothing answers,
because the listener is not on the container's external interface.

Check the daemon's documentation for a "listen on all interfaces" flag and set it
explicitly. Confirm with `netstat -ltn` inside the container that the socket is
bound to `0.0.0.0` and not `127.0.0.1` before concluding a port mapping is at
fault.

The inverse also applies deliberately: internal seams between processes in the
same container *should* bind `127.0.0.1` so they are never reachable from the
network, even by accident.

### Vendoring a Prebuilt Binary (Multi-Arch)

Some containers install their main process by downloading a prebuilt release
binary (`curl`-ing a GitHub release, for example) rather than an `apk`/`pip`
package. `apk` and `pip` already resolve the correct architecture for you, so
this problem is specific to hand-rolled downloads, and it is easy to miss
because the image still builds and runs — for whichever single architecture
was hardcoded.

Select the asset from `TARGETARCH`/`TARGETVARIANT`, which `docker buildx` sets
automatically from `--platform` with no other input needed:

```dockerfile
ARG TARGETARCH
ARG TARGETVARIANT

RUN case "${TARGETARCH}/${TARGETVARIANT}" in \
      arm64/*) ASSET=myapp_linux_arm64 ;; \
      arm/v7)  ASSET=myapp_linux_armv7 ;; \
      *) echo "unsupported platform: ${TARGETARCH}/${TARGETVARIANT}" >&2; exit 1 ;; \
    esac \
    && curl -fL -o /usr/local/bin/myapp "https://example.com/releases/${ASSET}" \
    && chmod +x /usr/local/bin/myapp
```

The explicit `exit 1` default case matters as much as the two real branches: a
silent fallback to one architecture reproduces the exact bug this is meant to
prevent, just with an extra step. Confirm the upstream project's own naming
convention for each asset (its release CI config, not the filename you happen
to see first) before writing the `case` — `arm`, `armv6` and `armv7` binaries
from the same Go toolchain are frequently all published as separate assets,
and guessing wrong from the filename pattern produces a binary that starts up
fine but was built for the wrong `GOARM` level.

This is exactly what Phase 2b's "build both architectures" step is meant to
catch — a single-architecture hardcoded asset builds and runs correctly for
that one architecture, so nothing looks wrong until someone tries the
smallest routers.

### Not Every Container Needs cp.py

Most samples in this repo talk to the Config Store, which can make it look
like a required convention. It is not. A container that has no reason to read
or write router state — because it only serves data it already has, or wraps
a self-contained third-party service — should skip `cp.py` and the
`$CONFIG_STORE` volume entirely rather than including them for consistency.
Decide this from what the container actually needs, per the Phase 1
clarifying question ("Whether it needs Config Store access"), not from what
the other examples happen to do.

### Paths Are Case-Sensitive in the Image, Probably Not on Your Machine

The image is Linux, where `App.py` and `app.py` are different files. macOS
(APFS/HFS+ by default) and Windows are case-insensitive, so a `COPY`, a
`PYTHONPATH` entry, an `import`, or a config path whose case does not match the
real filename **builds and runs correctly on your machine and fails on a Linux
builder or CI** — reporting a file not found for a file you can plainly see.

Two habits avoid it:

- Keep every path lowercase with underscores, matching the directory name, the
  `CP_APP_NAME`, and the compose service name (this is the same one-name rule
  that keeps a pushed image tag matching the deployed `image:`).
- **Verify paths against `git ls-files`, not the filesystem.** `os.path.exists()`
  and `ls` are not case-exact on a case-insensitive volume, so they will confirm
  a path that a fresh clone on Linux does not have. Git is case-exact and is the
  authority on what anyone else will actually check out.

```bash
# Does the repo really contain this path, exactly as written?
git ls-files | grep -x 'containers/my_sample/app.py'
```

Watch for the inverse too: with `core.ignorecase=true` (the default on macOS),
git does **not** notice a case-only rename done on disk, so the working tree and
the index can disagree indefinitely with nothing looking wrong locally. A
case-only rename has to go through `git mv` via a temporary name to be recorded.

### Python Applications

For Python-based containers:

```dockerfile
FROM alpine:3.18
RUN apk add --no-cache python3
COPY cp.py /opt/app/cp.py
COPY my_app.py /opt/app/my_app.py
ENV PYTHONPATH=/opt/app
CMD ["python3", "/opt/app/my_app.py"]
```

## Compose YAML

Containers are deployed via Docker Compose version `2.4`:

```yaml
version: '2.4'
services:
  my_service:
    network_mode: bridge
    image: 'myregistry/myimage:latest'
    ports:
      - '8080:8080'
    restart: always
```

### Key Compose Options

- **network_mode**: Usually `bridge`. Custom networks available with NCOS 7.2.50+.
- **ports**: `host_port:container_port` mapping
- **volumes**: Named volumes for data sharing between containers
- **devices**: Map host devices (USB serial, USB audio) into the container
- **restart**: Use `always` or `unless-stopped` for production containers
- **logging**: Use `json-file` driver for log access via `container logs`

## Communicating with the Router (Config Store)

### Enabling Config Store Access

In the Compose Builder under Volumes & Devices, enable the **Config Store** option. This mounts `/var/tmp/cs.sock` into the container.

In raw Compose YAML, use the `$CONFIG_STORE` variable:

```yaml
services:
  my_service:
    volumes:
      - $CONFIG_STORE
```

The platform resolves `$CONFIG_STORE` and handles the mount path automatically — do not append `:/var/tmp` or any other target path.

### Using cp.py

Copy `cp.py` from the repository root into your container and set `PYTHONPATH`. It is standard library only, so no extra Alpine packages are required:

```python
import signal
import threading

import cp

stop = threading.Event()
signal.signal(signal.SIGTERM, lambda *_: stop.set())

# Read router status. Every accessor can return None -- the router may not have
# this path, and a missing $CONFIG_STORE volume looks identical.
product_info = cp.get('status/product_info') or {}
cp.log(f"Running on {product_info.get('product_name')}")

# Read user-configured appdata
my_config = cp.get_appdata('my_setting') or 'default'

# Wait for router readiness at startup. Pass the stop event, or a SIGTERM
# arriving during the wait is not acted on until the timeout expires.
cp.wait_for_uptime(60, stop=stop)
cp.wait_for_wan_connection(timeout=120, stop=stop)
```

See [ncos-sdk-reference.md](ncos-sdk-reference.md) for the full API.

The same module drives a *remote* router over HTTP from a development machine
(`cp.use_rest()`), which is useful for testing an application's logic before it
is ever containerised. **On the router it is refused**: if the Config Store
socket exists, `cp.use_rest()` raises rather than switching, because local access
is already available and REST would only add credentials and the chance of
aiming at the wrong device. It also never engages on its own, so a container with
a missing `$CONFIG_STORE` volume fails visibly instead of quietly going
elsewhere. Do not put router credentials in an image; on the router, the socket
needs none.

### Giving More Than One Consumer Access to the Config Store

"Another application needs to read/write the Config Store too" resolves differently depending on where that application runs. Pick the narrowest scope that fits, since the last option carries real security weight the first two don't:

- **Another process in the same container** — no special handling. `cp.py` is stateless; every call opens its own connection to `/var/tmp/cs.sock`. Any thread or subprocess can `import cp` and call it concurrently with no coordination needed.
- **Another container in the same Compose project** — give that service its own `$CONFIG_STORE` entry and its own copy of `cp.py`. `$CONFIG_STORE` resolves to a bind mount of the same host socket for every service that lists it, so each container gets independent access; nothing needs to be proxied through the first container. Each service directory needs its own vendored `cp.py`, per the existing build-context constraint above — do not try to share one copy across services.
- **Another process in the same container, in a different language** — no adapter needed either. `cs.sock` is already mounted into that container's filesystem namespace, so a process in any language with Unix domain socket support can connect to it directly and speak the same small protocol `cp.py` speaks. See [cs-sock-protocol.md](cs-sock-protocol.md) for the full wire format — exact request/response framing, field counts per verb, and a pseudocode client — written specifically so a client can be built without reading `cp.py`'s source.
- **An external application that cannot embed a `cs.sock` client at all** (a genuinely separate host, or a device that isn't a container on this router) — this is the only case that needs a network-facing adapter, and it is the only case with real exposure: the adapter is handing out get/put/post/delete access to router configuration to whoever can reach it. If this is genuinely required:
  - Authenticate the adapter. Do not expose `cp.put()`/`cp.post()`/`cp.delete()` to an unauthenticated caller.
  - Allowlist writable paths explicitly, off by default, rather than forwarding an arbitrary path from the request. A service that can be driven to write anywhere in the config tree from the network is not safe to deploy. Reads can be broader than writes, but still scoped to what the caller actually needs.
  - Keep it off a WAN `ports:` mapping unless the caller genuinely needs to reach it from outside the LAN. Mapped ports are exposed on WAN as well as LAN with no router firewall filtering, so bind to loopback for a same-container caller, or put it on a custom Compose network tied to a Local IP Network for a same-LAN caller.
  - Verify writes by reading them back before reporting success, the same as any other `cp.py` write — see the Error Handling Contract in [ncos-sdk-reference.md](ncos-sdk-reference.md).

### What Does Not Work From a Container

| Capability | Status | What to do |
|------------|--------|------------|
| NCM custom alerts (`cp.alert()`) | **Works from a container.** Verified end-to-end: sent from a container, appeared in the NCM console as "Custom Alert" (R980, NCOS 7.26.21) | Use `cp.alert('text')` |
| Config store event subscriptions (`cp.register()` / `on()` / `unregister()`) | **UNVERIFIED** — said to require the event socket, but no test cited | Poll with `get()` on an interval |

### Alerts: what was actually observed

This repo previously stated in three places that custom alerts require the
on-router SDK application context and therefore cannot work from a container.
**That was wrong, and it had never been tested.** A container with only the
`$CONFIG_STORE` volume, no SDK app registration of any kind, sent the `alert`
verb, the Config Store replied `Alert added(...)`, and the alerts appeared in the
NCM console within seconds as `Custom Alert` rows against the correct device.

The wire format is three fields, and the field count is strict:

```
alert\n<name>\n<value>\n     ->  status: ok, body: Alert added('<name>: <value>')
alert\n\n<value>\n           ->  status: ok, body: Alert added('<value>')
alert\n<value>\n             ->  no reply; the socket blocks waiting for the
                                 missing third field until the client times out
```

Behaviour worth knowing before using it:

- **`<name>` does not reach NCM.** The socket echoes it as a prefix
  (`Alert added('<name>: <value>')`), but the console displays only `<value>` —
  alerts sent with and without a name render identically. Put anything you need
  to see inside the value.
- **A malformed command still creates an alert.** The two-field form blocks the
  client *and* produces an NCM entry reading
  `Router NCOS App Generated Alert` with none of your text. So an incomplete or
  empty alert is worse than no alert: it is an unfilterable placeholder in a
  human-facing console. `cp.alert()` refuses empty values for this reason.
- **Newlines in alert text would inject protocol fields.** Alert text usually
  carries interpolated data, so sanitise it. `cp.alert()` collapses newlines and
  tabs to spaces.
- Commands are ASCII-encoded by `cp.py`, so non-ASCII characters are replaced.
  Whether the Config Store accepts UTF-8 is untested.
- A trailing extra newline after the value is ignored.
- The response body is a **plain string, not JSON**.
- Omitting a field does not error — it hangs. Any client sending this verb needs
  a receive timeout.

Alerts are synced rather than streamed, and they are a shared, rate-limited,
human-facing channel. Send transitions and exceptions, not periodic samples, and
debounce anything derived from a noisy signal.

The lesson worth carrying: the original claim named a specific plausible
mechanism ("requires the on-router SDK application context") and was recorded as
fact without a test. It then nearly justified building an outbound webhook
workaround for a limitation that did not exist. Test the limitation before
designing around it.

If the event-subscription row holds, it has a real design consequence:
**a container cannot be event-driven off router state.** Any "react when X
changes" behaviour then has to be built from polling, which means choosing a
poll interval and detecting transitions by comparing against the previous
sample yourself. Polling is the safe assumption to budget for. Note that it is
an assumption — no test of `register()` from a container is on record either.

In the repo's `cp.py` both appear as stubs that log a clear message and return
`None`, so example code copied from on-router SDK samples fails legibly instead
of raising `AttributeError`.

### Feeding Config Store Data to an Off-the-Shelf Daemon

A recurring shape for these containers is an unmodified apk daemon that needs data only available through `cs.sock`. There are two ways to connect them, and one is much more reliable.

Prefer a **loopback socket**: a small Python process reads the Config Store, formats the data in whatever wire format the daemon expects, and serves it on `127.0.0.1:<port>`. Point the daemon at that address. Most network daemons accept a TCP or UDP source natively, this needs no special privileges, and either side can restart independently.

Avoid **synthetic device files**. Feeding a daemon through a FIFO or a pty pair to imitate a serial port or character device is fragile: support varies by daemon, many reject anything that fails their device probe, and blocking-open semantics on FIFOs cause startup order deadlocks.

Two things to get right in the adapter, whichever transport is used:

- **Propagate validity, do not fabricate it.** When the underlying router state goes stale or unavailable, emit the target protocol's "no data" or "invalid" representation. Repeating the last known value indefinitely turns a detectable outage into silent bad data, which is worse than an error for anything consuming it downstream.
- **Handle `None` from every `cp.get()`.** The router may not have the requested subtree, and the adapter runs continuously, so a single missing key must not kill the feed.

## Networking

### Default Bridge Network

By default, containers get IPs from `172.17.0.0/16`. The router is the gateway.

### Custom Networks (NCOS 7.2.50+)

Create dedicated subnets for containers via NETWORKING > Local Networks in NCM. Supports static IP or DHCP assignment.

### Exposing Ports

Use port mapping in Compose to expose container services:

```yaml
ports:
  - '1161:1161/udp'    # UDP port
  - '8080:8080'        # TCP port (default)
```

### Routing LAN Traffic Through a Container

A container that forwards, proxies or tunnels on behalf of LAN hosts needs those
hosts' traffic to arrive at it. Port mapping does not help — that publishes a
service, it does not put the container in the path. The container first needs an
address on a real LAN segment (a custom network bound to a Local IP Network, not
the `172.17.x` bridge), and then one of three mechanisms:

| Mechanism | Config | Use when |
|-----------|--------|----------|
| Static route | `config/routing/tables[name=Main]/routes` with `gw` = the container's address | Specific destinations only. Simplest, no client-side change |
| Source-based policy routing | `config/routing/tables` for the table, `config/routing/policies` with `src_ip_network` or `in_dev` and the table's UUID | *All* destinations, without redirecting the router's own traffic |
| DHCP options | `config/lan/dhcpd/options` (`option: 3` for the default gateway, `121` for classless static routes) plus `options_enabled` | No router routing config; DHCP clients only |

**A catch-all static route in the Main table pointing at a container is a trap.**
It captures the router's own traffic too — management, DNS, and any traffic the
container itself originates toward an upstream, which then loops back into the
container. This is what source-based policy routing exists for: it scopes the
redirect to LAN-sourced traffic and leaves the router's own default alone.
`config/routing/policies` fields are `src_ip_network`, `dst_ip_network`, `in_dev`,
`priority` and `table`.

Two things to verify on hardware, since the DTD proves only that the fields and
their types exist: that NCOS installs a route or policy whose next-hop is a
container address (`gw` is typed `ipany`, so the schema does not constrain it),
and which direction `priority` sorts for policies — the shipped default uses
`priority: 0` pointing at the Main table, and priority semantics are not
consistent across the NCOS config tree, so confirm by behaviour rather than
inference (see [ncos-api/dtd-usage.md](ncos-api/dtd-usage.md)).

Expect a hairpin with policy routing or a static route: the client sends to the
router, which forwards back out the same LAN interface to the container. That
works, and the router may emit ICMP redirects pointing clients at the container
directly. DHCP option 3 avoids the extra hop by having clients address the
container in the first place, at the cost of covering only DHCP clients.

### Reaching the Router from a Container

The router IP is the default gateway of the container's bridge network. To discover it programmatically:

```python
def get_router_ip():
    with open('/proc/net/route', 'r') as f:
        for line in f:
            parts = line.strip().split()
            if len(parts) >= 3 and parts[1] == '00000000':
                gw_hex = parts[2]
                gw_bytes = bytes.fromhex(gw_hex)
                return '%d.%d.%d.%d' % (gw_bytes[3], gw_bytes[2], gw_bytes[1], gw_bytes[0])
    return None
```

## Volumes and Storage

### Named Volumes (Container-to-Container)

```yaml
volumes:
  shared-data:

services:
  app1:
    volumes:
      - shared-data:/data
  app2:
    volumes:
      - shared-data:/data
```

### USB Storage (NCOS 7.23.20+)

Enable USB Storage in Volumes & Devices. Mounts at `/var/media`. FAT32 only (32GB max partition, 4GB max file).

### No Host Filesystem Access

For security, containers cannot mount the host NetCloud OS filesystem. Only named volumes, Config Store, and USB storage are available.

## Device Access

### USB Serial Port

Map via Volumes & Devices > Devices section for Out-of-Band Management.

### USB Audio (NCOS 7.25.20+)

```yaml
devices:
  - /dev/snd:/dev/snd
```

## Health Checks

```yaml
services:
  web:
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/health"]
      interval: 30s
      timeout: 10s
      retries: 3
```

Exit code 0 = healthy, non-zero = unhealthy.

**Whether an unhealthy container is actually restarted is UNVERIFIED.** This page
previously stated flatly that it is restarted after `retries` failures, with no
evidence cited. Plain Docker Engine does *not* restart on health check failure —
it only marks the container unhealthy; restart-on-unhealthy is Swarm behaviour.
Whether `cpdockerengine` acts on the result is unconfirmed, so a health check is
reliable as a *status signal* and not as a recovery mechanism.

**Put recovery inside the container**, where it does not depend on engine
behaviour. For a container that maintains a session with something — a tunnel, a
broker connection, a replication stream — that means a watchdog that checks
observed state on an interval and re-establishes, plus exiting non-zero when the
main process dies so the restart policy (which *is* documented) can act. Keep the
health check as well, for visibility, but do not let it be the only thing standing
between a dead session and an outage.

### Enumerate a Daemon's Recovery Triggers, Do Not Assume It Reconnects

An off-the-shelf daemon that advertises reconnection usually implements several
distinct mechanisms, each firing on a specific trigger — one at configuration
load, one when the *peer* closes the session, one when a liveness check concludes
the peer is dead. A session that disappears by any other route falls through all
of them and stays down permanently, and the container looks perfectly healthy
throughout because its main process is alive.

Read the daemon's documentation for which trigger each recovery option responds
to, then **test by inducing a teardown outside that set** — terminate the session
administratively rather than by blocking traffic or killing the peer. If nothing
recovers, add a watchdog keyed on observed session state rather than on the
daemon's own notion of failure.

## Verifying Before Deployment

Most of a container can be validated on a development machine. Do this before
pushing an image to a registry; a round trip through NCM to discover a missing
package is slow.

```bash
# 1. Both target architectures build, and every package exists for both.
#    A package present for arm64 is not guaranteed to exist for arm/v7.
docker buildx build --platform linux/arm64  -t myimage:arm64 --load .
docker buildx build --platform linux/arm/v7 -t myimage:armv7 --load .

# 2. Actually run it. On an ARM Mac the arm64 image runs natively; elsewhere
#    qemu emulation is slower but still exercises startup and packaging.
docker run --rm --name test -p 18080:8080 myimage:arm64

# 3. Exercise the endpoints, including failure paths and malformed input.
curl -s localhost:18080/health

# 4. Confirm signal handling. A clean stop returns promptly; a container that
#    takes the full timeout is being SIGKILLed, which means PID 1 is not
#    forwarding signals.
time docker stop -t 15 test
```

A prompt stop proves PID 1 forwards the signal, but not that a Python polling
loop *inside* that process reacts to it -- those are two different failure
modes. A `signal.signal()` handler running does not make a blocked
`time.sleep()` return early (Python retries the syscall to honor the full
duration per PEP 475), so `while True: ...; time.sleep(interval)` with a
flag-setting handler only exits on the *next* loop iteration after the current
sleep completes, and a startup call to `cp.wait_for_uptime()` or another
`wait_for_*` helper delays shutdown by up to its own `timeout` (300s default)
unless it is given the `stop` event. Test this specifically whenever a container
polls in a loop or calls a `wait_for_*` function at startup: stop it while it is
inside the sleep or the wait, not just at idle, and confirm the return time still
matches what step 4 measured. Use a `threading.Event` for the stop flag and pass
it to the readiness helpers; `Event.wait()` is both the sleep and the check. See
"Signal Handling in Polling Loops" in
[ncos-sdk-reference.md](ncos-sdk-reference.md).

**The engine and the kernel both differ, and this decides what "verified
locally" can claim.** The commands above run against Docker Desktop (or plain
Docker Engine) on the development machine. The router runs `cpdockerengine` — a
different, more restricted runtime with no Docker API or socket exposed, only
the `container` CLI and `status/container` (see
[ncos-api/status/container.md](ncos-api/status/container.md)). It was
historically balena-derived; whether that ancestry still holds on current
firmware is unconfirmed and shouldn't be assumed.

Sort every local result into one of these three buckets before quoting it:

**Transfers.** Anything that is a property of the image or of the application:
does it build, do packages exist for the target architecture, does the code
behave correctly, does a generated config parse, does PID 1 forward signals.
These are about image contents and universal namespace semantics, not about the
host.

**Does not transfer — kernel configuration.** Which kernel subsystems, modules,
device types and virtual interface types exist is a property of *that kernel
build*. Docker Desktop runs a linuxkit VM kernel; the router runs Cradlepoint's.
A feature missing locally says nothing about the router, and a feature present
locally says nothing either — **both directions cause real mistakes**, a false
negative dropping a viable design and a false positive shipping one that fails
at deploy. This covers anything reached through `ip link add ... type <x>`,
kernel IPsec/XFRM, netfilter modules, tunnel drivers, and anything whose
availability depends on a `CONFIG_*` option. Never conclude "the platform cannot
do X" from a local kernel result; probe on the router with `container exec`.

**Does not transfer — engine and namespace policy.** Exact restart policy
timing, health check internals, resource limit enforcement, which capabilities
`cap_add` actually grants, whether `devices:` and `sysctls:` are honoured, and
how user namespace remapping affects a privileged operation. Docker Desktop does
not apply userns remapping by default, so a privileged operation succeeding
locally is weak evidence at best.

State plainly which bucket a claim falls into rather than presenting
local-Docker-Desktop behaviour as router-verified.

What running locally will not tell you: anything Config Store related. Without
`cs.sock` every `cp.py` call returns `None`, so the application runs in its
"router unreachable" mode. That is worth testing deliberately — it is exactly
what a deployment with the `$CONFIG_STORE` volume missing looks like, and it
should degrade visibly rather than looking like an absence of data.

Config Store behaviour *can* be tested locally by pointing the client at a mock
socket. Bind a `socket.AF_UNIX` listener to a temp path, override
`cp.SOCKET_PATH`, and reply in the wire format — an HTTP-like header block
(`status:`, `content-length:`) terminated by CRLFCRLF, followed by a body.

Two details of that format, observed on a live router, that will break a naive
parser:

- **Header fields are separated by bare LF, but the block is terminated by
  CRLFCRLF**, e.g. `status: ok\ncontent-length: 90\n\r\n\r\n<body>`. Splitting
  the header block on `\r\n` therefore does not yield individual fields. Search
  for the CRLFCRLF terminator and match each field independently, as `cp.py`
  does.
- **The body is not always JSON.** Some verbs return a plain string, so a client
  must fall back to the raw text rather than assuming `json.loads()` will
  succeed. `content-length` is accurate and counts the body only.
That covers unwrapping, appdata round-trips, malformed and truncated replies,
receive timeouts, and the missing-socket path, none of which need a router.

Use it on your own status and health code too, not just on the happy path. A
probe that reports "available" while things are available has demonstrated
almost nothing — the only interesting direction is whether it can go red, so
induce each failure and watch it be reported. Three states are worth inducing
for anything reading `cs.sock`, and only the first happens for free by running
locally:

- **Socket absent** — the `$CONFIG_STORE` volume was not attached.
- **Socket present but never answering** — accept the connection and send
  nothing. This is what a wedged container engine looks like, and it is the state
  most likely to be misreported as healthy.
- **Socket answering badly** — a truncated body, or a plain-string body where
  JSON was expected.

`cp.py` covers these three paths itself now — see
[tests/test_cp.py](../tests/test_cp.py), which is worth reading as a worked
example of the mock. That suite exists because a review on 2026-08-17 found real
defects in exactly these paths, including a hung Config Store being counted as a
successful exchange. Your own status code is not covered by it, so induce the
same three states against whatever you build on top.

Any cached "unavailable" state also needs an explicit route back to available —
otherwise a socket that is simply not ready at second zero turns one transient
startup failure into a permanent outage in a container that keeps running and
logging normally. `cp.config_store_available()` re-probes on a 30-second
cooldown for this reason.

**Never run a container's entrypoint or config-generation scripts directly on
your development machine.** They write to absolute system paths such as
`/etc/<daemon>/` by design. Run them inside the built image
(`docker run --rm --entrypoint sh <image> -c 'python3 /opt/app/gen_conf.py'`)
where the writes are contained and thrown away.

Where a container translates data into a protocol some other software consumes,
validate the output by feeding it to that consumer rather than only unit-testing
the formatter. A checksum test proves a sentence is well-formed; only the real
consumer proves it is correct.

Before building the peer emulator, get the **real peer's configuration**. An
emulator configured from your assumptions verifies that the container works
against those assumptions, which is worth much less than it appears and can pass
while the real integration cannot connect at all. Every negotiated parameter that
was guessed is a failure at connect time. Where a **working client for the same
peer** already exists — a phone profile, a vendor client's exported settings — ask
for it too: it is a specification that has already been proven against the live
peer, and it resolves the parameters the peer's own config leaves ambiguous.

Where the integration involves PKI, generate a real certificate chain for the test
rather than exercising only the shared-secret path. A throwaway container with the
relevant tooling can issue a CA and a server certificate with the right identity in
a few commands, which is the difference between shipping the certificate code path
exercised and shipping it unexecuted.

When the consumer is third-party equipment nobody has on the desk, run an
open-source implementation of the **peer** role in a second local container. That
verifies the whole local stack end to end — module loading, privileges,
negotiation, and the data path — for the cost of one more service. Be precise
about which half of the question it settles: it shows *this container can do
this*, not *the vendor's box will accept it*, because both ends are then the same
implementation. The residual interop risk belongs in the write-up as specific
settings to confirm on the peer.

For anything that forwards or routes on behalf of other hosts, use a
**three-container topology**: a client, the container under test, and the peer. A
two-container test only exercises traffic the container originates itself, which
takes a different kernel path (output rather than forward) and misses routing,
NAT and MTU behaviour entirely.

Assert on the data plane, not just the control plane. A session reported as
established proves negotiation and authentication; it says nothing about whether
payload transits. Send real traffic, read the byte and packet counters on both
ends to confirm the path taken is the intended one, and verify the identity the
peer *observes* — in any design involving address translation, reading the
observed source on the peer is what proves the translation happened rather than
being bypassed by a route you did not notice. Exercise TCP and not only ICMP:
ICMP passing shows routing and encapsulation work, but only a stateful protocol
exercises connection tracking and only a real payload exercises MTU, and both of
those bugs are invisible to `ping`.

Where the container carries other hosts' traffic through a tunnel interface, test
MTU deliberately — sizes above and below the tunnel MTU, with and without the
don't-fragment bit, and check whether the sender learns the path MTU. The failure
mode is small packets working while large ones vanish, which reads as an
application bug rather than a network one. Clamp MSS regardless: path MTU
discovery depends on ICMP that real networks filter often enough to make relying
on it optimistic.

Assert success at the endpoint that consumes the service, not at the container in
the middle. A middlebox's own counters can show traffic flowing in both directions
while the client at the edge sees nothing — both readings accurate, because the
packets really did transit and then went somewhere other than the client. Use
intermediate counters to localise a fault after the endpoint reports one, not as
evidence the path works. And when counters are the evidence, reset them between
phases or record before/after and reason about the delta: an absolute count says
nothing about which phase produced it.

### Test the Dependency-Down State, and Prefer Fail-Closed

Any container that forwards, proxies or relays other hosts' traffic through a
conditional path needs verifying with that path **down**, not only up. The usual
behaviour is a silent fallback: the specific route disappears, traffic falls back
to the container's ordinary default route, and it egresses somewhere it was never
meant to — in the clear, and with nothing looking broken.

What makes this worse than an outage is how it interacts with a later change. If
the NAT rule is scoped to the intended egress interface, leaked packets get no
replies, so the leak is at least inert. Add a broader NAT rule while
troubleshooting — a natural thing to do — and the leaked traffic starts *working*,
silently bypassing the path it was supposed to take. **A leak that works is far
more dangerous than one that fails.**

Make the failure explicit rather than relying on routing:

```sh
iptables -P FORWARD DROP
iptables -A FORWARD -o <intended egress> -j ACCEPT
iptables -A FORWARD -i <intended egress> -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
```

Whether these rules can be installed at all on the router is a separate,
unprobed question — netfilter module availability is kernel configuration and
rule insertion is capability policy, both of which sit in the "does not transfer"
buckets. Confirm with the probes under
[Linux Capabilities](#linux-capabilities-unverified) before relying on a design
whose safety property depends on them.

Verify in both states — down, confirming nothing leaks, then up again, confirming
the ruleset did not also break the working case. A fail-closed ruleset that blocks
the happy path is an easy mistake and only re-testing catches it.

This is a separate concern from a health check. A health check notices the
dependency is gone; it says nothing about what the data path does in the meantime.
Both are needed.

### Catch-All Rules Also Match the Return Path

A wildcard route or policy rule installed for one direction of traffic generally
matches the reverse direction too, and the reverse direction is the one nobody
tests. A catch-all route in the container's own namespace can capture replies
destined for the local network the container serves, sending return traffic back
out the upstream path instead of to the host that asked. The symptom is one-way
traffic, and the natural reading is that the far end is at fault.

A narrow-selector version of the same configuration will not reproduce it, because
the table then holds only specific destinations. So this failure mode appears the
moment a requirement widens from "these destinations" to "everything" — worth
re-testing the reverse direction whenever a selector, redirect, NAT rule or
firewall default is broadened.

### Verify a Modular Daemon's Loaded Modules, Not Its Files

A plugin can be present in the image as a real shared object, with a config file
saying `load = yes`, and still not load — an unmet transitive dependency (a
crypto primitive from a package you did not install, for instance) is enough. The
daemon's own `failed to load` lines are no help, because they typically list a
dozen modules that were merely absent from the build, so the log looks
noisy-but-normal while the one module your feature needs is silently missing. The
symptom surfaces much later as a runtime failure in whatever the module was for.

Start the daemon once during verification, capture the line where it enumerates
what it loaded, and confirm the modules the feature needs are named there. `ls`
on the plugin directory is a different test and will pass while the feature is
dead. **Installed is not loaded, and loaded is not working** — each step needs
its own check when a feature depends on optional modules.

When a check fails, confirm the check itself is right before concluding the code
is broken. Shell quoting in test payloads and hand-computed expected values are
both easy to get wrong, and a false negative sends you looking for a bug that
does not exist.

## Security Considerations

- Containers run in a protected namespace with user namespace remapping
- No root access to NetCloud OS
- File ownership changes to `nobody:nobody` when replacing base image files (use copy-then-move workaround)
- Config Store access must be explicitly enabled per container

### Linux Capabilities (UNVERIFIED)

Beyond "no root access to NetCloud OS," this repo has no confirmed statement of
which Linux capabilities a container actually receives — in particular whether
`CAP_NET_RAW` (raw sockets, needed for tools like `ping`'s raw-socket mode,
packet crafting with `trafgen`/`mausezahn`, or `tcpreplay`) or `CAP_NET_ADMIN`
(creating interfaces, adding addresses, routes and NAT rules inside the
container's own network namespace) is granted, dropped, or configurable via
Compose `cap_add`/`cap_drop`, and whether a `devices:` mapping such as
`/dev/net/tun` is honoured. Any design that depends on raw sockets or other
non-default capabilities should treat this as an open question and verify with a
small probe before committing to a tool that needs it, rather than assuming from
a package's documentation that it will work unprivileged inside
`cpdockerengine`. Each probe is one command, and they run through
`container exec` in any container already deployed:

```sh
python3 -c 'import socket; socket.socket(socket.AF_INET, socket.SOCK_RAW, 1)'  # CAP_NET_RAW
ip tuntap add dev probe0 mode tun && ip link del probe0                        # CAP_NET_ADMIN + /dev/net/tun
iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE                           # netfilter modules + NAT
iptables -t filter -P FORWARD DROP                                             # policy changes permitted
cat /proc/sys/net/ipv4/ip_forward                                              # see the next section
```

The netfilter probes belong here rather than being assumed: **the availability of
`iptables`/`nftables` modules is kernel configuration, and whether rules can be
installed is capability policy.** Both fall in the "does not transfer" buckets
above, so a NAT or firewall design that worked on a development machine has not
been shown to work on the router — only that the rules and the logic are right.
The probes need `iptables` in the image, so they need a purpose-built probe
container rather than an existing deployment.

Running the same probes under Docker Desktop proves only that the image and the
kernel can do it. It says nothing about `cpdockerengine`, which is the engine in
question, and nothing about user namespace remapping, which Docker Desktop does
not apply by default. A capability is confirmed by a probe on the router and not
before.

### Preflight in the Real Container Instead of a Separate Probe

When a container depends on non-default capabilities, device mappings or kernel
state, put the probes in its own entrypoint rather than building a throwaway probe
project. Check each grant at startup, log one `PREFLIGHT ok` / `PREFLIGHT FAILED`
line per check naming the compose key that would fix it, and **refuse to start
when a check fails** rather than proceeding into a half-working data path.

This costs a few lines and collapses two artifacts into one: the first deployment
of the real container answers every platform question from `container logs`, and
the checks keep earning their place afterwards, because the same output
distinguishes "the engine stopped granting this after a firmware upgrade" from an
application fault. A separate probe container is still the right tool when the
answer decides whether to write the real container at all.

Order matters when a container installs the rules that enforce its own safety
property: install them **before** starting the process whose traffic they govern,
or there is a window during startup where traffic can take the path the rules
exist to prevent. Netfilter accepts rules naming an interface that does not exist
yet, so a firewall referring to an interface the daemon will create later can be
installed up front.

### Namespaced sysctls Are Readable but Not Writable

`/proc/sys` is mounted read-only in the container, so `sysctl -w` fails with
`permission denied` even for a namespaced key, and even with `CAP_NET_ADMIN` —
while reading the same key works fine. The only lever is a Compose `sysctls:`
entry, and whether `cpdockerengine` honours that is **UNVERIFIED**.

This matters most for `net.ipv4.ip_forward`, which any container routing or
NATing other hosts' traffic depends on. Because the container cannot change it,
the value it already has decides whether such a design is possible at all — so
read it early, as a go/no-go, rather than planning to set it during
implementation:

```bash
container exec <name> cat /proc/sys/net/ipv4/ip_forward
```

**Observed on the router: `1`.** Read via `container exec` in an ordinary
deployed container — no `cap_add`, no `sysctls:` entry, nothing special about
that project — so IPv4 forwarding appears to be **enabled by default** for
containers under `cpdockerengine`, and the read-only `/proc/sys` does not block a
forwarding design. Model and firmware were not captured with the result, so treat
it as "seen on at least one production router" rather than a guarantee across the
fleet, and re-read it on the target device; it is one command.

Note what this does *not* establish. Forwarding being permitted by the kernel for
that namespace says nothing about whether `cap_add` and `devices:` are honoured,
or whether netfilter rules can be installed — those need their own probes, above.

The general shape applies to any kernel state a container cannot modify: the
question is not "how do we set this" but "what is it already, and is that
survivable".

### Prefer the Implementation With the Smallest Privilege Surface

Where a daemon offers both a kernel-facility implementation and a userspace one
that needs nothing but a TUN/TAP device, default to the userspace path on this
platform. It asks only for capabilities scoped to the container's own network
namespace, it degrades predictably (slower) rather than mysteriously (`EPERM`
deep inside third-party code), and it avoids the unverified question of what
kernel state a remapped namespace is allowed to touch. Many network daemons ship
such an implementation precisely because they get run in restricted containers
elsewhere.

A userspace data path also tends to encapsulate its traffic in UDP, which helps
independently: containers on the default bridge sit behind the router's SNAT, and
a protocol that is neither TCP nor UDP has no translatable header. Treat the
kernel-facility path as an optimisation to confirm on hardware, not as the
design.

**Before switching implementations later, list what the current design depends on
as an artifact of the present one.** Two implementations of the same capability
are rarely interchangeable at the edges. A userspace data path typically produces
a **named interface**, and firewall, NAT and MSS rules naturally get keyed on it
(`-o <iface>`). The kernel equivalent may be **policy-based with no interface at
all**, in which case every one of those rules matches nothing — and the failure is
asymmetric in the worst way: the primary function still works, while a safety
property such as fail-closed forwarding silently stops applying. Nothing looks
broken.

So "does the alternative work?" is usually the wrong question. The useful one is
"does the alternative still produce everything my rules are keyed on?" Where it
does not, either re-key the rules onto something the alternative does provide, or
choose the variant of the alternative that restores the artifact — a routable
tunnel interface rather than bare policy mode, for instance, which is typically a
*further* kernel-config dependency on top of the base capability.

Scope the performance claim honestly too. Moving a data path from userspace into
the kernel removes a per-packet round trip; it is not hardware offload. The work
still happens in software in the container's own namespace, so the gain is real
but bounded, and a reader will assume the most favourable interpretation unless
told what it is not.

### Side Effects of User Namespace Remapping

Beyond file ownership, remapping affects anything using SysV IPC. Daemons that
create shared memory segments cannot always remove them again, producing errors
like:

```
SHM: shmctl(12) for IPC_RMID failed, Operation not permitted(1)
```

This is usually benign and appears at shutdown when the daemon tries to clean up
a segment it does not own in the remapped namespace. Do not treat it as a
startup failure. Verify the daemon's actual service still works rather than
chasing the message.

## Troubleshooting

### CLI Commands

```bash
container list                          # List all containers
container logs <container_name>         # View logs
container exec <container_name> sh      # Shell into container
cat /status/container/<project>/info    # Container info
```

### Common Issues

- **Container won't start**: Check image architecture matches router (ARMv7 vs ARMv8)
- **Out of memory**: Check available memory for the router model, disable unused services
- **Can't reach router API**: Ensure Config Store volume is enabled
- **File permission issues**: Use the copy-then-move workaround for base image files
- **Image too large for flash**: Reduce image size, use Alpine, minimize layers
