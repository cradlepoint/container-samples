# RTSP Viewer Container (go2rtc)

Turns one or more RTSP camera streams into a browser-viewable feed on an
Ericsson (Cradlepoint) NCOS router, using [go2rtc](https://github.com/AlexxIT/go2rtc)
as the media server. go2rtc handles the RTSP-to-WebRTC/MJPEG bridging, low-latency
browser playback, and a small web UI; this container just wires its config to
the deployment environment NCOS provides.

NCOS has no native RTSP viewing or transcoding capability, so this is a
straightforward gap-filler rather than a workaround for a defect in something
native.

## What It Does

```
RTSP camera(s)  -->  go2rtc  -->  Web UI / WebRTC :1984
                        |
                        +-->  RTSP relay :8554 (optional)
```

- **Web UI on 1984** to view any configured camera in a browser (WebRTC, with
  MJPEG/MSE fallback for streams the browser can't play natively).
- **Optional RTSP relay on 8554** to re-publish a stream to other RTSP clients
  on the network, e.g. an NVR that only pulls RTSP.
- **No Config Store integration.** This container does not use `cp.py` — it
  has nothing to read from or write to the router's config, it just serves
  video. There is accordingly no `$CONFIG_STORE` volume in the compose files.

## Files

| File | Purpose |
|------|---------|
| `Dockerfile` | Alpine base with `ffmpeg` and the `go2rtc` binary for the target architecture |
| `entrypoint.sh` | Generates `/config/go2rtc.yaml` from `CAMERA*_URL` env vars unless a config file is already present |
| `go2rtc.yaml` | Example config for local `docker compose` use, bind-mounted read-only |
| `docker-compose.yml` | Local build-and-run for development, with a bind-mounted config file |
| `docker-compose.cradlepoint.yml` | Deployment example for NCOS, configured entirely through `environment:` |

## Two Ways to Configure It

NCOS containers cannot bind-mount host files, so the same image supports two
configuration paths and picks between them at startup:

1. **Bind-mounted `go2rtc.yaml`** (`docker-compose.yml`, local development). If
   `/config/go2rtc.yaml` already has content when the container starts,
   `entrypoint.sh` leaves it alone and starts go2rtc directly.
2. **Environment variables** (`docker-compose.cradlepoint.yml`, NCOS). If no
   config file is present, `entrypoint.sh` generates one from `CAMERA<n>_URL` /
   `CAMERA<n>_NAME` pairs (n = 1..20), plus `WEBRTC_CANDIDATE` and the auth
   variables below.

Either way, camera credentials end up in the RTSP URL itself
(`rtsp://user:pass@host/path`), which is how most RTSP cameras authenticate.
There is currently no separate secret-handling path for those credentials —
they are only as protected as the compose environment they are set in.

## WEBRTC_CANDIDATE

go2rtc's WebRTC negotiation needs to know its own reachable address. Left
unset, it advertises the container's internal bridge IP (`172.17.0.x`), which
a browser outside the container can never reach — WebRTC playback then fails
silently while the web UI itself still loads. Set `WEBRTC_CANDIDATE` to the
router's LAN IP and the mapped WebRTC port (`<router-ip>:8555` by default).

## Security

**None of the published ports are authenticated unless you set the auth
variables below.** Mapped ports on NCOS are reachable on WAN as well as LAN,
with no firewall filtering in front of them, and this service streams live
video from a physical camera.

- Set `API_USERNAME` / `API_PASSWORD` and `RTSP_USERNAME` / `RTSP_PASSWORD` to
  enable Basic auth on the web UI/API and the RTSP relay respectively. Both
  pairs must be set together or the corresponding service stays open.
- **go2rtc always skips auth for calls it treats as coming from localhost**,
  even when a username/password is configured. This is go2rtc's own
  documented behavior, not a bug in this container, but it means auth alone
  does not make the port safe against anything already on the same network
  segment as the router's loopback (nothing normally is, but be aware Basic
  auth here is not a complete access-control story).
- For anything beyond a trusted network, the more robust option is to skip
  `ports:` entirely and attach the service to a Local IP Network instead, so
  it's reachable only from that LAN:

```yaml
services:
  rtsp-viewer:
    networks:
      container-lan:
        ipv4_address: 192.168.150.10
networks:
  container-lan:
    driver: bridge
    driver_opts:
      com.cradlepoint.network.bridge.uuid: <local-ip-network-uuid>
    ipam:
      driver: default
      config:
        - subnet: 192.168.150.0/24
          gateway: 192.168.150.1
```

- Basic auth is unencrypted over plain HTTP/RTSP. go2rtc supports TLS
  (`tls_listen`/`tls_cert`/`tls_key` under `api:`) if that matters for your
  deployment; this sample does not wire it up.

## Building

The Dockerfile picks the matching go2rtc release binary from `TARGETARCH`/
`TARGETVARIANT`, which `docker buildx` sets automatically from `--platform` —
there is one Dockerfile for both architectures, nothing to edit per build.

```bash
# ARMv8 64-bit: E300, E3000, R920, R980, R1900, R2100
docker buildx build --platform linux/arm64 -t yourregistry/rtsp-viewer:latest .

# ARMv7 32-bit: AER2200, IBR1700
docker buildx build --platform linux/arm/v7 -t yourregistry/rtsp-viewer:latest-armv7 .
```

Measured image sizes: **116 MB** for arm64, **75.9 MB** for arm/v7 — larger
than the other samples in this repo because of `ffmpeg` and its shared library
dependencies, which go2rtc uses for on-the-fly transcoding of codecs the
browser can't play natively. Idle memory use is under 25 MB; `mem_limit: 128M`
leaves headroom for transcoding, which is heavier than idle relay.

## Deployment

1. Build and push the image (see above). Do not point `image:` at a third
   party's Docker Hub repository — build this sample's own Dockerfile so the
   image matches what's in this repo.
2. In NCM, create a container project with `docker-compose.cradlepoint.yml` as
   a starting point, or write `config/container/projects` directly (see
   `docs/ncos-api/config/container.md`).
3. Set `CAMERA1_URL` (and `CAMERA2_URL`, etc.) to your camera's RTSP URL, and
   `WEBRTC_CANDIDATE` to the router's LAN IP.
4. Set the auth environment variables, or attach the service to a Local IP
   Network instead of publishing ports (see Security above).
5. Commit, then browse to `http://<router-ip>:1984/`.

No `$CONFIG_STORE` volume is needed — this container never talks to `cp.py`.

## Verified Before Deployment

Everything below was run on a development machine, with no router involved:

- Both architectures build (116 MB arm64, 75.9 MB arm/v7).
- Ran the arm64 image natively and confirmed the API, RTSP and WebRTC listeners
  all come up, and the web UI answers `200`.
- Bind-mounted-config path: an existing `go2rtc.yaml` is used as-is and
  `entrypoint.sh` does not touch it.
- Environment-variable path: multiple `CAMERA<n>_URL`/`CAMERA<n>_NAME` pairs
  and `WEBRTC_CANDIDATE` generate the expected YAML, confirmed by reading the
  file back inside the container.
- No env vars and no bind-mounted config: the container still starts with an
  empty `streams:` list rather than crashing, so the web UI is reachable to
  configure cameras through even from a bare deployment.
- Auth: with `API_USERNAME`/`API_PASSWORD` and `RTSP_USERNAME`/`RTSP_PASSWORD`
  set, an unauthenticated request to the published port gets `401` and an
  authenticated one gets `200` — and a request made from inside the container
  to `127.0.0.1` bypasses auth entirely, confirming go2rtc's documented
  localhost exemption applies here too.
- `docker stop` returns in well under a second, so go2rtc as PID 1 handles
  `SIGTERM` directly with no entrypoint shell in the way.

Not verifiable without a router: an actual camera feed, WebRTC playback
through the router's real WAN/LAN NAT, and whether `WEBRTC_CANDIDATE` needs
adjustment for a specific network's NAT behavior.

## Troubleshooting

| Symptom | Cause |
|---------|-------|
| Web UI loads but video never plays | `WEBRTC_CANDIDATE` unset or wrong — the browser is trying to reach the container's internal bridge IP |
| `401 Unauthorized` from the browser | Expected once `API_USERNAME`/`API_PASSWORD` are set; log in with those credentials |
| Camera never connects | Check the RTSP URL and credentials work from a client on the same network first, independent of this container |
| Stream is glitchy or won't play in-browser | Try `ffmpeg:` prefix on the source URL in `go2rtc.yaml` to force transcoding — see go2rtc's own README for source options |
