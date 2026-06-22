# USB Audio Support for Containers

Source: [Cradlepoint Docs](https://docs.cradlepoint.com/r/USB-Audio-Support-for-Containers)

Content was rephrased for compliance with licensing restrictions.

## Overview

USB audio support on Ericsson Cradlepoint routers is available in containers starting with NetCloud OS 7.25.20. This allows containerized applications to interact with external USB audio devices (microphones, headphones, etc.) and react to audio commands.

## Prerequisites

- A USB audio device (microphone, headphones, etc.)
- An Ericsson Cradlepoint router with NetCloud OS 7.25.20+ that supports Container Orchestrator
- A Docker container built with audio support

## Example Dockerfile

```dockerfile
FROM alpine:3.14
WORKDIR /app
RUN apk add --no-cache alsa-utils
COPY audio-file.wav /app
CMD ["aplay", "audio-file.wav"]
```

## Configuration

### Compose YAML

The key configuration is mapping the host sound device into the container:

```yaml
version: '3'
services:
  container:
    image: username/image:tag
    devices:
      - /dev/snd:/dev/snd
```

### Setup Steps

1. In NetCloud Manager, navigate to SYSTEM > Containers > Projects
2. Add or edit a project, enable it
3. Under Compose Builder, add a service with the image name
4. Use Bridge for Network Mode
5. In the Compose tab, add the `devices: - /dev/snd:/dev/snd` mapping
6. Save and commit

### Important Notes

- If multiple containers in a project use the USB device, all restart on plug/unplug
- If only one container uses it, only that container restarts on plug/unplug
