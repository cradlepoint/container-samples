# config/container

<!-- path: config/container -->
<!-- type: config -->
<!-- response: object -->

[config](README.md) / container

---

Container Orchestrator configuration: compose projects, image registries, and
the Docker bridge subnet.

The path is `config/container` — **not** `config/system/container`, despite the
NCM UI presenting it under SYSTEM > Containers. All paths are indexed in
[PATHS.md](PATHS.md); search it for `container` rather than guessing.

## Fields

| Field | Type | Description |
|-------|------|-------------|
| `daemon_opts` | struct | Container engine options |
| `daemon_opts/bip` | string | Docker bridge IP/CIDR. Default `172.17.0.1/16` |
| `projects` | array | Compose projects, unique by `name` |
| `registry` | array | Image registries, unique by `server` |

**projects[]**

| Field | Type | Description |
|-------|------|-------------|
| `_id_` | uuid | Assigned by the router |
| `name` | string | Project name. Must be unique |
| `config` | string | **The entire compose YAML, as a single string** |
| `enabled` | boolean | Default `true` |
| `update_interval` | u32 | Seconds between automatic image update checks. `0` disables |

**registry[]**

| Field | Type | Description |
|-------|------|-------------|
| `_id_` | uuid | Assigned by the router |
| `server` | string | Registry host. Must be unique |
| `username` | string | Registry username |
| `password` | string | Registry password. Stored encrypted (`encrypt: true` in the DTD) |
| `ca_uuid` | uuid | CA certificate for the registry, may be blank |
| `cert_uuid` | uuid | Client certificate, may be blank |

An **empty `registry` array means anonymous Docker Hub pulls**, which is all a
public image needs. Add an entry only for a private or third-party registry.

## Deploying a project without the NCM UI

The compose YAML is just a string field in an array, so a project can be created
and updated entirely over REST or the SDK. This makes the whole
build → push → deploy → read-logs loop scriptable from a workstation, which is
considerably faster than clicking through the Compose Builder for each iteration.

```python
import sys; sys.path.insert(0, 'tools')
import dev_router

compose = '''version: "2.4"
services:
  my_service:
    image: "myuser/myimage:latest"
    container_name: my_service
    network_mode: bridge
    restart: unless-stopped
    mem_limit: 64M
    volumes:
      - $CONFIG_STORE
    logging:
      driver: json-file
'''

dev_router.post('config/container/projects', {
    'name': 'my-project',
    'enabled': True,
    'update_interval': 0,
    'config': compose,
})

# Verify by read-back; the write status alone is not evidence.
projects = dev_router.get('config/container/projects') or []
print([p['name'] for p in projects])
```

To update an existing project, PUT the single field rather than replacing the
array:

```python
dev_router.put(f'config/container/projects/{project_id}/config', compose)
```

Notes:

- **Read an existing project first.** `cp.get('config/container/projects')` shows
  the exact compose shape this firmware has already accepted, which is better
  evidence than any example.
- `$CONFIG_STORE` stays literal in the stored string; the platform interpolates
  it when the project is deployed.
- Committing config this way applies it locally on the router. It does not go
  through NCM, so NCM's own view of the project may lag.

## Compose gotchas worth knowing

- **Quote `restart: "no"`.** Bare `no` is boolean false in YAML 1.1, not the
  string `no`, so an unquoted value is not the restart policy you wrote.
- **Set `container_name`** if anything will refer to the container by name.
  Without it, compose derives a name from the project and service, and
  `container logs <name>` becomes a guess.
- Escape literal `$` as `$$`, since the platform interpolates compose values.

## Related

- [status/container](../status/container.md) — running container state
- [containers-quick-start.md](../../containers-quick-start.md) — the NCM UI path
- [PATHS.md](PATHS.md) — full config path index
