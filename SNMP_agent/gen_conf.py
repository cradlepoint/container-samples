#!/usr/bin/env python3
"""Read SNMP config from router config + appdata, write snmpd.conf."""
import cp


def get_appdata(name):
    return cp.get_appdata(name) or ''


def main():
    snmp_port = get_appdata('snmp_port') or '1161'

    # Read router's SNMP config so our agent matches
    snmp_cfg = cp.get('config/system/snmp') or {}
    router_port = snmp_cfg.get('lan_port', 161) or 161
    snmp_version = snmp_cfg.get('snmp_version', 2)

    # SNMPv3 from router config or appdata
    snmpv3_user = snmp_cfg.get('user', '') or get_appdata('snmpv3_user')
    snmpv3_auth_pass = snmp_cfg.get('password', '') or get_appdata('snmpv3_auth_pass')
    snmpv3_priv_pass = snmp_cfg.get('privacy_password', '') or get_appdata('snmpv3_priv_pass')

    # Find router IP (default gateway from container's perspective)
    router_ip = _get_router_ip()

    lines = []
    # Our pass_persist handlers
    lines.append('pass_persist .1.3.6.1.2.1.2 /usr/bin/python3 /opt/snmp/ncos_snmp.py')
    lines.append('pass_persist .1.3.6.1.2.1.3 /usr/bin/python3 /opt/snmp/ncos_snmp.py')
    lines.append('pass_persist .1.3.6.1.2.1.4.20 /usr/bin/python3 /opt/snmp/ncos_snmp.py')
    lines.append('pass_persist .1.3.6.1.2.1.4.22 /usr/bin/python3 /opt/snmp/ncos_snmp.py')
    lines.append('pass_persist .1.3.6.1.2.1.17.7 /usr/bin/python3 /opt/snmp/ncos_snmp.py')
    lines.append('pass_persist .1.3.6.1.2.1.31 /usr/bin/python3 /opt/snmp/ncos_snmp.py')
    lines.append('')

    # Community strings - support comma-separated lists from appdata
    ro_communities = []
    rw_communities = []
    # Router config communities (may be comma-separated)
    router_ro = snmp_cfg.get('get_community', '')
    router_rw = snmp_cfg.get('set_community', '')
    if router_ro:
        for c in router_ro.split(','):
            c = c.strip()
            if c and c not in ro_communities:
                ro_communities.append(c)
    if router_rw:
        for c in router_rw.split(','):
            c = c.strip()
            if c and c not in rw_communities:
                rw_communities.append(c)
    # Appdata communities (comma-separated)
    appdata_ro = get_appdata('community_ro')
    appdata_rw = get_appdata('community_rw')
    if appdata_ro:
        for c in appdata_ro.split(','):
            c = c.strip()
            if c and c not in ro_communities:
                ro_communities.append(c)
    if appdata_rw:
        for c in appdata_rw.split(','):
            c = c.strip()
            if c and c not in rw_communities:
                rw_communities.append(c)
    for c in ro_communities:
        lines.append('rocommunity %s' % c)
    for c in rw_communities:
        lines.append('rwcommunity %s' % c)
    # Use first RO community for proxy
    community_ro = ro_communities[0] if ro_communities else ''

    # SNMPv3
    if snmpv3_user and snmpv3_auth_pass:
        if snmpv3_priv_pass:
            lines.append('createUser %s SHA "%s" AES "%s"' % (
                snmpv3_user, snmpv3_auth_pass, snmpv3_priv_pass))
            lines.append('rwuser %s priv' % snmpv3_user)
        else:
            lines.append('createUser %s SHA "%s"' % (
                snmpv3_user, snmpv3_auth_pass))
            lines.append('rwuser %s auth' % snmpv3_user)

    # System MIB from NCOS
    prod = cp.get('status/product_info') or {}
    fw = cp.get('status/fw_info') or {}
    ver = '%s.%s.%s' % (fw.get('major_version', 0), fw.get('minor_version', 0), fw.get('patch_version', 0))
    sys_descr = '%s %s NCOS %s' % (prod.get('company_name', 'Ericsson'), prod.get('product_name', 'Cradlepoint'), ver)
    sys_contact = snmp_cfg.get('sys_contact') or 'not set'
    sys_name = snmp_cfg.get('sys_name') or cp.get('config/system/system_id') or 'unknown'
    sys_location = snmp_cfg.get('sys_location') or 'not set'

    lines.append('')
    lines.append('sysDescr %s' % sys_descr)
    lines.append('sysObjectID .1.3.6.1.4.1.20992')
    lines.append('sysContact %s' % sys_contact)
    lines.append('sysName %s' % sys_name)
    lines.append('sysLocation %s' % sys_location)
    lines.append('sysServices 72')

    # Proxy: forward specific subtrees NOT handled by pass_persist to router
    # Our pass_persist handles: .1.3.6.1.2.1.2 (IF-MIB), .1.3.6.1.2.1.4.22 (ARP),
    #   .1.3.6.1.2.1.17.7 (Q-BRIDGE), .1.3.6.1.2.1.31 (ifXTable)
    # Proxy everything else to the router's built-in agent
    if router_ip and community_ro:
        proxy_community = community_ro
        proxy_target = '%s:%s' % (router_ip, router_port)
        lines.append('')
        lines.append('# Proxy to router built-in SNMP agent')
        # Proxy specific useful subtrees that we don't handle
        lines.append('proxy -v2c -c %s %s .1.3.6.1.2.1.25' % (proxy_community, proxy_target))  # HOST-RESOURCES
        lines.append('proxy -v2c -c %s %s .1.3.6.1.2.1.47' % (proxy_community, proxy_target))  # ENTITY-MIB
        lines.append('proxy -v2c -c %s %s .1.3.6.1.6' % (proxy_community, proxy_target))        # snmpModules

    conf = '\n'.join(lines)
    with open('/etc/snmp/snmpd.conf', 'w') as f:
        f.write(conf)
    with open('/tmp/snmp_port', 'w') as f:
        f.write(snmp_port)
    print(conf)


def _get_router_ip():
    """Get the router's IP reachable from the container (bridge gateway)."""
    try:
        with open('/proc/net/route', 'r') as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) >= 3 and parts[1] == '00000000':
                    gw_hex = parts[2]
                    gw_bytes = bytes.fromhex(gw_hex)
                    return '%d.%d.%d.%d' % (gw_bytes[3], gw_bytes[2], gw_bytes[1], gw_bytes[0])
    except Exception:
        pass
    # Fallback: try LAN IP from NCOS
    try:
        lan = cp.get('status/lan') or {}
        nets = lan.get('networks', {}) or {}
        for nn, nd in nets.items():
            ni = nd.get('info', {}) or {}
            ip = ni.get('ip_address', '')
            if ip:
                return ip
    except Exception:
        pass
    return None


if __name__ == '__main__':
    main()
