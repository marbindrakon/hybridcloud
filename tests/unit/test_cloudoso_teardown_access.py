"""An OpenStack environment's delete, the parts this round adds (cloudoso_teardown): after the emptiness check the
access groups go from the record (the client roles first, then the groups' roles and the groups), then the cluster
users a failed cluster teardown left, then the project. Run in ansible-playbook with the real helper against a Keystone
over HTTP (fake_keystone.py); the sign-in directory side is a trace (osoui_roles.FAKE_SSG)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import po_harness as H  # noqa: E402
import osoui_roles as O  # noqa: E402
from play_helper import by_name, load  # noqa: E402

try:
    import openstack  # noqa: F401
    HAVE_SDK = True
except ImportError:  # pragma: no cover
    HAVE_SDK = False

MAIN = os.path.join(O.ROOT, "eda", "rulebooks", "roles", "cloudoso_teardown", "tasks", "main.yml")
MARKER = "Managed by the sovereign cloud platform; do not change."


class Order(unittest.TestCase):
    def test_steps(self):
        block = load(MAIN)[0]["block"]
        inner = by_name(block, "Tear down the OpenStack project")["block"]
        names = [t.get("name") for t in inner]
        self.assertLess(names.index("Refuse to delete a project that still holds resources"),
                        names.index("Remove the environment's access groups"))
        self.assertLess(names.index("Remove the environment's access groups"),
                        names.index("Delete the cluster users left in the project"))
        self.assertLess(names.index("Delete the cluster users left in the project"),
                        names.index("Delete the recorded floating IPs, application credential and the project"))


@unittest.skipUnless(H.HAVE_ANSIBLE and HAVE_SDK, "ansible-playbook / openstacksdk not installed")
class TeardownAccess(unittest.TestCase):
    def setUp(self):
        self.site = O.Site()
        self.addCleanup(self.site.stop)
        env = mock.patch.dict(os.environ, self.site.env())
        env.start()
        self.addCleanup(env.stop)
        ks = self.site.ks
        self.pid = ks.add_project("acme_web", self.site.tenants)
        self.other = ks.add_project("acme_db", self.site.tenants)
        self.dev = ks.add_group("acme.developers", self.site.tenants, "x. " + MARKER)
        self.aud = ks.add_group("acme.auditors", self.site.tenants, "x. " + MARKER)
        ks.grant("group", self.dev, "project", self.pid, "member")
        ks.grant("group", self.aud, "project", self.pid, "reader")
        ks.grant("group", self.aud, "project", self.other, "reader")
        self.left = ks.add_user("shc-oso-acme-c1-0a1b2", self.site.tenants, description="c. " + MARKER)
        ks.grant("user", self.left, "project", self.pid, "member")
        self.person = ks.add_user("someone", self.site.tenants)
        ks.grant("user", self.person, "project", self.pid, "member")

    def test_groups_then_users(self):
        sweep = by_name(load(MAIN)[0]["block"], "Delete the cluster users left in the project")
        tasks = [{"ansible.builtin.include_tasks": "{{ role_path }}/tasks/access_down.yml"}, sweep]
        # the site account as the teardown loaded it before the emptiness check
        extra = {"os_account_clouds_yaml": self.site.account, "fake_clouds_yaml": self.site.account,
                 "cloudoso_cr_name": "web", "cloudoso_entity_for_status": "acme",
                 "oso_project_domain": "tenants", "oso_check": {"project_exists": True, "project_id": self.pid},
                 "oso_access": {"signInClient": "openstack-site-a", "groupDomain": "tenants",
                                "groups": [{"group": "acme.developers"}, {"group": "acme.auditors"}]},
                 "fake_ssg": {"all": ["acme.developers", "acme.auditors"],
                              "owned": {"acme.developers": ["CloudOSO/web"],
                                        "acme.auditors": ["CloudOSO/web", "CloudOSO/db"]}}}
        rc, trace, out = H.run("cloudoso_teardown", "main.yml", extra, fakes=O.FAKES, tasks=tasks)
        self.assertEqual(rc, 0, out)
        steps = [("ssg" if "ssg" in t else t["os"]["action"]) for t in trace if "ssg" in t or "os" in t]
        self.assertEqual(steps, ["ssg", "access-groups", "leftover-users"])
        ssg = [t["ssg"] for t in trace if "ssg" in t][0]
        self.assertEqual((ssg["wanted"], ssg["owner"], ssg["client"]), ([], "CloudOSO/web", "openstack-site-a"))
        groups = {g["name"] for g in self.site.ks.groups.values()}
        self.assertEqual(groups, {"acme.auditors"})   # another environment still uses it
        self.assertFalse(any(a[0] == "group" and a[3] == self.pid for a in self.site.ks.assignments))
        self.assertNotIn(self.left, self.site.ks.users)
        self.assertIn(self.person, self.site.ks.users)
        self.assertNotIn(self.site.account_password, str(trace) + out)

    def test_without_the_directory_no_group_is_deleted(self):
        """A record without a client (the directory was never asked): the groups' roles are revoked, no group is
        deleted (whether another environment's client role still uses it is not known); a pending removal of an
        earlier run (status.access.removing) is part of the record."""
        tasks = [{"ansible.builtin.include_tasks": "{{ role_path }}/tasks/access_down.yml"}]
        extra = {"os_account_clouds_yaml": self.site.account, "fake_clouds_yaml": self.site.account,
                 "cloudoso_cr_name": "web", "cloudoso_entity_for_status": "acme",
                 "oso_project_domain": "tenants", "oso_check": {"project_exists": True, "project_id": self.pid},
                 "oso_access": {"signInClient": "", "groupDomain": "tenants",
                                "groups": [{"group": "acme.developers"}], "removing": ["acme.auditors"]}}
        rc, trace, out = H.run("cloudoso_teardown", "main.yml", extra, fakes=O.FAKES, tasks=tasks)
        self.assertEqual(rc, 0, out)
        req = [t["os"] for t in trace if "os" in t][0]
        self.assertEqual(req["groups"], [{"group": "acme.developers", "delete": False},
                                         {"group": "acme.auditors", "delete": False}])
        self.assertEqual({g["name"] for g in self.site.ks.groups.values()}, {"acme.developers", "acme.auditors"})
        self.assertFalse(any(a[0] == "group" and a[3] == self.pid for a in self.site.ks.assignments))

    def test_a_group_the_site_cannot_remove_stops_the_delete(self):
        """V4: a group whose removal fails (not "already gone") stops the delete before the project goes, naming
        the group; the next run finishes it."""
        sweep = by_name(load(MAIN)[0]["block"], "Delete the cluster users left in the project")
        tasks = [{"ansible.builtin.include_tasks": "{{ role_path }}/tasks/access_down.yml"}, sweep]
        self.site.ks.fail_next.append(("DELETE", f"/v3/groups/{self.dev}", 503, "Service Unavailable"))
        extra = {"os_account_clouds_yaml": self.site.account, "fake_clouds_yaml": self.site.account,
                 "cloudoso_cr_name": "web", "cloudoso_entity_for_status": "acme",
                 "oso_project_domain": "tenants", "oso_check": {"project_exists": True, "project_id": self.pid},
                 "oso_access": {"signInClient": "openstack-site-a", "groupDomain": "tenants",
                                "groups": [{"group": "acme.developers"}, {"group": "acme.auditors"}]},
                 "fake_ssg": {"all": ["acme.developers", "acme.auditors"],
                              "owned": {"acme.developers": ["CloudOSO/web"], "acme.auditors": ["CloudOSO/web", "CloudOSO/db"]}}}
        rc, trace, out = H.run("cloudoso_teardown", "main.yml", extra, fakes=O.FAKES, tasks=tasks)
        self.assertNotEqual(rc, 0)
        self.assertIn("access group acme.developers could not be removed from the site", H.LAST_OUTPUT)
        self.assertIn(self.dev, self.site.ks.groups)
        self.assertEqual([t for t in trace if "os" in t and t["os"]["action"] == "leftover-users"], [])


    def test_a_group_name_over_the_sites_limit_does_not_block_the_delete(self):
        """Gate part 2 G-1: a recorded access group whose name is longer than the site allows (it was refused, so it
        never existed) is "not found": the delete goes on to the user sweep; the site is not asked by that name."""
        long = "acme." + "r" * 60
        sweep = by_name(load(MAIN)[0]["block"], "Delete the cluster users left in the project")
        tasks = [{"ansible.builtin.include_tasks": "{{ role_path }}/tasks/access_down.yml"}, sweep]
        extra = {"os_account_clouds_yaml": self.site.account, "fake_clouds_yaml": self.site.account,
                 "cloudoso_cr_name": "web", "cloudoso_entity_for_status": "acme",
                 "oso_project_domain": "tenants", "oso_check": {"project_exists": True, "project_id": self.pid},
                 "oso_access": {"signInClient": "", "groupDomain": "tenants",
                                "groups": [{"group": "acme.developers"}], "removing": [long]}}
        rc, trace, out = H.run("cloudoso_teardown", "main.yml", extra, fakes=O.FAKES, tasks=tasks)
        self.assertEqual(rc, 0, out)
        self.assertNotIn("could not be removed from the site", H.LAST_OUTPUT)
        self.assertIn("leftover-users", [t["os"]["action"] for t in trace if "os" in t])
        self.assertEqual(self.site.ks.long_name_lookups, [])

if __name__ == "__main__":
    unittest.main()
