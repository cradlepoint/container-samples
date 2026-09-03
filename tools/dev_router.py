#!/usr/bin/env python3
"""Development router access, for configuring and testing containers.

This is a **development-host** tool. It is not container code and must never be
copied into an image -- containers reach the router through `cp.py` and the
Config Store socket, with no credentials involved. This module exists for the
other side of the workflow: driving a dev router from a workstation to set
appdata, inspect container state, and read logs while iterating.

Configuration comes from `.env` at the repo root (see `.env.example`), and from
nowhere else. Process environment variables are deliberately ignored: this tool
only runs on a development host where `.env` always exists, so a second source
buys nothing and costs real confusion -- an exported value outranked the file and
did not track edits to it, which made a corrected router address look like an
unreachable router. Pass values explicitly in code to override one.

    python3 tools/dev_router.py init                     # create .env, mode 600
    python3 tools/dev_router.py check                    # verify access
    python3 tools/dev_router.py get status/product_info
    python3 tools/dev_router.py get status/container
    python3 tools/dev_router.py put config/system/gps/enabled true
    python3 tools/dev_router.py appdata gps_poll_interval 2.0
    python3 tools/dev_router.py ssh container logs my_container

Credential handling rules this module follows:

- The password is never printed, never logged, and never placed in a command
  line. `curl` and `sshpass -p` both expose credentials in the process list to
  any local user, so REST goes through `urllib` in-process instead.
- `Settings.__repr__` is redacted, so the password cannot leak through a
  traceback, a debugger, or a careless print.
- A world- or group-readable `.env` is reported as a warning.

Two API shapes are easy to confuse. The REST API wraps replies as
`{"success": true, "data": ...}` while the on-router SDK returns data directly.
This module unwraps, so `get()` here matches `cp.get()` in a container.
"""

import json
import os
import shutil
import ssl
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(REPO_ROOT, ".env")
ENV_EXAMPLE_PATH = os.path.join(REPO_ROOT, ".env.example")

_PREFIX = "NCOS_DEV_"


class DevRouterError(Exception):
    """Raised for configuration, transport and API errors alike.

    One exception type keeps the CLI's error handling simple; the message
    carries the distinction, and messages are written to say what to do next.
    """


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def parse_env_file(path: str) -> Dict[str, str]:
    """Parse a dotenv-style file into a dict.

    Deliberately minimal, and deliberately *not* configparser: no interpolation
    and no sections, so a password containing '%' or '$' survives intact.

    Inline comments are not stripped. `#` is a comment only at the start of a
    line, because it is a perfectly ordinary password character and silently
    truncating at one would produce a confusing auth failure.
    """
    values: Dict[str, str] = {}
    if not os.path.exists(path):
        return values
    with open(path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Strip one matching outer quote pair, if present.
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if key:
                values[key] = value
    return values


def _as_bool(value: Optional[str], default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    host: str = ""
    username: str = "admin"
    password: str = field(default="", repr=False)
    scheme: str = "auto"
    verify_tls: bool = False
    timeout: float = 10.0
    # Where each value came from, for `check` output and troubleshooting.
    sources: Dict[str, str] = field(default_factory=dict, repr=False)

    def __repr__(self) -> str:
        """Redacted on purpose. A default dataclass repr would print the
        password into any traceback that carries a Settings instance."""
        return (
            f"Settings(host={self.host!r}, username={self.username!r}, "
            f"password=<redacted:{'set' if self.password else 'empty'}>, "
            f"scheme={self.scheme!r}, verify_tls={self.verify_tls!r}, "
            f"timeout={self.timeout!r})"
        )

    @property
    def configured(self) -> bool:
        return bool(self.host and self.username and self.password)

    def describe(self) -> Dict[str, Any]:
        """Safe-to-print summary. Reports whether the password is set, never
        its value or length."""
        return {
            "host": self.host or None,
            "username": self.username or None,
            "password": "set" if self.password else "NOT SET",
            "scheme": self.scheme,
            "verify_tls": self.verify_tls,
            "timeout": self.timeout,
            "sources": self.sources,
        }


def check_env_permissions(path: str = ENV_PATH) -> Optional[str]:
    """Return a warning if the credentials file is readable by others."""
    if not os.path.exists(path):
        return None
    mode = os.stat(path).st_mode
    if mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH):
        return (
            f"{path} is mode {stat.filemode(mode)} -- readable beyond your user. "
            f"Run: chmod 600 {path}"
        )
    return None


def load(env_path: str = ENV_PATH) -> Settings:
    """Load settings from `.env`. That file is the only source.

    Process environment variables are deliberately ignored. This tool only ever
    runs on a development host, where `.env` always exists, so a second source
    buys nothing and costs real confusion: an exported `NCOS_DEV_*` value used to
    outrank the file and not track edits to it, so a corrected router address read
    as "the router is unreachable" rather than "your edit is being ignored", and a
    stale address that happened to be live drove the wrong device. Edit `.env`, or
    pass values explicitly in code.
    """
    from_file = parse_env_file(env_path)
    settings = Settings()
    sources: Dict[str, str] = {}

    def resolve(name: str) -> Optional[str]:
        key = _PREFIX + name
        if from_file.get(key):
            sources[name.lower()] = ".env"
            return from_file[key]
        # An explicitly empty value in the file is still a decision, and needs
        # to be distinguishable from an absent key when reporting sources.
        if key in from_file:
            sources[name.lower()] = ".env (empty)"
            return from_file[key]
        sources[name.lower()] = "default"
        return None

    host = resolve("HOST")
    username = resolve("USERNAME")
    password = resolve("PASSWORD")
    scheme = resolve("SCHEME")
    verify = resolve("VERIFY_TLS")
    timeout = resolve("TIMEOUT")

    settings.host = (host or "").strip().rstrip("/")
    # Tolerate a scheme pasted into the host, rather than failing obscurely
    # later with a URL like https://https://192.168.0.1/api/...
    for marker in ("https://", "http://"):
        if settings.host.startswith(marker):
            settings.scheme = marker[:-3]
            settings.host = settings.host[len(marker):]
            sources["scheme"] = "derived from host"
    settings.username = (username or "admin").strip()
    settings.password = password or ""
    if sources.get("scheme") != "derived from host":
        settings.scheme = (scheme or "auto").strip().lower()
    if settings.scheme not in ("auto", "https", "http"):
        raise DevRouterError(
            f"{_PREFIX}SCHEME must be auto, https or http (got {settings.scheme!r})"
        )
    settings.verify_tls = _as_bool(verify, False)
    try:
        settings.timeout = float(timeout) if timeout else 10.0
    except ValueError:
        raise DevRouterError(f"{_PREFIX}TIMEOUT must be a number (got {timeout!r})")
    settings.sources = sources
    return settings


def require_configured(settings: Settings) -> None:
    """Fail loudly and usefully when credentials are missing.

    The tool this replaced silently fell back to a hardcoded IP and an empty
    password, which produced a confusing connection error against a router that
    was not even the intended target.
    """
    if settings.configured:
        return
    missing = [
        name
        for name, value in (
            ("NCOS_DEV_HOST", settings.host),
            ("NCOS_DEV_USERNAME", settings.username),
            ("NCOS_DEV_PASSWORD", settings.password),
        )
        if not value
    ]
    hint = "run `python3 tools/dev_router.py init`" if not os.path.exists(ENV_PATH) else f"edit {ENV_PATH}"
    raise DevRouterError(
        f"development router is not configured: {', '.join(missing)} not set. "
        f"To fix, {hint}, or export the variables for this shell."
    )


# ---------------------------------------------------------------------------
# REST transport
# ---------------------------------------------------------------------------


def _ssl_context(settings: Settings) -> ssl.SSLContext:
    if settings.verify_tls:
        return ssl.create_default_context()
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _schemes(settings: Settings) -> Tuple[str, ...]:
    return ("https", "http") if settings.scheme == "auto" else (settings.scheme,)


def _request_once(
    settings: Settings, scheme: str, method: str, path: str, value: Any, sentinel: object
) -> Any:
    url = f"{scheme}://{settings.host}/api/{path.strip('/')}"
    data = None
    headers = {
        # Basic auth is built by hand rather than via HTTPBasicAuthHandler,
        # which only sends credentials after a 401 round-trip.
        "Authorization": "Basic "
        + b64encode(f"{settings.username}:{settings.password}".encode()).decode(),
        "Accept": "application/json",
    }
    if value is not sentinel:
        # The API expects a form-encoded 'data' field holding JSON.
        data = urllib.parse.urlencode({"data": json.dumps(value)}).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(
            request, timeout=settings.timeout, context=_ssl_context(settings)
        ) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace").strip()
        if exc.code == 401:
            raise DevRouterError(
                f"401 unauthorized for {settings.username}@{settings.host}. "
                "Check NCOS_DEV_USERNAME and NCOS_DEV_PASSWORD."
            ) from None
        raise DevRouterError(f"HTTP {exc.code} {exc.reason} for {method} {url}: {detail[:400]}") from None

    if not body.strip():
        return None
    try:
        payload = json.loads(body)
    except ValueError:
        # An HTML login page here means the router answered on this scheme but
        # did not accept the request as an API call.
        raise DevRouterError(
            f"{method} {url} returned non-JSON ({body[:120]!r}). "
            "Is this an NCOS router, and is the API reachable on this scheme?"
        ) from None

    # REST wraps replies; the SDK does not. Unwrap so callers see the same shape
    # cp.get() gives inside a container.
    if isinstance(payload, dict) and "success" in payload:
        if not payload.get("success"):
            raise DevRouterError(f"router rejected {method} {path}: {json.dumps(payload)[:400]}")
        return payload.get("data")
    return payload


_SENTINEL = object()


def request(method: str, path: str, value: Any = _SENTINEL, settings: Optional[Settings] = None) -> Any:
    """Perform one REST call, trying each candidate scheme in turn."""
    settings = settings or load()
    require_configured(settings)
    errors = []
    for scheme in _schemes(settings):
        try:
            return _request_once(settings, scheme, method, path, value, _SENTINEL)
        except DevRouterError as exc:
            # An auth failure or an API-level rejection is a definitive answer;
            # retrying on another scheme would only obscure it.
            if "401 unauthorized" in str(exc) or "router rejected" in str(exc):
                raise
            errors.append(f"{scheme}: {exc}")
        except (urllib.error.URLError, ssl.SSLError, OSError) as exc:
            errors.append(f"{scheme}: {type(exc).__name__}: {exc}")
    raise DevRouterError(
        f"could not reach {settings.host} ({'; '.join(errors)}). "
        "Check the router address, that you are on the same network, and NCOS_DEV_SCHEME."
    )


def get(path: str, settings: Optional[Settings] = None) -> Any:
    return request("GET", path, settings=settings)


def put(path: str, value: Any, settings: Optional[Settings] = None) -> Any:
    return request("PUT", path, value, settings=settings)


def post(path: str, value: Any, settings: Optional[Settings] = None) -> Any:
    return request("POST", path, value, settings=settings)


def delete(path: str, settings: Optional[Settings] = None) -> Any:
    return request("DELETE", path, settings=settings)


# ---------------------------------------------------------------------------
# Convenience for container work
# ---------------------------------------------------------------------------

_APPDATA_PATH = "config/system/sdk/appdata"


def get_appdata(name: str, settings: Optional[Settings] = None) -> Optional[str]:
    """Read one appdata value, mirroring `cp.get_appdata()` semantics."""
    entries = get(_APPDATA_PATH, settings=settings)
    if not isinstance(entries, list):
        return None
    target = name.lower()
    for entry in entries:
        if isinstance(entry, dict) and str(entry.get("name", "")).lower() == target:
            return entry.get("value")
    return None


def set_appdata(name: str, value: Any, settings: Optional[Settings] = None) -> bool:
    """Create or update an appdata value, verifying by read-back.

    Same contract as `cp.put_appdata()`: the router's success status is not
    trusted on its own.
    """
    settings = settings or load()
    text = str(value)
    entries = get(_APPDATA_PATH, settings=settings)
    updated = False
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict) and entry.get("name") == name:
                put(f'{_APPDATA_PATH}/{entry["_id_"]}/value', text, settings=settings)
                updated = True
                break
    if not updated:
        post(_APPDATA_PATH, {"name": name, "value": text}, settings=settings)
    return get_appdata(name, settings=settings) == text


def ssh_command(args, settings: Optional[Settings] = None) -> int:
    """Run a CLI command over SSH, for the `container` commands REST lacks.

    `container list`, `container logs` and `container exec` are CLI-only, so
    testing a container needs this alongside REST.

    The password is passed to sshpass through the environment, never argv --
    `sshpass -p secret` is visible to every local user via `ps`. Without
    sshpass installed the command is printed for the operator to run, rather
    than the password being handled less carefully.
    """
    settings = settings or load()
    require_configured(settings)
    target = f"{settings.username}@{settings.host}"
    remote = " ".join(args)

    if shutil.which("sshpass"):
        environment = dict(os.environ, SSHPASS=settings.password)
        command = [
            "sshpass", "-e",
            "ssh", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            target, remote,
        ]
        return subprocess.call(command, env=environment)

    print(
        "sshpass is not installed, so this command cannot run non-interactively.\n"
        "Install it (brew install sshpass / apk add sshpass), or run this and\n"
        "enter the password yourself:\n\n"
        f"    ssh {target} {remote}\n",
        file=sys.stderr,
    )
    return 127


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cmd_init() -> int:
    if os.path.exists(ENV_PATH):
        print(f"{ENV_PATH} already exists, leaving it alone.")
        warning = check_env_permissions()
        if warning:
            print(f"warning: {warning}")
        return 0
    if not os.path.exists(ENV_EXAMPLE_PATH):
        print(f"error: {ENV_EXAMPLE_PATH} is missing", file=sys.stderr)
        return 1
    shutil.copyfile(ENV_EXAMPLE_PATH, ENV_PATH)
    os.chmod(ENV_PATH, 0o600)
    print(f"created {ENV_PATH} (mode 600). Fill in NCOS_DEV_HOST and NCOS_DEV_PASSWORD.")
    print("It is gitignored; do not commit it.")
    return 0


def _cmd_check() -> int:
    settings = load()
    warning = check_env_permissions()
    if warning:
        print(f"warning: {warning}\n")

    print("configuration:")
    for key, value in settings.describe().items():
        if key == "sources":
            continue
        print(f"  {key:<12} {value}  (from {settings.sources.get(key, 'default')})")
    print()

    if not settings.configured:
        try:
            require_configured(settings)
        except DevRouterError as exc:
            print(f"not configured: {exc}", file=sys.stderr)
        return 1

    print(f"contacting {settings.host} ...")
    info = get("status/product_info", settings=settings)
    if not isinstance(info, dict):
        print(f"unexpected reply for status/product_info: {info!r}", file=sys.stderr)
        return 1
    firmware = get("status/fw_info", settings=settings) or {}
    print(f"  model     {info.get('product_name')}")
    print(f"  serial    {(info.get('manufacturing') or {}).get('serial_num')}")
    print(
        "  firmware  "
        f"{firmware.get('major_version')}.{firmware.get('minor_version')}.{firmware.get('patch_version')}"
    )

    # Container orchestration needs NCOS 7.2.20+ and an Advanced license, so
    # surfacing this here saves a confusing deployment failure later.
    containers = get("status/container", settings=settings)
    if isinstance(containers, dict) and containers:
        print(f"  container projects: {', '.join(sorted(containers)) or 'none'}")
    else:
        print("  container projects: none reported")
    print("\naccess OK")
    return 0


def _parse_value(text: str) -> Any:
    """JSON first, plain string otherwise, so both `true` and `eth0` work."""
    try:
        return json.loads(text)
    except ValueError:
        return text


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0

    command, rest = argv[0], argv[1:]
    try:
        if command == "init":
            return _cmd_init()
        if command == "check":
            return _cmd_check()
        if command == "get":
            if not rest:
                raise DevRouterError("usage: get <path>")
            print(json.dumps(get(rest[0]), indent=2))
            return 0
        if command in ("put", "post"):
            if len(rest) < 2:
                raise DevRouterError(f"usage: {command} <path> <json-or-string>")
            handler = put if command == "put" else post
            print(json.dumps(handler(rest[0], _parse_value(rest[1])), indent=2))
            return 0
        if command == "delete":
            if not rest:
                raise DevRouterError("usage: delete <path>")
            print(json.dumps(delete(rest[0]), indent=2))
            return 0
        if command == "appdata":
            if not rest:
                raise DevRouterError("usage: appdata <name> [value]")
            if len(rest) == 1:
                print(get_appdata(rest[0]))
                return 0
            ok = set_appdata(rest[0], rest[1])
            print(f"{rest[0]}={rest[1]!r} {'verified' if ok else 'NOT verified by read-back'}")
            return 0 if ok else 1
        if command == "ssh":
            if not rest:
                raise DevRouterError("usage: ssh <command...>")
            return ssh_command(rest)
        raise DevRouterError(f"unknown command {command!r}. Run --help.")
    except DevRouterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
