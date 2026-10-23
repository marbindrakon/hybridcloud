"""ACM object names of an Assignment (O-1, OD48): <tenant>-<assignment>, from one shared task.

eda/common/tasks/assignment_acm_name.yml computes the name; assignment_provision (when nothing is recorded yet) and
assignment_teardown (when nothing is recorded) both include it, and status.acmPolicyName records what was used. The
name is DNS-label safe and capped so that the policy namespace and the name stay within ACM's 62 characters; a longer
name ends in a short hash. An ACM object that already exists under the name and belongs to another Assignment (owner
annotation) is neither taken over by the provision job nor removed by the teardown; the provision job then takes the
per-Assignment form (start of the name + hash, R2A-F3) and the refusal never names the other Assignment.
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


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class CrossTenantCollision(unittest.TestCase):
    """R2A-F3: tenant acme + Assignment corp-web and tenant acme-corp + Assignment web share the plain name."""

    def names(self, namespace, assignment):
        return run_play(load(ACM_NAME) + [write_out("[assignment_acm_name | trim, assignment_acm_name_unique | trim]")],
                        {"aan_namespace": namespace, "aan_assignment": assignment,
                         "aan_policy_namespace": "sovereign-cloud-helpers", "aan_max_length": ""})

    def test_unique_form_differs_where_the_plain_name_collides(self):
        a_plain, a_unique = self.names("entity-acme", "corp-web")
        b_plain, b_unique = self.names("entity-acme-corp", "web")
        self.assertEqual(a_plain, b_plain)
        self.assertEqual(a_plain, "acme-corp-web")
        self.assertNotEqual(a_unique, b_unique)
        digest = hashlib.sha256(b"entity-acme-corp/web").hexdigest()[:8]
        self.assertEqual(b_unique, "acme-corp-web-" + digest)
        self.assertLessEqual(len(b_unique), 62 - len("sovereign-cloud-helpers"))
        self.assertEqual(self.names("entity-acme-corp", "web")[1], b_unique)  # stable

    def test_capped_name_is_already_the_unique_form(self):
        plain, unique = self.names("entity-a-very-long-tenant-name", "and-an-even-longer-assignment-name")
        self.assertEqual(plain, unique)

    def refusal_message(self):
        step = by_name(load(APPLY), "Refuse an ACM object of another Assignment — {{ acm_resource_name }}")
        meta = {"annotations": {"hybridsovereign.redhat/assignment": "entity-acme/corp-web"}}
        harness = [{"block": [step], "rescue": [write_out("ansible_failed_result.msg")]}]
        return run_play(harness, {"acm_post_result": {"status": 409}, "acm_get_result": {"json": {"metadata": meta}},
                                  "acm_resource_name": "Policy acme-corp-web", "acm_owner": "entity-acme-corp/web"})

    def test_refusal_never_names_the_other_tenant(self):
        msg = self.refusal_message()
        self.assertIn("another assignment", msg)
        self.assertNotIn("entity-acme/", msg)
        self.assertNotIn("corp-web", msg.replace("acme-corp-web", ""))
        self.assertNotIn("entity-acme-corp", msg)

    def provision_name(self, probe_results, stored=""):
        tasks = load(PROVISION_ACM)
        build = by_name(tasks, "Build ACM Policy name (recorded, else <tenant>-<assignment>)")
        take = by_name(tasks, "Take the per-Assignment name when the plain name is another Assignment's")
        return run_play([build, take, write_out("acm_policy_name | trim")],
                        {"assignment_stored_acm_policy_name": stored, "assignment_acm_name": "acme-corp-web",
                         "assignment_acm_name_unique": "acme-corp-web-1a2b3c4d",
                         "assignment_cr_namespace": "entity-acme-corp", "assignment_cr_name": "web",
                         "acm_name_probe": {"results": probe_results}})

    @staticmethod
    def found(owner):
        meta = {"annotations": {"hybridsovereign.redhat/assignment": owner}} if owner is not None else {}
        return {"status": 200, "json": {"metadata": meta}}

    def test_provision_takes_the_unique_name_when_another_assignment_holds_the_plain_one(self):
        missing = {"status": 404}
        self.assertEqual(self.provision_name([self.found("entity-acme/corp-web"), missing, missing]),
                         "acme-corp-web-1a2b3c4d")
        # a leftover Placement or Binding of the other Assignment counts too
        self.assertEqual(self.provision_name([missing, missing, self.found("entity-acme/corp-web")]),
                         "acme-corp-web-1a2b3c4d")

    def test_provision_keeps_the_plain_name_otherwise(self):
        missing = {"status": 404}
        self.assertEqual(self.provision_name([missing, missing, missing]), "acme-corp-web")
        self.assertEqual(self.provision_name([self.found("entity-acme-corp/web"), missing, missing]), "acme-corp-web")
        self.assertEqual(self.provision_name([self.found(None), missing, missing]), "acme-corp-web")
        self.assertEqual(self.provision_name([{"skipped": True}] * 3), "acme-corp-web")

    def test_probe_runs_only_without_a_record(self):
        probe = by_name(load(PROVISION_ACM), "Look for ACM objects of another Assignment under the name")
        self.assertIs(probe.get("no_log"), True)
        self.assertTrue(any("assignment_stored_acm_policy_name" in w for w in probe["when"]))
        names = [t["name"] for t in load(PROVISION_ACM)]
        self.assertLess(names.index("Take the per-Assignment name when the plain name is another Assignment's"),
                        names.index("Build ACM Policy body"))

    def teardown_name(self, unique_fetch):
        use = by_name(load(TEARDOWN), "Use the per-Assignment name when its Policy is this Assignment's")
        # what the teardown decided for the plain name (set as facts: extra vars would win over the step's set_fact)
        before = {"name": "plain name not ours", "ansible.builtin.set_fact": {"delete_acm_policy_name": "acme-corp-web",
                                                                             "acm_policy_ours": False}}
        return run_play([before, use, write_out("[delete_acm_policy_name | trim, acm_policy_ours | bool]")],
                        {"assignment_acm_name_unique": "acme-corp-web-1a2b3c4d",
                         "assignment_cr_namespace": "entity-acme-corp", "assignment_cr_name": "web",
                         "acm_policy_fetch_unique": unique_fetch})

    def test_teardown_without_record_finds_the_unique_name(self):
        self.assertEqual(self.teardown_name(self.found("entity-acme-corp/web")), ["acme-corp-web-1a2b3c4d", True])
        self.assertEqual(self.teardown_name({"status": 404}), ["acme-corp-web", False])
        self.assertEqual(self.teardown_name(self.found("entity-acme/corp-web")), ["acme-corp-web", False])
        self.assertEqual(self.teardown_name({"skipped": True}), ["acme-corp-web", False])

    def test_teardown_looks_there_only_without_a_record(self):
        fetch = by_name(load(TEARDOWN), "Fetch the ACM Policy under the per-Assignment name")
        self.assertIs(fetch.get("no_log"), True)
        self.assertTrue(any("delete_acm_policy_recorded" in w for w in fetch["when"]))
        self.assertTrue(any("acm_policy_ours" in w for w in fetch["when"]))


if __name__ == "__main__":
    unittest.main()
