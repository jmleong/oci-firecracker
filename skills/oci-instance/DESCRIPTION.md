# oci-instance

Spin up and manage Oracle Cloud (OCI) compute instances without leaving your chat. Say "create an
OCI instance" or "/oci-instance" and the skill drives the OCI Python SDK directly from the sandbox
— no CLI to install, no PowerShell script to run by hand.

## What you get

- **Interactive create**: pick an AMD EPYC bare-metal shape (`BM.Standard.E5.192` Genoa,
  `BM.Standard.E6.256` / `E6.Ax.192` Turin) or an `E`-flex VM (choose OCPU and memory). Any shape
  your tenancy is entitled to also works.
- **Automatic networking**: the skill creates or reuses a VCN, subnet, internet gateway, and an SSH
  security-list rule scoped to your egress CIDR — no manual console clicking.
- **Full lifecycle**: list, start, stop, and terminate instances, all tagged so cleanup is one
  command. It always reminds you to terminate bare-metal hosts, which bill hourly.
- **Bare-metal storage prep**: an optional reflink-XFS setup on local NVMe or an attached block
  volume, for Firecracker/microVM overlay workloads.

## Built for real corporate networks

The skill handles the two things that trip up OCI automation behind a corporate firewall:

- **TLS-inspecting proxy**: it builds a combined CA bundle (certifi + corporate roots) so the SDK
  doesn't choke on intercepted certificates.
- **Blocked outbound SSH**: instead of failing silently, it creates the instance and hands you a
  ready-to-use SSH command to run from your own machine.

A one-time bootstrap installs the SDK, writes your OCI config from `ClaudeTokens`, and generates a
host SSH key. After that, every operation is a single conversational request. The launch step even
doubles as a free service-limit probe: a limit-0 shape errors without creating (or billing) anything.

Ideal for benchmarking, quick test hosts, and anyone who provisions OCI compute often and wants it
to be as easy as asking.
