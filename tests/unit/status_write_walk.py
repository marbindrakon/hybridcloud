"""Static walk of status writes: the jobs' and the operator's.

The writes walked:
- the jobs (eda/rulebooks/roles/**, eda/common/tasks/): every include of eda/common/tasks/patch_cr_status.yml (its
  `cr_status_body`) and every direct call of the status module `hybridsovereign.shc.cr_status` (its `status`);
- the operator (operator/{namespace,primary}/roles/**, operator/roles/_common): every `operator_sdk.util.k8s_status`
  (its `status`).

A write's body becomes the set of key paths it can write:
- a YAML mapping gives its keys, a list its items (path element "[]");
- a Jinja expression gives the keys of its dict literals, of `combine` and `default` arguments, of both branches of
  `x if c else y`, of `a + b`, and of the variables it names;
- a Jinja statement template (`{% set %}`, `{% for %}`, `{% if %}`, `.append()` / `.extend()` / `.update()` on a
  variable of the template) gives the keys of the value it prints, every branch and loop body taken;
- a variable resolves to the task's own `vars:` (and those of the blocks around it) plus every set_fact of the name
  when it is one of them; else to every definition of the name in the files of its side (set_fact, the vars of an
  include, role defaults and vars); each definition with its own task's local variables.
What the walk cannot see (a value read from an API answer or a registered result, an attribute or item of a value,
a filter other than those above) adds no key and marks the value **opaque**. The walk is flow insensitive: a key that
a branch builds and a later condition leaves out is still listed.

The kind of a write: its literal plural (or kind), or the literal values of the variable it names; for a shared file
whose kind comes from the event or the watched object (fabric_finish.yml, operator/roles/_common), the role of each
include site (`<kind>_<job>` for a job; the operator's watches.yaml for the operator), followed through shared files
that include it, with the include's variables in front; with no include site in a role, every kind the other writes
of its side reach.

Not walked: status written by Python (the install observer's module, helpers that patch status themselves), and by a
generic `k8s` / `uri` task on the status subresource.

Read only; used by tests/unit/test_status_writes_kept.py.
"""
import os
import re

import jinja2
from jinja2 import nodes
import yaml

LIST = "[]"
WRITER = "patch_cr_status.yml"
JOB_MODULES = ("hybridsovereign.shc.cr_status",)
OPERATOR_MODULES = ("operator_sdk.util.k8s_status", "k8s_status")

_ENV = jinja2.Environment(extensions=["jinja2.ext.do", "jinja2.ext.loopcontrols"])


def _parse(text):
    try:
        return _ENV.parse(text)
    except jinja2.TemplateSyntaxError:
        return None


def _one_expression(text):
    """The expression node of a string that is one `{{ ... }}` and nothing else (blank around it), else None."""
    tree = _parse(text)
    if tree is None or len(tree.body) != 1 or not isinstance(tree.body[0], nodes.Output):
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


def _role_files(roles_dir, tasks, var_files):
    for dirpath, _dirs, files in os.walk(roles_dir):
        for f in files:
            if not f.endswith((".yml", ".yaml")):
                continue
            p = os.path.join(dirpath, f)
            part = os.path.relpath(p, roles_dir).split(os.sep)
            if len(part) > 1 and part[1] == "tasks":
                tasks.append(p)
            elif len(part) > 1 and part[1] in ("defaults", "vars"):
                var_files.append(p)


def job_files(root):
    """(task files, variable files) of the jobs that run: eda/rulebooks/roles/** and eda/common/{tasks,vars}."""
    tasks, var_files = [], []
    roles = os.path.join(root, "eda", "rulebooks", "roles")
    if os.path.isdir(roles):
        _role_files(roles, tasks, var_files)
    common = os.path.join(root, "eda", "common", "tasks")
    if os.path.isdir(common):
        tasks += [os.path.join(common, f) for f in os.listdir(common) if f.endswith((".yml", ".yaml"))]
    common_vars = os.path.join(root, "eda", "common", "vars")
    if os.path.isdir(common_vars):
        var_files += [os.path.join(common_vars, f) for f in os.listdir(common_vars) if f.endswith(".yml")]
    return sorted(tasks), sorted(var_files)


def operator_files(root):
    """(task files, variable files) of the operator's roles: both operators' roles and the shared _common role."""
    tasks, var_files = [], []
    for sub in (("operator", "namespace", "roles"), ("operator", "primary", "roles"), ("operator", "roles")):
        d = os.path.join(root, *sub)
        if os.path.isdir(d):
            _role_files(d, tasks, var_files)
    return sorted(tasks), sorted(var_files)


def operator_role_kinds(root):
    """{role name: kind} from the operators' watches.yaml (the role and the finalizer's role of each kind)."""
    out = {}
    for op in ("namespace", "primary"):
        p = os.path.join(root, "operator", op, "watches.yaml")
        if not os.path.exists(p):
            continue
        with open(p) as fh:
            for w in yaml.safe_load(fh) or []:
                if not isinstance(w, dict) or "kind" not in w:
                    continue
                if w.get("role"):
                    out[w["role"]] = w["kind"]
                fin = w.get("finalizer") or {}
                if isinstance(fin, dict) and fin.get("role"):
                    out[fin["role"]] = w["kind"]
    return out


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
    for key in ("ansible.builtin.include_role", "include_role", "ansible.builtin.import_role", "import_role"):
        v = task.get(key)
        if isinstance(v, dict) and v.get("tasks_from"):
            t = str(v["tasks_from"])
            return t if t.endswith((".yml", ".yaml")) else t + ".yml"
    return ""


def role_of(path):
    """The role directory name of a job or operator file under a roles/ directory ("" for the operator's _common
    and for eda/common/tasks)."""
    part = path.split(os.sep)
    if "roles" in part:
        i = part.index("roles")
        if i + 1 < len(part) and ("rulebooks" in part[:i] or "operator" in part[:i]):
            role = part[i + 1]
            return "" if role == "_common" else role
    return ""


class Write:
    """One status write: an include of patch_cr_status.yml, or a direct call of a status module.

    `body` is the written value; `plural` the raw plural (or None); `kind` the raw kind (operator) or None."""

    def __init__(self, path, name, body, plural, kind, local, how):
        self.path, self.name, self.body, self.plural, self.kind, self.local, self.how = (
            path, name, body, plural, kind, local, how)


class Include:
    """One include of a shared task file from a job file."""

    def __init__(self, path, local):
        self.path, self.local = path, local


class Index:
    """Every definition of a variable name in one side's files, every status write, every include site.

    side "jobs" (default) or "operator"."""

    def __init__(self, root, side="jobs"):
        self.root = root
        self.side = side
        self.defs = {}
        self.facts = {}
        self.writes = []
        self.includes = {}
        tasks, var_files = (operator_files if side == "operator" else job_files)(root)
        modules = OPERATOR_MODULES if side == "operator" else JOB_MODULES
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
                name = str(t.get("name", ""))
                tv = t.get("vars") or {}
                if target.endswith(WRITER) and side == "jobs" and isinstance(tv, dict) and "cr_status_body" in tv:
                    own = {k: v for k, v in local.items() if k != "cr_status_body"}
                    self.writes.append(Write(rel, name, tv["cr_status_body"], tv.get("cr_plural"), None, own,
                                             "library"))
                if os.path.basename(p) == WRITER:
                    continue  # the library's own call writes the bodies of its includes, walked above
                for m in modules:
                    args = t.get(m)
                    if isinstance(args, dict) and "status" in args:
                        self.writes.append(Write(rel, name, args["status"], args.get("plural"), args.get("kind"),
                                                 local, "module"))


class Shape:
    """Keys a value can hold: {key: Shape}; LIST holds the shape of list items. `opaque`: some value written here
    is one the walk cannot see into."""

    __slots__ = ("keys", "opaque")

    def __init__(self, opaque=False):
        self.keys = {}
        self.opaque = opaque

    def copy(self):
        s = Shape(self.opaque)
        s.keys = {k: v.copy() for k, v in self.keys.items()}
        return s

    def merge(self, other):
        self.opaque = self.opaque or other.opaque
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

    def opaque_paths(self, prefix=()):
        """Paths (below the root) whose value is opaque."""
        for k, v in sorted(self.keys.items()):
            p = prefix + (k,)
            if v.opaque:
                yield p
            yield from v.opaque_paths(p)


def _of_list(items):
    item = Shape()
    for i in items:
        item.merge(i)
    s = Shape()
    s.keys[LIST] = item
    return s


_MUTATORS = ("append", "extend", "update", "insert")


class Evaluator:
    def __init__(self, index, depth=8):
        self.index = index
        self.depth = depth
        self._stack = []
        self._local = [{}]
        self._tscope = []

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
        if isinstance(v, str) and "{%" in v:
            return self.template(v)
        if isinstance(v, str) and "{{" in v:
            expr = _one_expression(v)
            if expr is not None:
                return self.node(expr)
        return Shape()

    # -- statement templates ------------------------------------------------------------------------------------
    def template(self, text):
        """The shape a statement template prints: its `set` variables and the variables mutated by `.append()` and
        the like are collected over every branch and loop body (twice, so that a use before a later mutation sees
        it); the printed expressions give the shape."""
        tree = _parse(text)
        if tree is None:
            return Shape()
        scope = {}
        self._tscope.append(scope)
        try:
            out = Shape()
            for _ in range(2):
                out = Shape()
                printed = self._stmts(tree.body)
                for p in printed:
                    out.merge(p)
            return out
        finally:
            self._tscope.pop()

    def _set(self, name, shape):
        scope = self._tscope[-1]
        if name in scope:
            scope[name].merge(shape)
        else:
            scope[name] = shape.copy()

    def _stmts(self, body):
        printed = []
        for st in body:
            if isinstance(st, nodes.Output):
                for n in st.nodes:
                    if isinstance(n, nodes.TemplateData):
                        if n.data.strip():
                            printed.append(Shape())
                        continue
                    printed.append(self.node(n))
            elif isinstance(st, nodes.Assign):
                val = self.node(st.node)
                if isinstance(st.target, nodes.Name):
                    self._set(st.target.name, val)
                elif isinstance(st.target, nodes.Tuple) and isinstance(st.node, nodes.Tuple):
                    for t, v in zip(st.target.items, st.node.items):
                        if isinstance(t, nodes.Name):
                            self._set(t.name, self.node(v))
            elif isinstance(st, nodes.For):
                it = self.node(st.iter)
                if isinstance(st.target, nodes.Name):
                    self._set(st.target.name, it.keys.get(LIST, Shape(opaque=True)))
                printed += self._stmts(st.body)
                printed += self._stmts(st.else_)
            elif isinstance(st, nodes.If):
                self.node(st.test)
                printed += self._stmts(st.body)
                for e in st.elif_:
                    printed += self._stmts(e.body)
                printed += self._stmts(st.else_)
            elif isinstance(st, nodes.ExprStmt):
                self.node(st.node)
        return printed

    # -- expressions -------------------------------------------------------------------------------------------
    def name(self, n):
        for scope in reversed(self._tscope):
            if n in scope:
                return scope[n].copy()
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
                if not defs:
                    # a registered result, an inventory or event variable: nothing the walk can see
                    return Shape(opaque=True)
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
            if n.name in ("string", "int", "float", "bool", "trim", "lower", "upper", "length", "count",
                          "to_json", "to_nice_json", "join", "replace", "regex_replace", "truncate", "b64encode"):
                return Shape()  # a scalar
            return Shape(opaque=True)
        if isinstance(n, nodes.Add):
            return self.node(n.left).merge(self.node(n.right))
        if isinstance(n, nodes.Call):
            f = n.node
            if (self._tscope and isinstance(f, nodes.Getattr) and f.attr in _MUTATORS
                    and isinstance(f.node, nodes.Name) and any(f.node.name in s for s in self._tscope)):
                args = [self.node(a) for a in n.args]
                if f.attr == "append":
                    self._set(f.node.name, _of_list(args))
                elif f.attr == "insert" and len(args) == 2:
                    self._set(f.node.name, _of_list(args[1:]))
                else:
                    for a in args:
                        self._set(f.node.name, a)
                return Shape()
            return Shape(opaque=True)
        if isinstance(n, nodes.Getattr) or (isinstance(n, nodes.Getitem) and isinstance(n.arg, nodes.Const)
                                            and isinstance(n.arg.value, str)):
            # x.key of a value whose shape the walk knows: that key's shape (opaque when x may hold more)
            base = self.node(n.node)
            key = n.attr if isinstance(n, nodes.Getattr) else n.arg.value
            if key in base.keys:
                sub = base.keys[key].copy()
                sub.opaque = sub.opaque or base.opaque
                return sub
            return Shape(opaque=True)
        if isinstance(n, nodes.Getitem):
            return Shape(opaque=True)
        if isinstance(n, nodes.Const) and isinstance(n.value, (dict, list)):
            return self.value(n.value)
        return Shape()


def literal_plurals(index, raw, seen=()):
    """The literal values a write's plural (or kind) can be: itself, or the literal definitions of the variable it
    names."""
    if isinstance(raw, str) and "{{" not in raw:
        return {raw.strip()}
    out = set()
    m = re.fullmatch(r"\s*\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}\s*", raw) if isinstance(raw, str) else None
    if m and m.group(1) not in seen:
        for d, _ctx in index.defs.get(m.group(1), []):
            out |= literal_plurals(index, d, seen + (m.group(1),))
    return out


def _role_sites(index, basename, seen=()):
    """[(role, include site local)] of the roles that reach a shared file, through shared files that include it."""
    out = []
    for inc in index.includes.get(basename, []):
        role = role_of(inc.path)
        if role:
            out.append((role, inc.local, inc.path))
        else:
            b = os.path.basename(inc.path)
            if b != basename and b not in seen:
                for role, local, path in _role_sites(index, b, seen + (basename,)):
                    ctx = dict(local)
                    ctx.update(inc.local)
                    out.append((role, ctx, path))
    return out


class Walk:
    """{(plural, path tuple): set of write sites} in `written`; the same for paths whose value is opaque in `opaque`
    (the root of a body that is opaque as a whole has the path ())."""

    def __init__(self):
        self.written = {}
        self.opaque = {}

    def add(self, plurals, shape, site):
        for plural in plurals:
            for path in shape.paths():
                self.written.setdefault((plural, path), set()).add(site)
            if shape.opaque:
                self.opaque.setdefault((plural, ()), set()).add(site)
            for path in shape.opaque_paths():
                self.opaque.setdefault((plural, path), set()).add(site)


def walk(index, schemas_by_singular, kind_plurals=None, role_kinds=None):
    """Every status write of the index's side.

    schemas_by_singular maps a kind's singular to its plural (the role of a job's include site); kind_plurals maps a
    kind to its plural and role_kinds an operator role to its kind (the operator's writes name a kind)."""
    ev = Evaluator(index)
    res = Walk()
    later = []
    kind_plurals = kind_plurals or {}
    role_kinds = role_kinds or {}

    def plural_of_role(role):
        if index.side == "operator":
            return kind_plurals.get(role_kinds.get(role, ""))
        return schemas_by_singular.get(role.rsplit("_", 1)[0])

    for w in index.writes:
        if index.side == "operator":
            plurals = {kind_plurals[k] for k in literal_plurals(index, w.kind) if k in kind_plurals}
        else:
            plurals = literal_plurals(index, w.plural)
        if plurals:
            res.add(plurals, ev.value(w.body, w.local), w.path)
            continue
        # a shared file whose kind comes from the event or the watched object: one kind per role that reaches it
        reached = False
        for role, inc_local, inc_path in _role_sites(index, os.path.basename(w.path)):
            plural = plural_of_role(role)
            if plural is None:
                continue
            reached = True
            ctx = dict(inc_local)
            ctx.update(w.local)
            res.add([plural], ev.value(w.body, ctx), w.path + " <- " + inc_path)
        if not reached:
            later.append(w)
    kinds = sorted({plural for plural, _path in res.written})
    for w in later:
        res.add(kinds, ev.value(w.body, w.local), w.path)
    return res


def written_paths(index, schemas_by_singular):
    """{(plural, path tuple): set of write sites} for every status write of the jobs (see `walk`)."""
    return walk(index, schemas_by_singular).written


def _node_at(schema, path):
    node = schema
    for key in path:
        if node is None:
            return None
        if key == LIST:
            node = node.get("items")
            continue
        props = node.get("properties") or {}
        if key in props:
            node = props[key]
            continue
        ap = node.get("additionalProperties")
        node = ap if isinstance(ap, dict) else None
    return node


def lists_children(schema, path):
    """Whether the schema node at the path lists the keys it keeps (an object with properties that does not preserve
    unknown fields, or a list of such objects): a value written there that the walk cannot see into may lose keys."""
    node = _node_at(schema, path)
    for _ in range(2):
        if not isinstance(node, dict):
            return False
        if node.get("type") == "array":
            node = node.get("items")
            continue
        return bool(node.get("properties")) and not node.get("x-kubernetes-preserve-unknown-fields")
    return False


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
