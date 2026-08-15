# GPS Logger Container

A minimal example of reading GPS data from a Cradlepoint (Ericsson) NCOS
router: a single Python process that polls `status/gps/fix` from the Config
Store every 10 seconds and logs the position with `cp.log()`. No web UI, no
ports, no appdata.

For a full gpsd-protocol endpoint with a web map and track history, see the
`gpsd_server/` sample instead.

## What It Does

Every 10 seconds it reads the current fix and logs one line:

- No Config Store attached: logs the connectivity problem.
- No GPS data at all (model has no receiver, or GPS is disabled): says so.
- No lock yet: logs the satellite count.
- Locked: logs latitude, longitude, altitude, satellite count, ground speed
  and heading.

The poll interval is the `POLL_INTERVAL_SECONDS` constant in `gps_logger.py`.
Edit it and rebuild for a different interval.

## Files

| File | Purpose |
|------|---------|
| `Dockerfile` | Alpine base with `python3` only |
| `entrypoint.sh` | Starts the Python process |
| `gps_logger.py` | The polling loop |
| `cp.py` | NCOS SDK module for Config Store communication |
| `docker-compose.yml` | Deployment example |

## Building

```bash
# ARMv8 64-bit: E300, E3000, R920, R980, R1900, R2100
docker buildx build --platform linux/arm64 -t yourregistry/ncos_gps_logger:latest .

# ARMv7 32-bit: AER2200, IBR1700
docker buildx build --platform linux/arm/v7 -t yourregistry/ncos_gps_logger:latest-armv7 .
```

## Deployment

Push the built image to a registry the router can reach, then create a
container project on the router with this Compose:

```yaml
version: '2.4'
services:
  gps_logger:
    image: 'yourregistry/ncos_gps_logger:latest'
    volumes:
      - $CONFIG_STORE
```

The `$CONFIG_STORE` volume is required for `cp.py` -- without it every poll
logs a "no Config Store" message instead of GPS data. No ports are published.

Watch `container logs gps_logger` to see the output.

## Troubleshooting

| Symptom | Cause |
|---------|-------|
| `no Config Store: {...}` every poll | `$CONFIG_STORE` volume not attached to the service |
| `no GPS data at status/gps/fix` | Model has no GPS receiver, or GPS is disabled in router config (`config/system/gps/enabled`) |
| `no GPS lock (satellites=0)` | No antenna / no sky view. Check `status/gps` from the router CLI |
