#!/usr/bin/env python3
"""
Net-SNMP pass_persist handler for Cradlepoint NCOS.
Uses cp.py for cs.sock access.
"""
import sys
import time
import threading
import cp

CACHE_TTL = 10
_cache = {}
_cache_ts = {}

# Background tree: always serve from last successful build
_oid_tree = {}
_oid_tree_lock = threading.Lock()
_tree_ready = threading.Event()


def cached_get(path):
    now = time.time()
    if path in _cache and (now - _cache_ts.get(path, 0)) < CACHE_TTL:
        return _cache[path]
    try:
        val = cp.get(path)
    except Exception:
        val = None
    _cache[path] = val
    _cache_ts[path] = now
    return val


SYS_OID_BASE = '.1.3.6.1.2.1.1'
IF_TABLE_OID = '.1.3.6.1.2.1.2.2.1'
IF_NUMBER_OID = '.1.3.6.1.2.1.2.1.0'
IFX_TABLE_OID = '.1.3.6.1.2.1.31.1.1.1'
ARP_TABLE_OID = '.1.3.6.1.2.1.4.22.1'
ENTERPRISE_OID = '.1.3.6.1.4.1.20992'
IF_INDEX = 1; IF_DESCR = 2; IF_TYPE = 3; IF_MTU = 4; IF_SPEED = 5
IF_PHYS_ADDRESS = 6; IF_ADMIN_STATUS = 7; IF_OPER_STATUS = 8
IF_LAST_CHANGE = 9; IF_IN_OCTETS = 10; IF_IN_UCAST_PKTS = 11
IF_IN_NUCAST_PKTS = 12; IF_IN_DISCARDS = 13; IF_IN_ERRORS = 14
IF_IN_UNKNOWN_PROTOS = 15; IF_OUT_OCTETS = 16; IF_OUT_UCAST_PKTS = 17
IF_OUT_NUCAST_PKTS = 18; IF_OUT_DISCARDS = 19; IF_OUT_ERRORS = 20
IF_OUT_QLEN = 21; IF_SPECIFIC = 22
IFX_NAME = 1; IFX_IN_MCAST_PKTS = 2; IFX_IN_BCAST_PKTS = 3
IFX_OUT_MCAST_PKTS = 4; IFX_OUT_BCAST_PKTS = 5; IFX_HC_IN_OCTETS = 6
IFX_HC_IN_UCAST_PKTS = 7; IFX_HC_IN_MCAST_PKTS = 8
IFX_HC_IN_BCAST_PKTS = 9; IFX_HC_OUT_OCTETS = 10
IFX_HC_OUT_UCAST_PKTS = 11; IFX_HC_OUT_MCAST_PKTS = 12
IFX_HC_OUT_BCAST_PKTS = 13; IFX_LINK_UP_DOWN_TRAP = 14
IFX_HIGH_SPEED = 15; IFX_PROMISCUOUS = 16; IFX_CONNECTOR = 17
IFX_ALIAS = 18; IFX_COUNTER_DISC_TIME = 19
ARP_IF_INDEX = 1; ARP_PHYS_ADDRESS = 2; ARP_NET_ADDRESS = 3; ARP_TYPE = 4

# RFC 1213 atTable (.1.3.6.1.2.1.3.1.1) - old ARP table
# Index: {ifIndex}.1.{IP}
AT_TABLE_OID = '.1.3.6.1.2.1.3.1.1'
AT_IF_INDEX = 1; AT_PHYS_ADDRESS = 2; AT_NET_ADDRESS = 3

# ipAddrTable (.1.3.6.1.2.1.4.20.1) - IP address to interface mapping
# Index: {IP}
IPADDR_TABLE_OID = '.1.3.6.1.2.1.4.20.1'
IPADDR_ADDR = 1; IPADDR_IF_INDEX = 2; IPADDR_NETMASK = 3
IPADDR_BCASTADDR = 4; IPADDR_REASMMAX = 5

# Q-BRIDGE-MIB: dot1qTpFdbTable (.1.3.6.1.2.1.17.7.1.2.2.1)
# Index: {vlanId}.{mac as 6 decimal octets}
QBRIDGE_FDB_OID = '.1.3.6.1.2.1.17.7.1.2.2.1'
QFDB_ADDRESS = 1  # MAC address
QFDB_PORT = 2     # port number
QFDB_STATUS = 3   # 1=other,2=invalid,3=learned,4=self,5=mgmt


def _parse_link_speed(s):
    if not s or s == 'Unknown':
        return 0
    s = s.upper()
    for sfx in ('FD', 'HD', 'F', 'H'):
        if s.endswith(sfx):
            s = s[:-len(sfx)]
            break
    try:
        return int(s) * 1000000
    except (ValueError, TypeError):
        return 0


def _mac_to_hex(mac):
    """Convert MAC like '00:30:44:4e:3a:e3' to raw hex bytes for pass_persist."""
    if not mac:
        return ''
    clean = mac.replace(':', '').replace('-', '')
    if len(clean) != 12:
        return ''
    # Return as space-separated hex pairs for Net-SNMP octet string
    return ' '.join(clean[i:i+2].upper() for i in range(0, 12, 2))


def _mac_to_raw(mac):
    """Format MAC as hex string for pass_persist octet type."""
    if not mac:
        return ''
    clean = mac.replace(':', '').replace('-', '').upper()
    if len(clean) != 12:
        return ''
    # Format as "XX XX XX XX XX XX" for octet type
    return ' '.join(clean[i:i+2] for i in range(0, 12, 2))


def _ip_in_subnet(ip, net_ip, net_mask):
    """Check if ip is in the subnet defined by net_ip/net_mask."""
    try:
        ip_parts = [int(x) for x in ip.split('.')]
        net_parts = [int(x) for x in net_ip.split('.')]
        mask_parts = [int(x) for x in net_mask.split('.')]
        for i in range(4):
            if (ip_parts[i] & mask_parts[i]) != (net_parts[i] & mask_parts[i]):
                return False
        return True
    except Exception:
        return False


def _prefetch():
    cached_get('status/product_info')
    cached_get('status/ethernet')
    cached_get('status/wan/devices')
    cached_get('status/lan')
    cached_get('status/lan/clients')
    cached_get('config/vlan')


def discover_interfaces():
    prod = cached_get('status/product_info') or {}
    mac0 = prod.get('mac0', '')
    interfaces = []
    idx = 1
    for port in (cached_get('status/ethernet') or []):
        if not isinstance(port, dict):
            continue
        pn = port.get('port', 0)
        inc = port.get('incoming', {}) or {}
        out = port.get('outgoing', {}) or {}
        phys = mac0
        if mac0:
            try:
                mb = bytes.fromhex(mac0.replace(':', '').replace('-', ''))
                phys = ':'.join('%02x' % b for b in (int.from_bytes(mb, 'big') + pn).to_bytes(6, 'big'))
            except Exception:
                pass
        interfaces.append({
            'index': idx, 'descr': port.get('port_name', 'eth%d' % pn),
            'name': port.get('port_name', 'eth%d' % pn), 'type': 6,
            'mtu': inc.get('port_mtu', 1500) or 1500,
            'speed': _parse_link_speed(port.get('link_speed', '')),
            'phys_address': phys,
            'admin_status': 1 if port.get('enabled', True) else 2,
            'oper_status': 1 if port.get('link', 'down') == 'up' else 2,
            'in_octets': inc.get('bytes', 0) or 0,
            'in_ucast_pkts': inc.get('packets', 0) or 0,
            'in_discards': 0, 'in_errors': inc.get('errors', 0) or 0,
            'out_octets': out.get('bytes', 0) or 0,
            'out_ucast_pkts': out.get('packets', 0) or 0,
            'out_discards': 0, 'out_errors': out.get('errors', 0) or 0,
            'alias': 'Ethernet port %d' % pn,
            'set_admin_path': 'config/ethernet/%d/enabled' % pn,
            'set_admin_type': 'ethernet',
        })
        idx += 1
    wan = cached_get('status/wan/devices') or {}
    if isinstance(wan, dict):
        for did, dev in sorted(wan.items()):
            if not isinstance(dev, dict):
                continue
            info = dev.get('info', {}) or {}
            st = dev.get('status', {}) or {}
            stats = dev.get('stats', {}) or {}
            cell = did.startswith('mdm-')
            conn = st.get('connection_state', 'disconnected')
            lnk = st.get('link_state', 'down')
            descr = ('Cellular %s (%s)' % (did, info.get('model', did))) if cell else ('WAN %s' % did)
            spd = (150000000 if conn == 'connected' else 0) if cell else (1000000000 if lnk == 'up' else 0)
            interfaces.append({
                'index': idx, 'descr': descr, 'name': did,
                'type': 243 if cell else 6, 'mtu': 1500, 'speed': spd,
                'phys_address': mac0,
                'admin_status': 1 if conn != 'disconnected' else 2,
                'oper_status': 1 if conn == 'connected' else 2,
                'in_octets': stats.get('in', 0) or 0,
                'in_ucast_pkts': stats.get('ipackets', 0) or 0,
                'in_discards': stats.get('idrops', 0) or 0,
                'in_errors': stats.get('ierrors', 0) or 0,
                'out_octets': stats.get('out', 0) or 0,
                'out_ucast_pkts': stats.get('opackets', 0) or 0,
                'out_discards': stats.get('odrops', 0) or 0,
                'out_errors': stats.get('oerrors', 0) or 0,
                'alias': descr, 'set_admin_path': info.get('config_id', ''),
                'set_admin_type': 'wan',
            })
            idx += 1
    lan = cached_get('status/lan') or {}
    ls = lan.get('stats', {}) or {}
    nets = lan.get('networks', {}) or {}
    if isinstance(nets, dict):
        for nn, nd in sorted(nets.items()):
            if not isinstance(nd, dict):
                continue
            ni = nd.get('info', {}) or {}
            interfaces.append({
                'index': idx,
                'descr': 'LAN %s (%s/%s)' % (nn, ni.get('ip_address', ''), ni.get('netmask', '')),
                'name': nn, 'type': 6, 'mtu': 1500, 'speed': 1000000000,
                'phys_address': '', 'admin_status': 1, 'oper_status': 1,
                'in_octets': ls.get('in', 0) or 0,
                'in_ucast_pkts': ls.get('ipackets', 0) or 0,
                'in_discards': ls.get('idrops', 0) or 0, 'in_errors': 0,
                'out_octets': ls.get('out', 0) or 0,
                'out_ucast_pkts': ls.get('opackets', 0) or 0,
                'out_discards': 0, 'out_errors': 0,
                'alias': 'LAN %s' % nn, 'set_admin_path': '', 'set_admin_type': 'lan',
            })
            idx += 1
    return interfaces


def build_oid_tree():
    _prefetch()
    tree = {}
    prod = cached_get('status/product_info') or {}
    fw = cached_get('status/fw_info') or {}
    ss = cached_get('status/system') or {}
    ifaces = discover_interfaces()
    tree[IF_NUMBER_OID] = ('integer', str(len(ifaces)))
    for f in ifaces:
        i = f['index']; b = IF_TABLE_OID
        tree['%s.%d.%d' % (b,IF_INDEX,i)] = ('integer', str(i))
        tree['%s.%d.%d' % (b,IF_DESCR,i)] = ('string', f['descr'])
        tree['%s.%d.%d' % (b,IF_TYPE,i)] = ('integer', str(f['type']))
        tree['%s.%d.%d' % (b,IF_MTU,i)] = ('integer', str(f['mtu']))
        tree['%s.%d.%d' % (b,IF_SPEED,i)] = ('gauge', str(min(f['speed'], 4294967295)))
        mac_hex = _mac_to_raw(f['phys_address'])
        tree['%s.%d.%d' % (b,IF_PHYS_ADDRESS,i)] = ('octet', mac_hex)
        tree['%s.%d.%d' % (b,IF_ADMIN_STATUS,i)] = ('integer', str(f['admin_status']))
        tree['%s.%d.%d' % (b,IF_OPER_STATUS,i)] = ('integer', str(f['oper_status']))
        tree['%s.%d.%d' % (b,IF_LAST_CHANGE,i)] = ('timeticks', '0')
        tree['%s.%d.%d' % (b,IF_IN_OCTETS,i)] = ('counter', str(f['in_octets'] % 4294967296))
        tree['%s.%d.%d' % (b,IF_IN_UCAST_PKTS,i)] = ('counter', str(f['in_ucast_pkts'] % 4294967296))
        tree['%s.%d.%d' % (b,IF_IN_NUCAST_PKTS,i)] = ('counter', '0')
        tree['%s.%d.%d' % (b,IF_IN_DISCARDS,i)] = ('counter', str(f['in_discards'] % 4294967296))
        tree['%s.%d.%d' % (b,IF_IN_ERRORS,i)] = ('counter', str(f['in_errors'] % 4294967296))
        tree['%s.%d.%d' % (b,IF_IN_UNKNOWN_PROTOS,i)] = ('counter', '0')
        tree['%s.%d.%d' % (b,IF_OUT_OCTETS,i)] = ('counter', str(f['out_octets'] % 4294967296))
        tree['%s.%d.%d' % (b,IF_OUT_UCAST_PKTS,i)] = ('counter', str(f['out_ucast_pkts'] % 4294967296))
        tree['%s.%d.%d' % (b,IF_OUT_NUCAST_PKTS,i)] = ('counter', '0')
        tree['%s.%d.%d' % (b,IF_OUT_DISCARDS,i)] = ('counter', str(f['out_discards'] % 4294967296))
        tree['%s.%d.%d' % (b,IF_OUT_ERRORS,i)] = ('counter', str(f['out_errors'] % 4294967296))
        tree['%s.%d.%d' % (b,IF_OUT_QLEN,i)] = ('gauge', '0')
        tree['%s.%d.%d' % (b,IF_SPECIFIC,i)] = ('objectid', '.0.0')
    for f in ifaces:
        i = f['index']; b = IFX_TABLE_OID; sm = f['speed'] // 1000000 if f['speed'] else 0
        tree['%s.%d.%d' % (b,IFX_NAME,i)] = ('string', f['name'])
        tree['%s.%d.%d' % (b,IFX_IN_MCAST_PKTS,i)] = ('counter', '0')
        tree['%s.%d.%d' % (b,IFX_IN_BCAST_PKTS,i)] = ('counter', '0')
        tree['%s.%d.%d' % (b,IFX_OUT_MCAST_PKTS,i)] = ('counter', '0')
        tree['%s.%d.%d' % (b,IFX_OUT_BCAST_PKTS,i)] = ('counter', '0')
        tree['%s.%d.%d' % (b,IFX_HC_IN_OCTETS,i)] = ('counter64', str(f['in_octets']))
        tree['%s.%d.%d' % (b,IFX_HC_IN_UCAST_PKTS,i)] = ('counter64', str(f['in_ucast_pkts']))
        tree['%s.%d.%d' % (b,IFX_HC_IN_MCAST_PKTS,i)] = ('counter64', '0')
        tree['%s.%d.%d' % (b,IFX_HC_IN_BCAST_PKTS,i)] = ('counter64', '0')
        tree['%s.%d.%d' % (b,IFX_HC_OUT_OCTETS,i)] = ('counter64', str(f['out_octets']))
        tree['%s.%d.%d' % (b,IFX_HC_OUT_UCAST_PKTS,i)] = ('counter64', str(f['out_ucast_pkts']))
        tree['%s.%d.%d' % (b,IFX_HC_OUT_MCAST_PKTS,i)] = ('counter64', '0')
        tree['%s.%d.%d' % (b,IFX_HC_OUT_BCAST_PKTS,i)] = ('counter64', '0')
        tree['%s.%d.%d' % (b,IFX_LINK_UP_DOWN_TRAP,i)] = ('integer', '1')
        tree['%s.%d.%d' % (b,IFX_HIGH_SPEED,i)] = ('gauge', str(sm))
        tree['%s.%d.%d' % (b,IFX_PROMISCUOUS,i)] = ('integer', '2')
        tree['%s.%d.%d' % (b,IFX_CONNECTOR,i)] = ('integer', '1')
        tree['%s.%d.%d' % (b,IFX_ALIAS,i)] = ('string', f['alias'])
        tree['%s.%d.%d' % (b,IFX_COUNTER_DISC_TIME,i)] = ('timeticks', '0')
    clients = cached_get('status/lan/clients') or []
    # Map clients to LAN network ifIndex by IP subnet
    lan_ifaces = []
    for f in ifaces:
        if f.get('set_admin_type') == 'lan':
            descr = f.get('descr', '')
            net_ip = net_mask = ''
            if '(' in descr and '/' in descr:
                try:
                    inner = descr.split('(')[1].rstrip(')')
                    net_ip, net_mask = inner.split('/')
                except Exception:
                    pass
            lan_ifaces.append((f['index'], net_ip, net_mask))
    default_idx = lan_ifaces[-1][0] if lan_ifaces else 1
    if isinstance(clients, list):
        for c in clients:
            mac = c.get('mac', ''); ip = c.get('ip_address', '')
            if not mac or not ip or ':' in ip:
                continue
            matched_idx = default_idx
            for lidx, net_ip, net_mask in lan_ifaces:
                if net_ip and net_mask and _ip_in_subnet(ip, net_ip, net_mask):
                    matched_idx = lidx
                    break
            os = '%d.%s' % (matched_idx, ip)
            tree['%s.%d.%s' % (ARP_TABLE_OID,ARP_IF_INDEX,os)] = ('integer', str(matched_idx))
            tree['%s.%d.%s' % (ARP_TABLE_OID,ARP_PHYS_ADDRESS,os)] = ('octet', _mac_to_raw(mac))
            tree['%s.%d.%s' % (ARP_TABLE_OID,ARP_NET_ADDRESS,os)] = ('ipaddress', ip)
            tree['%s.%d.%s' % (ARP_TABLE_OID,ARP_TYPE,os)] = ('integer', '3')

    # RFC 1213 atTable (deprecated but widely used) - same data, different index
    # Index: {ifIndex}.1.{IP}
    if isinstance(clients, list):
        for c in clients:
            mac = c.get('mac', ''); ip = c.get('ip_address', '')
            if not mac or not ip or ':' in ip:
                continue
            at_matched = default_idx
            for lidx, net_ip, net_mask in lan_ifaces:
                if net_ip and net_mask and _ip_in_subnet(ip, net_ip, net_mask):
                    at_matched = lidx
                    break
            at_sfx = '%d.1.%s' % (at_matched, ip)
            tree['%s.%d.%s' % (AT_TABLE_OID, AT_IF_INDEX, at_sfx)] = ('integer', str(at_matched))
            tree['%s.%d.%s' % (AT_TABLE_OID, AT_PHYS_ADDRESS, at_sfx)] = ('octet', _mac_to_raw(mac))
            tree['%s.%d.%s' % (AT_TABLE_OID, AT_NET_ADDRESS, at_sfx)] = ('ipaddress', ip)

    # ipAddrTable - maps interface IPs to ifIndexes
    # Index: {IP}
    for f in ifaces:
        descr = f.get('descr', '')
        if '(' in descr and '/' in descr:
            try:
                inner = descr.split('(')[1].rstrip(')')
                if_ip, if_mask = inner.split('/')
                tree['%s.%d.%s' % (IPADDR_TABLE_OID, IPADDR_ADDR, if_ip)] = ('ipaddress', if_ip)
                tree['%s.%d.%s' % (IPADDR_TABLE_OID, IPADDR_IF_INDEX, if_ip)] = ('integer', str(f['index']))
                tree['%s.%d.%s' % (IPADDR_TABLE_OID, IPADDR_NETMASK, if_ip)] = ('ipaddress', if_mask)
                tree['%s.%d.%s' % (IPADDR_TABLE_OID, IPADDR_BCASTADDR, if_ip)] = ('integer', '1')
                tree['%s.%d.%s' % (IPADDR_TABLE_OID, IPADDR_REASMMAX, if_ip)] = ('integer', '65535')
            except Exception:
                pass

    # Q-BRIDGE-MIB dot1qTpFdbTable
    # Index: {vlanId}.{mac as 6 decimal octets}
    if isinstance(clients, list):
        for c in clients:
            mac = c.get('mac', '')
            vlan = c.get('vlan')
            port = c.get('port')
            if not mac or vlan is None:
                continue
            # Convert MAC to 6 decimal octets for OID index
            clean = mac.replace(':', '').replace('-', '')
            if len(clean) != 12:
                continue
            mac_oid = '.'.join(str(int(clean[i:i+2], 16)) for i in range(0, 12, 2))
            idx_suffix = '%d.%s' % (vlan, mac_oid)
            mac_hex = _mac_to_raw(mac)
            tree['%s.%d.%s' % (QBRIDGE_FDB_OID, QFDB_ADDRESS, idx_suffix)] = ('octet', mac_hex)
            tree['%s.%d.%s' % (QBRIDGE_FDB_OID, QFDB_PORT, idx_suffix)] = ('integer', str(port if port is not None else 0))
            tree['%s.%d.%s' % (QBRIDGE_FDB_OID, QFDB_STATUS, idx_suffix)] = ('integer', '3')  # learned

    return tree


def oid_sort_key(oid):
    return tuple(int(p) for p in oid.strip('.').split('.'))


def handle_set(oid, type_str, value):
    prefix = IF_TABLE_OID + '.%d.' % IF_ADMIN_STATUS
    if not oid.startswith(prefix):
        return False
    try:
        if_index = int(oid[len(prefix):])
    except (ValueError, IndexError):
        return False
    _prefetch()
    for f in discover_interfaces():
        if f['index'] == if_index:
            try:
                av = int(value)
            except (ValueError, TypeError):
                return False
            if av not in (1, 2):
                return False
            en = (av == 1); st = f.get('set_admin_type', ''); sp = f.get('set_admin_path', '')
            if not sp:
                return False
            try:
                if st == 'ethernet':
                    cp.put(sp, en); return True
                elif st == 'wan':
                    cp.put('config/wan/rules2/%s/disabled' % sp, not en); return True
            except Exception:
                pass
    return False


def _tree_builder_loop():
    """Background thread: rebuild OID tree every CACHE_TTL seconds."""
    global _oid_tree
    while True:
        try:
            new_tree = build_oid_tree()
            if new_tree:
                with _oid_tree_lock:
                    _oid_tree = new_tree
                _tree_ready.set()
        except Exception:
            pass
        time.sleep(CACHE_TTL)


def _get_tree():
    """Get the current OID tree (waits for first build)."""
    _tree_ready.wait(timeout=30)
    with _oid_tree_lock:
        return _oid_tree


def main():
    # Start background tree builder
    t = threading.Thread(target=_tree_builder_loop, daemon=True)
    t.start()

    while True:
        try:
            line = sys.stdin.readline()
            if not line:
                break
            cmd = line.strip().upper()
            if cmd == 'PING':
                sys.stdout.write('PONG\n'); sys.stdout.flush()
            elif cmd == 'GET':
                oid = sys.stdin.readline().strip()
                tree = _get_tree()
                if oid in tree:
                    vt, vl = tree[oid]
                    sys.stdout.write('%s\n%s\n%s\n' % (oid, vt, vl))
                else:
                    sys.stdout.write('NONE\n')
                sys.stdout.flush()
            elif cmd == 'GETNEXT':
                oid = sys.stdin.readline().strip()
                tree = _get_tree()
                for c in sorted(tree.keys(), key=oid_sort_key):
                    if oid_sort_key(c) > oid_sort_key(oid):
                        vt, vl = tree[c]
                        sys.stdout.write('%s\n%s\n%s\n' % (c, vt, vl)); break
                else:
                    sys.stdout.write('NONE\n')
                sys.stdout.flush()
            elif cmd == 'SET':
                oid = sys.stdin.readline().strip()
                vl = sys.stdin.readline().strip()
                parts = vl.split(' ', 1)
                if len(parts) == 2:
                    sys.stdout.write('DONE\n' if handle_set(oid, parts[0], parts[1]) else 'not-writable\n')
                else:
                    sys.stdout.write('wrong-type\n')
                sys.stdout.flush()
            elif cmd == '':
                break
        except EOFError:
            break
        except Exception as e:
            sys.stderr.write('Error: %s\n' % str(e)); sys.stderr.flush()


if __name__ == '__main__':
    main()
