# Running Containers on Ericsson Routers

## Platform Overview

Ericsson routers run containers on a Linux ARM64 (aarch64) platform using musl libc. Containers must be built for this target architecture and C library to function correctly.

### Target Architecture

- **Architecture:** ARM64 / aarch64
- **C Library:** musl libc (not glibc)
- **OS:** Linux

When building container images, ensure your base image and all compiled binaries target `linux/arm64` with musl libc. Alpine Linux is a common base image choice since it uses musl natively.

## Container Configuration

### Networking

Containers use bridge networking by default. Each container gets its own isolated network namespace with a virtual bridge interface. To expose services externally, use port mappings or assign the container an IP on a LAN network.

#### Default Bridge Network

With the default bridge, the container receives an IP in the Docker internal subnet (starting at `172.17.0.2`). Services are reached via port mappings on the router's IP.

#### Placing a Container on a LAN IP

To give a container its own IP address on a Local IP Network (LAN), define a custom Compose network that binds to an existing Local IP Network via its UUID. The container then appears as a distinct host on that LAN — reachable directly by IP without port mapping.

1. Create a Local IP Network in NetCloud Manager (e.g., `192.168.150.0/24`)
2. Find the network's UUID (see below)
3. Reference it in the Compose YAML under `driver_opts`
4. Optionally assign a static IP to the service

#### Finding the LAN UUID

**From NetCloud Manager (Configuration):**

In NCM, pull the device's configuration (JSON). The `lan` dictionary is keyed by UUID — each key is a Local IP Network UUID:

```json
{
  "lan": {
    "00000002-0d93-319d-8220-4a1fb0372b51": {
      "ip_address": "192.168.150.1",
      "netmask": "255.255.255.0",
      ...
    },
    "00000000-0d93-319d-8220-4a1fb0372b51": {
      "ip_address": "192.168.250.1",
      "netmask": "255.255.255.0",
      ...
    }
  }
}
```

**From the local router NCOS CLI:**

Connect to the router CLI (SSH or console) and query the Config Store:

```
get config/lan
```

This returns the list of Local IP Networks. Each network object is keyed by its UUID:

```
config/lan/00000002-0d93-319d-8220-4a1fb0372b51
config/lan/00000000-0d93-319d-8220-4a1fb0372b51
```

To inspect a specific network's details:

```
get config/lan/00000002-0d93-319d-8220-4a1fb0372b51
```

The output includes `ip_address`, `netmask`, and other settings you'll need to match in the Compose YAML's `ipam` config.

```yaml
version: '2.4'
services:
  my-service:
    image: my-image:latest
    networks:
      container-lan:
        ipv4_address: 192.168.150.10

networks:
  container-lan:
    driver: bridge
    driver_opts:
      com.cradlepoint.network.bridge.uuid: 00000002-0d93-319d-8220-4a1fb0372b51
    ipam:
      driver: default
      config:
        - subnet: 192.168.150.0/24
          gateway: 192.168.150.1
```

A service can attach to multiple LAN networks by listing them under its `networks:` key:

```yaml
version: '2.4'
services:
  my-service:
    image: my-image:latest
    networks:
      lan1:
        ipv4_address: 192.168.150.10
      lan2:
        ipv4_address: 192.168.250.10

networks:
  lan1:
    driver: bridge
    driver_opts:
      com.cradlepoint.network.bridge.uuid: 00000002-0d93-319d-8220-4a1fb0372b51
    ipam:
      driver: default
      config:
        - subnet: 192.168.150.0/24
          gateway: 192.168.150.1
  lan2:
    driver: bridge
    driver_opts:
      com.cradlepoint.network.bridge.uuid: 00000000-0d93-319d-8220-4a1fb0372b51
    ipam:
      driver: default
      config:
        - subnet: 192.168.250.0/24
          gateway: 192.168.250.1
```

The recommended workflow is to let the Compose Builder in NetCloud Manager generate the network YAML after adding networks in the UI, rather than hand-writing UUIDs.

### Ports

When using the default bridge network, ports must be explicitly mapped between the host and the container:

- Map specific TCP or UDP ports from the host to the container
- Both the host port and container port must be specified
- Multiple port mappings can be defined per container

**Important:** Mapped ports are exposed on all LAN and WAN interfaces, and the router firewall does not block them. This is not recommended for secure services — use a LAN IP network assignment instead to control which interfaces the service is reachable on.

```yaml
services:
  my-service:
    image: my-image:latest
    ports:
      - "8080:80"        # host:container TCP
      - "8443:443"       # HTTPS
      - "5000:5000/udp"  # UDP port
      - "9090:9090/tcp"  # Explicit TCP
```

### Volumes

Data persistence is achieved through named volumes. The router does not allow host filesystem mounts — only named volumes, Config Store, and USB storage are available.

- Named volumes are shared between containers in the same project
- Data in named volumes persists across container restarts
- Volume data is NOT updated when a new image is deployed (create a new project for fresh volumes)
- Be mindful of available storage space on the router

```yaml
version: '2.4'
services:
  my-service:
    image: my-image:latest
    volumes:
      - shared-data:/var/tmp       # Named volume
      - $CONFIG_STORE              # Config Store access (bare, no mount path)

volumes:
  shared-data:
    driver: local
```

For USB storage (requires NCOS 7.23.20+), add `$USB_STORAGE` as a volume. The device mounts at `/var/media` inside the container.

### Devices

Host devices can be passed through to containers when hardware access is required:

- Serial ports (e.g., `/dev/ttyUSB0`)
- USB devices
- Other character or block devices available on the host

Device passthrough gives the container direct access to the hardware, so appropriate permissions must be configured.

```yaml
services:
  my-service:
    image: my-image:latest
    devices:
      - "/dev/ttyUSB0:/dev/ttyUSB0"
      - "/dev/ttyS0:/dev/ttyS0"
      - "/dev/snd:/dev/snd"
```

### Environment Variables

Environment variables can be set at container launch to configure application behavior without modifying the image. This is useful for:

- API endpoints and credentials
- Feature flags
- Runtime configuration that varies between deployments

```yaml
services:
  my-service:
    image: my-image:latest
    environment:
      - API_ENDPOINT=https://api.example.com
      - LOG_LEVEL=info
      - FEATURE_FLAG_ENABLED=true
      - TZ=UTC
```

### Resource Constraints

Router hardware has limited CPU and memory compared to server environments. Consider:

- **Memory limits** — Set appropriate memory caps to prevent a container from exhausting system resources
- **CPU limits** — Restrict CPU usage to leave headroom for router operations
- **Restart policies** — Configure automatic restart behavior for fault tolerance

```yaml
version: '2.4'
services:
  my-service:
    image: my-image:latest
    mem_limit: 128M
    restart: unless-stopped
```

### Restart Policies

```yaml
services:
  my-service:
    image: my-image:latest
    restart: unless-stopped   # Restart on failure, not on manual stop

  my-critical-service:
    image: my-image:latest
    restart: always           # Always restart regardless of exit status

  my-oneshot-task:
    image: my-image:latest
    restart: on-failure       # Only restart if exit code is non-zero
```

## Build Considerations

- Always cross-compile or build on an ARM64 environment
- Verify that all dependencies are available for musl libc (some libraries assume glibc)
- Keep images minimal to conserve storage and reduce attack surface
- Statically linked binaries avoid C library compatibility issues entirely
- Multi-stage builds help reduce final image size

Build images locally or in CI targeting `linux/arm64`, then push to a registry. The router pulls pre-built images only — `build:` directives in Compose are not supported on the device.

## Container Lifecycle

Containers on routers should be designed to:

- Start automatically on boot
- Handle restarts gracefully
- Recover from unexpected shutdowns
- Log output to accessible locations for troubleshooting

```yaml
services:
  my-service:
    image: my-image:latest
    restart: unless-stopped
    logging:
      driver: json-file
```

## Full Compose Example

A complete `docker-compose.yml` combining all concepts:

```yaml
version: '2.4'
services:
  my-service:
    image: my-user/my-image:latest
    networks:
      container-lan:
        ipv4_address: 192.168.150.10
    volumes:
      - app-storage:/app/storage
      - $CONFIG_STORE
    devices:
      - "/dev/ttyUSB0:/dev/ttyUSB0"
    environment:
      - API_ENDPOINT=https://api.example.com
      - LOG_LEVEL=info
    mem_limit: 128M
    restart: unless-stopped
    logging:
      driver: json-file

networks:
  container-lan:
    driver: bridge
    driver_opts:
      com.cradlepoint.network.bridge.uuid: 00000002-0d93-319d-8220-4a1fb0372b51
    ipam:
      driver: default
      config:
        - subnet: 192.168.150.0/24
          gateway: 192.168.150.1

volumes:
  app-storage:
    driver: local
```
