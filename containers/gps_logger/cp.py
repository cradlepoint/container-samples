"""Minimal NCOS Config Store client for containers.

A container-focused replacement for the full NCOS SDK `cp.py`. Standard library
only: no `requests`, no third-party anything.

    import cp

    state = cp.get('status/wan/connection_state')   # 'connected'
    cp.put('config/system/gps/enabled', True)
    cp.log(f'WAN is {state}')

Two transports, and the choice is always explicit
-------------------------------------------------

`socket` (the default) speaks the Config Store protocol over the Unix socket at
/var/tmp/cs.sock. This is how a container talks to the router it runs on, and it
needs no credentials -- only the `$CONFIG_STORE` volume on the service. Without
that volume there is no socket and every accessor returns None; call
`cp.config_store_available()` to tell that apart from the router simply having no
data at a path.

`rest` speaks the router's HTTP/REST API, for driving a *remote* router from a
development machine:

    export NCOS_DEV_HOST=192.168.0.1 NCOS_DEV_PASSWORD=...   # or CP_ROUTER_*
    python3 -c "import cp; cp.use_rest(); print(cp.get_product_name())"

Every accessor in this module works over either transport, so code written
against the socket runs unchanged against a remote router.

Two rules keep the two apart, so that "on the router, always the socket" is
enforced rather than merely conventional:

  1. **No automatic fallback.** A container whose `$CONFIG_STORE` volume is
     missing fails visibly. It never quietly switches to REST, whatever is set
     in the environment. `use_rest()` is the only way to leave socket mode.
  2. **REST is refused when the Config Store socket exists.** On the router
     there is local access, so REST would be strictly worse: it needs
     credentials and can be aimed at the wrong device. `use_rest()` raises there
     unless passed `force=True`, which exists only for the deliberate case of
     reaching a *different* router.

REST is a development-host transport. `.env` is a development-host file too --
gitignored, never copied into an image -- so its variables are not normally
present in a container at all. Do not bake router credentials into an image to
change that. See `use_rest()` for the rest of the security notes.

Responses are unwrapped: `cp.get('status/system')` returns the data itself, so
never write `cp.get(...).get('data')`. The REST API wraps replies as
`{"success": true, "data": ...}`; this module unwraps them so both transports
present the same shape.

`alert()` sends a custom alert to NCM and works from a container -- verified
end-to-end on an R980 (NCOS 7.26.21), where the alerts appeared in the NCM
console as "Custom Alert" entries.

Not implemented, because there is no evidence they work from a container:

    register() / on() /           config store event subscriptions are said to
    unregister()                  need the event socket, which containers are
                                  said not to have. UNVERIFIED -- no test of
                                  this is on record. Poll instead.

Stubs for those remain so that copied example code fails with a clear log line
instead of an AttributeError.

API documentation: docs/ncos-api/
Wire protocol:     docs/cs-sock-protocol.md
Module reference:  docs/ncos-sdk-reference.md
Tests:             tests/test_cp.py (no router required)
"""

import base64
import hashlib
import hmac
import json
import os
import socket
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

__all__ = [
    # Core Config Store access
    'get', 'put', 'post', 'delete', 'decrypt', 'log',
    # Transport selection and diagnostics
    'use_rest', 'use_socket', 'transport', 'RestTarget',
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
    # NCM alerts
    'alert',
    # Present but unimplemented in containers
    'register', 'on', 'unregister',
]

# Name used as a log prefix. Set CP_APP_NAME to override -- an image with no
# WORKDIR runs at '/', where basename is empty, so without it every line is
# prefixed with the generic 'container:'.
APP_NAME = os.environ.get('CP_APP_NAME') or os.path.basename(os.getcwd()) or 'container'


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
    except (OSError, ValueError):
        # stdout closed during shutdown; losing a log line is not worth raising.
        pass


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

def _env_number(name: str, default: float) -> float:
    """Read a positive number from the environment, falling back on nonsense."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        log(f'{name}={raw!r} is not a number, using {default}')
        return default
    if value <= 0:
        log(f'{name}={raw!r} must be positive, using {default}')
        return default
    return value


SOCKET_PATH = '/var/tmp/cs.sock'

_END_OF_HEADER = b"\r\n\r\n"
_MAX_PACKET_SIZE = 8192

# Applies to a whole request/response exchange, not to each recv(). A command
# with a missing field does not error, it hangs the socket waiting for the field
# that never arrives, so this timeout is what stops a malformed command blocking
# a poller forever.
_RECV_TIMEOUT = _env_number('CP_TIMEOUT', 2.0)

# Hard ceiling on a single response. `get` with tree=1 can return a very large
# subtree, and these routers have as little as 135 MB for all containers.
_MAX_RESPONSE_BYTES = int(_env_number('CP_MAX_RESPONSE_BYTES', 4 * 1024 * 1024))

# How long config_store_available() waits before re-probing a backend that has
# already failed. Without a re-probe, one failure at startup -- a container that
# beat the Config Store to readiness, say -- would latch for the life of the
# process.
_PROBE_COOLDOWN = _env_number('CP_PROBE_COOLDOWN', 30.0)

# Repeated failures are logged once, then at most this often. Throttling by
# elapsed time rather than by attempt count keeps the rate the same whether the
# caller polls every second or every five minutes.
_LOG_THROTTLE_SECONDS = 60.0

# Longest single time.sleep() inside a wait helper. Nothing to do with pacing:
# a signal handler that only sets a flag cannot shorten a sleep already in
# progress (PEP 475), so long sleeps delay shutdown.
_SLEEP_STEP = 1.0

# Synthetic statuses this module produces itself. The router never sends them,
# and they always mean the request failed.
_SYNTHETIC_STATUSES = ('timeout', 'malformed')

_UNSET = object()

_lock = threading.Lock()
_transport: Dict[str, Any] = {
    'mode': 'socket',       # 'socket' or 'rest'
    'rest': None,           # RestTarget when mode == 'rest'
    'ok': None,             # None until the backend has been tried
    'error': None,          # text of the most recent failure
    'failures': 0,
    'successes': 0,
    'consecutive_failures': 0,
    'last_probe': 0.0,
    'last_failure_log': 0.0,
}

# Warnings that should be said once per process, not once per call.
_warned: set = set()


def _warn_once(key: str, message: str) -> None:
    with _lock:
        if key in _warned:
            return
        _warned.add(key)
    log(message)


# ---------------------------------------------------------------------------
# Transport health
# ---------------------------------------------------------------------------

class _CommandError(Exception):
    """A caller-side problem with a request: a bad path, an unencodable value.

    Kept distinct from a transport failure on purpose. Recording one of these
    against the backend's health would blame the router for a mistake made here,
    and would mark a perfectly reachable Config Store as unavailable.
    """


def _record(success: bool, error: Optional[str] = None) -> Tuple[int, bool]:
    """Update backend health. Returns (consecutive failures, should log now)."""
    with _lock:
        if success:
            _transport['ok'] = True
            _transport['error'] = None
            _transport['successes'] += 1
            _transport['consecutive_failures'] = 0
            return 0, False

        _transport['ok'] = False
        _transport['error'] = error
        _transport['failures'] += 1
        _transport['consecutive_failures'] += 1
        consecutive = _transport['consecutive_failures']
        now = time.monotonic()
        should_log = (
            consecutive == 1
            or (now - _transport['last_failure_log']) >= _LOG_THROTTLE_SECONDS
        )
        if should_log:
            _transport['last_failure_log'] = now
        return consecutive, should_log


def _fail(detail: str) -> Dict[str, Any]:
    """Record a transport failure, log it subject to throttling, return {}."""
    consecutive, should_log = _record(False, detail)
    if should_log:
        target = _target_description()
        if consecutive == 1:
            log(f'router unreachable via {target}: {detail}')
            if _mode() == 'socket' and not os.path.exists(SOCKET_PATH):
                log('config store: socket does not exist -- is the '
                    '$CONFIG_STORE volume attached to this service?')
        else:
            log(f'router still unreachable via {target} after '
                f'{consecutive} attempts: {detail}')
    return {}


def _mode() -> str:
    with _lock:
        return _transport['mode']


def _target_description() -> str:
    """Human-readable target for a log line. Never includes the password."""
    with _lock:
        mode = _transport['mode']
        target = _transport['rest']
    if mode == 'rest' and target is not None:
        return f'{target.scheme}://{target.host} as {target.username}'
    return f'unix:{SOCKET_PATH}'


def transport() -> str:
    """Which transport is active: 'socket' or 'rest'."""
    return _mode()


# ---------------------------------------------------------------------------
# Command construction
#
# One place builds and validates every request, so both transports reject the
# same input. Two failure classes matter here and neither is the router's fault:
# a field containing a newline, which would inject extra protocol fields, and a
# value that will not encode.
# ---------------------------------------------------------------------------

def _field(label: str, value: Any) -> str:
    """Validate one protocol field.

    Newlines are rejected rather than stripped. The protocol is
    newline-delimited, so an embedded newline in a path sends more fields than
    the verb takes and desyncs the command -- but silently stripping it would
    read or write a *different path* from the one the caller asked for, which is
    worse than refusing. Callers interpolating anything into a path should
    validate it upstream.
    """
    text = '' if value is None else str(value)
    for char, name in (('\n', 'newline'), ('\r', 'carriage return')):
        if char in text:
            raise _CommandError(
                f'{label} contains a {name} ({text!r}). The Config Store '
                'protocol is newline-delimited, so this would inject extra '
                'protocol fields. Reject or encode it upstream.'
            )
    return text


def _path_field(path: Any) -> str:
    checked = _field('path', path)
    if not checked.strip():
        raise _CommandError('path is empty')
    return checked


def _value_field(value: Any) -> str:
    """JSON-encode a value for put/post.

    Inside the command builder, and therefore inside the error handling: this
    used to run in the caller's frame, so a non-serialisable value raised
    straight past the module's "accessors do not raise" contract.
    """
    try:
        return json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise _CommandError(f'value is not JSON-serialisable: {exc}') from None


def _command_fields(verb: str, path: Any, query: Any, tree: Any,
                    value: Any, name: Any) -> List[str]:
    """Fields for one verb, in order. See docs/cs-sock-protocol.md.

    Field counts are exact: the Config Store blocks waiting for a missing field
    rather than returning an error, so nothing here may be built conditionally.
    """
    if verb == 'alert':
        return [_field('alert name', name), _field('alert text', value)]
    checked_path = _path_field(path)
    checked_query = _field('query', query)
    if verb == 'delete':
        return [checked_path, checked_query]
    if verb == 'post':
        return [checked_path, checked_query, _value_field(value)]
    if verb == 'put':
        return [checked_path, checked_query, _field('tree', tree), _value_field(value)]
    # get, decrypt
    return [checked_path, checked_query, _field('tree', tree)]


def _encode_command(verb: str, fields: Sequence[str]) -> bytes:
    command = verb + '\n' + ''.join(f'{field}\n' for field in fields)
    try:
        return command.encode('ascii')
    except UnicodeEncodeError as exc:
        raise _CommandError(
            f'command contains a non-ASCII character at position {exc.start}. '
            'Commands are ASCII-encoded; whether the Config Store accepts UTF-8 '
            'is untested, so this is refused rather than risked.'
        ) from None


# ---------------------------------------------------------------------------
# Socket transport
# ---------------------------------------------------------------------------

def _recv_chunk(sock: socket.socket, deadline: float) -> Optional[bytes]:
    """One recv() bounded by the exchange deadline.

    Returns b'' on an orderly close and None on timeout, so the caller can tell
    a truncated response from a hung one.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    sock.settimeout(remaining)
    try:
        return sock.recv(_MAX_PACKET_SIZE)
    except socket.timeout:
        return None


def _parse_headers(block: bytes) -> Tuple[str, int, Optional[str]]:
    """Parse the header block. Returns (status, content_length, error).

    Header fields are separated by a bare LF even though the block itself is
    terminated by CRLFCRLF, and they are not in a guaranteed order, so each is
    matched independently. The status value is taken whole rather than as a
    single word: the vocabulary beyond 'ok' is not catalogued and may contain
    spaces.
    """
    status: Optional[str] = None
    length: Optional[int] = None
    for line in block.split(b'\n'):
        line = line.strip()
        if not line or b':' not in line:
            continue
        raw_key, _, raw_value = line.partition(b':')
        key = raw_key.strip().lower().decode('ascii', 'replace')
        text = raw_value.strip().decode('utf-8', 'replace')
        if key == 'status':
            status = text
        elif key == 'content-length':
            try:
                length = int(text)
            except ValueError:
                return '', 0, f'content-length is not a number: {text!r}'
    if status is None:
        return '', 0, 'response has no status header'
    if length is None:
        return '', 0, 'response has no content-length header'
    if length < 0:
        return '', 0, f'negative content-length: {length}'
    if length > _MAX_RESPONSE_BYTES:
        return '', 0, (f'content-length {length} exceeds the {_MAX_RESPONSE_BYTES} '
                       'byte cap (raise CP_MAX_RESPONSE_BYTES if this is expected)')
    return status, length, None


def _receive(sock: socket.socket) -> Dict[str, Any]:
    """Read one Config Store response.

    Wire format is an HTTP-like header block terminated by CRLFCRLF, with an
    accurate content-length, followed by a body that is usually but not always
    JSON. A timeout or a truncated response comes back as a synthetic status,
    never as a partial success.
    """
    deadline = time.monotonic() + _RECV_TIMEOUT
    buffer = bytearray()
    header_end = -1

    while header_end < 0:
        chunk = _recv_chunk(sock, deadline)
        if chunk is None:
            return {'status': 'timeout', 'data': None,
                    'detail': f'no complete header within {_RECV_TIMEOUT}s '
                              f'({len(buffer)} bytes read)'}
        if not chunk:
            return {'status': 'malformed', 'data': None,
                    'detail': f'connection closed after {len(buffer)} bytes, '
                              'before the CRLFCRLF header terminator'}
        buffer += chunk
        if len(buffer) > _MAX_RESPONSE_BYTES:
            return {'status': 'malformed', 'data': None,
                    'detail': f'header exceeded the {_MAX_RESPONSE_BYTES} byte cap'}
        header_end = buffer.find(_END_OF_HEADER)

    status, content_length, error = _parse_headers(bytes(buffer[:header_end]))
    if error is not None:
        return {'status': 'malformed', 'data': None, 'detail': error}

    body = bytearray(buffer[header_end + len(_END_OF_HEADER):])
    while len(body) < content_length:
        chunk = _recv_chunk(sock, deadline)
        if chunk is None:
            return {'status': 'timeout', 'data': None,
                    'detail': f'body truncated at {len(body)}/{content_length} '
                              f'bytes after {_RECV_TIMEOUT}s'}
        if not chunk:
            return {'status': 'malformed', 'data': None,
                    'detail': f'connection closed with {len(body)}/'
                              f'{content_length} body bytes received'}
        body += chunk

    text = bytes(body[:content_length]).decode('utf-8', 'replace')
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        # Expected: alert replies, and some put errors, are plain strings.
        payload = text.strip()
    return {'status': status, 'data': payload}


def _socket_dispatch(command: bytes) -> Dict[str, Any]:
    """Send one command over cs.sock. Returns {} on any failure."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(_RECV_TIMEOUT)
            sock.connect(SOCKET_PATH)
            sock.sendall(command)
            response = _receive(sock)
    except Exception as exc:  # noqa: BLE001 - a poller must survive any failure
        return _fail(f'{type(exc).__name__}: {exc}')

    if response.get('status') in _SYNTHETIC_STATUSES:
        # A hung or truncated exchange is a failed request. Recording it as a
        # success would report a wedged Config Store as healthy, which is the
        # one failure the health counters exist to catch.
        return _fail(f"{response['status']}: {response.get('detail', '')}")

    _record(True)
    return response


# ---------------------------------------------------------------------------
# REST transport (development hosts)
# ---------------------------------------------------------------------------

class RestTarget:
    """Where the REST transport points, and how it authenticates.

    `__repr__` is redacted deliberately: a default repr would print the password
    into every traceback, debugger frame and stray print that touches this
    object.
    """

    __slots__ = ('host', 'username', 'password', 'scheme', 'verify_tls',
                 'timeout', 'sources')

    def __init__(self, host: str, username: str, password: str,
                 scheme: str = 'auto', verify_tls: bool = False,
                 timeout: float = 10.0,
                 sources: Optional[Dict[str, str]] = None) -> None:
        self.host = host
        self.username = username
        self.password = password
        self.scheme = scheme
        self.verify_tls = verify_tls
        self.timeout = timeout
        self.sources = sources or {}

    def __repr__(self) -> str:
        state = 'set' if self.password else 'empty'
        return (f'RestTarget(host={self.host!r}, username={self.username!r}, '
                f'password=<redacted:{state}>, scheme={self.scheme!r}, '
                f'verify_tls={self.verify_tls!r}, timeout={self.timeout!r})')

    def describe(self) -> Dict[str, Any]:
        """Safe to print or serve. Reports whether a password is set, never it."""
        return {
            'host': self.host,
            'username': self.username,
            'password': 'set' if self.password else 'NOT SET',
            'scheme': self.scheme,
            'verify_tls': self.verify_tls,
            'timeout': self.timeout,
            'sources': dict(self.sources),
        }

    def schemes(self) -> Tuple[str, ...]:
        return ('https', 'http') if self.scheme == 'auto' else (self.scheme,)


# Primary names, then the names tools/dev_router.py and .env already use, so an
# exported .env drives this module without being rewritten.
_REST_ENV_NAMES = {
    'host': ('CP_ROUTER_HOST', 'NCOS_DEV_HOST'),
    'username': ('CP_ROUTER_USERNAME', 'NCOS_DEV_USERNAME'),
    'password': ('CP_ROUTER_PASSWORD', 'NCOS_DEV_PASSWORD'),
    'scheme': ('CP_ROUTER_SCHEME', 'NCOS_DEV_SCHEME'),
    'verify_tls': ('CP_ROUTER_VERIFY_TLS', 'NCOS_DEV_VERIFY_TLS'),
    'timeout': ('CP_ROUTER_TIMEOUT', 'NCOS_DEV_TIMEOUT'),
}


def _resolve_rest_setting(key: str) -> Tuple[Optional[str], str]:
    for name in _REST_ENV_NAMES[key]:
        if os.environ.get(name):
            return os.environ[name], name
    return None, 'default'


def use_rest(host: Optional[str] = None, username: Optional[str] = None,
             password: Optional[str] = None, scheme: Optional[str] = None,
             verify_tls: Optional[bool] = None, timeout: Optional[float] = None,
             force: bool = False) -> RestTarget:
    """Point this module at a remote router's HTTP/REST API.

    For **development hosts**. A container talks to the router it runs on
    through the Config Store socket, which needs no credentials at all -- do not
    bake router credentials into an image to use this instead.

    **Refused on the router.** If the Config Store socket exists, this raises
    RuntimeError rather than switching: local access is available, so REST would
    be strictly worse -- it needs credentials, and it can be aimed at the wrong
    device. `force=True` overrides it for the one case that is not a mistake,
    reaching a *different* router from this one, which means accepting
    credentials inside the image.

    Anything not passed explicitly is read from the environment:
    `CP_ROUTER_HOST` / `USERNAME` / `PASSWORD` / `SCHEME` / `VERIFY_TLS` /
    `TIMEOUT`, or the `NCOS_DEV_*` equivalents that `.env` and
    `tools/dev_router.py` already use:

        set -a && . ./.env && set +a
        python3 -c "import cp; cp.use_rest(); print(cp.get_lat_long())"

    Note that `.env` is a development-host file -- gitignored, never copied into
    an image -- so these variables are not normally present in a container at
    all. Reaching REST from inside one takes all three of: credentials supplied
    to the container, a call to this function, and either no Config Store socket
    or `force=True`. The refusal above closes the third; the second is what
    covers a container running without the `$CONFIG_STORE` volume, where there is
    no socket to detect. See docs/ncos-sdk-reference.md for the full statement of
    what this does and does not guard.

    Raises ValueError, naming the variables that are unset, when it has no host
    or no password. It never falls back to a default address: a tool that
    defaults its target converts "you have not configured this" into "the router
    is unreachable", against a router you may not have intended to contact.

    Credential handling:

    - The password is never logged and never placed in a command line. `curl -u`
      and `sshpass -p` expose credentials to every local user via `ps`; this goes
      through `urllib` in-process instead.
    - `RestTarget.__repr__` is redacted, and `config_store_status()` reports only
      whether a password is set.
    - TLS verification is **off by default**, because routers ship a self-signed
      certificate. The connection is encrypted but not authenticated, which is
      fine on a trusted dev LAN and not fine over the internet. Pass
      `verify_tls=True` once a certificate that validates is installed.

    Not everything crosses this transport. `decrypt()` and `alert()` have no REST
    equivalent, and `query`/`tree` are ignored; each logs plainly rather than
    returning a quietly wrong answer. `validate_password()` cannot work either,
    because REST returns masked `$0$` hashes.

    Returns the resolved target, and switches this module to it process-wide.
    """
    # Checked before any credential is resolved, so a refusal never reads or
    # holds a password it was not going to use.
    if not force and os.path.exists(SOCKET_PATH):
        raise RuntimeError(
            f'refusing to enable the REST transport: the Config Store socket '
            f'exists at {SOCKET_PATH}, so this process has local access to the '
            'router it is running on. Use the socket -- it needs no credentials '
            'and cannot be aimed at the wrong device. Pass force=True only if '
            'you really mean to reach a different router from here, which means '
            'accepting router credentials inside this container.'
        )

    sources: Dict[str, str] = {}

    def resolve(key: str, explicit: Any) -> Optional[str]:
        if explicit is not None:
            sources[key] = 'argument'
            return str(explicit)
        value, origin = _resolve_rest_setting(key)
        sources[key] = origin
        return value

    resolved_host = (resolve('host', host) or '').strip().rstrip('/')
    resolved_scheme = (resolve('scheme', scheme) or 'auto').strip().lower()
    # Tolerate a scheme pasted into the host rather than failing later on a URL
    # like https://https://192.168.0.1/api/...
    for marker in ('https://', 'http://'):
        if resolved_host.startswith(marker):
            resolved_scheme = marker[:-3]
            resolved_host = resolved_host[len(marker):]
            sources['scheme'] = 'derived from host'
    resolved_user = (resolve('username', username) or 'admin').strip()
    resolved_password = resolve('password', password) or ''

    missing = [name for name, value in (
        (_REST_ENV_NAMES['host'][0], resolved_host),
        (_REST_ENV_NAMES['password'][0], resolved_password),
    ) if not value]
    if missing:
        raise ValueError(
            'the REST transport is not configured: '
            + ', '.join(f'{name} not set' for name in missing)
            + '. Pass host=/password= explicitly, or export those variables '
              '(the NCOS_DEV_* names from .env work too). Not defaulting to an '
              'address on purpose -- see use_rest() for why.'
        )
    if resolved_scheme not in ('auto', 'https', 'http'):
        raise ValueError(f"scheme must be auto, https or http (got {resolved_scheme!r})")

    if verify_tls is None:
        raw_verify, origin = _resolve_rest_setting('verify_tls')
        sources['verify_tls'] = origin
        resolved_verify = str(raw_verify).strip().lower() in ('1', 'true', 'yes', 'on') \
            if raw_verify else False
    else:
        sources['verify_tls'] = 'argument'
        resolved_verify = bool(verify_tls)

    if timeout is None:
        raw_timeout, origin = _resolve_rest_setting('timeout')
        sources['timeout'] = origin
        try:
            resolved_timeout = float(raw_timeout) if raw_timeout else 10.0
        except ValueError:
            raise ValueError(f'timeout must be a number (got {raw_timeout!r})') from None
    else:
        sources['timeout'] = 'argument'
        resolved_timeout = float(timeout)
    if resolved_timeout <= 0:
        raise ValueError(f'timeout must be positive (got {resolved_timeout})')

    target = RestTarget(resolved_host, resolved_user, resolved_password,
                        resolved_scheme, resolved_verify, resolved_timeout, sources)

    with _lock:
        _transport.update(mode='rest', rest=target, ok=None, error=None,
                          failures=0, successes=0, consecutive_failures=0,
                          last_probe=0.0, last_failure_log=0.0)
        _warned.discard('rest_tls')

    log(f'REST transport enabled for {target.scheme}://{target.host} '
        f'as {target.username}')
    if not target.verify_tls:
        _warn_once('rest_tls',
                   'REST transport: TLS certificate verification is OFF. The '
                   'connection is encrypted but the router is not authenticated '
                   '-- acceptable on a trusted development LAN only.')
    return target


def use_socket() -> None:
    """Return to the Config Store socket, discarding any REST credentials."""
    with _lock:
        _transport.update(mode='socket', rest=None, ok=None, error=None,
                          failures=0, successes=0, consecutive_failures=0,
                          last_probe=0.0, last_failure_log=0.0)
    log(f'socket transport enabled ({SOCKET_PATH})')


_REST_METHODS = {'get': 'GET', 'put': 'PUT', 'post': 'POST', 'delete': 'DELETE'}


def _rest_once(target: 'RestTarget', scheme: str, method: str, path: str,
               value: Any) -> Dict[str, Any]:
    """One REST call. Raises on transport failure, returns the unwrapped reply.

    urllib and ssl are imported here rather than at module scope so a container
    using the socket transport -- the common case, on a router with as little as
    135 MB for all containers -- never pays for them.
    """
    import ssl
    import urllib.error
    import urllib.parse
    import urllib.request

    url = f"{scheme}://{target.host}/api/{str(path).strip('/')}"
    headers = {
        # Built by hand rather than with HTTPBasicAuthHandler, which only sends
        # credentials after a 401 round-trip.
        'Authorization': 'Basic ' + base64.b64encode(
            f'{target.username}:{target.password}'.encode()).decode(),
        'Accept': 'application/json',
    }
    data = None
    if value is not _UNSET:
        # The API expects a form-encoded 'data' field holding JSON, not a JSON
        # request body. See docs/ncos-api/config/README.md.
        data = urllib.parse.urlencode({'data': json.dumps(value)}).encode()
        headers['Content-Type'] = 'application/x-www-form-urlencoded'

    if target.verify_tls:
        context = ssl.create_default_context()
    else:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=target.timeout,
                                    context=context) as response:
            body = response.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', 'replace').strip()
        if exc.code == 401:
            raise OSError(
                f'401 unauthorized for {target.username}@{target.host}. Check '
                f'{_REST_ENV_NAMES["username"][0]} and '
                f'{_REST_ENV_NAMES["password"][0]}.'
            ) from None
        raise OSError(f'HTTP {exc.code} {exc.reason} for {method} {url}: '
                      f'{detail[:200]}') from None

    if len(body) > _MAX_RESPONSE_BYTES:
        raise OSError(f'response exceeded the {_MAX_RESPONSE_BYTES} byte cap')

    text = body.decode('utf-8', 'replace')
    if not text.strip():
        return {'status': 'ok', 'data': None}
    try:
        payload = json.loads(text)
    except ValueError:
        # An HTML login page here means the router answered but did not treat
        # this as an API call.
        raise OSError(f'{method} {url} returned non-JSON ({text[:120]!r}). Is '
                      'this an NCOS router, and is the API on this scheme?') from None

    # REST wraps replies; the socket does not. Unwrap so both transports present
    # the same shape to every accessor above.
    if isinstance(payload, dict) and 'success' in payload:
        if not payload.get('success'):
            return {'status': 'error', 'data': payload.get('data'),
                    'detail': json.dumps(payload)[:200]}
        return {'status': 'ok', 'data': payload.get('data')}
    return {'status': 'ok', 'data': payload}


def _rest_dispatch(verb: str, path: Any, query: Any, tree: Any,
                   value: Any, target: 'RestTarget') -> Dict[str, Any]:
    if verb not in _REST_METHODS:
        log(f'{verb}: not available over the REST transport. Only '
            f'{"/".join(sorted(_REST_METHODS))} have a REST equivalent; '
            'use the Config Store socket on the router for the rest.')
        return {}
    if query:
        _warn_once(f'rest_query_{verb}',
                   f'{verb}: the REST transport ignores query={query!r}')
    if tree not in (0, '0', '', None):
        _warn_once(f'rest_tree_{verb}',
                   f'{verb}: the REST transport ignores tree={tree!r}')

    errors = []
    for scheme in target.schemes():
        try:
            response = _rest_once(target, scheme, _REST_METHODS[verb], path, value)
        except OSError as exc:
            message = str(exc)
            errors.append(f'{scheme}: {message}')
            # An auth failure is a definitive answer; retrying the other scheme
            # would only bury it.
            if '401 unauthorized' in message:
                break
            continue
        except Exception as exc:  # noqa: BLE001 - a poller must survive anything
            errors.append(f'{scheme}: {type(exc).__name__}: {exc}')
            continue

        if response.get('status') == 'error':
            # The router answered, so the transport is healthy; the request was
            # rejected. Surfaced rather than silently returning None.
            _record(True)
            log(f'{verb} {path}: router rejected the request: '
                f'{response.get("detail", "")}')
            return response
        _record(True)
        return response

    return _fail('; '.join(errors) or 'no scheme attempted')


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def _dispatch(verb: str, path: Any = '', query: Any = '', tree: Any = 0,
              value: Any = _UNSET, name: Any = None) -> Dict[str, Any]:
    """Send one request and return {'status': str, 'data': Any}.

    Returns an empty dict on any failure, so callers can use `.get('data')`
    without a None check. Caller mistakes are logged and returned as {} without
    touching backend health -- they say nothing about whether the router is
    reachable.
    """
    try:
        fields = _command_fields(verb, path, query, tree, value, name)
        # Encoded even for REST, so both transports enforce the same field
        # validation and the same ASCII restriction.
        command = _encode_command(verb, fields)
    except _CommandError as exc:
        log(f'{verb}: {exc}')
        return {}

    with _lock:
        mode = _transport['mode']
        target = _transport['rest']
    if mode == 'rest':
        if target is None:                       # defensive; use_rest sets both
            return _fail('REST transport selected with no target configured')
        return _rest_dispatch(verb, path, query, tree, value, target)
    return _socket_dispatch(command)


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

def config_store_available() -> bool:
    """True when the active backend is reachable.

    Every accessor returns None both when a path holds no data and when the
    router cannot be reached at all. Use this to tell those apart and report the
    real problem.

    Probes `status/product_info` when nothing has been tried yet, and re-probes a
    failed backend at most every `CP_PROBE_COOLDOWN` seconds (30 by default), so
    a socket that appears after startup is picked up without a restart.

    A poller that must recover from a late-appearing backend is still better
    written as "attempt the read, then explain a None" than as "ask whether the
    backend is up, then decide whether to read" -- within the cooldown window
    this returns the last known answer, which is the point of the cooldown.
    """
    now = time.monotonic()
    with _lock:
        state = _transport['ok']
        if state is None:
            probe = True                       # nothing tried yet
        elif state is False:
            probe = (now - _transport['last_probe']) >= _PROBE_COOLDOWN
        else:
            probe = False                      # already known good
        if probe:
            # Claim the probe slot before releasing the lock, so concurrent
            # callers do not stampede the backend.
            _transport['last_probe'] = now

    if probe:
        get('status/product_info')
        with _lock:
            state = _transport['ok']
    return bool(state)


def config_store_status() -> Dict[str, Any]:
    """Connectivity detail, suitable for a status endpoint or health check.

    Safe to serve: when the REST transport is active, `target` reports the host
    and username but never the password.
    """
    available = config_store_available()
    with _lock:
        mode = _transport['mode']
        target = _transport['rest']
        status = {
            'transport': mode,
            'available': available,
            'socket_path': SOCKET_PATH,
            'socket_exists': os.path.exists(SOCKET_PATH),
            'last_error': _transport['error'],
            'failures': _transport['failures'],
            'successes': _transport['successes'],
        }
    if mode == 'rest' and target is not None:
        status['target'] = target.describe()
    else:
        status['target'] = f'unix:{SOCKET_PATH}'
    return status


def last_transport_error() -> Optional[str]:
    """Text of the most recent transport failure, or None."""
    with _lock:
        return _transport['error']


# ---------------------------------------------------------------------------
# Core access
# ---------------------------------------------------------------------------

def get(path: str, query: str = '', tree: int = 0) -> Any:
    """Read from the router tree.

    Returns the data itself, already unwrapped -- do not call `.get('data')` on
    the result. Returns None when the path holds no data or the router is
    unreachable; `config_store_available()` tells those apart.

        cp.get('status/wan/connection_state')   # 'connected'
        cp.get('status/system')                 # {'uptime': ..., 'cpu': {...}}
    """
    return _dispatch('get', path, query, tree).get('data')


def decrypt(path: str, query: str = '', tree: int = 0) -> Any:
    """Read and decrypt an encrypted value, such as a certificate private key.

    Socket transport only -- there is no REST equivalent. Same return convention
    as `get()`.
    """
    return _dispatch('decrypt', path, query, tree).get('data')


def put(path: str, value: Any = '', query: str = '', tree: int = 0) -> Optional[Dict[str, Any]]:
    """Update a value in the router tree.

    Returns the raw response `{'status': str, 'data': Any}`, or None if the
    request failed. The exact success string in `status` is firmware dependent,
    so where a write really matters, confirm it by reading the value back rather
    than trusting the status field.

        cp.put('config/system/gps/enabled', True)
        cp.put(f'config/wan/rules2/{rule_id}/priority', 1)
    """
    return _dispatch('put', path, query, tree, value) or None


def post(path: str, value: Any = '', query: str = '') -> Optional[Dict[str, Any]]:
    """Create a new entry in the router tree.

    Used for list-valued config such as `config/wan/rules2/` or
    `config/system/sdk/appdata`. Same return convention as `put()`.
    """
    return _dispatch('post', path, query, value=value) or None


def delete(path: str, query: str = '') -> Optional[Dict[str, Any]]:
    """Delete an entry from the router tree. Same return convention as `put()`."""
    return _dispatch('delete', path, query) or None


# ---------------------------------------------------------------------------
# NCM alerts
# ---------------------------------------------------------------------------

# Alert text longer than this is truncated. The real ceiling is unknown; this is
# a conservative bound to keep a runaway message from being sent at all.
_ALERT_MAX_CHARS = 1024


def _sanitise_alert_field(value: Any) -> str:
    """Make a string safe to place in a newline-delimited protocol field.

    Alert text is human-facing prose that routinely carries interpolated data, so
    newlines and tabs are collapsed to spaces rather than rejected -- unlike a
    path, where quietly altering the value would change which node is addressed.

    Non-ASCII characters are replaced, because commands are ASCII-encoded and
    whether the Config Store accepts UTF-8 is untested.
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

    Socket transport only: there is no REST equivalent for this verb.
    """
    if _mode() == 'rest':
        log('alert: not available over the REST transport -- the alert verb '
            'exists only on the Config Store socket. Run this on the router.')
        return False

    text = _sanitise_alert_field(value)
    if not text:
        log('alert: refusing to send an empty alert -- NCM would show it as '
            '"Router NCOS App Generated Alert" with no detail')
        return False
    if len(text) > _ALERT_MAX_CHARS:
        text = text[:_ALERT_MAX_CHARS - 3] + '...'

    label = _sanitise_alert_field(APP_NAME if name is None else name)
    response = _dispatch('alert', value=text, name=label)
    if not response:
        log(f'alert: not sent, Config Store unreachable: {text[:80]}')
        return False

    status = str(response.get('status', ''))
    if status == 'ok':
        return True
    log(f"alert: router did not accept the alert "
        f"(status={status!r} body={response.get('data')!r})")
    return False


# ---------------------------------------------------------------------------
# Unimplemented in containers
# ---------------------------------------------------------------------------

_UNSUPPORTED_NOTE = (
    'not implemented for containers: {name}(). {reason} '
    'See the module docstring in cp.py.'
)

# Said to need the event socket, which containers are said not to have. No test
# of this from a container is on record, so the stub says so: a stub that states
# a confident reason is read as evidence, and this repo has already been wrong
# once about exactly this kind of claim (alert(), which turned out to work).
_EVENTS_REASON = (
    'Config store event subscriptions are said to need the event socket, which '
    'containers are said not to have -- UNVERIFIED, no test on record. Poll '
    'with get() on an interval and compare against the previous sample.'
)


def register(action: str, path: str, callback: Any, *args: Any) -> None:
    """Not implemented for containers. Poll with `get()` instead."""
    log(_UNSUPPORTED_NOTE.format(name='register', reason=_EVENTS_REASON))
    return None


def on(action: str, path: str, callback: Any, *args: Any) -> None:
    """Alias for `register()`. Not implemented for containers."""
    return register(action, path, callback, *args)


def unregister(eid: Any = None) -> None:
    """Counterpart to `register()`. Not implemented for containers."""
    log(_UNSUPPORTED_NOTE.format(name='unregister', reason=_EVENTS_REASON))
    return None


# ---------------------------------------------------------------------------
# Application configuration (appdata)
# ---------------------------------------------------------------------------

_APPDATA_PATH = 'config/system/sdk/appdata'


def _appdata_entries() -> Optional[List[Dict[str, Any]]]:
    entries = get(_APPDATA_PATH)
    if not isinstance(entries, list):
        return None
    return [entry for entry in entries if isinstance(entry, dict)]


def _match_appdata(entries: List[Dict[str, Any]], name: Any) -> List[Dict[str, Any]]:
    """Entries whose name matches, case-insensitively and ignoring surround.

    Read, write and delete all go through this one comparison on purpose. When
    they disagree -- a case-insensitive read against a case-sensitive write, as
    this module used to have -- a write creates a duplicate entry and then its
    own read-back verification inspects the older one and reports failure.
    """
    target = str(name).strip().lower()
    return [entry for entry in entries
            if str(entry.get('name', '')).strip().lower() == target]


def _appdata_id(entry: Dict[str, Any], caller: str, name: Any) -> Optional[str]:
    entry_id = entry.get('_id_')
    if entry_id is None:
        log(f'{caller}: appdata entry {name!r} has no _id_ field, so it cannot '
            f'be addressed individually (entry keys: {sorted(entry)})')
        return None
    return str(entry_id)


def get_appdata(name: str = '') -> Union[None, str, List[Dict[str, Any]]]:
    """Read a value set in NCM under System > SDK Data.

    With a name, returns that value as a string, or None if unset. With no name,
    returns the raw list of `{'_id_', 'name', 'value'}` entries.

    Appdata is how configuration reaches a container from NCM. All values are
    strings; parse and validate them with defaults. Name matching is
    case-insensitive, as it is for `put_appdata()` and `delete_appdata()`.
    """
    entries = _appdata_entries()
    if entries is None:
        return None
    if not name:
        return entries
    matches = _match_appdata(entries, name)
    return matches[0].get('value') if matches else None


def put_appdata(name: str, value: Any = '') -> bool:
    """Set an appdata value, creating it if it does not exist.

    Returns True only if the value was verified by reading it back. Config Store
    writes do not raise on failure, so an unverified True would be a claim this
    function cannot support.
    """
    text = str(value)
    entries = _appdata_entries()
    if entries is None:
        log(f'put_appdata: could not read {_APPDATA_PATH} -- is the router '
            f'reachable? ({last_transport_error() or "no data at that path"})')
        return False

    matches = _match_appdata(entries, name)
    if matches:
        if len(matches) > 1:
            log(f'put_appdata: {len(matches)} appdata entries are named '
                f'{name!r}; updating the first. NCM shows each as its own row '
                'and reads only ever return the first, so delete the extras.')
        entry_id = _appdata_id(matches[0], 'put_appdata', name)
        if entry_id is None:
            return False
        put(f'{_APPDATA_PATH}/{entry_id}/value', text)
    else:
        post(_APPDATA_PATH, {'name': str(name), 'value': text})
    return get_appdata(name) == text


def post_appdata(name: str, value: Any = '') -> bool:
    """Create a new appdata entry. Returns True if verified by read-back.

    Refuses when an entry of that name already exists, rather than creating a
    duplicate: NCM would show two rows for one setting and every read returns
    only the first. Use `put_appdata()`, which creates or updates.
    """
    text = str(value)
    entries = _appdata_entries()
    if entries is None:
        log(f'post_appdata: could not read {_APPDATA_PATH} -- is the router '
            f'reachable? ({last_transport_error() or "no data at that path"})')
        return False
    if _match_appdata(entries, name):
        log(f'post_appdata: an appdata entry named {name!r} already exists; '
            'refusing to create a duplicate. Use put_appdata() to update it.')
        return False
    post(_APPDATA_PATH, {'name': str(name), 'value': text})
    return get_appdata(name) == text


def delete_appdata(name: str) -> bool:
    """Delete an appdata entry. Returns True if it is gone afterwards.

    Removes every entry matching the name, so a set of duplicates created before
    this module matched case consistently is cleaned up in one call.
    """
    entries = _appdata_entries()
    if entries is None:
        log(f'delete_appdata: could not read {_APPDATA_PATH} -- is the router '
            f'reachable? ({last_transport_error() or "no data at that path"})')
        return False
    matches = _match_appdata(entries, name)
    if not matches:
        return True
    for entry in matches:
        entry_id = _appdata_id(entry, 'delete_appdata', name)
        if entry_id is not None:
            delete(f'{_APPDATA_PATH}/{entry_id}')
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
    """Firmware version, e.g. '7.25.20', or None.

    Returns None rather than assembling a version out of missing parts. An
    earlier version interpolated the three fields unchecked and produced the
    string 'None.None.None' on an unexpected payload, which then flowed into
    logs and comparisons looking like data.
    """
    info = get('status/fw_info')
    if not isinstance(info, dict):
        return None
    parts = [info.get('major_version'), info.get('minor_version'),
             info.get('patch_version')]
    if any(part is None for part in parts):
        log('get_firmware_version: status/fw_info has no major/minor/patch '
            f'version fields (keys present: {sorted(info)})')
        return None
    version = '.'.join(str(part) for part in parts)
    if not include_build_info:
        return version
    build = [str(info[key]) for key in ('build_date', 'build_version')
             if info.get(key)]
    return f"{version} ({' '.join(build)})" if build else version


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

def _pause(seconds: float, stop: Optional[threading.Event]) -> bool:
    """Wait, interruptibly. Returns False if `stop` was set during the wait.

    With a stop event this blocks on the event, so a shutdown is acted on the
    instant it is signalled. Without one it sleeps in short steps: a signal
    handler that only sets a flag cannot shorten a sleep already in progress,
    because Python reissues the syscall to honour the full duration (PEP 475).
    """
    if seconds <= 0:
        return not (stop is not None and stop.is_set())
    if stop is not None:
        return not stop.wait(seconds)
    remaining = seconds
    while remaining > 0:
        time.sleep(min(_SLEEP_STEP, remaining))
        remaining -= _SLEEP_STEP
    return True


def _wait(label: str, poll: Callable[[], Union[bool, float]], timeout: float,
          stop: Optional[threading.Event]) -> bool:
    """Poll until ready, bounded by `timeout` and interruptible by `stop`.

    `poll()` returns True when ready, or the number of seconds to wait before
    the next attempt. Every wait is clamped to the time left, so the helper
    cannot overshoot its own timeout -- which it used to do by up to a factor of
    three, indistinguishable from an entrypoint that ignores signals when it
    happens during `docker stop`.
    """
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        if stop is not None and stop.is_set():
            log(f'{label}: stopping early, shutdown requested')
            return False

        outcome = poll()
        if outcome is True:
            return True

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log(f'{label}: timed out after {timeout}s')
            return False

        try:
            interval = max(0.1, float(outcome))
        except (TypeError, ValueError):
            interval = 1.0
        if not _pause(min(interval, remaining), stop):
            log(f'{label}: stopping early, shutdown requested')
            return False


def wait_for_uptime(min_uptime_seconds: float = 60.0, timeout: float = 300.0,
                    stop: Optional[threading.Event] = None) -> bool:
    """Block until the router has been up for at least `min_uptime_seconds`.

    Useful at container start, since router services are still settling in the
    first minute after boot. Returns False on timeout, or as soon as `stop` is
    set. Pass the same `threading.Event` your signal handler sets, or a
    `docker stop` arriving during startup waits out the whole timeout.
    """
    def poll() -> Union[bool, float]:
        uptime = get_uptime()
        if uptime is not None and uptime >= min_uptime_seconds:
            return True
        # Wait roughly until the threshold should be reached, within reason.
        shortfall = min_uptime_seconds - (uptime or 0.0)
        return max(1.0, min(shortfall, 10.0))

    return _wait('wait_for_uptime', poll, timeout, stop)


def wait_for_ntp(timeout: float = 300.0, check_interval: float = 1.0,
                 stop: Optional[threading.Event] = None) -> bool:
    """Block until the router's clock looks NTP synchronised.

    Worth calling before timestamping anything that leaves the device. Returns
    False on timeout, or as soon as `stop` is set.

    Readiness is inferred from `status/system/ntp/sync_age` being present. That
    the field's presence means "synchronised" rather than merely "NTP has been
    attempted" is UNVERIFIED against a router that has never synchronised.
    """
    interval = max(0.1, float(check_interval))

    def poll() -> Union[bool, float]:
        if get('status/system/ntp/sync_age') is not None:
            return True
        return interval

    return _wait('wait_for_ntp', poll, timeout, stop)


def wait_for_wan_connection(timeout: float = 300.0, check_interval: float = 1.0,
                            stop: Optional[threading.Event] = None) -> bool:
    """Block until at least one WAN is connected.

    Returns False on timeout, or as soon as `stop` is set.
    """
    interval = max(0.1, float(check_interval))

    def poll() -> Union[bool, float]:
        if get('status/wan/connection_state') == 'connected':
            return True
        return interval

    return _wait('wait_for_wan_connection', poll, timeout, stop)


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
        if not str(uid).startswith('mdm-'):
            continue
        status = device.get('status') if isinstance(device, dict) else {}
        error_text = status.get('error_text', '') if isinstance(status, dict) else ''
        if error_text and 'NOSIM' in error_text:
            continue
        sims.append(uid)
    return sims


def _priority_key(rule: Dict[str, Any]) -> Tuple[int, float]:
    """Sort key for a WAN rule.

    Unparseable priorities sort last instead of raising. Mixing a string and a
    number across rules would otherwise make the whole sort fail with a
    TypeError, taking out a caller that only wanted to list the profiles.
    """
    try:
        return 0, float(rule.get('priority'))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 1, 0.0


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
    return sorted([rule for rule in rules if isinstance(rule, dict)],
                  key=_priority_key)


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
        log(f'get_gpio: no logical pin map for model {model!r}; use '
            "cp.get('status/gpio') for raw keys")
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

    One position this cannot get right: `degree` is an integer in the router's
    payload (docs/ncos-api/status/gps/fix.md), and integer zero carries no sign,
    so a location within one degree south of the equator or west of the prime
    meridian arrives indistinguishable from its northern or eastern mirror. The
    sign is lost before this function sees it. A float -0.0 does carry it and is
    handled.
    """
    try:
        degrees = float(degree)
        magnitude = (abs(degrees)
                     + abs(float(minute)) / 60.0
                     + abs(float(second)) / 3600.0)
    except (TypeError, ValueError):
        return None
    negative = degrees < 0 or str(degree).strip().startswith('-')
    return round(-magnitude if negative else magnitude, 6)


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


# ---------------------------------------------------------------------------
# Connectivity check: python3 cp.py [--rest] [path]
# ---------------------------------------------------------------------------

def _main(argv: Optional[Sequence[str]] = None) -> int:
    """Body of the CLI. A function rather than an `if __name__` block so the
    exit codes and the refusal paths can be tested in-process."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    paths = [arg for arg in arguments if arg != '--rest']

    if '--rest' in arguments:
        try:
            use_rest()
        except (ValueError, RuntimeError) as error:
            # ValueError: not configured. RuntimeError: refused, because the
            # Config Store socket is right here and should be used instead.
            log(f'{error}')
            return 2

    query_path = paths[0] if paths else 'status/product_info'
    state = config_store_status()
    log(f'transport: {state}')
    if not state['available']:
        if state['transport'] == 'socket':
            log('no Config Store. Attach the $CONFIG_STORE volume to this '
                'service, or use --rest with CP_ROUTER_HOST/PASSWORD set to '
                'reach a remote router.')
        else:
            log('router not reachable over REST.')
        return 1
    log(f'{query_path} = {json.dumps(get(query_path), indent=2)}')
    return 0


if __name__ == '__main__':
    sys.exit(_main())
