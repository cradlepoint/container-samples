# gpsd Server Container

Turns an Ericsson (Cradlepoint) NCOS router into a standard **gpsd** server, with
a web map showing current position and track history.

NCOS can already stream raw NMEA or TAIP sentences to a remote server or a local
port. What it does not do is speak the gpsd JSON protocol, which is what most
Linux GPS consumers actually expect: `chrony`, Kismet, navit, ROS nodes, the
Python `gps` module, and a long tail of telematics and AVL software. Without this
container each of those needs custom NMEA parsing against the router's stream.
With it, they get a normal gpsd endpoint on port 2947, and gpsd handles fix
validation, staleness and multi-client fan-out.

## What It Does

```
cp.get('status/gps/fix')  -->  nmea_server  -->  gpsd  -->  LAN clients :2947
   (Config Store poll)      127.0.0.1:10110
          |
          +--> track history --> web UI map :8080
```

- **gpsd on 2947** for any standard GPS client on the LAN.
- **Web UI on 8080** with a live map, position marker and breadcrumb track.
- **Honest fix state.** When the router has no lock, or a fix goes stale, the
  NMEA output says so (`GGA` quality 0, `RMC` status V) instead of repeating the
  last known position. gpsd then reports `mode: 1` and clients behave correctly.
  The UI still shows the last known position, clearly marked stale.

## Files

| File | Purpose |
|------|---------|
| `Dockerfile` | Alpine base with `gpsd`, `gpsd-clients`, `python3` |
| `entrypoint.sh` | Reads appdata, starts the Python adapter, then gpsd, and supervises both |
| `docker-compose.yml` | Deployment example with ports, volumes and a health check |
| `src/gen_conf.py` | Writes port values to a shell env file for the entrypoint |
| `src/main.py` | Thread orchestration, shutdown, Config Store diagnostics |
| `src/config.py` | Appdata configuration with validation and self-provisioning defaults |
| `src/gps_source.py` | Config Store poller, DMS to decimal conversion, staleness |
| `src/nmea.py` | NMEA 0183 sentence synthesis (RMC, GGA, VTG) with checksums |
| `src/nmea_server.py` | Loopback TCP feed that gpsd attaches to |
| `src/history.py` | Bounded breadcrumb track with atomic snapshots to a volume |
| `src/web_server.py` | Standard-library HTTP server: UI plus JSON API |
| `src/models.py` | Dataclasses: `Fix`, `TrackPoint` |
| `src/static/js/map.js` | Dependency-free slippy map (tiles plus canvas overlay) |
| `src/static/js/app.js` | UI controller |
| `cp.py` | NCOS SDK module |

## Why Config Store Polling

The adapter polls `status/gps/fix` rather than consuming the router's native
NMEA stream. The native stream would give better fidelity and cadence, but
polling keeps the sample self-contained: it works on any model with no GPS
connection configured first, and it makes the Config Store integration the
visible part of the design. `status/gps` is common across supported models.

Note that a container cannot be event-driven off router state — config store
event subscriptions are not available — so a poll interval is the only option
here. See `docs/container-development-guide.md`.

## Configuration (appdata)

All fields are created automatically on first run, so they appear in NCM ready to
edit. Values are strings; anything invalid logs a warning and falls back to the
default.

| Key | Default | Description |
|-----|---------|-------------|
| `gps_poll_interval` | `1.0` | Seconds between Config Store polls (0.2-60) |
| `gps_stale_after` | `10` | Seconds before a fix is treated as stale (1-3600) |
| `nmea_port` | `10110` | Loopback port gpsd reads from. Never published |
| `gpsd_port` | `2947` | Port gpsd listens on |
| `web_port` | `8080` | Web UI port |
| `history_points` | `2000` | Maximum breadcrumbs retained (10-20000) |
| `history_min_move_m` | `10` | Minimum movement in metres before recording a point |
| `history_min_interval_s` | `30` | Time floor for recording while stationary |
| `tile_url` | OSM tile URL | Basemap template. Blank disables tiles entirely |
| `enable_gps_if_disabled` | `false` | Allow the container to turn GPS on. See below |

Ports need a container restart. Everything else applies immediately, whether
changed in NCM or in the web UI.

### The one router-config write

With `enable_gps_if_disabled=true` the container may write
`config/system/gps/enabled`. That path is the only entry in an allowlist in
`gps_source.py`, and the write is attempted only if the path already exists and
reads as boolean `false`, proving it is present and the expected shape on this
firmware. If the read returns `None` the write is skipped and logged. Default is
off, so out of the box this container only reads router config.

## Building

Pure Python plus an apk package, so one source tree serves both architectures.

```bash
# ARMv8 64-bit: E300, E3000, R920, R980, R1900, R2100
docker buildx build --platform linux/arm64 -t yourregistry/ncos-gpsd-server:latest .

# ARMv7 32-bit: AER2200, IBR1700
docker buildx build --platform linux/arm/v7 -t yourregistry/ncos-gpsd-server:latest-armv7 .
```

Measured image sizes: **58.0 MB** for arm64 and **45.5 MB** for arm/v7.
`mem_limit: 64M` is enough at runtime. That fits every supported model,
including the 135 MB memory floor on AER2200 and IBR1700 with all key services
enabled, and is small against the 6 GB flash on the most constrained models.

## Deployment

1. Push the image to a registry the router can reach.
2. In NCM, create a container project with this service — or write
   `config/container/projects` directly, which is faster when iterating (see
   `docs/ncos-api/config/container.md`).
3. Map ports `2947:2947/tcp` and `8080:8080/tcp`.
4. Enable the **Config Store** volume. Without it every `cp.py` call returns
   `None` and the UI shows a banner saying so.
5. Add a named volume mounted at `/data` if you want history to survive
   restarts. Without it the track falls back to `/tmp` and works
   non-persistently.
6. Commit, then browse to `http://<router-ip>:8080`.

Verify gpsd from any LAN client:

```bash
gpspipe -w -n 5 <router-ip>:2947
# or
cgps -s <router-ip>:2947
```

## Security

**Both published ports are unauthenticated**, and the API serves the router's
physical location. Mapped ports on NCOS are reachable on WAN as well as LAN with
no firewall filtering in front of them.

For anything beyond a trusted network, drop the `ports:` block and attach the
service to a Local IP Network instead, so it is reachable only on that LAN:

```yaml
services:
  gpsd-server:
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

The loopback NMEA feed on 10110 binds `127.0.0.1` only and is never published.

## Map Tiles

Tiles are fetched **by the browser**, not by the router, from whatever
`tile_url` points at. On a client with internet access through the router the
default OpenStreetMap tiles work as-is. Note that OSM's tile usage policy is not
intended for production deployments; point `tile_url` at your own tile server or
a commercial provider for real use.

Set `tile_url` blank for fully offline operation. The map then draws a reference
grid with a distance scale, and the track and marker still work. The UI itself
has no CDN dependencies and no web fonts, so it loads with no internet access at
all.

## Location History

The track is a bounded breadcrumb list, capped at `history_points` and written to
`/data` as atomic snapshots when a volume is mounted there.

Points are recorded on movement rather than on a timer: a new point needs
`history_min_move_m` of movement, with `history_min_interval_s` as a time floor so
a stationary receiver still records occasionally without filling the buffer. Both
apply only to valid fixes, so a lost lock leaves a gap rather than a false line.

## API

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/` | GET | Web UI |
| `/health` | GET | 200 healthy, 503 otherwise. Covers poller, NMEA listener and gpsd |
| `/api/status` | GET | Current fix, last valid fix, gpsd state, counters |
| `/api/history` | GET | Track points. `?since=<epoch>` and `?limit=<n>` supported |
| `/api/history/clear` | POST | Delete all recorded history |
| `/api/config` | GET / POST | Read or update runtime settings |

The health check covers both supervised processes: the poller and NMEA listener
are threads inside the Python process, and `/health` also opens a socket to
gpsd. A two-process container needs this, otherwise gpsd can die while the
container still reports healthy.

## Verified Before Deployment

Everything below was run on a development machine, with no router involved:

- Both architectures build (58.0 MB arm64, 45.5 MB arm/v7).
- Config parsing, runtime updates, DMS conversion, staleness downgrade, NMEA
  checksums, history recording rules and the whole HTTP API, against a mock
  `AF_UNIX` Config Store.
- **NMEA validated against the real consumer.** gpsd 3.25 attaches to the
  loopback feed and reports `mode: 1` with no lock, which is what the synthesized
  `RMC` status V and `GGA` quality 0 are supposed to mean. A checksum test alone
  would not have shown that.
- The no-Config-Store path, which is exactly what a missing `$CONFIG_STORE`
  volume looks like: `/api/status` reports `config_store_ok: false`, the UI shows
  a banner, and the log names the missing volume rather than looking like a
  receiver that never gets a fix.
- `/health` returns 503 and names the failing component when gpsd is down.
- Static file traversal attempts (`/static/../../../etc/passwd`) return 404.
- `docker stop` returns well inside its timeout, so SIGTERM is handled by the
  supervisor rather than escalating to SIGKILL, and both children exit cleanly.

Not verifiable without a router: real GPS fixes, and NCM appdata round-trips
through an actual Config Store.

## Troubleshooting

| Symptom | Cause |
|---------|-------|
| UI banner about the Config Store | `$CONFIG_STORE` volume not attached to the service |
| `SHM: shmctl(...) for IPC_RMID failed, Operation not permitted` on shutdown | gpsd cleaning up its shared-memory export under user namespace remapping, which NCOS enables. Harmless; gpsd's JSON and NMEA outputs are unaffected |
| Fix never becomes valid | GPS disabled on the router, or no antenna/sky view. Check `status/gps` from the router CLI |
| gpsd reachable but always `mode: 1` | Working as intended with no lock. gpsd is being told there is no fix |
| Map is blank with a note | Browser cannot reach the tile server. Track and marker still work |
| History resets on redeploy | Volume data is not migrated to a new image. Create a new project for a fresh volume |
| Container restarts every few minutes | Either process died; the supervisor exits non-zero on purpose. Check `container logs` for which one |

Useful commands inside the container:

```bash
container exec gpsd-server sh
gpspipe -w -n 10 localhost:2947    # what gpsd is reporting
nc 127.0.0.1 10110                 # raw synthesized NMEA feed
```
