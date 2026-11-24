"""The access groups' names of an environment (cloudoso_provision group_names.yml, used by the OpenStack and the
virtualization environments): each <tenant>.<group> from the names function; one that does not fit (an access
group made before the rule for names) refuses that group alone with the function's sentence, and the others are
named and granted. The consumers leave a refused group out of what they grant and say why. Names are examples."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, load, run_play, walk, write_out  # noqa: E402

ROLE = os.path.join(ROOT, "eda", "rulebooks", "roles", "cloudoso_provision")
CV = os.path.join(ROOT, "eda", "rulebooks", "roles", "cloudvirt_provision", "tasks", "access.yml")


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class OneGroupRefused(unittest.TestCase):
    def test_a_long_name_refuses_that_group_only(self):
        long = "g" * 60
        out = run_play([{"ansible.builtin.include_tasks": os.path.join(ROLE, "tasks", "group_names.yml"),
                         "vars": {"role_path": ROLE}},
                        write_out("{'names': gn_names, 'refused': gn_refused}")],
                       {"gn_tenant": "acme-corp", "gn_groups": ["devs", long, "ops"]})
        self.assertEqual(out["names"], {"devs": "acme-corp.devs", "ops": "acme-corp.ops"})
        self.assertEqual(list(out["refused"]), [long])
        self.assertIn("at most 64", out["refused"][long])


class Consumers(unittest.TestCase):
    def test_the_openstack_grants_leave_a_refused_group_out(self):
        with open(os.path.join(ROLE, "tasks", "access.yml")) as f:
            text = f.read()
        self.assertIn("'nameRefused': gn_refused[g.rbac] | default('')", text)
        self.assertIn("if g.path | length > 0 and not g.nameRefused", text)
        self.assertIn("refused.append({'group': g.rbac, 'message': msg, 'by': 'name'})", text)

    def test_the_virtualization_grants_leave_a_refused_group_out(self):
        task = next(t for t in walk(load(CV)) if t.get("name") == "The groups of the Rbac CRs")
        self.assertIn("if g.rbac in gn_names", task["ansible.builtin.set_fact"]["cv_grants"])
        wait = next(t for t in walk(load(CV)) if t.get("name") == "Wait for Rbac CRs without a group yet")
        self.assertIn("gn_refused.items()", wait["ansible.builtin.set_fact"]["cv_access_waiting"])


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class VirtualizationWaiting(unittest.TestCase):
    def test_the_refused_group_is_said(self):
        wait = next(t for t in walk(load(CV)) if t.get("name") == "Wait for Rbac CRs without a group yet")
        out = run_play([wait, write_out("cv_access_waiting")],
                       {"cv_grants": [{"rbac": "devs", "path": "/acme-corp/devs"}, {"rbac": "ops", "path": ""}],
                        "gn_refused": {"long": "The group name is too long."}})
        self.assertEqual(out, ["Rbac ops (no Keycloak group yet)", "Rbac long refused: The group name is too long."])


if __name__ == "__main__":
    unittest.main()
