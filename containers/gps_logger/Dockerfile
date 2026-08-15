FROM alpine:3.18

# cp.py is standard library only -- no py3-requests needed
RUN apk add --no-cache python3

RUN mkdir -p /opt/app
WORKDIR /opt/app

COPY cp.py gps_logger.py entrypoint.sh /opt/app/
RUN chmod +x /opt/app/entrypoint.sh

ENV PYTHONPATH=/opt/app
# Without this, cp.log() falls back to the generic "container:" prefix.
ENV CP_APP_NAME=gps_logger

ENTRYPOINT ["/opt/app/entrypoint.sh"]
