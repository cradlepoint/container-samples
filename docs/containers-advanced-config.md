# Containers Advanced Configuration Guide

Source: [Cradlepoint Docs](https://docs.cradlepoint.com/r/Containers-Advanced-Configuration-Guide)

Content was rephrased for compliance with licensing restrictions.

## Overview

This document covers advanced container features: networking, volumes, USB serial ports, USB storage, and health checks.

## Networks

> Requires NetCloud OS 7.2.50 or later.

By default, Docker Compose assigns IPs starting from `172.17.0.2` in the Docker LAN subnet. You can configure static or DHCP-assigned IPs on custom subnets.

### Creating a Custom Container Network

1. In NetCloud Manager, navigate to NETWORKING > Local Networks > Local IP Networks
2. Add a new network (e.g., "Container Net" on `10.99.99.0/24`)
3. Optionally configure a DHCP range (e.g., `10.99.99.2` - `10.99.99.9`)
4. In SYSTEM > Containers > Projects, add the network to the project
5. Under Services, assign the network to a service
6. For static IPs, enter the address in the service config (reserve it in DHCP to avoid conflicts)

### Compose YAML for Custom Networks

When the Compose Builder adds a custom network to a project, it emits a `networks` block that binds each Compose network to an NCM Local IP Network via the `com.cradlepoint.network.bridge.uuid` driver opt. The UUID must match an existing Local IP Network on the router, and the `subnet`/`gateway` must match that network's configuration.

```yaml
version: '2.4'
services:
  app:
    image: alpine:3.18
    command: sleep infinity
    networks:
      - lan1
      - lan2
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

The recommended path is to let the Compose Builder generate this YAML after adding the networks in NCM, rather than hand-writing UUIDs.

## Volumes

Containers run in a protected namespace and cannot access the host filesystem. Data sharing options:

- **Shared volumes between containers**: Mount the same named volume in multiple services
- **SSH/FTP service in container**: Transfer files from external clients
- **Second container with FTP**: Mount a shared volume between containers

### Volume Configuration

In the Compose Builder under Volumes & Devices:
- Add a named volume (e.g., `shared-data`)
- Map it to a path in the container (e.g., `shared-data:/var/tmp`)

### Compose YAML Example

```yaml
version: '2.4'
services:
  redis1:
    image: redis:alpine
    volumes:
      - shared-data:/var/tmp
  redis2:
    image: redis:alpine
    volumes:
      - shared-data:/var/tmp
volumes:
  shared-data:
```

### Volume Options in Compose Builder

- **Config Store**: Exposes `cs.sock` so the container can communicate with the router's Config Store
- **USB Storage**: Allows containers to use USB storage devices

### Important Volume Notes

- **No host filesystem mounts**: For security, volumes cannot mount to the host NetCloud OS filesystem
- **Volume data persistence**: Data on a volume is NOT updated when a new image is deployed. Create a new project to get fresh volume data.
- **Devices**: USB and serial ports can be mounted via the Devices section

## Mapping a USB Serial Port

For Out-of-Band Management (OOBM):

1. Navigate to System > Containers > Projects > Volumes & Devices
2. Select the USB Serial Port from the Device drop-down
3. Save

## USB Storage for Containers

> Requires NetCloud OS 7.23.20 or later.

1. Navigate to SYSTEM > Containers > Projects
2. Edit a service > Volumes & Devices
3. Select "USB Storage" from the Volumes drop-down
4. The container mounts the USB device at `/var/media`

### USB Storage Considerations

- If multiple containers in a project use USB, all restart on plug/unplug
- If only one container uses USB, only that container restarts
- Multiple containers can simultaneously use USB storage
- FAT32 filesystem: max partition 32GB, max file 4GB
- Only one USB storage device supported at a time (no USB hub)
- Avoid writing NetCloud OS logs to USB while containers use it
- Rapid plug/unplug may cause sync issues

## Health Check

Health checks monitor container applications and restart on failure.

### Configuration Fields

| Field     | Description                                                    |
|-----------|----------------------------------------------------------------|
| Test      | Command executed inside the container (exit 0 = healthy)       |
| Interval  | Duration between test executions                               |
| Retries   | Number of failures before container is considered unhealthy    |
| Timeout   | Max time for test execution before it's considered failed      |
| Condition | Under what condition the container should be restarted         |

### Example Health Check

```yaml
services:
  web:
    image: my-web-app
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost", "||", "exit", "1"]
      interval: 30s
      timeout: 10s
      retries: 3
```
