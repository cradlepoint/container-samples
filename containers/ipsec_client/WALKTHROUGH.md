# Walkthrough: routing a LAN through the ipsec_client container

A start-to-finish setup for sending one LAN's traffic out an IPsec tunnel that a
container maintains. You configure four things, in this order:

1. A **Local IP Network** the container lives on (its own segment, not the LAN you forward).
2. The **compose project** (bind the network by UUID, set the addresses and the VPN credentials).
3. **Routing** on the router (a route table + a route policy that steers the LAN into the container).
4. The **firewall zone** so the forwarded LAN is allowed.

The example throughout uses:

| Thing | Value |
|-------|-------|
| Container's own network ("VPN") | `10.0.0.0/30`, gateway `10.0.0.1` |
| Container address | `10.0.0.2` |
| LAN being forwarded ("guestLAN") | `192.168.10.0/24` |
| Route table name | `VPN` |
| Firewall | Primary LAN Zone set to match "any" LAN |

Substitute your own values. The `/30` is deliberate — the container network only
ever holds the router (`.1`) and the container (`.2`), so a four-address subnet is
all it needs.

> Why a separate network for the container? It must not sit on the LAN it serves.
> `ip rule ... from <served-subnet>` only works while the container's own address
> is *outside* that subnet, and keeping it separate also keeps a routing mistake
> away from the segment you manage the router from. Give it its own Local IP
> Network, as below.

---

## Step 1 — Add the container's Local IP Network ("VPN")

In NCM (or the router UI): **Networking > Local Networks > Local IP Networks > Add**.

- **Name:** `VPN`
- **IP address / netmask:** `10.0.0.1/30` (this is the router's address on the segment; the container takes `10.0.0.2`)
- Attach it to no physical LAN ports — it exists only for the container bridge.
- DHCP is not required on this network; the container gets a static address from compose.

Save it. You now need its UUID for the compose file.

### Get the network's UUID from the Config Summary

The compose file binds to this network by its `_id_` (a UUID), not its name.

- In NCM, open the device's **Configuration Summary** (the full config JSON) and
  find the `VPN` entry under the LAN networks. Copy its `_id_` field.
- Equivalently over the CLI/REST, read `config/lan` and find the object whose
  `name` is `VPN`; its `_id_` is the UUID.

It looks like `00000002-d708-345b-9003-ab73ba0152bd`. Keep it for Step 2.

---

## Step 2 — Configure the compose project

Paste the compose below into **System > Containers > Projects > Add > Compose tab**
in NCM, then edit the marked values.

```yaml
version: "2.4"
services:
  ipsec_client:
    # Build and push this sample's own Dockerfile; do not deploy a third party's
    # image. See "Building the image" below.
    image: "yourregistry/ipsec_client:latest"
    container_name: ipsec_client
    restart: unless-stopped
    # NET_ADMIN: create the TUN device and install the routing/NAT/firewall rules.
    cap_add:
      - NET_ADMIN
    devices:
      - /dev/net/tun:/dev/net/tun
    networks:
      lan:
        ipv4_address: 10.0.0.2          # container's address on the VPN network
    environment:
      - VPN_GATEWAY=always-on.vpn.cyberreefsolutions.com
      - VPN_GATEWAY_ID=CN=always-on.vpn.cyberreefsolutions.com
      - VPN_USERNAME=<your-eap-username>
      - VPN_PASSWORD=<your-eap-password>
      # The LAN you are sending over the tunnel. Replies to this subnet are forced
      # back onto the main table so a full tunnel does not swallow the return path.
      - LAN_SUBNETS=192.168.10.0/24

networks:
  lan:
    driver: bridge
    driver_opts:
      # Paste the VPN network's _id_ (UUID) from Step 1 here.
      com.cradlepoint.network.bridge.uuid: "00000002-d708-345b-9003-ab73ba0152bd"
    ipam:
      config:
        - subnet: 10.0.0.0/30
          gateway: 10.0.0.1            # must match the VPN network's address
```

What you must change:

- **`com.cradlepoint.network.bridge.uuid`** — the `_id_` you copied in Step 1. The
  `subnet` and `gateway` under `ipam` must match the VPN network's configuration
  exactly, or the bind is rejected.
- **`ipv4_address`** — the container's address (`10.0.0.2`), inside the subnet but
  not the gateway.
- **`VPN_USERNAME` / `VPN_PASSWORD`** — the EAP credentials for your gateway.
- **`LAN_SUBNETS`** — the LAN you are forwarding (`192.168.10.0/24`, guestLAN).

Two things to watch on NCOS:

- **A literal `$` in any value must be doubled.** The platform interpolates `$` in
  every compose value, so a password like `P@ss$word` has to be written
  `P@ss$$word` or the `$word` part silently vanishes. A truncated password fails
  as an auth error with no hint why.
- **Credentials are visible to anyone with NCM config access** to the device. That
  is the normal trade for an NCOS-managed container; the container never logs the
  password.

This sample carries more environment variables than the four above (gateway auth
mode, CA pinning, EAP method, proposals, data path, fail-closed). The defaults suit
a certificate-authenticated EAP gateway. See [README.md](README.md) for the full
table if your gateway needs different proposals or a PSK.

### Building the image

Point `image:` at your own registry, not a third party's. Build both
architectures your fleet needs and push:

```bash
cd containers/ipsec_client

# ARMv8 routers: E300, E3000, R920, R980, R1900, R2100
docker buildx build --platform linux/arm64  -t yourregistry/ipsec_client:latest --push .

# ARMv7 routers: AER2200, IBR1700
docker buildx build --platform linux/arm/v7 -t yourregistry/ipsec_client:latest-armv7 --push .
```

Deploy the project. The image pulls over the router's primary WAN — if that is a
cellular modem the pull is slow and billable, so check `status/wan/primary_device`
first.

---

## Step 3 — Route the LAN into the container

The goal is: guestLAN traffic goes to the container (and out the tunnel), while the
router's *own* traffic — including the container's ESP packets to the gateway —
stays on the WAN. Source-based policy routing does exactly that. A plain default
route in the Main table does **not**: it captures the router's own traffic, so the
container's ESP loops straight back into the container and nothing works.

### 3a — Add a route TABLE ("VPN")

**Networking > Routing > Route Tables > Add** (config path `config/routing/tables`):

- **Name:** `VPN`
- Add one route to it:
  - **Destination / IP network:** `0.0.0.0/0`
  - **Gateway:** `10.0.0.2` (the container's address)

This is the "everything goes to the container" table. It is not the Main table, so
it only takes effect for traffic a policy directs at it.

### 3b — Add a route POLICY

**Networking > Routing > Routing Policies > Add** (config path
`config/routing/policies`):

- **Incoming device / source:** select **guestLAN** (the device or subnet you want
  tunnelled — `in_dev` = the guestLAN interface, or `src_ip_network` = `192.168.10.0/24`).
- **Route table:** select **VPN** (the table from 3a).

Now only traffic sourced from guestLAN is sent to the container; the router's
default route on the WAN is untouched, which is what lets the container's ESP reach
the gateway.

> Expect a hairpin: a client sends to the router, which forwards back out the same
> LAN toward the container. That is normal. Confirm on your router that a policy
> route with a container next-hop actually installs, and check which way `priority`
> sorts if you add more than one policy — priority direction is not consistent
> across the NCOS config tree, so verify behaviour rather than assuming.

---

## Step 4 — Update the firewall

The forwarded LAN has to be allowed to reach the container's zone. The quick,
working approach:

- Open the **Primary LAN Zone** firewall zone.
- Set its zone definition to match **"any" LAN** (rather than only the specific
  primary LAN network).

That puts guestLAN and the VPN network in the same trusted zone, so forwarding
between them is permitted. It is the "easy mode" that gets it working; a tighter
setup would define an explicit zone per network and a forwarding rule between them,
but "any LAN" is sufficient here.

---

## Step 5 — Verify

Read the container's log first — it is the status view (there is no web UI or
port). The engine attaches the syslog driver, so the output is in the router log
and `container logs ipsec_client` commonly returns nothing:

```bash
log show -i -s ipsec_client
```

- `PREFLIGHT` lines confirm each platform grant (`CAP_NET_ADMIN`, `/dev/net/tun`).
  If one says `absent` or `FAILED`, fix that before looking anywhere else.
- `SELECTED data path: ...` names kernel (xfrm) or userspace (TUN) ESP.
- `tunnel UP: ...` on a successful negotiation, with the assigned virtual address.

If `container logs` returns nothing, that is expected on the router — the engine
routes container output to the **router log** instead. Read it with:

```bash
log show -i -s ipsec_client
```

Then from a guestLAN client, confirm its public IP is the tunnel's egress (not the
router's WAN). Live SA detail on demand:

```bash
container exec ipsec_client swanctl --list-sas
```

### DNS (do not skip this)

With a full tunnel, clients still get DNS from NCOS DHCP pointing at the router,
which resolves out the **WAN** — so DNS bypasses the tunnel and internal names will
not resolve. The DNS servers the gateway pushes arrive inside the *container*, not
on your clients. Set **DHCP option 6** on guestLAN to a resolver reachable through
the tunnel (read it from the gateway config, or from `/etc/resolv.conf` inside the
container once the tunnel is up).

---

## Quick reference

| Where | What you set |
|-------|--------------|
| Local IP Network `VPN` | `10.0.0.1/30`; copy its `_id_` UUID from the Config Summary |
| Compose `driver_opts` UUID | the VPN network's `_id_` |
| Compose `ipv4_address` / `ipam` | `10.0.0.2`, subnet `10.0.0.0/30`, gateway `10.0.0.1` |
| Compose env | `VPN_USERNAME`, `VPN_PASSWORD`, `LAN_SUBNETS=192.168.10.0/24` |
| Route table `VPN` | route `0.0.0.0/0` gw `10.0.0.2` |
| Route policy | in_dev/src = guestLAN, table = `VPN` |
| Firewall | Primary LAN Zone matches "any" LAN |

## Common failure modes

| Symptom | Likely cause |
|---------|--------------|
| Bridge/network bind rejected on deploy | `subnet`/`gateway`/UUID in compose don't match the VPN Local IP Network exactly |
| Auth fails, password looks correct | An un-doubled `$` in `VPN_PASSWORD` truncated it — double every literal `$` |
| Router itself loses connectivity / ESP loops | A default route was put in the **Main** table instead of a separate policy-routed table |
| Tunnel up, client gets one-way loss | `LAN_SUBNETS` doesn't list the client subnet, so replies route into the tunnel |
| Client has internet but internal names fail | DNS still going out the WAN — set DHCP option 6 (Step 5) |
| `IDr mismatch` in the log | `VPN_GATEWAY_ID` doesn't match what the gateway presents; use the identity the log names |

See [README.md](README.md) for the complete environment-variable reference,
logging detail, and split-tunnel options.
