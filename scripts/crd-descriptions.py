#!/usr/bin/env python3
"""Read, edit and verify the description text of the hybridsovereign.redhat CRDs.

The per-kind sources operator/config/crd/bases/crd-<kind>.yaml are the only place CRD text is
edited. operator/primary/helm/templates/crds.yaml is generated from them by scripts/gen-crds.py;
`set` and `fill` regenerate it after editing. Edits are line-level, so the rest of each source
(layout, comments) stays byte-identical.

Paths name schema nodes below openAPIV3Schema: "." is the kind itself, "spec.cloudRef.name" a
field, "status.edaJobs[]" the items of an array, "status.edaJobs[].name" a field of those items.

usage:
  crd-descriptions.py dump KIND [--missing]       list path -> description (per-kind source)
  crd-descriptions.py set PATCH.yaml              apply {Kind: {path: text}}, regenerate crds.yaml
  crd-descriptions.py fill [--kinds K,K]          add the common descriptions that are missing
  crd-descriptions.py lockstep                    crds.yaml is the generated form of the sources
  crd-descriptions.py diff BASE_REF               nothing but descriptions changed since BASE_REF

Exit status 1 when a check fails or an edit cannot be verified.
"""
import argparse
import copy
import subprocess
import sys
import textwrap
from pathlib import Path

import yaml
import ruamel.yaml

ROOT = Path(__file__).resolve().parents[1]
BASES = ROOT / "operator/config/crd/bases"
CHART_CRDS = ROOT / "operator/primary/helm/templates/crds.yaml"
GROUP = "hybridsovereign.redhat"
WIDTH = 100

# Descriptions of the status and metadata leaves most kinds share. `fill` writes one only where a
# node has no description yet, so it is safe to re-run after a kind gains new status fields.
COMMON = {
    "spec": "Desired state of this object.",
    "spec.description": "What this object is for, in a few words.",
    "status": "Observed state, written by the platform. Do not edit.",
    "status.status": "Short state word written by the platform's automation.",
    "status.ready": "True when the platform finished creating or updating this object.",
    "status.message": "What happened last, in plain words. Read it when the object is not ready.",
    "status.lastReconciledAt": "When the platform last finished processing this object (RFC 3339).",
    "status.observedGeneration": (
        "The metadata.generation the status describes. When it is lower than metadata.generation, "
        "the latest change has not been processed yet."
    ),
    "status.deletionComplete": "True when everything this object created has been removed.",
    "status.edaJobs": "Recent automation runs for this object. Platform detail, for the platform team.",
    "status.edaJobs[]": "One automation run for this object.",
    "status.edaJobs[].name": "Name of the automation job template.",
    "status.edaJobs[].id": "Run identifier in the automation controller.",
    "status.edaJobs[].jobId": "Run identifier in the automation controller.",
    "status.edaJobs[].status": "Result of the run.",
    "status.edaJobs[].url": "Link to the run in the automation controller.",
    "status.edaJobs[].timestamp": "When the run was recorded.",
    "status.edaJobs[].startedAt": "When the run started.",
    "status.edaJobs[].finishedAt": "When the run finished.",
    "status.conditions": (
        "Standard conditions. Ready says whether the object is usable; the others say what the "
        "platform is waiting for or what failed."
    ),
    "status.conditions[]": "One condition.",
    "status.conditions[].type": "Condition type, for example Ready.",
    "status.conditions[].status": "True, False or Unknown.",
    "status.conditions[].reason": "One-word reason in CamelCase, stable enough to match on.",
    "status.conditions[].message": "What the reason means for this object, in plain words.",
    "status.conditions[].lastTransitionTime": "When the condition last changed status.",
    "status.conditions[].observedGeneration": "The metadata.generation the condition describes.",
}

# Descriptions by the last segments of a path, for references (`fabricRef.name`, ...).
COMMON_SUFFIX = {
    "Ref.name": "Name of the referenced object.",
}


def common_text(path):
    if path in COMMON:
        return COMMON[path]
    last = path.rsplit(".", 2)[-2:]
    if len(last) == 2 and last[0].endswith("Ref") and last[1] == "name":
        return COMMON_SUFFIX["Ref.name"]
    return None


# ---------------------------------------------------------------- loading


def _rt_yaml():
    y = ruamel.yaml.YAML()
    y.allow_duplicate_keys = True
    return y


def _helm_masked(text):
    """Replace Helm control lines (and multi-line Helm comments) with YAML comments so the file
    parses, keeping line numbers."""
    lines = text.split("\n")
    out = []
    in_tpl = False
    for ln in lines:
        if in_tpl:
            out.append("#")
            if "}}" in ln:
                in_tpl = False
            continue
        if ln.lstrip().startswith("{{"):
            out.append("#")
            if "}}" not in ln:
                in_tpl = True
            continue
        out.append(ln)
    return "\n".join(out), lines


def kind_files():
    return sorted(p for p in BASES.glob("crd-*.yaml"))


def load_rt(path):
    """Round-trip documents of one file: list of (kind, doc). Line numbers are file-absolute."""
    masked, _ = _helm_masked(path.read_text(encoding="utf-8"))
    out = []
    for doc in _rt_yaml().load_all(masked):
        if not doc or doc.get("kind") != "CustomResourceDefinition":
            continue
        if doc["spec"].get("group") != GROUP:
            continue
        out.append((doc["spec"]["names"]["kind"], doc))
    return out


def load_plain(text):
    masked, _ = _helm_masked(text)
    out = {}
    for doc in yaml.safe_load_all(masked):
        if not doc or doc.get("kind") != "CustomResourceDefinition":
            continue
        if doc["spec"].get("group") != GROUP:
            continue
        out.setdefault(doc["spec"]["names"]["kind"], []).append(doc)
    return out


def source_for(kind):
    """The per-kind file whose CRD is `kind` (crd.yaml, a stray copy, is skipped)."""
    for p in kind_files():
        if p.name == "crd.yaml":
            continue
        for k, _ in load_rt(p):
            if k == kind:
                return p
    raise SystemExit(f"no per-kind source for {kind}")


# ---------------------------------------------------------------- paths


def _schemas(doc):
    for v in doc["spec"]["versions"]:
        s = v.get("schema", {}).get("openAPIV3Schema")
        if s is not None:
            yield s


def resolve(schema, path):
    """Schema node for a path, or None."""
    node = schema
    if path in (".", ""):
        return node
    for seg in path.split("."):
        items = seg.endswith("[]")
        name = seg[:-2] if items else seg
        if name:
            props = node.get("properties") if hasattr(node, "get") else None
            if not props or name not in props:
                return None
            node = props[name]
        if items:
            node = node.get("items") if hasattr(node, "get") else None
            if node is None:
                return None
    return node


def walk(node, path="."):
    """Yield (path, node) for every schema node with a type or properties."""
    yield path, node
    for name, child in (node.get("properties") or {}).items():
        p = name if path == "." else f"{path}.{name}"
        if isinstance(child, dict):
            yield from walk(child, p)
    items = node.get("items")
    if isinstance(items, dict):
        yield from walk(items, (path if path != "." else "") + "[]")


# ---------------------------------------------------------------- text edits


def render(indent, text):
    """`description:` lines for a text. Paragraphs are separated by a blank line."""
    pad = " " * indent
    paras = [p.strip() for p in text.strip().split("\n\n")]
    single = len(paras) == 1 and len(pad) + 13 + len(paras[0]) <= WIDTH
    if single and _plain_safe(paras[0]):
        return [f"{pad}description: {paras[0]}"]
    out = [f"{pad}description: >-"]
    for i, para in enumerate(paras):
        if i:
            out.append("")
        para = " ".join(para.split())
        out += [pad + "  " + ln for ln in textwrap.wrap(para, WIDTH - indent - 2,
                                                         break_long_words=False,
                                                         break_on_hyphens=False)]
    return out


def _plain_safe(s):
    """True when a one-line text needs no quoting as a plain YAML scalar."""
    if not s or s[0] in "!&*{}[]|>'\"%@`#,?:-" or ": " in s or " #" in s or s.endswith(":"):
        return False
    try:
        return yaml.safe_load(f"k: {s}") == {"k": s}
    except yaml.YAMLError:
        return False


def _block_end(lines, start, col):
    """First line after a `key:` at `col` that is not part of its value."""
    i = start + 1
    while i < len(lines):
        ln = lines[i]
        if ln.strip() and (len(ln) - len(ln.lstrip())) <= col:
            break
        i += 1
    while i - 1 > start and not lines[i - 1].strip():
        i -= 1
    return i


def edit_file(path, kind, changes, only_missing=False):
    """Apply {path: text} to every version of `kind` in `path`. Returns the number of edits."""
    text = path.read_text(encoding="utf-8")
    _, lines = _helm_masked(text)
    docs = [d for k, d in load_rt(path) if k == kind]
    if not docs:
        raise SystemExit(f"{path}: no CRD for {kind}")
    edits = []  # (start, end, new_lines, path)
    for doc in docs:
        for schema in _schemas(doc):
            for p, new in changes.items():
                node = resolve(schema, p)
                if node is None:
                    raise SystemExit(f"{path}: {kind} has no schema node {p}")
                if not hasattr(node, "lc") or not len(node):
                    print(f"{path}: {kind} {p}: flow or empty mapping, skipped", file=sys.stderr)
                    continue
                if "description" in node:
                    if only_missing:
                        continue
                    line, col = node.lc.key("description")
                    edits.append((line, _block_end(lines, line, col), render(col, new), p))
                else:
                    first = next(iter(node))
                    line, col = node.lc.key(first)
                    if lines[line][:col].strip():
                        print(f"{path}: {kind} {p}: mapping starts mid-line, skipped",
                              file=sys.stderr)
                        continue
                    edits.append((line, line, render(col, new), p))
    for start, end, new_lines, _ in sorted(edits, key=lambda e: e[0], reverse=True):
        lines[start:end] = new_lines
    if edits:
        path.write_text("\n".join(lines), encoding="utf-8")
    # verify with a different parser: every edited description reads back exactly
    done = {e[3] for e in edits}
    for doc in load_plain(path.read_text(encoding="utf-8")).get(kind, []):
        for schema in _schemas(doc):
            for p in done:
                node = resolve(schema, p)
                have = node.get("description") if node is not None else None
                want = "\n".join(" ".join(x.split()) for x in changes[p].strip().split("\n\n"))
                if have != want:
                    raise SystemExit(f"{path}: {kind} {p}: read back {have!r}, wanted {want!r}")
    return len(edits)


def regenerate():
    """Regenerate crds.yaml from the sources with the BE-FOUNDATIONS generator."""
    r = subprocess.run([sys.executable, "-I", str(ROOT / "scripts/gen-crds.py")])
    if r.returncode:
        raise SystemExit("gen-crds.py failed; crds.yaml not regenerated")


# ---------------------------------------------------------------- commands


def cmd_dump(args):
    path = source_for(args.kind)
    for k, doc in load_plain(path.read_text(encoding="utf-8")).items():
        if k != args.kind:
            continue
        for schema in _schemas(doc[0]):
            for p, node in walk(schema):
                if p in ("apiVersion", "kind", "metadata"):
                    continue
                d = node.get("description")
                if args.missing and d:
                    continue
                print(f"{p}\t{node.get('type', '')}\t{d or '-'}")


def cmd_set(args):
    patch = yaml.safe_load(Path(args.patch).read_text(encoding="utf-8"))
    total = 0
    for kind, changes in patch.items():
        path = source_for(kind)
        n = edit_file(path, kind, changes)
        total += n
        print(f"{path.relative_to(ROOT)}: {kind}: {n} description(s) set")
    print(f"{total} edit(s)")
    if total:
        regenerate()


def cmd_fill(args):
    kinds = args.kinds.split(",") if args.kinds else None
    total = 0
    for src in kind_files():
        if src.name == "crd.yaml":
            continue
        for kind, doc in load_rt(src):
            if kinds and kind not in kinds:
                continue
            todo = {}
            for schema in _schemas(doc):
                for p, node in walk(schema):
                    text = common_text(p)
                    if text and "description" not in node:
                        todo[p] = text
            if not todo:
                continue
            n = edit_file(src, kind, todo, only_missing=True)
            total += n
            print(f"{src.relative_to(ROOT)}: {kind}: {n} filled")
    print(f"{total} description(s) filled")
    if total:
        regenerate()


def _strip(obj):
    """A copy with every schema description removed."""
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items() if k != "description"}
    if isinstance(obj, list):
        return [_strip(v) for v in obj]
    return obj


def _descriptions(doc):
    out = {}
    for schema in _schemas(doc):
        for p, node in walk(schema):
            if "description" in node:
                out[p] = node["description"]
    return out


def _git_show(ref, rel):
    r = subprocess.run(["git", "-C", str(ROOT), "show", f"{ref}:{rel}"], capture_output=True,
                       text=True)
    return r.stdout if r.returncode == 0 else None


def cmd_lockstep(args):
    r = subprocess.run([sys.executable, "-I", str(ROOT / "scripts/gen-crds.py"), "--check"])
    chart = load_plain(CHART_CRDS.read_text(encoding="utf-8"))
    bad = 1 if r.returncode else 0
    for src in kind_files():
        if src.name == "crd.yaml":
            continue
        for kind, docs in load_plain(src.read_text(encoding="utf-8")).items():
            if kind not in chart:
                print(f"{src.name}: {kind} missing from crds.yaml")
                bad += 1
                continue
            a, b = _descriptions(docs[0]), _descriptions(chart[kind][0])
            diff = sorted(p for p in set(a) | set(b) if a.get(p) != b.get(p))
            for p in diff:
                print(f"{src.name}: {kind} {p}: description differs from crds.yaml")
            bad += len(diff)
            if _strip(docs[0]["spec"]) != _strip(chart[kind][0]["spec"]):
                print(f"{src.name}: {kind}: schema differs from crds.yaml (not a description)")
                bad += 1
    print("lockstep: " + ("ok" if not bad else f"{bad} difference(s)"))
    return 1 if bad else 0


def cmd_diff(args):
    bad = 0
    changed = 0
    # crds.yaml is generated from the sources (lockstep checks that), so the sources suffice.
    files = [p for p in kind_files() if p.name != "crd.yaml"]
    for path in files:
        rel = str(path.relative_to(ROOT))
        old_text = _git_show(args.base, rel)
        if old_text is None:
            print(f"{rel}: not in {args.base}, skipped")
            continue
        old, new = load_plain(old_text), load_plain(path.read_text(encoding="utf-8"))
        for kind in sorted(set(old) | set(new)):
            if kind not in old or kind not in new:
                print(f"{rel}: {kind} added or removed")
                bad += 1
                continue
            o, n = old[kind][0], new[kind][0]
            if _strip(o) != _strip(n):
                print(f"{rel}: {kind}: NON-DESCRIPTION CHANGE")
                bad += 1
            do, dn = _descriptions(o), _descriptions(n)
            for p in sorted(set(do) | set(dn)):
                if do.get(p) != dn.get(p):
                    changed += 1
                    if args.verbose:
                        print(f"{rel}: {kind} {p}:\n  - {do.get(p)!r}\n  + {dn.get(p)!r}")
    print(f"diff vs {args.base}: {changed} description change(s), "
          f"{bad} non-description change(s)")
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("dump")
    p.add_argument("kind")
    p.add_argument("--missing", action="store_true")
    p = sub.add_parser("set")
    p.add_argument("patch")
    p = sub.add_parser("fill")
    p.add_argument("--kinds")
    sub.add_parser("lockstep")
    p = sub.add_parser("diff")
    p.add_argument("base")
    p.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    rc = {"dump": cmd_dump, "set": cmd_set, "fill": cmd_fill, "lockstep": cmd_lockstep,
          "diff": cmd_diff}[args.cmd](args)
    sys.exit(rc or 0)


if __name__ == "__main__":
    main()
