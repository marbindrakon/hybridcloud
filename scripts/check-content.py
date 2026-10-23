#!/usr/bin/env python3
"""Content lint: banned words, CRD description rules, lab knowledge and generated blocks.

Reads docs/content/terms.yaml (the content contract) and checks
  a) the CRD descriptions in operator/config/crd/bases (banned words, structural rules, the
     kind-description convention, undocumented spec fields),
  b) the UI string catalogues (en.json, fr.json values; console-extensions.json names, labels and
     descriptions) and the display strings hard-coded in ui/packages/*/src/**/*.ts(x) (JSX text,
     display props such as title= / label= / aria-label= with the literal branches of a ternary
     in them, display keys of object literals such
     as title: / label: / defaultValue:), ratcheted by docs/content/lint-baseline.txt,
  c) the README, docs/ and samples/ (banned words outside code; tenant words in docs/tenant/),
  d) lab knowledge in CRDs, samples, README and docs/ (the patterns of
     scripts/check-lab-literals.py plus the gitignored scripts/scrub-patterns.txt when present),
  e) one terms.yaml entry per CRD kind and the other way round,
  f) generated blocks are current (the role table of docs/platform/onboard-a-tenant.md, the
     terms table of docs/tenant/glossary.md, the reasons table of docs/tenant/status-reasons.md),
  g) runtime labels: the catalogue kind terms (kinds, kindsOne, kindsDense in en.json) match
     terms.yaml for every kind a tenant meets; the UI sources pass no raw kind into a display
     string, and no display string of a .tsx source (JSX text, label="..." and the other display
     props, literal ternary branches such as label={a ? 'X' : 'Y'}, display keys) shows a kind
     word (the `rendered` words and every CamelCase kind name); and the labels the vocabulary module builds at runtime (ui/scripts/runtime-strings.mjs:
     kind terms, object references, field labels of every CRD field, the templates that take a
     kind) show no banned or `rendered` word. Needs node and ui/node_modules (npm ci in ui/);
     without them this part is skipped with a warning.

Output: `file:line: rule: excerpt`. Exit 1 on any error. The lint reads no Secret.

usage:
  check-content.py                 run every check
  check-content.py --self-test     check the rules against scripts/tests/check-content/
  check-content.py --write-baseline  record today's UI hits as accepted (the baseline only shrinks)
  check-content.py --write-glossary  rewrite the generated terms table of docs/tenant/glossary.md
  check-content.py --rendered PATH...  check saved page texts (a GUI crawl of tenant pages, .txt
                                   files or directories of them) for the `rendered` words
"""
import argparse
import ast
import fnmatch
import json
import re
import shutil
import subprocess
import sys
import tempfile
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
# Hard-coded display strings in the UI sources (b); tests and declarations are not shown to users.
UI_SRC_GLOBS = ["ui/packages/*/src/**/*.tsx", "ui/packages/*/src/**/*.ts"]
UI_SRC_SKIP = ["*.test.ts", "*.test.tsx", "*.d.ts", "*/node_modules/*", "*/dist/*"]
DISPLAY_PROPS = ("title|label|aria-label|placeholder|description|subtitle|titleText|toggleText|alt|"
                 "helperText|emptyMessage|screenReaderText|tooltip|caption|heading|hint|summary|"
                 "labelOn|labelOff|noResultsText|textLabel")
DISPLAY_KEYS = ("title|label|description|subtitle|caption|heading|placeholder|hint|message|summary|"
                "tooltip|defaultValue|helpText|emptyMessage|text")
# JSX text between tags: no code punctuation, at least one letter, not part of "=>" / "->".
JSX_TEXT = re.compile(r"(?<![=\-])>([^<>{}`;=&|?]*[A-Za-z][^<>{}`;=&|?]*)(?=<|\{)")
# title="..." / title={'...'} / title={`...`}
JSX_PROP = re.compile(r"(?<![\w-])(?:" + DISPLAY_PROPS + r")=(?:\"([^\"\n]*)\"|\{\s*'([^'\n]*)'\s*\}|"
                      r"\{\s*\"([^\"\n]*)\"\s*\}|\{\s*`([^`]*)`\s*\})")
# title={cond ? 'A' : other ? "B" : `C`}: the literal branches of a ternary.
JSX_PROP_EXPR = re.compile(r"(?<![\w-])(?:" + DISPLAY_PROPS + r")=\{([^{}\n]*\?[^{}\n]*)\}")
TERNARY_LIT = re.compile(r"(?:^|[?:])\s*(?:'([^'\n]*)'|\"([^\"\n]*)\"|`([^`\n]*)`)")
# Code a JSX-text match picked up between generic type arguments: f<T>(items, 'Kind'), ...
CODE_TEXT = re.compile(r"[(,]\s*['\"]|['\"]\s*[),]")
# { title: '...' } / defaultValue: `...`
OBJ_KEY = re.compile(r"(?<![\w.])(?:" + DISPLAY_KEYS + r")\s*:\s*(?:'([^'\n]*)'|\"([^\"\n]*)\"|`([^`]*)`)")
# Interpolations and the translation-key look of a literal are not words.
TEMPLATE_EXPR = re.compile(r"\$\{[^}]*\}")
I18N_KEY = re.compile(r"^[a-zA-Z][\w-]*(\.[\w-]+)+$")

DOC_GLOBS = ["README.md", "docs/**/*.md", "samples/**/*.md", "samples/**/*.yaml",
             "architecture/README.md", "architecture/docs/architecture.md",
             "architecture/docs/tutorial/09-hybrid-networks.md"]
TENANT_DOCS = ["docs/tenant/**"]
# Every spec field of every kind must be described (an undescribed field is an error).
STATE_PREFIX = {"not-implemented": "Not implemented.", "deprecated": "Deprecated.",
                "experimental": "Experimental."}
FIELD_PATH = re.compile(r"\b(spec|status|metadata)(\.[A-Za-z0-9_\[\]]+)+")
GENERATORS = [["scripts/gen-role-table.py", "--check"], ["scripts/gen-reason-table.py", "--check"]]
GLOSSARY = ROOT / "docs/tenant/glossary.md"
GLOSSARY_BEGIN, GLOSSARY_END = "<!-- terms:begin -->", "<!-- terms:end -->"
CLOUDS = [("aws", "AWS"), ("gcp", "Google Cloud"), ("azure", "Azure"), ("openstack", "OpenStack"),
          ("openshift", "OpenShift")]
# Platform kinds a tenant meets (the rest of the platform kinds are not in the tenant glossary).
GLOSSARY_PLATFORM_KINDS = ("Entity", "CloudInfrastructure")
EN_CATALOGUE = ROOT / "ui/packages/shared/src/i18n/locales/en.json"
RUNTIME_STRINGS = ROOT / "ui/scripts/runtime-strings.mjs"
# Text on a rendered page that is not prose: URLs and paths, namespace names, `oc get` command lines.
RENDERED_NOT_TEXT = [re.compile(r"https?://\S+|(?<![\w/])/[\w/?=&%.-]+"), re.compile(r"\bentity-[a-z0-9-]+"),
                     re.compile(r"\boc get \S+( \S+)?")]
# A raw kind in a display string: t('x', { kind }) / t('x', { kind: item.kind }), and JSX
# text {item.kind}. kindTerm() / kindTermInText() / objectLabel() of i18n/vocabulary give the term.
T_CALL = re.compile(r"\bt\(\s*['\"`][^'\"`]+['\"`]\s*,\s*\{([^{}]*)\}")
RAW_KIND_OPT = re.compile(r"(?:^|[,{\s])kind\s*(?:,|$)|\bkind\s*:\s*(?:[\w?]+\.)*kind\s*(?:,|$)")
RAW_KIND_JSX = re.compile(r"(?<![=\w$])\{\s*[\w?]+(?:\.[\w?]+)*\.kind\s*\}")


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
        self.runtime_checked = False

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
                    rep.error(f"{rel} {kind} {p}", "undocumented", "spec field has no description")
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


def _strip_comments(text):
    """The source without // and /* */ comments; strings and template literals are kept."""
    out, i, n = [], 0, len(text)
    quote = None
    while i < n:
        c = text[i]
        if quote:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "'\"`":
            quote = c
            out.append(c)
            i += 1
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            seg = text[i:n if j < 0 else j + 2]
            out.append("\n" * seg.count("\n"))  # keep line numbers
            i = n if j < 0 else j + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def ui_source_strings(text):
    """(line, display string) of a .ts/.tsx source: JSX text, display props, display keys."""
    src = _strip_comments(text)
    found = []
    for rx in (JSX_TEXT, JSX_PROP, OBJ_KEY):
        for m in rx.finditer(src):
            val = next((g for g in m.groups() if g is not None), "")
            val = " ".join(TEMPLATE_EXPR.sub(" ", val).split())
            if not val or not re.search(r"[A-Za-z]", val) or I18N_KEY.match(val):
                continue
            if rx is JSX_TEXT and (val.count("(") != val.count(")") or CODE_TEXT.search(val)):
                continue  # code between two comparisons or type arguments, not text
            found.append((src.count("\n", 0, m.start()) + 1, val))
    for m in JSX_PROP_EXPR.finditer(src):
        expr = m.group(1)
        while True:  # arguments of a call are not display text: kindTerm(t, a ? 'X' : 'Y')
            flat = re.sub(r"\([^()]*\)", "()", expr)
            if flat == expr:
                break
            expr = flat
        for lit in TERNARY_LIT.finditer(expr):
            val = next((g for g in lit.groups() if g is not None), "")
            val = " ".join(TEMPLATE_EXPR.sub(" ", val).split())
            if val and re.search(r"[A-Za-z]", val) and not I18N_KEY.match(val):
                found.append((src.count("\n", 0, m.start()) + 1, val))
    return sorted(found)


def _ui_source_files():
    seen = set()
    for g in UI_SRC_GLOBS:
        for p in sorted(ROOT.glob(g)):
            rel = str(p.relative_to(ROOT))
            if p in seen or not p.is_file() or any(fnmatch.fnmatch(rel, x) for x in UI_SRC_SKIP):
                continue
            seen.add(p)
            yield p, rel


def check_ui_sources(rep, rules):
    """Banned words in the display strings of the UI sources; a hit's key is its text, not its
    line, so moving code does not churn the baseline."""
    for path, rel in _ui_source_files():
        for _line, value in ui_source_strings(path.read_text(encoding="utf-8")):
            for r in rules:
                if r.applies("ui", True, rel):
                    for m in r.rx.finditer(value):
                        hit = f"{rel}\t{_excerpt(value, 60)}\t{m.group(0)}"
                        rep.ui_hits[hit] += 1
                        rep.use[hit] = r.use


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


def _crd_names():
    out = {}
    for _, doc in _crd_docs():
        n = doc["spec"]["names"]
        out[n["kind"]] = n
    return out


def glossary_table(terms):
    """The terms table of the glossary: one row per term a tenant meets, from terms.yaml (term,
    analogy, note) and the CRDs (plural and short names for `oc get`)."""
    names = _crd_names()
    rows = ["| Term | In the API (`oc get ...`) | Comparable to | Note |", "|---|---|---|---|"]
    for kind, e in terms["kinds"].items():
        if e.get("state") in ("deprecated", "not-implemented"):
            continue
        if e.get("audience") != "tenant" and kind not in GLOSSARY_PLATFORM_KINDS:
            continue
        n = names.get(kind)
        if n:
            api = ", ".join(f"`{x}`" for x in [n["plural"], *(n.get("shortNames") or [])])
            api = f"`{kind}`: {api}"
        else:
            api = f"`{kind}` (available with this release)"
        like = "; ".join(f"{label}: {e['analogy'][key]}" for key, label in CLOUDS
                         if key in (e.get("analogy") or {})) or "-"
        note = " ".join(str(e.get("note", "")).split())
        if e.get("state") == "experimental":
            note = ("Experimental. " + note).strip()
        if e.get("dense"):
            note = (f"Short form: {e['dense']}. " + note).strip()
        term = e["term"]
        if e.get("first"):
            term = f"{term} ({e['first']})"
        rows.append(f"| {term} | {api} | {like} | {note or '-'} |")
    return "\n".join(rows)


def _glossary_render(terms):
    text = GLOSSARY.read_text(encoding="utf-8")
    if GLOSSARY_BEGIN not in text or GLOSSARY_END not in text:
        return text, None
    head, rest = text.split(GLOSSARY_BEGIN, 1)
    _, tail = rest.split(GLOSSARY_END, 1)
    return text, f"{head}{GLOSSARY_BEGIN}\n{glossary_table(terms)}\n{GLOSSARY_END}{tail}"


def check_glossary(rep, terms):
    if not GLOSSARY.exists():
        return
    text, new = _glossary_render(terms)
    rel = GLOSSARY.relative_to(ROOT)
    if new is None:
        rep.error(rel, "generated block", f"markers {GLOSSARY_BEGIN} / {GLOSSARY_END} missing")
    elif new != text:
        rep.error(rel, "generated block out of date", "run check-content.py --write-glossary")


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


# ------------------------------------------------------------------ g) runtime labels


def tenant_kinds(terms):
    """The kinds a tenant meets: tenant audience, plus the platform kinds of the tenant glossary."""
    return {k for k, e in terms["kinds"].items()
            if e.get("audience") == "tenant" or k in GLOSSARY_PLATFORM_KINDS}


def _sentence(term):
    return term[:1].upper() + term[1:]


def check_vocabulary(rep, terms):
    """kinds / kindsOne / kindsDense of the English catalogue follow terms.yaml."""
    if not EN_CATALOGUE.exists():
        return
    en = json.loads(EN_CATALOGUE.read_text(encoding="utf-8"))
    rel = EN_CATALOGUE.relative_to(ROOT)
    for kind in sorted(tenant_kinds(terms)):
        e = terms["kinds"][kind]
        if e.get("state") == "not-implemented":
            continue
        for sec, want in (("kindsOne", e["term"]), ("kinds", e["plural"])):
            got = (en.get(sec) or {}).get(kind)
            if got != _sentence(want):
                rep.error(f"{rel} {sec}.{kind}", "vocabulary",
                          f"is {got!r}, terms.yaml says {_sentence(want)!r}")
        dense = (en.get("kindsDense") or {}).get(kind)
        if e.get("dense") and dense != _sentence(e["dense"]):
            rep.error(f"{rel} kindsDense.{kind}", "vocabulary",
                      f"is {dense!r}, terms.yaml says {_sentence(e['dense'])!r}")
        if dense and not e.get("dense"):
            rep.error(f"{rel} kindsDense.{kind}", "vocabulary", "terms.yaml has no dense term")


def raw_kinds(text):
    """(offset, rule, code) of each raw kind in a display string of a UI source."""
    src = _strip_comments(text)
    out = []
    for m in T_CALL.finditer(src):
        opts = m.group(1)
        while True:  # arguments of nested calls are not options: kindTerm(t, kind)
            flat = re.sub(r"\([^()]*\)", "()", opts)
            if flat == opts:
                break
            opts = flat
        if RAW_KIND_OPT.search(opts):
            out.append((m.start(), "raw kind in a display string",
                        f"{m.group(0)} (use kindTerm / kindTermInText of i18n/vocabulary)"))
    for m in RAW_KIND_JSX.finditer(src):
        out.append((m.start(), "raw kind in page text",
                    f"{m.group(0)} (use kindTerm / objectLabel of i18n/vocabulary)"))
    return src, out


def _raw_kind_count(text):
    return len(raw_kinds(text)[1])


def source_kind_hits(text, rendered):
    """(line, word, string) of each kind word (CloudOSO, Rbac, Entity, ...) in a display string of a
    .tsx source: JSX text, display props (literals and literal ternary branches), display keys."""
    for line, value in ui_source_strings(text):
        for _pat, word in rendered_hits(value, rendered):
            yield line, word, value


def check_source_kinds(rep, terms):
    """No kind word in the display strings of the .tsx sources: a label names a kind through
    kindTerm() of i18n/vocabulary, which gives the terms.yaml words."""
    rendered = rendered_rules(terms)
    for path, rel in _ui_source_files():
        if not rel.endswith(".tsx"):
            continue
        for line, word, value in source_kind_hits(path.read_text(encoding="utf-8"), rendered):
            rep.error(f"{rel}:{line}", f"kind word '{word}' in a display string",
                      f"{_excerpt(value, 80)} (use kindTerm of i18n/vocabulary)")


def check_raw_kinds(rep):
    """No raw kind in display strings of the UI sources (the vocabulary module names kinds)."""
    for path, rel in _ui_source_files():
        if rel.endswith("i18n/vocabulary.ts"):
            continue
        src, hits = raw_kinds(path.read_text(encoding="utf-8"))
        for off, rule, code in hits:
            rep.error(f"{rel}:{src.count(chr(10), 0, off) + 1}", rule, code)


def rendered_rules(terms):
    pats = list(terms.get("rendered") or [])
    pats += [rf"\b{k}s?\b" for k in sorted(terms["kinds"]) if re.match(r"^[A-Z][a-z]+[A-Z]", k)]
    return [re.compile(p, re.I) for p in pats]


def rendered_hits(text, rendered):
    """(rendered pattern, matched text) on one line of page text."""
    for rx in RENDERED_NOT_TEXT:
        text = rx.sub(" ", text)
    for rx in rendered:
        m = rx.search(text)
        if m:
            yield rx.pattern, m.group(0)


def _crd_field_paths(kinds):
    """Every status field path of the given kinds, as the detail pages list them (spec fields are
    labelled from the catalogue). Fields described as "No longer set." and the helper keys are
    left out; the pages do not show them (ResourceDetail LEGACY_STATUS_KEYS)."""
    out = set()
    for _, doc in _crd_docs():
        if doc["spec"]["names"]["kind"] not in kinds:
            continue
        for v in doc["spec"]["versions"]:
            schema = (v.get("schema") or {}).get("openAPIV3Schema") or {}
            node = (schema.get("properties") or {}).get("status")
            if isinstance(node, dict):
                for p, child in _walk(node):
                    legacy = str(child.get("description", "")).startswith("No longer set") or \
                        "helper" in p.lower()  # ResourceDetail LEGACY_STATUS_KEYS
                    if p != "." and not legacy:
                        out.add(p.replace("[]", ""))
    return sorted(x for x in out if x)


def check_runtime(rep, rules, terms, tmpdir):
    """The labels the vocabulary module builds at runtime, for every kind a tenant meets."""
    node = shutil.which("node")
    if not node or not (ROOT / "ui/node_modules/esbuild").exists() or not RUNTIME_STRINGS.exists():
        rep.warn(RUNTIME_STRINGS.relative_to(ROOT), "runtime check skipped",
                 "needs node and ui/node_modules (npm ci in ui/)")
        return
    kinds = tenant_kinds(terms)
    paths_file = Path(tmpdir) / "paths.json"
    paths_file.write_text(json.dumps({"fieldPaths": _crd_field_paths(kinds)}), encoding="utf-8")
    r = subprocess.run([node, str(RUNTIME_STRINGS), str(paths_file)], capture_output=True, text=True)
    if r.returncode:
        rep.error(RUNTIME_STRINGS.relative_to(ROOT), "runtime check failed", r.stderr.strip()[-300:])
        return
    rendered = rendered_rules(terms)
    rel = RUNTIME_STRINGS.relative_to(ROOT)
    for e in json.loads(r.stdout):
        if e.get("kind") and e["kind"] not in kinds:
            continue  # a label of a platform kind: admin pages only
        text = e["text"]
        for rx, word in rendered_hits(text, rendered):
            rep.error(f"{rel} {e['source']}", f"runtime label shows '{word}'", text)
        for rl in rules:
            if rl.applies("ui", True, str(rel)):
                m = rl.rx.search(text)
                if m:
                    rep.error(f"{rel} {e['source']}", f"banned '{m.group(0)}' (use: {rl.use})", text)
    rep.runtime_checked = True


def check_rendered(terms, targets):
    """Saved page texts (a crawl of tenant pages) checked for the rendered words; exit code."""
    rendered = rendered_rules(terms)
    files = []
    for t in targets:
        p = Path(t)
        files += sorted(p.rglob("*.txt")) if p.is_dir() else [p]
    hits = 0
    for f in files:
        for n, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for _pat, word in rendered_hits(line, rendered):
                hits += 1
                print(f"{f}:{n}: rendered '{word}': {_excerpt(line)}")
    print(f"check-content --rendered: {len(files)} file(s), {hits} hit(s)")
    return 1 if hits else 0


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
    total = len(fx["cases"])
    for case in fx.get("sources", []):
        total += 1
        got = [v for _, v in ui_source_strings(case["source"])]
        if got != case["strings"]:
            failed += 1
            print(f"FAIL source {case['source']!r}: strings {got}, expected {case['strings']}")
    for case in fx.get("raw_kinds", []):
        total += 1
        got = _raw_kind_count(case["source"])
        if got != case["raw"]:
            failed += 1
            print(f"FAIL raw kind {case['source']!r}: {got}, expected {case['raw']}")
    rendered = rendered_rules(yaml.safe_load(TERMS.read_text(encoding="utf-8")))
    for case in fx.get("rendered", []):
        total += 1
        got = sorted({w for _, w in rendered_hits(case["text"], rendered)})
        if got != sorted(case["hits"]):
            failed += 1
            print(f"FAIL rendered {case['text']!r}: {got}, expected {case['hits']}")
    for case in fx.get("source_kinds", []):
        total += 1
        got = sorted({w for _, w, _ in source_kind_hits(case["source"], rendered)})
        if got != sorted(case["hits"]):
            failed += 1
            print(f"FAIL source kind {case['source']!r}: {got}, expected {case['hits']}")
    print(f"self-test: {total - failed}/{total} cases pass")
    return 1 if failed else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--write-baseline", action="store_true")
    ap.add_argument("--write-glossary", action="store_true")
    ap.add_argument("--warnings", action="store_true", help="print warnings, not only a count")
    ap.add_argument("--rendered", nargs="+", metavar="PATH",
                    help="check saved page texts for the rendered words (and nothing else)")
    args = ap.parse_args()
    terms, rules, crd_rules = load_terms()
    if args.rendered:
        sys.exit(check_rendered(terms, args.rendered))
    if args.self_test:
        sys.exit(self_test(rules, crd_rules))
    if args.write_glossary:
        _, new = _glossary_render(terms)
        if new is None:
            sys.exit(f"{GLOSSARY.relative_to(ROOT)}: markers missing")
        GLOSSARY.write_text(new, encoding="utf-8")
        print(f"wrote the terms table of {GLOSSARY.relative_to(ROOT)}")
        return
    rep = Report()
    labs = lab_patterns()
    check_ui(rep, rules)
    check_ui_sources(rep, rules)
    check_crds(rep, terms, rules, crd_rules, labs)
    if args.write_baseline:
        write_baseline(rep)
        return
    check_baseline(rep)
    check_docs(rep, rules, labs)
    check_generated(rep)
    check_glossary(rep, terms)
    check_vocabulary(rep, terms)
    check_raw_kinds(rep)
    check_source_kinds(rep, terms)
    with tempfile.TemporaryDirectory() as tmp:
        check_runtime(rep, rules, terms, tmp)
    for e in rep.errors:
        print(e)
    if args.warnings:
        for w in rep.warnings:
            print("warning: " + w)
    lab_note = "" if (ROOT / "scripts/scrub-patterns.txt").exists() else \
        " (scripts/scrub-patterns.txt absent: generic lab patterns only)"
    print(f"check-content: {len(rep.errors)} error(s), {len(rep.warnings)} warning(s), "
          f"{sum(rep.ui_hits.values())} banned-word hit(s) in UI strings and CRD text checked "
          f"against the baseline{lab_note}"
          f"{'; runtime labels checked' if rep.runtime_checked else '; runtime labels NOT checked'}")
    sys.exit(1 if rep.errors else 0)


if __name__ == "__main__":
    main()
