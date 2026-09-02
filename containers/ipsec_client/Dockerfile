# IKEv2 IPsec client: the gateway authenticates itself by certificate (default)
# or by pre-shared key, and this side authenticates with a username and password
# over EAP.
#
# The ESP data path is selected at startup, not baked in here: kernel ESP on an
# XFRM interface where the kernel supports it, userspace ESP over a TUN device
# where it does not. Both are built into this image, and the entrypoint's
# PREFLIGHT and SELECTED lines report which one a given router got.
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

# Four separate plugin packages are needed and each matters:
#   libcharon-extra-plugins    -> kernel-libipsec (userspace ESP over TUN)
#   libcharon-extauth-plugins  -> eap-mschapv2, xauth-generic
#   libstrongswan-standard-plugins -> openssl, which supplies the MD4 that
#       eap-mschapv2 needs for the NT hash. Without it eap-mschapv2 is present
#       as a file, is configured to load, and silently does NOT appear in
#       charon's loaded-plugin list -- the failure surfaces much later as an
#       authentication error.
#   libstrongswan-extra-plugins -> curl, the HTTP/HTTPS fetcher charon needs to
#       retrieve CRLs and OCSP responses. Without it the `revocation` plugin has
#       no transport, so charon logs `no capable fetcher found` followed by
#       `certificate status is not available` and proceeds anyway -- chain and
#       identity verified, revocation silently not. Debian does not split this
#       package per plugin, so it also supplies af-alg, ccm, chapoly, cmac, ctr,
#       curve25519, gcrypt, ldap, pkcs11, test-vectors and tpm. Those are the same
#       names that otherwise fill charon's `failed to load` block at startup; none
#       is required here, and none changes what is negotiated, because the
#       proposals are set explicitly. It also costs ~30 MB of image, almost all of
#       it libcurl4's transitive closure -- see README.
#
# Confirm what actually loaded from charon's own `loaded plugins:` line at startup,
# or `swanctl --stats` at runtime. Presence of the .so file is a weaker test.
#
# iptables/iproute2 are the data path: NAT out the tunnel, fail-closed
# forwarding, policy routing for the return path.
# ca-certificates supplies the system trust store, used when the gateway
# authenticates with a publicly issued certificate rather than a PSK. Pinning a
# single CA via VPN_CA_CERT_B64 is stronger and does not need it, but the
# fallback keeps the common case working out of the box.
# Revocation checking is not enforced by default. Confirmed against strongSwan
# 5.9.8's own `swanctl.opt`: `revocation` defaults to `relaxed`, which fails only
# when a certificate is explicitly known to be revoked -- so an unreachable CRL or
# OCSP responder stays a warning and cannot newly break an established profile.
# `ifuri` (fail only when the certificate advertises a URI and the check fails) or
# `strict` (fail whenever revocation status is unavailable) go in the connection's
# remote section, and both make the tunnel depend on reaching the CA over the WAN
# before it can come up.
#
# tpm is the one plugin in the extra set whose file is present but cannot
# initialise -- no TPM is exposed to a container, so it logs
# `tpm_plugin_create returned NULL` on every start. Turned off rather than left to
# fail, so installing the fetcher does not trade ten benign log lines for a new
# one. Every other plugin in the package loads cleanly.
RUN apt-get update -qq \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      strongswan-charon \
      strongswan-swanctl \
      libstrongswan-standard-plugins \
      libstrongswan-extra-plugins \
      libcharon-extra-plugins \
      libcharon-extauth-plugins \
      iproute2 \
      iptables \
      ca-certificates \
 && sed -i 's/^\( *\)load = yes/\1load = no/' /etc/strongswan.d/charon/tpm.conf \
 && grep -q 'load = no' /etc/strongswan.d/charon/tpm.conf \
 && rm -rf /var/lib/apt/lists/* \
 && rm -rf /etc/ipsec.d /etc/ipsec.conf /etc/ipsec.secrets

WORKDIR /opt/ipsec_client
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# No ports are published: this container initiates outbound and nothing needs
# to reach it except LAN clients routing through its own IP.
ENTRYPOINT ["/entrypoint.sh"]
