"""An environment's access groups may read the environment's own object on the services cluster (gate part 2, G-5).
The real cloudoso_provision/tasks/reader_access.yml and the real common/tasks/services_signin_groups.yml run in
ansible-playbook (po_harness.py: its kubernetes.core stand-in answers the Rbac list and records what is written)
against a sign-in directory over HTTPS (fake_keycloak.py) with the realm automation credentials in a Vault over
HTTP: the services sign-in client gets <entity>.<rbac> owned by the environment; the Role names this environment
only (get, list, watch on cloudosos), the RoleBinding names the groups, both owned by the CloudOSO. A group that
leaves spec.toolRbac leaves the subjects and the client role; the environment's delete leaves every client role.
Names are examples."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import po_harness as H  # noqa: E402
import osoui_roles as O  # noqa: E402
from play_helper import by_name, load  # noqa: E402

NS = "entity-acme"
UID = "0a1b2c3d-cloudoso-uid"
RBACS = [{"metadata": {"name": "developers"}, "status": {"group": "/acme/developers"}},
         {"metadata": {"name": "auditors"}, "status": {"group": "/acme/auditors"}},
         {"metadata": {"name": "newcomers"}, "status": {}}]
TEARDOWN = os.path.join(O.ROOT, "eda", "rulebooks", "roles", "cloudoso_teardown", "tasks", "main.yml")


@unittest.skipUnless(H.HAVE_ANSIBLE, "ansible-playbook not installed")
class Readers(unittest.TestCase):
    def setUp(self):
        import fake_keycloak
        self.site = O.Site()
        self.addCleanup(self.site.stop)
        self.kc = fake_keycloak.Keycloak().start()
        self.addCleanup(self.kc.stop)
        self.kc.add_client("openshift-services")
        for g in ("/acme/developers", "/acme/auditors"):
            self.kc.add_group(g)
        self.site.vault.data["central/keycloak-automation/keycloak-sovereign-tenants-services"] = self.kc.vault_entry()
        env = mock.patch.dict(os.environ, self.site.env())
        env.start()
        self.addCleanup(env.stop)

    def k8s(self):
        return {f"Rbac/{NS}/": RBACS, "Route/services-rhbk/": [{"metadata": {"name": "kc"}, "spec": {"host": self.kc.host}}]}

    def run_readers(self, tool):
        extra = {"oso_spec": {"toolRbac": tool}, "entity_name": "acme", "cloudoso_cr_name": "web",
                 "cloudoso_cr_namespace": NS,
                 "cloudoso_cr_lookup": {"resources": [{"metadata": {"name": "web", "uid": UID}, "status": {}}]},
                 "services_api_host": "https://services.example.org", "services_bearer_token": "token-not-in-data"}
        rc, trace, out = H.run("cloudoso_provision", "reader_access.yml", extra, k8s=self.k8s(),
                               result="{'readers': oso_readers, 'note': oso_reader_note}")
        self.assertEqual(rc, 0, out)
        written = {t["definition"]["kind"]: t["definition"] for t in trace if "k8s" in t and t.get("definition")}
        return trace[-1]["result"], written

    def owners(self, name):
        r = self.kc.role("openshift-services", name)
        return None if r is None else r["attributes"].get("owner")

    def test_add_change_remove(self):
        res, written = self.run_readers({"editRbac": ["developers"], "viewRbac": ["auditors", "newcomers"]})
        self.assertEqual(res["note"], "")
        # newcomers has no directory group yet: not a reader (yet)
        self.assertEqual([r["group"] for r in res["readers"]], ["acme.developers", "acme.auditors"])
        role, binding = written["Role"], written["RoleBinding"]
        self.assertEqual(role["rules"], [{"apiGroups": ["hybridsovereign.redhat"], "resources": ["cloudosos"],
                                          "resourceNames": ["web"], "verbs": ["get", "list", "watch"]}])
        for o in (role, binding):
            self.assertEqual((o["metadata"]["name"], o["metadata"]["namespace"]), ("web-readers", NS))
            self.assertEqual(o["metadata"]["ownerReferences"], [{"apiVersion": "hybridsovereign.redhat/v1alpha1",
                                                                 "kind": "CloudOSO", "name": "web", "uid": UID}])
        self.assertEqual(binding["roleRef"], {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "web-readers"})
        self.assertEqual([s["name"] for s in binding["subjects"]], ["acme.developers", "acme.auditors"])
        self.assertEqual({s["kind"] for s in binding["subjects"]}, {"Group"})
        # the services sign-in client carries the groups, owned by this environment
        self.assertEqual(self.owners("acme.developers"), ["CloudOSO/web"])
        self.assertEqual(self.owners("acme.auditors"), ["CloudOSO/web"])
        dev_client = self.kc.clients["openshift-services"]["id"]
        self.assertEqual(self.kc.groups["acme/developers"]["mappings"][dev_client], ["acme.developers"])
        # a group that leaves the environment loses both
        res, written = self.run_readers({"editRbac": ["developers"]})
        self.assertEqual([s["name"] for s in written["RoleBinding"]["subjects"]], ["acme.developers"])
        self.assertIsNone(self.kc.role("openshift-services", "acme.auditors"))
        self.assertEqual(self.owners("acme.developers"), ["CloudOSO/web"])
        # no access group: an empty RoleBinding, no client role of this environment
        res, written = self.run_readers({})
        self.assertEqual(written["RoleBinding"]["subjects"], [])
        self.assertIsNone(self.kc.role("openshift-services", "acme.developers"))

    def test_a_client_role_another_owner_holds_stays(self):
        self.run_readers({"editRbac": ["developers"]})
        r = self.kc.role("openshift-services", "acme.developers")
        r["attributes"]["owner"] = r["attributes"]["owner"] + ["Entity/acme"]
        self.run_readers({})
        self.assertEqual(self.owners("acme.developers"), ["Entity/acme"])

    def run_delete(self, empty):
        """The delete's own project step and the readers' release, in the order of the delete (the site faked:
        check-empty answers `empty`)."""
        block = load(TEARDOWN)[0]["block"]
        tasks = [by_name(block, "Tear down the OpenStack project"),
                 by_name(block, "The environment's readers leave the services sign-in client")]
        check = {"empty": empty, "found": {} if empty else {"servers": ["vm-1"]}, "project_exists": True,
                 "project_id": "p1"}
        extra = {"cloudoso_entity_for_status": "acme", "cloudoso_cr_name": "web", "cloudoso_cr_namespace": NS,
                 "services_api_host": "https://services.example.org", "services_bearer_token": "token-not-in-data",
                 "oso_vault_path": "cloudinfra/site-a", "oso_project_domain": "tenants", "oso_project_id": "p1",
                 "oso_project_name": "acme_web", "oso_fip_ids": [], "oso_dns_provider": "", "oso_env_dns": {},
                 "oso_dns_zone_id": "", "oso_dns_project_id": "", "oso_domain": "", "oso_app_cred_ids": [],
                 "oso_access": {}, "fake_clouds_yaml": "clouds: {}",
                 "fake_os": {"check-empty": check, "leftover-users": {"deleted": []}, "teardown": {"deleted": True}}}
        real_directory = {k: v for k, v in O.FAKES.items() if k != "services_signin_groups.yml"}
        return H.run("cloudoso_teardown", "main.yml", extra, k8s=self.k8s(), fakes=real_directory, tasks=tasks)

    def test_the_environments_delete_leaves_the_client_roles_last(self):
        self.run_readers({"editRbac": ["developers"], "viewRbac": ["auditors"]})
        rc, trace, out = self.run_delete(empty=True)
        self.assertEqual(rc, 0, out)
        self.assertEqual([t["os"]["action"] for t in trace if "os" in t], ["check-empty", "leftover-users", "teardown"])
        self.assertIsNone(self.kc.role("openshift-services", "acme.developers"))
        self.assertIsNone(self.kc.role("openshift-services", "acme.auditors"))

    def test_a_refused_delete_keeps_the_readers(self):
        """Review N2: a project that still holds servers stops the delete; the access groups keep the group on the
        services cluster, so they can open the environment's page and read why."""
        self.run_readers({"editRbac": ["developers"], "viewRbac": ["auditors"]})
        rc, trace, out = self.run_delete(empty=False)
        self.assertNotEqual(rc, 0)
        self.assertIn("is not empty, kept it", H.LAST_OUTPUT)
        self.assertEqual(self.owners("acme.developers"), ["CloudOSO/web"])
        self.assertEqual(self.owners("acme.auditors"), ["CloudOSO/web"])

    def test_the_release_is_the_deletes_last_step(self):
        names = [t.get("name") for t in load(TEARDOWN)[0]["block"]]
        release = names.index("The environment's readers leave the services sign-in client")
        for before in ("Tear down the OpenStack project", "Delete the project's Vault paths"):
            self.assertLess(names.index(before), release, before)
        self.assertLess(release, names.index("Mark CloudOSO deletion complete on services cluster"))


if __name__ == "__main__":
    unittest.main()
