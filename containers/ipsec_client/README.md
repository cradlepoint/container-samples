# ipsec_client

An IKEv2 IPsec client for a dialup gateway that requires a **username and
password over EAP**, with the gateway authenticating itself by **certificate or
pre-shared key**. It then gives LAN clients behind the router an outbound path
through the tunnel by forwarding and NATing their traffic.

Authentication always runs in userspace — charon performs IKEv2 and EAP itself in
every configuration. What is selectable is the **ESP data path**: kernel ESP with
an XFRM interface where the kernel supports it, or userspace ESP over a TUN device
where it does not. Both produce an interface with the same name, so the firewall,
NAT and MSS rules are identical either way.

## What it does and does not do

- **Outbound only.** Every LAN client is translated to the single virtual IP the
  gateway assigns, so the remote side cannot initiate connections inward and has
  no per-client visibility. This is NAT, not routed site-to-site.
- **Fail-closed by default.** While the tunnel is down, LAN clients have no path
  at all. They do not silently fall back to the router's WAN in cleartext.
- **Self-maintaining.** A watchdog re-initiates whenever no CHILD_SA is
  installed, which covers teardowns that charon's own `dpd_action` and
  `close_action` do not.
- **Self-preflighting.** On startup it checks each platform grant it depends on
  and refuses to run with a partial data path. Read those lines first when
  something is wrong.
- **Observable from the log.** There is no web UI and no published port. Every
  state change is logged when it happens, with a periodic line restating an
  unchanged state, so `container logs ipsec_client` is the status view.

## Files

- `containers/ipsec_client/Dockerfile` — Debian 12-slim base, with a comment
  explaining why not Alpine (Alpine's `strongswan` package ships no `libipsec`
  plugin, so userspace ESP is unavailable there)
- `containers/ipsec_client/entrypoint.sh` — validation, preflight, config
  generation, firewall and policy routing, charon supervision and the watchdog
- `containers/ipsec_client/docker-compose.yml` — local build and run
- `containers/ipsec_client/docker-compose.cradlepoint.yml` — NCOS deployment,
  including the Local IP Network binding

No `cp.py` and no `$CONFIG_STORE`: nothing here reads or writes router state.

## Configuration

All configuration is environment variables. The required ones have no defaults,
and the container exits immediately naming any that are missing rather than
starting in a half-configured state.

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `VPN_GATEWAY` | yes | — | Gateway address or hostname |
| `VPN_USERNAME` | yes | — | EAP username |
| `VPN_PASSWORD` | yes | — | EAP password |
| `LAN_SUBNETS` | yes | — | Comma-separated LAN subnets whose clients route through this container |
| `VPN_GATEWAY_AUTH` | no | `pubkey` | How the gateway authenticates itself: `pubkey` (certificate) or `psk` |
| `VPN_PSK` | only if `psk` | — | Pre-shared key. Not used, and not emitted into the config, with `pubkey` |
| `VPN_CA_CERT_B64` | no | — | Base64 PEM of the CA to pin (`base64 -w0 ca.pem`). Falls back to the system trust store |
| `VPN_GATEWAY_ID` | no | `$VPN_GATEWAY` | Identity the gateway presents — normally the FQDN in its certificate |
| `VPN_LOCAL_ID` | no | `$VPN_USERNAME` | EAP identity sent to the gateway |
| `VPN_EAP_METHOD` | no | `eap-mschapv2` | `eap-mschapv2`, `eap-md5`, `eap-gtc`, `eap-tls`, `eap-ttls` |
| `VPN_REMOTE_TS` | no | `0.0.0.0/0` | Remote traffic selector. Narrow this for a split tunnel |
| `VPN_IKE_PROPOSALS` | no | see entrypoint | IKE proposals, must overlap the gateway's phase 1 |
| `VPN_ESP_PROPOSALS` | no | see entrypoint | ESP proposals, must overlap phase 2. No DH group, so it matches a gateway with PFS disabled |
| `VPN_DATA_PATH` | no | `auto` | `auto`, `xfrm` (kernel ESP) or `userspace` (TUN). An explicit value is never silently downgraded |
| `VPN_IF_NAME` | no | `ipsec0` | Tunnel interface name, same for both data paths |
| `VPN_XFRM_IF_ID` | no | `42` | XFRM interface `if_id`, xfrm mode only |
| `VPN_XFRM_LINK` | no | default-route device | Underlying link for the XFRM interface |
| `VPN_ROUTE_TABLE` | no | `300` | Routing table used for tunnelled traffic in xfrm mode |
| `VPN_DPD_DELAY` | no | `30s` | Dead peer detection interval |
| `VPN_MSS_CLAMP` | no | `1` | Clamp TCP MSS to path MTU on the tunnel |
| `VPN_FAIL_CLOSED` | no | `1` | Drop forwarded traffic when the tunnel is down |
| `VPN_WATCHDOG_INTERVAL` | no | `20` | Seconds between SA checks |
| `VPN_STATUS_INTERVAL` | no | `300` | Seconds between periodic status lines. `0` logs transitions only |

`VPN_PSK` and `VPN_PASSWORD` are never logged, and the generated `swanctl.conf` is
written mode 600. They are still visible to anyone with NCM access to the device's
configuration, which is the normal trade for NCOS-managed containers.

### Matching the gateway

The proposals, EAP method and traffic selector have to match what the gateway is
configured for. Get the gateway's own configuration rather than working from
assumptions, and if a client that already connects to it exists — a phone profile,
another vendor's client — read that too: it is a specification that has already
been proven against the live gateway.

Three things are negotiated automatically and have no setting: the container
requests a virtual IP and installs it, NAT-T is detected and ESP is
UDP-encapsulated, and IKEv2 is the only version offered.

The gateway's identity is worth checking rather than assuming: it is normally the
FQDN in its certificate, but a gateway configured with an explicit local
identity may send that instead. On a mismatch, charon logs the identity it
actually received, so set `VPN_GATEWAY_ID` to whatever appears there.

DNS servers the gateway pushes arrive in the *container*, not on your clients —
see the DNS step under deployment.

## Building

Use the same name everywhere — directory, image, compose service and
`container_name` are all `ipsec_client`.

```bash
cd containers/ipsec_client

# ARMv8 routers: E300, E3000, R920, R980, R1900, R2100
docker buildx build --platform linux/arm64 -t yourregistry/ipsec_client:latest --push .

# ARMv7 routers: AER2200, IBR1700
docker buildx build --platform linux/arm/v7 -t yourregistry/ipsec_client:latest-armv7 --push .
```

Measured image sizes: 161 MB (arm64), 98.7 MB (arm/v7). That is flash and pull
cost; resident memory with a tunnel established is single-digit megabytes, well
under the `mem_limit: 128M` in the compose files.

About 30 MB of that is `libcurl4` and its transitive closure (krb5/GSSAPI, LDAP,
nghttp2, psl, rtmp, ssh2, brotli, zstd), pulled in because the CRL/OCSP fetcher
plugin needs it. It buys working revocation checking. If flash or pull time over a
metered WAN matters more than revocation for your deployment, dropping
`libstrongswan-extra-plugins` from the Dockerfile reverts to roughly 130 MB and
70 MB — at the cost of `no capable fetcher found` and revocation never being
verified.

## Deploying on NCOS

1. **Give the container an address on a real LAN segment.** LAN clients must
   reach it by IP, so the default `172.17.x` bridge will not work. The Compose
   file defines a network bound to an NCM Local IP Network via
   `com.cradlepoint.network.bridge.uuid`; set that UUID and make `subnet` and
   `gateway` match the Local IP Network exactly, then set `ipv4_address` to the
   address the container should own.

2. **Grant the capability and the device.** `cap_add: NET_ADMIN` and
   `devices: [/dev/net/tun:/dev/net/tun]`. If either is refused, the preflight
   lines say which one.

3. **Get LAN traffic to the container.** Three mechanisms, and the right one
   depends on whether the tunnel is full or split.

   **Policy routing on the router (preferred for a full tunnel).** NCOS supports
   source-based policy routing, which sends LAN traffic to the container while the
   router keeps its own default route on the WAN — which matters, because the
   container's own ESP packets have to reach the gateway over that WAN path:

   - `config/routing/tables` — add a table (e.g. `tunnel`) with one route:
     `ip_network: 0.0.0.0/0`, `gw:` the container's `ipv4_address`
   - `config/routing/policies` — add an entry with `src_ip_network` set to the LAN
     subnet (or `in_dev` set to the LAN interface) and `table` set to that table's
     UUID

   No client-side configuration, so statically addressed devices are covered too.
   Two things to confirm on your router, because the DTD proves only that the
   fields and types exist: that a policy route with a container next-hop is
   actually installed, and which direction `priority` sorts — the shipped default
   policy uses `priority: 0` pointing at the Main table, and priority semantics are
   not consistent across the NCOS config tree, so check behaviour rather than
   assuming. Expect a hairpin: the client sends to the router, which forwards back
   out the same LAN interface to the container. That works, and the router may emit
   ICMP redirects pointing clients at the container directly.

   **DHCP option 3 (alternative for a full tunnel).** Hand clients the container
   as their default gateway, so they reach it directly with no hairpin and no
   router routing config:

   - `config/lan/dhcpd/options` — add `option: 3`, `value:` the container's
     `ipv4_address`
   - `config/lan/dhcpd/options_enabled` — `true`

   Only DHCP clients are covered; statically addressed devices need their gateway
   set by hand. Do **not** try to achieve the same thing with a default route in
   the router's Main table pointing at the container: that captures the router's
   own traffic, including the container's ESP packets to the gateway, which then
   loop back to the container.

   **Split tunnel** (`VPN_REMOTE_TS` narrowed to specific remote subnets). A plain
   static route is enough and is the simplest option: add the remote subnets to
   `config/routing/tables[name=Main]/routes` with `gw` set to the container's
   address. The router's default stays untouched, so there is nothing to work
   around. DHCP option 121 (classless static routes) achieves the same from the
   client side if you would rather not touch the router's routing table.

4. **Deal with DNS.** Clients get DNS from NCOS DHCP pointing at the router,
   which resolves out the WAN — so with a full tunnel, DNS bypasses the tunnel.
   Internal names will not resolve and query metadata leaves outside the tunnel.
   The servers the gateway pushes via mode-config arrive in the *container*, not
   on your clients, so set DHCP option 6 (`config/lan/dhcpd/options`) to whatever
   the gateway pushes — read it from the gateway's own configuration, or from
   `/etc/resolv.conf` inside the container once the tunnel is up.

5. **Read the log before anything else.**

   ```bash
   container logs ipsec_client
   ```

   The `PREFLIGHT` lines confirm or deny each platform grant, and the
   authentication lines show which round succeeded.

## Status and logging

Everything goes to stdout and stderr, which the container runtime collects via the
`json-file` driver. Lines are printed bare — no timestamp and no source prefix —
because the driver already records a timestamp per line and the log is per
container, so repeating either in the message only makes it harder to read.

Retrieve it with `container logs ipsec_client`. The lines also surface in a
router-side log view, where the carrier prefixes each one with a timestamp, a
level and the container name — which is why the messages here are bare:

```
07:37:10 AM INFO ipsec_client SELECTED data path: userspace (interface ipsec0)
```

There is no web interface and no published port. `container logs ipsec_client` is
the status view, and `container exec ipsec_client swanctl --list-sas` gives live
detail on demand.

What gets logged, and when:

| Line | When |
|------|------|
| Configuration summary, `PREFLIGHT ...`, `SELECTED data path` | Once at startup |
| `FORWARD policy DROP`, `route:`, `policy route:` | Once, as the data plane is installed |
| `tunnel UP: ...` followed by the SA detail and the assigned address | On each transition to established |
| `tunnel DOWN: ...` | On each transition away from established, naming the fail-closed state |
| `watchdog: ... re-initiating (attempt N)` | First attempt, then every tenth |
| `status: tunnel up\|down, ...` | Every `VPN_STATUS_INTERVAL` seconds |
| `ERROR: charon exited` | Immediately before exiting non-zero so the restart policy fires |

Two deliberate choices worth knowing:

- **Transitions are logged once, not repeated.** A tunnel that stays up produces
  one `tunnel UP` line and then only the periodic `status:` heartbeat. The
  heartbeat exists so a healthy tunnel still proves the watchdog is running;
  set `VPN_STATUS_INTERVAL=0` to log transitions only.
- **Repeated recovery attempts are throttled.** An unreachable gateway would
  otherwise emit a pair of lines every `VPN_WATCHDOG_INTERVAL` forever, into a
  log shared with the rest of the router. The first attempt and every tenth are
  logged, and the line says so.

Expect one `loaded certificate` line per trust anchor at startup — that is the
trust store being read, not an error. With the system CA bundle fallback it is
around 150 lines.

There should be **no** `plugin '<name>': failed to load` lines. If any appear,
read them rather than dismissing them as noise: that block is where a plugin your
configuration actually needs would hide, and its failure surfaces much later as an
authentication or crypto error. To decide whether a given one matters, check it
against what the tunnel actually negotiated (`swanctl --list-sas` names the IKE and
ESP algorithms) and against the plugins this container depends on: `openssl`,
`x509`, `revocation`, `curl`, `eap-mschapv2`, and `kernel-libipsec` or
`kernel-netlink` for the selected data path. Confirm what is genuinely loaded with:

```bash
container exec ipsec_client swanctl --stats
```

File presence is a different and weaker test — a plugin can exist on disk, be
configured to load, and silently not load on an unmet dependency.

Certificate revocation is checked when the gateway authenticates with a
certificate: the `curl` fetcher is installed, so charon retrieves CRLs and OCSP
responses. Enforcement is strongSwan's default `relaxed`, which rejects a
certificate only when it is *known* revoked — an unreachable responder logs a
warning and establishment continues. Those fetches leave via the container's WAN
route before the tunnel exists, so they are outbound traffic on a possibly metered
link. See the Dockerfile comment for `ifuri` and `strict` if your policy requires
revocation status to be available rather than merely consulted.

## Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| `PREFLIGHT absent : /dev/net/tun` | `devices:` mapping absent or not honoured; userspace data path unavailable |
| `PREFLIGHT absent : kernel XFRM interface support` | This kernel has no `xfrm` link type; `auto` falls back to userspace |
| `PREFLIGHT FAILED : CAP_NET_ADMIN ...` | `cap_add: NET_ADMIN` absent or not honoured |
| `no trusted RSA public key found for '<id>'` | Wrong or missing CA. Pin the issuing CA with `VPN_CA_CERT_B64` |
| `IDr mismatch` or authentication failure naming another identity | `VPN_GATEWAY_ID` does not match what the gateway sends; use the identity from the log |
| `PREFLIGHT FAILED : net.ipv4.ip_forward=0` | `/proc/sys` is read-only in a container, so add a compose `sysctls:` entry; if the engine ignores it, routing clients through a container is not possible |
| `required configuration is unset: ...` | Missing environment variables, named in the message |
| Auth fails and `eap-mschapv2` is absent from charon's plugin list | `libstrongswan-standard-plugins` missing, so MD4 for the NT hash is unavailable |
| Tunnel establishes, clients get one-way loss | `LAN_SUBNETS` does not list the client subnet, so replies are routed into the tunnel instead of back to the LAN |
| Small packets work, large ones hang | MTU. Confirm `VPN_MSS_CLAMP=1` and that the gateway is not dropping ICMP |
| `unauthorized` / `denied` pulling the image | Compare the pushed tag against `image:` character by character, then check the repository is not private |

## Limitations worth weighing

- **All LAN internet traffic crosses userspace ESP in one container.** Crypto is
  single-threaded in userspace; throughput on router hardware has not been
  measured and will be well below the native tunnel's.
- **No NCOS integration.** No WAN failover participation, no IP verify, no
  tunnel state in NCM. The watchdog and health check are the only recovery.
- **Fail-closed means a hard dependency.** When the tunnel is down, LAN clients
  configured to use this container have no connectivity at all.
- **Outbound only.** Nothing at the remote site can initiate a connection to a
  LAN host.
