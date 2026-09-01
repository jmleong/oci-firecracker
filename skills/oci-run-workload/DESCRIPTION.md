# oci-run-workload

Benchmark or run any workload across a fleet of Oracle Cloud (OCI) instances — from one chat, with
no babysitting. Say "benchmark these OCI shapes" or "/oci-run-workload" and the skill launches the
fleet, runs your workload on every host, collects the results, and tears everything down.

## Pick a workload three ways

- **Built-in benchmarks**: the Firecracker microVM suite (boot / density / virtio-net / virtio-blk /
  AES), sysbench-cpu, sysbench-oltp (MariaDB), 7-zip, and STREAM memory bandwidth.
- **Your own local harness**: point at a directory and give a run command.
- **A git repo**: URL + command + which result files to keep.

Each host runs the same workload, so results are directly comparable across shapes (e.g. AMD EPYC
E5 Genoa vs E6 Turin). An optional per-workload report step runs locally on the collected results.

## Built for a locked-down network

The usual paths are blocked here: no outbound SSH from the runner, and no Object Storage access. So
results move over plain **HTTP** — each instance serves its output on port 80 and the runner pulls
it. No admin policy, no storage buckets. Verified end-to-end on real hardware.

## Safe by default

- **Cost-first**: it estimates the fleet's hourly cost and asks before launching anything billable.
- **Never orphans instances**: everything is tagged, auto-terminated when results are in, and a
  built-in reaper finds and cleans up leftovers if a run is interrupted.
- **Bare-metal aware**: KVM-dependent benchmarks (Firecracker) run only on bare-metal shapes, and a
  failed launch on a zero-limit shape costs nothing while telling you to request a quota increase.

Reuses the `/oci-instance` auth + networking foundation, so if you can create an OCI instance you can
run a fleet benchmark. Ideal for CPU/DB/memory comparisons, microVM density studies, and any
repeatable "run this across N shapes and show me the numbers" task.
