# Development Host Tools

Tools that run on your workstation, not on the router and not inside a
container. Nothing here belongs in an image: containers reach the router through
`cp.py` and the Config Store socket, with no credentials involved.

## Development router access

Credentials live in `.env` at the repo root, which is gitignored. Create it:

```bash
python3 tools/dev_router.py init     # copies .env.example, mode 600
$EDITOR .env                         # set NCOS_DEV_HOST and NCOS_DEV_PASSWORD
python3 tools/dev_router.py check
```

`check` prints the model, serial, firmware and any container projects, and never
prints the password.

Real environment variables override the file, so the password need never touch
disk:

```bash
NCOS_DEV_PASSWORD="$(op read op://vault/dev-router/password)" \
    python3 tools/dev_router.py check
```

### Usage

```bash
python3 tools/dev_router.py get status/product_info
python3 tools/dev_router.py get status/container         # container projects
python3 tools/dev_router.py get status/gps/fix
python3 tools/dev_router.py put config/system/gps/enabled true
python3 tools/dev_router.py appdata gps_poll_interval    # read one value
python3 tools/dev_router.py appdata gps_poll_interval 2.0  # write, verified
python3 tools/dev_router.py ssh container list           # CLI-only commands
python3 tools/dev_router.py ssh container logs my_service
```

Importable as a module too:

```python
import sys; sys.path.insert(0, 'tools')
import dev_router
dev_router.get('status/wan/connection_state')
dev_router.set_appdata('poll_interval', '5')
```

### Behaviour worth knowing

- **Responses are unwrapped.** REST replies are `{"success": true, "data": ...}`;
  this returns the `data` so calls match `cp.get()` inside a container. A
  `success: false` reply raises rather than returning `None`, so a rejected path
  cannot be mistaken for an empty one.
- **Writes are verified by read-back.** `set_appdata()` re-reads the value and
  returns `False` if it did not land, the same contract as `cp.put_appdata()`.
- **REST covers `config/`, `status/` and `control/`. The `container` commands are
  CLI-only**, so `container list`, `container logs` and `container exec` go over
  SSH via the `ssh` subcommand.
- **Scheme is `auto` by default**, trying HTTPS then HTTP, since dev routers vary
  in which they answer on. Pin it with `NCOS_DEV_SCHEME` once you know.
- **TLS verification is off by default.** Routers ship self-signed certificates,
  so the connection is encrypted but not authenticated. Acceptable on a trusted
  dev LAN; set `NCOS_DEV_VERIFY_TLS=true` if you have installed a certificate
  that validates. Do not use this tool across the internet.

### Credential handling

- The password is never printed, never logged, and never passed on a command
  line. `curl -u user:pass` and `sshpass -p secret` are both visible to every
  local user through `ps`, so REST is done in-process with `urllib` and SSH
  passes the password to `sshpass -e` through the environment.
- `Settings.__repr__` is redacted, so the password cannot surface in a traceback
  or a stray `print`.
- `.env` is created mode 600, and a group- or world-readable file is reported as
  a warning with the `chmod` to fix it.
- These are full admin credentials. Use a development router, not production.

### Not verified

The REST and SSH paths have been exercised against a mock NCOS API (auth,
envelope unwrapping, appdata round-trips, error paths) but **not against a real
router**. The request shape for writes — a form-encoded `data` field holding
JSON — comes from the documented API in `docs/ncos-api/`, and the HTTPS-then-HTTP
fallback assumes the router answers Basic auth on at least one. Both need
confirming on hardware the first time this is used.
