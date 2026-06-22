# Memory Resources for NetCloud Container Orchestrator

Source: [Cradlepoint Docs](https://docs.cradlepoint.com/r/Adjusting-Memory-Resources-for-NetCloud-Container-Orchestrator)

Content was rephrased for compliance with licensing restrictions.

## Overview

Container memory is limited per router model to ensure key services (Wi-Fi, Analytics/IDS/IPS) have enough resources. If those services are not needed, they can be disabled to free memory for containers.

## Available Memory by Router

| Configuration              | AER2200 | IBR1700 | E300/E3000      | R1900   | R2100   | R920   | R980   |
|----------------------------|---------|---------|-----------------|---------|---------|--------|--------|
| No key services enabled    | 460 MB  | 460 MB  | 921 MB / 1.84 GB | 1.80 GB | 1.80 GB | 921 MB | 921 MB |
| All key services enabled   | 135 MB  | 135 MB  | 371 MB / 1.29 GB | 1.45 GB | 1.45 GB | 371 MB | 371 MB |
| Wi-Fi enabled only         | 260 MB  | 260 MB  | 621 MB / 1.54 GB | 1.66 GB | 1.66 GB | 621 MB | 621 MB |
| IDS/IPS enabled only       | 335 MB  | 335 MB  | 671 MB / 1.59 GB | 1.58 GB | 1.58 GB | 671 MB | 671 MB |

## Available Flash Storage

| AER2200 | IBR1700 | E300 | E3000 | R1900 | R2100 | R920 | R980 |
|---------|---------|------|-------|-------|-------|------|------|
| 6 GB    | 6 GB    | 6 GB | 14 GB | 6 GB  | 8 GB  | 8 GB | 8 GB |

## Disabling Key Services to Free Memory

### Disabling Wi-Fi

Disable all radios via CLI and reboot:

```
put/config/wlan/radio/0/enabled false
put/config/wlan/radio/1/enabled false
```

For IBR1700 only (has a third radio):
```
put/config/wlan/radio/2/enabled false
```

Reboot the router after disabling.

### Disabling IDS/IPS

First disable Analytics for the device in NetCloud Manager, then via CLI:

```
put/config/security/ips/mode "off"
```

Reboot the router after disabling.

## Key Takeaways for Container Development

- **AER2200/IBR1700**: Very constrained (135-460 MB). Use minimal base images (Alpine). Avoid heavy runtimes.
- **E300/R920/R980**: Moderate (371-921 MB). Suitable for most lightweight containers.
- **E3000/R1900/R2100**: Most capable (1.29-1.84 GB). Can run more complex workloads.
- **Flash storage**: 6-14 GB total. Keep container images small. Alpine-based images are strongly preferred.
- Always account for the router's other services when sizing your container.
