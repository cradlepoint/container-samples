"""Emit shell variables for entrypoint.sh.

The entrypoint needs the gpsd and NMEA ports to build the gpsd command line,
but the authoritative values live in appdata. Rather than parsing JSON in ash,
this runs first and writes a small env file for the shell to source -- the same
pattern the snmp_agent sample uses for its port.

Appdata defaults are also provisioned here so the fields exist in NCM even if
the main process fails to start.
"""

import sys

import cp

import config

_ENV_PATH = "/tmp/gpsd_server.env"


def main() -> int:
    try:
        config.provision_defaults()
        cfg = config.load()
    except Exception as exc:  # noqa: BLE001
        # Falling back to defaults is better than refusing to start: without the
        # Config Store volume every appdata read returns None, and the container
        # should still come up far enough to show a diagnostic in the web UI.
        cp.log(f"gen_conf: falling back to defaults ({exc})")
        cfg = config.AppConfig()

    lines = [
        f"NMEA_PORT={cfg.nmea_port}",
        f"GPSD_PORT={cfg.gpsd_port}",
        f"WEB_PORT={cfg.web_port}",
    ]
    try:
        with open(_ENV_PATH, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as exc:
        cp.log(f"gen_conf: could not write {_ENV_PATH}: {exc}")
        return 1

    cp.log(f"gen_conf: wrote {_ENV_PATH} ({' '.join(lines)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
