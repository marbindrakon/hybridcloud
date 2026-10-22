#!/usr/bin/env python3
"""Write the tenant role tables from the role definitions.

Reads the roles the platform creates in a tenant namespace (entity_named_roles in
eda/rulebooks/roles/entity_provision/vars/main.yml), their display names (roles: in
docs/content/terms.yaml), the kinds' display terms and the CRDs' categories, and replaces
- the text between <!-- roles:begin --> and <!-- roles:end --> in docs/platform/onboard-a-tenant.md
  (what each role may create, change, delete and read), and
- the text between <!-- list-commands:begin --> and <!-- list-commands:end --> in docs/tenant/glossary.md
  (the CRD categories and, per role, the `oc get` that lists what the role reads without Forbidden lines).
Deprecated keys are left out.

usage: gen-role-table.py [--check]     --check: exit 1 when a document is not current
"""
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLES = ROOT / "eda/rulebooks/roles/entity_provision/vars/main.yml"
TERMS = ROOT / "docs/content/terms.yaml"
GUIDE = ROOT / "docs/platform/onboard-a-tenant.md"
BEGIN, END = "<!-- roles:begin -->", "<!-- roles:end -->"
GLOSSARY = ROOT / "docs/tenant/glossary.md"
LIST_BEGIN, LIST_END = "<!-- list-commands:begin -->", "<!-- list-commands:end -->"
# The tenant-wide category and the narrower ones, in the order a command names them. Every tenant role also reads the
# tenant profile (tenant_baseline_rules in eda/common/vars/scope_levels.yml).
TENANT_CATEGORY = "shc-tenant"
NARROW_CATEGORIES = ["shc-network", "shc-environment", "shc-appplatform", "shc-services", "shc-access"]
BASELINE_KINDS = {"TenantProfile"}
# A plural that names another resource on an OpenShift cluster is written with its group.
QUALIFIED = {"projects": "projects.hybridsovereign.redhat"}


def crd_names():
    out = []
    for p in sorted((ROOT / "operator/config/crd/bases").glob("crd-*.yaml")):
        text = "\n".join("#" if ln.lstrip().startswith("{{") else ln
                         for ln in p.read_text(encoding="utf-8").split("\n"))
        for doc in yaml.safe_load_all(text):
            if doc and doc.get("kind") == "CustomResourceDefinition":
                out.append(doc["spec"]["names"])
    return out


def plural_to_kind():
    return {n["plural"]: n["kind"] for n in crd_names()}


def categories():
    out = {}
    for n in crd_names():
        for c in n.get("categories", []):
            out.setdefault(c, []).append(n["kind"])
    return {c: sorted(k) for c, k in out.items()}


def table():
    roles = yaml.safe_load(ROLES.read_text(encoding="utf-8"))["entity_named_roles"]
    terms = yaml.safe_load(TERMS.read_text(encoding="utf-8"))
    kinds, names = terms["kinds"], terms["roles"]
    p2k = plural_to_kind()

    def words(resources):
        out = []
        for r in resources:
            kind = p2k.get(r)
            out.append(kinds[kind]["plural"] if kind in kinds else r)
        return ", ".join(out) if out else "-"

    lines = ["| Role | Key in `spec.namespaceRbac` | Create, change and delete | Read |",
             "|---|---|---|---|"]
    for r in roles:
        meta = names.get(r["spec_key"], {})
        if meta.get("state") == "deprecated":
            continue
        lines.append(f"| {meta.get('name', r['spec_key'])} | `{r['spec_key']}` | "
                     f"{words(r['crud_resources'])} | "
                     f"{words(r['read_resources']) if r['read_resources'] else 'the same'} |")
    return "\n".join(lines)


def list_command(readable, plurals, cats):
    """The `oc get` arguments that list the readable kinds and nothing else (a category whose kinds are all
    readable stands for them; the tenant-wide one only when every kind of it is readable)."""
    if set(cats.get(TENANT_CATEGORY, [])) <= readable | BASELINE_KINDS:
        return [TENANT_CATEGORY]
    args, covered = [], set()
    for c in NARROW_CATEGORIES:
        kinds = set(cats.get(c, []))
        if kinds and kinds <= readable and not kinds <= covered:
            args.append(c)
            covered |= kinds
    args += [QUALIFIED.get(p, p) for p, k in plurals if k in readable and k not in covered]
    return args


def list_tables():
    roles = yaml.safe_load(ROLES.read_text(encoding="utf-8"))["entity_named_roles"]
    terms = yaml.safe_load(TERMS.read_text(encoding="utf-8"))
    kinds, names = terms["kinds"], terms["roles"]
    p2k, cats = plural_to_kind(), categories()

    lines = ["| Category | Kinds |", "|---|---|"]
    for c in [TENANT_CATEGORY] + NARROW_CATEGORIES:
        lines.append(f"| `{c}` | " + ", ".join(
            f"{kinds[k]['plural'] if k in kinds else k} (`{k}`)" for k in cats.get(c, [])) + " |")
    lines += ["", "| Role | Lists what the role reads |", "|---|---|"]
    for r in roles:
        meta = names.get(r["spec_key"], {})
        if meta.get("state") == "deprecated":
            continue
        resources = list(dict.fromkeys(r["crud_resources"] + r["read_resources"]))
        plurals = [(p, p2k[p]) for p in resources if p in p2k]
        args = list_command({k for _, k in plurals}, plurals, cats)
        lines.append(f"| {meta.get('name', r['spec_key'])} | `oc get {','.join(args)} -n entity-<tenant>` |")
    return "\n".join(lines)


def replace(path, begin, end, body):
    text = path.read_text(encoding="utf-8")
    if begin not in text or end not in text:
        sys.exit(f"{path.relative_to(ROOT)}: markers {begin} / {end} missing")
    head, rest = text.split(begin, 1)
    _, tail = rest.split(end, 1)
    return text, f"{head}{begin}\n{body}\n{end}{tail}"


def main():
    check = "--check" in sys.argv[1:]
    stale = False
    for path, begin, end, body, what in ((GUIDE, BEGIN, END, table(), "role table"),
                                         (GLOSSARY, LIST_BEGIN, LIST_END, list_tables(), "list commands")):
        text, new = replace(path, begin, end, body)
        if check:
            if new != text:
                print(f"{path.relative_to(ROOT)}: {what} is not current; run scripts/gen-role-table.py")
                stale = True
            continue
        path.write_text(new, encoding="utf-8")
        print(f"wrote the {what} of {path.relative_to(ROOT)}")
    if stale:
        sys.exit(1)


if __name__ == "__main__":
    main()
