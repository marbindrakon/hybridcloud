"""ACM object names of an Assignment (O-1, OD48): <tenant>-<assignment>, from one shared task.

eda/common/tasks/assignment_acm_name.yml computes the name; assignment_provision (when nothing is recorded yet) and
assignment_teardown (when nothing is recorded) both include it, and status.acmPolicyName records what was used. The
name is DNS-label safe and capped so that the policy namespace and the name stay within ACM's 62 characters; a longer
name ends in a short hash. An ACM object that already exists under the name and belongs to another Assignment (owner
annotation) is neither taken over by the provision job nor removed by the teardown.
"""
import hashlib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, walk, write_out  # noqa: E402

ACM_NAME = os.path.join(ROOT, "eda", "common", "tasks", "assignment_acm_name.yml")
ROLES = os.path.join(ROOT, "eda", "rulebooks", "roles")
PROVISION_ACM = os.path.join(ROLES, "assignment_provision", "tasks", "provision_acm.yml")
APPLY = os.path.join(ROLES, "assignment_provision", "tasks", "apply_acm_resource.yml")
TEARDOWN = os.path.join(ROLES, "assignment_teardown", "tasks", "main.yml")
INCLUDE = "{{ role_path }}/../../../common/tasks/assignment_acm_name.yml"


def includes(path):
    return [t for t in walk(load(path)) if t.get("ansible.builtin.include_tasks") == INCLUDE]


class Wiring(unittest.TestCase):
    def test_provision_and_teardown_use_the_shared_task(self):
        for path in (PROVISION_ACM, TEARDOWN):
            inc = includes(path)
            self.assertEqual(len(inc), 1, path)
            v = inc[0]["vars"]
            self.assertEqual(v["aan_namespace"], "{{ assignment_cr_namespace }}")
            self.assertEqual(v["aan_assignment"], "{{ assignment_cr_name }}")
            self.assertEqual(v["aan_policy_namespace"], "{{ acm_policy_namespace }}")
        # nothing else derives a name from the Assignment name alone any more
        for path in (PROVISION_ACM, TEARDOWN):
            with open(path) as f:
                text = f.read()
            self.assertNotIn("assignment_cr_name | lower", text, path)
            self.assertNotIn("legacy", text, path)

    def test_provision_keeps_a_recorded_name(self):
        step = by_name(load(PROVISION_ACM), "Build ACM Policy name (recorded, else <tenant>-<assignment>)")
        expr = step["ansible.builtin.set_fact"]["acm_policy_name"]
        self.assertLess(expr.index("assignment_stored_acm_policy_name"), expr.index("assignment_acm_name"))

    def test_objects_carry_their_owner(self):
        acm = load(PROVISION_ACM)
        for task, key in (("Build ACM Policy body", "acm_policy_body"),
                          ("Build ACM Placement body", "acm_placement_body"),
                          ("Build ACM PlacementBinding body", "acm_placementbinding_body")):
            body = by_name(acm, task)["ansible.builtin.set_fact"][key]
            self.assertEqual(body["metadata"]["annotations"]["hybridsovereign.redhat/assignment"], "{{ acm_owner }}")

    def test_foreign_object_is_refused_before_the_record(self):
        names = [t["name"].split(" — ")[0] for t in load(APPLY)]
        self.assertLess(names.index("Refuse an ACM object of another Assignment"),
                        names.index("Record the ACM policy of this Assignment"))


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class Names(unittest.TestCase):
    def name(self, namespace, assignment, policy_ns="sovereign-cloud-helpers", max_length=""):
        return run_play(load(ACM_NAME) + [write_out("assignment_acm_name | trim")],
                        {"aan_namespace": namespace, "aan_assignment": assignment,
                         "aan_policy_namespace": policy_ns, "aan_max_length": max_length})

    def test_tenant_prefix(self):
        self.assertEqual(self.name("entity-acme-corp", "web"), "acme-corp-web")
        self.assertEqual(self.name("entity-globex", "web"), "globex-web")
        # a namespace without the tenant prefix is used as it is
        self.assertEqual(self.name("team-a", "web"), "team-a-web")

    def test_dns_safe(self):
        self.assertEqual(self.name("entity-Acme_Corp", "Web..App-"), "acme-corp-web-app")

    def test_cap_and_hash(self):
        ns, asg = "entity-a-very-long-tenant-name", "and-an-even-longer-assignment-name"
        got = self.name(ns, asg)
        # sovereign-cloud-helpers (23) + name <= 62
        self.assertLessEqual(len(got), 62 - len("sovereign-cloud-helpers"))
        digest = hashlib.sha256(("%s/%s" % (ns, asg)).encode()).hexdigest()[:8]
        self.assertTrue(got.endswith("-" + digest), got)
        self.assertTrue(got.startswith("a-very-long-tenant-name-and-an"), got)
        self.assertNotIn("--", got)
        # stable, and distinct for another tenant
        self.assertEqual(self.name(ns, asg), got)
        self.assertNotEqual(self.name("entity-a-very-long-tenant-name-2", asg), got)

    def test_short_names_are_never_hashed(self):
        got = self.name("entity-acme", "a" * 34)  # 4 + 1 + 34 = 39, the cap
        self.assertEqual(got, "acme-" + "a" * 34)

    def test_cap_follows_the_policy_namespace_and_the_override(self):
        self.assertEqual(len(self.name("entity-acme", "a" * 40, policy_ns="p")), 45)
        self.assertEqual(len(self.name("entity-acme", "a" * 40, max_length="30")), 30)


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class Ownership(unittest.TestCase):
    def refused(self, live_owner, status=409):
        step = by_name(load(APPLY), "Refuse an ACM object of another Assignment — {{ acm_resource_name }}")
        meta = {"annotations": {"hybridsovereign.redhat/assignment": live_owner}} if live_owner is not None else {}
        harness = [{"block": [step, write_out("'continued'")],
                    "rescue": [write_out("'refused'")]}]
        return run_play(harness, {"acm_post_result": {"status": status},
                                  "acm_get_result": {"json": {"metadata": meta}},
                                  "acm_resource_name": "Policy acme-web", "acm_owner": "entity-acme/web"})

    def test_provision_refuses_another_assignments_object(self):
        self.assertEqual(self.refused("entity-acme-x/y"), "refused")
        self.assertEqual(self.refused("entity-acme/web"), "continued")
        self.assertEqual(self.refused(None), "continued")
        self.assertEqual(self.refused("entity-acme-x/y", status=201), "continued")

    def ours(self, live_owner, status=200):
        step = by_name(load(TEARDOWN), "Decide whether the fetched ACM Policy is this Assignment's")
        meta = {"annotations": {"hybridsovereign.redhat/assignment": live_owner}} if live_owner is not None else {}
        return run_play([step, write_out("acm_policy_ours | bool")],
                        {"acm_policy_fetch": {"status": status, "json": {"metadata": meta}},
                         "assignment_cr_namespace": "entity-acme", "assignment_cr_name": "web"})

    def test_teardown_leaves_another_assignments_policy(self):
        self.assertIs(self.ours("entity-acme/web"), True)
        self.assertIs(self.ours(None), True)
        self.assertIs(self.ours("entity-acme-x/y"), False)
        self.assertIs(self.ours(None, status=404), False)


if __name__ == "__main__":
    unittest.main()
