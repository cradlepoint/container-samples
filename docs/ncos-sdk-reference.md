# NCOS SDK Reference (cp.py)

This is a reference for the `cp.py` module used inside containers to communicate with the Cradlepoint router's Config Store via the Unix domain socket `/var/tmp/cs.sock`.

## Setup

To use `cp.py` in a container:

1. Copy `cp.py` into the container image
2. Mount the Config Store socket by enabling the "Config Store" volume option in the container project
3. Set `PYTHONPATH` to include the directory containing `cp.py`

### Dockerfile Example

```dockerfile
FROM alpine:3.18
RUN apk add --no-cache python3 py3-requests
COPY cp.py /opt/app/cp.py
ENV PYTHONPATH=/opt/app
```

### Compose Volume for Config Store

In the Compose Builder, under Volumes & Devices, enable the **Config Store** option. This exposes `cs.sock` at `/var/tmp/cs.sock` inside the container.

In raw Compose YAML, use the `$CONFIG_STORE` variable:

```yaml
services:
  my_service:
    volumes:
      - $CONFIG_STORE
```

The platform resolves `$CONFIG_STORE` automatically — do not append a mount target path.

## Core Functions

### Config Store Access

| Function | Description |
|----------|-------------|
| `cp.get(path, query='', tree=0)` | GET data from the router config/status tree |
| `cp.put(path, value='', query='', tree=0)` | PUT (update) data in the router tree |
| `cp.post(path, value='', query='')` | POST (create) data in the router tree |
| `cp.delete(path, query='')` | DELETE data from the router tree |
| `cp.decrypt(path, query='', tree=0)` | GET and decrypt encrypted data (NCOS only) |

### Logging and Alerts

| Function | Description |
|----------|-------------|
| `cp.log(value='')` | Write to syslog (NCOS) or stdout (container) or console (dev) |
| `cp.alert(value='')` | Send a custom alert to NCM (NCOS only) |

### AppData (Container Configuration)

AppData is the mechanism for passing configuration values from NetCloud Manager to a container at runtime.

| Function | Description |
|----------|-------------|
| `cp.get_appdata(name='')` | Get appdata value by name, or all appdata if no name |
| `cp.put_appdata(name='', value='')` | Update an appdata value |
| `cp.post_appdata(name='', value='')` | Create a new appdata entry |
| `cp.delete_appdata(name='')` | Delete an appdata entry |

### Device Information

| Function | Description |
|----------|-------------|
| `cp.get_mac(format_with_colons=False)` | Get device MAC address |
| `cp.get_serial_number()` | Get device serial number |
| `cp.get_product_type()` | Get device product type |
| `cp.get_name()` | Get device name |
| `cp.get_firmware_version(include_build_info=False)` | Get firmware version string |
| `cp.get_router_model()` | Get router model from product name |
| `cp.get_uptime()` | Get router uptime in seconds |

### Network and WAN

| Function | Description |
|----------|-------------|
| `cp.get_connected_wans(max_retries=10)` | List connected WAN UIDs |
| `cp.get_sims(max_retries=10)` | List modem UIDs with SIMs |
| `cp.get_wan_status()` | Detailed WAN status |
| `cp.get_ipv4_wired_clients()` | List IPv4 wired clients |
| `cp.get_ipv4_wifi_clients()` | List IPv4 Wi-Fi clients |
| `cp.get_ipv4_lan_clients()` | All IPv4 clients (wired + Wi-Fi) |

### GPS and Location

| Function | Description |
|----------|-------------|
| `cp.get_lat_long(max_retries=5, retry_delay=0.1)` | Get latitude/longitude as floats |
| `cp.get_gps_status()` | Detailed GPS status |
| `cp.dec(deg, min=0.0, sec=0.0)` | Convert DMS to decimal degrees |

### System Status

| Function | Description |
|----------|-------------|
| `cp.get_system_status()` | System status details |
| `cp.get_wlan_status()` | WLAN status details |
| `cp.get_ncm_status(include_details=False)` | NCM connection status |
| `cp.get_gpio(gpio_name=None, router_model=None)` | Read GPIO state |
| `cp.get_all_gpios(router_model=None)` | Raw GPIO structure |
| `cp.get_available_gpios(router_model=None)` | List available GPIO names |

### Startup Helpers

| Function | Description |
|----------|-------------|
| `cp.wait_for_uptime(min_uptime_seconds=60)` | Block until router uptime exceeds threshold |
| `cp.wait_for_ntp(timeout=300, check_interval=1)` | Block until NTP is synchronized |
| `cp.wait_for_wan_connection(timeout=300)` | Block until a WAN connection is active |

### Event Registration

| Function | Description |
|----------|-------------|
| `cp.register(action, path, callback, *args)` | Register callback for config store events |
| `cp.on(action, path, callback, *args)` | Alias for `register` |
| `cp.unregister(eid)` | Unregister a callback by event ID |

### Security and Certificates

| Function | Description |
|----------|-------------|
| `cp.get_ncm_api_keys()` | Get NCM API keys from certificate config |
| `cp.extract_cert_and_key(cert_name_or_uuid='')` | Extract cert and key to filesystem |

### Device Control

| Function | Description |
|----------|-------------|
| `cp.reboot_device(force=False)` | Reboot the router |

## Common Config/Status Paths

These are frequently used paths with `cp.get()`:

| Path | Returns |
|------|---------|
| `status/product_info` | Product info (model, MAC, etc.) |
| `status/fw_info` | Firmware version details |
| `status/system` | System status |
| `status/wan/devices` | WAN device status and stats |
| `status/lan` | LAN status and stats |
| `status/lan/clients` | Connected LAN clients |
| `status/ethernet` | Ethernet port status |
| `status/gps` | GPS data |
| `config/system/snmp` | SNMP configuration |
| `config/system/system_id` | System identifier |
| `config/vlan` | VLAN configuration |

## Usage Pattern

```python
import cp

# Wait for router to be ready
cp.wait_for_uptime(60)
cp.wait_for_wan_connection(timeout=120)

# Read configuration
snmp_config = cp.get('config/system/snmp') or {}
community = snmp_config.get('get_community', 'public')

# Read appdata (user-configured values from NCM)
my_setting = cp.get_appdata('my_setting') or 'default_value'

# Log messages
cp.log('Container started successfully')

# Write configuration
cp.put('config/some/path', 'new_value')
```
