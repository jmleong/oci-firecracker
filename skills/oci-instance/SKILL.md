---
name: oci-instance
description: "Create and manage OCI compute instances interactively — pick an AMD E5/E6 bare-metal or E-flex VM shape, auto-provision networking (VCN/subnet/security-list), launch via the OCI Python SDK, then list/start/stop/terminate, handling the corporate TLS-proxy CA bundle automatically. Use when you want to spin up, list, or tear down an Oracle Cloud VM from the sandbox, or say \"create an OCI instance\", \"list my OCI instances\", or \"/oci-instance\"."
license: "Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved. Portions of this content consists of AI generated content."
metadata:
  author: jorleong
  version: "1.0.0"
  category: automation
  tags:
    - oci
    - oracle-cloud
    - compute
    - amd-epyc
    - vm-provisioning
    - infrastructure
compatibility:
  universal: true
---

# OCI Instance Creator & Manager

You help the user create and manage Oracle Cloud (OCI) compute instances directly from the
sandbox using the OCI Python SDK. Unlike the GCP flow, OCI is fully drivable here — you launch,
list, and terminate instances yourself. **One limit:** the sandbox's outbound SSH (port 22) is
blocked by the corporate proxy, so you create the instance and hand the user a ready-to-use SSH
command; they connect from their own machine. All instances you create are tagged `fcbench=1` so
you can always find and clean them up.

Work through the steps in order. Ask only for values you don't already know.

---

## Step 0 — Bootstrap auth + helper (always run first)

This ensures the OCI SDK is installed, the credentials + corporate CA bundle are in place, and
writes a helper library (`_ocilib.py`) into the persistent `ClaudeTokens/.oci-stage` folder.

```bash
python3 - <<'PYEOF'
import json, glob, os, subprocess, sys, shutil
from pathlib import Path

# helper library materialized into the persistent stage dir; used by later steps
HELPER = r'''
import oci, glob, json
from pathlib import Path
STAGE = Path(glob.glob("/sessions/*/mnt/ClaudeTokens/.oci-stage")[0])
TOK = Path(glob.glob("/sessions/*/mnt/ClaudeTokens/tokens.json")[0])
CA = str(STAGE / "ca-bundle.pem")
OCPU_RATE = 0.025   # OCI E-series list price $/OCPU-hr (pre-discount; override in tokens.json oci.ocpu_rate)
MEM_RATE  = 0.0015  # $/GB-hr

def clients(region=None):
    c = oci.config.from_file(file_location=str(STAGE / "config"))
    if region: c["region"] = region
    def mk(cls):
        x = cls(c); x.base_client.session.verify = CA; return x
    return (c, mk(oci.core.VirtualNetworkClient), mk(oci.core.ComputeClient),
            mk(oci.identity.IdentityClient))

def cfg_get(key, default=None):        # read a cached site value from tokens.json['oci']
    return json.loads(TOK.read_text()).get("oci", {}).get(key, default)

def cfg_set(**kv):                     # cache site values so we don't re-ask next run
    t = json.loads(TOK.read_text()); t.setdefault("oci", {}).update(kv)
    TOK.write_text(json.dumps(t, indent=2))

def latest_ubuntu(compartment, region, ver="22.04"):   # newest x86_64 Ubuntu image OCID
    _, _, cmp, _ = clients(region)
    imgs = cmp.list_images(compartment, operating_system="Canonical Ubuntu",
        operating_system_version=ver, sort_by="TIMECREATED", sort_order="DESC").data
    imgs = [i for i in imgs if "aarch64" not in i.display_name.lower()]
    return imgs[0].id if imgs else None

def shape_ocpu_mem(shape, region, compartment):        # (ocpus, memory_gb) for a shape
    _, _, cmp, _ = clients(region)
    for s in oci.pagination.list_call_get_all_results(cmp.list_shapes, compartment).data:
        if s.shape == shape:
            return (s.ocpus, s.memory_in_gbs)
    return (None, None)

def est_cost_hr(shape, region, compartment, ocpu=None, mem=None):   # list-price $/hr estimate
    r = cfg_get("ocpu_rate", OCPU_RATE); mr = cfg_get("mem_rate", MEM_RATE)
    if ocpu is None:
        ocpu, mem = shape_ocpu_mem(shape, region, compartment)
    if not ocpu: return None
    return ocpu * r + (mem or 0) * mr
'''

TOK = Path(glob.glob('/sessions/*/mnt/ClaudeTokens/tokens.json')[0])
STAGE = TOK.parent / '.oci-stage'; STAGE.mkdir(exist_ok=True)
t = json.loads(TOK.read_text())
if 'oci' not in t:
    print("ERROR: no 'oci' key in tokens.json. Set up OCI API-key auth first: generate an API "
          "signing key, upload the public half in the OCI Console (Profile > API keys), and store "
          "user/tenancy OCID + fingerprint + region under the 'oci' key in ClaudeTokens/tokens.json.")
    sys.exit(1)
o = t['oci']
# write the OCI config (points key_file at the private key placed during API-key setup)
key = STAGE / 'oci_api_key.pem'
cfg = STAGE / 'config'
cfg.write_text("[DEFAULT]\n" + "".join(
    f"{k}={o[k]}\n" for k in ('user','fingerprint','tenancy','region') if k in o)
    + f"key_file={key}\n")
os.chmod(cfg, 0o600)
if not key.exists():
    print(f"WARN: private key {key} missing — place your API signing private key there "
          "(the half matching the public key uploaded to the OCI Console).")
# ensure SDK
try:
    import oci  # noqa
except ImportError:
    subprocess.run([sys.executable,'-m','pip','install','oci','--break-system-packages','-q'], check=True)
# build combined CA bundle (certifi + corporate/proxy CAs) — REQUIRED behind a TLS-inspecting proxy
import certifi
bundle = STAGE / 'ca-bundle.pem'
shutil.copy(certifi.where(), bundle)
with open(bundle, 'ab') as b:
    for p in sorted(glob.glob('/usr/local/share/ca-certificates/*.crt')):
        b.write(Path(p).read_bytes())
# host SSH key for instance login (public half injected via cloud metadata)
hk = STAGE / 'oci_host_ssh'
if not hk.exists():
    subprocess.run(['ssh-keygen','-q','-t','ed25519','-f',str(hk),'-N',''], check=True)
# materialize the helper
(STAGE / '_ocilib.py').write_text(HELPER)
print("OK bootstrap complete. stage:", STAGE)
PYEOF
```

If the block prints an ERROR about a missing `oci` key, stop and help the user set up API-key auth
before continuing. Otherwise report "OCI ready" and proceed.

**Site values (cached in `tokens.json['oci']`).** The skill needs a **compartment OCID** (ask once,
persist with `O.cfg_set(compartment=...)`). Other values have sensible defaults:
- **Image is auto-resolved** — never ask for an image OCID. `O.latest_ubuntu(compartment, region)`
  returns the newest x86_64 Ubuntu 22.04 image for the region.
- **SSH ingress defaults to the AMD corporate egress ranges + Zscaler**
  (`YOUR_SSH_INGRESS_CIDR`) — these are the ranges an AMD machine actually
  SSHes *out of*. **Do NOT use whatismyipaddress.com** here: on the AMD network that reports the
  web-proxy (Zscaler) IP, but SSH (port 22) egresses via the corporate ranges, so a whatismyip
  `/32` would silently block SSH (timeout that looks like a firewall bug). Override only if the user
  connects from a non-AMD network: `O.cfg_set(ssh_cidr="<their-cidr>[,<another>]")` (comma-separated).
- If the user doesn't know the compartment, list it with `clients()[3].list_compartments(tenancy, ...)`.

---

## Step 1 — What do you want to do?

Ask: **create**, **list**, **start**, **stop**, or **terminate**. Route to the matching section.

---

## Step 2 (create) — Choose a shape

Present AMD-first, and note any valid OCI shape name works:

**AMD bare-metal (EPYC):**
- `BM.Standard.E5.192` — Genoa/Zen4, 192 OCPU
- `BM.Standard.E6.256` — Turin/Zen5, 256 OCPU (local NVMe)
- `BM.Standard.E6.Ax.192` — Turin/Zen5 Acceleron, 192 OCPU (local NVMe)
- `BM.Standard.E4.128` — Milan/Zen3, 128 OCPU

**AMD flexible VM (pick OCPU + memory):** `VM.Standard.E5.Flex`, `VM.Standard.E4.Flex`
(For a flex shape, ask OCPU count and memory GB — default 8 GB/OCPU.)

**Other:** Intel `BM.Standard3.64` / `VM.Standard3.Flex`, Ampere `VM.Standard.A1.Flex`. Any shape the
tenancy is entitled to works — accept a raw shape name too.

Then ask for an **instance name** (suggest `<user>-<shape-suffix>-test`) and confirm the **region**
(default from config).

**Show the cost estimate before launching** (bare-metal is billed hourly and can be expensive):

```bash
python3 - <<'PYEOF'
import sys, glob; sys.path.insert(0, glob.glob("/sessions/*/mnt/ClaudeTokens/.oci-stage")[0])
import _ocilib as O
REGION="us-ashburn-1"; SHAPE="BM.Standard.E6.Ax.192"
COMPARTMENT=O.cfg_get("compartment")
OCPU=None; MEM_GB=None      # set for a *.Flex shape
hr = O.est_cost_hr(SHAPE, REGION, COMPARTMENT, OCPU, MEM_GB)
if hr: print(f"Est. list price: ${hr:,.2f}/hr  (~${hr*730:,.0f}/mo)  — pre-discount")
else:  print("Could not estimate cost for", SHAPE)
PYEOF
```

Show the estimate and get explicit confirmation before creating a bare-metal instance.

---

## Step 3 (create) — Ensure networking, then launch

```bash
python3 - <<'PYEOF'
import sys, glob; sys.path.insert(0, glob.glob("/sessions/*/mnt/ClaudeTokens/.oci-stage")[0])
import _ocilib as O, oci
# ---- EDIT THESE for the request ----
REGION="us-ashburn-1"; SHAPE="BM.Standard.E6.Ax.192"; NAME="my-oci-test"
OCPU=None; MEM_GB=None         # set ints for a *.Flex shape; leave None for bare-metal
NEW_COMPARTMENT=None           # set once to cache a compartment OCID
NEW_SSH_CIDR=None              # set once to cache the user's IP as "<ip>/32"
# ------------------------------------
if NEW_COMPARTMENT or NEW_SSH_CIDR:
    O.cfg_set(**{k:v for k,v in {"compartment":NEW_COMPARTMENT,"ssh_cidr":NEW_SSH_CIDR}.items() if v})
COMPARTMENT = O.cfg_get("compartment")
# SSH ingress: default to AMD corporate egress ranges + Zscaler — the ranges an AMD machine
# actually SSHes out of. (whatismyipaddress shows only the web-proxy IP and would BLOCK SSH.)
SSH_CIDR = O.cfg_get("ssh_cidr") or "YOUR_SSH_INGRESS_CIDR"
assert COMPARTMENT, "Set NEW_COMPARTMENT once to cache the compartment OCID"
IMAGE = O.latest_ubuntu(COMPARTMENT, REGION)   # auto-resolved; no manual image OCID
print("using image:", IMAGE, "| ssh ingress:", SSH_CIDR)
cfg, net, cmp, ident = O.clients(REGION)
# --- Networking: create-or-reconcile EVERY run so it's always correct (idempotent) ---
# 1) VCN
vcns = net.list_vcns(COMPARTMENT, display_name="fcbench-vcn").data
vcn = vcns[0] if vcns else net.create_vcn(oci.core.models.CreateVcnDetails(
    cidr_block="10.10.0.0/16", compartment_id=COMPARTMENT, display_name="fcbench-vcn")).data
# 2) Internet gateway
igws = net.list_internet_gateways(COMPARTMENT, vcn_id=vcn.id).data
igw = igws[0] if igws else net.create_internet_gateway(oci.core.models.CreateInternetGatewayDetails(
    compartment_id=COMPARTMENT, vcn_id=vcn.id, is_enabled=True, display_name="fcbench-igw")).data
# 3) Route table -> IGW for 0.0.0.0/0 + AMD corp ranges (mirrors the known-good vcn-dlau)
rt = net.get_route_table(vcn.default_route_table_id).data
rules = list(rt.route_rules); have = {r.destination for r in rules}
for dest in ("0.0.0.0/0",):
    if dest not in have:
        rules.append(oci.core.models.RouteRule(destination=dest, destination_type="CIDR_BLOCK",
                                               network_entity_id=igw.id))
if len(rules) != len(rt.route_rules):
    net.update_route_table(rt.id, oci.core.models.UpdateRouteTableDetails(route_rules=rules))
# 4) Security list: SSH(22) ingress from EACH CIDR (comma-separated), all egress
cidrs = [c.strip() for c in SSH_CIDR.split(",") if c.strip()]
ingress = [oci.core.models.IngressSecurityRule(protocol="6", source=c,
    tcp_options=oci.core.models.TcpOptions(destination_port_range=oci.core.models.PortRange(min=22,max=22)))
    for c in cidrs]
sl = net.get_security_list(vcn.default_security_list_id).data
net.update_security_list(sl.id, oci.core.models.UpdateSecurityListDetails(
    ingress_security_rules=ingress,
    egress_security_rules=[oci.core.models.EgressSecurityRule(protocol="all", destination="0.0.0.0/0")]))
# 5) Subnet
subs = net.list_subnets(COMPARTMENT, vcn_id=vcn.id, display_name="fcbench-subnet").data
sub = subs[0] if subs else net.create_subnet(oci.core.models.CreateSubnetDetails(
    compartment_id=COMPARTMENT, vcn_id=vcn.id, cidr_block="10.10.1.0/24", display_name="fcbench-subnet")).data
# 2) launch (walk ADs; a LimitExceeded/capacity miss falls through to the next AD)
pub = open(str(O.STAGE/"oci_host_ssh.pub")).read().strip()
ADs = [a.name for a in ident.list_availability_domains(COMPARTMENT).data]
det = dict(compartment_id=COMPARTMENT, shape=SHAPE, display_name=NAME, freeform_tags={"fcbench":"1"},
    source_details=oci.core.models.InstanceSourceViaImageDetails(image_id=IMAGE),
    create_vnic_details=oci.core.models.CreateVnicDetails(subnet_id=sub.id, assign_public_ip=True),
    metadata={"ssh_authorized_keys": pub})
if OCPU:  # flex shape
    det["shape_config"] = oci.core.models.LaunchInstanceShapeConfigDetails(
        ocpus=OCPU, memory_in_gbs=MEM_GB or OCPU*8)
last=None
for ad in ADs:
    try:
        inst = cmp.launch_instance(oci.core.models.LaunchInstanceDetails(availability_domain=ad, **det)).data
        print("LAUNCHED", inst.id, "in", ad); break
    except oci.exceptions.ServiceError as e:
        last=e
        if e.code in ("LimitExceeded","QuotaExceeded","OutOfHostCapacity","InternalError"):
            print(f"  {ad}: {e.code} — trying next AD"); continue
        raise
else:
    print("FAILED:", getattr(last,'code',None), getattr(last,'message',''))
PYEOF
```

Fill in REGION/SHAPE/NAME (and OCPU/MEM_GB for a flex shape) before running. COMPARTMENT, SSH_CIDR,
and IMAGE come from cache/auto-resolve — on the **first run only**, set NEW_COMPARTMENT and
NEW_SSH_CIDR (the user's IP as `<ip>/32`) to cache them.
A `LimitExceeded` on every AD means the shape's service limit is 0 — tell the user to request an
increase (bare-metal limits are often 0 by default), and note that this launch attempt costs nothing.

Then wait for RUNNING and print the public IP + SSH command:

```bash
python3 - <<'PYEOF'
import sys, glob, time; sys.path.insert(0, glob.glob("/sessions/*/mnt/ClaudeTokens/.oci-stage")[0])
import _ocilib as O
OCID="ocid1.instance....REPLACE"; REGION="us-ashburn-1"; COMPARTMENT="ocid1.compartment....REPLACE"
cfg, net, cmp, ident = O.clients(REGION)
for _ in range(30):
    st = cmp.get_instance(OCID).data.lifecycle_state
    if st == "RUNNING": break
    if st in ("TERMINATED","TERMINATING","STOPPED"): print("state", st); break
    time.sleep(15)
ip=None
for va in cmp.list_vnic_attachments(COMPARTMENT, instance_id=OCID).data:
    v = net.get_vnic(va.vnic_id).data
    if v.public_ip: ip = v.public_ip; break
print("RUNNING ip=", ip)
print("SSH (from YOUR machine — sandbox SSH is proxy-blocked):")
print(f"  ssh -i <ClaudeTokens>/.oci-stage/oci_host_ssh ubuntu@{ip}")
PYEOF
```

Bare-metal provisioning can take 5–15 minutes; poll patiently. Hand the user the SSH command
(the private key is `ClaudeTokens/.oci-stage/oci_host_ssh`).

---

## Step 4 (create, optional) — Bare-metal storage prep (reflink)

OCI boot volumes are `ext4` (no `reflink`), which breaks reflink-based per-VM overlay workloads
(e.g. Firecracker microVM density). To put a working dir on a reflink-capable XFS on **local NVMe**
(E6 shapes) or an attached block volume (E5), the user runs this **on the host** (over their SSH,
since sandbox SSH is blocked):

```bash
# on the OCI host: pick a fast disk (local NVMe on E6, else an attached block volume) and make XFS+reflink
DISK=$(lsblk -dpno NAME,TYPE | awk '$2=="disk"{print $1}' | grep -v -E 'sda' | head -1)
sudo mkfs.xfs -f -m reflink=1 "$DISK" && sudo mkdir -p /mnt/work && sudo mount "$DISK" /mnt/work
echo "reflink XFS mounted at /mnt/work on $DISK"
```

E5.192 has no local NVMe — attach a block volume first (OCI Console or CLI) and use it as `$DISK`.

---

## Step 5 (manage) — list / start / stop / terminate

```bash
python3 - <<'PYEOF'
import sys, glob; sys.path.insert(0, glob.glob("/sessions/*/mnt/ClaudeTokens/.oci-stage")[0])
import _ocilib as O, oci
REGION="us-ashburn-1"; COMPARTMENT="ocid1.compartment....REPLACE"
ACTION="list"                       # list | start | stop | terminate
TARGET=None                         # instance OCID or display-name for start/stop/terminate
cfg, net, cmp, ident = O.clients(REGION)
insts = [i for i in oci.pagination.list_call_get_all_results(cmp.list_instances, COMPARTMENT).data
         if i.lifecycle_state not in ("TERMINATED","TERMINATING")]
if ACTION=="list":
    print(f"{'STATE':10} {'SHAPE':24} {'NAME':22} OCID")
    for i in insts:
        print(f"{i.lifecycle_state:10} {i.shape:24} {i.display_name:22} {i.id}")
else:
    sel=[i for i in insts if TARGET in (i.id, i.display_name)]
    for i in sel:
        if ACTION=="terminate": cmp.terminate_instance(i.id, preserve_boot_volume=False)
        elif ACTION=="stop":    cmp.instance_action(i.id, "SOFTSTOP")
        elif ACTION=="start":   cmp.instance_action(i.id, "START")
        print(ACTION, "->", i.display_name, i.id)
PYEOF
```

Render `list` output as a markdown table with 🟢 RUNNING / 🔴 STOPPED / 🟡 other. For
start/stop/terminate, confirm the target with the user first. **Always remind the user to terminate
bare-metal hosts when done — they bill hourly.**

---

## Key rules
- **Always run Step 0 first** — it installs the SDK, writes the config, and (critically) builds the
  combined **CA bundle** required behind a TLS-inspecting proxy. Every client sets
  `base_client.session.verify` to it; skipping this yields `SSLCertVerificationError`.
- **Sandbox SSH (port 22) is blocked** — never try to SSH to the instance from here. Create it and
  hand the user the SSH command to run from their own machine.
- **Launch = free limit check.** OCI `LaunchInstance` on a limit-0 shape errors without creating
  anything. If every AD returns `LimitExceeded`, tell the user to request a limit increase.
- **Bare-metal is billable hourly and slow to provision** (5–15 min). Confirm before launching;
  remind the user to `terminate` when done. Delete any attached scratch block volumes too.
- All instances are tagged `fcbench=1` so `list`/`terminate` can find them.
- Availability domain names are tenancy-specific (a prefix like `abCD:US-ASHBURN-AD-1`) — always
  enumerate them via `list_availability_domains`, never hardcode.
