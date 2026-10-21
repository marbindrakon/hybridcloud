#!/usr/bin/env python3
"""Content lint: banned words, CRD description rules, lab knowledge and generated blocks.

Reads docs/content/terms.yaml (the content contract) and checks
  a) the CRD descriptions in operator/config/crd/bases (banned words, structural rules, the
     kind-description convention, undocumented spec fields),
  b) the UI string catalogues (en.json, fr.json values; console-extensions.json names, labels and
     descriptions), ratcheted by docs/content/lint-baseline.txt,
  c) the README, docs/ and samples/ (banned words outside code; tenant words in docs/tenant/),
  d) lab knowledge in CRDs, samples, README and docs/ (the patterns of
     scripts/check-lab-literals.py plus the gitignored scripts/scrub-patterns.txt when present),
  e) one terms.yaml entry per CRD kind and the other way round,
  f) generated blocks are current (the role table of docs/platform/onboard-a-tenant.md).

Output: `file:line: rule: excerpt`. Exit 1 on any error. The lint reads no Secret.

usage:
  check-content.py                 run every check
  check-content.py --self-test     check the rules against scripts/tests/check-content/
  check-content.py --write-baseline  record today's UI hits as accepted (the baseline only shrinks)
"""
import argparse
import ast
import fnmatch
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TERMS = ROOT / "docs/content/terms.yaml"
BASELINE = ROOT / "docs/content/lint-baseline.txt"
BASES = ROOT / "operator/config/crd/bases"
FIXTURES = ROOT / "scripts/tests/check-content/fixtures.yaml"
UI_FILES = [
    "ui/packages/shared/src/i18n/locales/en.json",
    "ui/packages/shared/src/i18n/locales/fr.json",
    "ui/packages/admin-console-plugin/console-extensions.json",
    "ui/packages/tenant-console-plugin/console-extensions.json",
]
DOC_GLOBS = ["README.md", "docs/**/*.md", "samples/**/*.md", "samples/**/*.yaml",
             "architecture/README.md", "architecture/docs/architecture.md"]
TENANT_DOCS = ["docs/tenant/**"]
# Kinds whose every spec field must be described (the others: warnings).
STRICT_KINDS = {"Entity", "Rbac", "RbacConfig", "Team", "Project", "Assignment",
                "PlatformOpenshift", "CloudOSO", "Persona", "TransportLink"}
STATE_PREFIX = {"not-implemented": "Not implemented.", "deprecated": "Deprecated.",
                "experimental": "Experimental."}
FIELD_PATH = re.compile(r"\b(spec|status|metadata)(\.[A-Za-z0-9_\[\]]+)+")
GENERATORS = [["scripts/gen-role-table.py", "--check"]]


class Rule:
    def __init__(self, entry, rid):
        self.id = rid
        self.rx = re.compile(entry["match"])
        self.use = entry.get("use", "")
        self.where = set(entry.get("where", ["crd", "ui", "docs"]))
        self.audience = entry.get("audience", "any")
        self.exempt = entry.get("except", [])

    def applies(self, surface, tenant, path=""):
        if surface not in self.where:
            return False
        if self.audience == "tenant" and not tenant:
            return False
        return not any(fnmatch.fnmatch(path, g) for g in self.exempt)


def load_terms():
    terms = yaml.safe_load(TERMS.read_text(encoding="utf-8"))
    rules = [Rule(e, f"banned:{e['match'][:24]}") for e in terms.get("banned", [])]
    crd_rules = [Rule(dict(e, where=["crd"]), "crd-rule") for e in terms.get("crd_rules", [])]
    return terms, rules, crd_rules


def lab_patterns():
    """The lab patterns of check-lab-literals.py (read, not imported) and scrub-patterns.txt."""
    out = []
    src = ROOT / "scripts/check-lab-literals.py"
    if src.exists():
        for node in ast.walk(ast.parse(src.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Assign) and any(
                    getattr(t, "id", "") == "PATTERNS" for t in node.targets):
                for k, v in zip(node.value.keys, node.value.values):
                    if isinstance(v, ast.Call) and v.args and isinstance(v.args[0], ast.Constant):
                        out.append((k.value, re.compile(v.args[0].value)))
    scrub = ROOT / "scripts/scrub-patterns.txt"
    if scrub.exists():
        for line in scrub.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#") and "=" in line:
                old = line.split("=", 1)[0].strip()
                if len(old) >= 4:
                    out.append(("lab string", re.compile(re.escape(old))))
    return out


class Report:
    def __init__(self):
        self.errors = []
        self.warnings = []
        self.ui_hits = Counter()  # ratcheted hits: UI strings and CRD text other streams own
        self.use = {}

    def error(self, where, rule, text):
        self.errors.append(f"{where}: {rule}: {_excerpt(text)}")

    def warn(self, where, rule, text):
        self.warnings.append(f"{where}: {rule}: {_excerpt(text)}")


def _excerpt(text, n=110):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 3] + "..."


# ------------------------------------------------------------------ a) CRDs and e) kinds


def _crd_docs():
    for path in sorted(BASES.glob("crd-*.yaml")):
        if path.name == "crd.yaml":
            continue
        text = "\n".join("#" if ln.lstrip().startswith("{{") else ln
                         for ln in path.read_text(encoding="utf-8").split("\n"))
        for doc in yaml.safe_load_all(text):
            if doc and doc.get("kind") == "CustomResourceDefinition":
                yield path, doc


def _walk(node, path="."):
    yield path, node
    for name, child in (node.get("properties") or {}).items():
        if isinstance(child, dict):
            yield from _walk(child, name if path == "." else f"{path}.{name}")
    items = node.get("items")
    if isinstance(items, dict):
        yield from _walk(items, (path if path != "." else "") + "[]")


def check_crds(rep, terms, rules, crd_rules, labs):
    kinds = terms["kinds"]
    seen = set()
    for path, doc in _crd_docs():
        kind = doc["spec"]["names"]["kind"]
        seen.add(kind)
        rel = path.relative_to(ROOT)
        entry = kinds.get(kind)
        if entry is None:
            rep.error(rel, "terms", f"kind {kind} has no entry in docs/content/terms.yaml")
            continue
        tenant = entry.get("audience") == "tenant"
        for v in doc["spec"]["versions"]:
            schema = (v.get("schema") or {}).get("openAPIV3Schema") or {}
            top = (schema.get("description") or "").strip()
            if not top:
                rep.error(f"{rel} {kind}", "kind-description", "empty kind description")
            prefix = STATE_PREFIX.get(entry.get("state"))
            if prefix and not top.startswith(prefix):
                rep.error(f"{rel} {kind}", "kind-description", f"must start with {prefix!r}")
            if not prefix and entry.get("analogy"):
                first = re.split(r"(?<=[.!?])\s", top, maxsplit=1)[0]
                if "comparable to" not in first:
                    rep.error(f"{rel} {kind}", "kind-description",
                              "first sentence must end with 'comparable to ...'")
            for p, node in _walk(schema):
                d = node.get("description")
                if d:
                    d = FIELD_PATH.sub("<field>", d)  # field names are API, not words
                    where = f"{rel} {kind} {p}"
                    for r in rules + crd_rules:
                        if r.applies("crd", tenant, str(rel)):
                            for m in r.rx.finditer(d):
                                key = f"{rel}\t{kind} {p}\t{m.group(0)}"
                                rep.ui_hits[key] += 1
                                rep.use[key] = r.use
                    for what, rx in labs:
                        m = rx.search(d)
                        if m:
                            rep.error(where, f"lab knowledge ({what}) '{m.group(0)}'", d)
                elif p.startswith("spec") and not p.endswith("[]"):
                    msg = (f"{rel} {kind} {p}", "undocumented", "spec field has no description")
                    (rep.error if kind in STRICT_KINDS else rep.warn)(*msg)
    for kind in sorted(set(kinds) - seen):
        if kinds[kind].get("planned"):
            # entry written ahead of the CRD (another change adds the kind); checked once it lands
            rep.warn(TERMS.relative_to(ROOT), "terms", f"{kind}: planned kind, no CRD yet")
            continue
        rep.error(TERMS.relative_to(ROOT), "terms", f"{kind} is not a kind in {BASES.name}")


# ------------------------------------------------------------------ b) UI strings


def _ui_strings(rel):
    data = json.loads((ROOT / rel).read_text(encoding="utf-8"))
    if rel.endswith("console-extensions.json"):
        def walk(o, path):
            if isinstance(o, dict):
                for k, v in o.items():
                    if k in ("name", "label", "description", "title") and isinstance(v, str):
                        yield f"{path}.{k}", v
                    else:
                        yield from walk(v, f"{path}.{k}")
            elif isinstance(o, list):
                for i, v in enumerate(o):
                    yield from walk(v, f"{path}[{i}]")
        yield from walk(data, "")
    else:
        def walk(o, path):
            if isinstance(o, dict):
                for k, v in o.items():
                    yield from walk(v, f"{path}.{k}" if path else k)
            elif isinstance(o, str):
                yield path, o
        yield from walk(data, "")


def check_ui(rep, rules):
    for rel in UI_FILES:
        if not (ROOT / rel).exists():
            continue
        for key, value in _ui_strings(rel):
            for r in rules:
                if r.applies("ui", True, rel):
                    for m in r.rx.finditer(value):
                        hit = f"{rel}\t{key}\t{m.group(0)}"
                        rep.ui_hits[hit] += 1
                        rep.use[hit] = r.use


def check_baseline(rep):
    base = Counter()
    if BASELINE.exists():
        for line in BASELINE.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#"):
                *key, n = line.split("\t")
                base["\t".join(key)] = int(n)
    for key, n in sorted(rep.ui_hits.items()):
        if n > base.get(key, 0):
            f, k, word = key.split("\t")
            rep.error(f"{f} {k}", f"banned '{word}'", f"use: {rep.use.get(key, '?')}")
    for key, n in sorted(base.items()):
        if rep.ui_hits.get(key, 0) < n:
            f, k, word = key.split("\t")
            rep.error(BASELINE.relative_to(ROOT), "stale baseline",
                      f"{f} {k} '{word}' is fixed; remove the line (--write-baseline)")


def write_baseline(rep):
    lines = ["# Accepted banned-word hits in UI strings and CRD text, written by check-content.py "
             "--write-baseline.",
             "# A new hit fails the lint; a fixed hit must be removed from this file, so the "
             "list only shrinks.",
             "# file<TAB>key<TAB>word<TAB>count"]
    lines += [f"{k}\t{n}" for k, n in sorted(rep.ui_hits.items())]
    BASELINE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {BASELINE.relative_to(ROOT)} ({len(rep.ui_hits)} entries)")


# ------------------------------------------------------------------ c) docs, d) lab knowledge


def _doc_files():
    seen = set()
    for g in DOC_GLOBS:
        for p in sorted(ROOT.glob(g)):
            if p.is_file() and p not in seen:
                seen.add(p)
                yield p


def _prose_lines(text, markdown):
    """(lineno, line with code removed, raw line, inside retired table)."""
    fence = False
    retired = False
    for n, raw in enumerate(text.splitlines(), 1):
        s = raw.strip()
        if "<!-- retired:begin -->" in s:
            retired = True
        if "<!-- retired:end -->" in s:
            retired = False
        if markdown and s.startswith("```"):
            fence = not fence
            yield n, "", raw, retired
            continue
        if not markdown:
            # YAML: only comments are prose
            i = raw.find("#")
            yield n, raw[i + 1:] if i >= 0 else "", raw, retired
            continue
        prose = "" if fence else re.sub(r"`[^`]*`", "", raw)
        prose = re.sub(r"\]\([^)]*\)", "]", prose)  # link targets are paths
        prose = re.sub(r"\[[^\]]*\.(md|yaml|yml)\]", "[]", prose)  # so are file-name link texts
        yield n, prose, raw, retired


def check_docs(rep, rules, labs):
    for path in _doc_files():
        rel = str(path.relative_to(ROOT))
        tenant = any(fnmatch.fnmatch(rel, g) for g in TENANT_DOCS)
        md = path.suffix == ".md"
        for n, prose, raw, retired in _prose_lines(path.read_text(encoding="utf-8"), md):
            if prose and not retired:
                for r in rules:
                    if r.applies("docs", tenant, rel):
                        m = r.rx.search(prose)
                        if m:
                            rep.error(f"{rel}:{n}", f"banned '{m.group(0)}' (use: {r.use})", raw)
            for what, rx in labs:
                m = rx.search(raw)
                if m:
                    rep.error(f"{rel}:{n}", f"lab knowledge ({what}) '{m.group(0)}'", raw)


# ------------------------------------------------------------------ f) generated blocks


def check_generated(rep):
    for cmd in GENERATORS:
        script = ROOT / cmd[0]
        if not script.exists():
            continue
        r = subprocess.run([sys.executable, "-I", str(script), *cmd[1:]], capture_output=True,
                           text=True)
        if r.returncode:
            rep.error(cmd[0], "generated block out of date",
                      (r.stdout + r.stderr).strip() or "run it without --check")


# ------------------------------------------------------------------ self-test


def self_test(rules, crd_rules):
    fx = yaml.safe_load(FIXTURES.read_text(encoding="utf-8"))
    failed = 0
    for case in fx["cases"]:
        surface = case["surface"]
        tenant = case.get("tenant", False)
        pool = crd_rules + rules if surface == "crd" else rules
        hits = sorted({m.group(0) for r in pool if r.applies(surface, tenant)
                       for m in [r.rx.search(case["text"])] if m})
        want = sorted(case.get("hits", []))
        if hits != want:
            failed += 1
            print(f"FAIL {surface}{' tenant' if tenant else ''}: {case['text']!r}: "
                  f"hits {hits}, expected {want}")
    print(f"self-test: {len(fx['cases']) - failed}/{len(fx['cases'])} cases pass")
    return 1 if failed else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--write-baseline", action="store_true")
    ap.add_argument("--warnings", action="store_true", help="print warnings, not only a count")
    args = ap.parse_args()
    terms, rules, crd_rules = load_terms()
    if args.self_test:
        sys.exit(self_test(rules, crd_rules))
    rep = Report()
    labs = lab_patterns()
    check_ui(rep, rules)
    check_crds(rep, terms, rules, crd_rules, labs)
    if args.write_baseline:
        write_baseline(rep)
        return
    check_baseline(rep)
    check_docs(rep, rules, labs)
    check_generated(rep)
    for e in rep.errors:
        print(e)
    if args.warnings:
        for w in rep.warnings:
            print("warning: " + w)
    lab_note = "" if (ROOT / "scripts/scrub-patterns.txt").exists() else \
        " (scripts/scrub-patterns.txt absent: generic lab patterns only)"
    print(f"check-content: {len(rep.errors)} error(s), {len(rep.warnings)} warning(s), "
          f"{sum(rep.ui_hits.values())} banned-word hit(s) in UI strings and CRD text checked "
          f"against the baseline{lab_note}")
    sys.exit(1 if rep.errors else 0)


if __name__ == "__main__":
    main()
