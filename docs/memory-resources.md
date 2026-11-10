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

## Measured Image Size Reference

Useful for planning against the memory floor and the 6 GB flash on the smallest
models. Measured with `docker image ls` on Alpine 3.18 containing `python3`, one
small network daemon and its client tools, plus `cp.py` and application source:

| Platform | Image size | Installed packages |
|----------|-----------|--------------------|
| `linux/arm64` | 58.1 MB | 59 MiB (34 packages) |
| `linux/arm/v7` | 45.6 MB | 47 MiB (34 packages) |

Strip the daemon and its client tools and you reach the floor for this stack:
Alpine 3.18 with `python3`, `cp.py` and a polling application measures **53.5 MB
on `linux/arm64` and 42.1 MB on `linux/arm/v7`**, with a 7.0 MiB resident set at
idle by local `docker stats`. Most of that image is `python3` and its
dependencies, so an app-only sample has little room left to trim.

An Alpine plus Python container of this shape keeps its resident set well under
64 MB. Do **not** cap it with a `mem_limit` in Compose — on NCOS that is reported
to cause containers to fail with unknown errors (device model and firmware not
captured). Control memory by building minimal images against the per-model budget
above, not by setting a limit. Treat these sizes as a baseline: adding compiled
Python wheels
(numpy, opencv, tflite, onnxruntime) moves an image into the hundreds of
megabytes (measured below at 361 MB for one such stack), which is
what makes the AER2200 and IBR1700 floor of 135 MB the real constraint for
heavier workloads.

### Image size and memory use are different numbers

Image size is a **flash** cost (and a pull-time cost over a possibly metered
WAN). Resident memory is what competes with router services for the allowance in
the table above, and the two can be orders of magnitude apart. Measured in one
run: a Debian 12-slim based image carrying a network daemon plus its optional
plugin packages came to 121-132 MB on `linux/arm64` and 69 MB on `linux/arm/v7`,
while the daemon's resident set with a session established was **4.4 MiB**.

So a large image does not by itself rule a design out on memory grounds — report
both numbers, from `docker image ls` and `docker stats` respectively, and judge
each against the constraint it actually consumes. A non-Alpine base is still worth
avoiding where Alpine can do the job, but the reason is flash and pull time, not
the memory floor.

A second measurement at the heavy end, for the computer-vision workload class:

| Platform | Image size | Local `docker stats` | On-router `memory_stats.usage` |
|----------|-----------|----------------------|--------------------------------|
| `linux/arm64` | 361 MB | 105–118 MiB | 451.7 MiB |

That is `python:3.12-slim` plus `numpy`, `opencv-python-headless` and
`onnxruntime`, carrying 32 MB of ONNX models, running a two-stage video pipeline
(detector then a second model on each crop). The on-router figure comes from
`status/container`'s `stats.memory_stats.usage` on an **R2400-5GF-NA running
NCOS 7.26.81**.

**The two columns are different measurements and the gap is 4x.** `docker stats`
reports a cache-excluded working set; `memory_stats.usage` includes page cache,
and a pipeline reading large video files generates a lot of it. Sizing a
deployment against the local number is how a workload that "fits" turns out not
to. Read `memory_stats.stats.rss` (or subtract `.stats.cache`) from the
on-router payload for the comparable figure — which of the two to budget
against here is **UNVERIFIED**, because that breakdown was not captured.

The image size is the firmer constraint either way: it is three times the whole
container allowance of an AER2200 and awkward against its flash budget. Flash
and pull time are the binding
constraints for this class of workload, not memory.

Note also that this stack is **arm64 only**, because neither `onnxruntime` nor
`opencv-python-headless` publishes an `armv7l` wheel — and Alpine is not an
option for it either, because neither publishes a `musllinux` wheel. Check both
tags per pinned version before choosing a base image; the method is in
[container-development-guide.md](container-development-guide.md) under "Use
Alpine Base Images".

## Key Takeaways for Container Development

- **AER2200/IBR1700**: Very constrained (135-460 MB). Use minimal base images (Alpine). Avoid heavy runtimes.
- **E300/R920/R980**: Moderate (371-921 MB). Suitable for most lightweight containers.
- **E3000/R1900/R2100**: Most capable (1.29-1.84 GB). Can run more complex workloads.
- **Flash storage**: 6-14 GB total. Keep container images small. Alpine-based images are strongly preferred.
- Always account for the router's other services when sizing your container.
