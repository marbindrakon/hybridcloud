#!/usr/bin/env python3
"""Read one Vault KV v2 secret and write the asked fields to private files, so that no secret ever becomes a task
result, a fact or a module argument (the job's Vault token stays in its environment).

Request on stdin: {"path": "<path under the mount>", "mount": "central" (default), "files": {"<field>": "<file>"},
"missing_ok": false}. Each field present in the secret is written to its file (mode 0600, written in full or not at
all); a field the secret lacks leaves its file absent. Result on stdout (field names only, never a value):
{"status": 200, "keys": [...], "written": [...]} or {"status": 404, "keys": [], "written": []} (only with
missing_ok). Any other answer of Vault, or no answer, exits 1 with {"error": ...}: the caller stops with nothing
changed. Uses VAULT_ADDR (default the in-cluster service) and VAULT_TOKEN; VAULT_CACERT when set, else the
certificate is not verified (as the jobs' Vault tasks).
"""
import json
import os
import ssl
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_ADDR = "http://vault-active.central-vault.svc:8200"


def _write_private(path, text):
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".shc-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def read(req, env=os.environ):
    path = str(req.get("path") or "").strip().strip("/")
    if not path:
        raise ValueError("refused: path is empty")
    token = env.get("VAULT_TOKEN") or ""
    if not token:
        raise ValueError("VAULT_TOKEN is not set. Attach sovereign-vault-admin-credential to the job template.")
    addr = (env.get("VAULT_ADDR") or DEFAULT_ADDR).rstrip("/")
    url = f"{addr}/v1/{req.get('mount') or 'central'}/data/{urllib.parse.quote(path)}"
    ctx = None
    if url.startswith("https:"):
        ctx = ssl.create_default_context(cafile=env.get("VAULT_CACERT") or None)
        if not env.get("VAULT_CACERT"):
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
    r = urllib.request.Request(url, headers={"X-Vault-Token": token})
    try:
        with urllib.request.urlopen(r, timeout=30, context=ctx) as resp:
            body = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        if e.code == 404 and req.get("missing_ok"):
            return {"status": 404, "keys": [], "written": []}
        raise RuntimeError(f"Vault answered HTTP {e.code} for {path}") from None
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(f"Vault did not answer for {path} ({type(e).__name__})") from None
    data = ((body.get("data") or {}).get("data")) or {}
    written = []
    for field, dest in (req.get("files") or {}).items():
        if dest and field in data and data[field] not in (None, ""):
            value = data[field]
            _write_private(dest, value if isinstance(value, str) else json.dumps(value))
            written.append(field)
    return {"status": 200, "keys": sorted(data), "written": sorted(written)}


def main(stdin=sys.stdin, stdout=sys.stdout):
    try:
        print(json.dumps(read(json.load(stdin))), file=stdout)
        return 0
    except Exception as e:  # noqa: BLE001 - one line for the job log, never a value of the secret
        print(json.dumps({"error": str(e)}), file=stdout)
        return 1


if __name__ == "__main__":
    sys.exit(main())
