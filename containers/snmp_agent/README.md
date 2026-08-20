# SNMP Agent Container

An SNMP agent container for Ericsson (Cradlepoint) NCOS routers that provides stable interface indexing (ifIndex) for SNMP monitoring. The router's built-in SNMP agent reassigns ifIndex values when services or interfaces restart, which breaks NMS polling, graphing continuity, and alerting rules that rely on consistent interface identifiers. This container masks that behavior by maintaining its own deterministic interface table derived from the router's Config Store, ensuring ifIndex values remain consistent across service restarts.

## What It Does

This container runs a Net-SNMP daemon (`snmpd`) that rebuilds interface and network tables from the router's Config Store on a fixed schedule rather than relying on the kernel's volatile interface numbering. It uses `pass_persist` handlers to serve a stable OID tree to SNMP managers, and proxies requests for other MIBs to the router's built-in agent.

### Supported MIBs

| OID Subtree | MIB | Description |
|-------------|-----|-------------|
| `.1.3.6.1.2.1.2` | IF-MIB (ifTable) | Interface table with stats for Ethernet, WAN, and LAN interfaces |
| `.1.3.6.1.2.1.3` | RFC 1213 atTable | Legacy ARP table |
| `.1.3.6.1.2.1.4.20` | ipAddrTable | IP address to interface mapping |
| `.1.3.6.1.2.1.4.22` | ipNetToMediaTable | ARP/neighbor table |
| `.1.3.6.1.2.1.17.7` | Q-BRIDGE-MIB | MAC forwarding database (dot1qTpFdbTable) |
| `.1.3.6.1.2.1.31` | IF-MIB (ifXTable) | Extended interface table with 64-bit counters |

Additional subtrees (HOST-RESOURCES, ENTITY-MIB, snmpModules) are proxied to the router's built-in SNMP agent.

### SET Support

The agent supports SNMP SET for `ifAdminStatus` to enable/disable Ethernet ports and WAN interfaces.

## Files

| File | Purpose |
|------|---------|
| `Dockerfile` | Alpine-based image with Net-SNMP and Python |
| `entrypoint.sh` | Generates snmpd.conf from router config, then starts snmpd |
| `gen_conf.py` | Reads SNMP settings from router Config Store and appdata, writes `/etc/snmp/snmpd.conf` |
| `ncos_snmp.py` | Net-SNMP `pass_persist` handler that serves MIB data from the router |
| `cp.py` | NCOS SDK module for Config Store communication |

## Configuration

The agent reads configuration from two sources:

### Router SNMP Config (`config/system/snmp`)
- Community strings (get/set)
- SNMPv3 user, auth password, privacy password
- System contact, name, location

### AppData (user-configurable via NCM)
| Key | Description | Default |
|-----|-------------|---------|
| `snmp_port` | UDP port the agent listens on | `1161` |
| `community_ro` | Additional read-only communities (comma-separated) | — |
| `community_rw` | Additional read-write communities (comma-separated) | — |
| `snmpv3_user` | SNMPv3 username (fallback if not in router config) | — |
| `snmpv3_auth_pass` | SNMPv3 auth password (fallback) | — |
| `snmpv3_priv_pass` | SNMPv3 privacy password (fallback) | — |

## Building

```bash
# For ARMv8 64-bit routers (E300, E3000, R920, R980, R1900, R2100)
docker buildx build --platform linux/arm64 -t snmp-agent:latest .

# For ARMv7 32-bit routers (AER2200, IBR1700)
docker buildx build --platform linux/arm/v7 -t snmp-agent:latest .
```

## Deployment

1. Push the image to a container registry accessible by the router
2. In NetCloud Manager, create a container project with:
   - **Image**: your registry/image:tag
   - **Port mapping**: `1161:1161/udp`
   - **Config Store volume**: Enabled (required for cp.py)
3. Configure appdata values as needed
4. Commit and deploy

## How It Works

1. `entrypoint.sh` runs `gen_conf.py` to generate `snmpd.conf`
2. `gen_conf.py` reads router SNMP config and appdata via `cp.py` / `cs.sock`
3. `snmpd` starts with the generated config, excluding built-in modules that are replaced by `pass_persist`
4. `ncos_snmp.py` runs as a `pass_persist` handler, building an OID tree from router status data every 10 seconds in a background thread
5. SNMP requests for interface, ARP, and bridge data are served from the OID tree
6. Requests for other subtrees are proxied to the router's built-in SNMP agent
