"""Minimal NCOS Config Store client for containers.

A container-focused replacement for the full NCOS SDK `cp.py`. It speaks the
Config Store protocol over the Unix socket at /var/tmp/cs.sock and nothing else:
no HTTP fallback, no `requests` dependency, standard library only.

    import cp

    state = cp.get('status/wan/connection_state')   # 'connected'
    cp.put('config/system/gps/enabled', True)
    cp.log(f'WAN is {state}')

Requires the `$CONFIG_STORE` volume on the service. Without it there is no
socket and every accessor returns None -- call `cp.config_store_available()` to
tell that apart from the router simply having no data at a path.

Responses are unwrapped: `cp.get('status/system')` returns the data itself, so
never write `cp.get(...).get('data')`.

`alert()` sends a custom alert to NCM and works from a container -- verified
end-to-end on an R980 (NCOS 7.26.21), where the alerts appeared in the NCM
console as "Custom Alert" entries.

Not included, because it does not work from a container:

    register() / on() /           config store event subscriptions need the
    unregister()                  event socket, which is not exposed to
                                  containers

Stubs for those remain so that copied example code fails with a clear log line
instead of an AttributeError. Poll instead of subscribing.

Also deliberately absent: the HTTP/REST transport for running against a remote
router from a workstation. Use `docs/ncos-api/explore_status.py`, curl or the
SSH CLI for that.

API documentation: docs/ncos-api/
"""

import base64
import hashlib
import hmac
import json
import os
import re
import socket
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple, Union

__all__ = [
    # Core Config Store access
    'get', 'put', 'post', 'delete', 'decrypt', 'log',
    # Transport diagnostics
    'config_store_available', 'config_store_status', 'last_transport_error',
    # Application configuration
    'get_appdata', 'put_appdata', 'post_appdata', 'delete_appdata',
    # Device identity
    'get_serial_number', 'get_mac', 'get_product_name', 'get_router_model',
    'get_firmware_version', 'get_name', 'get_uptime',
    # Readiness
    'wait_for_uptime', 'wait_for_ntp', 'wait_for_wan_connection',
    # Convenience wrappers documented in docs/ncos-api/
    'get_connected_wans', 'get_sims', 'get_wan_profiles', 'get_gpio',
    'get_lat_long', 'dec', 'validate_password',
    # Present but unsupported in containers
    'alert', 'register', 'on', 'unregister',
]

SOCKET_PATH = '/var/tmp/cs.sock'

_END_OF_HEADER = b"\r\n\r\n"
_STATUS_HEADER_RE = re.compile(rb"status: \w*")
_CONTENT_LENGTH_HEADER_RE = re.compile(rb"content-length: \w*")
_MAX_PACKET_SIZE = 8192
_RECV_TIMEOUT = 2.0

# Name used as a log prefix. Set CP_APP_NAME to override.
APP_NAME = os.environ.get('CP_APP_NAME') or os.path.basename(os.getcwd()) or 'container'

_lock = threading.Lock()
_transport = {
    'ok': None,        # None until the socket has been tried
    'error': None,     # text of the most recent failure
    'failures': 0,
    'successes': 0,
    'consecutive_failures': 0,
}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(value: Any = '') -> None:
    """Write a line to stdout, where the container runtime collects it.

    Visible with `container logs <name>` on the router.
    """
    stamp = time.strftime('%Y-%m-%dT%H:%M:%S')
    try:
        print(f'{stamp} {APP_NAME}: {value}', flush=True)
    except (BrokenPipeError, ValueError):
        # stdout closed during shutdown; losing a log line is not worth raising.
        pass


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def _record(success: bool, error: Optional[str] = None) -> int:
    """Update transport health. Returns the consecutive failure count."""
    with _lock:
        if success:
            _transport['ok'] = True
            _transport['error'] = None
            _transport['successes'] += 1
            _transport['consecutive_failures'] = 0
        else:
            _transport['ok'] = False
            _transport['error'] = error
            _transport['failures'] += 1
            _transport['consecutive_failures'] += 1
        return _transport['consecutive_failures']


def _receive(sock: socket.socket) -> Dict[str, Any]:
    """Read one Config Store response.

    Wire format is an HTTP-like header block terminated by CRLFCRLF, with a
    content-length, followed by a JSON body.
    """
    sock.settimeout(_RECV_TIMEOUT)
    data = b""
    end_of_header = -1

    while end_of_header < 0:
        try:
            buf = sock.recv(_MAX_PACKET_SIZE)
        except socket.timeout:
            return {'status': 'timeout', 'data': None}
        if not buf:
            break
        data += buf
        end_of_header = data.find(_END_OF_HEADER)

    status_match = _STATUS_HEADER_RE.search(data)
    length_match = _CONTENT_LENGTH_HEADER_RE.search(data)
    if end_of_header < 0 or status_match is None or length_match is None:
        # Truncated or unrecognised response. Report it rather than raising an
        # AttributeError from a failed regex match, which is what the original
        # SDK did and which obscured the real problem.
        return {'status': 'malformed', 'data': None}

    status = status_match.group(0)[len('status: '):].decode()
    content_length = int(length_match.group(0)[len('content-length: '):])
    remaining = content_length - (len(data) - end_of_header - len(_END_OF_HEADER))

    while remaining > 0:
        buf = sock.recv(_MAX_PACKET_SIZE)
        if not buf:
            break
        data += buf
        remaining -= len(buf)

    body = data[end_of_header + len(_END_OF_HEADER):].decode(errors='replace')
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        # The config store does not return valid JSON for some 'put' errors;
        # the body is a plain message in that case.
        payload = body.strip()
    return {'status': status, 'data': payload}


def _dispatch(cmd: str) -> Dict[str, Any]:
    """Send one command and return {'status': str, 'data': Any}.

    Returns an empty dict on transport failure, so callers can use
    `.get('data')` without a NoneType check.
    """
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(_RECV_TIMEOUT)
            sock.connect(SOCKET_PATH)
            sock.sendall(cmd.encode('ascii'))
            response = _receive(sock)
        _record(True)
        return response if isinstance(response, dict) else {}
    except Exception as exc:  # noqa: BLE001 - a poller must survive any failure
        consecutive = _record(False, f'{type(exc).__name__}: {exc}')
        # Log the first failure, then throttle. A missing $CONFIG_STORE volume
        # would otherwise emit one line per poll, forever.
        if consecutive == 1:
            log(f'config store unreachable at {SOCKET_PATH}: {exc}')
            if not os.path.exists(SOCKET_PATH):
                log('config store: socket does not exist -- is the $CONFIG_STORE volume attached?')
        elif consecutive % 60 == 0:
            log(f'config store still unreachable after {consecutive} attempts: {exc}')
        return {}


def config_store_available() -> bool:
    """True when the Config Store socket is reachable.

    Every accessor returns None both when a path holds no data and when the
    router cannot be reached at all. Use this to tell those apart and report the
    real problem.
    """
    with _lock:
        state = _transport['ok']
    if state is None:
        get('status/product_info')
        with _lock:
            state = _transport['ok']
    return bool(state)


def config_store_status() -> Dict[str, Any]:
    """Connectivity detail, suitable for a status endpoint or health check."""
    available = config_store_available()
    with _lock:
        return {
            'available': available,
            'socket_path': SOCKET_PATH,
            'socket_exists': os.path.exists(SOCKET_PATH),
            'last_error': _transport['error'],
            'failures': _transport['failures'],
            'successes': _transport['successes'],
        }


def last_transport_error() -> Optional[str]:
    """Text of the most recent Config Store transport failure, or None."""
    with _lock:
        return _transport['error']


# ---------------------------------------------------------------------------
# Core Config Store access
# ---------------------------------------------------------------------------

def get(path: str, query: str = '', tree: int = 0) -> Any:
    """Read from the router tree.

    Returns the data itself, already unwrapped -- do not call `.get('data')` on
    the result. Returns None when the path holds no data or the Config Store is
    unreachable.

        cp.get('status/wan/connection_state')   # 'connected'
        cp.get('status/system')                 # {'uptime': ..., 'cpu': {...}}
    """
    return _dispatch(f'get\n{path}\n{query}\n{tree}\n').get('data')


def decrypt(path: str, query: str = '', tree: int = 0) -> Any:
    """Read and decrypt an encrypted value, such as a certificate private key.

    Same return convention as `get()`.
    """
    return _dispatch(f'decrypt\n{path}\n{query}\n{tree}\n').get('data')


def put(path: str, value: Any = '', query: str = '', tree: int = 0) -> Optional[Dict[str, Any]]:
    """Update a value in the router tree.

    Returns the raw response `{'status': str, 'data': Any}`, or None if the
    Config Store was unreachable. The exact success string in `status` is
    firmware dependent, so where a write really matters, confirm it by reading
    the value back rather than trusting the status field.

        cp.put('config/system/gps/enabled', True)
        cp.put(f'config/wan/rules2/{rule_id}/priority', 1)
    """
    response = _dispatch(f'put\n{path}\n{query}\n{tree}\n{json.dumps(value)}\n')
    return response or None


def post(path: str, value: Any = '', query: str = '') -> Optional[Dict[str, Any]]:
    """Create a new entry in the router tree.

    Used for list-valued config such as `config/wan/rules2/` or
    `config/system/sdk/appdata`. Same return convention as `put()`.
    """
    response = _dispatch(f'post\n{path}\n{query}\n{json.dumps(value)}\n')
    return response or None


def delete(path: str, query: str = '') -> Optional[Dict[str, Any]]:
    """Delete an entry from the router tree. Same return convention as `put()`."""
    response = _dispatch(f'delete\n{path}\n{query}\n')
    return response or None


# ---------------------------------------------------------------------------
# Unsupported in containers
# ---------------------------------------------------------------------------

_UNSUPPORTED_NOTE = (
    'not available from a container: {name}(). {reason} '
    'See the module docstring in cp.py.'
)

# Alert text longer than this is truncated. The real ceiling is unknown; this is
# a conservative bound to keep a runaway message from being sent at all.
_ALERT_MAX_CHARS = 1024


def _sanitise_alert_field(value: Any) -> str:
    """Make a string safe to place in a newline-delimited protocol field.

    The Config Store protocol separates fields with newlines, so an embedded
    newline in alert text would inject extra protocol fields -- alert text
    routinely carries interpolated data, which makes that a real hazard rather
    than a theoretical one. Newlines and tabs collapse to spaces.

    Non-ASCII characters are replaced, because `_dispatch()` encodes commands as
    ASCII. Whether the Config Store accepts UTF-8 is untested, so this degrades
    the text rather than risking an encode error on the whole command.
    """
    text = '' if value is None else str(value)
    for char in ('\r', '\n', '\t'):
        text = text.replace(char, ' ')
    return text.encode('ascii', 'replace').decode('ascii').strip()


def alert(value: str = '', name: Optional[str] = None) -> bool:
    """Send a custom alert to NCM. Returns True if the router accepted it.

    Works from a container with only the `$CONFIG_STORE` volume attached -- no
    SDK application registration is needed. Verified end-to-end on an R980
    (NCOS 7.26.21): the alerts appeared in the NCM console as "Custom Alert"
    entries carrying `value` as their text.

        cp.alert(f'tank level critical: {level}%')

    `name` defaults to APP_NAME and is sent because the protocol requires the
    field, but **NCM does not display it** -- alerts sent with and without a
    name render identically in the console. Do not rely on it to distinguish
    sources; put anything you need to see inside `value`.

    Alerts are synced to NCM rather than streamed, so expect a delay of up to a
    few minutes. They are also a shared, rate-limited, human-facing channel:
    send transitions and exceptions, not periodic samples. Debounce anything
    derived from a noisy signal, or the console fills with duplicates.

    Returns False, having sent nothing, when `value` is empty. An empty value
    still creates an alert on the router, but NCM shows it as the placeholder
    "Router NCOS App Generated Alert" with no detail, which is worse than no
    alert at all.
    """
    text = _sanitise_alert_field(value)
    if not text:
        log('alert: refusing to send an empty alert -- NCM would show it as '
            '"Router NCOS App Generated Alert" with no detail')
        return False
    if len(text) > _ALERT_MAX_CHARS:
        text = text[:_ALERT_MAX_CHARS - 3] + '...'

    label = _sanitise_alert_field(APP_NAME if name is None else name)

    # Exactly three fields. The Config Store blocks waiting for a missing field
    # rather than returning an error, so this must never be built conditionally.
    response = _dispatch(f'alert\n{label}\n{text}\n')
    if not response:
        log(f'alert: not sent, Config Store unreachable: {text[:80]}')
        return False

    status = str(response.get('status', ''))
    if status == 'ok':
        return True
    log(f"alert: router did not accept the alert "
        f"(status={status!r} body={response.get('data')!r})")
    return False


def register(action: str, path: str, callback: Any, *args: Any) -> None:
    """Not available from a container. Poll with `get()` instead.

    Config store event subscriptions need the event socket, which is not
    exposed to containers.
    """
    log(_UNSUPPORTED_NOTE.format(
        name='register',
        reason='Config store events need the event socket, which containers cannot access. Poll instead.',
    ))
    return None


def on(action: str, path: str, callback: Any, *args: Any) -> None:
    """Alias for `register()`. Not available from a container."""
    return register(action, path, callback, *args)


def unregister(eid: Any = None) -> None:
    """Counterpart to `register()`. Not available from a container."""
    log(_UNSUPPORTED_NOTE.format(
        name='unregister',
        reason='Config store events are not available to containers.',
    ))
    return None


# ---------------------------------------------------------------------------
# Application configuration (appdata)
# ---------------------------------------------------------------------------

_APPDATA_PATH = 'config/system/sdk/appdata'


def get_appdata(name: str = '') -> Union[None, str, List[Dict[str, Any]]]:
    """Read a value set in NCM under System > SDK Data.

    With a name, returns that value as a string, or None if unset. With no name,
    returns the raw list of `{'_id_', 'name', 'value'}` entries.

    Appdata is how configuration reaches a container from NCM. All values are
    strings; parse and validate them with defaults.
    """
    entries = get(_APPDATA_PATH)
    if not isinstance(entries, list):
        return None
    if not name:
        return entries
    target = name.lower()
    for entry in entries:
        if isinstance(entry, dict) and str(entry.get('name', '')).lower() == target:
            return entry.get('value')
    return None


def put_appdata(name: str, value: Any = '') -> bool:
    """Set an appdata value, creating it if it does not exist.

    Returns True only if the value was verified by reading it back. Config Store
    writes do not raise on failure, so an unverified True would be a claim this
    function cannot support.
    """
    text = str(value)
    entries = get(_APPDATA_PATH)
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict) and entry.get('name') == name:
                put(f'{_APPDATA_PATH}/{entry["_id_"]}/value', text)
                return get_appdata(name) == text
    post(_APPDATA_PATH, {'name': name, 'value': text})
    return get_appdata(name) == text


def post_appdata(name: str, value: Any = '') -> bool:
    """Create a new appdata entry. Returns True if verified by read-back."""
    post(_APPDATA_PATH, {'name': name, 'value': str(value)})
    return get_appdata(name) == str(value)


def delete_appdata(name: str) -> bool:
    """Delete an appdata entry. Returns True if it is gone afterwards."""
    entries = get(_APPDATA_PATH)
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if isinstance(entry, dict) and entry.get('name') == name:
            delete(f'{_APPDATA_PATH}/{entry["_id_"]}')
    return get_appdata(name) is None


# ---------------------------------------------------------------------------
# Device identity
# ---------------------------------------------------------------------------

def get_product_name() -> Optional[str]:
    """Model name, e.g. 'R1900'."""
    info = get('status/product_info')
    return info.get('product_name') if isinstance(info, dict) else None


def get_router_model() -> Optional[str]:
    """Alias for `get_product_name()`."""
    return get_product_name()


def get_mac() -> Optional[str]:
    """Primary MAC address as reported by the router."""
    info = get('status/product_info')
    return info.get('mac0') if isinstance(info, dict) else None


def get_serial_number() -> Optional[str]:
    """Chassis serial number."""
    info = get('status/product_info')
    if not isinstance(info, dict):
        return None
    manufacturing = info.get('manufacturing')
    if isinstance(manufacturing, dict):
        return manufacturing.get('serial_num')
    return None


def get_firmware_version(include_build_info: bool = False) -> Optional[str]:
    """Firmware version, e.g. '7.25.20'."""
    info = get('status/fw_info')
    if not isinstance(info, dict):
        return None
    version = f"{info.get('major_version')}.{info.get('minor_version')}.{info.get('patch_version')}"
    if include_build_info:
        return f"{version} ({info.get('build_date', '')} {info.get('build_version', '')})".strip()
    return version


def get_name() -> Optional[str]:
    """Router hostname / system id."""
    return get('config/system/system_id')


def get_uptime() -> Optional[float]:
    """Seconds since boot."""
    value = get('status/system/uptime')
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Readiness helpers
# ---------------------------------------------------------------------------

def wait_for_uptime(min_uptime_seconds: float = 60.0, timeout: float = 300.0) -> bool:
    """Block until the router has been up for at least `min_uptime_seconds`.

    Useful at container start, since router services are still settling in the
    first minute after boot. Returns False on timeout.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        uptime = get_uptime()
        if uptime is not None and uptime >= min_uptime_seconds:
            return True
        remaining = min_uptime_seconds - (uptime or 0)
        time.sleep(max(1.0, min(remaining, 10.0)))
    log(f'wait_for_uptime: timed out after {timeout}s')
    return False


def wait_for_ntp(timeout: float = 300.0, check_interval: float = 1.0) -> bool:
    """Block until the router's clock is NTP synchronised.

    Worth calling before timestamping anything that leaves the device.
    Returns False on timeout.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if get('status/system/ntp/sync_age') is not None:
            return True
        time.sleep(check_interval)
    log(f'wait_for_ntp: timed out after {timeout}s')
    return False


def wait_for_wan_connection(timeout: float = 300.0, check_interval: float = 1.0) -> bool:
    """Block until at least one WAN is connected. Returns False on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if get('status/wan/connection_state') == 'connected':
            return True
        time.sleep(check_interval)
    log(f'wait_for_wan_connection: timed out after {timeout}s')
    return False


# ---------------------------------------------------------------------------
# WAN and modem wrappers
# ---------------------------------------------------------------------------

def get_connected_wans() -> List[str]:
    """UIDs of currently connected WAN devices.

    Handy for store-and-forward: buffer locally while this is empty, drain when
    a WAN returns.
    """
    devices = get('status/wan/devices')
    if not isinstance(devices, dict):
        return []
    connected = []
    for uid, device in devices.items():
        status = device.get('status') if isinstance(device, dict) else None
        if isinstance(status, dict) and status.get('connection_state') == 'connected':
            connected.append(uid)
    return connected


def get_sims() -> List[str]:
    """UIDs of modem devices that have a SIM installed.

    Returns a list of strings such as ['mdm-abcd1234'], not dicts. Each SIM slot
    appears as its own mdm device. Devices reporting a NOSIM error are excluded.
    """
    devices = get('status/wan/devices')
    if not isinstance(devices, dict):
        return []
    sims = []
    for uid, device in devices.items():
        if not uid.startswith('mdm-'):
            continue
        status = device.get('status') if isinstance(device, dict) else {}
        error_text = status.get('error_text', '') if isinstance(status, dict) else ''
        if error_text and 'NOSIM' in error_text:
            continue
        sims.append(uid)
    return sims


def get_wan_profiles() -> List[Dict[str, Any]]:
    """WAN rules from `config/wan/rules2`, sorted ascending by priority.

    Lower `priority` values are more preferred for WAN failover, so ascending
    order is most-preferred first. Decrease a value to promote a rule. Note that
    other NCOS config sections use the opposite convention -- see
    docs/ncos-api/config/wan-rules2.md.

    `config/wan/rules2` is a list of rule dicts. To change one field, use the
    indexed path with the rule's `_id_`:

        cp.put(f'config/wan/rules2/{rule["_id_"]}/priority', 1)

    Never put the whole list back -- that rewrites every rule.
    """
    rules = get('config/wan/rules2')
    if not isinstance(rules, list):
        return []
    return sorted(
        [rule for rule in rules if isinstance(rule, dict)],
        key=lambda rule: rule.get('priority', 0) or 0,
    )


# ---------------------------------------------------------------------------
# GPIO
# ---------------------------------------------------------------------------

# Logical name -> model-specific key in status/gpio.
# Source: docs/ncos-api/status/gpio.md. Models absent from this table still work
# with raw keys via cp.get('status/gpio').
_GPIO_MAP: Dict[str, Dict[str, str]] = {
    'IBR200': {
        'power_input': 'CGPIO_CONNECTOR_INPUT',
        'power_output': 'CGPIO_CONNECTOR_OUTPUT',
    },
    'IBR600': {
        'power_input': 'CONNECTOR_INPUT',
        'power_output': 'CONNECTOR_OUTPUT',
    },
    'IBR900': {
        'power_input': 'CONNECTOR_INPUT',
        'power_output': 'CONNECTOR_OUTPUT',
        'sata_1': 'SATA_GPIO_1',
        'sata_2': 'SATA_GPIO_2',
        'sata_3': 'SATA_GPIO_3',
        'sata_4': 'SATA_GPIO_4',
        'sata_ignition_sense': 'SATA_IGNITION_SENSE',
    },
    'IBR1100': {
        'power_input': 'CGPIO_CONNECTOR_INPUT',
        'power_output': 'CGPIO_CONNECTOR_OUTPUT',
        'expander_1': 'CGPIO_SERIAL_INPUT_1',
        'expander_2': 'CGPIO_SERIAL_INPUT_2',
        'expander_3': 'CGPIO_SERIAL_INPUT_3',
    },
    'R920': {
        'power_input': 'CONNECTOR_GPIO_1',
        'power_output': 'CONNECTOR_GPIO_2',
    },
    'R980': {
        'power_input': 'CONNECTOR_GPIO_1',
        'power_output': 'CONNECTOR_GPIO_2',
    },
    'R1900': {
        'power_input': 'CONNECTOR_GPIO_2',
        'power_output': 'CONNECTOR_GPIO_1',
        'expander_1': 'EXPANDER_GPIO_1',
        'expander_2': 'EXPANDER_GPIO_2',
        'expander_3': 'EXPANDER_GPIO_3',
        'accessory_1': 'ACCESSORY_GPIO_1',
    },
}


def get_gpio(name: Optional[str] = None, router_model: Optional[str] = None) -> Any:
    """Read GPIO by logical name, mapping to this model's raw key.

    With a name, returns that pin's value (0 or 1) or None if unmapped. With no
    name, returns a dict of every mapped logical name for this model.

        cp.get_gpio('power_input')
        cp.get_gpio()

    Pin names differ per model. `cp.get('status/gpio')` returns the raw payload,
    and `cp.put('control/gpio/<RAW_KEY>', 1)` writes an output.
    """
    model = router_model or get_product_name()
    pins = get('status/gpio')
    if not isinstance(pins, dict):
        return None

    mapping = _GPIO_MAP.get((model or '').upper())
    if mapping is None:
        log(f'get_gpio: no logical pin map for model {model!r}; use cp.get(\'status/gpio\') for raw keys')
        return None if name else {}

    if name is None:
        return {logical: pins.get(raw) for logical, raw in mapping.items()}

    raw_key = mapping.get(name)
    if raw_key is None:
        log(f'get_gpio: {name!r} is not mapped for {model}; available: {sorted(mapping)}')
        return None
    return pins.get(raw_key)


# ---------------------------------------------------------------------------
# GPS
# ---------------------------------------------------------------------------

def dec(degree: float, minute: float = 0.0, second: float = 0.0) -> Optional[float]:
    """Convert degrees/minutes/seconds to signed decimal degrees.

    The router reports coordinates as DMS with the sign carried on the degree
    component, so minutes and seconds are always added in the direction the
    degree sign indicates.
    """
    try:
        magnitude = abs(float(degree)) + abs(float(minute)) / 60.0 + abs(float(second)) / 3600.0
        return round(-magnitude if str(degree).strip().startswith('-') else magnitude, 6)
    except (TypeError, ValueError):
        return None


def get_lat_long() -> Tuple[Optional[float], Optional[float]]:
    """Current position as signed decimal degrees, or (None, None).

    Returns (None, None) when there is no GPS lock. A caller that keeps serving
    the last value it received should mark it as stale rather than presenting it
    as current.
    """
    fix = get('status/gps/fix')
    if not isinstance(fix, dict) or not fix.get('lock'):
        return None, None
    latitude = fix.get('latitude')
    longitude = fix.get('longitude')
    if not isinstance(latitude, dict) or not isinstance(longitude, dict):
        return None, None
    return (
        dec(latitude.get('degree', 0), latitude.get('minute', 0) or 0, latitude.get('second', 0) or 0),
        dec(longitude.get('degree', 0), longitude.get('minute', 0) or 0, longitude.get('second', 0) or 0),
    )


# ---------------------------------------------------------------------------
# Password validation
# ---------------------------------------------------------------------------

def validate_password(username: str, password: str) -> Dict[str, Any]:
    """Check a password against the router's stored hash.

    Returns {'valid': bool} or {'valid': False, 'error': str}.

    Hashes are `$3$<iterations>$<salt>$<key_b64>`, PBKDF2-HMAC-SHA256, where the
    salt is used as raw ASCII bytes and is not base64 decoded.

    Only the on-router socket returns real `$3$` hashes. The REST API returns
    masked `$0$` values, which cannot be validated -- this returns an error in
    that case rather than reporting a password invalid.
    """
    users = get('config/system/users/')
    if not isinstance(users, list):
        return {'valid': False, 'error': 'could not read config/system/users/'}

    entry = next(
        (user for user in users
         if isinstance(user, dict) and user.get('username') == username),
        None,
    )
    if entry is None:
        return {'valid': False, 'error': f'no such user: {username}'}

    stored = str(entry.get('password', ''))
    parts = stored.split('$')
    # ['', scheme, iterations, salt, key_b64]
    if len(parts) < 2:
        return {'valid': False, 'error': f'unrecognised hash format for {username}'}
    # Check the scheme before the field count. A masked hash may carry fewer
    # fields than a real one, and reporting it as "unrecognised format" would
    # hide the actual cause.
    if parts[1] == '0':
        return {
            'valid': False,
            'error': 'hash is masked ($0$) and cannot be validated; '
                     'only the on-router Config Store returns real $3$ hashes',
        }
    if parts[1] != '3':
        return {'valid': False, 'error': f'unsupported hash scheme ${parts[1]}$'}
    if len(parts) < 5:
        return {'valid': False, 'error': f'incomplete $3$ hash for {username}'}

    try:
        iterations = int(parts[2])
        salt = parts[3].encode('utf-8')
        expected = base64.b64decode(parts[4])
        derived = hashlib.pbkdf2_hmac(
            'sha256', password.encode('utf-8'), salt, iterations, dklen=len(expected)
        )
        return {'valid': hmac.compare_digest(derived, expected)}
    except Exception as exc:  # noqa: BLE001
        return {'valid': False, 'error': f'validation failed: {exc}'}


if __name__ == '__main__':
    # Quick connectivity check: python3 cp.py [path]
    path = sys.argv[1] if len(sys.argv) > 1 else 'status/product_info'
    status = config_store_status()
    log(f'config store: {status}')
    if status['available']:
        log(f'{path} = {json.dumps(get(path), indent=2)}')
    else:
        log('no Config Store. Attach the $CONFIG_STORE volume to this service.')
        sys.exit(1)
