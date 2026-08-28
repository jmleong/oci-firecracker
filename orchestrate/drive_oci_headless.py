#!/usr/bin/env python3
"""
drive_oci_headless.py — SSH-free OCI orchestration for the Firecracker bench.

Why this exists: the Cowork sandbox (and likely the corporate laptop) can't open
outbound SSH/22 — Zscaler only allows web ports. So instead of driving each host
over SSH, we hand each instance a cloud-init script that self-provisions, runs
the whole suite, and uploads result.json to OCI Object Storage via a pre-auth
write URL (PAR). The sandbox then harvests results over HTTPS (which works
through Zscaler). No SSH anywhere.

Reuses config + clients + networking from drive_oci.py.

Subcommands:
  bucket              Ensure the results bucket + a write PAR; cache the PAR locally.
  launch <shape>      Ensure net + PAR, then launch <shape> with a self-running
                      cloud-init (embeds the patched host/ harness as a tarball).
  harvest             Download every <name>.json (and .cloudinit.log) that has
                      landed in the bucket into results/collected/.
  status              Show which expected results have landed vs still pending.
  down / list / net   (use drive_oci.py — unchanged)

Flow for the full run:
  python3 drive_oci.py net
  python3 drive_oci_headless.py launch BM.Standard.E6.Ax.192
  python3 drive_oci_headless.py launch BM.Standard.E5.192
  python3 drive_oci_headless.py launch BM.Standard.E6.256
  ...wait ~15-20 min...
  python3 drive_oci_headless.py harvest
  python3 drive_oci.py down --all         # stop billing when results are in
"""
import argparse, base64, datetime, io, os, sys, tarfile, glob
import oci

import drive_oci as D   # reuse config + clients (no side effects at import)

BUCKET   = os.environ.get("OCI_BUCKET", "fcbench-results")
PAR_FILE = f"{D.STAGE}/fcbench_par.txt"          # cached "https://.../p/<tok>/n/.../o/" base
HARNESS  = D.HARNESS_DIR                          # firecracker-bench/host

def _os_client():
    cfg = oci.config.from_file(file_location=D.OCI_CONFIG); cfg["region"] = D.REGION
    c = oci.object_storage.ObjectStorageClient(cfg); c.base_client.session.verify = D.CA_BUNDLE
    return c, c.get_namespace().data

# --------------------------------------------------------------- bucket + PAR
def ensure_bucket_par(days=7):
    os_, ns = _os_client()
    try:
        os_.create_bucket(ns, oci.object_storage.models.CreateBucketDetails(
            name=BUCKET, compartment_id=D.COMPARTMENT, public_access_type="NoPublicAccess"))
    except oci.exceptions.ServiceError as e:
        if e.code != "BucketAlreadyExists":
            raise
    exp = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days)
    par = os_.create_preauthenticated_request(ns, BUCKET,
        oci.object_storage.models.CreatePreauthenticatedRequestDetails(
            name=f"fcbench-write-{int(exp.timestamp())}",
            access_type="AnyObjectWrite", time_expires=exp)).data
    base = f"https://objectstorage.{D.REGION}.oraclecloud.com{par.access_uri}"
    open(PAR_FILE, "w").write(base)
    return base

def _par_base():
    if os.path.exists(PAR_FILE):
        return open(PAR_FILE).read().strip()
    return ensure_bucket_par()

def cmd_bucket(args):
    print("PAR base:", ensure_bucket_par())

# ------------------------------------------------------------- cloud-init build
def _harness_tarball_b64():
    """gzip the patched host/ dir into a base64 blob for embedding in user_data."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(HARNESS, arcname="host")
    return base64.b64encode(buf.getvalue()).decode()

def _cloud_init(par_base):
    tb = _harness_tarball_b64()
    # Self-contained: unpack harness, run suite, upload result.json + logs to PAR.
    # Object name is "<shape>-<hostname>"; harvest keys off the ".done" sentinel.
    script = f"""#!/bin/bash
set -x
exec > /var/log/fcbench-cloudinit.log 2>&1
PAR="{par_base}"
SHAPE=$(curl -s -H "Authorization: Bearer Oracle" http://169.254.169.254/opc/v2/instance/ \\
        | sed -n 's/.*"shape"[ ]*:[ ]*"\\([^"]*\\)".*/\\1/p' | head -1)
NAME="${{SHAPE:-unknown}}-$(hostname)"
mkdir -p /opt/fcbench-src
echo "{tb}" | base64 -d | tar -xzf - -C /opt/fcbench-src
chmod +x /opt/fcbench-src/host/*.sh
# run the full suite (no S3 arg -> writes /opt/fcbench/results/result.json)
bash /opt/fcbench-src/host/run_all.sh
# upload results + this log so we can debug headlessly
curl -s -T /opt/fcbench/results/result.json "${{PAR}}${{NAME}}.json" || true
curl -s -T /var/log/fcbench-cloudinit.log "${{PAR}}${{NAME}}.cloudinit.log" || true
echo "done $(date -u +%FT%TZ)" > /tmp/fcbench.done
curl -s -T /tmp/fcbench.done "${{PAR}}${{NAME}}.done" || true
"""
    b = base64.b64encode(script.encode()).decode()
    if len(b) > 32000:
        raise SystemExit(f"user_data too large ({len(b)} b64 bytes > 32KB). "
                         "Fall back to git-clone in cloud-init.")
    return b

# ------------------------------------------------------------------- launch
def cmd_launch(args):
    cfg, net, cmp = D._clients()
    par = _par_base()
    ud = _cloud_init(par)
    subnet = D._subnet_id(net)
    shape = args.shape
    last = None
    for ad in D.AD_ORDER:
        try:
            print(f"launching {shape} in {ad} (headless cloud-init) ...")
            det = oci.core.models.LaunchInstanceDetails(
                compartment_id=D.COMPARTMENT, availability_domain=ad, shape=shape,
                display_name=f"fcbench-{shape.split('.')[-1].lower()}",
                freeform_tags=D.TAG_NS_FREE,
                source_details=oci.core.models.InstanceSourceViaImageDetails(image_id=D.IMAGE_OCID),
                create_vnic_details=oci.core.models.CreateVnicDetails(
                    subnet_id=subnet, assign_public_ip=True),
                metadata={"user_data": ud})
            inst = cmp.launch_instance(det).data
            print(f"launched {shape}: {inst.id}")
            print("  it will self-run and upload results to the bucket; no SSH needed.")
            return inst.id
        except oci.exceptions.ServiceError as e:
            last = e
            if e.code in ("LimitExceeded","QuotaExceeded","OutOfHostCapacity","InternalError"):
                print(f"  {ad}: {e.code}; trying next AD"); continue
            raise
    raise SystemExit(f"could not launch {shape}: {last.code if last else '?'} {getattr(last,'message','')}")

# ------------------------------------------------------------------- harvest
def cmd_harvest(args):
    os_, ns = _os_client()
    os.makedirs(D.COLLECT_DIR, exist_ok=True)
    objs = oci.pagination.list_call_get_all_results(os_.list_objects, ns, BUCKET).data.objects
    names = [o.name for o in objs]
    dones = [n for n in names if n.endswith(".done")]
    got = 0
    for n in names:
        if n.endswith(".json") or n.endswith(".cloudinit.log"):
            data = os_.get_object(ns, BUCKET, n).data.content
            open(os.path.join(D.COLLECT_DIR, n), "wb").write(data)
            if n.endswith(".json"): got += 1
    print(f"harvested {got} result.json ({len(dones)} hosts signalled done) -> {D.COLLECT_DIR}")
    for n in sorted(dones):
        print("  done:", n[:-5])

def cmd_status(args):
    os_, ns = _os_client()
    try:
        objs = oci.pagination.list_call_get_all_results(os_.list_objects, ns, BUCKET).data.objects
    except oci.exceptions.ServiceError as e:
        raise SystemExit(f"bucket read failed: {e.code}")
    for o in sorted(objs, key=lambda z: z.name):
        print(f"  {o.size:>8}  {o.name}")

def main():
    ap = argparse.ArgumentParser(description="Headless (SSH-free) OCI Firecracker driver")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("bucket")
    p = sub.add_parser("launch"); p.add_argument("shape")
    sub.add_parser("harvest")
    sub.add_parser("status")
    args = ap.parse_args()
    {"bucket": cmd_bucket, "launch": cmd_launch, "harvest": cmd_harvest,
     "status": cmd_status}[args.cmd](args)

if __name__ == "__main__":
    main()
