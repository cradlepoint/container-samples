FROM alpine:3.18

ARG GO2RTC_VERSION=v1.9.14
# Set automatically by buildx from --platform; used to pick the matching
# go2rtc release asset below. Do not hardcode one architecture here -- the
# smallest routers (AER2200, IBR1700) are arm/v7, not arm64.
ARG TARGETARCH
ARG TARGETVARIANT

# ffmpeg is used by go2rtc for on-the-fly transcoding of streams the
# browser can't play natively (e.g. H.265 -> H.264, or MJPEG fallback).
# No python3 here: go2rtc is a static Go binary and nothing in this image
# talks to the Config Store, so there is no cp.py dependency to satisfy.
RUN apk add --no-cache ffmpeg curl ca-certificates \
    && case "${TARGETARCH}/${TARGETVARIANT}" in \
         arm64/*) GO2RTC_ASSET=go2rtc_linux_arm64 ;; \
         arm/v7)  GO2RTC_ASSET=go2rtc_linux_arm ;; \
         arm/v6)  GO2RTC_ASSET=go2rtc_linux_armv6 ;; \
         amd64/*) GO2RTC_ASSET=go2rtc_linux_amd64 ;; \
         *) echo "unsupported platform: ${TARGETARCH}/${TARGETVARIANT}" >&2; exit 1 ;; \
       esac \
    && curl -fL -o /usr/local/bin/go2rtc \
       "https://github.com/AlexxIT/go2rtc/releases/download/${GO2RTC_VERSION}/${GO2RTC_ASSET}" \
    && chmod +x /usr/local/bin/go2rtc

WORKDIR /config
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# 1984 = web UI / HTTP API, 8554 = RTSP relay (optional), 8555 = WebRTC TCP/UDP
EXPOSE 1984 8554 8555/tcp 8555/udp

# entrypoint.sh generates /config/go2rtc.yaml from CAMERA*_URL env vars if
# no config file is already present (e.g. bind-mounted for local use).
ENTRYPOINT ["/entrypoint.sh"]
