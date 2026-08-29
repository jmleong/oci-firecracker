# Firecracker microVM benchmark on Oracle Cloud (OCI)

Run a five-metric [Firecracker](https://github.com/firecracker-microvm/firecracker)
microVM benchmark on OCI **bare-metal** shapes and compare AMD EPYC generations —
Genoa/Zen4 (`E5`) vs Turin/Zen5 (`E6`) — plus a cross-cloud comparison against an
AWS AMD Turin baseline.

The on-host benchmark suite (`host/`) is a lightly-adapted copy of the vendor-neutral
harness from a companion AWS Firecracker benchmark; the OCI orchestration, storage handling,
and report tooling here are the OCI-specific additions. The suite measures boot latency,
fleet density, virtio-net, virtio-blk, and in-guest AES-256 compute.

## Why this is not "AMD vs Intel"

Unlike the AWS study, OCI has **no same-generation Intel bare-metal shape** available,
so this compares **AMD generations** (Genoa vs Turin). AES-256 is the clean,
storage-independent CPU signal; the other metrics are reported *as-shipped*.

## Repo layout

```
host/                     on-host benchmark harness (runs on each bare-metal host)
  install_firecracker.sh    idempotent provisioner (FC binary + CI kernel + rootfs)
  lib.sh vm.sh              helpers + microVM launcher
  bench_*.sh run_all.sh     the five benches + per-host aggregator -> result.json
  oci_setup_storage.sh      OCI: put working dir on reflink disk (NVMe or block volume)
  oci_setup_tmpfs.sh        OCI: RAM-backed reflink working dir (storage-normalized runs)
orchestrate/
  drive_oci.py              OCI SDK driver: net/up/attachvol/provision/run/collect/down
  drive_oci_headless.py     SSH-free variant (cloud-init self-run + Object Storage)
report/
  gen_report.py             result.json -> Markdown report + normalized chart
results/
  oci-run-20260828/         OCI E5.192 / E6.Ax.192 / E6.256 results
  run-20260629/             AWS AMD Turin baseline (c8a/m8a) for cross-cloud comparison
config.env.example          copy to config.env and fill in your OCIDs / CIDRs
```

## Prerequisites

- An OCI tenancy with **service limits** for the bare-metal shapes you want.
- OCI **API-key** auth (`~/.oci/config` with tenancy/user OCID, fingerprint, key).
- Python 3.10+, `pip install oci matplotlib`.
- Firecracker needs `/dev/kvm` — OCI exposes it only on **bare-metal** shapes.

### Behind a TLS-inspecting proxy?

The OCI SDK will reject an intercepted certificate. Build a combined CA bundle (your
corporate root CA + certifi) and set `client.base_client.session.verify = <bundle>`
(or `OCI_CA_BUNDLE`).

## Setup

```bash
cp config.env.example config.env      # then edit: compartment OCID, image OCID,
                                       # availability domains, SSH ingress CIDR
pip install oci matplotlib --user
```

`config.env` is gitignored — it holds your site-specific values and is never committed.

## Running

```bash
cd orchestrate
python3 drive_oci.py net                    # VCN + subnet + security list
python3 drive_oci.py up  BM.Standard.E6.256 # launch a bare-metal host (billable!)
python3 drive_oci.py provision <ip>         # rsync host/ + install
python3 drive_oci.py run <ip> e6.256        # run suite, collect result.json
python3 drive_oci.py down --all             # TERMINATE everything (stop billing)
```

Notes:
- **Storage matters.** OCI boot volumes are `ext4` (no `reflink`), so the density bench's
  per-VM overlays would fill the disk. `host/oci_setup_storage.sh` puts the working dir on
  a reflink XFS filesystem — a local **NVMe** on E6, or an attached **block volume** on E5
  (no local NVMe; use `drive_oci.py attachvol <ip>`). Export `FC_ROOT` to that path.
- **Terminate** bare-metal hosts and delete scratch volumes when done — billed hourly.
  `down --all` finds everything tagged `fcbench=1`.

### Generate the report

```bash
python3 report/gen_report.py results/oci-run-20260828 -o report/report.md
```

## Headline findings (this run)

- **AES-256 (clean CPU signal):** Zen5 (E6) approx. **+8-11%** per-thread over Zen4 (E5).
- **As-shipped storage is a real shape difference:** E6 ships with local NVMe (~255k
  randread IOPS, ~122 ms cold boot); E5 has none and depends on an attached block volume
  (higher boot latency, ~2x lower IOPS). Not normalized away — a customer deploys the
  shape as-shipped.
- **Cross-cloud (AMD Turin, OCI vs AWS):** mixed — OCI leads on virtio-net (~1.2x) and
  virtio-blk (~1.1x), trails slightly on boot (~0.82x) and full-socket AES (~0.92x).

See `report/firecracker-amd-oci-and-aws.md` for full tables, chart, and methodology.

## Credits

On-host benchmark harness: adapted from a vendor-neutral AWS Firecracker benchmark suite.
OCI orchestration, storage handling, and reporting: this repo.
