# status/container

<!-- path: status/container -->
<!-- type: status -->
<!-- response: object -->

[status](README.md) / container

---

Container runtime status. The on-router engine is `cpdockerengine` (historically
balena-derived; whether that is still true on current firmware is
unconfirmed — treat the underlying engine as an implementation detail rather
than something to depend on). It presents no Docker API or socket, only the
CLI (`container list`/`logs`/`exec`, no REST equivalent — see
`docs/ncos-api/config/container.md`) and this status path. Top-level keys here
are container names.

### Structure

**{container_name}** (per-container object)

| Field | Type | Description |
|-------|------|-------------|
| `info` | object | See [container/arc.md](container/arc.md) |
| `stats` | object | See [container/arc.md](container/arc.md) |
| `state` | object | `{state, info}` |

### SDK Example
```python
import cp
cont = cp.get('status/container')
if cont:
    for name, c in cont.items():
        if isinstance(c, dict):
            state = c.get('state', {})
            cp.log(f'Container {name}: {state.get("state")}')
```

### REST
```
GET /api/status/container
```

### Breakout docs (in container/)
- [container/arc.md](container/arc.md) – info.arc[] and stats.arc[] (depth 3)
