#!/usr/bin/env python3
"""Minimal GPS logging example.

Polls `status/gps/fix` from the router's Config Store every 10 seconds and
logs the position with `cp.log()`. This is the smallest useful cp.py polling
loop in this repo. For a full gpsd protocol translator with a web map, see
the `gpsd_server/` sample instead.
"""

import time

import cp

POLL_INTERVAL_SECONDS = 10


def log_fix():
    """Read and log the current GPS fix, or explain why there isn't one.

    A missing $CONFIG_STORE volume and a router with no GPS data both surface
    as None from cp.get() -- config_store_available() is what tells them apart
    (see the Error Handling Contract in docs/ncos-sdk-reference.md).
    """
    if not cp.config_store_available():
        cp.log(f'no Config Store: {cp.config_store_status()}')
        return

    fix = cp.get('status/gps/fix')
    if not isinstance(fix, dict):
        cp.log('no GPS data at status/gps/fix (this model may have no GPS '
               'receiver, or GPS is disabled in router config)')
        return

    if not fix.get('lock'):
        cp.log(f"no GPS lock (satellites={fix.get('satellites', 0)})")
        return

    latitude, longitude = cp.get_lat_long()
    if latitude is None or longitude is None:
        cp.log('fix reports lock=True but latitude/longitude could not be parsed')
        return

    cp.log(
        f'lat={latitude:.6f} lon={longitude:.6f} '
        f'alt={fix.get("altitude_meters")}m '
        f'satellites={fix.get("satellites")} '
        f'speed={fix.get("ground_speed_knots")}kn '
        f'heading={fix.get("heading")}'
    )


if __name__ == '__main__':
    cp.log(f'starting GPS logger, polling every {POLL_INTERVAL_SECONDS}s')
    while True:
        log_fix()
        time.sleep(POLL_INTERVAL_SECONDS)
