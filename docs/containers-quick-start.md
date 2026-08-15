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

### Deploying Without the UI

The compose YAML is stored as a plain string in `config/container/projects`, so a
project can also be created and updated over REST or the SDK — no UI clicking per
iteration. This is the faster loop when developing against a test router. See
[ncos-api/config/container.md](ncos-api/config/container.md).

### Before Debugging a Failed Deployment

Confirm the entitlement is actually present rather than assuming, since
Container Orchestration is licensed separately:

```python
features = cp.get('status/feature')   # or GET /api/status/feature
# db entries are [uuid, name, expires_days, remaining_days];
# look for "Container Orchestration"
```

## Configuring a Container Registry

By default, images are pulled anonymously from Docker Hub, which only works
against a **public** repository. A newly created Docker Hub repository is
private by default, so a fresh push there fails on the router with an
`unauthorized`/`denied` pull error until the repo's visibility is explicitly
set to Public — see the FAQ entry below for the exact symptom.

To use a private repository or a different registry:

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

- **Pull fails with `denied: requested access to the resource is denied` /
  `unauthorized: authentication required`?** The line just before these in
  `status/log`, `No matching registry auth information for url ...`, is logged
  on every anonymous pull attempt and is not itself diagnostic — it appears
  identically for a successful public-image pull. The `unauthorized`/`denied`
  pair is the actual failure, and Docker Hub returns the identical pair for
  several different causes, so check them in this order:

  1. **The `image:` value in the compose YAML does not exactly match what was
     pushed.** Compare the two strings character for character, including
     punctuation — `myimage-name` and `myimage_name` are different
     repositories to Docker Hub, and pushing one while deploying the other
     produces exactly this error with nothing router-specific about it. This
     is the most common cause in practice; rule it out before assuming a
     permissions problem.
  2. **The repository is private.** Docker Hub repositories default to
     **private** when first created — you have to explicitly set visibility
     to Public for an anonymous pull to work.
  3. **The repository or tag doesn't exist at all** (a typo, or it was never
     pushed).

  Docker Hub's error text does not distinguish any of these three from each
  other. Confirm which one by running `docker pull namespace/repo:tag` — using
  the exact string from the compose file — from a workstation while logged out
  of Docker Hub. The same failure there confirms it's a registry-side
  naming/visibility issue and not anything specific to the router. Fix by
  correcting the name, making the repository public, or adding credentials for
  it under [config/container/registry](ncos-api/config/container.md).

- **Project appears in `container list` with no containers under it?** This one
  symptom has three different causes, and they are distinguishable. Note first
  that `container list` itself reads project *config*, so it answers normally
  even when the engine is dead — its output is not evidence the engine is alive.

  | `status/container` | `containers` lines in `status/log` | Diagnosis |
  |--------------------|-----------------------------------|-----------|
  | Returns data | Present, active | Image pull still in progress |
  | **Hangs / times out** | Present, with `daemon is not responding ... DeadlineExceeded` | Engine wedged. A reboot clears it |
  | **Returns `null` promptly** | **None at all** | Engine not running. Nothing is even attempting the pull |

  Absence of log lines from a facility is a positive signal, not a lack of
  information: count facilities in `status/log` to see which subsystems are
  alive. Check all of this before re-reading your own compose file — when the
  platform is the variable, debugging your artifact wastes time.

  Also note the engine logs `High system CPU load?` as a *guess* whenever a call
  times out. Verify against `status/system` (`cpu`, `load_avg`, `memory`) before
  treating load as the cause, because that message appears on an idle router.

- **Container subsystem missing after a firmware upgrade?** Project config in
  `config/container/projects` and the `status/feature` entitlement can both
  survive an upgrade while the engine does not come back. Verify the subsystem is
  alive before concluding your deployment is at fault. (Cause UNVERIFIED. The
  account-level toggle under Tools > Container Orchestration, which triggers the
  runtime download, is the first thing worth re-checking, but this has not been
  confirmed as the fix.)
- **Can Docker volumes be pruned?** No, volumes on routers cannot be pruned.
- **User namespace remapping?** Yes, user namespace remapping is employed. See file ownership caveat above.
- **Volume not updated with new image?** Volumes are not updated from new images. Create a new project to get a fresh volume.
- **Data usage mismatch?** Container usage is measured at Layer 2 (Ethernet) while client usage is Layer 3 (IP), causing a ~14-byte-per-packet difference.
