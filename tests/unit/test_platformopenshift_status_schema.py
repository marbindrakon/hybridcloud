"""Every status key the cluster's jobs write is kept by the PlatformOpenshift CRD's schema.

The API server prunes a key that a structural schema does not list where the object lists its properties and does not
keep unknown fields (`x-kubernetes-preserve-unknown-fields` on `status` does not reach a nested object that lists its
own properties). A key pruned there is lost on the next read, and a job that reads it back decides wrongly (a cluster's
floating IPs found by a description the API had dropped). The test walks every status write of the cluster's jobs
(`patch_cr_status.yml` with `cr_plural: platformopenshifts` in the cluster roles and the common task files they use):
literal keys, facts set in the same roles whose value is a literal mapping, and the keys of mapping literals inside
Jinja expressions, against the CRD's schema. A key the schema prunes fails the test with its path."""
import os
import re
import unittest

import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CRD = os.path.join(ROOT, "operator", "config", "crd", "bases", "crd-platformopenshift.yaml")
ROLES = os.path.join(ROOT, "eda", "rulebooks", "roles")
FILES = [os.path.join(ROLES, r, "tasks", f) for r in ("platformopenshift_provision", "platformopenshift_teardown")
         for f in sorted(os.listdir(os.path.join(ROLES, r, "tasks"))) if f.endswith(".yml")]
FILES.append(os.path.join(ROOT, "eda", "common", "tasks", "openstack_cluster_credential.yml"))
FACT = re.compile(r"^\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}$")
JKEY = re.compile(r"""['"]([A-Za-z_][A-Za-z0-9_]*)['"]\s*:""")


def tasks_of(node):
    if isinstance(node, list):
        for t in node:
            yield from tasks_of(t)
    elif isinstance(node, dict):
        yield node
        for k in ("block", "rescue", "always"):
            if k in node:
                yield from tasks_of(node[k])


def load_all():
    out = []
    for f in FILES:
        with open(f) as fh:
            out.append((f, list(tasks_of(yaml.safe_load(fh) or []))))
    return out


def facts(all_tasks):
    """fact name -> [literal mapping values it is set to] in the cluster roles."""
    out = {}
    for _, tasks in all_tasks:
        for t in tasks:
            sf = t.get("ansible.builtin.set_fact") or t.get("set_fact")
            if isinstance(sf, dict):
                for k, v in sf.items():
                    if isinstance(v, dict):
                        out.setdefault(k, []).append(v)
    return out


def writes(all_tasks):
    """[(file, task name, status body)] of every PlatformOpenshift status write."""
    out = []
    for f, tasks in all_tasks:
        for t in tasks:
            inc = str(t.get("ansible.builtin.include_tasks") or t.get("include_tasks") or "")
            v = t.get("vars") or {}
            if inc.endswith("patch_cr_status.yml") and v.get("cr_plural") == "platformopenshifts":
                out.append((os.path.relpath(f, ROOT), t.get("name"), v.get("cr_status_body")))
    return out


def schema():
    with open(CRD) as fh:
        crd = yaml.safe_load(fh)
    return crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["status"]


def child(node, key):
    """(the schema of key under node, whether node prunes key)."""
    if node is None:
        return None, False
    props = node.get("properties")
    if props is None:
        # no properties listed: kept as given (preserve) or a free map
        return (node.get("additionalProperties") if isinstance(node.get("additionalProperties"), dict) else None), False
    if key in props:
        return props[key], False
    return None, not node.get("x-kubernetes-preserve-unknown-fields", False)


def walk(value, node, path, known, pruned, seen=()):
    if isinstance(value, dict):
        for k, v in value.items():
            sub, gone = child(node, k)
            if gone:
                pruned.append(path + "." + k)
                continue
            if sub is not None and sub.get("type") == "array" and isinstance(v, list):
                for item in v:
                    walk(item, sub.get("items"), path + "." + k + "[]", known, pruned, seen)
            else:
                walk(v, sub, path + "." + k, known, pruned, seen)
    elif isinstance(value, str) and node is not None:
        m = FACT.match(value.strip())
        if m and m.group(1) in known and m.group(1) not in seen:
            for v in known[m.group(1)]:
                walk(v, node, path, known, pruned, seen + (m.group(1),))
        elif "{" in value and node.get("properties") and not node.get("x-kubernetes-preserve-unknown-fields"):
            # the keys of mapping literals in the expression, at this level
            for k in JKEY.findall(value):
                if k not in node["properties"]:
                    pruned.append(path + "." + k + " (in an expression)")


class StatusKeysAgainstTheSchema(unittest.TestCase):
    def test_every_written_key_is_kept(self):
        all_tasks = load_all()
        known = facts(all_tasks)
        found = writes(all_tasks)
        self.assertGreater(len(found), 20)          # the walk sees the cluster's writes
        st = schema()
        problems = []
        for f, name, body in found:
            pruned = []
            walk(body, st, "status", known, pruned)
            problems += ["%s (%s): %s" % (f, name, p) for p in pruned]
        self.assertEqual(problems, [])

    def test_the_walk_sees_a_pruned_key(self):
        """The check itself: a description under floatingIPs.api, which the schema does not list, is reported."""
        pruned = []
        walk({"floatingIPs": {"api": {"address": "a", "id": "i", "description": "d"}}}, schema(), "status", {}, pruned)
        self.assertEqual(pruned, ["status.floatingIPs.api.description"])
        pruned = []
        walk({"floatingIPs": "{{ cb_fips }}"}, schema(), "status",
             {"cb_fips": [{"api": {"address": "a", "id": "i", "description": "d"}}]}, pruned)
        self.assertEqual(pruned, ["status.floatingIPs.api.description"])

    def test_the_floating_ips_record_holds_address_and_id(self):
        known = facts(load_all())
        self.assertEqual([sorted(v["api"]) for v in known["cb_fips"]], [["address", "id"]])



# keys of other kinds' writes the schema prunes today, each to be added to its CRD (asked of the API's owners); a key
# that is not listed here fails the test
KNOWN_ELSEWHERE = {
    "cloudinfrastructures status.site.signIn.clientId (in an expression)",
    "cloudinfrastructures status.site.signIn.idpName (in an expression)",
}


class OtherKindsAgainstTheirSchemas(unittest.TestCase):
    """The same walk for every kind's status writes in every role and common task file (a kind named by a variable
    is not walked)."""

    def test_no_new_pruned_key(self):
        import glob
        crds = {}
        for f in glob.glob(os.path.join(ROOT, "operator", "config", "crd", "bases", "*.yaml")):
            with open(f) as fh:
                d = yaml.safe_load(fh)
            if d and "spec" in d:
                crds[d["spec"]["names"]["plural"]] = (
                    d["spec"]["versions"][0]["schema"]["openAPIV3Schema"].get("properties", {}).get("status"))
        files = sorted(glob.glob(os.path.join(ROLES, "*", "tasks", "*.yml"))
                       + glob.glob(os.path.join(ROOT, "eda", "common", "tasks", "*.yml")))
        all_tasks = []
        for f in files:
            with open(f) as fh:
                all_tasks.append((f, list(tasks_of(yaml.safe_load(fh) or []))))
        known = facts(all_tasks)
        found = set()
        for f, tasks in all_tasks:
            for t in tasks:
                inc = str(t.get("ansible.builtin.include_tasks") or t.get("include_tasks") or "")
                v = t.get("vars") or {}
                if inc.endswith("patch_cr_status.yml") and v.get("cr_plural") in crds:
                    pruned = []
                    walk(v.get("cr_status_body"), crds[v["cr_plural"]], "status", known, pruned)
                    found.update("%s %s" % (v["cr_plural"], p) for p in pruned)
        self.assertEqual(sorted(found - KNOWN_ELSEWHERE), [])


if __name__ == "__main__":
    unittest.main()
