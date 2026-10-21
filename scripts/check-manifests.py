#!/usr/bin/env python3
"""Server-side dry run of the sample objects and of the YAML snippets in the docs.

Finds every hybridsovereign.redhat object in the given YAML files (samples) and in the ```yaml
blocks of the given Markdown files (docs), and sends each one to the cluster with
`oc apply --dry-run=server --validate=strict`: the API server validates it (schema, CEL rules,
admission) and persists nothing. Tenant objects are moved into $SAMPLES_NS, an existing tenant
namespace on that cluster; platform objects keep their platform namespace.

usage: KUBECONFIG=<services kubeconfig> OC=<oc> SAMPLES_NS=entity-<tenant> \\
         check-manifests.py [PATH ...]          (default: samples docs)

Prints `file#n kind/name: ok|error <message>`; exit 1 when any object is refused.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
GROUP = "hybridsovereign.redhat"
FENCE = re.compile(r"^```ya?ml\s*$(.*?)^```\s*$", re.M | re.S)


def tenant_kinds():
    terms = yaml.safe_load((ROOT / "docs/content/terms.yaml").read_text(encoding="utf-8"))
    return {k for k, v in terms["kinds"].items() if v.get("audience") == "tenant"}


def objects(path):
    """(label, object) for every API object of the group in a file."""
    text = path.read_text(encoding="utf-8")
    chunks = [m.group(1) for m in FENCE.finditer(text)] if path.suffix == ".md" else [text]
    n = 0
    for chunk in chunks:
        try:
            docs = list(yaml.safe_load_all(chunk))
        except yaml.YAMLError as e:
            yield f"{path.relative_to(ROOT)}#{n + 1}", e
            continue
        for doc in docs:
            if isinstance(doc, dict) and str(doc.get("apiVersion", "")).startswith(GROUP + "/"):
                n += 1
                yield f"{path.relative_to(ROOT)}#{n}", doc


def files(args):
    for a in args or ["samples", "docs"]:
        p = (ROOT / a) if not Path(a).is_absolute() else Path(a)
        if p.is_dir():
            yield from sorted(q for q in p.rglob("*") if q.suffix in (".yaml", ".yml", ".md"))
        elif p.exists():
            yield p


def main():
    oc = os.environ.get("OC", "oc")
    ns = os.environ.get("SAMPLES_NS")
    if not os.environ.get("KUBECONFIG") or not ns:
        sys.exit("set KUBECONFIG (the services cluster) and SAMPLES_NS (an existing tenant namespace)")
    tenant = tenant_kinds()
    bad = total = 0
    for path in files(sys.argv[1:]):
        for label, obj in objects(path):
            total += 1
            if isinstance(obj, Exception):
                bad += 1
                print(f"{label}: error: not YAML: {obj}")
                continue
            kind, name = obj.get("kind"), obj.get("metadata", {}).get("name")
            if kind in tenant:
                obj.setdefault("metadata", {})["namespace"] = ns
            r = subprocess.run([oc, "apply", "--dry-run=server", "--validate=strict", "-o", "name",
                                "-f", "-"], input=yaml.safe_dump(obj), capture_output=True,
                               text=True)
            if r.returncode == 0:
                print(f"{label} {kind}/{name}: ok")
            else:
                bad += 1
                msg = " ".join((r.stderr or r.stdout).split())
                print(f"{label} {kind}/{name}: error {msg[:400]}")
    print(f"check-manifests: {total - bad}/{total} accepted (server dry run, nothing persisted)")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
