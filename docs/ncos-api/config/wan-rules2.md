# config/wan/rules2

WAN profiles that match and configure WAN devices. Each rule has a trigger pattern that matches device properties.

## Structure

List of rule objects. Each rule:

| Field | Type | Description |
|-------|------|-------------|
| `_id_` | uuid | Unique rule identifier |
| `trigger_name` | string | Human-readable name |
| `trigger_string` | string | Match pattern (e.g., `type\|is\|mdm%sim\|is\|sim1`) |
| `priority` | float | Failover/failback priority. **Lower value = more preferred** |
| `disabled` | boolean | **True = rule disabled, False/absent = enabled** |

## Priority Direction

**Lower numbers are more preferred.** The DTD describes this field only as
"Failover and failback priority" without stating a direction, but the shipped
defaults settle it — they are in NCOS's normal WAN preference order, wired ahead
of cellular and 5G ahead of 3G:

| `priority` | `trigger_name` |
|-----------|----------------|
| -10.012 | Wbond |
| 1 | Ethernet |
| 1.5 | 5G/LTE Multi-mode Modems |
| 2 | LTE-only Modems |
| 2.5 | LTE/3G Multi-mode Modems |
| 4 | WiFi as WAN |
| 5 | 3G-only Modems |

Consequences:

- Sorting rules ascending by `priority` lists them most-preferred first. This is
  what `cp.get_wan_profiles()` returns.
- To make a rule *more* preferred, **decrease** its priority. To demote a clone
  just below the rule it was copied from, add a small increment
  (`priority + 0.1`).
- Negative values are legal and outrank everything positive.
- Priority is not the same as enable/disable. A negative or very high priority
  still allows the device to be used; `disabled` prevents it entirely.

**Do not generalise this direction to other NCOS `priority` fields.** The
convention is not consistent across the config tree. Per the DTD comments:

| Path | Direction |
|------|-----------|
| `config/wan/rules2/<id>/priority` | Lower = more preferred (this page) |
| `config/wan/affinity/.../trigger_priority` | "lower numbers indicate higher priority" |
| `config/wan/rules/<id>/trigger_priority` | "low numbers indicate low priority" |
| `config/firewall/portfwd/<id>/priority` | "low numbers indicate low priority" |
| `config/firewall/portproxy/<id>/priority` | "low numbers indicate low priority" |
| `config/webfilter/filterlist/<id>/priority` | "low numbers indicate low priority" |
| `config/webfilter/macfilterlist/<id>/priority` | "low numbers indicate low priority" |

Check the DTD comment for the specific path before assuming a direction. Note
that `rules2` has no `trigger_priority` field; that belongs to the older
`config/wan/rules`.

## Disabling WAN Devices

**Use the `disabled` field to enable/disable WAN connections:**

```python
# Get device's rule ID from status
device = cp.get('status/wan/devices/mdm-41949674')
config_id = device.get('info', {}).get('config_id')

# Disable the rule
cp.put(f'config/wan/rules2/{config_id}/disabled', True)

# Enable the rule
cp.put(f'config/wan/rules2/{config_id}/disabled', False)
```

## Device-to-Rule Mapping

Each WAN device in `status/wan/devices/{device_id}/info` has a `config_id` field that references its matched rule's `_id_`:

```python
# Find which rule a device is using
devices = cp.get('status/wan/devices') or {}
for device_id, device_data in devices.items():
    config_id = device_data.get('info', {}).get('config_id')
    if config_id:
        rule = cp.get(f'config/wan/rules2/{config_id}')
        print(f"{device_id} uses rule: {rule.get('trigger_name')}")
```

## Common Patterns

### Disable specific modem

```python
# Get modem's config_id
modem = cp.get('status/wan/devices/mdm-41949674')
rule_id = modem.get('info', {}).get('config_id')

# Disable it
if rule_id:
    cp.put(f'config/wan/rules2/{rule_id}/disabled', True)
```

### Disable all modems

```python
rules = cp.get('config/wan/rules2') or []
for rule in rules:
    if 'type|is|mdm' in rule.get('trigger_string', ''):
        rule_id = rule.get('_id_')
        cp.put(f'config/wan/rules2/{rule_id}/disabled', True)
```

## Notes

- **Multiple devices can match one rule** (e.g., SIM1 and SIM2 both match a generic modem rule)
- **Disabled rules prevent device activation** - device won't connect even if plugged in
- **Priority only matters for enabled rules** - disabled rules are ignored regardless of priority
- **Negative priority is NOT the same as disabled** - negative priority affects failover order, disabled completely prevents use

## See Also

- [status/wan/devices/info](../status/wan/devices/info.md) - Device config_id field
- [config/wan/](README.md) - WAN configuration overview
