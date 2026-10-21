#!/usr/bin/env python3
"""Schema replay: load exported hybridsovereign.redhat objects into a throwaway API server under candidate CRDs
(and admission policies) and report every rejection and every field the server pruned, defaulted or changed.

Modes:
  fresh    (no --baseline-crds)  install the candidate CRDs, create every object, write its status, read it back.
  upgrade  (--baseline-crds DIR) install the baseline CRDs, load the objects, apply the candidate CRDs, then send a
                                 no-op UPDATE of every object's spec and status (exercises ratcheting and transition
                                 rules the way a live update would), read it back.
Input layout (tests/schema-replay/export.sh writes it): OBJECTS/<plural>.<group>.json, each `oc get -o json` list;
NAMESPACES (optional) = `oc get namespaces -o json` (labels kept, created before the objects).

Backends:
  envtest     etcd + kube-apiserver from $KUBEBUILDER_ASSETS (or --assets) as unprivileged subprocesses, a static
              admin token, RBAC authorization. Runs anywhere, also inside the ansible-runner image.
  kind        `kind create cluster` (podman or docker provider) on a workstation; deleted at the end unless --keep.
  kubeconfig  an existing EMPTY cluster (--kubeconfig); nothing is cleaned up.

Writes use field validation "warn", as the controllers' writes do: an unknown field is pruned (and reported as a
diff and a warning), not refused. Exit 0 only with zero rejections and zero diffs (and a zero exit of --exec, when
given).

usage:
  python3 -I tests/schema-replay/replay.py --objects DIR --crds FILE [--namespaces FILE] [--baseline-crds DIR]
      [--policies FILE ...] [--backend envtest|kind|kubeconfig] [--as USER] [--inventory OUT] [--report OUT]
      [--exec CMD] [--keep]
"""
import argparse
import copy
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    print("replay: PyYAML is required", file=sys.stderr)
    sys.exit(2)

GROUP = "hybridsovereign.redhat"
SERVER_METADATA = ("uid", "resourceVersion", "generation", "creationTimestamp", "managedFields", "selfLink",
                   "deletionTimestamp", "deletionGracePeriodSeconds", "ownerReferences", "finalizers")
SERVER_ANNOTATIONS = ("kubectl.kubernetes.io/last-applied-configuration",)
KIND_IMAGE = "kindest/node:v1.35.0"
HELM_COMMENT = re.compile(r"\{\{-?\s*/\*.*?\*/\s*-?\}\}", re.S)


def log(msg):
    print(f"replay: {msg}", file=sys.stderr, flush=True)


# ─── input ───────────────────────────────────────────────────────────────────

def load_docs(path):
    """Kubernetes objects from a YAML/JSON file or directory (Helm template lines `{{` dropped, Lists expanded)."""
    path = Path(path)
    files = sorted(p for p in path.rglob("*") if p.suffix in (".yaml", ".yml", ".json")) if path.is_dir() else [path]
    out = []
    for f in files:
        text = f.read_text()
        if f.suffix == ".json":
            docs = [json.loads(text)]
        else:
            text = HELM_COMMENT.sub("", text)
            docs = list(yaml.safe_load_all("\n".join(ln for ln in text.splitlines() if "{{" not in ln)))
        for d in docs:
            if not isinstance(d, dict):
                continue
            if d.get("kind", "").endswith("List") and "items" in d:
                out.extend(i for i in d["items"] if isinstance(i, dict))
            else:
                out.append(d)
    return out


def clean_crd(crd):
    crd = copy.deepcopy(crd)
    md = crd.setdefault("metadata", {})
    for k in SERVER_METADATA:
        md.pop(k, None)
    ann = md.get("annotations") or {}
    for k in SERVER_ANNOTATIONS:
        ann.pop(k, None)
    crd.pop("status", None)
    return crd


def load_crds(path):
    crds = {}
    for d in load_docs(path):
        if d.get("kind") == "CustomResourceDefinition" and d.get("spec", {}).get("group") == GROUP:
            crds[d["metadata"]["name"]] = clean_crd(d)
    if not crds:
        raise SystemExit(f"replay: no {GROUP} CRDs in {path}")
    return crds


def clean_object(o):
    o = copy.deepcopy(o)
    md = o.setdefault("metadata", {})
    for k in SERVER_METADATA:
        md.pop(k, None)
    ann = md.get("annotations")
    if ann is not None:
        for k in SERVER_ANNOTATIONS:
            ann.pop(k, None)
        if not ann:
            md.pop("annotations")
    if md.get("labels") == {}:
        md.pop("labels")
    return o


def load_objects(objects_dir):
    objs = []
    for f in sorted(Path(objects_dir).glob(f"*.{GROUP}.json")):
        data = json.loads(f.read_text())
        for item in data.get("items", []):
            objs.append(clean_object(item))
    return objs


# ─── backends ────────────────────────────────────────────────────────────────

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Backend:
    kubeconfig = None

    def start(self):
        raise NotImplementedError

    def stop(self):
        pass


class Envtest(Backend):
    """etcd + kube-apiserver, unprivileged, in a temporary directory."""

    def __init__(self, assets, keep=False):
        self.assets = Path(assets or os.environ.get("KUBEBUILDER_ASSETS") or "")
        self.keep = keep
        self.procs = []

    def binary(self, name):
        p = self.assets / name
        if not p.is_file():
            raise SystemExit(f"replay: {name} not found in KUBEBUILDER_ASSETS={self.assets} (see README: envtest)")
        return str(p)

    def start(self):
        self.dir = Path(tempfile.mkdtemp(prefix="shc-replay-"))
        etcd_port, peer_port, api_port = free_port(), free_port(), free_port()
        token = os.urandom(16).hex()
        (self.dir / "tokens.csv").write_text(f'{token},replay-admin,replay-admin,"system:masters"\n')
        subprocess.run(["openssl", "genrsa", "-out", str(self.dir / "sa.key"), "2048"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["openssl", "rsa", "-in", str(self.dir / "sa.key"), "-pubout", "-out", str(self.dir / "sa.pub")],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        etcd_log = open(self.dir / "etcd.log", "w")
        self.procs.append(subprocess.Popen(
            [self.binary("etcd"), "--data-dir", str(self.dir / "etcd"),
             "--listen-client-urls", f"http://127.0.0.1:{etcd_port}",
             "--advertise-client-urls", f"http://127.0.0.1:{etcd_port}",
             "--listen-peer-urls", f"http://127.0.0.1:{peer_port}",
             "--initial-advertise-peer-urls", f"http://127.0.0.1:{peer_port}",
             "--initial-cluster", f"default=http://127.0.0.1:{peer_port}",
             "--unsafe-no-fsync"],
            stdout=etcd_log, stderr=subprocess.STDOUT, start_new_session=True))
        api_log = open(self.dir / "kube-apiserver.log", "w")
        self.procs.append(subprocess.Popen(
            [self.binary("kube-apiserver"),
             "--etcd-servers", f"http://127.0.0.1:{etcd_port}",
             "--cert-dir", str(self.dir / "certs"),
             "--secure-port", str(api_port), "--bind-address", "127.0.0.1", "--advertise-address", "127.0.0.1",
             "--token-auth-file", str(self.dir / "tokens.csv"),
             "--authorization-mode", "RBAC",
             "--service-cluster-ip-range", "10.0.0.0/24",
             "--service-account-issuer", "https://replay.invalid",
             "--service-account-key-file", str(self.dir / "sa.pub"),
             "--service-account-signing-key-file", str(self.dir / "sa.key"),
             "--disable-admission-plugins", "ServiceAccount"],
            stdout=api_log, stderr=subprocess.STDOUT, start_new_session=True))
        self.kubeconfig = str(self.dir / "kubeconfig")
        Path(self.kubeconfig).write_text(yaml.safe_dump({
            "apiVersion": "v1", "kind": "Config", "current-context": "replay",
            "clusters": [{"name": "replay", "cluster": {"server": f"https://127.0.0.1:{api_port}",
                                                        "insecure-skip-tls-verify": True}}],
            "users": [{"name": "replay-admin", "user": {"token": token}}],
            "contexts": [{"name": "replay", "context": {"cluster": "replay", "user": "replay-admin"}}]}))
        os.chmod(self.kubeconfig, 0o600)
        deadline = time.time() + 90
        while time.time() < deadline:
            if any(p.poll() is not None for p in self.procs):
                break
            r = subprocess.run(kubectl_base(self.kubeconfig) + ["get", "--raw", "/readyz"],
                               capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip() == "ok":
                log(f"envtest API server ready on 127.0.0.1:{api_port} ({self.dir})")
                return
            time.sleep(1)
        tail = (self.dir / "kube-apiserver.log").read_text()[-2000:]
        self.stop()
        raise SystemExit(f"replay: envtest API server did not become ready\n{tail}")

    def stop(self):
        for p in reversed(self.procs):
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                    p.wait(timeout=20)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(p.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        if not self.keep and getattr(self, "dir", None):
            shutil.rmtree(self.dir, ignore_errors=True)


class Kind(Backend):
    def __init__(self, keep=False, name="shc-replay", image=KIND_IMAGE):
        self.keep, self.name, self.image = keep, name, image

    def start(self):
        if not shutil.which("kind"):
            raise SystemExit("replay: kind not found (use --backend envtest)")
        self.dir = Path(tempfile.mkdtemp(prefix="shc-replay-kind-"))
        self.kubeconfig = str(self.dir / "kubeconfig")
        env = {**os.environ}
        if shutil.which("podman") and not shutil.which("docker"):
            env.setdefault("KIND_EXPERIMENTAL_PROVIDER", "podman")
        subprocess.run(["kind", "create", "cluster", "--name", self.name, "--image", self.image,
                        "--kubeconfig", self.kubeconfig, "--wait", "120s"], check=True, env=env)
        self.env = env

    def stop(self):
        if not self.keep:
            subprocess.run(["kind", "delete", "cluster", "--name", self.name], env=self.env, check=False)
            shutil.rmtree(self.dir, ignore_errors=True)


class Existing(Backend):
    def __init__(self, kubeconfig):
        if not kubeconfig:
            raise SystemExit("replay: --backend kubeconfig needs --kubeconfig")
        self.kubeconfig = kubeconfig

    def start(self):
        pass


# ─── kubectl ─────────────────────────────────────────────────────────────────

KUBECTL = None


def kubectl_base(kubeconfig):
    return [KUBECTL, "--kubeconfig", kubeconfig]


class Kube:
    def __init__(self, kubeconfig, as_user=None):
        self.kc = kubeconfig
        self.as_user = as_user

    def run(self, args, body=None, as_user=None, check=False):
        cmd = kubectl_base(self.kc) + args
        who = as_user if as_user is not None else self.as_user
        if who:
            cmd += [f"--as={who}"]
        r = subprocess.run(cmd, input=json.dumps(body) if body is not None else None, capture_output=True, text=True)
        if check and r.returncode != 0:
            raise RuntimeError(f"kubectl {' '.join(args)}: {r.stderr.strip()}")
        return r

    def json(self, args, body=None, as_user=None):
        r = self.run(args + ["-o", "json"], body=body, as_user=as_user)
        out = json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else None
        warnings = [ln for ln in r.stderr.splitlines() if ln.startswith("Warning:")]
        err = r.stderr.strip() if r.returncode != 0 else ""
        return out, warnings, err

    def apply_docs(self, docs, server_side=True):
        if not docs:
            return
        body = {"apiVersion": "v1", "kind": "List", "items": docs}
        args = ["apply", "-f", "-"] + (["--server-side", "--force-conflicts", "--field-manager=shc-replay"]
                                       if server_side else [])
        r = self.run(args, body=body, as_user="")
        if r.returncode != 0:
            raise RuntimeError(f"apply failed: {r.stderr.strip()[:2000]}")
        return r


def wait_established(kube, names, timeout=120):
    args = ["wait", "--for=condition=Established", f"--timeout={timeout}s"] + [f"crd/{n}" for n in names]
    r = kube.run(args, as_user="")
    if r.returncode != 0:
        raise RuntimeError(f"CRDs not Established: {r.stderr.strip()}")
    # discovery lags Established a little
    time.sleep(2)


# ─── diff ────────────────────────────────────────────────────────────────────

def diff(a, b, prefix=""):
    """[(path, kind)] where b (from the server) differs from a (sent): pruned, defaulted, changed."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            p = f"{prefix}.{k}" if prefix else k
            if k not in b:
                out.append((p, "pruned"))
            elif k not in a:
                out.append((p, "defaulted"))
            else:
                out.extend(diff(a[k], b[k], p))
        return out
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            out.extend(diff(x, y, f"{prefix}[{i}]"))
        return out
    return [] if a == b else [(prefix, "changed")]


def compared(o):
    md = o.get("metadata") or {}
    ann = {k: v for k, v in (md.get("annotations") or {}).items() if k not in SERVER_ANNOTATIONS}
    return {"spec": o.get("spec"), "status": o.get("status"), "labels": md.get("labels") or {}, "annotations": ann}


def key_paths(v, prefix=""):
    out = set()
    if isinstance(v, dict):
        for k, x in v.items():
            p = f"{prefix}.{k}" if prefix else k
            out.add(p)
            out |= key_paths(x, p)
    elif isinstance(v, list):
        for x in v:
            out |= key_paths(x, prefix + "[]")
    return out


# ─── replay ──────────────────────────────────────────────────────────────────

def plural_of(obj, crds):
    for c in crds.values():
        if c["spec"]["names"]["kind"] == obj["kind"]:
            return c["spec"]["names"]["plural"]
    return None


def ref(o):
    md = o["metadata"]
    return f"{o['kind']}/{md.get('namespace', '')}/{md['name']}"


def replay(args, kube, report):
    candidate = load_crds(args.crds)
    baseline = load_crds(args.baseline_crds) if args.baseline_crds else None
    first = baseline or candidate
    objects = load_objects(args.objects)
    report["mode"] = "upgrade" if baseline else "fresh"
    report["objects"] = len(objects)
    log(f"{report['mode']} mode: {len(first)} CRDs first, {len(objects)} objects")

    kube.apply_docs(list(first.values()))
    wait_established(kube, list(first))

    policy_docs = []
    for p in args.policies or []:
        policy_docs.extend(d for d in load_docs(p)
                           if d.get("kind") in ("ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding"))
    if policy_docs:
        kube.apply_docs(policy_docs)
        time.sleep(3)  # policies are picked up asynchronously
        report["policies"] = [d["metadata"]["name"] for d in policy_docs if d["kind"] == "ValidatingAdmissionPolicy"]
        for d in policy_docs:
            if d["kind"] != "ValidatingAdmissionPolicy":
                continue
            st, _, _ = kube.json(["get", "validatingadmissionpolicy", d["metadata"]["name"]], as_user="")
            tc = ((st or {}).get("status") or {}).get("typeChecking") or {}
            for w in tc.get("expressionWarnings") or []:
                report["policyTypeWarnings"].append({"policy": d["metadata"]["name"], **w})

    if args.as_user:
        kube.apply_docs([{"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
                          "metadata": {"name": "shc-replay-as-user"},
                          "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole",
                                      "name": "cluster-admin"},
                          "subjects": [{"apiGroup": "rbac.authorization.k8s.io", "kind": "User",
                                        "name": args.as_user}]}])

    # namespaces, with their exported labels
    ns_labels = {}
    if args.namespaces:
        for n in load_docs(args.namespaces):
            if n.get("kind") == "Namespace":
                ns_labels[n["metadata"]["name"]] = (n["metadata"].get("labels") or {})
    wanted = sorted({o["metadata"].get("namespace") for o in objects if o["metadata"].get("namespace")})
    kube.apply_docs([{"apiVersion": "v1", "kind": "Namespace",
                      "metadata": {"name": n, "labels": {k: v for k, v in ns_labels.get(n, {}).items()
                                                         if k != "kubernetes.io/metadata.name"}}}
                     for n in wanted])

    sent = {}
    for o in objects:
        r = ref(o)
        plural = plural_of(o, first)
        if plural is None:
            report["rejections"].append({"object": r, "step": "create", "error": "no CRD for this kind"})
            continue
        body = copy.deepcopy(o)
        status = body.pop("status", None)
        out, warns, err = kube.json(["create", "--validate=warn", "-f", "-"], body=body)
        record_warnings(report, r, "create", warns)
        if out is None:
            report["rejections"].append({"object": r, "step": "create", "error": err})
            continue
        if status is not None:
            out["status"] = status
            st, warns, err = kube.json(["replace", "--validate=warn", "--subresource=status", "-f", "-"], body=out)
            record_warnings(report, r, "status", warns)
            if st is None:
                report["rejections"].append({"object": r, "step": "status", "error": err})
        sent[r] = (o, plural)
        for p in key_paths(o.get("status") or {}):
            report["inventory"].setdefault(o["kind"], set()).add(p)

    if baseline:
        kube.apply_docs(list(candidate.values()))
        wait_established(kube, list(candidate))
        for r, (o, plural) in sent.items():
            md = o["metadata"]
            ns = ["-n", md["namespace"]] if md.get("namespace") else []
            live, _, err = kube.json(["get", f"{plural}.{GROUP}", md["name"]] + ns, as_user="")
            if live is None:
                report["rejections"].append({"object": r, "step": "get", "error": err})
                continue
            up, warns, err = kube.json(["replace", "--validate=warn", "-f", "-"], body=live)
            record_warnings(report, r, "update", warns)
            if up is None:
                report["rejections"].append({"object": r, "step": "update", "error": err})
                continue
            if "status" in live:
                up["status"] = live["status"]
                st, warns, err = kube.json(["replace", "--validate=warn", "--subresource=status", "-f", "-"], body=up)
                record_warnings(report, r, "status-update", warns)
                if st is None:
                    report["rejections"].append({"object": r, "step": "status-update", "error": err})

    for r, (o, plural) in sent.items():
        md = o["metadata"]
        ns = ["-n", md["namespace"]] if md.get("namespace") else []
        live, _, err = kube.json(["get", f"{plural}.{GROUP}", md["name"]] + ns, as_user="")
        if live is None:
            report["rejections"].append({"object": r, "step": "read", "error": err})
            continue
        d = diff(compared(o), compared(live))
        if d:
            report["diffs"].append({"object": r, "paths": [{"path": p, "kind": k} for p, k in d]})


def record_warnings(report, r, step, warns):
    for w in warns:
        report["warnings"].append({"object": r, "step": step, "warning": w})


def main():
    global KUBECTL
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--objects", required=True, help="directory of <plural>.<group>.json lists")
    ap.add_argument("--namespaces", help="namespaces.json (oc get namespaces -o json)")
    ap.add_argument("--crds", required=True, help="candidate CRDs: crds.yaml, a rendered chart, or a directory")
    ap.add_argument("--baseline-crds", help="CRDs the objects were written under (upgrade mode)")
    ap.add_argument("--policies", action="append", help="rendered ValidatingAdmissionPolicies/bindings (repeatable)")
    ap.add_argument("--backend", choices=("envtest", "kind", "kubeconfig"), default="envtest")
    ap.add_argument("--kubeconfig", help="cluster for --backend kubeconfig")
    ap.add_argument("--assets", help="envtest binaries (default $KUBEBUILDER_ASSETS)")
    ap.add_argument("--kubectl", help="kubectl binary (default: <assets>/kubectl, kubectl, oc)")
    ap.add_argument("--as", dest="as_user", help="create and update the objects as this user (bound to cluster-admin)")
    ap.add_argument("--inventory", help="write the union of status key paths per kind (JSON)")
    ap.add_argument("--report", help="write the full report (JSON)")
    ap.add_argument("--exec", dest="exec_cmd", help="run this command (KUBECONFIG set) after the replay; its exit "
                                                    "status counts")
    ap.add_argument("--keep", action="store_true", help="keep the kind cluster / envtest directory")
    args = ap.parse_args()

    assets = args.assets or os.environ.get("KUBEBUILDER_ASSETS") or ""
    KUBECTL = args.kubectl or next((c for c in (str(Path(assets) / "kubectl") if assets else "",
                                                shutil.which("kubectl") or "", shutil.which("oc") or "")
                                    if c and Path(c).exists()), None)
    if not KUBECTL:
        raise SystemExit("replay: no kubectl (set --kubectl)")

    backend = {"envtest": lambda: Envtest(assets, args.keep), "kind": lambda: Kind(args.keep),
               "kubeconfig": lambda: Existing(args.kubeconfig)}[args.backend]()
    report = {"backend": args.backend, "rejections": [], "diffs": [], "warnings": [], "policyTypeWarnings": [],
              "inventory": {}}
    exec_rc = 0
    try:
        backend.start()
        kube = Kube(backend.kubeconfig, args.as_user)
        replay(args, kube, report)
        if args.exec_cmd:
            log(f"exec: {args.exec_cmd}")
            exec_rc = subprocess.run(args.exec_cmd, shell=True, env={**os.environ, "KUBECONFIG": backend.kubeconfig,
                                                                     "REPLAY_KUBECTL": KUBECTL}).returncode
            report["execExit"] = exec_rc
    finally:
        backend.stop()

    report["inventory"] = {k: sorted(v) for k, v in sorted(report["inventory"].items())}
    if args.inventory:
        Path(args.inventory).write_text(json.dumps(report["inventory"], indent=1) + "\n")
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=1) + "\n")
    print(f"replay ({report['mode']}, {args.backend}): {report['objects']} objects, "
          f"{len(report['rejections'])} rejection(s), {len(report['diffs'])} object(s) with a diff, "
          f"{len(report['warnings'])} warning(s), {len(report['policyTypeWarnings'])} policy type warning(s)")
    for x in report["rejections"]:
        print(f"  REJECTED {x['object']} at {x['step']}: {x['error'][:400]}")
    for x in report["diffs"]:
        print(f"  DIFF {x['object']}: " + ", ".join(f"{p['path']} ({p['kind']})" for p in x["paths"][:12]))
    for x in report["warnings"][:50]:
        print(f"  warning {x['object']} at {x['step']}: {x['warning']}")
    for x in report["policyTypeWarnings"]:
        print(f"  policy type warning {x['policy']}: {x.get('fieldRef')}: {x.get('warning')}")
    if args.exec_cmd:
        print(f"  exec exit {exec_rc}")
    ok = not report["rejections"] and not report["diffs"] and exec_rc == 0
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
