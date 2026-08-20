# IKEv2 IPsec client: gateway authenticates with a PSK, this side authenticates
# with a username and password over EAP. ESP is processed in userspace and rides
# a TUN device, so no kernel IPsec (XFRM) state is required.
#
# Why Debian and not Alpine, which is this repo's default base:
# Alpine's `strongswan` package does not ship the `libipsec` / `kernel-libipsec`
# plugin -- checked on 3.18, 3.20 and 3.22, whose only subpackages are doc,
# logfile and openrc. Without it the only data path is kernel XFRM, whose
# availability inside a container on this platform is unverified. Debian ships
# the userspace path in a package, so the container can ask for nothing beyond
# CAP_NET_ADMIN and /dev/net/tun. Cost of that choice, measured with
# `docker image ls`: see README. Runtime RSS is single-digit megabytes.
FROM debian:12-slim

# Three separate packages are needed and each matters:
#   libcharon-extra-plugins    -> kernel-libipsec (userspace ESP over TUN)
#   libcharon-extauth-plugins  -> eap-mschapv2, xauth-generic
#   libstrongswan-standard-plugins -> openssl, which supplies the MD4 that
#       eap-mschapv2 needs for the NT hash. Without it eap-mschapv2 is present
#       as a file, is configured to load, and silently does NOT appear in
#       charon's loaded-plugin list -- the failure surfaces much later as an
#       authentication error. Verify with the PLUGINS line the entrypoint logs.
# iptables/iproute2 are the data path: NAT out the tunnel, fail-closed
# forwarding, policy routing for the return path.
# ca-certificates supplies the system trust store, used when the gateway
# authenticates with a publicly issued certificate rather than a PSK. Pinning a
# single CA via VPN_CA_CERT_B64 is stronger and does not need it, but the
# fallback keeps the common case working out of the box.
# Note: strongSwan's `curl` plugin is not installed, so CRL and OCSP cannot be
# fetched over the network. Certificate chain and identity are still verified.
RUN apt-get update -qq \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      strongswan-charon \
      strongswan-swanctl \
      libstrongswan-standard-plugins \
      libcharon-extra-plugins \
      libcharon-extauth-plugins \
      iproute2 \
      iptables \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && rm -rf /etc/ipsec.d /etc/ipsec.conf /etc/ipsec.secrets

WORKDIR /opt/ipsec_client
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# No ports are published: this container initiates outbound and nothing needs
# to reach it except LAN clients routing through its own IP.
ENTRYPOINT ["/entrypoint.sh"]
