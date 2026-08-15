"""gpsd server entry point.

Wires up the pipeline and owns shutdown:

    Config Store poll --> Fix --> NMEA on 127.0.0.1 --> gpsd --> LAN clients
                            |
                            +--> track history --> web UI map

gpsd itself is started by entrypoint.sh, not from here. This process only owns
the Python side; the entrypoint supervises both and exits if either dies.
"""

import signal
import sys
import threading
import time
from typing import Optional

import cp

import config
import gps_source
import history
import nmea
import web_server
from nmea_server import NmeaServer

_STATS_INTERVAL_S = 60.0

_stop_event = threading.Event()


def _handle_signal(signum, _frame) -> None:
    cp.log(f"main: received signal {signum}, shutting down")
    _stop_event.set()


class Diagnostics:
    """Tracks whether the Config Store is actually readable.

    Without the $CONFIG_STORE volume every cp.get() returns None rather than
    raising, so the application would otherwise look like a working GPS that
    simply never gets a lock. Probing a path that must always exist tells the
    two failures apart and lets the UI say which one it is.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ok = False

    def probe(self, announce: bool = False) -> bool:
        ok = cp.config_store_available()
        with self._lock:
            changed = ok != self._ok
            self._ok = ok
        if announce or changed:
            if ok:
                cp.log(
                    "main: config store OK, running on "
                    f"{cp.get_product_name() or 'unknown model'} "
                    f"serial={cp.get_serial_number() or 'unknown'} "
                    f"fw={cp.get_firmware_version() or 'unknown'}"
                )
            else:
                status = cp.config_store_status()
                cp.log(
                    f"main: config store unavailable ({status['last_error']}). "
                    f"socket {status['socket_path']} "
                    f"{'exists' if status['socket_exists'] else 'is missing'} -- "
                    "attach the $CONFIG_STORE volume to this service. "
                    "No GPS data will be available until then."
                )
        return ok

    def config_store_ok(self) -> bool:
        with self._lock:
            return self._ok


def _stats_loop(source, nmea_srv, track, diagnostics: Diagnostics, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        stop_event.wait(_STATS_INTERVAL_S)
        if stop_event.is_set():
            break
        # Re-probe so a Config Store that becomes readable later is picked up
        # without a restart, and so a regression is reported.
        diagnostics.probe()
        try:
            gps_stats = source.stats()
            fix = source.current()
            cp.log(
                "stats: "
                f"polls={gps_stats['polls']} errors={gps_stats['errors']} "
                f"fix={'valid' if fix.valid else 'invalid'} sats={fix.satellites} "
                f"nmea_clients={nmea_srv.stats()['clients']} "
                f"track_points={track.stats()['points']}"
            )
            if gps_stats["polls"] == 0:
                cp.log("stats: no successful Config Store polls yet")
        except Exception as exc:  # noqa: BLE001
            cp.log(f"stats: {exc}")


def main() -> int:
    started_at = time.time()
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    diagnostics = Diagnostics()
    diagnostics.probe(announce=True)

    config.provision_defaults()
    cfg = config.load()
    store = config.ConfigStore(cfg)
    cp.log(
        "main: config "
        f"poll={cfg.gps_poll_interval}s stale_after={cfg.gps_stale_after}s "
        f"nmea_port={cfg.nmea_port} gpsd_port={cfg.gpsd_port} web_port={cfg.web_port} "
        f"history_points={cfg.history_points}"
    )

    gps_source.ensure_gps_enabled(cfg.enable_gps_if_disabled)

    source = gps_source.GpsSource(_stop_event, cfg.gps_poll_interval, cfg.gps_stale_after)

    track = history.TrackStore(
        max_points=cfg.history_points,
        min_move_m=cfg.history_min_move_m,
        min_interval_s=cfg.history_min_interval_s,
    )
    track.load()
    source.add_listener(track.on_fix)

    nmea_srv = NmeaServer(
        stop_event=_stop_event,
        port=cfg.nmea_port,
        fix_provider=source.current,
        sentence_builder=nmea.sentences_for,
        emit_interval=max(1.0, cfg.gps_poll_interval),
    )

    def _on_config_change(updated: config.AppConfig) -> None:
        source.apply_config(updated.gps_poll_interval, updated.gps_stale_after)
        track.apply_config(
            updated.history_min_move_m, updated.history_min_interval_s, updated.history_points
        )
        cp.log("main: runtime configuration updated")

    store.on_change(_on_config_change)

    try:
        nmea_srv.start()
    except OSError as exc:
        cp.log(f"main: cannot bind NMEA port {cfg.nmea_port}: {exc}")
        return 1

    source.start()

    threading.Thread(
        target=track.run_saver, args=(_stop_event,), name="track-saver", daemon=True
    ).start()
    threading.Thread(
        target=_stats_loop,
        args=(source, nmea_srv, track, diagnostics, _stop_event),
        name="stats",
        daemon=True,
    ).start()

    context = web_server.AppContext(
        config_store=store,
        gps_source=source,
        nmea_server=nmea_srv,
        track_store=track,
        stop_event=_stop_event,
        started_at=started_at,
        gpsd_port_provider=lambda: store.snapshot().gpsd_port,
        config_store_ok_provider=diagnostics.config_store_ok,
    )

    try:
        server = web_server.make_server(cfg.web_port, context)
    except OSError as exc:
        cp.log(f"main: cannot bind web port {cfg.web_port}: {exc}")
        _stop_event.set()
        return 1

    web_thread = web_server.start(server)
    cp.log(f"main: web UI on port {cfg.web_port}, NMEA feed on 127.0.0.1:{cfg.nmea_port}")

    # Idle here until a signal arrives. All work happens in the threads above.
    while not _stop_event.is_set():
        _stop_event.wait(1.0)

    cp.log("main: stopping")
    web_server.stop(server, web_thread)
    nmea_srv.close()
    track.save(force=True)
    cp.log("main: stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
