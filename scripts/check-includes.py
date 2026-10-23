#!/usr/bin/env python3
"""Fail when an EDA playbook or task file includes a task file or role that does not exist.

`ansible-playbook --syntax-check` does not follow dynamic includes (`include_tasks`,
`include_role`), so a wrong path only shows when a job runs. This script resolves every
literal path the way Ansible does and reports the ones that resolve to nothing.

Resolved forms:
  - `include_tasks` / `import_tasks` (bare string, or `file:`), with `{{ role_path }}` and
    `{{ playbook_dir }}` expanded; a path relative to the including file, the role's
    `tasks/` directory or the playbook directory.
  - `include_role` / `import_role` `name:` (and `tasks_from:`) against the role directories.
  - `roles:` entries of plays, and `import_playbook`.
A path that still contains another Jinja expression after expansion is dynamic: it is
counted and skipped (listed with --verbose).

Scope: the job trees that AAP runs (`eda/rulebooks`, `eda/common`). The per-kind copies
under `eda/<kind>/roles` are not run by any job template and are not checked.

usage: python3 -I scripts/check-includes.py [--root DIR] [--verbose]
exit 0 = every literal include resolves, 1 = at least one is missing.
"""
import argparse
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    print("check-includes: PyYAML is required", file=sys.stderr)
    sys.exit(2)

SCAN_DIRS = ("eda/rulebooks", "eda/common")
# Role search path of the job project (playbook-adjacent roles, then shared role trees).
ROLE_DIRS = ("eda/rulebooks/roles", "migration/roles")
TASK_KEYS = ("include_tasks", "import_tasks")
ROLE_KEYS = ("include_role", "import_role")
PLAY_KEYS = ("import_playbook",)
NESTED = ("block", "rescue", "always", "tasks", "pre_tasks", "post_tasks", "handlers")
JINJA = re.compile(r"\{\{.*?\}\}")
# Includes that were already broken before this check existed, each with the reason it is
# tolerated. They are reported as "known" and do not fail the run; remove an entry once fixed.
KNOWN_MISSING = {
    ("migration/roles/migration_cr_handler/tasks/main.yml", "resolve_vmware_credentials.yml"):
        "OpenStackMigration VMware/CloudOSO credential tasks were never committed (migration path deferred)",
    ("migration/roles/migration_cr_handler/tasks/main.yml", "resolve_cloudoso_credentials.yml"):
        "OpenStackMigration VMware/CloudOSO credential tasks were never committed (migration path deferred)",
}


class Loader(yaml.SafeLoader):
    """SafeLoader that ignores Ansible's custom tags (!vault, !unsafe)."""


def _ignore_tag(loader, suffix, node):  # noqa: ARG001
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


Loader.add_multi_constructor("!", _ignore_tag)


def short(key):
    """'ansible.builtin.include_tasks' -> 'include_tasks'."""
    return key.rsplit(".", 1)[-1]


def role_of(path):
    """The role directory a file belongs to (…/roles/<name>/tasks/x.yml), or None."""
    parts = path.parts
    for i in range(len(parts) - 2, 0, -1):
        if parts[i - 1] == "roles" and i + 1 < len(parts):
            return Path(*parts[: i + 1])
    return None


class Checker:
    def __init__(self, root, verbose):
        self.root = root
        self.verbose = verbose
        self.missing = []
        self.dynamic = []
        self.resolved = 0
        self.seen = set()
        self.visited = set()

    def rel(self, p):
        try:
            return str(p.resolve().relative_to(self.root.resolve()))
        except ValueError:
            return str(p)

    def find_role(self, name):
        for d in ROLE_DIRS:
            cand = self.root / d / name
            if cand.is_dir():
                return cand
        return None

    def expand(self, raw, ctx):
        s = str(raw).strip()
        if ctx.get("role"):
            s = re.sub(r"\{\{\s*role_path\s*\}\}", str(ctx["role"]), s)
        if ctx.get("playbook_dir"):
            s = re.sub(r"\{\{\s*playbook_dir\s*\}\}", str(ctx["playbook_dir"]), s)
        for name, value in (ctx.get("vars") or {}).items():
            s = re.sub(r"\{\{\s*" + re.escape(name) + r"\s*\}\}", value, s)
        return s

    def resolve_tasks(self, raw, src, ctx):
        s = self.expand(raw, ctx)
        if JINJA.search(s):
            self.dynamic.append((self.rel(src), raw))
            return None
        p = Path(s)
        cands = []
        if p.is_absolute():
            cands.append(p)
        else:
            cands.append(src.parent / p)
            if ctx.get("role"):
                cands.append(ctx["role"] / "tasks" / p)
                cands.append(ctx["role"] / p)
            if ctx.get("playbook_dir"):
                cands.append(ctx["playbook_dir"] / p)
        for c in cands:
            if c.is_file():
                self.resolved += 1
                return c
        self.missing.append((self.rel(src), str(raw)))
        return None

    def resolve_role(self, name, tasks_from, src, ctx, role_vars=None):
        name = self.expand(name, ctx)
        if JINJA.search(name) or (tasks_from and JINJA.search(str(tasks_from))):
            self.dynamic.append((self.rel(src), f"role {name} {tasks_from or ''}".strip()))
            return
        if "." in name and "/" not in name:
            # A collection role (namespace.collection.role): not resolved here.
            self.dynamic.append((self.rel(src), f"collection role {name}"))
            return
        rdir = self.find_role(name)
        if rdir is None:
            self.missing.append((self.rel(src), f"role {name}"))
            return
        entry = str(tasks_from or "main")
        cands = [rdir / "tasks" / entry, rdir / "tasks" / f"{entry}.yml", rdir / "tasks" / f"{entry}.yaml"]
        target = next((c for c in cands if c.is_file()), None)
        if target is None:
            self.missing.append((self.rel(src), f"role {name} tasks_from {entry}"))
            return
        self.resolved += 1
        # String vars passed to the role, expanded in the caller's context ({{ role_path }} of the caller).
        rvars = {k: self.expand(v, ctx) for k, v in (role_vars or {}).items() if isinstance(v, str)}
        self.walk_file(target, {"role": rdir, "playbook_dir": ctx.get("playbook_dir"), "vars": rvars})

    def walk_tasks(self, tasks, src, ctx):
        if not isinstance(tasks, list):
            return
        for t in tasks:
            if not isinstance(t, dict):
                continue
            for key, val in t.items():
                k = short(key)
                if k in TASK_KEYS:
                    raw = val.get("file") if isinstance(val, dict) else val
                    if raw is None:
                        continue
                    target = self.resolve_tasks(raw, src, ctx)
                    if target is not None:
                        self.walk_file(target, ctx)
                elif k in ROLE_KEYS and isinstance(val, dict) and val.get("name"):
                    self.resolve_role(val["name"], val.get("tasks_from"), src, ctx, t.get("vars"))
                elif k in NESTED:
                    self.walk_tasks(val, src, ctx)

    def walk_file(self, path, ctx):
        key = (str(path.resolve()), str(ctx.get("role")), str(ctx.get("playbook_dir")),
               str(sorted((ctx.get("vars") or {}).items())))
        if key in self.seen:
            return
        self.seen.add(key)
        self.visited.add(str(path.resolve()))
        try:
            docs = list(yaml.load_all(path.read_text(), Loader=Loader))
        except (yaml.YAMLError, OSError) as e:
            self.missing.append((self.rel(path), f"unreadable: {e}"))
            return
        for doc in docs:
            self.walk_tasks(doc, path, ctx)

    def walk_playbook(self, path):
        try:
            docs = list(yaml.load_all(path.read_text(), Loader=Loader))
        except (yaml.YAMLError, OSError) as e:
            self.missing.append((self.rel(path), f"unreadable: {e}"))
            return
        ctx = {"playbook_dir": path.parent, "role": None}
        for doc in docs:
            if not isinstance(doc, list):
                continue
            for play in doc:
                if not isinstance(play, dict):
                    continue
                for key, val in play.items():
                    if short(key) in PLAY_KEYS:
                        target = path.parent / self.expand(val, ctx)
                        if target.is_file():
                            self.resolved += 1
                        else:
                            self.missing.append((self.rel(path), str(val)))
                if "hosts" not in play:
                    continue
                for r in play.get("roles") or []:
                    name = r.get("role") or r.get("name") if isinstance(r, dict) else r
                    if name:
                        self.resolve_role(str(name), None, path, ctx)
                for sect in ("pre_tasks", "tasks", "post_tasks", "handlers"):
                    self.walk_tasks(play.get(sect), path, ctx)

    def run(self):
        playbooks = sorted((self.root / "eda" / "rulebooks").glob("*.yml"))
        for pb in playbooks:
            text = pb.read_text()
            if re.search(r"^\s*-?\s*hosts:", text, re.M):
                self.walk_playbook(pb)
        # Every task file in the scanned trees, also those only reached dynamically.
        for d in SCAN_DIRS:
            for f in sorted((self.root / d).rglob("*.yml")):
                if "/collections/" in str(f) or f.parent == self.root / "eda" / "rulebooks":
                    continue
                role = role_of(f.relative_to(self.root))
                ctx = {"role": self.root / role if role else None,
                       "playbook_dir": self.root / "eda" / "rulebooks"}
                if role is not None and f.relative_to(self.root / role).parts[0] not in ("tasks", "handlers"):
                    continue
                if str(f.resolve()) in self.visited:
                    continue  # already checked in the context it is included from
                self.walk_file(f, ctx)
        return playbooks


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    c = Checker(Path(a.root), a.verbose)
    playbooks = c.run()
    missing = sorted(m for m in set(c.missing) if m not in KNOWN_MISSING)
    known = sorted(m for m in set(c.missing) if m in KNOWN_MISSING)
    for src, raw in missing:
        print(f"MISSING {src}: {raw}")
    for src, raw in known:
        print(f"known   {src}: {raw} ({KNOWN_MISSING[(src, raw)]})")
    if a.verbose:
        for src, raw in sorted(set(c.dynamic)):
            print(f"dynamic {src}: {raw}")
    print(f"check-includes: {len(playbooks)} playbooks, {c.resolved} includes resolved, "
          f"{len(set(c.dynamic))} dynamic skipped, {len(known)} known, {len(missing)} missing")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
