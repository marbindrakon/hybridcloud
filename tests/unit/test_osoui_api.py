"""The API of an OpenStack environment that people can use: the CRD fields (CloudOSO spec.toolRbac, status.access and
status.endpoints; CloudInfrastructure spec.openstack.dashboard / signIn and status.site.signIn; PlatformOpenshift
status.credential; NetworkPlacement status.siteNetwork; TenantProfile capability values), validated with the CRDs'
own schemas (jsonschema on openAPIV3Schema; Kubernetes adds structural rules this does not check), the reasons the
jobs write, and the realization defaults."""
import copy
import glob
import os
import re
import sys
import unittest

import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BASES = os.path.join(ROOT, "operator", "config", "crd", "bases")
LIB = os.path.join(ROOT, "eda", "rulebooks", "collections", "ansible_collections", "hybridsovereign", "shc",
                   "plugins", "module_utils")
sys.path.insert(0, LIB)
import shc_status_reasons as R  # noqa: E402

try:
    import jsonschema
except ImportError:  # pragma: no cover
    jsonschema = None


def schema(kind):
    for path in glob.glob(os.path.join(BASES, "crd-*.yaml")):
        with open(path) as f:
            doc = yaml.safe_load(f)
        if doc["spec"]["names"]["kind"] == kind:
            return doc["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
    raise KeyError(kind)


def k8s_to_jsonschema(node):
    """openAPIV3Schema -> a JSON schema (drop the x-kubernetes-* keys; preserve-unknown-fields allows extra keys)."""
    if isinstance(node, dict):
        out = {k: k8s_to_jsonschema(v) for k, v in node.items() if not k.startswith("x-kubernetes")}
        return out
    if isinstance(node, list):
        return [k8s_to_jsonschema(v) for v in node]
    return node


def sub(node, path):
    for p in path.split("."):
        node = node["items"] if p == "[]" else node["properties"][p]
    return node


def walk(node, path=""):
    yield path, node
    for k, v in (node.get("properties") or {}).items():
        yield from walk(v, f"{path}.{k}" if path else k)
    if "items" in node:
        yield from walk(node["items"], path + "[]")


@unittest.skipIf(jsonschema is None, "jsonschema not installed")
class Schemas(unittest.TestCase):
    def valid(self, kind, obj):
        sch = k8s_to_jsonschema(schema(kind))
        sch.pop("required", None)   # the object-level required spec is not this test's
        jsonschema.validate(obj, sch)

    def invalid(self, kind, obj):
        with self.assertRaises(jsonschema.ValidationError):
            self.valid(kind, obj)

    def test_cloudoso_spec_and_status(self):
        obj = {"spec": {"cloudRef": {"name": "site-a"},
                        "toolRbac": {"editRbac": ["developers"], "viewRbac": ["auditors"]}},
               "status": {"access": {"signInClient": "openstack-site-a", "groupDomain": "tenants", "groups": [
                   {"rbac": "developers", "group": "acme.developers", "access": "Edit", "role": "member",
                    "state": "Granted", "message": ""}]},
                   "endpoints": {"console": "https://dashboard.site-a.example.org", "signIn": {
                       "method": "TenantSSO", "idpName": "Example", "start": "Dashboard"},
                       "signInGroups": ["acme.developers"], "api": "https://identity.site-a.example.org",
                       "project": "acme_web", "projectId": "p", "domain": "tenants", "domainId": "d",
                       "region": "RegionOne", "location": "On-premises",
                       "apiCredentials": {"state": "Offered", "validMinutesAfterSignIn": 1440, "source": "Site"}}}}
        self.valid("CloudOSO", obj)
        bad = copy.deepcopy(obj)
        bad["status"]["endpoints"]["apiCredentials"]["validMinutesAfterSignIn"] = ""
        self.invalid("CloudOSO", bad)   # an empty string where the job means "not known" must never be written
        bad = copy.deepcopy(obj)
        bad["spec"]["toolRbac"]["editRbac"] = "developers"
        self.invalid("CloudOSO", bad)

    def test_cloudinfrastructure_sign_in(self):
        base = {"spec": {"type": "openstack", "credentialsRef": {"vaultPath": "x"}, "openstack": {
            "projectDomain": "tenants", "dashboard": {"url": "https://dashboard.site-a.example.org"},
            "signIn": {"identityProvider": "platform-tenants", "protocol": "openid", "displayName": "Example",
                       "start": "IdentityProvider", "authorizationTTLMinutes": 1440}}}}
        self.valid("CloudInfrastructure", base)   # groupDomain optional (default: projectDomain)
        for path, value in (("start", "Other"), ("authorizationTTLMinutes", -1)):
            bad = copy.deepcopy(base)
            bad["spec"]["openstack"]["signIn"][path] = value
            self.invalid("CloudInfrastructure", bad)
        bad = copy.deepcopy(base)
        del bad["spec"]["openstack"]["signIn"]["protocol"]
        self.invalid("CloudInfrastructure", bad)
        bad = copy.deepcopy(base)
        bad["spec"]["openstack"]["dashboard"]["url"] = "http://dashboard.site-a.example.org"
        self.invalid("CloudInfrastructure", bad)
        si = sub(schema("CloudInfrastructure"), "spec.openstack.signIn")
        self.assertEqual(si["properties"]["start"]["default"], "Dashboard")
        self.assertNotIn("groupDomain", si["required"])
        st = {"status": {"capabilities": {"consoleSignIn": True, "apiCredentials": False},
                         "site": {"dashboard": "https://dashboard.site-a.example.org", "signIn": {
                             "identityProvider": "platform-tenants", "protocol": "openid", "groupDomain": "tenants",
                             "groupDomainId": "d", "start": "Dashboard", "startUrl": "https://x",
                             "authorizationTTLSource": "Unknown", "confirmed": ["groupDomain"],
                             "notConfirmed": ["identityProvider"], "warnings": [],
                             "account": [{"role": "manager", "scope": "domain tenants"}]}}}}
        self.valid("CloudInfrastructure", dict(base, **st))

    def test_platformopenshift_credential_and_placement_network(self):
        self.valid("PlatformOpenshift", {"status": {"credential": {"id": "c", "user": "shc-b", "userId": "u",
                                                                   "domain": "d", "createdAt": "2026-11-14"}}})
        self.valid("NetworkPlacement", {"status": {"siteNetwork": {"name": "hn10001", "id": "n", "project": "p",
                                                                   "subnets": [{"family": 4, "prefix": "10.0.0.0/24",
                                                                                "gateway": "10.0.0.1", "id": "s"}]}}})

    def test_tenantprofile_capabilities(self):
        enum = sub(schema("TenantProfile"), "status.sites.[].capabilities.[]")["enum"]
        self.assertIn("ConsoleSignIn", enum)
        self.assertIn("ApiCredentials", enum)

    def test_new_status_fields_carry_no_write_hazards(self):
        # enum / pattern / format / required / nullable on a status field make a live status write fail
        for kind, root in (("CloudOSO", "status.access"), ("CloudOSO", "status.endpoints"),
                           ("CloudInfrastructure", "status.site.signIn"), ("PlatformOpenshift", "status.credential"),
                           ("NetworkPlacement", "status.siteNetwork")):
            for path, node in walk(sub(schema(kind), root), root):
                with self.subTest(kind=kind, path=path):
                    if not path.endswith("[]") or node.get("type") == "object":
                        self.assertTrue(node.get("description"), "every field has a description")
                    for hazard in ("enum", "pattern", "format", "required", "nullable"):
                        self.assertNotIn(hazard, node)


class Texts(unittest.TestCase):
    INTERNAL = re.compile(r"\b(G\d+|N\d|OD\d+|P\d+(\.\d+)?|DESIGN|spike|lab|atuin|signal9|shc-tenants|"
                          r"osoui|fabric_realization|tenantSignIn|clusterIdentity|Keycloak|EDA|AAP)\b", re.I)

    def test_crd_text_is_api_text(self):
        for kind, root in (("CloudOSO", "spec.toolRbac"), ("CloudOSO", "status.access"),
                           ("CloudOSO", "status.endpoints"), ("CloudInfrastructure", "spec.openstack.signIn"),
                           ("CloudInfrastructure", "spec.openstack.dashboard"),
                           ("CloudInfrastructure", "status.site.signIn"), ("PlatformOpenshift", "status.credential"),
                           ("NetworkPlacement", "status.siteNetwork")):
            for path, node in walk(sub(schema(kind), root), root):
                with self.subTest(kind=kind, path=path):
                    self.assertIsNone(self.INTERNAL.search(node.get("description", "")), node.get("description"))

    def test_reasons_are_registered(self):
        for r in ("GroupsGranted", "NoAccessGroups", "SignInNotSetUp", "WaitingForGroup", "GrantRefused", "Confirmed",
                  "DeclaredNotConfirmed", "NotDeclared", "IdentityProviderNotFound", "GroupDomainNotFound",
                  "ClientNotFound", "MappingWarning", "OwnCredential", "UserRefused", "RoleRefused",
                  "PasswordRefused", "CredentialRefused"):
            self.assertIsNotNone(R.lookup(r), r)
        self.assertEqual(R.lookup("UserRefused")["actor"], "P")


class Realization(unittest.TestCase):
    def test_defaults(self):
        with open(os.path.join(ROOT, "eda", "common", "vars", "fabric_realization.yml")) as f:
            os_ = yaml.safe_load(f)["fabric_realization_defaults"]["openstack"]
        self.assertEqual(os_["tenantSignIn"], {"realm": "", "clientId": "openstack-{site}", "rbacConfig": "",
                                               "editRoles": ["member"], "viewRoles": ["reader"]})
        # the cluster's user and credential are named by the platform; no name setting is offered
        self.assertEqual(os_["clusterIdentity"], {"roles": ["member"], "rolesBySite": {}})


if __name__ == "__main__":
    unittest.main()


class AccessReadyTime(unittest.TestCase):
    """Gate part 2 G-4: AccessReady carries no time of its own; the status writer gives it the form of the other
    conditions (seconds, Z) and keeps it while its status stays."""

    def test_the_status_writer_times_it(self):
        sys.path.insert(0, os.path.dirname(__file__))
        from play_helper import load, walk
        main = load(os.path.join(ROOT, "eda", "rulebooks", "roles", "cloudoso_provision", "tasks", "main.yml"))
        conds = [c for t in walk(main) for c in (((t.get("vars") or {}).get("cr_status_body") or {}).get("conditions")
                                                 or []) if isinstance(c, dict) and c.get("type") == "AccessReady"]
        self.assertTrue(conds)
        for c in conds:
            self.assertNotIn("lastTransitionTime", c)
        sys.path.insert(0, os.path.join(ROOT, "eda", "rulebooks", "collections", "ansible_collections", "hybridsovereign",
                                        "shc", "plugins", "module_utils"))
        import shc_status
        out = shc_status.merge_conditions([], [{"type": "AccessReady", "status": "True", "reason": "GroupsGranted"}])
        self.assertRegex(out[0]["lastTransitionTime"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
