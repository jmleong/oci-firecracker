---
name: oci-run-workload
description: "Run a workload or benchmark headlessly across a fleet of OCI instances and collect the results without SSH — pick a built-in benchmark (firecracker microVM suite, sysbench-cpu, sysbench-oltp, 7-zip, STREAM) or bring your own local dir or git repo, and the skill launches the fleet, self-provisions each host via cloud-init, HTTP-pulls results, optionally reports, and tears the fleet down. Use when you want to benchmark or compare OCI shapes, run a headless workload across OCI hosts, or clean up orphaned OCI benchmark instances — or say \"benchmark these OCI shapes\", \"run a workload on OCI\", or \"/oci-run-workload\"."
license: "Copyright © Advanced Micro Devices, Inc., or its affiliates. All rights reserved. Portions of this content consists of AI generated content."
metadata:
  author: jorleong
  version: "1.0.0"
  category: automation
  tags:
    - oci
    - benchmark
    - workload
    - fleet
    - amd-epyc
    - cloud-init
    - performance
compatibility:
  universal: true
---

# OCI Headless Workload / Benchmark Runner

Run a workload across one or more OCI instances and get the results back — **without SSH**.
Two environment constraints shaped this skill: the sandbox's outbound **SSH is proxy-blocked**,
and this OCI user has **no Object Storage object access**. So results move over **HTTP**: each host
self-provisions via cloud-init, runs the workload, and serves its results on **port 80**; the
sandbox (whose web egress goes out via Zscaler) **HTTP-pulls** them. Every instance is tagged
`fcbench=1` for cleanup. Verified working end-to-end.

Use a task list to track: bootstrap → pick workload → pick fleet + confirm cost → launch →
poll & harvest → (optional) report → **tear down**.

---

## Step 0 — Bootstrap (auth, CA bundle, helper) — always first

```bash
python3 - <<'PYEOF'
import json, glob, os, subprocess, sys, shutil
from pathlib import Path
TOK=Path(glob.glob('/sessions/*/mnt/ClaudeTokens/tokens.json')[0]); STAGE=TOK.parent/'.oci-stage'; STAGE.mkdir(exist_ok=True)
t=json.loads(TOK.read_text())
if 'oci' not in t: print("ERROR: no 'oci' key in tokens.json — set up OCI API-key auth first "
    "(generate an API signing key, upload the public half in the OCI Console, store user/tenancy "
    "OCID + fingerprint + region under the 'oci' key, and place the private key at .oci-stage/oci_api_key.pem)."); sys.exit(1)
o=t['oci']; key=STAGE/'oci_api_key.pem'; cfg=STAGE/'config'
cfg.write_text("[DEFAULT]\n"+"".join(f"{k}={o[k]}\n" for k in ('user','fingerprint','tenancy','region') if k in o)+f"key_file={key}\n"); os.chmod(cfg,0o600)
if not key.exists(): print(f"WARN: private key {key} missing")
try: import oci
except ImportError: subprocess.run([sys.executable,'-m','pip','install','oci','--break-system-packages','-q'],check=True)
import certifi; b=STAGE/'ca-bundle.pem'; shutil.copy(certifi.where(),b)
with open(b,'ab') as f:
    for p in sorted(glob.glob('/usr/local/share/ca-certificates/*.crt')): f.write(Path(p).read_bytes())
hk=STAGE/'oci_host_ssh'
if not hk.exists(): subprocess.run(['ssh-keygen','-q','-t','ed25519','-f',str(hk),'-N',''],check=True)
print("bootstrap OK")
PYEOF
# ensure the pipeline helper (_ociwl.py) is present in the persistent stage dir.
# It ships in this skill's source folder and in the firecracker-on-oci repo; if it's
# ever missing from .oci-stage, restore it by searching the mounted folders.
STAGE=$(ls -d /sessions/*/mnt/ClaudeTokens/.oci-stage)
if [ ! -f "$STAGE/_ociwl.py" ]; then
  SRC=$(find /sessions "$HOME" -name _ociwl.py -path '*oci-run-workload*' 2>/dev/null | head -1)
  [ -n "$SRC" ] && cp "$SRC" "$STAGE/_ociwl.py" && echo "restored _ociwl.py from $SRC"
fi
if [ ! -f "$STAGE/_ociwl.py" ]; then
  echo "ERROR: _ociwl.py not found. Copy it from the oci-run-workload skill source folder (skills/oci-run-workload/_ociwl.py) into $STAGE and re-run."
else
  python3 -c "import sys;sys.path.insert(0,'$STAGE');import _ociwl as W;print('helper OK — presets:',list(W.PRESETS))"
fi
```

If the block prints an `ERROR` about a missing `oci` key, stop and help the user set up API-key auth.
Also confirm the **compartment OCID** is cached: `python3 -c "import sys,glob;sys.path.insert(0,glob.glob('/sessions/*/mnt/ClaudeTokens/.oci-stage')[0]);import _ociwl as W;print(W.cfg_get('compartment'))"` — if `None`, ask the user and `W.cfg_set(compartment=...)`.

---

## Step 1 — Choose the workload

Offer three ways (AskUserQuestion), and build a **spec**:

- **Preset benchmark** — one of `W.PRESETS`: `firecracker` (needs bare-metal), `sysbench-cpu`,
  `sysbench-oltp`, `7zip`, `stream`. Use `spec = W.PRESETS[name]`.
- **Local directory** — the user gives a path with a runnable harness. Build a spec:
  `{"source": None, "install": "<deps>", "run": "<cmd, writes to /tmp/wl/result.txt>", "results": ["/tmp/wl/result.txt"], "needs_reflink": <bool>, "report": None}`
  and embed the dir with `src_b64 = W.tar_b64("<path>")` (must keep the cloud-init under 32 KB —
  small harnesses only; for large trees, host them in a git repo instead).
- **Git repo** — build a spec with `{"source": {"git": "<url>", "subdir": "<opt>"}, "install":..., "run":..., "results":[...], ...}`.

Ask the user for the run **duration** (default 180 s) for the timed presets.

---

## Step 2 — Choose the fleet + confirm cost

Ask for the **shapes** (default the AMD set: `BM.Standard.E5.192`, `BM.Standard.E6.Ax.192`,
`BM.Standard.E6.256`; or flex VMs for cheap tests) and **region** (default from config). For flex
shapes, cache size with `W.cfg_set(flex_ocpu=..., flex_mem=...)`.

Print a per-shape cost estimate and the fleet total, then get explicit confirmation:
```bash
python3 - <<'PYEOF'
import sys, glob; sys.path.insert(0, glob.glob("/sessions/*/mnt/ClaudeTokens/.oci-stage")[0])
import _ociwl as W
R="us-ashburn-1"; COMP=W.cfg_get("compartment")
SHAPES=["BM.Standard.E6.Ax.192","BM.Standard.E5.192"]
tot=0
for s in SHAPES:
    hr=W.est_cost_hr(s,R,COMP); tot+=hr or 0
    print(f"  {s:26} ${hr:,.2f}/hr" if hr else f"  {s}: n/a")
print(f"  FLEET TOTAL: ${tot:,.2f}/hr")
PYEOF
```

---

## Step 3 — Launch the fleet (headless)

```bash
python3 - <<'PYEOF'
import sys, glob; sys.path.insert(0, glob.glob("/sessions/*/mnt/ClaudeTokens/.oci-stage")[0])
import _ociwl as W
R="us-ashburn-1"; COMP=W.cfg_get("compartment")
SHAPES=["BM.Standard.E6.Ax.192","BM.Standard.E5.192"]
PRESET="firecracker"; DUR=180
spec=W.PRESETS[PRESET]                      # or a custom spec dict you built in Step 1
src_b64=None                                # for a local dir: src_b64=W.tar_b64("<path>")
ud=W.build_cloud_init(spec, duration=DUR, source_tgz_b64=src_b64)
img=W.latest_ubuntu(COMP,R); pub=open(str(W.STAGE/"oci_host_ssh.pub")).read().strip()
fleet=W.launch_fleet(SHAPES, R, COMP, img, pub, ud)   # also opens :80 from the sandbox egress
print("fleet:", fleet)
PYEOF
```
`ensure_network` (called by `launch_fleet`) opens SSH:22 (AMD ranges) **and** HTTP:80 (sandbox
egress). A `FAILED:LimitExceeded` on a shape means its bare-metal limit is 0 — request an increase.

---

## Step 4 — Poll & harvest (repeat until all DONE)

Each host boots, waits out the apt lock, installs, runs the workload (minutes), then serves results
on :80. Poll in **short calls** (the shell caps ~178 s). For each running instance, get its IP and
`harvest_http` (which polls `http://ip/DONE`, then pulls the results + cloud-init log):
```bash
python3 - <<'PYEOF'
import sys, glob; sys.path.insert(0, glob.glob("/sessions/*/mnt/ClaudeTokens/.oci-stage")[0])
import _ociwl as W
R="us-ashburn-1"; COMP=W.cfg_get("compartment")
OUT=glob.glob("/sessions/*/mnt/outputs")[0]+"/oci-wl"   # collected results land here
spec=W.PRESETS["firecracker"]
for st,shape,name,ocid in W.reap_list(R,COMP):
    ip=W.public_ip(R,COMP,ocid)
    if not ip: print(f"{name}: {st}, no ip yet"); continue
    got=W.harvest_http(ip, spec, name, OUT, done_timeout=90)   # short per-call timeout; repeat if not done
    print(f"{name} @ {ip}: {'DONE, harvested' if got else 'still running'}")
PYEOF
```
Repeat this block until every host reports harvested. Results are saved as `<name>-<file>` under `OUT`.

---

## Step 5 — (optional) Report

If the workload spec has a `report` command, run it **locally** on the harvested results. For the
firecracker preset, arrange the collected `*-result.json` into the `gen_report.py` layout and run it
to produce the E5-vs-E6 report + chart. For custom workloads, parse the `*-result.txt` yourself
(e.g. sysbench "events per second", STREAM "Best Rate MB/s") into a comparison table.

---

## Step 6 — Tear down (always) + reaper

**Terminate the fleet when results are in — bare-metal bills hourly.**
```bash
python3 - <<'PYEOF'
import sys, glob; sys.path.insert(0, glob.glob("/sessions/*/mnt/ClaudeTokens/.oci-stage")[0])
import _ociwl as W
R="us-ashburn-1"; COMP=W.cfg_get("compartment")
print("terminating:", W.teardown(R, COMP))     # all tagged fcbench=1 + scratch volumes
print("remaining:", W.reap_list(R, COMP))
PYEOF
```
**Reaper** (for a lost session / orphaned fleet): `W.reap_list(R, COMP)` to list, then
`W.teardown(R, COMP, [ocid, ...])` for specific ones. Confirm with the user before terminating.

---

## Key rules
- **Run Step 0 first** — installs the SDK, builds the CA bundle (needed behind the TLS proxy), and
  materializes the `_ociwl.py` helper.
- **Transport is HTTP-pull, not SSH or Object Storage** — both are blocked here. Instances serve
  results on :80; the sandbox pulls them. `ensure_network` opens :80 from the sandbox's web egress.
- **Confirm the fleet cost before launching** and **always tear down** — headless fleets are easy to
  forget. Everything is tagged `fcbench=1`; the reaper finds orphans.
- **Bare-metal presets (firecracker) need `/dev/kvm`** — only bare-metal shapes; and a
  `LimitExceeded` means request a limit increase (a failed launch costs nothing).
- **Poll harvest in short calls** — the workload runs for minutes; don't block a single shell call on it.
