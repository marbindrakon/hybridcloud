"""The operator roles compare numbers as numbers.

The operator images run ansible-core 2.18 without native Jinja types. A fact set with "{{ ... | int }}" is text there,
and a comparison of two such facts is a text comparison: "9" >= "10" is true, so a Ready object skipped a spec change
from generation 9 to 10 (also 99 to 100). The local ansible-core (2.19 and later) has native types only and cannot
be switched back (ANSIBLE_JINJA2_NATIVE is ignored), so no run here shows the difference by itself:

- Static: no task file under operator/ compares a numeric fact (a generation, or any fact set through
  "{{ ... | int }}" or "| float" in the same role) without the cast at the comparison. The check reads the Jinja
  expressions as syntax trees; names assigned inside the same template ({% set x = ... | int %}) are numbers there.
- Behavioural: every role's skip decision (the role's own fact and decision tasks) runs with the generation facts
  as text, the way the operator's ansible-core leaves them, and has to skip an object only when its spec was
  processed.
"""
import concurrent.futures
import os
import sys
import tempfile
import time
import unittest

import yaml
from jinja2 import Environment, nodes
from jinja2.exceptions import TemplateSyntaxError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, load, run_cases, walk  # noqa: E402

ENV = Environment(extensions=[])
ORDERING = {"gt", "gteq", "lt", "lteq"}
EQUALITY = {"eq", "ne"}
CASTS = {"int", "float"}
# directories whose task files hold the operators' roles
ROLE_PARENTS = ("operator/primary/roles", "operator/namespace/roles", "operator/roles")
# keys whose string value is a bare expression, not a template
EXPRESSION_KEYS = {"when", "failed_when", "changed_when", "until", "that"}


def _parse(text, bare):
    return ENV.parse("{{ %s }}" % text if bare else text)


def _numeric_cast(text):
    """True when the whole value is one expression ending in | int or | float (text in a non-native ansible)."""
    if "{{" not in text:
        return False
    try:
        tree = _parse(text.strip(), False)
    except TemplateSyntaxError:
        return False
    body = tree.body
    if len(body) != 1 or not isinstance(body[0], nodes.Output) or len(body[0].nodes) != 1:
        return False
    only = body[0].nodes[0]
    return isinstance(only, nodes.Filter) and only.name in CASTS


def _numeric_facts(obj, found):
    """Names set through "{{ ... | int }}" (set_fact and vars) anywhere in a task file."""
    if isinstance(obj, dict):
        for key in ("ansible.builtin.set_fact", "set_fact", "vars"):
            block = obj.get(key)
            if isinstance(block, dict):
                for name, value in block.items():
                    if isinstance(value, str) and _numeric_cast(value):
                        found.add(name)
        for value in obj.values():
            _numeric_facts(value, found)
    elif isinstance(obj, list):
        for value in obj:
            _numeric_facts(value, found)


def _strings(obj, name="", key=""):
    """(task name, key, text, is a bare expression) for every string in a task file."""
    if isinstance(obj, dict):
        name = obj["name"] if isinstance(obj.get("name"), str) else name
        for k, value in obj.items():
            yield from _strings(value, name, k)
    elif isinstance(obj, list):
        for value in obj:
            yield from _strings(value, name, key)
    elif isinstance(obj, str):
        if key in EXPRESSION_KEYS:
            yield name, key, obj, True
        elif "{{" in obj or "{%" in obj:
            yield name, key, obj, False


def _uncast_fact(expr, facts):
    """The fact's name when the operand is that fact itself (or its default) and not a number."""
    while isinstance(expr, nodes.Filter) and expr.name in ("default", "d") and expr.node is not None:
        expr = expr.node
    return expr.name if isinstance(expr, nodes.Name) and expr.name in facts else None


def _is_number(expr):
    return ((isinstance(expr, nodes.Const) and isinstance(expr.value, (int, float)) and not isinstance(expr.value, bool))
            or (isinstance(expr, nodes.Filter) and expr.name in CASTS))


def _task_files(root):
    for parent in ROLE_PARENTS:
        for dirpath, _dirs, files in os.walk(os.path.join(root, parent)):
            if os.path.basename(dirpath) == "tasks":
                for f in sorted(files):
                    if f.endswith((".yml", ".yaml")):
                        yield os.path.join(dirpath, f)


def scan(root):
    """Comparisons of a numeric fact without the cast under root/operator/*/roles.

    Returns (findings, checked, unparsed): findings are (file, task, operator, facts), one per comparison, checked
    counts the ordering comparisons of a numeric fact that carry the cast, unparsed the strings that are not valid
    Jinja.
    """
    by_role = {}
    for path in _task_files(root):
        by_role.setdefault(path.split("/tasks/")[0], []).append(path)
    findings, checked, unparsed = [], 0, []
    for _role, paths in sorted(by_role.items()):
        docs = {}
        facts = set()
        for path in paths:
            with open(path) as f:
                docs[path] = yaml.safe_load(f)
            _numeric_facts(docs[path], facts)
        for path, doc in docs.items():
            for task, key, text, bare in _strings(doc):
                try:
                    tree = _parse(text, bare)
                except TemplateSyntaxError as exc:
                    unparsed.append((os.path.relpath(path, root), task, str(exc)))
                    continue
                # a name assigned in the template itself ({% set generation = ... | int %}) is a number there
                local = {t.name for a in tree.find_all((nodes.Assign, nodes.AssignBlock))
                         for t in ([a.target] if isinstance(a.target, nodes.Name) else a.target.iter_child_nodes())
                         if isinstance(t, nodes.Name)}
                names = (facts | {n for n in _names(tree) if _generation_name(n)}) - local
                for cmp_ in tree.find_all(nodes.Compare):
                    sides = [cmp_.expr] + [o.expr for o in cmp_.ops]
                    for i, op in enumerate(cmp_.ops):
                        left, right = sides[i], sides[i + 1]
                        text_facts = []
                        for mine, other in ((left, right), (right, left)):
                            fact = _uncast_fact(mine, names)
                            if fact and (op.op in ORDERING or (op.op in EQUALITY and _is_number(other))):
                                text_facts.append(fact)
                        if text_facts:
                            findings.append((os.path.relpath(path, root), task, op.op, ", ".join(text_facts)))
                        elif op.op in ORDERING and any(
                                isinstance(s, nodes.Filter) and s.name in CASTS and _uncast_fact(s.node, names)
                                for s in (left, right)):
                            checked += 1
    return findings, checked, unparsed


def _generation_name(name):
    return name.endswith("_generation") and name != "_generation"


def _names(tree):
    return {n.name for n in tree.find_all(nodes.Name)}


class StaticScan(unittest.TestCase):
    def _scan_text(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "operator/primary/roles/demo/tasks/main.yml")
            os.makedirs(os.path.dirname(path))
            with open(path, "w") as f:
                f.write(text)
            return scan(tmp)

    FACTS = '''---
- ansible.builtin.set_fact:
    demo_generation: "{{ cr.metadata.generation | default(0) | int }}"
    demo_observed_generation: "{{ cr.status.observedGeneration | default(0) | int }}"
    demo_count: "{{ cr.spec.count | default(0) | int }}"
    demo_name: "{{ cr.metadata.name }}"
'''

    def test_the_scan_flags_what_it_has_to(self):
        for expr in ("demo_observed_generation >= demo_generation",
                     "(demo_observed_generation | int) >= demo_generation",
                     "demo_generation > demo_observed_generation | int",
                     "demo_count < 5 and demo_generation | int > 0 and demo_count | default(1) > demo_count | int",
                     "demo_count == 0",
                     "0 != demo_count"):
            for text in ("{{ %s }}" % expr, "{%% if %s %%}x{%% endif %%}" % expr):
                findings, _checked, unparsed = self._scan_text(
                    self.FACTS + "- ansible.builtin.set_fact:\n    demo_skip: \"%s\"\n" % text)
                self.assertTrue(findings, text)
                self.assertFalse(unparsed)
            findings, _c, _u = self._scan_text(self.FACTS + "- ansible.builtin.debug:\n    msg: ok\n  when: %s\n" % expr)
            self.assertTrue(findings, "when: " + expr)

    def test_the_scan_accepts_what_is_a_number(self):
        for expr in ("demo_observed_generation | int >= demo_generation | int",
                     "(demo_observed_generation | int) > (demo_generation | int)",
                     "demo_count | int < 5",
                     "demo_name | length > 0",
                     "demo_count | int == 0",
                     "demo_name == 'x'",
                     "demo_count == 'x'"):
            findings, _c, unparsed = self._scan_text(
                self.FACTS + "- ansible.builtin.set_fact:\n    demo_skip: \"{{ %s }}\"\n" % expr)
            self.assertEqual(findings, [], expr)
            self.assertEqual(unparsed, [])

    def test_the_scan_leaves_template_locals_alone(self):
        text = self.FACTS + (
            "- ansible.builtin.set_fact:\n"
            "    demo_phase: >-\n"
            "      {%- set generation = cr.metadata.generation | default(0, true) | int -%}\n"
            "      {%- set observed = cr.status.observedGeneration | default(0, true) | int -%}\n"
            "      {{ generation > observed and observed >= generation }}\n")
        findings, _c, unparsed = self._scan_text(text)
        self.assertEqual((findings, unparsed), ([], []))

    def test_the_scan_follows_facts_set_in_another_file_of_the_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "operator/namespace/roles/demo/tasks")
            os.makedirs(base)
            with open(os.path.join(base, "main.yml"), "w") as f:
                f.write(self.FACTS)
            with open(os.path.join(base, "other.yml"), "w") as f:
                f.write("- ansible.builtin.set_fact:\n    demo_big: \"{{ demo_count > 3 }}\"\n")
            findings, _c, _u = scan(tmp)
        self.assertEqual([f[3] for f in findings], ["demo_count"])


class OperatorTree(unittest.TestCase):
    def test_no_numeric_fact_is_compared_as_text(self):
        findings, checked, unparsed = scan(ROOT)
        self.assertEqual(unparsed, [], "task strings that are not valid Jinja")
        self.assertEqual(findings, [], "\n".join(
            "%s, task '%s': %s compares %s without | int" % f for f in findings))
        # the scan sees the skip decisions (one per kind with a reconcile period), so it cannot pass by reading nothing
        self.assertGreaterEqual(checked, 25)


def _skip_roles(root):
    """Every role with a skip decision: kind, role, fact prefix, CR variable, facts task, decision task, decision
    fact, the tasks between them that the decision needs, and the role's defaults."""
    out = []
    for kind in ("primary", "namespace"):
        base = os.path.join(root, "operator", kind, "roles")
        for role in sorted(os.listdir(base)):
            path = os.path.join(base, role, "tasks/main.yml")
            if not os.path.exists(path):
                continue
            ordered = list(walk(load(path)))
            facts = skip = None
            for task in ordered:
                block = task.get("ansible.builtin.set_fact")
                if isinstance(block, dict):
                    if any(k.endswith("_observed_generation") for k in block):
                        facts = task
                    if any(k.endswith("_skip_reconcile") for k in block):
                        skip = task
            if not (facts and skip):
                continue
            block = facts["ansible.builtin.set_fact"]
            prefix = next(k for k in block if k.endswith("_observed_generation"))[:-len("_observed_generation")]
            cr = next(v for v in block.values() if "_hybridsovereign_redhat_" in str(v))
            crvar = "_hybridsovereign_redhat_" + str(cr).split("_hybridsovereign_redhat_")[1].split(".")[0].split(" ")[0]
            extras = []
            if "_probe_age" in str(skip):
                extras = [t for t in ordered[ordered.index(facts) + 1:ordered.index(skip)]
                          if isinstance(t.get("ansible.builtin.set_fact"), dict)]
            defaults = os.path.join(base, role, "defaults/main.yml")
            out.append((kind, role, prefix, crvar, facts, skip,
                        next(k for k in skip["ansible.builtin.set_fact"] if k.endswith("_skip_reconcile")),
                        extras, load(defaults) if os.path.exists(defaults) else {}))
    return out


@unittest.skipIf(not HAVE_ANSIBLE, "ansible-playbook not installed")
class SkipDecisionWithTextFacts(unittest.TestCase):
    CHANGED = [(2, 1), (10, 9), (12, 9), (100, 99), (11, 10), (20, 3)]
    PROCESSED = [(1, 1), (9, 9), (10, 10), (100, 100)]

    def test_a_spec_change_is_never_skipped_across_a_digit_boundary(self):
        roles = _skip_roles(ROOT)
        self.assertGreaterEqual(len(roles), 28)
        rr = load(os.path.join(ROOT, "operator/roles/_common/tasks/reconcile_request.yml"))
        pairs = self.CHANGED + self.PROCESSED
        probed = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 600))
        crs = [{"metadata": {"name": "x", "generation": g, "annotations": {}},
                "status": {"observedGeneration": o, "status": "ready", "ready": True, "lastReconciledAt": probed,
                           "lastHandledReconcileRequest": "2026-11-02T10:00:00Z"}} for g, o in pairs]
        def run(entry):
            kind, role, prefix, crvar, facts, skip, skipkey, extras, defaults = entry
            steps = [{"ansible.builtin.set_fact": {"reconcile_request_cr": "{{ case }}", crvar: "{{ case }}"}}, facts]
            # the operator's ansible-core leaves these as text
            steps.append({"ansible.builtin.set_fact": {
                "%s_generation" % prefix: "{{ %s_generation | string }}" % prefix,
                "%s_observed_generation" % prefix: "{{ %s_observed_generation | string }}" % prefix}})
            steps += rr + extras + [skip]
            return run_cases(steps, crs, "{'role': '%s/%s', 'text': %s_generation is string, 'skip': %s | bool}"
                             % (kind, role, prefix, skipkey), dict(defaults or {}))

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            per_role = list(pool.map(run, roles))
        rows = [dict(row, case=i) for role_rows in per_role for i, row in enumerate(role_rows)]
        self.assertEqual(len(rows), len(roles) * len(pairs))
        wrong = []
        for row in rows:
            generation, observed = pairs[row["case"]]
            self.assertTrue(row["text"], row["role"])
            if row["skip"] != ((generation, observed) in self.PROCESSED):
                wrong.append("%s: generation %s, observed %s, skip %s" % (row["role"], generation, observed, row["skip"]))
        self.assertEqual(wrong, [])


if __name__ == "__main__":
    unittest.main()
