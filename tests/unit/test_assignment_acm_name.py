"""ACM object names of an Assignment (O-1, OD48, M1): <tenant>-<assignment>-<hash>, from one shared task.

eda/common/tasks/assignment_acm_name.yml computes the name; assignment_provision (when nothing is recorded yet) and
assignment_teardown (when nothing is recorded) both include it, and status.acmPolicyName records what was used. The
name is DNS-label safe and capped so that the policy namespace and the name stay within ACM's 62 characters, and it
always ends in a short hash of <namespace>/<assignment>: the readable part alone is not unique (tenant acme with
Assignment corp-web and tenant acme-corp with Assignment web read the same). An ACM object that already exists under the name and belongs to another Assignment (owner
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
        step = by_name(load(PROVISION_ACM), "Build ACM Policy name (recorded, else <tenant>-<assignment>-<hash>)")
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

    @staticmethod
    def digest(namespace, assignment):
        return hashlib.sha256(("%s/%s" % (namespace, assignment)).encode()).hexdigest()[:8]

    def test_tenant_prefix(self):
        self.assertEqual(self.name("entity-acme-corp", "web"), "acme-corp-web-" + self.digest("entity-acme-corp", "web"))
        self.assertEqual(self.name("entity-globex", "web"), "globex-web-" + self.digest("entity-globex", "web"))
        # a namespace without the tenant prefix is used as it is
        self.assertEqual(self.name("team-a", "web"), "team-a-web-" + self.digest("team-a", "web"))

    def test_dns_safe(self):
        got = self.name("entity-Acme_Corp", "Web..App-")
        self.assertEqual(got, "acme-corp-web-app-" + self.digest("entity-Acme_Corp", "Web..App-"))
        self.assertRegex(got, r"^[a-z0-9]+(-[a-z0-9]+)*$")

    def test_readable_collisions_get_distinct_names(self):
        # M1: the same readable part across tenants, and within one tenant
        for (ns1, a1), (ns2, a2) in ((("entity-acme", "corp-web"), ("entity-acme-corp", "web")),
                                     (("entity-acme", "a.b"), ("entity-acme", "a-b")),
                                     (("entity-acme", "Web"), ("entity-acme", "web"))):
            one, two = self.name(ns1, a1), self.name(ns2, a2)
            self.assertNotEqual(one, two, (ns1, a1, ns2, a2))
            self.assertEqual(one.rsplit("-", 1)[0], two.rsplit("-", 1)[0])
            self.assertEqual(one, self.name(ns1, a1))  # stable

    def test_cap_and_hash(self):
        ns, asg = "entity-a-very-long-tenant-name", "and-an-even-longer-assignment-name"
        got = self.name(ns, asg)
        # sovereign-cloud-helpers (23) + name <= 62
        self.assertLessEqual(len(got), 62 - len("sovereign-cloud-helpers"))
        self.assertTrue(got.endswith("-" + self.digest(ns, asg)), got)
        self.assertTrue(got.startswith("a-very-long-tenant-name-and-an"), got)
        self.assertNotIn("--", got)
        # stable, and distinct for another tenant
        self.assertEqual(self.name(ns, asg), got)
        self.assertNotEqual(self.name("entity-a-very-long-tenant-name-2", asg), got)

    def test_short_names_are_hashed_too(self):
        got = self.name("entity-acme", "a" * 25)  # 4 + 1 + 25 + 9 = 39, the cap
        self.assertEqual(got, "acme-" + "a" * 25 + "-" + self.digest("entity-acme", "a" * 25))
        self.assertEqual(len(got), 39)
        # one character more: the readable part is cut, the hash stays
        got = self.name("entity-acme", "a" * 26)
        self.assertEqual(got, "acme-" + "a" * 25 + "-" + self.digest("entity-acme", "a" * 26))

    def test_cut_never_leaves_a_double_hyphen(self):
        # the cut falls right after a hyphen of the readable part
        got = self.name("entity-acme", "a" * 24 + "-bcd")
        self.assertEqual(got, "acme-" + "a" * 24 + "-" + self.digest("entity-acme", "a" * 24 + "-bcd"))

    def test_cap_follows_the_policy_namespace_and_the_override(self):
        self.assertEqual(len(self.name("entity-acme", "a" * 60, policy_ns="p")), 61)
        self.assertEqual(len(self.name("entity-acme", "a" * 40, policy_ns="p")), 45 + 9)
        self.assertEqual(len(self.name("entity-acme", "a" * 40, max_length="30")), 30)


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class Ownership(unittest.TestCase):
    def refused(self, live_owner, status=409, message=False):
        step = by_name(load(APPLY), "Refuse an ACM object of another Assignment — {{ acm_resource_name }}")
        meta = {"annotations": {"hybridsovereign.redhat/assignment": live_owner}} if live_owner is not None else {}
        harness = [{"block": [step, write_out("'continued'")],
                    "rescue": [write_out("ansible_failed_result.msg" if message else "'refused'")]}]
        return run_play(harness, {"acm_post_result": {"status": status},
                                  "acm_get_result": {"json": {"metadata": meta}},
                                  "acm_resource_name": "Policy acme-web", "acm_owner": "entity-acme/web"})

    def test_refusal_does_not_name_the_other_assignment(self):
        # M1: the message reaches the tenant's status; it names this Assignment's object only
        msg = self.refused("entity-globex/secret-app", message=True)
        self.assertNotIn("globex", msg)
        self.assertNotIn("secret-app", msg)
        self.assertIn("Policy acme-web", msg)
        self.assertIn("another assignment", msg)

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
