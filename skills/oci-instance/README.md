# oci-instance

Create and manage Oracle Cloud (OCI) compute instances interactively, driven directly from the
Cowork sandbox via the OCI Python SDK.

## What it does

- **Create** an instance: pick an AMD `E5`/`E6` bare-metal or `E`-flex VM shape, auto-provision the
  network (VCN, subnet, internet gateway, SSH security-list rule), launch it, and get back the
  public IP + a ready-to-use SSH command.
- **Manage**: list / start / stop / terminate instances (all tagged `fcbench=1`).
- **Bare-metal storage prep**: optional reflink-XFS setup on local NVMe or an attached block volume
  (for Firecracker/microVM-style overlay workloads).

Unlike the GCP flow (which generates a PowerShell script you run locally), OCI is fully driven from
the sandbox — the skill launches and terminates instances itself.

## Prerequisites

1. **OCI API-key auth** stored under the `oci` key in `ClaudeTokens/tokens.json`
   (`user`, `tenancy`, `fingerprint`, `region`), with the matching **private key** placed at
   `ClaudeTokens/.oci-stage/oci_api_key.pem`. Upload the public half in the OCI Console
   (Profile → API keys).
2. Service limits for the shapes you want (bare-metal limits are often 0 by default — request an
   increase in the Console).
3. Three site values the skill will ask for once: compartment OCID, a region-specific Ubuntu 22.04
   x86_64 image OCID, and the SSH-ingress CIDR (your public egress).

## Quick start

Trigger with **"create an OCI instance"**, **"list my OCI instances"**, or **/oci-instance**.
The skill runs a one-time bootstrap (installs the SDK, writes the config, builds the corporate
CA bundle), then walks you through the operation.

## Notes

- **SSH from the sandbox is proxy-blocked** — the skill creates the instance and gives you the SSH
  command to run from your own machine.
- **Bare-metal is billed hourly** — terminate when done (`/oci-instance` → terminate, or `down`).
- Behind a TLS-inspecting proxy, the SDK needs a combined CA bundle; the skill builds this
  automatically in step 0.
