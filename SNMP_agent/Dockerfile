FROM alpine:3.18

RUN apk add --no-cache net-snmp net-snmp-tools python3 py3-requests

# Remove MIB files that cause dependency warnings or type re-encoding
RUN rm -f /usr/share/snmp/mibs/IP-MIB.txt \
         /usr/share/snmp/mibs/IF-MIB.txt \
         /usr/share/snmp/mibs/IP-FORWARD-MIB.txt

RUN mkdir -p /opt/snmp /etc/snmp

COPY cp.py /opt/snmp/cp.py
COPY ncos_snmp.py /opt/snmp/ncos_snmp.py
COPY gen_conf.py /opt/snmp/gen_conf.py
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /opt/snmp/ncos_snmp.py /opt/snmp/gen_conf.py /entrypoint.sh

ENV PYTHONPATH=/opt/snmp

EXPOSE 1161/udp

ENTRYPOINT ["/entrypoint.sh"]
