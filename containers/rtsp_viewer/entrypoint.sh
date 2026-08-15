#!/bin/sh
# Generates /config/go2rtc.yaml from environment variables, unless a config
# file has already been provided (e.g. via a bind-mounted go2rtc.yaml for
# local docker-compose use). This lets the same image be used:
#   - locally, with go2rtc.yaml mounted as a volume, or
#   - on a Cradlepoint router (NCOS), which can't bind-mount host files but
#     can set "environment:" in its Compose config.
set -eu

CONFIG_FILE="/config/go2rtc.yaml"

if [ -s "$CONFIG_FILE" ]; then
    echo "Using existing config: $CONFIG_FILE"
else
    echo "Generating $CONFIG_FILE from environment variables"
    {
        echo "streams:"
        found=0
        i=1
        while [ "$i" -le 20 ]; do
            eval "name=\${CAMERA${i}_NAME:-}"
            eval "url=\${CAMERA${i}_URL:-}"
            if [ -n "$url" ]; then
                found=1
                if [ -z "$name" ]; then
                    name="camera${i}"
                fi
                echo "  ${name}: \"${url}\""
            fi
            i=$((i + 1))
        done
        # Simple single-camera fallback if CAMERA1_URL wasn't used.
        if [ "$found" -eq 0 ] && [ -n "${RTSP_URL:-}" ]; then
            echo "  ${CAMERA_NAME:-camera1}: \"${RTSP_URL}\""
        fi

        echo ""
        echo "api:"
        echo "  listen: \":1984\""
        # go2rtc skips Basic auth for localhost callers even when configured, so
        # this only protects the port once it's reachable from the network --
        # which on NCOS means WAN too, since mapped ports aren't firewalled.
        if [ -n "${API_USERNAME:-}" ] && [ -n "${API_PASSWORD:-}" ]; then
            echo "  username: \"${API_USERNAME}\""
            echo "  password: \"${API_PASSWORD}\""
        fi
        echo ""
        echo "rtsp:"
        echo "  listen: \":8554\""
        if [ -n "${RTSP_USERNAME:-}" ] && [ -n "${RTSP_PASSWORD:-}" ]; then
            echo "  username: \"${RTSP_USERNAME}\""
            echo "  password: \"${RTSP_PASSWORD}\""
        fi
        echo ""
        echo "webrtc:"
        echo "  listen: \":8555\""
        # If the container sits behind NAT/port-mapping (e.g. on a router's
        # Docker bridge), go2rtc needs to advertise the real reachable
        # address instead of its internal bridge IP for WebRTC to work.
        if [ -n "${WEBRTC_CANDIDATE:-}" ]; then
            echo "  candidates:"
            echo "    - \"${WEBRTC_CANDIDATE}\""
        fi
    } > "$CONFIG_FILE"
fi

exec go2rtc -config "$CONFIG_FILE"
