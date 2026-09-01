# Maintainer notes — oci-run-workload

## Architecture

`SKILL.md` (small, agent workflow) + `_ociwl.py` (the pipeline, ~230 lines). Step 0 keeps
`_ociwl.py` in the persistent `ClaudeTokens/.oci-stage/` folder; if missing it restores it by
`find`-ing the copy that ships in this skill's source folder (`*oci-run-workload*`). The helper is
NOT base64-embedded in SKILL.md on purpose — a 20 KB blob can't be reproduced reliably through
`save_skill` (a smart-quote crept in and broke it during development).

Transport = **HTTP-pull**. Each instance's cloud-init runs the workload, copies results to `/srv/wl`,
writes a `DONE` marker, opens local iptables :80, and serves `/srv/wl` via `python3 -m http.server 80`.
The sandbox pulls `http://<ip>/DONE` then each result file. `ensure_network` opens :80 from the
sandbox's detected web-egress /24 (plus SSH :22 from the AMD corporate ranges).

## Hard-won gotchas (all fixed in `_ociwl.py`; keep them)

- **Object Storage is unusable** for this user — object PUT/GET 404 even via PARs. Do NOT reintroduce
  a bucket/PAR transport. HTTP-pull is the verified path.
- **Config `key_file` has a session-id in its absolute path** → breaks when the session id changes.
  `_cfg()` self-heals it every load.
- **cloud-init apt race**: the OS's unattended-upgrades holds the apt *lists* lock at boot, and
  `DPkg::Lock::Timeout` does NOT cover it. The template **retries `apt-get update` until it wins**,
  and presets must NOT run `apt-get update` themselves (only `apt-get install`).
- **user_data cap is 32 KB (base64)** — `build_cloud_init` raises if exceeded; large local sources
  must use a git repo instead of an embedded tarball.
- **Flex vs bare-metal**: `shape_config` (ocpus/mem, from `cfg_get flex_ocpu/flex_mem`) is set only
  for `*.Flex` shapes.

## How to test (cheap)

The whole pipeline was validated with one `VM.Standard.E5.Flex` (1 OCPU, ~$0.037/hr) running the
`sysbench-cpu` preset: launch → harvest_http → verify "events per second" in the result → teardown.
Repeat that for a smoke test. Non-billable checks: `build_cloud_init` for every preset (serves :80,
DONE marker, <32 KB), `ensure_network` (opens :80), `est_cost_hr`, `reap_list`.

## Extending

- New preset: add a spec to `PRESETS` (`install` must NOT run `apt-get update`; write results to a
  file listed in `results`).
- AMD telemetry: a preset could install AMD µProf / `perf` and add its output to `results`.
- Cross-workload report: presets set `report`; wire richer local parsing per preset in Step 5.
