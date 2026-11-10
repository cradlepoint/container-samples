# Security Policy

## About this policy

This repository (`cradlepoint/container-samples`) contains sample and
demonstration containers for the Ericsson Cradlepoint NCOS container platform. It
is published as free and open-source software under Ericsson's brand.

Ericsson acts as an **Open Source Software Steward** for this repository under the
EU Cyber Resilience Act (CRA). This policy documents how to report security issues,
what is in and out of scope, and the response you can expect.

## Scope

**In scope**

- The security and integrity of this repository itself: its contents, the
  container images published to the GitHub Container Registry
  (`ghcr.io/cradlepoint/container-samples/<sample>:latest` and `:latest-armv7`)
  by the build workflow, the build/CI configuration (`.github/workflows/`), and
  the repository's GitHub configuration.
- Vulnerabilities in the shared libraries and tooling maintained in this
  repository that are common to the samples — for example the root Config Store
  client (`cp.py`) and the development-host operator tool (`tools/dev_router.py`).
- Reports that a published image has been tampered with, or that a contributor
  account or GitHub Action has been compromised.

**Out of scope**

- The behavior, correctness, or security of individual sample containers under
  `containers/` (for example `alpr`, `edge_ai`, `gps_logger`, `gpsd_server`,
  `ipsec_client`, `rtsp_viewer`, `snmp_agent`). These are provided as
  **known-working examples**, not as supported products. Ericsson supports the
  platform (NCM deployment and NCOS container execution) but does **not** support
  the individual container code itself. See *What Ericsson supports* below.
- Vulnerabilities in third-party base images, packages, or model files that a
  sample pulls in, except insofar as the repository's own build configuration
  selects them. These should be reported upstream to the respective projects.
- The NCOS operating system, the on-router container engine, NetCloud Manager
  (NCM), and Cradlepoint router hardware and firmware. These are commercial
  products with their own support and security reporting channels — see
  *Reporting a vulnerability in the platform* below.
- Vulnerabilities that exist only because a sample was modified, extended, or
  combined with other code after it was copied or pulled from this repository.

## What Ericsson supports

Ericsson supports the **platform**: deploying containers through NetCloud Manager
and running them on NCOS. The sample containers in this repository are provided as
examples that are known to work at the time of publication. The individual
container code is not sold as a product and is not supported as one. Use it as a
starting point, and review and test it before deploying to production routers.

## Reporting a vulnerability

### A vulnerability in this repository (in scope)

Please report privately. **Do not open a public issue for a security vulnerability.**

- Use GitHub's **private vulnerability reporting**: go to the **Security** tab of this
  repository and click **Report a vulnerability**. This opens a private advisory
  visible only to you and the maintainers.

Please include:

- The affected file(s), published image, or configuration.
- A description of the issue and its potential impact.
- Steps to reproduce, a proof of concept, or a patch if you have one.
- Any relevant version, commit hash, image tag, or image digest.

### A vulnerability in the platform (NCOS, the container engine, NCM, hardware/firmware)

Vulnerabilities in Ericsson Cradlepoint products — as opposed to this sample
repository — should be reported to Ericsson's Product Security Incident Response Team
(PSIRT). See the PSIRT page for how to contact them and what to include:

- https://www.ericsson.com/en/about-us/security/psirt

If you are unsure whether an issue is in this repository or in the platform, report it
privately here and we will route it appropriately.

## Response expectations

As a Steward, Ericsson provides best-effort handling of in-scope reports:

<!-- PLACEHOLDER: the response-time numbers below are placeholders and must be
     confirmed with PSIRT / compliance before this policy is treated as final. -->

- **Acknowledgement:** within 10 business days of receipt. _(placeholder — confirm with PSIRT/compliance)_
- **Initial assessment:** we will confirm whether the report is in scope and provide
  an initial severity assessment. _(target timeframe to be confirmed with PSIRT/compliance)_
- **Resolution:** for confirmed in-scope issues, we will work on a fix or mitigation
  and coordinate disclosure with you. Timelines depend on severity and complexity.

Reports that are out of scope (for example, issues in individual sample container code
or in the platform) will be acknowledged and redirected to the appropriate channel
rather than fixed under this policy.

## Supported versions

Security maintenance applies only to the **latest** state of the `master` branch and
the most recently published container images
(`ghcr.io/cradlepoint/container-samples/<sample>:latest` and `:latest-armv7`).
Older commits, tags, and previously published image digests are **not** maintained. If
you are running an older copy or image, update to the latest version before reporting
an issue.

## Coordinated disclosure

We ask that you give us a reasonable opportunity to investigate and remediate an
in-scope issue before any public disclosure. We will keep you informed of progress and
credit reporters who wish to be acknowledged.
