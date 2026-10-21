#!/usr/bin/env python3
"""Write the tenant role table of docs/platform/onboard-a-tenant.md from the role definitions.

Reads the roles the platform creates in a tenant namespace (entity_named_roles in
eda/rulebooks/roles/entity_provision/vars/main.yml), their display names (roles: in
docs/content/terms.yaml) and the kinds' display terms, and replaces the text between
<!-- roles:begin --> and <!-- roles:end --> in the guide. Deprecated keys are left out.

usage: gen-role-table.py [--check]     --check: exit 1 when the guide is not current
"""
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLES = ROOT / "eda/rulebooks/roles/entity_provision/vars/main.yml"
TERMS = ROOT / "docs/content/terms.yaml"
GUIDE = ROOT / "docs/platform/onboard-a-tenant.md"
BEGIN, END = "<!-- roles:begin -->", "<!-- roles:end -->"


def plural_to_kind():
    out = {}
    for p in (ROOT / "operator/config/crd/bases").glob("crd-*.yaml"):
        text = "\n".join("#" if ln.lstrip().startswith("{{") else ln
                         for ln in p.read_text(encoding="utf-8").split("\n"))
        for doc in yaml.safe_load_all(text):
            if doc and doc.get("kind") == "CustomResourceDefinition":
                n = doc["spec"]["names"]
                out[n["plural"]] = n["kind"]
    return out


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


def main():
    check = "--check" in sys.argv[1:]
    text = GUIDE.read_text(encoding="utf-8")
    if BEGIN not in text or END not in text:
        sys.exit(f"{GUIDE.relative_to(ROOT)}: markers {BEGIN} / {END} missing")
    head, rest = text.split(BEGIN, 1)
    _, tail = rest.split(END, 1)
    new = f"{head}{BEGIN}\n{table()}\n{END}{tail}"
    if check:
        if new != text:
            print(f"{GUIDE.relative_to(ROOT)}: role table is not current; run "
                  f"scripts/gen-role-table.py")
            sys.exit(1)
        return
    GUIDE.write_text(new, encoding="utf-8")
    print(f"wrote the role table of {GUIDE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
