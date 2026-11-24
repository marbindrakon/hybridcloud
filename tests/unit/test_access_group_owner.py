"""Refuse, never adopt: the access groups in the sign-in directory and the realm automation clients.

rbac_provision/tasks/group_owner.yml decides whose the tenant's group /<tenant> and the access group
/<tenant>/<group> are, for every state the directory and the object's status can be in (what exists × whose mark ×
what status recorded). rbac_teardown deletes only this tenant's groups. rbacconfig_provision refuses a realm
automation client it did not make. The roles of the access kinds check names against the rule before they make
anything (reason NameNotAllowed).
"""
import itertools
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_cases, walk  # noqa: E402

ROLES = os.path.join(ROOT, "eda", "rulebooks", "roles")
OWNER = os.path.join(ROLES, "rbac_provision", "tasks", "group_owner.yml")
TEARDOWN = os.path.join(ROLES, "rbac_teardown", "tasks", "main.yml")
CLIENT = os.path.join(ROLES, "rbacconfig_provision", "tasks", "automation_client.yml")

TENANT = "acme"
# the directory: (found, entity attribute, id)
PARENTS = {"absent": (False, [], ""), "ours": (True, [TENANT], "p1"), "other": (True, ["acme-corp"], "p1"),
           "unmarked": (True, [], "p1")}
CHILDREN = {"absent": (False, [], ""), "recorded": (True, [], "c1"), "ours": (True, [TENANT], "c2"),
            "other": (True, ["acme-corp"], "c3"), "unmarked": (True, [], "c4")}
RECORDED = {"none": ("", []), "this": ("c1", []), "sibling": ("", ["/acme/devs"]),
            "foreign": ("", ["/acme-corp/devs"])}


def expected(parent, child, recorded):
    """The rule in words: ours = our mark, or (access group) the id recorded in status; anything else existing is
    refused; the tenant's group from before the mark is ours when one of its access groups is."""
    child_ours = child == "ours" or (child == "recorded" and recorded == "this")
    c = "create" if child == "absent" else ("use" if child_ours else "refuse")
    if parent == "absent":
        p = "create"
    elif parent == "ours":
        p = "use"
    elif parent == "other":
        p = "refuse"
    else:
        p = "mark" if child_ours or recorded == "sibling" else "refuse"
    return {"parent": p, "child": c}


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class GroupOwner(unittest.TestCase):
    def test_every_state(self):
        cases, want = [], []
        for p, c, r in itertools.product(PARENTS, CHILDREN, RECORDED):
            if p == "absent" and c != "absent":
                continue                     # an access group lives in the tenant's group
            pf, pe, _ = PARENTS[p]
            cf, ce, cid = CHILDREN[c]
            rid, paths = RECORDED[r]
            cases.append({"rgo_parent_found": pf, "rgo_parent_entity": pe, "rgo_child_found": cf,
                          "rgo_child_entity": ce, "rgo_child_id": cid, "rgo_recorded_id": rid,
                          "rgo_recorded_paths": paths, "label": "%s/%s/%s" % (p, c, r)})
            want.append(expected(p, c, r))
        step = dict(by_name(load(OWNER), "Decide whose the groups are"))
        step["vars"] = {k: "{{ case.%s }}" % k for k in cases[0] if k != "label"}
        got = run_cases([step], cases, "rbac_group_owner", extra_vars={"entity_name": TENANT})
        self.assertEqual(len(got), len(cases))
        for case, g, w in zip(cases, got, want):
            self.assertEqual(g, w, case["label"])
        # what the job then does: refuse when either is refused; nothing else is ever taken over
        self.assertIn({"parent": "refuse", "child": "refuse"}, got)
        self.assertNotIn({"parent": "use", "child": "use"},
                         [g for c, g in zip(cases, got) if c["label"].split("/")[1] in ("other", "unmarked")])

    def test_refusal_names_the_group_and_no_other_tenant(self):
        block = by_name(load(OWNER), "Refuse a group that is not this tenant's")
        fail = by_name(block["block"], "Stop before anything is made in the sign-in directory")
        self.assertNotIn("acme-corp", fail["ansible.builtin.fail"]["msg"])
        self.assertIn("rbac_failure_reason: NameTakenOnSite",
                      "rbac_failure_reason: " + by_name(block["block"], "Record the reason")[
                          "ansible.builtin.set_fact"]["rbac_failure_reason"])

    def test_nothing_is_made_before_the_decision(self):
        names = [t.get("name") for t in walk(load(os.path.join(ROLES, "rbac_provision", "tasks", "main.yml")))]
        self.assertLess(names.index("Check the names against the rule for names"),
                        names.index("Decide whose the groups are"))
        self.assertLess(names.index("Decide whose the groups are"), names.index("Create parent entity group when missing"))
        for t in walk(load(OWNER)):
            uri = t.get("ansible.builtin.uri")
            if uri:
                self.assertEqual(uri.get("method", "GET"), "GET", t.get("name"))


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class Teardown(unittest.TestCase):
    def test_only_this_tenants_access_group_is_deleted(self):
        step = dict(by_name(load(TEARDOWN), "Decide whether the access group is this tenant's"))
        step["vars"] = {"child_delete_lookup": "{{ case.lookup }}", "child_delete_read": "{{ case.read }}",
                        "rbac_cr_lookup": "{{ case.cr }}"}
        rec = {"resources": [{"status": {"groupId": "c1"}}]}
        none = {"resources": [{"status": {}}]}
        gone = {"resources": []}
        found = {"status": 200, "json": {"id": "c1"}}
        cases = [
            {"lookup": {"status": 404}, "read": {}, "cr": rec},                                         # absent
            {"lookup": found, "read": {"status": 200, "json": {"id": "c1", "attributes": {}}}, "cr": rec},   # recorded
            {"lookup": found, "read": {"status": 200, "json": {"id": "c1", "attributes": {"entity": [TENANT]}}},
             "cr": none},                                                                                 # marked
            {"lookup": found, "read": {"status": 200, "json": {"id": "c9", "attributes": {}}}, "cr": rec},   # unmarked
            {"lookup": found, "read": {"status": 200, "json": {"id": "c9", "attributes": {"entity": ["acme-corp"]}}},
             "cr": rec},                                                                                  # another's
            {"lookup": found, "read": {"status": 200, "json": {"id": "c9", "attributes": {}}}, "cr": gone},  # no record
        ]
        got = run_cases([step], cases, "rbac_child_ours | bool", extra_vars={"entity_name": TENANT})
        self.assertEqual(got, [False, True, True, False, False, False])

    def test_the_tenants_group_only_with_its_mark_and_empty(self):
        t = by_name(load(TEARDOWN), "Delete parent entity group from Keycloak")
        when = " ".join(t["when"])
        self.assertIn("== [entity_name]", when)
        self.assertIn("(parent_delete_children.json | default([1])) | length == 0", when)


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class AutomationClient(unittest.TestCase):
    def test_a_client_this_job_did_not_make_is_refused(self):
        block = by_name(load(CLIENT), "Refuse a {{ rac.purpose }} client this job did not make")
        cond = " and ".join("(%s)" % w for w in block["when"])
        step = {"ansible.builtin.set_fact": {"refused": "{{ %s }}" % cond},
                "vars": {"_rac_lookup": "{{ case.lookup }}", "_rac_client_id": "example-identity",
                         "_rac_recorded": "{{ case.recorded | default('') }}",
                         "_rac_description": "Realm automation (membership) for RbacConfig example-identity"}}
        mine = "Realm automation (membership) for RbacConfig example-identity"
        hand = {"json": [{"clientId": "example-identity", "description": "made by hand"}]}
        bare = {"json": [{"clientId": "example-identity"}]}
        cases = [{"lookup": {"json": []}},
                 {"lookup": {"json": [{"clientId": "example-identity", "description": mine}]}},
                 {"lookup": hand},
                 {"lookup": bare},
                 # the client this RbacConfig's status records is its own whatever its description (an earlier
                 # release recorded it and wrote none); a record of another client's id is no proof
                 {"lookup": hand, "recorded": "example-identity"},
                 {"lookup": bare, "recorded": "example-identity"},
                 {"lookup": bare, "recorded": "example-identity-clients"}]
        self.assertEqual(run_cases([step], cases, "refused | bool"), [False, False, True, True, False, False, True])

    def test_the_record_is_read_from_this_objects_status(self):
        name = by_name(load(CLIENT), "Name the {{ rac.purpose }} client")
        recorded = name["ansible.builtin.set_fact"]["_rac_recorded"]
        self.assertIn("realmAutomation", recorded)
        self.assertIn("rac.purpose ~ 'ClientId'", recorded)
        step = {"ansible.builtin.set_fact": {"got": recorded}}
        st = {"realmAutomation": {"membershipClientId": "example-identity", "clientsClientId": "example-identity-clients"}}
        cases = [{"cr": {"resources": [{"status": st}]}, "purpose": "membership"},
                 {"cr": {"resources": [{"status": st}]}, "purpose": "clients"},
                 {"cr": {"resources": [{"status": {}}]}, "purpose": "membership"},
                 {"cr": {"resources": [{}]}, "purpose": "clients"}]
        step["vars"] = {"rbac_config_cr": "{{ case.cr }}", "rac": {"purpose": "{{ case.purpose }}"}}
        got = [g.strip() for g in run_cases([step], cases, "got")]
        self.assertEqual(got, ["example-identity", "example-identity-clients", "", ""])


class NamesBeforeAnything(unittest.TestCase):
    """Each job of the access kinds checks the names it uses before its first change."""
    FIRST_CHANGE = {"entity_provision": "Create entity namespace with labels",
                    "project_provision": "Grant the Project's access groups",
                    "team_provision": "Grant the Team's access groups",
                    "assignment_provision": "Provision via ACM Policy on central cluster",
                    "rbac_provision": "Create parent entity group when missing"}
    REASON_VAR = {"entity_provision": "entity_failure_reason", "project_provision": "project_failure_reason",
                  "team_provision": "team_failure_reason", "assignment_provision": "assignment_failure_reason",
                  "rbac_provision": "rbac_failure_reason"}

    def test_order_and_reason(self):
        for role, first in self.FIRST_CHANGE.items():
            path = os.path.join(ROLES, role, "tasks", "main.yml")
            tasks = load(path)
            names = [t.get("name") for t in walk(tasks)]
            self.assertLess(names.index("Check the names against the rule for names"), names.index(first), role)
            check = by_name(tasks, "Check the names against the rule for names")
            self.assertTrue(check["ansible.builtin.include_tasks"].endswith("common/tasks/name_check.yml"), role)
            refuse = by_name(tasks, "Refuse a name the rule does not allow")
            self.assertEqual(by_name(refuse["block"], "Record the reason")["ansible.builtin.set_fact"],
                             {self.REASON_VAR[role]: "NameNotAllowed"}, role)
            with open(path) as fh:
                self.assertIn("%s | default(" % self.REASON_VAR[role], fh.read(), role)

    @unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
    def test_assignment_checks_its_team_and_app_projects(self):
        check = by_name(load(os.path.join(ROLES, "assignment_provision", "tasks", "main.yml")),
                        "Check the names against the rule for names")
        step = {"ansible.builtin.set_fact": {"items": check["vars"]["name_check_items"]},
                "vars": {"assignment_entity": "acme", "assignment_cr_name": "web-on-prod",
                         "assignment_team": "{{ case.team }}", "assignment_projects": "{{ case.projects }}"}}
        got = run_cases([step], [{"team": "devs", "projects": ["api", "Web.UI"]}, {"team": "", "projects": []}],
                        "items")
        self.assertEqual(got[0], [{"value": "acme", "what": "tenant"},
                                  {"value": "web-on-prod", "what": "cluster assignment"},
                                  {"value": "devs", "what": "team"}, {"value": "api", "what": "app project"},
                                  {"value": "Web.UI", "what": "app project"}])
        self.assertEqual(len(got[1]), 2)


if __name__ == "__main__":
    unittest.main()
