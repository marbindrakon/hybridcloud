"""Names of the platform's own services: the registry organization, the automation organization and the secrets
store's sign-in client (eda/rulebooks/roles/quayorg_*, aaporg_*, vault_*; eda/common/tasks/keycloak_client_ensure.yml).

What must hold:
  - the names come from the names function, <tenant>_<name> and vault_<tenant>_<name>, so the two tenants of the
    pair acme-corp + web and acme + corp-web get two names (today's hyphen join gave them one);
  - a name the status records is kept, and the teardown reads it from status (it never rebuilds it);
  - an organization or a sign-in client is this object's only when this object's status records it (the automation
    organization by the gateway's id, the registry organization by its name, the sign-in client by its id); with
    nothing recorded the job creates, and something of the wanted name is refused (NameTakenOnSite) whatever it
    carries: the registry organization's address and the automation organization's description are written for
    people and decide nothing. The decisions are enumerated over (what exists) x (what status records) x (what the
    object carries), with the release before's objects (recorded name, old fixed address, old hyphen name) and a
    forged mark with nothing recorded;
  - the automation organization's sign-in maps are found by the ids its status records (or by their exact names
    pointing at its organization), never by the beginning of a name.
Names are examples.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(__file__))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_cases, walk  # noqa: E402
from test_shc_names import names  # noqa: E402

ROLES = os.path.join(ROOT, "eda", "rulebooks", "roles")
QUAY_P = os.path.join(ROLES, "quayorg_provision", "tasks", "main.yml")
QUAY_T = os.path.join(ROLES, "quayorg_teardown", "tasks", "main.yml")
AAP_P = os.path.join(ROLES, "aaporg_provision", "tasks", "main.yml")
AAP_MAPS = os.path.join(ROLES, "aaporg_provision", "tasks", "teams_and_roles.yml")
AAP_T = os.path.join(ROLES, "aaporg_teardown", "tasks", "main.yml")
VAULT_OIDC = os.path.join(ROLES, "vault_provision", "tasks", "configure_oidc.yml")
VAULT_T = os.path.join(ROLES, "vault_teardown", "tasks", "main.yml")
KCE = os.path.join(ROOT, "eda", "common", "tasks", "keycloak_client_ensure.yml")
SSG = os.path.join(ROOT, "eda", "common", "tasks", "services_signin_groups.yml")
KV_ENGINE = os.path.join(ROLES, "vaultkv_provision", "tasks", "kv_engine.yml")
KV_T = os.path.join(ROLES, "vaultkv_teardown", "tasks", "main.yml")
TOOL_T = {"aapconfig": os.path.join(ROLES, "aapconfig_teardown", "tasks", "main.yml"),
          "quayconfig": os.path.join(ROLES, "quayconfig_teardown", "tasks", "main.yml")}

PAIRS = [("acme-corp", "web"), ("acme", "corp-web")]


def text(path):
    with open(path) as f:
        return f.read()


class TwoTenants(unittest.TestCase):
    """The pair that gave one name with the hyphen join gets two names."""

    def test_each_kind_gives_the_pair_two_names(self):
        for kind, extra in (("registry-organization", []), ("automation-organization", []),
                            ("identity-client", ["vault"])):
            got = [names.name(kind, extra + list(p))["name"] for p in PAIRS]
            self.assertNotEqual(got[0], got[1], kind)
            self.assertTrue(all(names.name(kind, extra + list(p))["exact"] for p in PAIRS), kind)
        self.assertEqual(names.name("registry-organization", list(PAIRS[0]))["name"], "acme-corp_web")
        self.assertEqual(names.name("registry-organization", list(PAIRS[1]))["name"], "acme_corp-web")
        self.assertEqual(names.name("identity-client", ["vault"] + list(PAIRS[0]))["name"], "vault_acme-corp_web")
        self.assertEqual(names.name("identity-client", ["vault"] + list(PAIRS[1]))["name"], "vault_acme_corp-web")

    def test_todays_hyphen_join_gave_one_name(self):
        """The join the roles had: both tenants of the pair reached one object."""
        self.assertEqual("-".join(PAIRS[0]), "-".join(PAIRS[1]))
        self.assertEqual("vault-" + "-".join(PAIRS[0]), "vault-" + "-".join(PAIRS[1]))

    def test_the_longest_names_fit_their_platforms(self):
        """Tenant and name at the rule's 30 characters: exact, and within each platform's limit read in its source
        (registry 255, automation 512, sign-in client ids: only a blank id is refused)."""
        t, n = "t" + "x" * 29, "n" + "y" * 29
        for kind, extra, limit in (("registry-organization", [], 255), ("automation-organization", [], 512),
                                   ("identity-client", ["vault"], 255)):
            got = names.name(kind, extra + [t, n])
            self.assertTrue(got["exact"], kind)
            self.assertLessEqual(len(got["name"]), limit)

    def test_the_registry_accepts_the_name(self):
        """Quay's rule for a namespace (util/validation.py VALID_USERNAME_REGEX, 2-255 characters)."""
        import re
        quay = re.compile(r"^([a-z0-9]+(?:[._-][a-z0-9]+)*)$")
        for p in PAIRS + [("t" + "x" * 29, "n" + "y" * 29), ("a", "b")]:
            got = names.name("registry-organization", list(p))["name"]
            self.assertTrue(quay.match(got) and 2 <= len(got) <= 255, got)
            # the contact address <organization>@<domain> keeps a local part of at most 64 characters
            self.assertLessEqual(len(got), 64)

    def test_the_roles_join_nothing_by_hand(self):
        self.assertNotIn("{{ entity_name }}-{{ quayorg_cr_name }}", text(QUAY_P))
        self.assertNotIn("{{ entity_name }}-{{ aaporg_cr_name }}", text(AAP_P))
        self.assertNotIn("vault-{{ entity_name }}-{{ vault_cr_name }}", text(VAULT_OIDC))
        for path, kind in ((QUAY_P, "registry-organization"), (AAP_P, "automation-organization"),
                           (VAULT_OIDC, "identity-client")):
            self.assertIn("kind: %s" % kind, text(path), path)


class TeardownsReadStatus(unittest.TestCase):
    def test_no_teardown_rebuilds_the_name(self):
        for path in (QUAY_T, AAP_T, VAULT_T):
            body = text(path)
            self.assertNotIn("fin_entity_name ~ '-'", body, path)
            self.assertNotIn("status.entity | default('') }}-", body, path)
        self.assertIn(".orgName", text(QUAY_T))
        self.assertIn(".orgName", text(AAP_T))
        self.assertIn("status.oidcClientId", text(VAULT_T))

    def test_no_delete_by_a_names_beginning(self):
        for path in (AAP_T, AAP_MAPS):
            body = text(path)
            self.assertNotIn("name__startswith", body, path)
            self.assertNotIn("_prefix_re", body, path)


# ---- the decisions, enumerated --------------------------------------------------------------------------------

WANTED = "acme-corp_web"                    # the names function's name for tenant acme-corp, object web
OLD_HYPHEN = "acme-corp-web"                # the name the release before gave it (hyphen join)
DOMAIN = "example.org"
# what a registry organization can carry as its contact address
ADDRESSES = {"platform's": "%s@" + DOMAIN,
             "release before's": "%s@sovereign.local",     # the fixed address the release before wrote
             "none": "",
             "someone's": "someone@example.net",
             "forged": WANTED + "@" + DOMAIN}            # any registry user who makes an organization chooses it


def _run_quay(cases):
    pre = [{"ansible.builtin.set_fact": {"_org_existing": "{{ case.existing }}", "quayorg_recorded_name": "{{ case.recorded }}",
                                         "org_name": "{{ case.recorded or '%s' }}" % WANTED}}]
    return run_cases(pre + [by_name(load(QUAY_P), "Decide: ours, create or refuse")], cases, "quayorg_decision | trim")


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class Decisions(unittest.TestCase):
    """(exists on the platform) x (what status records) x (what the object carries) -> ours | adopt | create | refuse."""

    def test_registry_organization(self):
        cases, want = [], []
        for recorded in ("", WANTED, OLD_HYPHEN):
            name = recorded or WANTED
            cases.append({"existing": {"status": 404}, "recorded": recorded})
            want.append("create")
            for kind, address in ADDRESSES.items():
                email = address % name if "%s" in address else address
                cases.append({"existing": {"status": 200, "json": {"name": name, "email": email}}, "recorded": recorded})
                # recorded: ours whatever its address (the release before's included); nothing recorded: refused,
                # the forged platform address included
                want.append("ours" if recorded else "refuse")
        self.assertEqual(_run_quay(cases), want)

    def test_the_mail_domain_is_needed_only_to_create(self):
        step = by_name(load(QUAY_P), "Require the registry organizations' mail domain")
        for decision in ("ours", "refuse"):
            self.assertEqual(run_cases([step], [{}], "'passed'", extra_vars={
                "quayorg_decision": decision, "quayorg_mail_domain": "", "org_name": WANTED}), ["passed"])
        with self.assertRaises(AssertionError) as e:
            run_cases([step], [{}], "'passed'", extra_vars={"quayorg_decision": "create", "quayorg_mail_domain": "",
                                                              "org_name": WANTED})
        self.assertIn("REGISTRY_ORG_MAIL_DOMAIN", str(e.exception))

    def test_registry_organization_is_never_rewritten_or_marked(self):
        """Nothing writes the address of an organization that exists: no PUT on the organization."""
        for t in walk(load(QUAY_P)):
            uri = t.get("ansible.builtin.uri") or {}
            if str(uri.get("method", "GET")).upper() == "PUT":
                self.assertIn("/team/", str(uri.get("url")), t.get("name"))

    def test_automation_organization(self):
        mark = "(AAPOrg entity-acme-corp/web)"
        descriptions = {"platform's": "Automation organization web of tenant acme-corp, made by the platform " + mark,
                        "release before's": "Sovereign tenant org for entity acme-corp", "none": "",
                        "another object's": "made by the platform (AAPOrg entity-acme/corp-web)",
                        "forged": "anything " + mark}
        by_id = {"none recorded": None, "there": {"status": 200}, "gone": {"status": 404}}
        cases, want = [], []
        for recorded_name in ("", WANTED, OLD_HYPHEN):
            org = recorded_name or WANTED
            for id_state, answer in by_id.items():
                if id_state != "none recorded" and not recorded_name:
                    continue                                    # an id is recorded together with the name
                for found in [None] + list(descriptions):
                    results = [] if found is None else [{"id": 7, "name": org, "description": descriptions[found]}]
                    rec_id = "" if id_state == "none recorded" else "7" if id_state == "there" else "9"
                    by = {} if answer is None else dict(answer, json={"id": int(rec_id), "name": "renamed-by-someone",
                                                                      "description": "edited"})
                    cases.append({"by_id": by, "results": results, "rid": rec_id, "rname": recorded_name, "org": org})
                    if id_state == "there":
                        want.append("ours")         # by its id, whatever its name or description now
                    elif found is None:
                        want.append("create")
                    elif id_state == "none recorded" and recorded_name:
                        want.append("adopt")        # the release before recorded only the name: taken once, id recorded
                    else:
                        want.append("refuse")       # nothing recorded (forged mark included), or not the recorded id
        pre = [{"ansible.builtin.set_fact": {"_gw_org_by_id": "{{ case.by_id }}",
                                             "_gw_org_lookup": {"json": {"results": "{{ case.results }}"}},
                                             "aaporg_recorded_id": "{{ case.rid }}",
                                             "aaporg_recorded_name": "{{ case.rname }}", "org_name": "{{ case.org }}"}}]
        steps = [by_name(load(AAP_P), "Decide: ours, adopt, create or refuse")]
        self.assertEqual(run_cases(pre + steps, cases, "aaporg_decision | trim"), want)

    def test_a_renamed_organization_is_found_by_its_id_and_named_back(self):
        """An organization's administrator renames ours: the provision finds it by id and sets its name back; another
        tenant's object whose wanted name is the new name finds it unrecorded and refuses it."""
        steps = [by_name(load(AAP_P), "Decide: ours, adopt, create or refuse")]
        pre = [{"ansible.builtin.set_fact": {
            "_gw_org_by_id": {"status": 200, "json": {"id": 7, "name": "globex_ci", "description": "(AAPOrg entity-globex/ci)"}},
            "_gw_org_lookup": {"json": {"results": []}}, "aaporg_recorded_id": "7", "aaporg_recorded_name": WANTED,
            "org_name": WANTED}}]
        got = run_cases(pre + steps, [{}], "{'d': aaporg_decision | trim, 'name': aaporg_found.name}")[0]
        self.assertEqual(got, {"d": "ours", "name": "globex_ci"})
        patch = by_name(load(AAP_P), "Set the name and the description of this organization")
        self.assertEqual(patch["ansible.builtin.uri"]["body"]["name"], "{{ org_name }}")
        pre = [{"ansible.builtin.set_fact": {
            "_gw_org_by_id": {}, "aaporg_recorded_id": "", "aaporg_recorded_name": "", "org_name": "globex_ci",
            "_gw_org_lookup": {"json": {"results": [{"id": 7, "name": "globex_ci",
                                                     "description": "x (AAPOrg entity-globex/ci)"}]}}}}]
        self.assertEqual(run_cases(pre + steps, [{}], "aaporg_decision | trim"), ["refuse"])

    def test_sign_in_client(self):
        owner = "Vault/entity-acme-corp/web"
        found = {"none": [],
                 "ours": [{"id": "u1", "clientId": "vault_acme-corp_web",
                           "attributes": {"hybridsovereign.redhat/owner": owner}}],
                 "unmarked": [{"id": "u1", "clientId": "vault_acme-corp_web", "attributes": {}}],
                 "other": [{"id": "u1", "clientId": "vault_acme-corp_web",
                            "attributes": {"hybridsovereign.redhat/owner": "Vault/entity-acme/corp-web"}}]}
        cases, want = [], []
        for state, clients in found.items():
            for recorded in (False, True):
                cases.append({"clients": clients, "recorded": recorded})
                if state == "none":
                    want.append("create")
                elif not recorded:
                    want.append("refuse")           # not recorded: refused, our own mark included
                else:
                    want.append({"ours": "ours", "unmarked": "adopt"}.get(state, "refuse"))
        pre = [{"ansible.builtin.set_fact": {"_kce_lookup": {"json": "{{ case.clients }}"},
                                             "kce_recorded": "{{ case.recorded }}"}}]
        steps = [by_name(load(KCE), "Decide: ours, adopt, create or refuse ({{ kce_client }})")]
        got = run_cases(pre + steps, cases, "kce_decision | trim",
                        extra_vars={"kce_client": "vault_acme-corp_web", "kce_owner": owner})
        self.assertEqual(got, want)
        # without an owner (a caller that marks nothing) the client is used as before
        got = run_cases(pre + steps, [{"clients": found["other"][:], "recorded": False}], "kce_decision | trim",
                        extra_vars={"kce_client": "vault_acme-corp_web", "kce_owner": ""})
        self.assertEqual(got, ["ours"])

    def test_the_callers_say_what_their_status_records(self):
        for path in (VAULT_OIDC, os.path.join(ROLES, "aapconfig_provision", "tasks", "main.yml"),
                     os.path.join(ROLES, "quayconfig_provision", "tasks", "main.yml")):
            body = text(path)
            self.assertNotIn("kce_adopt_unmarked", body, path)
            self.assertIn("kce_recorded:", body, path)
        self.assertNotIn("kce_adopt_unmarked", text(KCE))

    def test_a_teardown_whose_recorded_connection_is_gone(self):
        """(organization recorded | not) x (the recorded connection: there | gone | not readable): with something
        recorded and no connection the teardown stops, names what it left, and never says deleted (the rescue keeps
        the finalizer with TeardownFailed)."""
        for path, kind, conn_var, step in ((QUAY_T, "QuayOrg", "_quay_config", "Stop when the recorded registry connection is gone"),
                                           (AAP_T, "AAPOrg", "_aap_config", "Stop when the recorded automation connection is gone")):
            stop = by_name(load(path), step)
            probe = {"ansible.builtin.set_fact": {"_stops": "{{ %s }}" % stop["when"], "_msg": stop["ansible.builtin.fail"]["msg"]}}
            cases = [{"org": WANTED, "conn": {"resources": [{"metadata": {"name": "c"}}]}},
                     {"org": WANTED, "conn": {"resources": []}},
                     {"org": WANTED, "conn": {"failed": True}},
                     {"org": "", "conn": {"resources": []}}]
            pre = [{"ansible.builtin.set_fact": {conn_var: "{{ case.conn }}", "fin_org_name": "{{ case.org }}",
                                                 "fin_org_id": ""}}]
            got = run_cases(pre + [probe], cases, "{'stops': _stops | bool, 'msg': _msg}",
                            extra_vars={"quayorg_cr_name": "web", "aaporg_cr_name": "web",
                                        "quayorg_spec": {"quayConfig": "registry-a"}, "aaporg_spec": {"aapConfig": "aap-a"}})
            self.assertEqual([g["stops"] for g in got], [False, True, True, False], kind)
            self.assertIn("no longer exists", got[1]["msg"])
            self.assertIn("could not be read", got[2]["msg"])
            self.assertIn(WANTED + " was left in place", got[1]["msg"])
            self.assertNotIn("deleted by", got[1]["msg"])
            names_ = [t.get("name") for t in load(path)[0]["block"]]
            self.assertLess(names_.index(step), names_.index("Get current timestamp for status"), kind)

    def test_teardowns_delete_what_status_records(self):
        # registry: the recorded organization goes whatever its address; nothing recorded, nothing is read or deleted
        pre = [{"ansible.builtin.set_fact": {"_fin_org": "{{ case }}"}}]
        steps = [by_name(load(QUAY_T), "Decide: delete or gone")]
        cases = [{"status": 404}] + [{"status": 200, "json": {"email": (a % WANTED) if "%s" in a else a}}
                                     for a in ADDRESSES.values()]
        got = run_cases(pre + steps, cases, "quayorg_fin_decision | trim", extra_vars={"fin_org_name": WANTED})
        self.assertEqual(got, ["gone"] + ["delete"] * len(ADDRESSES))
        block = by_name(load(QUAY_T), "Delete Quay org")
        self.assertIn("fin_org_name | length > 0", block["when"])
        self.assertNotIn("quayorg_mail_domain", text(QUAY_T))
        # automation organization: (recorded id, recorded name) x (what the gateway answers)
        steps = [by_name(load(AAP_T), "Decide: delete or gone")]
        org = {"id": 7, "name": "renamed-by-someone", "description": "edited by its administrators"}
        cases = [{"id": "7", "by_id": {"status": 200, "json": org}, "results": []},
                 {"id": "7", "by_id": {"status": 404}, "results": [{"id": 8, "name": WANTED, "description": ""}]},
                 {"id": "", "by_id": {}, "results": [{"id": 7, "name": OLD_HYPHEN, "description": "old"}]},
                 {"id": "", "by_id": {}, "results": [{"id": 7, "name": OLD_HYPHEN + "-ci", "description": "x"}]},
                 {"id": "", "by_id": {}, "results": []}]
        pre = [{"ansible.builtin.set_fact": {"fin_org_id": "{{ case.id }}", "_gw_org_by_id": "{{ case.by_id }}",
                                             "_gw_org_lookup": {"json": {"results": "{{ case.results }}"}}}}]
        got = run_cases(pre + steps, cases, "{'d': aaporg_fin_decision | trim, 'id': aaporg_fin_org.id | default(0)}",
                        extra_vars={"fin_org_name": OLD_HYPHEN})
        self.assertEqual(got, [{"d": "delete", "id": 7}, {"d": "gone", "id": 0}, {"d": "delete", "id": 7},
                               {"d": "gone", "id": 0}, {"d": "gone", "id": 0}])
        # secrets store's sign-in client: the recorded id, with this Vault's mark or none; another Vault's mark (only
        # the platform writes client attributes) is left
        owner = "Vault/entity-acme-corp/web"
        pre = [{"ansible.builtin.set_fact": {"kc_client_lookup_fin": {"json": "{{ case }}"}}}]
        steps = [by_name(load(VAULT_T), "Decide: delete or leave")]
        cases = [[], [{"id": "u", "clientId": "vault_acme-corp_web", "attributes": {"hybridsovereign.redhat/owner": owner}}],
                 [{"id": "u", "clientId": "vault_acme-corp_web"}],
                 [{"id": "u", "clientId": "vault_acme-corp_web",
                   "attributes": {"hybridsovereign.redhat/owner": "Vault/entity-acme/corp-web"}}]]
        got = run_cases(pre + steps, cases, "vault_fin_decision | trim",
                        extra_vars={"vault_fin_oidc_client_id": "vault_acme-corp_web", "vault_fin_owner": owner})
        self.assertEqual(got, ["gone", "delete", "delete", "leave"])


def _tasks_in_order(path):
    return [t for t in walk(load(path)) if not any(t.get(k) for k in ("block", "rescue", "always"))]


class RecordedWithTheCreate(unittest.TestCase):
    """The name or id reaches status in the run that made the object, right after the create: between the create
    and the record only the refusal of a failed create (no other write)."""

    def check(self, path, create, record):
        tasks = _tasks_in_order(path)
        names = [t.get("name") for t in tasks]
        i, j = names.index(create), names.index(record)
        self.assertLess(i, j, path)
        for t in tasks[i + 1:j]:
            self.assertFalse(writes(t) and "patch_cr_status" not in str(t), "%s: %s" % (path, t.get("name")))

    def test_each_object(self):
        self.check(QUAY_P, "Create organization in Quay", "Record the organization's name")
        self.check(AAP_P, "Create organization via gateway API", "Record the organization's name and id")
        self.check(AAP_MAPS, "Create the authenticator maps", "Record this organization's maps in its status")
        self.check(VAULT_OIDC, "Ensure the Keycloak sign-in client of this Vault", "Record the sign-in client's id")
        for role, ensure in (("aapconfig", "Ensure the Keycloak sign-in client for AAP"),
                             ("quayconfig", "Ensure the Keycloak sign-in client for Quay")):
            self.check(os.path.join(ROLES, role + "_provision", "tasks", "main.yml"), ensure,
                       "Record the sign-in client's id")


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class SignInMaps(unittest.TestCase):
    """The automation organization's maps: named by the names function; recorded ids, and exact names (today's or the
    earlier form) pointing at its organization only."""

    def run_maps(self, have, recorded, grants, status=None):
        tasks = load(AAP_MAPS)
        steps = [by_name(tasks, n) for n in ("Wanted authenticator maps of this organization", "Work out the map changes",
                                             "Work out what to create, update and delete")]
        rec = dict(status or {}, signInMaps="{{ case.recorded }}")
        pre = [{"ansible.builtin.set_fact": {"_aaporg_maps": {"json": {"results": "{{ case.have }}"}},
                                             "aaporg_recorded": rec}}]
        expr = ("{'create': aaporg_maps_create | map(attribute='name') | list, "
                "'delete': aaporg_maps_delete | map(attribute='id') | list, "
                "'foreign': aaporg_maps_foreign}")
        map_names = {"%s %s" % (g["kind"], g["rbac"]): names.name("automation-map", ["acme", "web", g["kind"], g["rbac"]])["name"]
                     for g in grants}
        return run_cases(pre + steps, [{"have": have, "recorded": recorded}], expr, extra_vars={
            "org_name": "acme_web", "aap_auth_id": "3", "aaporg_grants": grants, "aaporg_map_names": map_names})[0]

    def test_the_map_names_come_from_the_names_function(self):
        self.assertEqual(names.name("automation-map", ["acme", "web", "admin", "devs"])["name"], "acme_web_admin_devs")
        # the pair that the hand join <org>-<kind>-map-<group> made one name of gives two names
        a = names.name("automation-map", ["acme", "web", "admin", "a-admin-map-b"])["name"]
        b = names.name("automation-map", ["acme", "web-admin-map-a", "admin", "b"])["name"]
        self.assertNotEqual(a, b)
        self.assertEqual("acme_web" + "-admin-map-" + "a-admin-map-b", "acme_web-admin-map-a" + "-admin-map-" + "b")
        self.assertNotIn("-map-' ~ g", text(AAP_MAPS).split("_earlier_names")[0])
        self.assertIn("kind: automation-map", text(os.path.join(ROLES, "aaporg_provision", "tasks", "map_name.yml")))

    def test_a_longer_org_of_the_same_beginning_is_never_touched(self):
        devs = {"kind": "admin", "team": "admins", "order": 1, "rbac": "devs", "group": "acme/devs"}
        qa = {"kind": "admin", "team": "admins", "order": 1, "rbac": "qa", "group": "acme/qa"}
        have = [{"id": 1, "name": "acme_web_admin_devs", "organization": "acme_web", "team": "acme_web-admins",
                 "triggers": {"groups": {"has_or": ["/acme/devs"]}}},
                {"id": 2, "name": "acme_web_admin_ops", "organization": "acme_web", "team": "acme_web-admins",
                 "triggers": {"groups": {"has_or": ["/acme/ops"]}}},
                {"id": 3, "name": "acme_web-ci_admin_devs", "organization": "acme_web-ci",
                 "team": "acme_web-ci-admins", "triggers": {"groups": {"has_or": ["/acme/devs"]}}},
                {"id": 4, "name": "acme_web_admin_qa", "organization": "acme_web-admin-map-acme",
                 "team": "x", "triggers": {}}]
        got = self.run_maps(have, [{"id": "1", "name": "acme_web_admin_devs"},
                                   {"id": "2", "name": "acme_web_admin_ops"}], [devs])
        self.assertEqual(got["delete"], [2])            # recorded and no longer wanted
        self.assertEqual(got["create"], [])
        self.assertEqual(got["foreign"], [])
        # nothing recorded: only the exact wanted names of this organization
        got = self.run_maps(have, [], [devs, qa])
        self.assertEqual(got["delete"], [])
        self.assertEqual(got["foreign"], ["acme_web_admin_qa"])   # another organization's: not touched
        self.assertEqual(got["create"], [])

    def test_the_earlier_names_of_this_organization_are_replaced(self):
        """A map of the release before (<org>-<kind>-map-<group>) pointing at this organization goes once the new one
        is made; one of that name pointing at another organization stays."""
        devs = {"kind": "admin", "team": "admins", "order": 1, "rbac": "devs", "group": "acme/devs"}
        have = [{"id": 5, "name": "acme_web-admin-map-acme/devs", "organization": "acme_web", "team": "acme_web-admins",
                 "triggers": {"groups": {"has_or": ["/acme/devs"]}}},
                {"id": 6, "name": "acme_web-admin-map-acme/ops", "organization": "acme_web", "team": "acme_web-admins",
                 "triggers": {"groups": {"has_or": ["/acme/ops"]}}},
                {"id": 7, "name": "acme_web-admin-map-acme/devs", "organization": "other", "team": "x", "triggers": {}}]
        got = self.run_maps(have, [], [devs], status={"adminGroups": ["acme/devs", "acme/ops"]})
        self.assertEqual(got["create"], ["acme_web_admin_devs"])
        self.assertEqual(sorted(got["delete"]), [5, 6])
        self.assertEqual(got["foreign"], [])


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class AdoptionElsewhere(unittest.TestCase):
    """Client roles of a site's sign-in client, the tools' clients and the secrets store's paths."""

    def test_client_roles_of_a_sign_in_client(self):
        """(what the client holds under the wanted name) x (adopt unmarked asked) -> this entity's | refused."""
        tasks = load(SSG)
        steps = [by_name(tasks, n) for n in ("This entity's client roles and their owners",
                                             "Take the wanted client roles made before the marks",
                                             "A wanted group held by a role of another entity is that group's fault (per group)")]
        holds = {"nothing": [],
                 "ours": [{"name": "acme.devs", "attributes": {"entity": ["acme"], "owner": ["CloudVirt/prod"]}}],
                 "unmarked": [{"name": "acme.devs", "attributes": {}}],
                 "no-attributes": [{"name": "acme.devs"}],
                 "other": [{"name": "acme.devs", "attributes": {"entity": ["acme-corp"]}}]}
        cases, want = [], []
        for state, roles in holds.items():
            for adopt in (False, True):
                cases.append({"roles": roles, "adopt": adopt})
                if state == "nothing":
                    want.append({"mine": False, "foreign": False})
                elif state == "ours" or (state in ("unmarked", "no-attributes") and adopt):
                    want.append({"mine": True, "foreign": False})
                else:
                    want.append({"mine": False, "foreign": True})
        pre = [{"ansible.builtin.set_fact": {"_ssg_roles_now": {"json": "{{ case.roles }}"},
                                             "ssg_adopt_unmarked": "{{ case.adopt }}", "ssg_faults": [],
                                             "ssg_wanted_groups": ["acme.devs"]}}]
        got = run_cases(pre + steps, cases,
                        "{'mine': 'acme.devs' in ssg_entity_roles, 'foreign': ssg_faults | length > 0}",
                        extra_vars={"ssg_entity": "acme", "ssg_legacy_owner": "Entity/acme", "ssg_per_group": True,
                                    "ssg_kc": "https://kc.example.org", "ssg_realm": "tenants",
                                    "ssg_client_id": "openshift-site", "_ssg_client": {"json": [{"id": "c1"}]},
                                    "ssg_wanted_eff": [{"rbac": "devs", "path": "/acme/devs", "group": "acme.devs"}]})
        self.assertEqual(got, want)

    def test_a_secrets_path_in_use(self):
        """(create answer) x (what is mounted there) x (does status record the path)."""
        mark = "(VaultKV entity-acme/app)"
        mounted = {"kv2-ours": {"type": "kv", "options": {"version": "2"}, "description": "Secrets path app " + mark},
                   "kv2-unmarked": {"type": "kv", "options": {"version": "2"}, "description": ""},
                   "kv2-other": {"type": "kv", "options": {"version": "2"}, "description": "the team's own"},
                   "kv1": {"type": "kv", "options": {"version": "1"}, "description": ""},
                   "other-engine": {"type": "pki", "description": "Secrets path app " + mark}}
        cases, want = [{"create": 204, "existing": {}, "recorded": ""}], ["create"]
        for state, m in mounted.items():
            for recorded in ("", "app", "other"):
                cases.append({"create": 400, "existing": {"status": 200, "json": {"data": m}}, "recorded": recorded})
                # recorded and KV v2: this object's whatever the description; nothing recorded: refused, a mount
                # carrying this object's description included
                if recorded != "app" or not state.startswith("kv2"):
                    want.append("refuse")
                else:
                    want.append("ours" if state == "kv2-ours" else "adopt")
        cases.append({"create": 400, "existing": {"status": 400, "json": {"errors": ["no matching mount"]}},
                      "recorded": ""})                       # a reserved path (sys, identity, ...)
        want.append("refuse")
        pre = [{"ansible.builtin.set_fact": {"_kv_mount_create": {"status": "{{ case.create }}"},
                                             "_kv_mount_existing": "{{ case.existing }}",
                                             "kv_recorded_path": "{{ case.recorded }}"}}]
        steps = [by_name(load(KV_ENGINE), "Decide: ours, adopt, create or refuse")]
        got = run_cases(pre + steps, cases, "kv_decision | trim", extra_vars={"kv_path": "app", "kv_mark": mark})
        self.assertEqual(got, want)
        # the teardown removes the recorded path whatever its description, while a KV v2 engine is mounted there
        pre = [{"ansible.builtin.set_fact": {"_kv_fin_mount": "{{ case }}"}}]
        steps = [by_name(load(KV_T), "Decide: delete or leave")]
        cases = [{"status": 404}, {"status": 200, "json": {"data": mounted["kv2-ours"]}},
                 {"status": 200, "json": {"data": mounted["kv2-unmarked"]}},
                 {"status": 200, "json": {"data": mounted["kv2-other"]}}]
        cases.append({"status": 200, "json": {"data": mounted["other-engine"]}})
        got = run_cases(pre + steps, cases, "kv_fin_decision | trim", extra_vars={"kv_fin_path": "app",
                                                                                   "kv_fin_mark": mark})
        self.assertEqual(got, ["gone", "delete", "delete", "delete", "leave"])

    def test_tool_client_teardowns(self):
        for role, kind in (("aapconfig", "AAPConfig"), ("quayconfig", "QuayConfig")):
            owner = kind + "/sovereign-cloud-plugins/main"
            cid = ("aap" if role == "aapconfig" else "quay") + "-main"
            pre = [{"ansible.builtin.set_fact": {"_kc_client": {"json": "{{ case }}"}}}]
            steps = [by_name(load(TOOL_T[role]), "Decide: delete or leave")]
            cases = [[], [{"id": "u", "clientId": cid, "attributes": {"hybridsovereign.redhat/owner": owner}}],
                     [{"id": "u", "clientId": cid}],
                     [{"id": "u", "clientId": cid, "attributes": {"hybridsovereign.redhat/owner": "Vault/x/y"}}]]
            got = run_cases(pre + steps, cases, role + "_fin_decision | trim",
                            extra_vars={role + "_fin_client_id": cid, role + "_fin_owner": owner})
            self.assertEqual(got, ["gone", "delete", "delete", "leave"], role)

    def test_tool_clients_are_marked_and_named_by_the_names_function(self):
        for role, kind, tool in (("aapconfig", "AAPConfig", "aap"), ("quayconfig", "QuayConfig", "quay")):
            body = text(os.path.join(ROLES, role + "_provision", "tasks", "main.yml"))
            self.assertIn('kce_owner: "%s/{{ %s_cr_namespace }}/{{ %s_cr_name }}"' % (kind, role, role), body)
            self.assertNotIn("clientId=%s-{{" % tool, text(TOOL_T[role]))
            self.assertNotIn('"%s-{{' % tool, body)
            self.assertIn('kind: tool-client, parts: ["%s"' % tool, body)
            self.assertIn("status | default({}, true)).keycloakClientId", body)
        # a connection's tool client can't take the id of a realm automation client (<connection>, <connection>-clients)
        self.assertEqual(names.name("tool-client", ["aap", "x"])["name"], "aap_x")
        self.assertNotEqual(names.name("tool-client", ["aap", "x"])["name"], "aap-x")

    def test_realm_automation_clients_teardown(self):
        """The RbacConfig teardown deletes the clients its status records, with the description its provision gives,
        in the realm it recorded: (recorded) x (what the realm holds)."""
        tasks = load(os.path.join(ROLES, "rbacconfig_teardown", "tasks", "main.yml"))
        steps = [by_name(tasks, "The realm automation clients the status records")]
        expr = "{'realm': rbacconfig_fin_realm, 'clients': rbacconfig_fin_clients | map(attribute='clientId') | list}"
        got = run_cases(steps, [{}], expr, extra_vars={"shc_live_cr": {"status": {
            "realm": "tenants", "realmAutomation": {"membershipClientId": "main", "clientsClientId": "main-clients"}}}})
        self.assertEqual(got, [{"realm": "tenants", "clients": ["main", "main-clients"]}])
        got = run_cases(steps, [{}], expr, extra_vars={"shc_live_cr": {"status": {}}})
        self.assertEqual(got, [{"realm": "", "clients": []}])
        delete = by_name(tasks, "Delete the recorded clients that carry this connection's description")
        mine = {"id": "u1", "clientId": "main", "description": "Realm automation (membership) for RbacConfig main"}
        lookups = [{"item": {"purpose": "membership", "clientId": "main"}, "json": [mine]},
                   {"item": {"purpose": "clients", "clientId": "main-clients"},
                    "json": [{"id": "u2", "clientId": "main-clients", "description": "made by hand"}]}]
        steps = [{"ansible.builtin.set_fact": {"_rbacconfig_fin_lookup": {"results": "{{ case }}"}}},
                 {"ansible.builtin.set_fact": {"_deleted": delete["loop"]}}]
        cases = [[], lookups, [dict(lookups[0], json=[])],
                 [dict(lookups[0], json=[dict(mine, description="Realm automation (membership) for RbacConfig other")])]]
        got = run_cases(steps, cases, "_deleted | map(attribute='id') | list", extra_vars={"rbac_config_cr_name": "main"})
        self.assertEqual(got, [[], ["u1"], [], []])
        self.assertIn("r.item.purpose", delete["loop"])
        self.assertIn("rbac_config_cr_name", delete["loop"])
        self.assertNotIn("~ '-clients'", text(os.path.join(ROLES, "rbacconfig_teardown", "tasks", "main.yml")))


# ---- every job checks the names before it makes anything -------------------------------------------------------

PROVISIONS = {"quayorg_provision": ["main.yml"], "aaporg_provision": ["main.yml", "teams_and_roles.yml"],
              "vault_provision": ["main.yml", "deploy_resources.yml", "init_unseal.yml", "configure_oidc.yml"],
              "vaultkv_provision": ["main.yml", "kv_engine.yml", "identity.yml"],
              "quayconfig_provision": ["main.yml"], "aapconfig_provision": ["main.yml"]}


def flat(role, files):
    """The role's tasks in the order they run, include_tasks of the role's own files expanded."""
    base = os.path.join(ROLES, role, "tasks")

    def expand(tasks):
        for t in walk(tasks):
            inc = t.get("ansible.builtin.include_tasks")
            if isinstance(inc, str) and "/" not in inc and inc in files:
                yield t
                yield from expand(load(os.path.join(base, inc)))
            elif not any(t.get(k) for k in ("block", "rescue", "always")):
                yield t
    return list(expand(load(os.path.join(base, "main.yml"))))


def writes(t):
    uri = t.get("ansible.builtin.uri")
    if isinstance(uri, dict) and str(uri.get("method", "GET")).upper() in ("POST", "PUT", "PATCH", "DELETE"):
        return True
    k8s = t.get("kubernetes.core.k8s")
    return isinstance(k8s, dict) and k8s.get("state", "present") == "present"


class NameCheckFirst(unittest.TestCase):
    def test_every_provision_checks_names_before_it_makes_anything(self):
        for role, files in PROVISIONS.items():
            tasks = flat(role, files)
            check = [i for i, t in enumerate(tasks)
                     if "name_check.yml" in str(t.get("ansible.builtin.include_tasks", ""))]
            first = [i for i, t in enumerate(tasks) if writes(t)]
            self.assertTrue(check, role)
            self.assertTrue(first, role)
            self.assertLess(check[0], first[0], "%s: %s before the name check" % (role, tasks[first[0]].get("name")))

    def test_the_refusal_carries_its_reason(self):
        for role in PROVISIONS:
            body = text(os.path.join(ROLES, role, "tasks", "main.yml"))
            self.assertIn("NameNotAllowed", body, role)


# ---- lab strings and the realm ---------------------------------------------------------------------------------

OWNED = [os.path.join(dp, f) for top in ("eda/rulebooks/roles",) for dp, _, fs in os.walk(os.path.join(ROOT, top))
         for f in fs if f.endswith(".yml") and os.path.relpath(dp, os.path.join(ROOT, top)).split(os.sep)[0].split("_")[0]
         in ("vault", "vaultkv", "quayorg", "quayconfig", "aaporg", "aapconfig", "dnsconfig", "rbac", "rbacconfig")]
OWNED += [os.path.join(ROOT, "eda", "common", "tasks", f) for f in os.listdir(os.path.join(ROOT, "eda", "common", "tasks"))
          if f.startswith("keycloak_") or f in ("services_signin_groups.yml", "obtain_keycloak_realm_token.yml")]
OWNED += [os.path.join(dp, f) for dp, _, fs in os.walk(os.path.join(ROOT, "aap-config")) for f in fs if f.endswith(".yml")]
OWNED += [os.path.join(dp, f) for dp, _, fs in os.walk(os.path.join(ROOT, "charts", "charts", "workload-identity"))
          for f in fs]


class LabStrings(unittest.TestCase):
    def test_no_lab_domain_or_mail_domain(self):
        self.assertGreater(len(OWNED), 30)
        for path in OWNED:
            body = text(path)
            for word in ("shc.signal9.gg", "signal9", "sovereign.local", "eda-lab"):
                self.assertNotIn(word, body, path)

    def test_the_product_label_keys(self):
        self.assertIn("hybridsovereign.redhat/cloud-identity", text(os.path.join(ROOT, "aap-config", "controller",
                                                                                  "instance_groups.yml")))
        self.assertIn("hybridsovereign.redhat/spiffe-id", text(os.path.join(
            ROOT, "charts", "charts", "workload-identity", "templates", "serviceaccounts.yaml")))

    def test_one_source_for_the_realm(self):
        """The realm is the one whose automation clients the RbacConfig published (obtain_keycloak_realm_token.yml
        keycloak_realm_name); no file names a realm, and one place names the default RbacConfig."""
        holders = []
        for path in OWNED:
            body = text(path)
            self.assertNotIn("KEYCLOAK_REALM", body, path)
            self.assertNotIn("/realms/sovereign-tenants", body, path)
            self.assertNotIn("plugin_vault_keycloak_realm", body, path)
            if "keycloak-sovereign-tenants-services" in body:
                holders.append(os.path.basename(path))
        self.assertEqual(holders, ["obtain_keycloak_realm_token.yml"])

    def test_the_access_group_jobs_use_the_published_realm(self):
        """The realm is named once, by the RbacConfig job that makes it and publishes it; the access group jobs use
        the realm its credentials name (keycloak_realm_name), the RbacConfig teardown the realm its status records."""
        holders = sorted({os.path.relpath(p, ROLES).split(os.sep)[0] for p in OWNED
                          if "plugin_rbac_keycloak_realm" in text(p) and p.startswith(ROLES)})
        self.assertEqual(holders, ["rbacconfig_provision"])
        for role in ("rbac_provision", "rbac_teardown"):
            body = "".join(text(os.path.join(ROLES, role, "tasks", f))
                           for f in os.listdir(os.path.join(ROLES, role, "tasks")))
            self.assertIn("/admin/realms/{{ keycloak_realm_name }}/", body, role)


# ---- the secrets store's address -------------------------------------------------------------------------------

class VaultAddress(unittest.TestCase):
    def test_the_label_fits_and_the_pair_differs(self):
        import re
        label = re.compile(r"^[a-z]([-a-z0-9]*[a-z0-9])?$")
        got = [names.name("vault-route", list(p))["name"] for p in PAIRS]
        self.assertEqual(got, ["vault-acme-corp--web", "vault-acme--corp-web"])
        long_ = names.name("vault-route", ["t" + "x" * 29, "n" + "y" * 29])
        self.assertFalse(long_["exact"])
        self.assertLessEqual(len(long_["name"]), 63)
        self.assertTrue(label.match(long_["name"]))
        other = names.name("vault-route", ["t" + "x" * 29, "n" + "y" * 28 + "z"])
        self.assertNotEqual(long_["name"], other["name"])

    @unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
    def test_a_default_host_that_fits_is_kept(self):
        """The default first label vault-<name>-entity-<tenant> passes 63 characters with two names of 30."""
        role = os.path.join(ROLES, "vault_provision")
        tasks = load(os.path.join(role, "tasks", "main.yml"))
        steps = [by_name(tasks, "The address's first label when the default does not fit"),
                 by_name(tasks, "Keep the Route's subdomain")]
        cases = [{"t": "acme-corp", "v": "web"}, {"t": "t" + "x" * 29, "v": "n" + "y" * 29},
                 {"t": "t" * 24, "v": "v" * 25}, {"t": "t" * 24, "v": "v" * 26}]
        pre = [{"ansible.builtin.set_fact": {"entity_name": "{{ case.t }}", "vault_cr_name": "{{ case.v }}",
                                             "vault_instance_name": "vault-{{ case.v }}",
                                             "vault_cr_namespace": "entity-{{ case.t }}"}}]
        got = run_cases(pre + steps, cases, "vault_route_subdomain", extra_vars={"role_path": role})
        self.assertEqual(got[0], "")
        self.assertTrue(got[1].startswith("vault-t") and len(got[1]) <= 63, got[1])
        self.assertEqual(got[2], "")                           # 6 + 25 + 1 + 31 = 63: fits
        self.assertEqual(got[3], "vault-" + "t" * 24 + "--" + "v" * 26)


# ---- the shared sign-in client task against a small directory over HTTP ----------------------------------------

class Directory:
    """clients?clientId= (exact), create 201/409, update 204, protocol mappers; enough for keycloak_client_ensure."""

    def __init__(self):
        self.clients = {}
        self.log = []
        d = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=None):
                raw = json.dumps(body).encode() if body is not None else b""
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}")

            def do_GET(self):
                u = urllib.parse.urlsplit(self.path)
                d.log.append(("GET", u.path))
                cid = urllib.parse.parse_qs(u.query).get("clientId", [""])[0]
                return self._send(200, [c for c in d.clients.values() if c["clientId"] == cid])

            def do_POST(self):
                body = self._body()
                d.log.append(("POST", self.path))
                if self.path.endswith("/clients"):
                    if body["clientId"] in d.clients:
                        return self._send(409, {"errorMessage": "exists"})
                    body["id"] = "uuid-" + body["clientId"]
                    d.clients[body["clientId"]] = body
                    return self._send(201)
                return self._send(201)

            def do_PUT(self):
                body = self._body()
                d.log.append(("PUT", self.path))
                for c in d.clients.values():
                    if self.path.endswith("/clients/" + c["id"]):
                        c.update(body)
                return self._send(204)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def add(self, client_id, owner=None):
        attrs = {"hybridsovereign.redhat/owner": owner} if owner else {}
        self.clients[client_id] = {"id": "uuid-" + client_id, "clientId": client_id, "attributes": attrs,
                                   "redirectUris": [], "webOrigins": [], "protocolMappers": []}

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def run_ensure(directory, owner, recorded):
    tmp = tempfile.mkdtemp(prefix="shc-kce-")
    try:
        common = os.path.join(tmp, "eda", "common")
        os.makedirs(os.path.join(common, "tasks"))
        shutil.copytree(os.path.join(ROOT, "eda", "common", "vars"), os.path.join(common, "vars"))
        shutil.copy(KCE, os.path.join(common, "tasks", "keycloak_client_ensure.yml"))
        role = os.path.join(tmp, "eda", "rulebooks", "roles", "r")
        os.makedirs(role)
        with open(os.path.join(tmp, "play.yml"), "w") as f:
            f.write("- hosts: localhost\n  connection: local\n  gather_facts: false\n  tasks:\n"
                    "    - ansible.builtin.include_tasks: eda/common/tasks/keycloak_client_ensure.yml\n")
        with open(os.path.join(tmp, "vars.json"), "w") as f:
            json.dump({"role_path": role, "kce_url": directory.url, "kce_realm": "tenants", "kce_token": "t",
                       "kce_client": "vault_acme-corp_web", "kce_name": "Vault acme-corp/web",
                       "kce_redirect_uris": ["https://vault.example.org/cb"], "kce_owner": owner,
                       "kce_recorded": recorded}, f)
        env = dict(os.environ, ANSIBLE_LOCALHOST_WARNING="false", ANSIBLE_INVENTORY_UNPARSED_WARNING="false",
                   ANSIBLE_NOCOLOR="1", ANSIBLE_LOCAL_TEMP=os.path.join(tmp, "lt"),
                   ANSIBLE_REMOTE_TMP=os.path.join(tmp, "rt"))
        p = subprocess.run(["ansible-playbook", "-i", "localhost,", "play.yml", "-e", "@vars.json"], cwd=tmp,
                           env=env, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout + p.stderr
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class SignInClientAgainstADirectory(unittest.TestCase):
    OWNER = "Vault/entity-acme-corp/web"

    def setUp(self):
        self.d = Directory()
        self.addCleanup(self.d.stop)

    def writes(self):
        return [e for e in self.d.log if e[0] in ("POST", "PUT")]

    def test_new_client_carries_the_mark(self):
        rc, out = run_ensure(self.d, self.OWNER, False)
        self.assertEqual(rc, 0, out[-3000:])
        self.assertEqual(self.d.clients["vault_acme-corp_web"]["attributes"]["hybridsovereign.redhat/owner"],
                         self.OWNER)

    def test_a_recorded_client_is_converged(self):
        self.d.add("vault_acme-corp_web", self.OWNER)
        rc, out = run_ensure(self.d, self.OWNER, True)
        self.assertEqual(rc, 0, out[-3000:])
        self.assertEqual(self.d.clients["vault_acme-corp_web"]["redirectUris"], ["https://vault.example.org/cb"])

    def test_a_client_not_recorded_is_refused_untouched_whatever_it_carries(self):
        """Nothing recorded: a client of the id is refused, this Vault's own owner attribute included."""
        for owner in (self.OWNER, None, "Vault/entity-acme/corp-web"):
            self.d.clients.clear()
            self.d.log.clear()
            self.d.add("vault_acme-corp_web", owner)
            rc, out = run_ensure(self.d, self.OWNER, False)
            self.assertNotEqual(rc, 0, owner)
            self.assertIn("was not made for this object", " ".join(out.split()))
            self.assertEqual(self.writes(), [], owner)
            self.assertEqual(self.d.clients["vault_acme-corp_web"]["redirectUris"], [], owner)

    def test_a_recorded_client_with_another_owners_mark_is_refused(self):
        self.d.add("vault_acme-corp_web", "Vault/entity-acme/corp-web")
        rc, out = run_ensure(self.d, self.OWNER, True)
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.writes(), [])

    def test_a_recorded_unmarked_client_is_taken_and_marked(self):
        self.d.add("vault_acme-corp_web")
        rc, out = run_ensure(self.d, self.OWNER, True)
        self.assertEqual(rc, 0, out[-3000:])
        self.assertEqual(self.d.clients["vault_acme-corp_web"]["attributes"]["hybridsovereign.redhat/owner"],
                         self.OWNER)


if __name__ == "__main__":
    unittest.main()
