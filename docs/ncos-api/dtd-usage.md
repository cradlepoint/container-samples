# Using DTD to Verify API Structures

## DTD Endpoint

**ALWAYS check the DTD before writing config API code:**

```bash
curl -s -u admin:pass http://router/api/dtd/config/path | python3 -m json.tool
```

The DTD (Document Type Definition) shows:
- Exact field names and types
- Required vs optional fields
- Default values
- Min/max values for numbers
- Array max lengths
- Allowed options for select fields

## Example: QoS Configuration

### Check DTD First

```bash
curl -s -u admin:pass http://router/api/dtd/config/qos | python3 -m json.tool
```

Shows:
- `enabled` (boolean, default: false)
- `queues` (array, maxlength: 20)
- `rules` (array, maxlength: 20, default: [])

### QoS Rules DTD

From `/api/dtd/config/qos`, rules have these fields:
- `enabled` (boolean, default: true)
- `name` (string, maxlength: 32)
- `lipaddr` (ipv4_address, allowBlank: true) - Local IP
- `lmask` (ipv4_address, allowBlank: true) - Local mask
- `ripaddr` (ipv4_address, allowBlank: true) - Remote IP
- `rmask` (ipv4_address, allowBlank: true) - Remote mask
- `lport_start`, `lport_end` (u16, min: 1, allowBlank: true)
- `rport_start`, `rport_end` (u16, min: 1, allowBlank: true)
- `protocol` (select: tcp/udp, tcp, udp, icmp, any, default: tcp/udp)
- `queue` (string, maxlength: 32) - Queue name to assign
- `ip_version` (select: ip4, ip6, default: ip4)
- `match_pri` (u16, default: 0)

**NO MAC address fields exist** - only IP addresses

### QoS Queues DTD

- `name` (string, maxlength: 32)
- `dlenabled` (boolean, default: true)
- `download_bw` (u32, default: 0) - kbps
- `ulenabled` (boolean, default: true)
- `upload_bw` (u32, default: 0) - kbps
- `pri` (u8, 0-7, default: 3)
- `downpri` (u8, 0-7, default: 3)
- `dlsharing` (boolean, default: true)
- `ulsharing` (boolean, default: true)

## Correct QoS Configuration

```python
import cp

# Build complete structure
qos_data = {
    'enabled': True,
    'queues': [{
        'name': 'throttle_queue',
        'dlenabled': True,
        'download_bw': 512,
        'ulenabled': True,
        'upload_bw': 512,
        'pri': 1,
        'downpri': 1,
        'dlsharing': True,
        'ulsharing': True
    }],
    'rules': [{
        'enabled': True,
        'name': 'limit_client',
        'lipaddr': '192.168.1.100',
        'lmask': '255.255.255.255',
        'queue': 'throttle_queue'
    }]
}

# PUT entire structure
cp.put('config/qos', qos_data)
```

## Key Learnings

1. **Always check DTD first** - don't assume field names or types
2. **Use `/api/dtd/config/path`** to see exact structure
3. **Build fresh structures** - don't append to existing and PUT back
4. **Match DTD types exactly** - boolean not string, u32 not string
5. **QoS rules match by IP, not MAC** - use lipaddr/lmask fields
6. **Silent failures** - router may accept PUT but silently reject invalid rules
7. **Test after PUT** - always GET the config back to verify it was saved
8. **Check logs** - cp.put() returns status but router may still reject data

## Querying the Offline DTD Snapshot

`config/dtd/` holds captured DTD JSON per model and NCOS version, so field
semantics can be resolved with no router available — useful when developing a
container against documentation alone.

Grep is the wrong tool for this file. A match tells you a string exists but not
which config section it belongs to, and `priority`, `enabled` and `name` appear
under dozens of unrelated paths. Walk the structure and keep the path:

```python
import json

data = json.load(open('config/dtd/E3000-NCOS-7.25.101.json'))

def walk(node, path=''):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from walk(value, f'{path}/{key}')
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from walk(value, f'{path}[{i}]')
    else:
        yield path, node

# Every priority field in the tree, with the section it belongs to
for path, value in walk(data):
    if path.endswith('priority/comment'):
        print(path, '->', value)
```

Field definitions live under `/data/config/nodes/<section>/nodes/<field>/` with
`type`, `comment`, `allowBlank` and similar. Shipped defaults live under
`/data/config/nodes/<section>/default`.

## Defaults Are Evidence

When a `comment` is ambiguous or absent, the **shipped defaults usually settle
the question**, because they encode the behaviour the platform intends.

Worked example: `config/wan/rules2` describes `priority` only as "Failover and
failback priority" with no direction. The defaults are ordered wired-before-
cellular and 5G-before-3G, which is only consistent with lower values being more
preferred. See [config/wan-rules2.md](config/wan-rules2.md).

This is stronger evidence than prose in a guide, including prose in these docs,
because the defaults are what the firmware actually ships.

## Semantics Are Per-Path, Not Global

Do not carry a field's meaning from one config section to another. The same field
name can have the opposite meaning elsewhere in the tree — `priority` runs in
both directions depending on the section, per the DTD's own comments. Always
check the comment and defaults for the exact path being written.

## Checklist Before Writing Any Config Path

1. Confirm the field exists at that path for this model and NCOS version.
2. Match the declared `type` exactly (boolean not string, `u32` not string).
3. Read the `comment` for semantics, and check `default` values when the comment
   is ambiguous.
4. Read the current value first. `None` means the path is absent on this
   firmware, and a blind write there cannot be detected afterwards.
5. Read the value back after writing. The router can accept a PUT and still
   reject the data.
