# Maintainer notes — oci-instance

## Architecture

Single `SKILL.md` with embedded Python blocks (matches the local Cowork convention used by
`gcp-instance`, not the AMD pandora/uv template). Step 0 materializes a helper library
`_ocilib.py` into the persistent `ClaudeTokens/.oci-stage/` folder; later steps
`sys.path.insert` that dir and `import _ocilib`. Credentials, CA bundle, host SSH key, and the
helper all live in `.oci-stage/`, which survives restarts — so the skill installs cleanly as
SKILL.md-only via `save_skill` with nothing to lose on a restart wipe.

## How to test (no billing)

Read-only checks exercise the whole backbone without launching anything:

1. Run the Step 0 bootstrap block → expect `OK bootstrap complete`.
2. Run the Step 5 `list` block (ACTION="list") → expect a table (or "0 active") and no SSL error.
3. Confirm ADs enumerate via `list_availability_domains`.

If those pass, `create`/`terminate` use the same SDK calls and are considered good. Only do a real
`create` when you actually need an instance — bare-metal bills hourly.

## Common issues & fixes

- **`SSLCertVerificationError`** → the CA bundle wasn't applied. Every client must set
  `base_client.session.verify = <.oci-stage/ca-bundle.pem>`. Step 0 rebuilds it from
  certifi + `/usr/local/share/ca-certificates/*.crt` (the corporate/proxy roots).
- **`NotAuthenticated` / 401** → wrong fingerprint or missing/incorrect private key at
  `.oci-stage/oci_api_key.pem`. Re-check `tokens.json['oci']`.
- **`LimitExceeded` on every AD** → the shape's service limit is 0; request an increase. The failed
  launch creates nothing and costs nothing (this doubles as a free limit probe).
- **SSH to the instance hangs from the sandbox** → expected; port 22 egress is proxy-blocked. Give
  the user the SSH command to run from their own machine.
- **AD name mismatch** → AD prefixes are tenancy-specific (e.g. `abCD:US-ASHBURN-AD-1`); always
  enumerate them, never hardcode.
- **Flex vs bare-metal** → set `shape_config` (ocpus/memory) only for `*.Flex` shapes; bare-metal
  shapes reject it.

## Extending

- Add block-volume attach/detach helpers to `_ocilib.py` for E5 storage prep automation.
- Add an Object Storage upload/harvest path for headless (cloud-init) provisioning when SSH is
  blocked end-to-end (pattern proven in the firecracker-on-oci repo's `drive_oci_headless.py`).
