# Containers Quick Start Guide

Source: [Cradlepoint Docs](https://docs.cradlepoint.com/r/Containers-Quick-Start-Guide)

Content was rephrased for compliance with licensing restrictions.

## Overview

The NetCloud Container Orchestrator enables lightweight applications to run inside secure, isolated containers on Ericsson (Cradlepoint) router endpoints. It supports OCI-compatible Docker container workloads from any Docker container registry (Docker Hub, Amazon ECR, etc.) and uses Docker Compose (YAML) for defining multi-container applications.

## Minimum Requirements

- **NetCloud OS**: version 7.2.20 or later
- **Advanced license** required on the router
- Sufficient memory for the Container Orchestrator (see [memory-resources.md](memory-resources.md))

### Supported Routers

| Router   | Architecture    |
|----------|-----------------|
| AER2200  | ARMv7 32-bit    |
| IBR1700  | ARMv7 32-bit    |
| E300     | ARMv8 64-bit    |
| E3000    | ARMv8 64-bit    |
| R920     | ARMv8 64-bit    |
| R980     | ARMv8 64-bit    |
| R1900    | ARMv8 64-bit    |
| R2100    | ARMv8 64-bit    |

**Important**: When building container images, the target architecture must match the router. Use multi-arch builds or build specifically for `linux/arm/v7` (ARMv7) or `linux/arm64` (ARMv8).

## Enabling the Container Orchestrator

The NetCloud Container Orchestrator service must be enabled once per account in NetCloud Manager:

1. Log into NetCloud Manager
2. Navigate to Tools > Container Orchestration
3. Toggle "Enable NetCloud Container Orchestrator" on

## Deploying a Container

Containers are deployed via NetCloud Manager at the device or group level.

### Compose YAML Format

Containers use Docker Compose version 2.4 format:

```yaml
version: '2.4'
services:
  my_service:
    network_mode: bridge
    image: 'redis:alpine'
    ports:
      - '6379:6379'
```

### Deployment Steps

1. Log into NetCloud Manager
2. Navigate to Devices > select router > Configuration > Edit
3. Go to SYSTEM > Containers > Projects
4. Click Add to create a new project
5. Enter project name, enable it
6. (Optional) Set an Update Interval in seconds for automatic image updates
7. Under Compose Builder, add services with name, image, network mode, and port mappings
8. Port mapping syntax: `host_port:container_port`
9. Save and Commit Changes

After committing, the router automatically:
- Syncs container changes
- Downloads and installs the container runtime
- Pulls the container image(s)

## Configuring a Container Registry

By default, images are pulled from Docker Hub. To use a different registry:

1. Navigate to SYSTEM > Containers > Registry
2. Add the registry URL and credentials
3. For Amazon ECR: username is `AWS`, password is the ECR authorization token

## Verifying Containers

### Via NetCloud Manager
- Navigate to Devices > select device > Containers tab
- Check container state is "running"
- View CPU/Memory usage

### Via CLI Console
```bash
# List containers
container list

# View container info
cat /status/container/<project_name>/info

# View container logs
container logs <container_name>

# Execute command in container
container exec <container_name> sh
```

## Logging

Add logging to the Compose YAML to enable container logs:

```yaml
services:
  my_service:
    image: 'my_image:tag'
    logging:
      driver: json-file
```

View logs with: `container logs <container_name>`

## File Ownership Caveat

When replacing a file from a container's base image, ownership changes to `nobody:nobody` and becomes locked. Workaround:

```bash
cp main.py main_copy.py
# edit main_copy.py
mv main_copy.py main.py
```

## FAQ

- **Can Docker volumes be pruned?** No, volumes on routers cannot be pruned.
- **User namespace remapping?** Yes, user namespace remapping is employed. See file ownership caveat above.
- **Volume not updated with new image?** Volumes are not updated from new images. Create a new project to get a fresh volume.
- **Data usage mismatch?** Container usage is measured at Layer 2 (Ethernet) while client usage is Layer 3 (IP), causing a ~14-byte-per-packet difference.
