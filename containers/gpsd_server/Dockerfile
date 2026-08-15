FROM alpine:3.18

# gpsd          - the network service published to LAN clients on 2947
# gpsd-clients  - gpspipe/cgps, for debugging inside the container
# python3       - the Config Store adapter and web UI
# cp.py is standard library only, so no py3-requests is needed
RUN apk add --no-cache gpsd gpsd-clients python3

RUN mkdir -p /opt/gpsd_server/src /data

COPY cp.py /opt/gpsd_server/src/cp.py
COPY src/ /opt/gpsd_server/src/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV PYTHONPATH=/opt/gpsd_server/src
# Keep stdout unbuffered so container logs appear as they happen rather than
# arriving in blocks when the buffer flushes.
ENV PYTHONUNBUFFERED=1
# cp.py derives its log prefix from basename(cwd), which is empty at / and
# becomes the generic 'container'. Set it so log lines identify this service.
ENV CP_APP_NAME=gpsd_server

# 2947 gpsd, 8080 web UI
EXPOSE 2947/tcp
EXPOSE 8080/tcp

ENTRYPOINT ["/entrypoint.sh"]
