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

# Print the message and nothing else. The container runtime already stamps each
# line with a timestamp and the container name on its way to the router log, so
# adding either here just duplicates it. `ERROR` stays because severity is part
# of the message, not metadata the log carrier supplies.
log() { echo "$*"; }
fail() { echo "ERROR: $*" >&2; exit 1; }

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
# Defaults suit a common dialup profile: aes256-sha256 with a 2048-bit DH group
# for IKE, aes256-sha256 for ESP. The ESP proposal deliberately carries no DH
# group, because adding one where the gateway has PFS disabled makes the
# CHILD_SA proposal fail to match. Both must overlap what the gateway offers.
: "${VPN_IKE_PROPOSALS:=aes256-sha256-modp2048,aes128-sha256-modp2048,default}"
: "${VPN_ESP_PROPOSALS:=aes256-sha256,aes128-sha256,default}"

# How the *gateway* authenticates itself: pubkey (certificate) or psk.
# Certificate is the common case for a dialup gateway using EAP, so it is the
# default; use psk only where the gateway really has a shared secret configured.
# This side always authenticates with EAP either way.
: "${VPN_GATEWAY_AUTH:=pubkey}"
: "${VPN_CA_CERT_B64:=}"
: "${VPN_DPD_DELAY:=30s}"
: "${VPN_MSS_CLAMP:=1}"
: "${VPN_FAIL_CLOSED:=1}"
: "${VPN_WATCHDOG_INTERVAL:=20}"
# Heartbeat interval for the periodic status line. Transitions are logged the
# moment they happen regardless of this; it only bounds how often an unchanged
# state is restated. 0 disables the heartbeat and logs transitions only.
: "${VPN_STATUS_INTERVAL:=300}"

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
# Both helpers print the command's own error text on failure. A preflight exists
# so that one deployment answers the platform questions from `container logs`, and
# a verdict without the reason answers only half of them -- "not permitted",
# "no such file or directory" and "table does not exist" send you to three
# different fixes, and discarding stderr makes them indistinguishable.
_preflight_err=/tmp/preflight.err
check() {
    label="$1"; shift
    if "$@" >/dev/null 2>"$_preflight_err"; then
        log "PREFLIGHT ok      : ${label}"
        return 0
    fi
    log "PREFLIGHT FAILED  : ${label}"
    _report_preflight_error
    preflight_failed=1
    # Deliberately returns 0. These are called as bare top-level commands, and
    # `set -e` exits the script on a non-zero one -- which aborted the whole
    # preflight at the FIRST failure, so the remaining grants went unreported and
    # the explanatory message below never printed. The point of a preflight is
    # that one deployment answers every platform question, so a failing check
    # must record itself in preflight_failed and let the run continue.
    return 0
}
probe() {
    # Like check(), but informational: reports capability without failing startup.
    label="$1"; shift
    if "$@" >/dev/null 2>"$_preflight_err"; then
        log "PREFLIGHT ok      : ${label}"
        return 0
    fi
    log "PREFLIGHT absent  : ${label}"
    _report_preflight_error
    return 1
}
_report_preflight_error() {
    [ -s "$_preflight_err" ] || return 0
    # First two lines only: iptables in particular repeats itself.
    head -2 "$_preflight_err" | while read -r line; do
        [ -n "$line" ] && log "                    reason: ${line}"
    done
    : > "$_preflight_err"
}

check "CAP_NET_ADMIN can add a policy routing rule" \
    sh -c 'ip rule add to 192.0.2.0/24 lookup main pref 32000 && ip rule del to 192.0.2.0/24 lookup main pref 32000'

# ------------------------------------------------------- netfilter backend
# Debian ships iptables with the nf_tables backend by default and keeps the
# legacy one alongside it. They are not interchangeable here: they talk to
# different kernel subsystems, so whichever one this kernel lacks fails while the
# other works. A container that only ever calls `iptables` therefore fails on a
# kernel that would have supported it via the other backend, and the error text
# ("table does not exist", typically) does not say so.
#
# So probe both and use whichever answers. VPN_IPTABLES_BACKEND=nft|legacy forces
# one, for the case where both load but only one actually filters.
: "${VPN_IPTABLES_BACKEND:=auto}"
case "$VPN_IPTABLES_BACKEND" in
    auto|nft|legacy) ;;
    *) fail "VPN_IPTABLES_BACKEND=${VPN_IPTABLES_BACKEND} is not one of: auto, nft, legacy" ;;
esac

_backend_works() {
    # A NAT-table write is the demanding case: it needs the nat chain type, not
    # just the binary. Test what the container actually depends on.
    "$1" -t nat -N preflight >/dev/null 2>&1 || return 1
    "$1" -t nat -X preflight >/dev/null 2>&1 || true
    return 0
}

IPT=''
case "$VPN_IPTABLES_BACKEND" in
    nft)    IPT=iptables-nft ;;
    legacy) IPT=iptables-legacy ;;
    auto)
        for candidate in iptables-nft iptables-legacy; do
            if _backend_works "$candidate"; then
                IPT="$candidate"
                break
            fi
            log "netfilter backend ${candidate}: no usable nat table, trying the next"
        done
        ;;
esac

if [ -z "$IPT" ]; then
    # Fall back to the default binary purely so the checks below produce a real
    # error message naming the reason, rather than this line being the last word.
    IPT=iptables
    log "netfilter: neither backend has a usable nat table -- see the reasons above"
fi
log "netfilter backend: ${IPT} ($("$IPT" --version 2>/dev/null | head -1))"

check "netfilter nat table writable (${IPT})" \
    sh -c "${IPT} -t nat -N preflight && ${IPT} -t nat -X preflight"
check "netfilter filter policy writable (${IPT})" \
    sh -c "${IPT} -t filter -N preflight && ${IPT} -t filter -X preflight"

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
        $IPT -P FORWARD DROP
        log "FORWARD policy DROP (fail-closed: LAN clients have no path while the tunnel is down)"
    else
        log "WARNING: VPN_FAIL_CLOSED=0 -- traffic will fall back to the router and leave"
        log "         this container unencrypted whenever the tunnel is down."
    fi

    $IPT -A FORWARD -o "$VPN_IF_NAME" -j ACCEPT
    $IPT -A FORWARD -i "$VPN_IF_NAME" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

    # All LAN clients are translated to the address the gateway assigns, which
    # is what makes this outbound-only: the far side cannot initiate inward.
    $IPT -t nat -A POSTROUTING -o "$VPN_IF_NAME" -j MASQUERADE

    if [ "$VPN_MSS_CLAMP" = "1" ]; then
        $IPT -t mangle -A FORWARD -o "$VPN_IF_NAME" \
            -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu
        log "MSS clamped to path MTU on ${VPN_IF_NAME}"
    fi
}

setup_routing() {
    # Keep LAN-destined traffic out of the tunnel, in BOTH data paths.
    #
    # This was originally only in the userspace branch, which is a bug worth
    # naming: with a catch-all remote_ts, *any* table holding a default route via
    # the tunnel also matches replies destined for the local network -- charon's
    # table 220 behind its "from all" rule in userspace mode, and the explicit
    # table below in xfrm mode. The failure differs in blast radius rather than
    # in kind: LAN clients see one-way loss, and if the container's own address
    # is inside LAN_SUBNETS, its own replies leave via the tunnel too, which
    # takes out management access to anything on that segment.
    #
    # pref 100 is deliberately lower than the pref 200 rule below, so this is
    # consulted first.
    for subnet in $(echo "$LAN_SUBNETS" | tr ',' ' '); do
        [ -n "$subnet" ] || continue
        ip rule add to "$subnet" lookup main pref 100
        log "policy route: traffic to ${subnet} uses the main table, not the tunnel"
    done

    if [ "$DATA_PATH" = userspace ]; then
        # charon installs its own routes in table 220 behind a "from all" rule,
        # so the exclusion above is all that is needed here.
        return
    fi

    # xfrm mode: routing is explicit, in a table selected by source address, so
    # only forwarded LAN traffic uses the tunnel.
    #
    # NOTE the assumption in that sentence: it holds only while the container's
    # own address is OUTSIDE every LAN_SUBNETS entry. Put the container on the
    # same segment it serves and `from <lan>` also matches packets the container
    # originates -- including the ESP it sends to the gateway, which would then
    # route into the tunnel it is trying to build. Give the container its own
    # network, or narrow VPN_REMOTE_TS so no catch-all default exists.
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
#
# It also reports state. Transitions are logged the instant they are observed,
# because up->down and down->up are the lines an operator correlates against a
# router event; an unchanged state is restated only every VPN_STATUS_INTERVAL so
# a healthy tunnel does not emit a line per check.
report_sas() {
    # Echo swanctl's own lines rather than picking fields out of them. The column
    # layout is not a stable interface, and a wrong pattern here would print
    # nothing while still looking like a working check -- the failure mode is a
    # status report that is silently empty.
    swanctl --list-sas 2>/dev/null | grep -E 'ESTABLISHED|INSTALLED' | tr -s ' ' \
        | while read -r line; do log "  ${line}"; done
}

SLEEP_STEP=5
elapsed=0
since_status=0
down_checks=0
tunnel_state=unknown

while kill -0 "$CHARON_PID" 2>/dev/null; do
    sleep "$SLEEP_STEP"
    elapsed=$((elapsed + SLEEP_STEP))
    since_status=$((since_status + SLEEP_STEP))

    [ "$elapsed" -ge "$VPN_WATCHDOG_INTERVAL" ] || continue
    elapsed=0

    if swanctl --list-sas 2>/dev/null | grep -q INSTALLED; then
        state=up
    else
        state=down
    fi

    # Changes first, so a transition is never swallowed by a heartbeat that
    # happened to fall in the same interval.
    if [ "$state" != "$tunnel_state" ]; then
        if [ "$state" = up ]; then
            log "tunnel UP: gateway ${VPN_GATEWAY}, data path ${DATA_PATH}, interface ${VPN_IF_NAME}"
            report_sas
            addr=$(ip -4 -o addr show dev "$VPN_IF_NAME" 2>/dev/null | awk '{print $4}' | head -1) || true
            if [ -n "${addr:-}" ]; then
                log "  assigned address ${addr}"
            fi
        elif [ "$tunnel_state" = unknown ]; then
            log "tunnel not established yet"
        else
            log "tunnel DOWN: forwarded LAN traffic is now being dropped (fail_closed=${VPN_FAIL_CLOSED})"
        fi
        tunnel_state="$state"
        since_status=0
    fi

    if [ "$state" = down ]; then
        # Re-initiate on every check, but do not narrate every attempt. A
        # permanently unreachable gateway would otherwise emit a pair of lines
        # per interval forever, into a log that is shared with the rest of the
        # router. Log the first attempt, then every tenth, the way cp.py
        # throttles repeated transport failures.
        down_checks=$((down_checks + 1))
        if [ "$down_checks" -eq 1 ] || [ $((down_checks % 10)) -eq 0 ]; then
            log "watchdog: no installed CHILD_SA -- re-initiating (attempt ${down_checks})"
            verbose_attempt=1
        else
            verbose_attempt=0
        fi
        if ! swanctl --initiate --child tunnel --timeout 30 >/dev/null 2>&1; then
            if [ "$verbose_attempt" = 1 ]; then
                log "watchdog: re-initiate did not complete; retrying every ${VPN_WATCHDOG_INTERVAL}s, logging every 10th"
            fi
        fi
    else
        down_checks=0
    fi

    if [ "$VPN_STATUS_INTERVAL" -gt 0 ] && [ "$since_status" -ge "$VPN_STATUS_INTERVAL" ]; then
        since_status=0
        log "status: tunnel ${state}, gateway ${VPN_GATEWAY}, data path ${DATA_PATH}"
        if [ "$state" = up ]; then
            report_sas
        fi
    fi
done
log "ERROR: charon exited -- restarting container"
exit 1
