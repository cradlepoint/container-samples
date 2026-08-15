# cp.py Reference

`cp.py` is a minimal client for the router's Config Store, for use inside
containers. It talks to the Unix socket at `/var/tmp/cs.sock` and uses the
standard library only — no `requests`, no HTTP fallback.

The canonical copy lives at the repository root. Each sample keeps an identical
copy because a Docker build cannot reach outside its build context.

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

### Log Prefix and APP_NAME

`log()` prefixes every line with `APP_NAME`, which is resolved once at import:

```python
APP_NAME = os.environ.get('CP_APP_NAME') or os.path.basename(os.getcwd()) or 'container'
```

An image with no `WORKDIR` runs at `/`, where `basename` is empty, so every line
falls back to the generic `container:` — unhelpful the moment two services log to
the same place. **Set it explicitly in the Dockerfile:**

```dockerfile
ENV CP_APP_NAME=my_service
```

This matters beyond cosmetics wherever `APP_NAME` is used as protocol data rather
than just a prefix, because the payload would otherwise depend on the working
directory. `alert()` sends it as a field, for instance.

## Error Handling Contract

Accessors do not raise. A read failure and an absent path both produce `None`,
so a long-running poller survives a transient socket problem — but it also means
`None` alone does not tell you what went wrong.

### Distinguish "no Config Store" from "no data"

```python
if not cp.config_store_available():
    cp.log(cp.config_store_status())    # includes last_error and socket_exists
```

| Function | Purpose |
|----------|---------|
| `config_store_available()` | `True` when the socket is reachable. Probes once, then uses cached transport state |
| `config_store_status()` | Dict: `available`, `socket_path`, `socket_exists`, `last_error`, `failures`, `successes` |
| `last_transport_error()` | Text of the most recent transport failure, or `None` |

Surface this in any status endpoint or UI. A missing `$CONFIG_STORE` volume
otherwise looks exactly like a router with nothing to report, and re-probing
periodically means a socket that appears later is picked up without a restart.

Repeated failures are logged once and then throttled, so a missing volume does
not emit one line per poll forever.

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
| `post_appdata(name, value)` | `True` if verified by read-back |
| `delete_appdata(name)` | `True` if the field is gone afterwards |

```python
interval = cp.get_appdata('poll_interval')
if interval is None:
    cp.put_appdata('poll_interval', '1.0')      # self-provision on first run
```

Name matching in `get_appdata()` is case-insensitive.

## Device Identity

| Function | Returns |
|----------|---------|
| `get_product_name()` | Model, e.g. `'R1900'` |
| `get_router_model()` | Alias for `get_product_name()` |
| `get_mac()` | Primary MAC |
| `get_serial_number()` | Chassis serial |
| `get_firmware_version(include_build_info=False)` | e.g. `'7.25.20'` |
| `get_name()` | Hostname / system id |
| `get_uptime()` | Seconds since boot, as a float |

## Readiness

Router services are still settling in the first minute after boot. All three
return `False` on timeout rather than raising.

| Function | Blocks until |
|----------|--------------|
| `wait_for_uptime(min_uptime_seconds=60, timeout=300)` | Uptime exceeds the threshold |
| `wait_for_ntp(timeout=300, check_interval=1)` | Clock is NTP synchronised |
| `wait_for_wan_connection(timeout=300, check_interval=1)` | A WAN reports connected |

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

## Not Available in Containers

These exist as stubs that log a clear message and return `None`, so example code
copied from elsewhere fails legibly instead of raising `AttributeError`.

| Function | Status | Use instead |
|----------|--------|-------------|
| `register()` / `on()` / `unregister()` | Config store event subscriptions need the event socket, which is not exposed to containers | Poll with `get()` on an interval |

There is also no HTTP/REST transport for driving a remote router from a
workstation. Use [ncos-api/explore_status.py](ncos-api/explore_status.py), curl,
or the SSH CLI.

## Usage Pattern

```python
import cp

if not cp.config_store_available():
    cp.log(f'config store unavailable: {cp.config_store_status()}')

cp.wait_for_uptime(60)

interval = cp.get_appdata('poll_interval')
if interval is None:
    cp.put_appdata('poll_interval', '1.0')
    interval = '1.0'
try:
    interval = max(0.2, float(interval))
except (TypeError, ValueError):
    cp.log(f'poll_interval={interval!r} is not a number, using 1.0')
    interval = 1.0

while True:
    system = cp.get('status/system') or {}
    cpu = system.get('cpu', {})
    cp.log(f"cpu={(cpu.get('user', 0) + cpu.get('system', 0)) * 100:.1f}% "
           f"temp={system.get('temperature')}C")
    time.sleep(interval)
```
