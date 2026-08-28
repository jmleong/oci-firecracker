#!/usr/bin/env python3
"""
gen_report.py — turn OCI Firecracker result.json files into a Markdown report.

Reads the per-shape result.json produced by host/run_all.sh (schema
firecracker-bench/v1) and emits a comparison report modelled on the AWS
AMD-vs-Intel report: header, executive summary with headline ratios, a full
per-metric results table with a generational ratio column, a test-configuration
table, and methodology notes.

This report compares AMD generations on OCI (no Intel — OCI has no same-gen Intel
bare-metal): EPYC Genoa/Zen4 (E5) vs Turin/Zen5 (E6). The core-matched pair
(E5.192 vs E6.Ax.192, both 384 threads) drives the headline ratios; E6.256 is
shown as the larger flagship.

Usage: gen_report.py <results_dir> [-o report.md]
  results_dir contains BM.Standard.E5.192.json, BM.Standard.E6.Ax.192.json,
  BM.Standard.E6.256.json.
"""
import argparse, json, os, sys, glob, math

# which shapes, in display order, and their short labels + generation
SHAPES = [
    ("BM.Standard.E5.192",    "E5.192",    "Genoa / Zen4"),
    ("BM.Standard.E6.Ax.192", "E6.Ax.192", "Turin / Zen5"),
    ("BM.Standard.E6.256",    "E6.256",    "Turin / Zen5"),
]
# core-matched generational pair for the headline ratios
BASE = "BM.Standard.E5.192"       # Zen4
GEN  = "BM.Standard.E6.Ax.192"    # Zen5, same 384 threads as E5.192

def load(results_dir):
    d = {}
    for key, _, _ in SHAPES:
        p = os.path.join(results_dir, key + ".json")
        if os.path.exists(p):
            d[key] = json.load(open(p))
    return d

def g(d, key, *path, default=None):
    cur = d.get(key, {})
    for p in path:
        cur = cur.get(p, {}) if isinstance(cur, dict) else {}
    return cur if cur != {} else default

def ratio(a, b, higher_better=True, win_hi="E6.Ax", win_lo="E5"):
    """Return a formatted 'X.XX faster/higher' string for gen(b) vs base(a).
    win_hi/win_lo label which shape won (the E5-vs-E6 comparison uses E6.Ax.192)."""
    try:
        a = float(a); b = float(b)
        if a == 0 or b == 0: return "n/a"
        r = (b / a) if higher_better else (a / b)
        who = win_hi if r >= 1 else win_lo
        return f"{r:.2f}× {'higher' if higher_better else 'faster'} ({who})"
    except Exception:
        return "n/a"

def fnum(x, nd=1):
    try:
        return f"{float(x):,.{nd}f}"
    except Exception:
        return str(x)

def build(d):
    rows = []
    L = rows.append
    ts = g(d, GEN, "host", "timestamp_utc", default="")
    fc = g(d, GEN, "host", "fc_version", default="")
    kern = g(d, GEN, "host", "kernel", default="")
    L(f"# Firecracker microVM performance: AMD EPYC Genoa (E5) vs Turin (E6) on OCI bare-metal")
    L("")
    L(f"*OCI bare-metal · Ashburn (us-ashburn-1) · Firecracker {fc} · kernel {kern} · "
      f"THP madvise · generated from run {ts}*")
    L("")
    L("> **Note on Intel:** OCI has no same-generation Intel bare-metal shape "
      "(the only Intel bare-metal, `BM.Standard4.Ax`, was not available in-region), "
      "so this study compares AMD generations — EPYC **Genoa (Zen4)** vs **Turin (Zen5)** — "
      "rather than AMD vs Intel. A companion AWS study (AMD Turin vs Intel Granite "
      "Rapids) remains the cross-vendor reference.")
    L("")
    # --- executive summary ---
    aes1_b = g(d, BASE, "results", "guest_compute", "aes256_1t_mibps")
    aes1_g = g(d, GEN,  "results", "guest_compute", "aes256_1t_mibps")
    aes8_b = g(d, BASE, "results", "guest_compute", "aes256_nt_mibps")
    aes8_g = g(d, GEN,  "results", "guest_compute", "aes256_nt_mibps")
    L("## Executive summary")
    L("")
    L("The same five-metric Firecracker suite was run on three OCI AMD bare-metal shapes "
      "under an identical Firecracker build, guest kernel, and golden rootfs. The core-matched "
      "pair — **E5.192 (Genoa/Zen4)** and **E6.Ax.192 (Turin/Zen5)**, both 384 threads — isolates "
      "the generational step; **E6.256** is the larger 512-thread flagship.")
    L("")
    L("The cleanest, storage-independent generational signal is **in-guest AES-256** (AES-NI/VAES): "
      f"per-thread it rises from **{fnum(aes1_b)} → {fnum(aes1_g)} MiB/s** "
      f"({ratio(aes1_b, aes1_g)}), and at 8 threads **{fnum(aes8_b)} → {fnum(aes8_g)} MiB/s** "
      f"({ratio(aes8_b, aes8_g)}) — a ~8–11% per-thread Zen5 uplift.")
    L("")
    L("The other metrics are reported **as-shipped**: E6 shapes include **local NVMe**, while "
      "**E5.192 has none** and ran on a standard iSCSI block volume (the only option for that "
      "shape). Boot latency, density fleet-boot, and block IOPS therefore reflect the **shape as "
      "a package** (CPU *and* its bundled storage), not the CPU alone — which is exactly what a "
      "customer deploying the shape experiences. See the storage note below.")
    L("")
    # --- full results table ---
    L("## Full results")
    L("")
    hdr = "| Metric | E5.192 (Zen4) | E6.Ax.192 (Zen5) | E6.256 (Zen5) | E5.192 → E6.Ax.192 |"
    L(hdr)
    L("|---|---|---|---|---|")
    def row(label, path, nd=1, higher=True, storage=False):
        b = g(d, BASE, *path); a = g(d, GEN, *path); c = g(d, "BM.Standard.E6.256", *path)
        star = " †" if storage else ""
        L(f"| {label}{star} | {fnum(b,nd)} | {fnum(a,nd)} | {fnum(c,nd)} | {ratio(b,a,higher)} |")
    L("| **Boot latency (cold-start, fast-init)** | | | | |")
    row("p50 boot (ms)", ("results","boot_latency","p50"), 1, higher=False, storage=True)
    row("p90 boot (ms)", ("results","boot_latency","p90"), 1, higher=False, storage=True)
    L("| **Fleet density (256 MiB/VM)** | | | | |")
    row("microVMs ready", ("results","density","ready"), 0, higher=True)
    row("fleet boot time (s)", ("results","density","fleet_boot_seconds"), 2, higher=False, storage=True)
    L("| **Network (virtio-net, iperf3)** | | | | |")
    row("host→guest (Gbps)", ("results","net_iperf3","fwd_gbps"), 1, higher=True)
    row("guest→host (Gbps)", ("results","net_iperf3","rev_gbps"), 1, higher=True)
    L("| **Block I/O (virtio-blk, fio direct)** | | | | |")
    row("4K randread (IOPS)", ("results","block_fio","randread_iops"), 0, higher=True, storage=True)
    row("4K randwrite (IOPS)", ("results","block_fio","randwrite_iops"), 0, higher=True, storage=True)
    row("1M seqread (MiB/s)", ("results","block_fio","seqread_mibps"), 0, higher=True, storage=True)
    L("| **Guest compute (in-VM)** | | | | |")
    row("AES-256 1-thread (MiB/s)", ("results","guest_compute","aes256_1t_mibps"), 1, higher=True)
    row("AES-256 8-thread (MiB/s)", ("results","guest_compute","aes256_nt_mibps"), 1, higher=True)
    L("")
    L("Ratio column = E6.Ax.192 vs E5.192 (the 384-thread core-matched pair). "
      "† = storage-sensitive metric (see note): E6 on local NVMe, E5 on iSCSI block volume.")
    L("")
    # --- methodology notes ---
    L("## Methodology notes")
    L("")
    L("**Storage is a real shape difference, not normalized away.** E6.Ax.192 and E6.256 ship with "
      "local NVMe; E5.192 has no local NVMe, so its Firecracker working set (guest rootfs + overlays) "
      "ran on an attached iSCSI block volume. We deliberately did **not** normalize storage (e.g. via a "
      "RAM disk) because a customer deploys the shape as-shipped: an E6 tenant gets local NVMe, an E5 "
      "tenant must attach block storage. The storage-sensitive rows (†) therefore reflect the true "
      "as-deployed experience. The pure CPU-generation signal is carried by AES-256, which is "
      "storage-independent.")
    L("")
    L("**Density is thread-capped.** At 256 MiB/VM every host saturates at one microVM per hardware "
      "thread (E5.192 & E6.Ax.192: 384; E6.256: 512) long before RAM is exhausted, so density scales "
      "with thread count, not a memory advantage.")
    L("")
    L("**Network gap larger than the CPU step alone.** The E5→E6 virtio-net ratio is much bigger than "
      "the ~1.1× seen on the pure-CPU AES metric. virtio-net throughput is driven by the host vhost "
      "worker and is sensitive to NUMA placement of the guest vCPUs, the TAP, and the vhost thread — "
      "none of which the harness pins. On the dual-socket E5 this can split the data path across sockets "
      "and depress throughput. The E5 network figure (measured consistently at ~8.6 Gbps across "
      "directions) should be treated as a floor pending a NUMA-pinned re-measurement, not as the ceiling "
      "of Genoa's virtio-net capability.")
    L("")
    L("**matrixprod excluded.** A stress-ng matrixprod stressor is collected but excluded from "
      "conclusions (it dispatches into Intel AMX on Intel parts — an ISA AMD does not expose). AES-256-GCM "
      "is the retained compute benchmark; both AMD generations support AES-NI/VAES.")
    L("")
    # --- test config ---
    L("## Test configuration")
    L("")
    L("| Shape | CPU | Sockets × cores | Threads | RAM | Local storage |")
    L("|---|---|---|---|---|---|")
    stor = {"BM.Standard.E5.192":"none (iSCSI block volume)",
            "BM.Standard.E6.Ax.192":"2×960 GB NVMe",
            "BM.Standard.E6.256":"2×960 GB NVMe"}
    for key, short, gen in SHAPES:
        h = g(d, key, "host", default={})
        if not h: continue
        cores = f"{h.get('sockets','?')} × {h.get('cores_per_socket','?')}"
        ram = f"{h.get('mem_total_kb',0)//1024//1024} GiB"
        L(f"| {short} ({gen}) | {h.get('cpu_model','?')} | {cores} | {h.get('threads_total','?')} | {ram} | {stor.get(key,'?')} |")
    L("")
    return "\n".join(rows)

# ----------------------------------------------------- cross-cloud (OCI vs AWS)
def _geomean(vals):
    vals = [v for v in vals if v]
    return math.exp(sum(math.log(v) for v in vals) / len(vals)) if vals else 0

def _aws_hosts(aws_dir):
    """Return {'c8a': result_json, 'm8a': result_json} for the AWS AMD Turin hosts."""
    out = {}
    for tag in ("c8a", "m8a"):
        fs = glob.glob(os.path.join(aws_dir, tag + "*.json"))
        if fs: out[tag] = json.load(open(fs[0]))
    return out or None

def _host_val(doc, path):
    c = doc["results"]
    for p in path: c = c[p]
    return c

def _aws_baseline(aws_dir):
    """Geomean of the AWS AMD Turin baseline hosts (c8a + m8a)."""
    hosts = _aws_hosts(aws_dir)
    if not hosts: return None
    def val(path):
        return _geomean([_host_val(d, path) for d in hosts.values()])
    return val

# metric: (label, path, higher_is_better)
XCLOUD = [
    ("Boot p50 (ms)",             ("boot_latency","p50"),          False),
    ("virtio-net h→g (Gbps)",     ("net_iperf3","fwd_gbps"),       True),
    ("virtio-blk randread (IOPS)",("block_fio","randread_iops"),   True),
    ("AES-256 8-thread (MiB/s)",  ("guest_compute","aes256_nt_mibps"), True),
]

def cross_cloud_section(d, aws_val, aws_hosts, chart_path):
    """Markdown table + normalized bar chart: OCI AMD Turin (E6.256) vs AWS AMD Turin.
    Shows both AWS hosts (c8a, m8a) as actual values; ratio is vs their geomean."""
    oci = d.get("BM.Standard.E6.256", {}).get("results", {})
    c8a = aws_hosts.get("c8a"); m8a = aws_hosts.get("m8a")
    L = []
    L.append("## Cross-cloud: AMD Turin on OCI vs AWS")
    L.append("")
    L.append("Same silicon generation (AMD EPYC Turin/Zen5), different cloud. OCI **E6.256** "
             "(local NVMe) vs the two AWS Turin metal hosts from the companion AWS study — **c8a** "
             "(compute) and **m8a** (general-purpose), both EPYC 9R45, Turin + local NVMe. The two AWS "
             "hosts share the same silicon (they differ only in RAM class), so their per-guest numbers "
             "are near-identical; the ratio column compares OCI to their geometric mean (the same "
             "pair-geomean method used in the AWS study). This isolates the *cloud platform* (host "
             "kernel, VMM tuning, storage, power limits) on matched silicon.")
    L.append("")
    L.append("| Metric | c8a (AWS) | m8a (AWS) | OCI E6.256 | OCI vs AWS geomean |")
    L.append("|---|---|---|---|---|")
    labels, norms = [], []
    for label, path, hb in XCLOUD:
        a = aws_val(path)
        vc = _host_val(c8a, path) if c8a else 0
        vm = _host_val(m8a, path) if m8a else 0
        o = oci
        for p in path: o = o.get(p, 0) if isinstance(o, dict) else 0
        r = (o / a) if hb else (a / o)   # >1 = OCI better
        arrow = "OCI" if r >= 1.0 else "AWS"
        L.append(f"| {label} | {vc:,.1f} | {vm:,.1f} | {o:,.1f} | {r:.2f}× ({arrow}) |")
        labels.append(label.split(" (")[0]); norms.append(r)
    L.append("")
    L.append("*OCI vs AWS = normalized so >1.0 means OCI is better on that metric "
             "(boot is inverted — lower latency is better). Ratio is against the geomean of c8a and m8a.*")
    L.append("")
    # bar chart
    made = _make_chart(labels, norms, chart_path)
    if made:
        L.append(f"![OCI AMD Turin normalized to AWS AMD Turin]({os.path.basename(chart_path)})")
        L.append("")
        L.append("*Bars are OCI E6.256 normalized to the AWS AMD baseline (dashed line = AWS = 1.0). "
                 "Above the line = OCI advantage.*")
        L.append("")
    L.append("**Read:** OCI's AMD Turin leads on virtio-net and virtio-block but trails slightly on "
             "cold-start boot and full-socket AES — a mixed result, not a clean sweep. The boot and AES "
             "gaps are small and plausibly reflect host-kernel/VMM tuning and per-SKU clock/power-limit "
             "differences (OCI 9J45 vs AWS 9R45 custom Turin parts), not an architectural difference.")
    L.append("")
    return "\n".join(L)

def _make_chart(labels, norms, path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return False
    fig, ax = plt.subplots(figsize=(8, 4.2))
    colors = ["#2e7d32" if v >= 1.0 else "#c62828" for v in norms]
    bars = ax.bar(labels, norms, color=colors, width=0.55)
    ax.axhline(1.0, ls="--", color="#555", lw=1)
    ax.set_ylabel("OCI performance ÷ AWS (AMD Turin)")
    ax.set_title("Firecracker on AMD Turin: OCI E6.256 normalized to AWS baseline")
    ax.set_ylim(0, max(1.35, max(norms) * 1.15))
    for b, v in zip(bars, norms):
        ax.text(b.get_x() + b.get_width()/2, v + 0.02, f"{v:.2f}×", ha="center", fontsize=10)
    ax.text(0.99, 1.02, "AWS baseline = 1.0", transform=ax.get_yaxis_transform(),
            ha="right", fontsize=8, color="#555")
    plt.xticks(rotation=15, ha="right", fontsize=9)
    plt.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return True

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results_dir")
    ap.add_argument("--aws-dir", default=None, help="dir with the AWS baseline result JSONs (c8a/m8a)")
    ap.add_argument("-o", "--out", default=None)
    a = ap.parse_args()
    d = load(a.results_dir)
    if BASE not in d or GEN not in d:
        print("ERROR: need at least E5.192 and E6.Ax.192 result JSONs", file=sys.stderr); sys.exit(1)
    md = build(d)
    # append cross-cloud section if AWS baseline available
    aws_dir = a.aws_dir or os.path.join(os.path.dirname(a.results_dir.rstrip("/")), "run-20260629")
    aws_val = _aws_baseline(aws_dir) if os.path.isdir(aws_dir) else None
    aws_hosts = _aws_hosts(aws_dir) if os.path.isdir(aws_dir) else None
    if aws_val and aws_hosts and a.out:
        chart_path = os.path.join(os.path.dirname(os.path.abspath(a.out)), "oci-vs-aws-amd-turin.png")
        md = md + "\n" + cross_cloud_section(d, aws_val, aws_hosts, chart_path)
    if a.out:
        open(a.out, "w").write(md)
        print("wrote", a.out)
    else:
        print(md)

if __name__ == "__main__":
    main()
