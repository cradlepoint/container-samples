# Container Development Guide for Cradlepoint Routers

This is a practical guide for building Docker containers that run on Ericsson (Cradlepoint) NCOS routers via the NetCloud Container Orchestrator.

## Architecture Constraints

### Target Architectures

| Router           | Architecture     | Docker Platform Flag   |
|------------------|------------------|------------------------|
| AER2200, IBR1700 | ARMv7 32-bit     | `linux/arm/v7`         |
| E300, E3000, R920, R980, R1900, R2100 | ARMv8 64-bit | `linux/arm64` |

Build images for the correct architecture. Use `docker buildx` for cross-compilation:

```bash
# For ARMv8 64-bit routers
docker buildx build --platform linux/arm64 -t myimage:latest .

# For ARMv7 32-bit routers
docker buildx build --platform linux/arm/v7 -t myimage:latest .
```

### Memory Limits

Containers share memory with router services. Keep images and runtime footprint small.

- **Smallest routers** (AER2200, IBR1700): 135-460 MB available
- **Mid-range** (E300, R920, R980): 371-921 MB available
- **Largest** (E3000, R1900, R2100): up to 1.84 GB available

See [memory-resources.md](memory-resources.md) for full details.

### Flash Storage

Container images are stored in flash: 6-14 GB depending on model. Keep images small.

## Dockerfile Best Practices

### Use Alpine Base Images

Alpine Linux is the preferred base for NCOS containers due to its small footprint:

```dockerfile
FROM alpine:3.18
RUN apk add --no-cache python3 py3-requests
```

### Keep Images Minimal

- Use `--no-cache` with `apk add` to avoid caching package indexes
- Combine RUN commands to reduce layers
- Remove unnecessary build dependencies after compilation
- Do not install documentation or man pages

### Entrypoint Pattern

Use an entrypoint script for initialization:

```dockerfile
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
```

### Python Applications

For Python-based containers:

```dockerfile
FROM alpine:3.18
RUN apk add --no-cache python3 py3-requests
COPY cp.py /opt/app/cp.py
COPY my_app.py /opt/app/my_app.py
ENV PYTHONPATH=/opt/app
CMD ["python3", "/opt/app/my_app.py"]
```

## Compose YAML

Containers are deployed via Docker Compose version `2.4`:

```yaml
version: '2.4'
services:
  my_service:
    network_mode: bridge
    image: 'myregistry/myimage:latest'
    ports:
      - '8080:8080'
    restart: always
```

### Key Compose Options

- **network_mode**: Usually `bridge`. Custom networks available with NCOS 7.2.50+.
- **ports**: `host_port:container_port` mapping
- **volumes**: Named volumes for data sharing between containers
- **devices**: Map host devices (USB serial, USB audio) into the container
- **restart**: Use `always` or `unless-stopped` for production containers
- **logging**: Use `json-file` driver for log access via `container logs`

## Communicating with the Router (Config Store)

### Enabling Config Store Access

In the Compose Builder under Volumes & Devices, enable the **Config Store** option. This mounts `/var/tmp/cs.sock` into the container.

In raw Compose YAML, use the `$CONFIG_STORE` variable:

```yaml
services:
  my_service:
    volumes:
      - $CONFIG_STORE
```

The platform resolves `$CONFIG_STORE` and handles the mount path automatically — do not append `:/var/tmp` or any other target path.

### Using cp.py

Copy `cp.py` into your container and set `PYTHONPATH`:

```python
import cp

# Read router status
product_info = cp.get('status/product_info')
cp.log(f"Running on {product_info.get('product_name')}")

# Read user-configured appdata
my_config = cp.get_appdata('my_setting') or 'default'

# Wait for router readiness at startup
cp.wait_for_uptime(60)
cp.wait_for_wan_connection(timeout=120)
```

See [ncos-sdk-reference.md](ncos-sdk-reference.md) for the full API.

## Networking

### Default Bridge Network

By default, containers get IPs from `172.17.0.0/16`. The router is the gateway.

### Custom Networks (NCOS 7.2.50+)

Create dedicated subnets for containers via NETWORKING > Local Networks in NCM. Supports static IP or DHCP assignment.

### Exposing Ports

Use port mapping in Compose to expose container services:

```yaml
ports:
  - '1161:1161/udp'    # UDP port
  - '8080:8080'        # TCP port (default)
```

### Reaching the Router from a Container

The router IP is the default gateway of the container's bridge network. To discover it programmatically:

```python
def get_router_ip():
    with open('/proc/net/route', 'r') as f:
        for line in f:
            parts = line.strip().split()
            if len(parts) >= 3 and parts[1] == '00000000':
                gw_hex = parts[2]
                gw_bytes = bytes.fromhex(gw_hex)
                return '%d.%d.%d.%d' % (gw_bytes[3], gw_bytes[2], gw_bytes[1], gw_bytes[0])
    return None
```

## Volumes and Storage

### Named Volumes (Container-to-Container)

```yaml
volumes:
  shared-data:

services:
  app1:
    volumes:
      - shared-data:/data
  app2:
    volumes:
      - shared-data:/data
```

### USB Storage (NCOS 7.23.20+)

Enable USB Storage in Volumes & Devices. Mounts at `/var/media`. FAT32 only (32GB max partition, 4GB max file).

### No Host Filesystem Access

For security, containers cannot mount the host NetCloud OS filesystem. Only named volumes, Config Store, and USB storage are available.

## Device Access

### USB Serial Port

Map via Volumes & Devices > Devices section for Out-of-Band Management.

### USB Audio (NCOS 7.25.20+)

```yaml
devices:
  - /dev/snd:/dev/snd
```

## Health Checks

```yaml
services:
  web:
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/health"]
      interval: 30s
      timeout: 10s
      retries: 3
```

Exit code 0 = healthy, non-zero = unhealthy. After `retries` failures, the container is restarted.

## Security Considerations

- Containers run in a protected namespace with user namespace remapping
- No root access to NetCloud OS
- File ownership changes to `nobody:nobody` when replacing base image files (use copy-then-move workaround)
- Config Store access must be explicitly enabled per container

## Troubleshooting

### CLI Commands

```bash
container list                          # List all containers
container logs <container_name>         # View logs
container exec <container_name> sh      # Shell into container
cat /status/container/<project>/info    # Container info
```

### Common Issues

- **Container won't start**: Check image architecture matches router (ARMv7 vs ARMv8)
- **Out of memory**: Check available memory for the router model, disable unused services
- **Can't reach router API**: Ensure Config Store volume is enabled
- **File permission issues**: Use the copy-then-move workaround for base image files
- **Image too large for flash**: Reduce image size, use Alpine, minimize layers
