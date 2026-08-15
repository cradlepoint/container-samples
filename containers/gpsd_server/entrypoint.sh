#!/bin/sh
# Start the Config Store poller, then gpsd on top of it, and supervise both.
#
# Two processes share this container because gpsd is the network service and the
# Python side is the adapter that feeds it from cs.sock. Alpine's ash has no
# "wait -n", so a polling supervisor is used: if either process dies the script
# exits non-zero and the restart policy brings the container back.

set -e

APP_DIR=/opt/gpsd_server/src
ENV_FILE=/tmp/gpsd_server.env

echo "--- reading configuration from appdata ---"
python3 "${APP_DIR}/gen_conf.py" || echo "WARNING: gen_conf.py failed, using built-in defaults"

# Defaults must match config.py in case gen_conf could not run at all.
NMEA_PORT=10110
GPSD_PORT=2947
WEB_PORT=8080
if [ -f "${ENV_FILE}" ]; then
    # shellcheck disable=SC1090
    . "${ENV_FILE}"
fi

echo "--- starting gpsd server (nmea=${NMEA_PORT} gpsd=${GPSD_PORT} web=${WEB_PORT}) ---"
python3 "${APP_DIR}/main.py" &
APP_PID=$!

# gpsd retries a refused connection, but starting it after the feed is listening
# avoids a confusing burst of connection errors in the log.
echo "--- waiting for NMEA feed on 127.0.0.1:${NMEA_PORT} ---"
i=0
while [ "$i" -lt 30 ]; do
    if python3 -c "import socket,sys; s=socket.socket(); s.settimeout(1); sys.exit(0 if s.connect_ex(('127.0.0.1',${NMEA_PORT}))==0 else 1)" 2>/dev/null; then
        echo "--- NMEA feed is up ---"
        break
    fi
    if ! kill -0 "${APP_PID}" 2>/dev/null; then
        echo "ERROR: gpsd server exited before the NMEA feed came up"
        exit 1
    fi
    i=$((i + 1))
    sleep 1
done

# -N: run in the foreground so it can be supervised
# -n: poll the source without waiting for a client to connect
# -G: listen on all interfaces; gpsd binds loopback only by default, which
#     would make the published port unreachable
echo "--- starting gpsd on port ${GPSD_PORT} ---"
gpsd -N -n -G -S "${GPSD_PORT}" "tcp://127.0.0.1:${NMEA_PORT}" &
GPSD_PID=$!

terminate() {
    echo "--- signal received, stopping ---"
    kill "${APP_PID}" "${GPSD_PID}" 2>/dev/null || true
    wait "${APP_PID}" 2>/dev/null || true
    wait "${GPSD_PID}" 2>/dev/null || true
    exit 0
}
trap terminate TERM INT

while kill -0 "${APP_PID}" 2>/dev/null && kill -0 "${GPSD_PID}" 2>/dev/null; do
    sleep 5
done

if ! kill -0 "${APP_PID}" 2>/dev/null; then
    echo "ERROR: gpsd server process exited"
else
    echo "ERROR: gpsd exited"
fi
kill "${APP_PID}" "${GPSD_PID}" 2>/dev/null || true
exit 1
