#!/bin/sh
# Read SNMP config from NCOS appdata via cs.sock, then start snmpd.

echo "--- generating snmpd.conf from appdata ---"
python3 /opt/snmp/gen_conf.py

if [ ! -f /etc/snmp/snmpd.conf ]; then
    echo "ERROR: Failed to generate snmpd.conf"
    exit 1
fi

SNMP_PORT=$(cat /tmp/snmp_port 2>/dev/null || echo 1161)

# Build exclusion list for built-in modules we replace with pass_persist
EXCLUDE="ifTable,ifXTable,interfaces,interface"
EXCLUDE="${EXCLUDE},ip,ipv6,ip_scalars,ipAddressTable,ipAddressPrefixTable"
EXCLUDE="${EXCLUDE},ipDefaultRouterTable,inetNetToMediaTable,ipNetToMediaTable"
EXCLUDE="${EXCLUDE},ipSystemStatsTable,ipIfStatsTable,ipCidrRouteTable"
EXCLUDE="${EXCLUDE},ipv4InterfaceTable,ipv6InterfaceTable,ipv6ScopeZoneIndexTable"
EXCLUDE="${EXCLUDE},ip_forward,ipv6_route,route_write"
EXCLUDE="${EXCLUDE},at"

echo "--- starting snmpd on port ${SNMP_PORT} ---"
echo "--- excluding modules: ${EXCLUDE} ---"
export MIBS=""
exec snmpd -f -Le -I -${EXCLUDE} -C -c /etc/snmp/snmpd.conf udp:${SNMP_PORT}
