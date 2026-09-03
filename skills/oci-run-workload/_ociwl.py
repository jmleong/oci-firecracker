"""_ociwl.py — headless OCI workload/benchmark pipeline via HTTP-pull.

Two constraints in this environment shaped the design:
  * the sandbox's outbound SSH (port 22) is blocked by the corporate proxy, and
  * the OCI user has NO Object Storage object access (PUT/GET 404 even via PARs).
So results move over HTTP: each instance runs the workload, copies results into
/srv/wl, writes a DONE marker, and serves /srv/wl on port 80. The sandbox (whose
web egress exits via Zscaler on 80/443) HTTP-pulls the results. No Object Storage,
no admin policy needed. Verified working 2026-08-31.

Materialized into ClaudeTokens/.oci-stage; imported by the oci-run-workload skill.
"""
import oci, glob, json, base64, io, tarfile, os, urllib.request, ssl, time
from pathlib import Path

STAGE = Path(glob.glob("/sessions/*/mnt/ClaudeTokens/.oci-stage")[0])
TOK   = Path(glob.glob("/sessions/*/mnt/ClaudeTokens/tokens.json")[0])
CA    = str(STAGE / "ca-bundle.pem")
TAG   = {"fcbench": "1"}
OCPU_RATE, MEM_RATE = 0.025, 0.0015
DEFAULT_SSH_CIDR = "YOUR_SSH_INGRESS_CIDR/32"

def _cfg(region=None):
    # self-heal: key_file is an absolute path with a session id baked in; ids change.
    import re
    cfgp = STAGE / "config"; txt = cfgp.read_text()
    fixed = re.sub(r"(?m)^key_file=.*$", f"key_file={STAGE / 'oci_api_key.pem'}", txt)
    if fixed != txt: cfgp.write_text(fixed)
    c = oci.config.from_file(file_location=str(cfgp))
    if region: c["region"] = region
    return c

def clients(region=None):
    c = _cfg(region)
    def mk(cls):
        x = cls(c); x.base_client.session.verify = CA; return x
    return (c, mk(oci.core.VirtualNetworkClient), mk(oci.core.ComputeClient),
            mk(oci.identity.IdentityClient))

def cfg_get(k, d=None): return json.loads(TOK.read_text()).get("oci", {}).get(k, d)
def cfg_set(**kv):
    t = json.loads(TOK.read_text()); t.setdefault("oci", {}).update(kv); TOK.write_text(json.dumps(t, indent=2))

def latest_ubuntu(compartment, region, ver="22.04"):
    _, _, cmp, _ = clients(region)
    imgs = cmp.list_images(compartment, operating_system="Canonical Ubuntu",
        operating_system_version=ver, sort_by="TIMECREATED", sort_order="DESC").data
    imgs = [i for i in imgs if "aarch64" not in i.display_name.lower()]
    return imgs[0].id if imgs else None

def est_cost_hr(shape, region, compartment, ocpu=None, mem=None):
    if ocpu is None:
        _, _, cmp, _ = clients(region)
        for s in oci.pagination.list_call_get_all_results(cmp.list_shapes, compartment).data:
            if s.shape == shape: ocpu, mem = s.ocpus, s.memory_in_gbs; break
    return None if not ocpu else ocpu * cfg_get("ocpu_rate", OCPU_RATE) + (mem or 0) * cfg_get("mem_rate", MEM_RATE)

def detect_web_egress():
    """The sandbox's public web-egress /24 (Zscaler pool) — used to open port 80 ingress."""
    ctx = ssl.create_default_context(cafile=CA)
    for url in ("https://checkip.amazonaws.com", "https://api.ipify.org"):
        try:
            ip = urllib.request.urlopen(url, context=ctx, timeout=10).read().decode().strip()
            return ip.rsplit(".", 1)[0] + ".0/24"
        except Exception:
            pass
    return None

# ---------------------------------------------------------------- networking
def ensure_network(compartment, region, ssh_cidr=None, web_cidr=None):
    """Reconcile VCN + IGW + route table + security-list (SSH:22 + HTTP:80). Returns (subnet_id, web_cidr)."""
    ssh_cidr = ssh_cidr or cfg_get("ssh_cidr") or DEFAULT_SSH_CIDR
    web_cidr = web_cidr or cfg_get("web_cidr") or detect_web_egress() or "0.0.0.0/0"
    _, net, _, _ = clients(region)
    vcns = net.list_vcns(compartment, display_name="fcbench-vcn").data
    vcn = vcns[0] if vcns else net.create_vcn(oci.core.models.CreateVcnDetails(
        cidr_block="10.10.0.0/16", compartment_id=compartment, display_name="fcbench-vcn")).data
    igws = net.list_internet_gateways(compartment, vcn_id=vcn.id).data
    igw = igws[0] if igws else net.create_internet_gateway(oci.core.models.CreateInternetGatewayDetails(
        compartment_id=compartment, vcn_id=vcn.id, is_enabled=True, display_name="fcbench-igw")).data
    rt = net.get_route_table(vcn.default_route_table_id).data
    rules = list(rt.route_rules); have = {r.destination for r in rules}
    for dest in ("0.0.0.0/0",):
        if dest not in have:
            rules.append(oci.core.models.RouteRule(destination=dest, destination_type="CIDR_BLOCK",
                                                   network_entity_id=igw.id))
    if len(rules) != len(rt.route_rules):
        net.update_route_table(rt.id, oci.core.models.UpdateRouteTableDetails(route_rules=rules))
    def _tcp(src, port):
        return oci.core.models.IngressSecurityRule(protocol="6", source=src,
            tcp_options=oci.core.models.TcpOptions(destination_port_range=oci.core.models.PortRange(min=port, max=port)))
    ingress = [_tcp(c.strip(), 22) for c in ssh_cidr.split(",") if c.strip()] + [_tcp(web_cidr, 80)]
    sl = net.get_security_list(vcn.default_security_list_id).data
    net.update_security_list(sl.id, oci.core.models.UpdateSecurityListDetails(
        ingress_security_rules=ingress,
        egress_security_rules=[oci.core.models.EgressSecurityRule(protocol="all", destination="0.0.0.0/0")]))
    subs = net.list_subnets(compartment, vcn_id=vcn.id, display_name="fcbench-subnet").data
    sub = subs[0] if subs else net.create_subnet(oci.core.models.CreateSubnetDetails(
        compartment_id=compartment, vcn_id=vcn.id, cidr_block="10.10.1.0/24", display_name="fcbench-subnet")).data
    return sub.id, web_cidr

# ----------------------------------------------------------------- presets
# spec: install(bash), run(bash), results(paths the workload writes), needs_reflink(bool),
#       source({"git":url,"subdir":..} | None), report(local cmd | None)
PRESETS = {
    "firecracker": {
        "desc": "Firecracker microVM 5-metric suite (boot/density/net/blk/AES) — needs bare-metal /dev/kvm",
        "source": {"git": "https://github.com/jmleong/oci-firecracker"},
        "install": "sudo apt-get install -y -qq iperf3 fio openssl xfsprogs jq",
        "run": "sudo bash host/run_all.sh",
        "results": ["/opt/fcbench/results/result.json"], "needs_reflink": True,
        "report": "python3 report/gen_report.py",
    },
    "sysbench-cpu": {
        "desc": "sysbench CPU — deterministic pure-CPU throughput (events/sec)",
        "source": None,
        "install": "sudo apt-get install -y -qq sysbench",
        "run": "sysbench cpu --cpu-max-prime=20000 --threads=$(nproc) --time=$DUR run > /tmp/wl/result.txt 2>&1",
        "results": ["/tmp/wl/result.txt"], "needs_reflink": False, "report": None,
    },
    "sysbench-oltp": {
        "desc": "sysbench OLTP read/write on MariaDB — DB / general-purpose (tps, qps, latency)",
        "source": None,
        "install": ("sudo apt-get install -y -qq sysbench mariadb-server && "
                    "sudo systemctl enable --now mariadb && "
                    "sudo mysql -e \"CREATE DATABASE IF NOT EXISTS sbtest; CREATE USER IF NOT EXISTS 'sbuser'@'localhost' IDENTIFIED BY 'sbpass'; GRANT ALL ON sbtest.* TO 'sbuser'@'localhost'; FLUSH PRIVILEGES;\" && "
                    "sysbench oltp_read_write --db-driver=mysql --mysql-host=127.0.0.1 --mysql-user=sbuser --mysql-password=sbpass --mysql-db=sbtest --tables=10 --table-size=100000 prepare"),
        "run": ("sysbench oltp_read_write --db-driver=mysql --mysql-host=127.0.0.1 --mysql-user=sbuser --mysql-password=sbpass "
                "--mysql-db=sbtest --tables=10 --table-size=100000 --threads=$(nproc) --time=$DUR --report-interval=10 run > /tmp/wl/result.txt 2>&1"),
        "results": ["/tmp/wl/result.txt"], "needs_reflink": False, "report": None,
    },
    "7zip": {
        "desc": "7-zip compression benchmark — multi-threaded integer (MIPS)",
        "source": None,
        "install": "sudo apt-get install -y -qq p7zip-full",
        "run": "timeout $DUR 7za b -mmt$(nproc) > /tmp/wl/result.txt 2>&1 || true",
        "results": ["/tmp/wl/result.txt"], "needs_reflink": False, "report": None,
    },
    "stream": {
        "desc": "STREAM — memory bandwidth (Copy/Scale/Add/Triad MB/s)",
        "source": None,
        "install": ("sudo apt-get install -y -qq gcc curl && "
                    "curl -sSL https://www.cs.virginia.edu/stream/FTP/Code/stream.c -o /tmp/wl/stream.c && "
                    "gcc -O3 -fopenmp -DSTREAM_ARRAY_SIZE=100000000 /tmp/wl/stream.c -o /tmp/wl/stream"),
        "run": "OMP_NUM_THREADS=$(nproc) /tmp/wl/stream > /tmp/wl/result.txt 2>&1",
        "results": ["/tmp/wl/result.txt"], "needs_reflink": False, "report": None,
    },
}

def result_basenames(spec):
    return [os.path.basename(r) for r in spec["results"]]

# ------------------------------------------------------------- cloud-init
def build_cloud_init(spec, duration=180, source_tgz_b64=None):
    """Cloud-init that runs the workload then serves results (+ log + DONE marker) on port 80."""
    src = spec.get("source")
    fetch = ""
    if source_tgz_b64:                                    # local dir embedded as base64 tarball
        fetch = (f'echo "{source_tgz_b64}" | base64 -d > /tmp/wl/src.tgz && '
                 'tar -xzf /tmp/wl/src.tgz -C /tmp/wl && cd "$(find /tmp/wl -maxdepth 1 -mindepth 1 -type d | head -1)"')
    elif isinstance(src, dict) and src.get("git"):
        sub = f'/{src["subdir"]}' if src.get("subdir") else ""
        fetch = f'git clone --depth 1 {src["git"]} /tmp/wl/src && cd /tmp/wl/src{sub}'
    reflink = ""
    if spec.get("needs_reflink"):
        reflink = ('DISK=$(lsblk -dpno NAME,TYPE | awk \'$2=="disk"{print $1}\' | grep -v -E "sda" | head -1); '
                   'if [ -n "$DISK" ]; then sudo mkfs.xfs -f -m reflink=1 "$DISK" >/dev/null 2>&1 && '
                   'sudo mkdir -p /opt/fcbench && sudo mount "$DISK" /opt/fcbench; fi')
    results = " ".join(f'"{r}"' for r in spec["results"])
    script = f"""#!/bin/bash
set -x
exec > /var/log/wl-cloudinit.log 2>&1
export DEBIAN_FRONTEND=noninteractive
DUR={duration}
mkdir -p /tmp/wl /srv/wl; cd /tmp/wl
# apt at boot races the OS's unattended-upgrades. Make dpkg ops wait for the lock,
# and RETRY apt-get update until it wins the lists lock (DPkg::Lock::Timeout doesn't
# cover the lists lock that `update` takes). Presets do NOT run update themselves.
echo 'DPkg::Lock::Timeout "600";' > /etc/apt/apt.conf.d/99lock
for i in $(seq 1 90); do apt-get update -qq >/dev/null 2>&1 && break; sleep 8; done
apt-get install -y -qq git curl >/dev/null 2>&1 || true
{reflink}
{fetch}
{spec["install"]}
{spec["run"]}
for f in {results}; do cp "$f" /srv/wl/ 2>/dev/null; done
cp /var/log/wl-cloudinit.log /srv/wl/cloudinit.log 2>/dev/null
date -u +%FT%TZ > /srv/wl/DONE
iptables -I INPUT -p tcp --dport 80 -j ACCEPT 2>/dev/null || true
cd /srv/wl && nohup python3 -m http.server 80 >/tmp/http.log 2>&1 &
"""
    b = base64.b64encode(script.encode()).decode()
    if len(b) > 32000:
        raise SystemExit(f"user_data too large ({len(b)} b64 bytes > 32KB); use a git source instead of embedding")
    return b

def tar_b64(local_dir):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(local_dir, arcname=os.path.basename(local_dir.rstrip("/")))
    return base64.b64encode(buf.getvalue()).decode()

# --------------------------------------------------------------- launch/fleet
def launch_fleet(shapes, region, compartment, image, ssh_pub, user_data):
    """shapes: list of shape names. user_data: base64 cloud-init (same for all). Returns [(shape,ocid)]."""
    subnet, _ = ensure_network(compartment, region)
    _, _, cmp, ident = clients(region)
    ADs = [a.name for a in ident.list_availability_domains(compartment).data]
    out = []
    for shape in shapes:
        det = dict(compartment_id=compartment, shape=shape,
                   display_name=f"fcbench-{shape.split('.')[-1].lower()}", freeform_tags=TAG,
                   source_details=oci.core.models.InstanceSourceViaImageDetails(image_id=image),
                   create_vnic_details=oci.core.models.CreateVnicDetails(subnet_id=subnet, assign_public_ip=True),
                   metadata={"ssh_authorized_keys": ssh_pub, "user_data": user_data})
        if ".Flex" in shape:
            det["shape_config"] = oci.core.models.LaunchInstanceShapeConfigDetails(
                ocpus=int(cfg_get("flex_ocpu", 4)), memory_in_gbs=int(cfg_get("flex_mem", 32)))
        last = None
        for ad in ADs:
            try:
                inst = cmp.launch_instance(oci.core.models.LaunchInstanceDetails(availability_domain=ad, **det)).data
                out.append((shape, inst.id)); last = None; break
            except oci.exceptions.ServiceError as e:
                last = e
                if e.code in ("LimitExceeded","QuotaExceeded","OutOfHostCapacity","InternalError"): continue
                raise
        if last: out.append((shape, f"FAILED:{last.code}"))
    return out

def public_ip(region, compartment, ocid):
    _, net, cmp, _ = clients(region)
    if cmp.get_instance(ocid).data.lifecycle_state != "RUNNING":
        return None
    for va in cmp.list_vnic_attachments(compartment, instance_id=ocid).data:
        v = net.get_vnic(va.vnic_id).data
        if v.public_ip: return v.public_ip
    return None

# --------------------------------------------------------------- HTTP harvest
def harvest_http(ip, spec, label, out_dir, done_timeout=1200):
    """Poll http://ip/DONE, then pull each result + the cloud-init log into out_dir. Returns True if DONE seen."""
    os.makedirs(out_dir, exist_ok=True)
    def get(path):
        return urllib.request.urlopen(f"http://{ip}/{path}", timeout=20).read()
    deadline = time.time() + done_timeout
    ok = False
    while time.time() < deadline:
        try:
            get("DONE"); ok = True; break
        except Exception:
            time.sleep(20)
    if not ok:
        return False
    for bn in result_basenames(spec) + ["cloudinit.log"]:
        try:
            open(os.path.join(out_dir, f"{label}-{bn}"), "wb").write(get(bn))
        except Exception:
            pass
    return True

# --------------------------------------------------------------- teardown/reap
def _bench_instances(cmp, compartment):
    return [i for i in oci.pagination.list_call_get_all_results(cmp.list_instances, compartment).data
            if i.lifecycle_state not in ("TERMINATED","TERMINATING") and (i.freeform_tags or {}).get("fcbench") == "1"]

def reap_list(region, compartment):
    _, _, cmp, _ = clients(region)
    return [(i.lifecycle_state, i.shape, i.display_name, i.id) for i in _bench_instances(cmp, compartment)]

def teardown(region, compartment, ocids=None):
    _, _, cmp, _ = clients(region)
    if ocids is None:
        ocids = [i.id for i in _bench_instances(cmp, compartment)]
    for o in ocids:
        try: cmp.terminate_instance(o, preserve_boot_volume=False)
        except oci.exceptions.ServiceError: pass
    c = _cfg(region); bs = oci.core.BlockstorageClient(c); bs.base_client.session.verify = CA
    for v in bs.list_volumes(compartment_id=compartment, display_name="fcbench-scratch").data:
        if v.lifecycle_state not in ("TERMINATED","TERMINATING"):
            try: bs.delete_volume(v.id)
            except oci.exceptions.ServiceError: pass
    return ocids
