# cp.py Reference

`cp.py` is a minimal client for the router's Config Store, for use inside
containers. Standard library only — no `requests`, no third-party anything.

It has two transports, and the choice between them is always explicit:

| Transport | Talks to | Needs | Used by |
|-----------|----------|-------|---------|
| `socket` (default) | `/var/tmp/cs.sock` on the router it runs on | the `$CONFIG_STORE` volume, no credentials | containers |
| `rest` | a remote router's HTTP API | host, username, password | development machines |

Every accessor works over either one, so code written against the socket runs
unchanged against a remote router. See
[Remote Router (REST transport)](#remote-router-rest-transport). There is
deliberately **no automatic fallback** between them.

The canonical copy lives at the repository root. Each sample keeps an identical
copy because a Docker build cannot reach outside its build context. Tests are at
[tests/test_cp.py](../tests/test_cp.py) and need no router:

```bash
python3 -m unittest discover -s tests
```

Building a client in a different language? Don't reverse-engineer this module
— [cs-sock-protocol.md](cs-sock-protocol.md) specifies the wire protocol
directly, independent of Python.

For the API paths themselves — what to read and write — see [ncos-api/](ncos-api/).
This document covers the module, not the router's data model.

## Setup

```dockerfile
FROM alpine:3.18
RUN apk add --no-cache python3
COPY cp.py /opt/app/cp.py
ENV PYTHONPATH=/opt/app
```

```yaml
services:
  my_service:
    volumes:
      - $CONFIG_STORE      # bare, no mount target
```

Without the `$CONFIG_STORE` volume there is no socket, and every accessor
returns `None`.

Check connectivity from a shell in the container:

```bash
python3 /opt/app/cp.py                      # prints status/product_info
python3 /opt/app/cp.py status/gps/fix       # or any path
```

The same check runs from a development machine against a remote router, which
needs no container at all:

```bash
python3 cp.py --rest status/gps/fix         # see "Remote Router" below
```

## Core Access

| Function | Returns |
|----------|---------|
| `get(path, query='', tree=0)` | The data itself, or `None` |
| `decrypt(path, query='', tree=0)` | Decrypted data, or `None` |
| `put(path, value='', query='', tree=0)` | `{'status': str, 'data': Any}`, or `None` if unreachable |
| `post(path, value='', query='')` | Same as `put()` |
| `delete(path, query='')` | Same as `put()` |
| `log(value)` | `None`. Writes to stdout, collected by `container logs` |
| `alert(value, name=None)` | `True` if the router accepted a custom alert for NCM |

**Responses are unwrapped.** `cp.get('status/system')` returns the system dict
directly. Never write `cp.get(...).get('data')`.

```python
import cp

state = cp.get('status/wan/connection_state')   # 'connected'
cp.put('config/system/gps/enabled', True)
cp.post('config/wan/rules2/', new_rule)
cp.log(f'WAN is {state}')
```

### Where log output actually goes

`log()` writes to stdout, which the container runtime collects. Where it goes from
there is not where the Docker-shaped guess would put it.

**Container output goes to the router log, via the syslog driver.** Confirmed on a
router: the engine attaches `LogConfig.Type: syslog` with `tag: {{.Name}}` and an
empty `LogPath`, so `container logs <name>` returns nothing and the lines appear in
the router log instead, read with `log show -i -s <container_name>`:

```
07:37:10 AM INFO ipsec_client <the message>
```

The carrier supplies the **timestamp, the level, and the container name**, so the
application should not add any of them. **stdout becomes `INFO` and stderr becomes
`ERR`** — worth knowing, because routine output written to stderr lands in the
router's error log at ERR severity.

Two caveats on this observation. Model and firmware were not captured, so treat it
as "seen on at least one router" rather than fleet-wide. And these lines were found
by the `log` CLI; a `status/log` read filtered on the container name returned only
engine API chatter, so whether the same lines are retrievable through
`status/log` over REST is **not** established — if you need programmatic access
rather than a human reading `log show`, verify that separately.

`alert()` remains the only channel *confirmed end to end* to the NCM console as a
structured, filterable event; the log is prose.

### Log Prefix and APP_NAME

`log()` prefixes every line with a timestamp **and** `APP_NAME`, which is resolved
once at import:

```python
APP_NAME = os.environ.get('CP_APP_NAME') or os.path.basename(os.getcwd()) or 'container'
```

An image with no `WORKDIR` runs at `/`, where `basename` is empty, so every line
falls back to the generic `container:` — unhelpful the moment two services log to
the same place. **Set it explicitly in the Dockerfile:**

```dockerfile
ENV CP_APP_NAME=my_service
```

Note that the log carrier stamps lines too. On the router the carrier is the
**syslog** driver, and the router log view prefixes each line with a timestamp, a
severity and the container name:

```
07:37:10 AM INFO my_service SELECTED data path: userspace
```

(Locally, where a `json-file` driver does apply, it records a timestamp per entry
and the log is already per container.) Either way `log()`'s own
timestamp and name are **duplicates** of metadata the carrier holds, which is
harmless in a Python application reading its own output but worth knowing when
you control the format. A shell entrypoint writing directly to stdout should
generally print the bare message rather than reimplementing this prefix — the
carrier's copy is not lost, and one timestamp per line reads better than two.
`APP_NAME` still earns its keep independently: it is the `name` field `alert()`
sends, and it distinguishes sources when several services write to one place.

This matters beyond cosmetics wherever `APP_NAME` is used as protocol data rather
than just a prefix, because the payload would otherwise depend on the working
directory. `alert()` sends it as a field, for instance.

## Error Handling Contract

Accessors do not raise. A read failure and an absent path both produce `None`,
so a long-running poller survives a transient socket problem — but it also means
`None` alone does not tell you what went wrong.

### Why `None` and not an exception

This is a deliberate contract, not an oversight, and it is worth understanding
before working around it:

- **These containers are pollers.** A transient socket timeout, or a Config Store
  that is still coming up while the container has already started, must not kill
  a process whose job is to keep sampling. With exceptions, every call site needs
  a `try` that almost always does nothing but `continue` — and the one that
  forgets takes the container down.
- **The router genuinely has no "not found".** A path that does not exist and a
  path that exists and holds nothing both return a body of `null` at the protocol
  level (see [cs-sock-protocol.md](cs-sock-protocol.md)). So an exception could
  only ever mean "the transport failed", not "no such path" — the ambiguity that
  actually bites is not one exceptions can resolve.
- **Changing it now would be a silent breaking change** across every sample and
  every copied snippet, in the worst possible way: code that currently handles
  `None` correctly would start crashing at runtime instead of failing a test.

What was missing was not exceptions but *distinguishability*, and that is what
`config_store_available()` and `config_store_status()` are for. Two rules make
the contract safe to work with:

1. Treat every `None` as "no data **or** no router", and reach for
   `config_store_status()` whenever the difference matters to a human.
2. Never report a write as successful on the strength of its return value. Read
   it back.

Caller mistakes are the exception to the rule, and they do not return `None`
quietly — they log and are visible immediately. See
[Caller errors](#caller-errors-are-not-transport-failures).

### Distinguish "no Config Store" from "no data"

```python
if not cp.config_store_available():
    cp.log(cp.config_store_status())    # includes last_error and socket_exists
```

| Function | Purpose |
|----------|---------|
| `config_store_available()` | `True` when the active backend is reachable. Probes `status/product_info` when nothing has been tried yet, and re-probes a *failed* backend at most every 30s |
| `config_store_status()` | Dict: `transport`, `available`, `target`, `socket_path`, `socket_exists`, `last_error`, `failures`, `successes` |
| `last_transport_error()` | Text of the most recent transport failure, or `None` |

Surface this in any status endpoint or UI. A missing `$CONFIG_STORE` volume
otherwise looks exactly like a router with nothing to report. `config_store_status()`
is safe to serve: with the REST transport its `target` reports the host and
username but never the password.

Repeated failures are logged once and then throttled to at most one line a
minute, so a missing volume does not emit one line per poll forever. Throttling
is by elapsed time, so the rate is the same whether you poll every second or
every five minutes.

**A failed backend is re-probed, but only after a cooldown** (30s, or
`CP_PROBE_COOLDOWN`). Within that window `config_store_available()` returns the
last known answer rather than retrying on every call. That is what makes it cheap
to call in a loop, and it means a socket that appears after startup is picked up
without a restart.

Even so, prefer *attempting the read and explaining a `None`* over *asking
whether the backend is up and then deciding whether to read*:

```python
# Better: one round trip, and the diagnosis is only built when it is needed.
fix = cp.get('status/gps/fix')
if fix is None and not cp.config_store_available():
    cp.log(f'no Config Store: {cp.config_store_status()}')
```

A gate consulted before every read adds a way to be wrong without adding
information, and inside the cooldown window it answers from cache anyway.

### Caller errors are not transport failures

A malformed request is refused locally: nothing is sent, the reason is logged,
and backend health is left alone — an unencodable path says nothing about
whether the router is reachable, and counting it as a transport failure used to
mark a perfectly healthy Config Store as unavailable.

| Refused | Why |
|---------|-----|
| A newline or carriage return in `path` or `query` | The protocol is newline-delimited, so this would inject extra protocol fields. **Rejected, not stripped** — a stripped path addresses a different node than the one you asked for |
| An empty `path` | Almost always a bug in the caller's string building |
| Non-ASCII anywhere in the command | Commands are ASCII-encoded; whether the Config Store accepts UTF-8 is untested. Note that non-ASCII *values* are fine: `put()` JSON-encodes them, which escapes them to ASCII |
| A `value` that will not JSON-encode | Encoding happens inside the module's error handling, so this logs and returns `None` rather than raising `TypeError` past the "accessors do not raise" contract |

Alert text is the deliberate exception: `alert()` collapses newlines and tabs to
spaces rather than refusing, because it is human-facing prose and altering it
beats failing to report the condition at all.

### Writes

`put()`, `post()` and `delete()` return the Config Store's response, but the
exact success string in `status` is firmware dependent. Where a write matters,
confirm it by reading the value back. The appdata helpers already do this.

Before writing to a `config/...` path, read it first: `None` means the path does
not exist on this firmware or model, and writing blindly into an unknown tree
does nothing detectable.

Confirm the field's type and meaning in the DTD before writing it, rather than
inferring either from example code. Field semantics are per-path and the same
name can mean opposite things in different sections — see
[ncos-api/dtd-usage.md](ncos-api/dtd-usage.md).

## NCM Custom Alerts

`alert()` works from a container with only the `$CONFIG_STORE` volume — no SDK
application registration. Verified end-to-end: sent from a container, appeared in
the NCM console as a `Custom Alert` row against the correct device (R980,
NCOS 7.26.21).

```python
cp.alert(f'tank level critical: {level}%')   # True if the router accepted it
```

| Behaviour | Detail |
|-----------|--------|
| Return | `True` only when the router accepted it. `False` on an empty value, a rejection, or no Config Store |
| `name` | Defaults to `APP_NAME`. Sent because the protocol requires the field, but **NCM does not display it** — put anything you need to see in `value` |
| Empty value | Refused, and nothing is sent. An empty value still creates an alert on the router, but NCM shows the placeholder `Router NCOS App Generated Alert` with no detail |
| Newlines / tabs | Collapsed to spaces. The protocol is newline-delimited, so unsanitised text would inject protocol fields |
| Non-ASCII | Replaced. Commands are ASCII-encoded; UTF-8 support is untested |
| Length | Truncated at 1024 characters with an ellipsis. The real ceiling is unknown |
| Latency | Alerts sync rather than stream; allow up to a few minutes |

Alerts are a shared, rate-limited, human-facing channel. Send transitions and
exceptions, not periodic samples, and debounce anything derived from a noisy
signal or the console fills with duplicates.

## Application Configuration (appdata)

Appdata is how settings reach a container from NCM (System > SDK Data). Values
are always strings — parse and validate them with defaults.

| Function | Returns |
|----------|---------|
| `get_appdata(name='')` | The value as a string, or the full list of entries when no name is given. `None` if unset |
| `put_appdata(name, value)` | `True` only if verified by read-back. Creates the field if absent |
| `post_appdata(name, value)` | `True` if verified by read-back. `False` if an entry of that name already exists |
| `delete_appdata(name)` | `True` if the field is gone afterwards. Removes every entry matching the name |

```python
interval = cp.get_appdata('poll_interval')
if interval is None:
    cp.put_appdata('poll_interval', '1.0')      # self-provision on first run
```

**Name matching is case-insensitive in all four**, and deliberately identical
across them: a case-insensitive read against a case-sensitive write meant
`put_appdata('Poll_Interval', …)` created a *second* entry alongside an existing
`poll_interval`, then reported `False` because its own read-back inspected the
older one. Whenever a read-back is used as write verification, the read and the
write have to agree on which record they address, or the verification itself
lies.

`post_appdata()` refuses to create a duplicate rather than doing it silently: NCM
shows each entry as its own row, and reads only ever return the first. Use
`put_appdata()`, which creates or updates. If a duplicate pair already exists
from before this behaviour, `delete_appdata()` clears all of them in one call.

## Device Identity

| Function | Returns |
|----------|---------|
| `get_product_name()` | Model, e.g. `'R1900'` |
| `get_router_model()` | Alias for `get_product_name()` |
| `get_mac()` | Primary MAC |
| `get_serial_number()` | Chassis serial |
| `get_firmware_version(include_build_info=False)` | e.g. `'7.25.20'`, or `None` |
| `get_name()` | Hostname / system id |
| `get_uptime()` | Seconds since boot, as a float |

Every one of these returns `None` on an unexpected payload rather than
assembling something that resembles data. `get_firmware_version()` in particular
returns `None` when `status/fw_info` has no major/minor/patch fields; it used to
interpolate them unchecked and produce the string `'None.None.None'`, which then
flowed into logs and comparisons looking like a version.

## Readiness

Router services are still settling in the first minute after boot. All three
return `False` on timeout rather than raising, and none can overshoot its own
`timeout` — every internal wait is clamped to the time remaining.

| Function | Blocks until |
|----------|--------------|
| `wait_for_uptime(min_uptime_seconds=60, timeout=300, stop=None)` | Uptime exceeds the threshold |
| `wait_for_ntp(timeout=300, check_interval=1, stop=None)` | Clock looks NTP synchronised |
| `wait_for_wan_connection(timeout=300, check_interval=1, stop=None)` | A WAN reports connected |

**Pass `stop` if the container needs to shut down promptly.** It takes a
`threading.Event` — the same one your signal handler sets — and the wait returns
`False` the instant it is set, instead of running to its own timeout:

```python
stop = threading.Event()
signal.signal(signal.SIGTERM, lambda *_: stop.set())

cp.wait_for_uptime(60, stop=stop)      # returns immediately on SIGTERM
```

Without `stop`, a SIGTERM arriving mid-wait is not acted on until the timeout
(300s by default) elapses on its own, because a signal handler that only sets a
flag cannot shorten a `time.sleep()` already in progress — see
[Signal Handling in Polling Loops](#signal-handling-in-polling-loops). The
default timeout is longer than the usual container stop grace period, so a
startup wait without `stop` is the most likely reason a container gets
`SIGKILL`ed on `docker stop`.

`wait_for_ntp()` infers readiness from `status/system/ntp/sync_age` being
present. That its presence means "synchronised" rather than merely "NTP has been
attempted" is **UNVERIFIED** against a router that has never synchronised.

## Convenience Wrappers

Thin helpers over paths that are awkward enough to be worth wrapping. Everything
else is a plain `get()`.

| Function | Returns |
|----------|---------|
| `get_connected_wans()` | UIDs of connected WAN devices. Useful for store-and-forward |
| `get_sims()` | UIDs of modems with a SIM, e.g. `['mdm-abcd1234']`. Strings, not dicts. Excludes NOSIM |
| `get_wan_profiles()` | `config/wan/rules2` as a list, sorted ascending by priority (most preferred first) |
| `get_gpio(name=None, router_model=None)` | GPIO by logical name, mapped to this model's raw key |
| `get_lat_long()` | `(latitude, longitude)` in signed decimal degrees, or `(None, None)` |
| `dec(degree, minute=0, second=0)` | DMS to signed decimal degrees |
| `validate_password(username, password)` | `{'valid': bool}` or `{'valid': False, 'error': str}` |

Notes:

- `get_lat_long()` returns `(None, None)` when there is no lock. The router
  reports DMS with the sign on the degree component; `dec()` handles that.
- `dec()` cannot recover the sign when `degree` is integer zero, because integer
  zero has no sign and the router reports `degree` as an integer
  ([status/gps/fix.md](ncos-api/status/gps/fix.md)). A position within one degree
  *south* of the equator or *west* of the prime meridian therefore arrives
  indistinguishable from its northern or eastern mirror. The information is lost
  in the payload, before `cp.py` sees it — parse `status/gps/nmea` directly if you
  need to be right in that band.
- `get_wan_profiles()` sorts ascending, which is most-preferred first: lower
  `priority` values win for WAN failover. Update a single field with the indexed
  path, `cp.put(f'config/wan/rules2/{rule["_id_"]}/priority', 1)` — never put the
  whole list back. The direction of `priority` is not consistent across the NCOS
  config tree; see [ncos-api/config/wan-rules2.md](ncos-api/config/wan-rules2.md).
- `get_gpio()` only knows logical names for models listed in
  [ncos-api/status/gpio.md](ncos-api/status/gpio.md). Use
  `cp.get('status/gpio')` for raw keys on other models.
- `validate_password()` returns an error for masked `$0$` hashes, which the REST
  API returns and which cannot be validated. Only the on-router socket returns
  real `$3$` hashes.

## Not Implemented for Containers

These exist as stubs that log a clear message and return `None`, so example code
copied from elsewhere fails legibly instead of raising `AttributeError`.

| Function | Status | Use instead |
|----------|--------|-------------|
| `register()` / `on()` / `unregister()` | Config store event subscriptions are *said* to need the event socket, which containers are *said* not to have. **UNVERIFIED** — no test of this from a container is on record | Poll with `get()` on an interval and compare against the previous sample |

The stub's own log message says UNVERIFIED too. A stub that states a confident
reason gets read as evidence, and this repo has already been wrong once about
exactly this kind of claim: `alert()` was stubbed for the same sort of plausible,
untested reason and turned out to work fine. Do not justify a workaround, an
extra dependency or a dropped feature with the event-socket claim without
testing it first.

## Remote Router (REST transport)

For driving a **development** router from a workstation. A container talks to the
router it runs on through the Config Store socket, which involves no credentials
at all — do not bake router credentials into an image to use this instead.

```bash
set -a && . ./.env && set +a          # or export CP_ROUTER_HOST / _PASSWORD
python3 -c "import cp; cp.use_rest(); print(cp.get_lat_long())"
python3 cp.py --rest status/gps/fix   # same thing from the CLI
```

```python
cp.use_rest()                                    # from the environment
cp.use_rest(host='192.168.0.1', password=pw)     # or explicitly
cp.use_socket()                                  # back to cs.sock
cp.transport()                                   # 'socket' or 'rest'
```

| Function | Purpose |
|----------|---------|
| `use_rest(host=None, username=None, password=None, scheme=None, verify_tls=None, timeout=None, force=False)` | Switch to a remote router. Returns the resolved `RestTarget`. Raises `ValueError` if unconfigured, `RuntimeError` if the Config Store socket exists and `force` is not set |
| `use_socket()` | Switch back, discarding the credentials |
| `transport()` | `'socket'` or `'rest'` |

Anything not passed explicitly comes from the environment: `CP_ROUTER_HOST`,
`_USERNAME`, `_PASSWORD`, `_SCHEME`, `_VERIFY_TLS`, `_TIMEOUT`, falling back to
the `NCOS_DEV_*` names that `.env` and [tools/README.md](../tools/README.md)
already use — so an exported `.env` drives this module with no rewriting.
`RestTarget.describe()` reports where each value came from.

### What does not cross this transport

| Feature | Behaviour over REST |
|---------|---------------------|
| `alert()` | Refused with an explanation. The `alert` verb exists only on the socket |
| `decrypt()` | Refused with an explanation. No REST equivalent |
| `query` / `tree` arguments | Ignored, and reported once per process |
| `validate_password()` | Returns an error. REST returns masked `$0$` hashes, which cannot be validated |

Everything else — `get`/`put`/`post`/`delete`, appdata, identity, readiness, the
convenience wrappers — behaves identically, including the unwrapping. REST wraps
replies as `{"success": true, "data": …}`; this module unwraps them so call sites
match container code. A `success: false` reply is logged and surfaces as `None`,
and it counts as a *healthy* transport, because the router did answer — only the
request failed.

### On the router, always the socket

Two rules make that an enforced invariant rather than a convention, and both are
tested:

- **REST is refused when the Config Store socket exists.** `use_rest()` raises
  `RuntimeError` rather than switching. On the router there is local access
  already, so REST would be strictly worse: it needs credentials, and it can be
  aimed at the wrong device. The check runs before any credential is resolved, so
  a refused call never reads or holds a password.
- **No automatic fallback, ever.** A missing `$CONFIG_STORE` volume does not
  cause a switch to REST. A container that cannot reach its own Config Store
  fails visibly instead.

`force=True` overrides the first rule, for the one case that is not a mistake: a
container deliberately reaching a *different* router. That means accepting router
credentials inside the image, so treat it as a decision rather than a
convenience.

Worth being precise about how exposed this actually is, rather than louder than
the facts support. `.env` is a development-host file — gitignored, never copied
into an image — so `CP_ROUTER_HOST` and friends are not normally present in a
container at all. For REST to engage inside one, all three of these have to hold:

1. Credentials reach the container, via a deliberate compose `environment:` entry
   or Dockerfile `ENV`.
2. Something calls `use_rest()`. It is never called for you.
3. Either the Config Store socket is absent, **or** `force=True` is passed.

**Be clear about which case rule 1 does not close.** The socket-exists check
catches the normal on-router deployment, where the `$CONFIG_STORE` volume is
attached. It does *not* catch a container running on a router **without** that
volume, because then there is no socket to detect — and this module has no
reliable way to know it is on a router otherwise (nothing in the container
namespace is a confirmed marker, and inferring one from an artifact string is how
this repo has been wrong before). What protects that case is condition 2: nothing
calls `use_rest()` unless you wrote the call. A container that never calls it
cannot reach REST no matter what is in its environment.

So the rules exist so that no single mistake is enough, not because a stray
variable could redirect a container on its own.

### Security

- **No default address.** `use_rest()` raises and names the unset variables
  rather than guessing, so "you have not configured this" can never present
  itself as "the router is unreachable" against a router you did not intend to
  contact.
- **The password is never logged, never printed, and never put in a command
  line.** `curl -u` and `sshpass -p` expose credentials to every local user via
  `ps`; this uses `urllib` in-process. `RestTarget.__repr__` is redacted so a
  traceback cannot leak it, and `config_store_status()` reports only whether a
  password is set.
- **TLS verification is off by default**, because routers ship a self-signed
  certificate. The connection is encrypted but the router is *not*
  authenticated — fine on a trusted development LAN, not fine over the internet.
  Pass `verify_tls=True` once a certificate that validates is installed.

For the host-side workflow around this — `.env` handling, `container logs` over
SSH, deploying compose projects — use [tools/dev_router.py](../tools/dev_router.py),
which covers the CLI-only commands REST has no equivalent for.

## Signal Handling in Polling Loops

A signal handler running does **not** make a blocked `time.sleep()` call return
early. Python retries the underlying syscall to honor the full requested
duration (PEP 475) unless the handler itself raises, so `signal.signal(SIGTERM,
handler); time.sleep(300)` still sleeps the full 300 seconds even though the
handler ran the instant the signal arrived — confirmed by timing it directly.

The practical effect: a poller that does `while True: ...; time.sleep(interval)`
with only a flag-setting handler will not exit until the *next* iteration after
the current sleep finishes. For a long interval, `docker stop` will wait out the
full container stop grace period and then be `SIGKILL`ed — indistinguishable
from a broken entrypoint unless you know to check for this specifically.

Use a `threading.Event` rather than a bare flag. `Event.wait()` returns the
moment the event is set, so it is both the sleep and the check:

```python
_stop = threading.Event()

signal.signal(signal.SIGTERM, lambda *_: _stop.set())
signal.signal(signal.SIGINT, lambda *_: _stop.set())

while not _stop.is_set():
    ...
    _stop.wait(interval)        # returns early on SIGTERM
```

The readiness helpers take the same event via their `stop` argument, so a startup
wait is covered by the same mechanism — see [Readiness](#readiness). A bare flag
plus `time.sleep()` in short steps also works, but it is strictly worse: it wakes
up to check, and it still delays shutdown by up to one step.

## Tunables

All optional, all read from the environment at import.

| Variable | Default | Effect |
|----------|---------|--------|
| `CP_APP_NAME` | `basename(cwd)` | Log prefix, and the `name` field `alert()` sends. Set it in the Dockerfile |
| `CP_TIMEOUT` | `2.0` | Seconds for a whole socket request/response exchange, not per `recv()` |
| `CP_MAX_RESPONSE_BYTES` | `4194304` | Ceiling on one response. `get` with `tree=1` can return a very large subtree, and the smallest routers have 135 MB for all containers |
| `CP_PROBE_COOLDOWN` | `30.0` | How long `config_store_available()` caches a failure before re-probing |
| `CP_ROUTER_*` / `NCOS_DEV_*` | unset | REST transport target; see [Remote Router](#remote-router-rest-transport). Never causes a fallback on their own |

## Usage Pattern

```python
import signal
import threading

import cp

_stop = threading.Event()
signal.signal(signal.SIGTERM, lambda *_: _stop.set())
signal.signal(signal.SIGINT, lambda *_: _stop.set())

cp.wait_for_uptime(60, stop=_stop)     # returns immediately on SIGTERM

interval = cp.get_appdata('poll_interval')
if interval is None:
    cp.put_appdata('poll_interval', '1.0')     # self-provision on first run
    interval = '1.0'
try:
    interval = max(0.2, float(interval))
except (TypeError, ValueError):
    cp.log(f'poll_interval={interval!r} is not a number, using 1.0')
    interval = 1.0

while not _stop.is_set():
    system = cp.get('status/system')
    if system is None:
        # No data and no router look identical; only ask which when it matters.
        if not cp.config_store_available():
            cp.log(f'config store unavailable: {cp.config_store_status()}')
        else:
            cp.log('status/system returned no data')
    else:
        cpu = system.get('cpu', {})
        cp.log(f"cpu={(cpu.get('user', 0) + cpu.get('system', 0)) * 100:.1f}% "
               f"temp={system.get('temperature')}C")
    _stop.wait(interval)
```
