#!/bin/sh
# IKEv2 client: PSK for the gateway, username/password (EAP) for this side.
# Forwards and NATs LAN clients through the tunnel, fail-closed.
#
# Authentication always runs in userspace -- charon does IKEv2 and EAP itself in
# every configuration, and that is not a mode you select. What IS selectable is
# the ESP data path:
#
#   xfrm       kernel ESP with an XFRM interface (if_id). Crypto in the kernel,
#              so no per-packet userspace round trip. Needs the kernel to have
#              the XFRM subsystem and XFRM interface support.
#   userspace  ESP processed in userspace over a TUN device (kernel-libipsec).
#              Needs only /dev/net/tun. Works where kernel IPsec does not, at
#              the cost of throughput.
#
# Both produce an interface with the SAME name, deliberately: every firewall,
# NAT and MSS rule below is keyed on that interface, so switching data paths
# does not silently void the fail-closed guarantee. Bare policy-mode kernel
# IPsec would have no interface at all, which is exactly why this uses an XFRM
# interface rather than plain policy mode.
#
# VTI is not offered. It is superseded by XFRM interfaces (Linux 4.19+) and its
# conventional setup needs per-interface sysctls such as disable_policy, which
# cannot be set from a container because /proc/sys is mounted read-only --
# verified here by the preflight, though the sysctl requirement itself is
# upstream documentation rather than something this sample tested.
#
# Order of operations is deliberate:
#   1. validate config          -- absent config must not look like a runtime fault
#   2. preflight the platform   -- report exactly which grant or kernel feature is missing
#   3. pick the data path       -- explicit request is honoured or fails; auto falls back
#   4. create the interface     -- before the firewall, so rules have their target
#   5. install the firewall     -- BEFORE the tunnel, so no window exists where LAN
#                                  traffic can leave in the clear
#   6. start charon             -- it establishes and re-establishes
#   7. supervise + watchdog     -- charon death exits non-zero; a missing SA is re-initiated
set -eu

log() { echo "ipsec_client: $*"; }
fail() { echo "ipsec_client: ERROR: $*" >&2; exit 1; }

# ---------------------------------------------------------------- configuration
: "${VPN_GATEWAY:=}"
: "${VPN_PSK:=}"
: "${VPN_USERNAME:=}"
: "${VPN_PASSWORD:=}"
: "${LAN_SUBNETS:=}"

: "${VPN_GATEWAY_ID:=${VPN_GATEWAY}}"
: "${VPN_LOCAL_ID:=${VPN_USERNAME}}"
: "${VPN_EAP_METHOD:=eap-mschapv2}"
: "${VPN_REMOTE_TS:=0.0.0.0/0}"
# Defaults match a common FortiGate dialup profile: phase 1 aes256-sha256 with
# dhgrp 14, phase 2 aes256-sha256 with pfs disabled -- which is why the ESP
# proposal carries no DH group. Adding one where the gateway has PFS off makes
# the CHILD_SA proposal fail to match.
: "${VPN_IKE_PROPOSALS:=aes256-sha256-modp2048,aes128-sha256-modp2048,default}"
: "${VPN_ESP_PROPOSALS:=aes256-sha256,aes128-sha256,default}"

# How the *gateway* authenticates itself: pubkey (certificate) or psk.
# Certificate is the common case for FortiGate dialup with EAP -- `authmethod:
# signature` with a `certificate:` entry and no psksecret. This side always
# authenticates with EAP either way.
: "${VPN_GATEWAY_AUTH:=pubkey}"
: "${VPN_CA_CERT_B64:=}"
: "${VPN_DPD_DELAY:=30s}"
: "${VPN_MSS_CLAMP:=1}"
: "${VPN_FAIL_CLOSED:=1}"
: "${VPN_WATCHDOG_INTERVAL:=20}"

# auto | xfrm | userspace
: "${VPN_DATA_PATH:=auto}"
: "${VPN_IF_NAME:=ipsec0}"
: "${VPN_XFRM_IF_ID:=42}"
: "${VPN_XFRM_LINK:=}"
: "${VPN_ROUTE_TABLE:=300}"

case "$VPN_GATEWAY_AUTH" in
    pubkey|psk) ;;
    *) fail "VPN_GATEWAY_AUTH=${VPN_GATEWAY_AUTH} is not one of: pubkey, psk" ;;
esac

missing=''
[ -n "$VPN_GATEWAY" ]  || missing="$missing VPN_GATEWAY"
[ -n "$VPN_USERNAME" ] || missing="$missing VPN_USERNAME"
[ -n "$VPN_PASSWORD" ] || missing="$missing VPN_PASSWORD"
[ -n "$LAN_SUBNETS" ]  || missing="$missing LAN_SUBNETS"
if [ "$VPN_GATEWAY_AUTH" = psk ] && [ -z "$VPN_PSK" ]; then
    missing="$missing VPN_PSK"
fi
if [ -n "$missing" ]; then
    fail "required configuration is unset:${missing}. Nothing was started. See README."
fi

case "$VPN_DATA_PATH" in
    auto|xfrm|userspace) ;;
    *) fail "VPN_DATA_PATH=${VPN_DATA_PATH} is not one of: auto, xfrm, userspace" ;;
esac

# Never log VPN_PSK or VPN_PASSWORD. Identities are not secret.
log "gateway=${VPN_GATEWAY} gateway_id=${VPN_GATEWAY_ID} local_id=${VPN_LOCAL_ID}"
log "eap_method=${VPN_EAP_METHOD} remote_ts=${VPN_REMOTE_TS} lan_subnets=${LAN_SUBNETS}"
log "gateway auth: ${VPN_GATEWAY_AUTH} (this side always authenticates with EAP)"
log "requested data path: ${VPN_DATA_PATH}"

# ------------------------------------------------------------- trust anchor
# Only needed when the gateway authenticates with a certificate. swanctl loads
# every file in x509ca as a trusted CA.
if [ "$VPN_GATEWAY_AUTH" = pubkey ]; then
    mkdir -p /etc/swanctl/x509ca
    if [ -n "$VPN_CA_CERT_B64" ]; then
        echo "$VPN_CA_CERT_B64" | base64 -d > /etc/swanctl/x509ca/gateway-ca.pem \
            || fail "VPN_CA_CERT_B64 is not valid base64"
        grep -q 'BEGIN CERTIFICATE' /etc/swanctl/x509ca/gateway-ca.pem \
            || fail "VPN_CA_CERT_B64 decoded to something that is not a PEM certificate"
        log "trust anchor: pinned CA from VPN_CA_CERT_B64"
    elif [ -d /etc/ssl/certs ] && [ -n "$(ls /etc/ssl/certs/*.pem 2>/dev/null | head -1)" ]; then
        cp /etc/ssl/certs/*.pem /etc/swanctl/x509ca/ 2>/dev/null || true
        log "trust anchor: system CA bundle ($(ls /etc/swanctl/x509ca | wc -l) CAs)"
        log "         NOTE: any publicly trusted CA issuing a certificate for"
        log "         '${VPN_GATEWAY_ID}' would be accepted. Set VPN_CA_CERT_B64 to pin"
        log "         the issuing CA instead -- that is strictly stronger."
    else
        fail "VPN_GATEWAY_AUTH=pubkey needs a trust anchor: set VPN_CA_CERT_B64, or install ca-certificates in the image."
    fi
fi

# ------------------------------------------------------------------- preflight
# Each check names the grant or kernel feature it tests, so one deployment
# answers the platform questions from `container logs <name>`. Capability grants
# and kernel configuration cannot be established from a development machine --
# the router's engine and kernel are the only authority.
preflight_failed=0
check() {
    label="$1"; shift
    if "$@" >/dev/null 2>&1; then
        log "PREFLIGHT ok      : ${label}"
        return 0
    fi
    log "PREFLIGHT FAILED  : ${label}"
    preflight_failed=1
    return 1
}
probe() {
    # Like check(), but informational: reports capability without failing startup.
    label="$1"; shift
    if "$@" >/dev/null 2>&1; then
        log "PREFLIGHT ok      : ${label}"
        return 0
    fi
    log "PREFLIGHT absent  : ${label}"
    return 1
}

check "CAP_NET_ADMIN can add a policy routing rule" \
    sh -c 'ip rule add to 192.0.2.0/24 lookup main pref 32000 && ip rule del to 192.0.2.0/24 lookup main pref 32000'
check "netfilter nat table writable" \
    sh -c 'iptables -t nat -N preflight && iptables -t nat -X preflight'
check "netfilter filter policy writable" \
    sh -c 'iptables -t filter -N preflight && iptables -t filter -X preflight'

ip_forward="$(cat /proc/sys/net/ipv4/ip_forward 2>/dev/null || echo unknown)"
if [ "$ip_forward" = "1" ]; then
    log "PREFLIGHT ok      : net.ipv4.ip_forward=1"
else
    log "PREFLIGHT FAILED  : net.ipv4.ip_forward=${ip_forward} -- /proc/sys is read-only in a"
    log "                    container, so this cannot be set from here. Add a compose"
    log "                    'sysctls:' entry; if the engine ignores it, routing clients"
    log "                    through this container is not possible."
    preflight_failed=1
fi

# Data-path capabilities. Probed, not required: which one is missing decides the
# data path rather than aborting, unless one was explicitly requested.
tun_ok=0
if [ -c /dev/net/tun ]; then
    log "PREFLIGHT ok      : /dev/net/tun present (devices: mapping honoured)"
    if probe "CAP_NET_ADMIN can create a TUN device (userspace data path)" \
        sh -c 'ip tuntap add dev preflight0 mode tun && ip link del preflight0'; then
        tun_ok=1
    fi
else
    log "PREFLIGHT absent  : /dev/net/tun -- userspace data path unavailable"
fi

xfrm_ok=0
if probe "kernel XFRM subsystem reachable" ip xfrm state; then
    if probe "kernel XFRM interface support (xfrm link type)" \
        sh -c 'ip link add preflightx type xfrm dev lo if_id 999 && ip link del preflightx'; then
        xfrm_ok=1
    fi
fi

if [ "$preflight_failed" -ne 0 ]; then
    fail "preflight failed -- see PREFLIGHT lines above. Refusing to start with a partial data path."
fi

# ------------------------------------------------------------ data path choice
# An explicit request is never silently downgraded: being quietly given the
# slower path when the fast one was asked for is the kind of surprise that shows
# up as a throughput complaint months later.
case "$VPN_DATA_PATH" in
    xfrm)
        [ "$xfrm_ok" -eq 1 ] || fail "VPN_DATA_PATH=xfrm but this kernel has no XFRM interface support (see PREFLIGHT lines). Use auto to fall back, or userspace explicitly."
        DATA_PATH=xfrm
        ;;
    userspace)
        [ "$tun_ok" -eq 1 ] || fail "VPN_DATA_PATH=userspace but no usable /dev/net/tun (see PREFLIGHT lines)."
        DATA_PATH=userspace
        ;;
    auto)
        if [ "$xfrm_ok" -eq 1 ]; then
            DATA_PATH=xfrm
        elif [ "$tun_ok" -eq 1 ]; then
            DATA_PATH=userspace
        else
            fail "auto: neither kernel XFRM interfaces nor /dev/net/tun are available. No data path is possible; see PREFLIGHT lines."
        fi
        ;;
esac
log "SELECTED data path: ${DATA_PATH} (interface ${VPN_IF_NAME})"

# ------------------------------------------------------- strongSwan plugin load
if [ "$DATA_PATH" = userspace ]; then
    # kernel-libipsec must load ahead of kernel-netlink so its userspace ESP
    # implementation registers as the kernel-ipsec backend first. kernel-netlink
    # stays loaded because it still provides the kernel-net interface.
    cat > /etc/strongswan.d/zz-ipsec_client.conf <<EOF
charon {
    filelog {
        stderr {
            default = 1
            ike = 1
            cfg = 1
            knl = 1
        }
    }
    plugins {
        kernel-libipsec {
            load = 10
            tun_name = ${VPN_IF_NAME}
        }
        kernel-netlink {
            load = 5
        }
    }
    install_routes = yes
}
EOF
else
    # Kernel ESP. install_routes = no because an XFRM interface is route-based:
    # routing is set up explicitly below, in a dedicated table selected by
    # source, which keeps the container's own ESP packets on the main table and
    # therefore out of the tunnel they are building.
    cat > /etc/strongswan.d/zz-ipsec_client.conf <<EOF
charon {
    filelog {
        stderr {
            default = 1
            ike = 1
            cfg = 1
            knl = 1
        }
    }
    plugins {
        kernel-libipsec {
            load = no
        }
        kernel-netlink {
            load = yes
        }
    }
    install_routes = no
    install_virtual_ip_on = ${VPN_IF_NAME}
}
EOF
fi

# --------------------------------------------------------------- swanctl config
# Two authentication rounds: the gateway proves itself with the PSK, this side
# then authenticates the user over EAP. start_action=start plus the restart
# actions mean charon establishes and re-establishes on its own; the watchdog
# below covers the teardowns those actions do not fire on.
if [ "$DATA_PATH" = xfrm ]; then
    IF_ID_LINES="        if_id_in = ${VPN_XFRM_IF_ID}
        if_id_out = ${VPN_XFRM_IF_ID}"
else
    IF_ID_LINES=""
fi

# A PSK section is only emitted when the gateway actually uses one. With
# certificate auth there is no shared secret to hold, and an empty one would be
# a confusing artifact in the generated config.
if [ "$VPN_GATEWAY_AUTH" = psk ]; then
    PSK_SECRET_BLOCK="    ike-gateway {
        secret = \"${VPN_PSK}\"
        id = \"${VPN_GATEWAY_ID}\"
    }
"
else
    PSK_SECRET_BLOCK=""
fi

mkdir -p /etc/swanctl/conf.d
umask 077
cat > /etc/swanctl/conf.d/vpn.conf <<EOF
connections {
    vpn {
        version = 2
        remote_addrs = ${VPN_GATEWAY}
        vips = 0.0.0.0
        proposals = ${VPN_IKE_PROPOSALS}
        dpd_delay = ${VPN_DPD_DELAY}
        fragmentation = yes
${IF_ID_LINES}
        local-eap {
            auth = ${VPN_EAP_METHOD}
            eap_id = "${VPN_LOCAL_ID}"
        }
        remote-gw {
            auth = ${VPN_GATEWAY_AUTH}
            id = "${VPN_GATEWAY_ID}"
        }
        children {
            tunnel {
                remote_ts = ${VPN_REMOTE_TS}
                esp_proposals = ${VPN_ESP_PROPOSALS}
                start_action = start
                dpd_action = restart
                close_action = restart
            }
        }
    }
}
secrets {
${PSK_SECRET_BLOCK}    eap-user {
        id = "${VPN_USERNAME}"
        secret = "${VPN_PASSWORD}"
    }
}
EOF
umask 022
log "wrote /etc/swanctl/conf.d/vpn.conf (mode 600)"

# --------------------------------------------------------- tunnel interface
# In xfrm mode the interface is ours to create and exists before any SA. In
# userspace mode charon creates the TUN itself, named by tun_name above.
if [ "$DATA_PATH" = xfrm ]; then
    link="$VPN_XFRM_LINK"
    if [ -z "$link" ]; then
        link="$(ip -o route show default 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1); exit}')"
        [ -n "$link" ] || link=lo
    fi
    ip link add "$VPN_IF_NAME" type xfrm dev "$link" if_id "$VPN_XFRM_IF_ID" \
        || fail "could not create XFRM interface ${VPN_IF_NAME} on ${link}"
    ip link set "$VPN_IF_NAME" up
    log "created XFRM interface ${VPN_IF_NAME} (link ${link}, if_id ${VPN_XFRM_IF_ID})"
fi

# ------------------------------------------------------------------- data plane
# Installed before charon starts. Netfilter accepts rules naming an interface
# that does not exist yet, and the DROP policy is what guarantees nothing leaves
# in the clear during startup or after the tunnel drops. These rules are
# identical for both data paths -- that is the point of giving both the same
# interface name.
setup_firewall() {
    if [ "$VPN_FAIL_CLOSED" = "1" ]; then
        iptables -P FORWARD DROP
        log "FORWARD policy DROP (fail-closed: LAN clients have no path while the tunnel is down)"
    else
        log "WARNING: VPN_FAIL_CLOSED=0 -- traffic will fall back to the router and leave"
        log "         this container unencrypted whenever the tunnel is down."
    fi

    iptables -A FORWARD -o "$VPN_IF_NAME" -j ACCEPT
    iptables -A FORWARD -i "$VPN_IF_NAME" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

    # All LAN clients are translated to the address the gateway assigns, which
    # is what makes this outbound-only: the far side cannot initiate inward.
    iptables -t nat -A POSTROUTING -o "$VPN_IF_NAME" -j MASQUERADE

    if [ "$VPN_MSS_CLAMP" = "1" ]; then
        iptables -t mangle -A FORWARD -o "$VPN_IF_NAME" \
            -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu
        log "MSS clamped to path MTU on ${VPN_IF_NAME}"
    fi
}

setup_routing() {
    if [ "$DATA_PATH" = userspace ]; then
        # charon installs its own routes in table 220 behind a "from all" rule.
        # With a catch-all remote_ts that default also matches replies destined
        # for the LAN, sending return traffic back up the tunnel -- every client
        # then sees one-way loss while the far end looks healthy.
        for subnet in $(echo "$LAN_SUBNETS" | tr ',' ' '); do
            [ -n "$subnet" ] || continue
            ip rule add to "$subnet" lookup main pref 100
            log "policy route: replies to ${subnet} use the main table, not the tunnel"
        done
        return
    fi

    # xfrm mode: routing is explicit, in a table selected by source address, so
    # only forwarded LAN traffic uses the tunnel. The container's own packets --
    # including the ESP it sends to the gateway -- stay on the main table, which
    # is what prevents a catch-all route looping the tunnel through itself.
    for prefix in $(echo "$VPN_REMOTE_TS" | tr ',' ' '); do
        [ -n "$prefix" ] || continue
        ip route add "$prefix" dev "$VPN_IF_NAME" table "$VPN_ROUTE_TABLE"
        log "route: ${prefix} via ${VPN_IF_NAME} in table ${VPN_ROUTE_TABLE}"
    done
    for subnet in $(echo "$LAN_SUBNETS" | tr ',' ' '); do
        [ -n "$subnet" ] || continue
        ip rule add from "$subnet" lookup "$VPN_ROUTE_TABLE" pref 200
        log "policy route: traffic from ${subnet} uses table ${VPN_ROUTE_TABLE}"
    done
}

setup_firewall
setup_routing

# ----------------------------------------------------------------- run charon
/usr/lib/ipsec/charon &
CHARON_PID=$!

terminate() {
    log "shutting down"
    kill "$CHARON_PID" 2>/dev/null || true
    wait "$CHARON_PID" 2>/dev/null || true
    exit 0
}
trap terminate TERM INT

# Wait for the vici socket rather than sleeping a fixed interval.
i=0
while [ ! -S /var/run/charon.vici ]; do
    i=$((i + 1))
    if [ "$i" -gt 100 ]; then
        fail "charon did not create its vici socket within 10s"
    fi
    if ! kill -0 "$CHARON_PID" 2>/dev/null; then
        fail "charon exited during startup"
    fi
    sleep 0.1
done

swanctl --load-all
log "configuration loaded; charon will establish and maintain the tunnel"

# ------------------------------------------------------- supervisor + watchdog
# Two jobs, covering different failures:
#
#   charon died            -> exit non-zero so the restart policy fires, rather
#                             than leaving a container that looks healthy with
#                             no tunnel.
#   charon alive, no SA    -> re-initiate. Not redundant with charon's own
#                             recovery: start_action fires once at config load,
#                             close_action only when the *peer* closes the
#                             CHILD_SA, and dpd_action only when DPD concludes
#                             the peer is dead. A teardown outside those paths
#                             leaves the tunnel down permanently -- verified.
#
# Sleeping in short steps keeps shutdown prompt: a trap does not interrupt an
# in-progress sleep in POSIX sh, so one long sleep would delay SIGTERM handling
# past the stop grace period and end in SIGKILL.
SLEEP_STEP=5
elapsed=0

while kill -0 "$CHARON_PID" 2>/dev/null; do
    sleep "$SLEEP_STEP"
    elapsed=$((elapsed + SLEEP_STEP))
    if [ "$elapsed" -ge "$VPN_WATCHDOG_INTERVAL" ]; then
        elapsed=0
        if ! swanctl --list-sas 2>/dev/null | grep -q INSTALLED; then
            log "watchdog: no installed CHILD_SA -- re-initiating"
            swanctl --initiate --child tunnel --timeout 30 >/dev/null 2>&1 || \
                log "watchdog: re-initiate did not complete; will retry in ${VPN_WATCHDOG_INTERVAL}s"
        fi
    fi
done
log "ERROR: charon exited -- restarting container"
exit 1
