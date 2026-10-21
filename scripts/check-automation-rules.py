#!/usr/bin/env python3
"""Static check: every call the EDA jobs make on services with the automation token is allowed by its rules.

Scans the live EDA trees for tasks that use the services connection (services_bearer_token / services_api_host)
and derives the Kubernetes permission each needs:
  kubernetes.core.k8s           present: get, create, patch (+ update with force); absent: get, delete
  kubernetes.core.k8s_info      get, list (+ watch with wait)
  kubernetes.core.k8s_json_patch get, patch
  kubernetes.core.k8s_exec      pods/exec create, get
  kubernetes.core.k8s_log       pods/log get
  kubernetes.core.k8s_scale     <resource> get, <resource>/scale patch
  kubernetes.core.k8s_drain     nodes patch, pods/eviction create
  kubernetes.core.k8s_cp        pods/exec create, get
  kubernetes.core.k8s_rollback  get, patch
  hybridsovereign.shc.cr_status <plural>/status get, patch
  ansible.builtin.uri           the REST path (/api/v1/..., /apis/<group>/<version>/...), verb from the method
and checks each against RBAC rules with the API server's matching (apiGroups, resources incl. */sub, verbs,
resourceNames only for a literal name).

Rules: --rules FILE (a YAML list of PolicyRules, or a mapping with 'rules') or --values FILE (central values;
servicesCluster.automation.rules). With neither, bootstrap/helm/central/values.yaml is read; when it has no
automation rules the check is skipped (exit 0, says so).

Exit 1 when a needed permission is not granted. Unresolvable kinds (templated) are listed as notes.
usage: scripts/check-automation-rules.py [--rules FILE | --values FILE] [--root DIR] [--verbose] [--list]
"""
import argparse
import os
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
TREES = ("eda/rulebooks", "eda/common")
SKIP_PARTS = ("/collections/", "/rulebooks/common/")

# kind -> (group, plural) for the kinds the jobs touch whose plural is not lowercase + "s"
PLURALS = {
    "Ingress": "ingresses", "NetworkPolicy": "networkpolicies", "Endpoints": "endpoints",
    "StorageClass": "storageclasses", "IngressClass": "ingressclasses", "PodSecurityPolicy": "podsecuritypolicies",
    "SecurityContextConstraints": "securitycontextconstraints", "ResourceQuota": "resourcequotas",
    "Policy": "policies", "Proxy": "proxies", "DNS": "dnses", "Gateway": "gateways",
    "ProjectRequest": "projectrequests", "PriorityClass": "priorityclasses",
}

TEMPLATE = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.S)


def load_crd_plurals(root):
    """The plurals of the platform's own kinds, from the CRD sources."""
    for f in sorted((root / "operator/config/crd/bases").glob("crd-*.yaml")):
        d = yaml.safe_load(f.read_text())
        if isinstance(d, dict) and d.get("kind") == "CustomResourceDefinition":
            n = d["spec"]["names"]
            PLURALS.setdefault(n["kind"], n["plural"])


def plural_of(kind):
    if kind in PLURALS:
        return PLURALS[kind]
    k = kind.lower()
    if k.endswith("y") and not k.endswith(("ay", "ey", "oy", "uy")):
        return k[:-1] + "ies"
    if k.endswith(("s", "x", "ch", "sh")):
        return k + "es"
    return k + "s"


def group_of(api_version):
    av = str(api_version or "")
    return av.split("/")[0] if "/" in av else ""


def literal(v):
    """The value if it holds no template, else None."""
    if v is None:
        return None
    s = str(v)
    return None if TEMPLATE.search(s) else s


def uses_services(args):
    blob = " ".join(str(args.get(k, "")) for k in ("api_key", "host", "kubeconfig", "url", "headers"))
    return "services_bearer_token" in blob or "services_api_host" in blob


def needs_from_k8s(mod, a):
    """[(group, resource, verbs, name)] or a note string."""
    short = mod.split(".")[-1]
    kind, av = a.get("kind"), a.get("api_version")
    d = a.get("definition")
    if kind is None and isinstance(d, dict):
        kind, av = d.get("kind"), d.get("apiVersion")
    if kind is None and isinstance(d, str):
        m = re.search(r"^\s*kind:\s*([A-Za-z]+)\s*$", d, re.M)
        kind = m.group(1) if m else None
        m = re.search(r"^\s*apiVersion:\s*([\w./-]+)\s*$", d, re.M)
        av = m.group(1) if m else av
    name = literal(a.get("name") or (d.get("metadata", {}).get("name") if isinstance(d, dict) else None))
    if short == "k8s_exec" or short == "k8s_cp":
        return [("", "pods/exec", {"create", "get"}, literal(a.get("pod")))]
    if short == "k8s_log":
        return [("", "pods/log", {"get"}, literal(a.get("name")))]
    if short == "k8s_drain":
        return [("", "nodes", {"get", "patch"}, None), ("", "pods/eviction", {"create"}, None)]
    if short == "cr_status":
        pl = literal(a.get("plural"))
        if pl is None:
            return f"cr_status with a templated plural ({a.get('plural')})"
        return [(group_of(literal(a.get("api_version")) or ""), pl + "/status", {"get", "patch"}, name)]
    kind_l, av_l = literal(kind), literal(av if av is not None else "v1")  # the modules default to v1
    if not kind_l or av_l is None:
        return f"{short} with an unresolved kind ({kind}, {av})"
    group, res = group_of(av_l), plural_of(kind_l)
    if short == "k8s_info":
        verbs = {"get", "list"} | ({"watch"} if a.get("wait") else set())
        return [(group, res, verbs, None)]
    if short == "k8s_scale":
        return [(group, res, {"get"}, name), (group, res + "/scale", {"get", "patch"}, name)]
    if short in ("k8s_json_patch", "k8s_rollback"):
        return [(group, res, {"get", "patch"}, name)]
    state = literal(a.get("state")) or "present"
    if state == "absent":
        return [(group, res, {"get", "delete"}, name)]
    if state == "patched":
        return [(group, res, {"get", "patch"}, name)]
    verbs = {"get", "create", "patch"} | ({"update"} if a.get("force") else set())
    return [(group, res, verbs, name)]


REST = re.compile(r"/(api/v1|apis/([^/]+)/[^/]+)(/.*)?$")


VAR = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def resolve(url, facts):
    """Substitute {{ var }} with the var's set_fact value from the same file (a few levels)."""
    for _ in range(4):
        new = VAR.sub(lambda m: str(facts.get(m.group(1), m.group(0))).strip(), url)
        if new == url:
            break
        url = new
    return url


def needs_from_uri(u, facts):
    url = resolve(str(u.get("url", "")), facts)
    if "services_api_host" not in url:
        if "services_bearer_token" in str(u.get("headers", "")):
            return f"uri with the services token to an unresolved URL ({u.get('url')})"
        return None  # a call to another service (e.g. a Route), not the services API
    m = REST.search(TEMPLATE.sub("X", url).split("?")[0])
    if not m:
        return f"uri to an unrecognised API path ({url})"
    group = m.group(2) or ""
    parts = [p for p in (m.group(3) or "").split("/") if p]
    if len(parts) >= 2 and parts[0] == "namespaces" and len(parts) > 2:
        parts = parts[2:]  # namespaced resource
    if not parts:
        return f"uri to an API root ({url})"
    res, name, sub = parts[0], (parts[1] if len(parts) > 1 else None), (parts[2] if len(parts) > 2 else None)
    method = literal(u.get("method")) or "GET"
    verb = {"GET": "get" if name else "list", "POST": "create", "PUT": "update", "PATCH": "patch",
            "DELETE": "delete", "HEAD": "get"}.get(method.upper(), method.lower())
    if res in ("subjectaccessreviews", "selfsubjectaccessreviews", "selfsubjectreviews", "tokenreviews"):
        name = None
    return [(group, res + (f"/{sub}" if sub else ""), {verb}, literal(name) if name and name != "X" else None)]


def collect_facts(node, facts):
    if isinstance(node, list):
        for n in node:
            collect_facts(n, facts)
    elif isinstance(node, dict):
        for key in ("ansible.builtin.set_fact", "set_fact", "vars"):
            v = node.get(key)
            if isinstance(v, dict):
                for k, val in v.items():
                    if isinstance(val, str):
                        facts.setdefault(k, val)
        for v in node.values():
            if isinstance(v, (list, dict)):
                collect_facts(v, facts)


def walk(node, path, out, notes, facts):
    if isinstance(node, list):
        for n in node:
            walk(n, path, out, notes, facts)
        return
    if not isinstance(node, dict):
        return
    for key, a in node.items():
        if not isinstance(a, dict):
            continue
        mod = key
        if mod.startswith("kubernetes.core.k8s") or mod in ("k8s", "k8s_info", "k8s_exec", "k8s_log", "k8s_scale",
                                                            "k8s_json_patch") or mod.endswith("shc.cr_status"):
            if not uses_services(a):
                continue
            r = needs_from_k8s(mod, a)
        elif mod in ("ansible.builtin.uri", "uri"):
            if not uses_services(a):
                continue
            r = needs_from_uri(a, facts)
            if r is None:
                continue
        else:
            continue
        if isinstance(r, str):
            notes.append((path, r))
        else:
            for need in r:
                out.append((path, node.get("name", "?"), mod.split(".")[-1], need))
    for v in node.values():
        if isinstance(v, (list, dict)):
            walk(v, path, out, notes, facts)


def scan(root):
    out, notes = [], []
    for tree in TREES:
        for dp, _, files in os.walk(root / tree):
            if any(s in dp + "/" for s in SKIP_PARTS):
                continue
            for f in sorted(files):
                if not f.endswith((".yml", ".yaml")):
                    continue
                p = Path(dp) / f
                try:
                    docs = list(yaml.safe_load_all(p.read_text()))
                except Exception:
                    continue
                facts = {}
                collect_facts(docs, facts)
                walk(docs, str(p.relative_to(root)), out, notes, facts)
    return out, notes


def resource_matches(rule_res, res):
    sub = res.split("/", 1)[1] if "/" in res else ""
    for r in rule_res:
        if r == "*" or r == res or (sub and r == f"*/{sub}"):
            return True
    return False


def allowed(rules, group, res, verb, name):
    for r in rules:
        if not ({"*", group} & set(r.get("apiGroups") or [])):
            continue
        if not resource_matches(r.get("resources") or [], res):
            continue
        if not ({"*", verb} & set(r.get("verbs") or [])):
            continue
        rn = r.get("resourceNames") or []
        if rn and (name is None or name not in rn):
            continue
        return True
    return False


def load_rules(a, root):
    if a.rules:
        d = yaml.safe_load(Path(a.rules).read_text())
        return (d.get("rules") if isinstance(d, dict) else d), a.rules
    vf = Path(a.values) if a.values else root / "bootstrap/helm/central/values.yaml"
    d = yaml.safe_load(vf.read_text()) or {}
    rules = (((d.get("servicesCluster") or {}).get("automation") or {}).get("rules"))
    return rules, f"{vf} servicesCluster.automation.rules"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--rules")
    g.add_argument("--values")
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--verbose", action="store_true", help="also list the unresolved calls")
    ap.add_argument("--list", action="store_true", help="print every derived permission (group, resource, verb)")
    a = ap.parse_args()
    root = Path(a.root)
    rules, src = load_rules(a, root)
    if not rules:
        print(f"check-automation-rules: SKIP, no automation rules in {src} (pass --rules or --values)")
        return 0
    load_crd_plurals(root)
    needs, notes = scan(root)
    missing = {}
    for path, task, mod, (group, res, verbs, name) in needs:
        for v in sorted(verbs):
            if not allowed(rules, group, res, v, name):
                missing.setdefault((group or "core", res, v), []).append(f"{path}: {task} ({mod})")
    if a.list:
        seen = sorted({(g or "core", r, v) for _, _, _, (g, r, vs, _) in needs for v in vs})
        for g, r, v in seen:
            print(f"  need {v} {r} ({g})")
    for path, note in notes:
        if a.verbose:
            print(f"  note {path}: {note}")
    print(f"check-automation-rules: {len(needs)} services calls checked against {src}; "
          f"{len(missing)} missing permission(s); {len(notes)} unresolved call(s)")
    for (group, res, verb), where in sorted(missing.items()):
        print(f"  MISSING {verb} {res} ({group}), {len(where)} call(s), e.g. {where[0]}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
