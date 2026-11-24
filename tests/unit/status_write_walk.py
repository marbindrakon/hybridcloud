"""Static walk of the jobs' status writes: every include of eda/common/tasks/patch_cr_status.yml.

A write's cr_status_body becomes the set of key paths it can write:
- a YAML mapping gives its keys, a list its items (path element "[]");
- a Jinja expression gives the keys of its dict literals, of `combine` and `default` arguments, of both branches of
  `x if c else y`, of `a + b`, and of the variables it names;
- a variable resolves to the task's own `vars:` (and those of the blocks around it) plus every set_fact of the name
  when it is one of them; else to every definition of the name in the job files (set_fact, the vars of an include,
  role defaults and vars); each definition with its own task's local variables.
What the walk cannot see (a value read from an API answer, a filter's result) adds no path. The walk is flow
insensitive: a key that a branch builds and a later condition leaves out is still listed.

The kind of a write: its literal cr_plural, or the literal values of the variable it names; for a shared file whose
plural is computed from the event (fabric_finish.yml and the like), the role of each include site (`<kind>_<job>`),
with the include's variables in front; with no include site in a role, every kind the other writes reach.

Read only; used by tests/unit/test_status_writes_kept.py.
"""
import os
import re

import jinja2
from jinja2 import nodes
import yaml

LIST = "[]"
WRITER = "patch_cr_status.yml"

_ENV = jinja2.Environment(extensions=["jinja2.ext.do", "jinja2.ext.loopcontrols"])


def _one_expression(text):
    """The expression node of a string that is one `{{ ... }}` and nothing else (blank around it), else None."""
    try:
        tree = _ENV.parse(text)
    except jinja2.TemplateSyntaxError:
        return None
    if len(tree.body) != 1 or not isinstance(tree.body[0], nodes.Output):
        return None
    exprs = [n for n in tree.body[0].nodes
             if not (isinstance(n, nodes.TemplateData) and not n.data.strip())]
    return exprs[0] if len(exprs) == 1 and not isinstance(exprs[0], nodes.TemplateData) else None


class _Loader(yaml.SafeLoader):
    pass


_Loader.add_constructor("!unsafe", lambda loader, node: loader.construct_scalar(node))
_Loader.add_constructor("!vault", lambda loader, node: "")


def _load(path):
    with open(path) as fh:
        try:
            return yaml.load(fh, Loader=_Loader)
        except yaml.YAMLError:
            return None


def job_files(root):
    """(task files, variable files) of the jobs that run: eda/rulebooks/roles/** and eda/common/{tasks,vars}."""
    tasks, var_files = [], []
    roles = os.path.join(root, "eda", "rulebooks", "roles")
    for dirpath, _dirs, files in os.walk(roles):
        for f in files:
            if not f.endswith((".yml", ".yaml")):
                continue
            p = os.path.join(dirpath, f)
            part = os.path.relpath(p, roles).split(os.sep)
            if len(part) > 1 and part[1] == "tasks":
                tasks.append(p)
            elif len(part) > 1 and part[1] in ("defaults", "vars"):
                var_files.append(p)
    common = os.path.join(root, "eda", "common", "tasks")
    tasks += [os.path.join(common, f) for f in os.listdir(common) if f.endswith((".yml", ".yaml"))]
    common_vars = os.path.join(root, "eda", "common", "vars")
    if os.path.isdir(common_vars):
        var_files += [os.path.join(common_vars, f) for f in os.listdir(common_vars) if f.endswith(".yml")]
    return sorted(tasks), sorted(var_files)


def _walk_tasks(items, inherited):
    """(task, its local variables: the vars of the blocks around it and its own)."""
    for t in items or []:
        if not isinstance(t, dict):
            continue
        local = dict(inherited)
        if isinstance(t.get("vars"), dict):
            local.update(t["vars"])
        yield t, local
        for key in ("block", "rescue", "always"):
            if isinstance(t.get(key), list):
                yield from _walk_tasks(t[key], local)


def _include_target(task):
    for key in ("ansible.builtin.include_tasks", "include_tasks", "ansible.builtin.import_tasks", "import_tasks"):
        if key in task:
            v = task[key]
            if isinstance(v, dict):
                v = v.get("file", "")
            return str(v)
    return ""


def role_of(path):
    """The role directory name of a job file under eda/rulebooks/roles/, else ""."""
    part = path.split(os.sep)
    if "roles" in part:
        i = part.index("roles")
        if i + 1 < len(part) and "rulebooks" in part[:i]:
            return part[i + 1]
    return ""


class Write:
    """One include of patch_cr_status.yml."""

    def __init__(self, path, name, task_vars, local):
        self.path, self.name, self.vars, self.local = path, name, task_vars, local


class Include:
    """One include of a shared task file from a job file."""

    def __init__(self, path, local):
        self.path, self.local = path, local


class Index:
    """Every definition of a variable name in the job files, every status write, every include site."""

    def __init__(self, root):
        self.root = root
        self.defs = {}
        self.facts = {}
        self.writes = []
        self.includes = {}
        tasks, var_files = job_files(root)
        for p in var_files:
            data = _load(p)
            if isinstance(data, dict):
                for k, v in data.items():
                    self.defs.setdefault(k, []).append((v, {}))
        for p in tasks:
            data = _load(p)
            if not isinstance(data, list):
                continue
            rel = os.path.relpath(p, root)
            for t, local in _walk_tasks(data, {}):
                target = _include_target(t)
                if target and isinstance(t.get("vars"), dict):
                    # an include's vars reach the included file's tasks
                    for k, v in t["vars"].items():
                        self.defs.setdefault(k, []).append((v, local))
                for key in ("ansible.builtin.set_fact", "set_fact"):
                    if isinstance(t.get(key), dict):
                        for k, v in t[key].items():
                            if k != "cacheable":
                                self.defs.setdefault(k, []).append((v, local))
                                self.facts.setdefault(k, []).append((v, local))
                if target:
                    self.includes.setdefault(os.path.basename(target), []).append(Include(rel, local))
                if target.endswith(WRITER):
                    self.writes.append(Write(rel, str(t.get("name", "")), t.get("vars") or {}, local))


class Shape:
    """Keys a value can hold: {key: Shape}; LIST holds the shape of list items."""

    __slots__ = ("keys",)

    def __init__(self):
        self.keys = {}

    def copy(self):
        s = Shape()
        s.keys = {k: v.copy() for k, v in self.keys.items()}
        return s

    def merge(self, other):
        for k, v in other.keys.items():
            if k in self.keys:
                self.keys[k].merge(v)
            else:
                self.keys[k] = v.copy()
        return self

    def paths(self, prefix=()):
        for k, v in sorted(self.keys.items()):
            p = prefix + (k,)
            yield p
            yield from v.paths(p)


def _of_list(items):
    item = Shape()
    for i in items:
        item.merge(i)
    s = Shape()
    s.keys[LIST] = item
    return s


class Evaluator:
    def __init__(self, index, depth=8):
        self.index = index
        self.depth = depth
        self._stack = []
        self._local = [{}]

    def value(self, v, local=None):
        if local is not None:
            self._local.append(local)
            try:
                return self.value(v)
            finally:
                self._local.pop()
        if isinstance(v, dict):
            s = Shape()
            for k, x in v.items():
                if isinstance(k, str) and "{{" not in k:
                    s.keys[k] = self.value(x)
            return s
        if isinstance(v, list):
            return _of_list([self.value(i) for i in v])
        if isinstance(v, str) and "{{" in v:
            expr = _one_expression(v)
            if expr is not None:
                return self.node(expr)
        return Shape()

    def name(self, n):
        if n in self._stack or len(self._stack) >= self.depth:
            return Shape()
        self._stack.append(n)
        try:
            local = self._local[-1]
            s = Shape()
            if n in local:
                # the task's own variable (or its block's) hides the other tasks' and roles' variables of the name;
                # a set_fact of the name still wins over it when it ran before (Ansible's precedence)
                rest = {k: v for k, v in local.items() if k != n}
                s.merge(self.value(local[n], rest))
                defs = self.index.facts.get(n, [])
            else:
                defs = self.index.defs.get(n, [])
            for d, ctx in defs:
                s.merge(self.value(d, ctx))
            return s
        finally:
            self._stack.pop()

    def node(self, n):
        if isinstance(n, nodes.Dict):
            s = Shape()
            for pair in n.items:
                if isinstance(pair.key, nodes.Const) and isinstance(pair.key.value, str):
                    sub = self.node(pair.value)
                    if pair.key.value in s.keys:
                        s.keys[pair.key.value].merge(sub)
                    else:
                        s.keys[pair.key.value] = sub
            return s
        if isinstance(n, (nodes.List, nodes.Tuple)):
            return _of_list([self.node(i) for i in n.items])
        if isinstance(n, nodes.Name):
            return self.name(n.name)
        if isinstance(n, nodes.CondExpr):
            s = self.node(n.expr1)
            return s.merge(self.node(n.expr2)) if n.expr2 is not None else s
        if isinstance(n, nodes.Filter):
            base = self.node(n.node) if n.node is not None else Shape()
            if n.name == "combine":
                for a in n.args:
                    base.merge(self.node(a))
                return base
            if n.name in ("default", "d"):
                return base.merge(self.node(n.args[0])) if n.args else base
            if n.name in ("list", "unique", "sort", "reverse", "flatten"):
                return base
            return Shape()
        if isinstance(n, nodes.Add):
            return self.node(n.left).merge(self.node(n.right))
        if isinstance(n, nodes.Const) and isinstance(n.value, (dict, list)):
            return self.value(n.value)
        return Shape()


def literal_plurals(index, raw, seen=()):
    """The literal values a write's cr_plural can be: itself, or the literal definitions of the variable it names."""
    if isinstance(raw, str) and "{{" not in raw:
        return {raw.strip()}
    out = set()
    m = re.fullmatch(r"\s*\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}\s*", raw) if isinstance(raw, str) else None
    if m and m.group(1) not in seen:
        for d, _ctx in index.defs.get(m.group(1), []):
            out |= literal_plurals(index, d, seen + (m.group(1),))
    return out


def written_paths(index, schemas_by_singular):
    """{(plural, path tuple): set of write sites} for every status write; plurals are those the write can reach.
    A write whose kind is known only from the event and that no role includes is judged against every kind that
    the other writes reach.

    schemas_by_singular maps a kind's singular to its plural (for the role of an include site)."""
    ev = Evaluator(index)
    out = {}
    later = []

    def add(plurals, shape, site):
        for plural in plurals:
            for path in shape.paths():
                out.setdefault((plural, path), set()).add(site)

    for w in index.writes:
        if "cr_status_body" not in w.vars:
            continue
        local = {k: v for k, v in w.local.items() if k != "cr_status_body"}
        plurals = literal_plurals(index, w.vars.get("cr_plural"))
        if plurals:
            add(plurals, ev.value(w.vars["cr_status_body"], local), w.path)
            continue
        # a shared file whose plural comes from the event: one kind per role that includes it
        sites = [i for i in index.includes.get(os.path.basename(w.path), []) if role_of(i.path)]
        reached = False
        for inc in sites:
            singular = role_of(inc.path).rsplit("_", 1)[0]
            plural = schemas_by_singular.get(singular)
            if plural is None:
                continue
            reached = True
            ctx = dict(inc.local)
            ctx.update(local)
            add([plural], ev.value(w.vars["cr_status_body"], ctx), w.path + " <- " + inc.path)
        if not reached:
            later.append((w, local))
    kinds = sorted({plural for plural, _path in out})
    for w, local in later:
        add(kinds, ev.value(w.vars["cr_status_body"], local), w.path)
    return out


def kept(schema, path):
    """Whether the API server keeps the key path under this schema (structural pruning).

    A key is kept when its object lists it in `properties`, or the object has no such property and either has
    `additionalProperties` (the key then has that schema) or `x-kubernetes-preserve-unknown-fields: true` (all below
    it is kept). An object that lists properties without preserve-unknown-fields drops every other key, also when an
    object above it preserves unknown fields. Returns (kept, index of the first dropped element or None)."""
    node = schema
    for i, key in enumerate(path):
        if node is None:
            return True, None
        if key == LIST:
            if "items" in node:
                node = node["items"]
                continue
            if node.get("x-kubernetes-preserve-unknown-fields"):
                return True, None
            return False, i
        props = node.get("properties") or {}
        if key in props:
            node = props[key]
            continue
        ap = node.get("additionalProperties")
        if isinstance(ap, dict):
            node = ap
            continue
        if ap is True or node.get("x-kubernetes-preserve-unknown-fields"):
            return True, None
        return False, i
    return True, None
