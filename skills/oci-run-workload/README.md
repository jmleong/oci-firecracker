# oci-run-workload

Run a workload or benchmark **headlessly across a fleet of OCI instances** and collect the results —
no SSH, no Object Storage. Modeled on `aws-perfspect-benchmark`, adapted to this environment's two
hard constraints (sandbox SSH is proxy-blocked; the OCI user has no Object Storage object access).

## How it works

Each instance self-provisions via **cloud-init**, runs the workload, and serves its results on
**port 80**. The sandbox (whose web egress exits via Zscaler on 80/443) **HTTP-pulls** the results.
No admin policy needed. Every instance is tagged `fcbench=1` for cleanup. Verified end-to-end.

Lifecycle: pick workload → pick fleet + confirm cost → launch → poll & HTTP-harvest → optional
report → tear down (with a tag-based reaper for orphans).

## Choose a workload three ways

- **Preset benchmark**: `firecracker` (microVM 5-metric suite, needs bare-metal), `sysbench-cpu`,
  `sysbench-oltp`, `7zip`, `stream`.
- **Local directory**: point at a folder with a runnable harness (embedded in cloud-init; small trees).
- **Git repo**: give a repo URL + command + which result files to collect.

Each resolves to a spec `{source, install, run, results, needs_reflink, report}`; presets are just
pre-filled specs. An optional per-workload `report` hook runs locally on the harvested results
(the firecracker preset points at its `gen_report.py`).

## Prerequisites

- OCI **API-key** auth under the `oci` key in `ClaudeTokens/tokens.json`, private key at
  `.oci-stage/oci_api_key.pem` (same as `/oci-instance`).
- A cached **compartment OCID** (`W.cfg_set(compartment=...)`).
- Service limits for the shapes you want (bare-metal is often 0 → request an increase).
- Python 3.10+, `pip install oci`.

## Quick start

Trigger with **"benchmark these OCI shapes"**, **"run a workload on OCI"**, or **/oci-run-workload**.
Step 0 bootstraps auth + the CA bundle and materializes the `_ociwl.py` pipeline helper into
`.oci-stage` (it also ships in this folder). Then follow the steps: pick a preset, pick shapes,
confirm the fleet cost, launch, poll until each host is DONE, then tear the fleet down.

## Files

- `SKILL.md` — the agent workflow (bootstrap → workload → fleet → launch → harvest → report → teardown).
- `_ociwl.py` — the pipeline helper (clients, `ensure_network`, presets, cloud-init builder,
  `launch_fleet`, `public_ip`, `harvest_http`, `reap_list`, `teardown`).
- `README.md` / `AGENTS.md` / `DESCRIPTION.md`.

## Notes

- **Always tear down** — headless fleets bill hourly and are easy to forget; the reaper finds orphans.
- **Bare-metal presets need `/dev/kvm`** (bare-metal shapes only).
- Poll the harvest in short calls — the workload runs for minutes.
