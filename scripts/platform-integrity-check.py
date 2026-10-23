#!/usr/bin/env python3
"""Full hybridsovereign platform integrity check + CR retrigger.

Uses existing kubeconfig. Optionally logs into services cluster for CR annotations.
Does not print secrets.
"""
from __future__ import annotations

import base64
import json
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/home/gshankar/storage/sovereign/hybridcloud")
ENV_FILE = ROOT / ".env"
RECONCILE_ANN = "ansible.sdk.operatorframework.io/reconcileNow"
FORCE_SYNC_ANN = "force-sync"

# Core helper.hybridsovereign CRDs (operators + plugins)
CR_KINDS = [
    "entities.hybridsovereign.redhat",
    "personas.hybridsovereign.redhat",
    "teams.hybridsovereign.redhat",
    "assignments.hybridsovereign.redhat",
    "projects.hybridsovereign.redhat",
    "platformopenshifts.hybridsovereign.redhat",
    "cloudosos.hybridsovereign.redhat",
    "cloudaws.hybridsovereign.redhat",
    "openstackmigrations.hybridsovereign.redhat",
    "rbacs.hybridsovereign.redhat",
    "rbacconfigs.hybridsovereign.redhat",
    "vaults.hybridsovereign.redhat",
    "vaultkvs.hybridsovereign.redhat",
    "aapconfigs.hybridsovereign.redhat",
    "aaporgs.hybridsovereign.redhat",
    "quayconfigs.hybridsovereign.redhat",
    "quayorgs.hybridsovereign.redhat",
    "iaacs.hybridsovereign.redhat",
    "hybridfabrics.hybridsovereign.redhat",
    "cloudgateways.hybridsovereign.redhat",
    "transportlinks.hybridsovereign.redhat",
    "hybridnetworks.hybridsovereign.redhat",
    "networkplacements.hybridsovereign.redhat",
    "uihealthcheckers.hybridsovereign.redhat",
    # shc (P7.1, tenant DNS D1, P7.3.3)
    "cloudinfrastructures.hybridsovereign.redhat",
    "dnsconfigs.hybridsovereign.redhat",
    "cloudvirts.hybridsovereign.redhat",
]


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def oc(args: list[str], timeout: int = 90, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["oc", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        input=input_text,
    )


def oc_retry(args: list[str], attempts: int = 5, timeout: int = 90) -> subprocess.CompletedProcess[str]:
    last = None
    for i in range(attempts):
        try:
            last = oc(args, timeout=timeout)
            if last.returncode == 0:
                return last
        except subprocess.TimeoutExpired as e:
            last = subprocess.CompletedProcess(args, 1, "", f"timeout: {e}")
        time.sleep(2 + i)
    assert last is not None
    return last


def section(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def ensure_central_login(env: dict[str, str]) -> None:
    r = oc_retry(["whoami", "--request-timeout=20s"], attempts=3, timeout=40)
    if r.returncode == 0:
        print(f"central auth: {r.stdout.strip()}", flush=True)
        return
    print("relogin central...", flush=True)
    r = oc(
        [
            "login",
            env["OCP_CENTRAL_SERVER"],
            "--username",
            env["OCP_CENTRAL_USERNAME"],
            "--password",
            env["OCP_CENTRAL_PASSWORD"],
            "--insecure-skip-tls-verify=true",
        ],
        timeout=90,
    )
    if r.returncode != 0:
        raise SystemExit(f"central login failed: {(r.stderr or r.stdout)[-200:]}")
    print("central login ok", flush=True)


def ensure_services_login(env: dict[str, str]) -> str | None:
    """Login to services and return context name, or None if unavailable."""
    server = env.get("OCP_SERVICES_SERVER")
    user = env.get("OCP_SERVICES_USERNAME")
    password = env.get("OCP_SERVICES_PASSWORD")
    if not (server and user and password):
        print("services credentials missing in .env", flush=True)
        return None
    # Save central context
    cur = oc(["config", "current-context"], timeout=20).stdout.strip()
    r = oc(
        [
            "login",
            server,
            "--username",
            user,
            "--password",
            password,
            "--insecure-skip-tls-verify=true",
        ],
        timeout=90,
    )
    if r.returncode != 0:
        print(f"services login failed: {(r.stderr or r.stdout)[-200:]}", flush=True)
        if cur:
            oc(["config", "use-context", cur], timeout=20)
        return None
    svc_ctx = oc(["config", "current-context"], timeout=20).stdout.strip()
    print(f"services auth ok context={svc_ctx}", flush=True)
    # switch back to central for Argo work
    if cur:
        oc(["config", "use-context", cur], timeout=20)
    return svc_ctx


def check_nodes() -> None:
    section("Nodes")
    r = oc_retry(["get", "nodes", "--no-headers", "--request-timeout=40s"], timeout=60)
    if r.returncode != 0:
        print("nodes FAIL", (r.stderr or "")[-200:], flush=True)
        return
    c = Counter()
    bad = []
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        c[parts[1]] += 1
        if parts[1] != "Ready":
            bad.append(line[:120])
    print(f"summary {dict(c)}", flush=True)
    for b in bad:
        print(f"  NOT READY: {b}", flush=True)


def check_argocd() -> dict:
    section("ArgoCD")
    r = oc_retry(
        [
            "get",
            "pods",
            "-n",
            "openshift-gitops",
            "--request-timeout=40s",
            "-o",
            "json",
        ],
        timeout=70,
    )
    out = {"apps": Counter(), "controller": "?"}
    if r.returncode == 0:
        for p in json.loads(r.stdout).get("items", []):
            name = p["metadata"]["name"]
            phase = p.get("status", {}).get("phase")
            ready = "?"
            for cs in p.get("status", {}).get("containerStatuses") or []:
                ready = str(cs.get("ready"))
                if "application-controller" in name:
                    out["controller"] = f"{phase} ready={ready} restarts={cs.get('restartCount')}"
            if phase != "Running" or ready == "False":
                print(f"  pod issue: {name} {phase} ready={ready}", flush=True)
        print(f"controller: {out['controller']}", flush=True)
    else:
        print("pods FAIL", (r.stderr or "")[-150:], flush=True)

    r = oc_retry(
        [
            "get",
            "applications.argoproj.io",
            "-n",
            "openshift-gitops",
            "-o",
            "json",
            "--request-timeout=60s",
        ],
        timeout=100,
    )
    if r.returncode != 0:
        print("apps FAIL", (r.stderr or "")[-150:], flush=True)
        return out
    items = json.loads(r.stdout).get("items", [])
    degraded = []
    for a in items:
        name = a["metadata"]["name"]
        sync = (a.get("status") or {}).get("sync", {}).get("status", "?")
        health = (a.get("status") or {}).get("health", {}).get("status", "?")
        out["apps"][f"{sync}/{health}"] += 1
        if health in ("Degraded", "Missing", "Suspended") or sync == "OutOfSync":
            degraded.append((name, sync, health))
    print(f"apps total={len(items)}", flush=True)
    for k, v in out["apps"].most_common():
        print(f"  {v:3d} {k}", flush=True)
    if degraded:
        print(f"attention ({len(degraded)}):", flush=True)
        for name, sync, health in sorted(degraded)[:40]:
            print(f"  {name}: {sync}/{health}", flush=True)
    out["app_names"] = [a["metadata"]["name"] for a in items]
    out["items"] = items
    return out


def hard_refresh_apps(names: list[str]) -> None:
    section("ArgoCD hard refresh (critical + all)")
    critical = [
        "sovereign-central-apps",
        "dns-forwarder-services",
        "external-secrets-services",
        "vault-secret-store",
        "gitea-central",
        "aap-central",
        "entity-operator-services",
        "team-operator-services",
        "assignment-operator-services",
        "project-operator-services",
        "persona-operator-services",
        "platformopenshift-operator-services",
    ]
    # refresh critical first, then batch the rest
    targets = []
    for n in critical + names:
        if n not in targets:
            targets.append(n)
    ok = fail = 0
    for name in targets:
        r = oc(
            [
                "annotate",
                "applications.argoproj.io",
                name,
                "-n",
                "openshift-gitops",
                "argocd.argoproj.io/refresh=hard",
                "--overwrite",
                "--request-timeout=25s",
            ],
            timeout=45,
        )
        if r.returncode == 0:
            ok += 1
        else:
            fail += 1
            if fail <= 10:
                print(f"  refresh fail {name}: {(r.stderr or '')[-80:].strip()}", flush=True)
        time.sleep(0.15)
    print(f"refreshed ok={ok} fail={fail}", flush=True)


def unseal_and_verify_vault(env: dict[str, str]) -> bool:
    section("Vault")
    r = oc_retry(
        [
            "get",
            "secret",
            "vault-init-secrets",
            "-n",
            "central-vault",
            "-o",
            "json",
            "--request-timeout=40s",
        ],
        timeout=70,
    )
    if r.returncode != 0:
        print("vault-init-secrets missing", flush=True)
        return False
    data = json.loads(r.stdout)["data"]
    keys = json.loads(base64.b64decode(data["unseal_keys"]).decode())
    token = base64.b64decode(data["root_token"]).decode()

    # status via route
    ctx = ssl._create_unverified_context()
    vault = env.get("VAULT_CENTRAL_URL", "").rstrip("/")

    def http(method, path, body=None, auth=False, timeout=20):
        raw = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(vault + path, data=raw, method=method)
        req.add_header("Content-Type", "application/json")
        if auth:
            req.add_header("X-Vault-Token", token)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.status, json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode())
            except Exception:
                return e.code, {}
        except Exception as e:
            return -1, {"error": str(e)}

    code, st = http("GET", "/v1/sys/seal-status")
    print(f"route seal-status http={code} sealed={st.get('sealed')}", flush=True)
    if st.get("sealed"):
        for i, key in enumerate(keys[:3]):
            code, st = http("PUT", "/v1/sys/unseal", {"key": key})
            print(f"  unseal step {i+1} sealed={st.get('sealed')}", flush=True)
            if not st.get("sealed"):
                break

    # also unseal each pod via PF if needed
    for pod, port in (("vault-0", 18400), ("vault-1", 18401), ("vault-2", 18402)):
        pf = subprocess.Popen(
            ["oc", "port-forward", "-n", "central-vault", f"pod/{pod}", f"{port}:8200"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            up = False
            for _ in range(25):
                if pf.poll() is not None:
                    break
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/sys/seal-status", timeout=2)
                    up = True
                    break
                except urllib.error.HTTPError:
                    up = True
                    break
                except Exception:
                    time.sleep(0.4)
            if not up:
                print(f"  {pod}: pf not ready (skip)", flush=True)
                continue
            req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/sys/seal-status")
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    pst = json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                pst = json.loads(e.read().decode())
            print(f"  {pod}: sealed={pst.get('sealed')}", flush=True)
            if pst.get("sealed"):
                for key in keys[:3]:
                    body = json.dumps({"key": key}).encode()
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{port}/v1/sys/unseal", data=body, method="PUT"
                    )
                    req.add_header("Content-Type", "application/json")
                    try:
                        with urllib.request.urlopen(req, timeout=15) as resp:
                            pst = json.loads(resp.read().decode())
                    except urllib.error.HTTPError as e:
                        pst = json.loads(e.read().decode())
                    if not pst.get("sealed"):
                        break
                print(f"  {pod}: after unseal sealed={pst.get('sealed')}", flush=True)
        finally:
            pf.terminate()
            try:
                pf.wait(5)
            except Exception:
                pf.kill()

    code, got = http("GET", "/v1/central/data/lab-config", auth=True)
    if code == 200:
        d = got.get("data", {}).get("data", {})
        print(f"lab-config OK keys={len(d)}", flush=True)
        return True
    print(f"lab-config read FAIL http={code} {got}", flush=True)
    return False


def check_lab_config_cm() -> None:
    section("lab-config ConfigMap/Secret/ES")
    for kind in ("configmap", "secret", "externalsecret"):
        r = oc_retry(
            ["get", kind, "lab-config", "-n", "openshift-gitops", "-o", "json", "--request-timeout=30s"],
            attempts=3,
            timeout=50,
        )
        if r.returncode != 0:
            print(f"  {kind}/lab-config: MISSING", flush=True)
            continue
        d = json.loads(r.stdout)
        if kind == "configmap":
            print(f"  cm keys={len(d.get('data') or {})}", flush=True)
        elif kind == "secret":
            print(f"  secret keys={len(d.get('data') or {})}", flush=True)
        else:
            cond = ((d.get("status") or {}).get("conditions") or [{}])[0]
            print(
                f"  ES ready={cond.get('status')} reason={cond.get('reason')} msg={cond.get('message')}",
                flush=True,
            )


def list_crs(context: str | None = None) -> list[tuple[str, str, str, dict]]:
    """Return list of (kind, namespace, name, status_summary)."""
    found = []
    ctx_args = ["--context", context] if context else []
    for kind in CR_KINDS:
        r = oc(
            [
                *ctx_args,
                "get",
                kind,
                "-A",
                "-o",
                "json",
                "--request-timeout=45s",
            ],
            timeout=70,
        )
        if r.returncode != 0:
            # CRD may not exist on this cluster
            continue
        for item in json.loads(r.stdout).get("items", []):
            md = item["metadata"]
            st = item.get("status") or {}
            found.append(
                (
                    kind,  # full CRD name for patch
                    md.get("namespace", ""),
                    md["name"],
                    {
                        "ready": st.get("ready"),
                        "status": st.get("status"),
                        "generation": md.get("generation"),
                        "observedGeneration": st.get("observedGeneration"),
                    },
                )
            )
    return found


def retrigger_crs(crs: list[tuple[str, str, str, dict]], context: str | None = None) -> tuple[int, int]:
    section(f"Retrigger CRs ({'services' if context else 'central'}) count={len(crs)}")
    ts = str(int(time.time()))
    ok = fail = 0
    ctx_args = ["--context", context] if context else []
    for kind, ns, name, st in crs:
        # Prefer reconcileNow (operator SDK watch); also set force-sync for plugins that use it
        patch = {
            "metadata": {
                "annotations": {
                    # The legacy literal: operators of every generation act on it each time and remove
                    # it after publishing (a timestamp is only understood by the current ones)
                    RECONCILE_ANN: "true",
                    FORCE_SYNC_ANN: ts,
                }
            }
        }
        args = [
            *ctx_args,
            "patch",
            kind,
            name,
            "--type=merge",
            "-p",
            json.dumps(patch),
            "--request-timeout=30s",
        ]
        if ns:
            args = [
                *ctx_args,
                "patch",
                kind,
                name,
                "-n",
                ns,
                "--type=merge",
                "-p",
                json.dumps(patch),
                "--request-timeout=30s",
            ]
        r = oc(args, timeout=50)
        if r.returncode == 0:
            ok += 1
            print(f"  ok {kind}/{ns}/{name} was status={st.get('status')} ready={st.get('ready')}", flush=True)
        else:
            fail += 1
            print(f"  FAIL {kind}/{ns}/{name}: {(r.stderr or '')[-100:].strip()}", flush=True)
        time.sleep(0.1)
    print(f"retrigger ok={ok} fail={fail}", flush=True)
    return ok, fail


def check_operator_pods(context: str | None, label: str) -> None:
    section(f"Operator pods ({label})")
    ctx_args = ["--context", context] if context else []
    for ns in ("sovereign-cloud", "sovereign-cloud-plugins", "sovereign-cloud-jobs"):
        r = oc(
            [*ctx_args, "get", "pods", "-n", ns, "--no-headers", "--request-timeout=40s"],
            timeout=60,
        )
        if r.returncode != 0:
            print(f"  {ns}: unreachable/missing", flush=True)
            continue
        lines = [l for l in r.stdout.splitlines() if l.strip()]
        bad = [l for l in lines if not any(x in l for x in ("Running", "Completed"))]
        print(f"  {ns}: pods={len(lines)} not-running={len(bad)}", flush=True)
        for l in bad[:15]:
            print(f"    {l[:140]}", flush=True)


def check_jobs(context: str | None, label: str) -> None:
    section(f"Recent Jobs ({label})")
    ctx_args = ["--context", context] if context else []
    r = oc(
        [
            *ctx_args,
            "get",
            "jobs",
            "-n",
            "sovereign-cloud-jobs",
            "--sort-by=.metadata.creationTimestamp",
            "--request-timeout=40s",
        ],
        timeout=60,
    )
    if r.returncode != 0:
        print("  jobs ns missing/unreachable", flush=True)
        return
    lines = r.stdout.strip().splitlines()
    print("\n".join(lines[-25:] if len(lines) > 25 else lines), flush=True)


def check_eso() -> None:
    section("External Secrets")
    r = oc_retry(
        ["get", "clustersecretstore", "vault-backend", "-o", "json", "--request-timeout=30s"],
        timeout=50,
    )
    if r.returncode == 0:
        cond = ((json.loads(r.stdout).get("status") or {}).get("conditions") or [{}])[0]
        print(f"vault-backend Ready={cond.get('status')} msg={cond.get('message')}", flush=True)
    r = oc_retry(
        ["get", "pods", "-n", "external-secrets", "--no-headers", "--request-timeout=30s"],
        timeout=50,
    )
    print(f"external-secrets ns pods:\n{(r.stdout or r.stderr or 'none')[:500]}", flush=True)
    r = oc_retry(
        [
            "get",
            "pods",
            "-n",
            "external-secrets-operator",
            "--no-headers",
            "--request-timeout=30s",
        ],
        timeout=50,
    )
    print(f"operator pods:\n{(r.stdout or '')[:500]}", flush=True)


def wait_and_resummarize_crs(context: str | None, label: str, wait_s: int = 90) -> None:
    section(f"Post-retrigger CR status ({label}) after {wait_s}s")
    time.sleep(wait_s)
    crs = list_crs(context)
    by_status = Counter()
    not_ready = []
    for kind, ns, name, st in crs:
        key = f"status={st.get('status')} ready={st.get('ready')}"
        by_status[key] += 1
        if st.get("ready") not in (True, "True", "true") and st.get("status") not in ("ready", "Ready"):
            not_ready.append((kind, ns, name, st))
    print(f"total CRs={len(crs)}", flush=True)
    for k, v in by_status.most_common():
        print(f"  {v:3d} {k}", flush=True)
    if not_ready:
        print(f"not ready ({len(not_ready)}):", flush=True)
        for kind, ns, name, st in not_ready[:50]:
            print(f"  {kind}/{ns}/{name}: {st}", flush=True)


def main() -> int:
    print(f"integrity check start {datetime.now(timezone.utc).isoformat()}", flush=True)
    env = load_env()
    ensure_central_login(env)
    check_nodes()
    argo = check_argocd()
    unseal_and_verify_vault(env)
    check_lab_config_cm()
    check_eso()

    names = argo.get("app_names") or []
    hard_refresh_apps(names)

    # Central CRs (usually few)
    central_crs = list_crs(None)
    section(f"Central hybridsovereign CRs ({len(central_crs)})")
    for kind, ns, name, st in central_crs:
        print(f"  {kind}/{ns}/{name}: {st}", flush=True)
    if central_crs:
        retrigger_crs(central_crs, None)

    svc_ctx = ensure_services_login(env)
    if svc_ctx:
        # switch for listing
        oc(["config", "use-context", svc_ctx], timeout=20)
        check_operator_pods(None, "services")
        check_jobs(None, "services")
        svc_crs = list_crs(None)
        section(f"Services hybridsovereign CRs ({len(svc_crs)})")
        for kind, ns, name, st in svc_crs:
            print(f"  {kind}/{ns}/{name}: {st}", flush=True)
        if svc_crs:
            retrigger_crs(svc_crs, None)
        wait_and_resummarize_crs(None, "services", wait_s=120)
        # back to central
        ensure_central_login(env)
    else:
        print("SKIP services CR retrigger — could not login", flush=True)

    check_operator_pods(None, "central")
    check_jobs(None, "central")

    # Final argo snapshot
    time.sleep(20)
    check_argocd()
    check_lab_config_cm()
    print("\n=== DONE ===", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.TimeoutExpired as e:
        print(f"TIMEOUT {e}", file=sys.stderr)
        raise SystemExit(1)
