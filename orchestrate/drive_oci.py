#!/usr/bin/env python3
"""
drive_oci.py — OCI replacement for orchestrate/drive.sh.

Provisions AMD bare-metal shapes on OCI, pushes the cloud-agnostic host/ harness,
runs run_all.sh on each host (results written locally, collected via scp), and
tears the instances down. Uses the OCI Python SDK (no OCI CLI needed).

Nothing here is AWS-specific; the host/ scripts are byte-identical to the ones
that produced the AWS baseline, which is what preserves comparability.

Subcommands:
  net                 Ensure VCN + subnet + IGW + security list (SSH ingress).
  up <shape>          Launch one bare-metal instance of <shape>; print its OCID+IP.
  provision <ip>      rsync host/ to the instance and run install_firecracker.sh.
  run <ip>            Run the full suite (run_all.sh) and collect result.json.
  smoke <ip>          Boot one microVM (vm.sh smoke) — validates KVM works.
  collect <ip> <name> scp the result.json back to results/collected/<name>.json.
  down [ocid|--all]   Terminate an instance (or every bench instance we tagged).
  list                List bench instances we launched (by freeform tag).
  all <shape>         net -> up -> provision -> smoke -> run -> collect (ONE host).

Safety: `up`/`all` launch BILLABLE bare-metal. This script never runs them
implicitly — you invoke them explicitly. Always `down` when finished.

Config comes from ClaudeTokens/.oci-stage/{config,ca-bundle.pem}. Override the
compartment/region/image via the constants below or env vars of the same name.
"""
import argparse, os, subprocess, sys, time, json, glob
from pathlib import Path

import oci

# --- static config (Ashburn / FAE-SOLUTIONS-ARCHITECT, from session setup) ---
def _stage():
    m = glob.glob("/sessions/*/mnt/ClaudeTokens/.oci-stage")
    # fall back to an env-provided path (e.g. when run from Claude Code on Windows)
    return m[0] if m else os.environ.get("OCI_STAGE", str(Path.home() / ".oci-stage"))

def _load_dotenv():
    """Load key=value pairs from config.env (repo root) or $FCBENCH_CONFIG into the
    environment (without overriding already-set vars). Keeps site-specific values —
    OCIDs, allowed SSH CIDRs, AD names — OUT of the source tree. See config.env.example."""
    candidates = [Path(__file__).resolve().parent.parent / "config.env"]
    if os.environ.get("FCBENCH_CONFIG"):
        candidates.insert(0, Path(os.environ["FCBENCH_CONFIG"]))
    for p in candidates:
        if p and p.exists():
            for line in p.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            break
_load_dotenv()

STAGE       = _stage()
OCI_CONFIG  = os.environ.get("OCI_CONFIG_FILE", f"{STAGE}/config")
CA_BUNDLE   = os.environ.get("OCI_CA_BUNDLE",  f"{STAGE}/ca-bundle.pem")
COMPARTMENT = os.environ.get("OCI_COMPARTMENT", "")     # required — set in config.env
REGION      = os.environ.get("OCI_REGION", "us-ashburn-1")
IMAGE_OCID  = os.environ.get("OCI_IMAGE", "")           # required — region-specific Ubuntu 22.04 image OCID
# Availability-domain names are tenancy-specific (the prefix differs per tenancy);
# provide them comma-separated in config.env, in preference order.
AD_ORDER    = [a.strip() for a in os.environ.get("OCI_ADS", "").split(",") if a.strip()]
SSH_USER    = os.environ.get("OCI_SSH_USER", "ubuntu")  # default user on OCI Ubuntu images
HOST_KEY    = f"{STAGE}/oci_host_ssh"                    # SSH key for host login (auto-created)
TAG_NS_FREE = {"fcbench": "1"}                          # freeform tag to find our instances
VCN_CIDR    = os.environ.get("OCI_VCN_CIDR", "10.10.0.0/16")
SUBNET_CIDR = os.environ.get("OCI_SUBNET_CIDR", "10.10.1.0/24")
NAME_VCN    = "fcbench-vcn"
NAME_SUBNET = "fcbench-subnet"
HARNESS_DIR = str(Path(__file__).resolve().parent.parent / "host")
COLLECT_DIR = str(Path(__file__).resolve().parent.parent / "results" / "collected")

def _clients():
    cfg = oci.config.from_file(file_location=OCI_CONFIG)
    cfg["region"] = REGION
    net = oci.core.VirtualNetworkClient(cfg); net.base_client.session.verify = CA_BUNDLE
    cmp = oci.core.ComputeClient(cfg);        cmp.base_client.session.verify = CA_BUNDLE
    return cfg, net, cmp

def _ensure_host_key():
    if not os.path.exists(HOST_KEY):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-f", HOST_KEY, "-N", ""], check=True)
    return open(HOST_KEY + ".pub").read().strip()

def _ssh_ingress_cidrs():
    # CIDRs allowed to SSH into the benchmark hosts. Set OCI_SSH_CIDR (comma-separated)
    # in config.env to the public egress range(s) of wherever you run this driver.
    #
    # GOTCHA worth knowing: behind a forward proxy/VPN, your *web* (HTTPS) egress IP
    # can differ from your *raw-TCP* egress IP. If you allow only the IP that an HTTPS
    # "what's my IP" service reports, OCI may silently drop SSH (a timeout that looks
    # like a firewall block) because your SSH packets arrive from a different address.
    # When unsure, allow your whole corporate/VPN egress range, then narrow it.
    env = os.environ.get("OCI_SSH_CIDR")
    if env:
        return [c.strip() for c in env.split(",") if c.strip()]
    print("WARN: OCI_SSH_CIDR not set — defaulting SSH ingress to 0.0.0.0/0 (open). "
          "Set it in config.env to your egress CIDR.", file=sys.stderr)
    return ["0.0.0.0/0"]

# --------------------------------------------------------------------------- net
def cmd_net(args):
    _, net, _ = _clients()
    # VCN (idempotent by display name)
    vcns = net.list_vcns(COMPARTMENT, display_name=NAME_VCN).data
    if vcns:
        vcn = vcns[0]; print(f"vcn exists: {vcn.id}")
    else:
        vcn = net.create_vcn(oci.core.models.CreateVcnDetails(
            cidr_block=VCN_CIDR, compartment_id=COMPARTMENT, display_name=NAME_VCN)).data
        print(f"created vcn: {vcn.id}")
    # Internet gateway
    igws = net.list_internet_gateways(COMPARTMENT, vcn_id=vcn.id).data
    igw = igws[0] if igws else net.create_internet_gateway(
        oci.core.models.CreateInternetGatewayDetails(
            compartment_id=COMPARTMENT, vcn_id=vcn.id, is_enabled=True,
            display_name="fcbench-igw")).data
    # Default route table -> IGW
    rt = net.get_route_table(vcn.default_route_table_id).data
    if not any(r.network_entity_id == igw.id for r in rt.route_rules):
        net.update_route_table(rt.id, oci.core.models.UpdateRouteTableDetails(
            route_rules=[oci.core.models.RouteRule(
                destination="0.0.0.0/0", destination_type="CIDR_BLOCK",
                network_entity_id=igw.id)]))
        print("route -> igw added")
    # Security list: allow SSH ingress from our CIDRs + all egress
    cidrs = _ssh_ingress_cidrs()
    sl = net.get_security_list(vcn.default_security_list_id).data
    ingress = [oci.core.models.IngressSecurityRule(
        protocol="6", source=c,
        tcp_options=oci.core.models.TcpOptions(
            destination_port_range=oci.core.models.PortRange(min=22, max=22))) for c in cidrs]
    egress = [oci.core.models.EgressSecurityRule(protocol="all", destination="0.0.0.0/0")]
    net.update_security_list(sl.id, oci.core.models.UpdateSecurityListDetails(
        ingress_security_rules=ingress, egress_security_rules=egress))
    print(f"security list: SSH ingress from {cidrs}, all egress")
    # Subnet (idempotent by display name)
    subs = net.list_subnets(COMPARTMENT, vcn_id=vcn.id, display_name=NAME_SUBNET).data
    if subs:
        sub = subs[0]; print(f"subnet exists: {sub.id}")
    else:
        sub = net.create_subnet(oci.core.models.CreateSubnetDetails(
            compartment_id=COMPARTMENT, vcn_id=vcn.id, cidr_block=SUBNET_CIDR,
            display_name=NAME_SUBNET)).data
        print(f"created subnet: {sub.id}")
    return sub.id

def _subnet_id(net):
    vcn = net.list_vcns(COMPARTMENT, display_name=NAME_VCN).data
    if not vcn:
        raise SystemExit("no VCN — run `net` first")
    subs = net.list_subnets(COMPARTMENT, vcn_id=vcn[0].id, display_name=NAME_SUBNET).data
    if not subs:
        raise SystemExit("no subnet — run `net` first")
    return subs[0].id

# ---------------------------------------------------------------------------- up
def cmd_up(args):
    cfg, net, cmp = _clients()
    shape = args.shape
    pubkey = _ensure_host_key()
    subnet = _subnet_id(net)
    last_err = None
    for ad in AD_ORDER:
        try:
            print(f"launching {shape} in {ad} ...")
            details = oci.core.models.LaunchInstanceDetails(
                compartment_id=COMPARTMENT, availability_domain=ad,
                shape=shape, display_name=f"fcbench-{shape.split('.')[-1].lower()}",
                freeform_tags=TAG_NS_FREE,
                source_details=oci.core.models.InstanceSourceViaImageDetails(image_id=IMAGE_OCID),
                create_vnic_details=oci.core.models.CreateVnicDetails(
                    subnet_id=subnet, assign_public_ip=True),
                metadata={"ssh_authorized_keys": pubkey})
            inst = cmp.launch_instance(details).data
            print(f"launched: {inst.id}  (waiting for RUNNING + public IP)")
            inst = oci.wait_until(cmp, cmp.get_instance(inst.id), "lifecycle_state",
                                  "RUNNING", max_wait_seconds=1200).data
            ip = _public_ip(cfg, net, cmp, inst.id)
            print(f"RUNNING  ocid={inst.id}  ip={ip}")
            return inst.id, ip
        except oci.exceptions.ServiceError as e:
            last_err = e
            if e.code in ("LimitExceeded", "QuotaExceeded", "OutOfHostCapacity", "InternalError"):
                print(f"  {ad}: {e.code} — {e.message}; trying next AD")
                continue
            raise
    raise SystemExit(f"could not launch {shape} in any AD: {last_err.code if last_err else '?'} "
                     f"{last_err.message if last_err else ''}")

def _public_ip(cfg, net, cmp, instance_id):
    vas = cmp.list_vnic_attachments(COMPARTMENT, instance_id=instance_id).data
    for va in vas:
        vnic = net.get_vnic(va.vnic_id).data
        if vnic.public_ip:
            return vnic.public_ip
    return None

# ----------------------------------------------------------------- ssh helpers
SSH_BASE = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=10", "-o", "LogLevel=ERROR"]

def _wait_ssh(ip, timeout=300):
    print(f"waiting for host SSH on {ip} ...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = subprocess.run(["ssh", "-i", HOST_KEY, *SSH_BASE, f"{SSH_USER}@{ip}", "true"],
                           capture_output=True)
        if r.returncode == 0:
            print("host SSH up"); return True
        time.sleep(5)
    raise SystemExit(f"host SSH never came up on {ip}")

def _ssh(ip, cmd):
    return subprocess.run(["ssh", "-i", HOST_KEY, *SSH_BASE, f"{SSH_USER}@{ip}", cmd])

def cmd_provision(args):
    ip = args.ip
    _wait_ssh(ip)
    print("rsyncing host/ harness ...")
    subprocess.run(["rsync", "-az", "-e", f"ssh -i {HOST_KEY} " + " ".join(SSH_BASE),
                    HARNESS_DIR + "/", f"{SSH_USER}@{ip}:/tmp/host/"], check=True)
    print("running install_firecracker.sh (sudo) ...")
    _ssh(ip, "sudo bash /tmp/host/install_firecracker.sh")

def cmd_smoke(args):
    _ssh(args.ip, "sudo bash /tmp/host/vm.sh smoke")

def cmd_run(args):
    ip = args.ip
    print("running run_all.sh (no S3 arg -> local result.json) ...")
    # run detached-safe but wait synchronously (suite ~10 min); capture stdout tail
    _ssh(ip, "sudo bash /tmp/host/run_all.sh")
    cmd_collect(argparse.Namespace(ip=ip, name=args.name or "host"))

def cmd_collect(args):
    os.makedirs(COLLECT_DIR, exist_ok=True)
    dest = os.path.join(COLLECT_DIR, f"{args.name}.json")
    print(f"collecting result.json -> {dest}")
    subprocess.run(["scp", "-i", HOST_KEY, *SSH_BASE,
                    f"{SSH_USER}@{args.ip}:/opt/fcbench/results/result.json", dest], check=True)
    try:
        print(json.dumps(json.load(open(dest))["host"], indent=2))
    except Exception:
        pass

# -------------------------------------------------------------------- teardown
def _bench_instances(cmp):
    out = []
    for it in oci.pagination.list_call_get_all_results(
            cmp.list_instances, COMPARTMENT).data:
        if it.lifecycle_state in ("TERMINATED", "TERMINATING"):
            continue
        if (it.freeform_tags or {}).get("fcbench") == "1":
            out.append(it)
    return out

def cmd_list(args):
    _, _, cmp = _clients()
    for it in _bench_instances(cmp):
        print(f"{it.lifecycle_state:10} {it.shape:24} {it.display_name:22} {it.id}")

def cmd_down(args):
    _, _, cmp = _clients()
    if args.all:
        targets = [it.id for it in _bench_instances(cmp)]
    elif args.ocid:
        targets = [args.ocid]
    else:
        raise SystemExit("down needs an OCID or --all")
    for ocid in targets:
        print(f"terminating {ocid}")
        cmp.terminate_instance(ocid, preserve_boot_volume=False)
    print(f"requested termination of {len(targets)} instance(s)")

def cmd_attachvol(args):
    """Create + attach a scratch block volume (for shapes with no local NVMe, e.g.
    E5.192). Bare metal supports ISCSI attachment only. Prints the iscsiadm
    connection info the host needs (IQN IP PORT) so the caller can log in; then
    oci_setup_storage.sh formats the disk XFS-reflink for the overlays."""
    cfg, _, cmp = _clients()
    bs = oci.core.BlockstorageClient(cfg); bs.base_client.session.verify = CA_BUNDLE
    inst = cmp.get_instance(args.ocid).data
    # reuse an existing AVAILABLE scratch volume if one is lying around
    vol = None
    for v in bs.list_volumes(compartment_id=COMPARTMENT,
                             availability_domain=inst.availability_domain,
                             display_name="fcbench-scratch").data:
        if v.lifecycle_state == "AVAILABLE":
            vol = v; break
    if vol is None:
        # vpus_per_gb: 10=Balanced (default), 20=Higher Performance, 30+=Ultra High.
        # Use Higher Performance so an E5 (no local NVMe) is measured on the storage a
        # perf-conscious Firecracker customer would actually provision, not the slowest tier.
        vol = bs.create_volume(oci.core.models.CreateVolumeDetails(
            compartment_id=COMPARTMENT, availability_domain=inst.availability_domain,
            display_name="fcbench-scratch", size_in_gbs=int(args.size),
            vpus_per_gb=int(getattr(args, "vpus", 20) or 20),
            freeform_tags=TAG_NS_FREE)).data
        vol = oci.wait_until(bs, bs.get_volume(vol.id), "lifecycle_state", "AVAILABLE",
                             max_wait_seconds=300).data
    att = cmp.attach_volume(oci.core.models.AttachVolumeDetails(
        instance_id=args.ocid, volume_id=vol.id, type="iscsi",
        display_name="fcbench-scratch")).data
    att = oci.wait_until(cmp, cmp.get_volume_attachment(att.id), "lifecycle_state",
                         "ATTACHED", max_wait_seconds=300).data
    # emit the connection info for host-side iscsiadm login
    print(f"ISCSI {att.iqn} {att.ipv4} {att.port}")

def cmd_all(args):
    """One-host end-to-end: net -> up -> provision -> smoke -> run -> collect."""
    cmd_net(args)
    ocid, ip = cmd_up(args)
    cmd_provision(argparse.Namespace(ip=ip))
    cmd_smoke(argparse.Namespace(ip=ip))
    cmd_run(argparse.Namespace(ip=ip, name=args.shape.split(".")[-2] + "." + args.shape.split(".")[-1]))
    print(f"\nDONE. Instance still RUNNING (billable): {ocid} @ {ip}")
    print(f"Terminate with:  python3 {sys.argv[0]} down {ocid}")

def main():
    ap = argparse.ArgumentParser(description="OCI Firecracker bench driver")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("net")
    p = sub.add_parser("up");        p.add_argument("shape")
    p = sub.add_parser("provision"); p.add_argument("ip")
    p = sub.add_parser("smoke");     p.add_argument("ip")
    p = sub.add_parser("run");       p.add_argument("ip"); p.add_argument("name", nargs="?")
    p = sub.add_parser("collect");   p.add_argument("ip"); p.add_argument("name")
    p = sub.add_parser("down");      p.add_argument("ocid", nargs="?"); p.add_argument("--all", action="store_true")
    sub.add_parser("list")
    p = sub.add_parser("attachvol"); p.add_argument("ocid"); p.add_argument("size", nargs="?", default="200"); p.add_argument("--vpus", default="20")
    p = sub.add_parser("all");       p.add_argument("shape")
    args = ap.parse_args()
    {"net": cmd_net, "up": cmd_up, "provision": cmd_provision, "smoke": cmd_smoke,
     "run": cmd_run, "collect": cmd_collect, "down": cmd_down, "list": cmd_list,
     "attachvol": cmd_attachvol, "all": cmd_all}[args.cmd](args)

if __name__ == "__main__":
    main()
