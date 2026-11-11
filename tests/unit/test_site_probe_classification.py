"""Site probe errors are classified: unreachable before credentials (cloudinfrastructure_provision). The OpenStack
site probe lists the site's global external networks: a network with an access_as_external policy for every project
(target "*") is listed with its address families, a network external for some projects only is not; a list that
cannot be read is left out (the site keeps what it had). The site job publishes them in status.site.externalNetworks
with default true for the network the site names."""
import os
import re
import sys
import types
import unittest

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TASKS = os.path.join(ROOT, "eda/rulebooks/roles/cloudinfrastructure_provision/tasks/openshift.yml")
OS_TASKS = os.path.join(ROOT, "eda/rulebooks/roles/cloudinfrastructure_provision/tasks/openstack.yml")
sys.path.insert(0, os.path.join(ROOT, "eda", "common", "files"))
try:
    import openstack  # noqa: F401
except ImportError:  # stub the SDK so the module imports
    _stub = types.ModuleType("openstack")
    _stub.exceptions = types.SimpleNamespace(ForbiddenException=PermissionError, NotFoundException=LookupError)
    _stub.connect = None
    sys.modules.setdefault("openstack", _stub)
import shc_openstack as S  # noqa: E402
sys.path.insert(0, os.path.dirname(__file__))
from play_helper import HAVE_ANSIBLE, by_name, load, run_cases  # noqa: E402

UNREACHABLE = [
    "HTTPSConnectionPool(host='api.site.example', port=6443): Max retries exceeded with url: /version "
    "(Caused by NewConnectionError('<urllib3.connection.HTTPSConnection>: Failed to establish a new connection: "
    "[Errno 113] No route to host'))",
    "Failed to get client due to HTTPSConnectionPool(host='api.x', port=6443): Read timed out. (read timeout=20)",
    "[Errno -2] Name or service not known",
    "Connection refused",
]
CREDENTIALS = ["Failed to get client due to 401\nReason: Unauthorized", "403 Forbidden: clusterversions is forbidden"]


@unittest.skipIf(yaml is None, "PyYAML not installed")
class Classification(unittest.TestCase):
    def setUp(self):
        with open(TASKS) as fh:
            tasks = yaml.safe_load(fh)
        task = next(t for t in tasks if t.get("name") == "Set the API failure reason")
        pat = task["vars"]["ci_unreachable_pattern"]
        self.unreachable = re.compile(pat)
        self.creds = re.compile(r"401|403|[Uu]nauthori[sz]ed|[Ff]orbidden")

    def classify(self, msg):
        if self.creds.search(msg):
            return "CredentialsInvalid"
        if self.unreachable.search(msg):
            return "SiteUnreachable"
        return "ProbeFailed" if msg else "CredentialsInvalid"

    def test_cases(self):
        for msg in UNREACHABLE:
            self.assertEqual(self.classify(msg), "SiteUnreachable", msg)
        for msg in CREDENTIALS:
            self.assertEqual(self.classify(msg), "CredentialsInvalid", msg)
        self.assertEqual(self.classify("the server could not find the requested resource"), "ProbeFailed")


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeNetwork:
    """rbac_policies honours the filters the way Neutron does; get_network and subnets read the fixtures."""

    def __init__(self, nets, subnets, rbacs, fail=False):
        self.nets, self.subs, self.rbacs, self.fail = nets, subnets, rbacs, fail
        self.filters = []

    def rbac_policies(self, **f):
        if self.fail:
            raise PermissionError("rbac listing forbidden")
        self.filters.append(f)
        return [r for r in self.rbacs if all(getattr(r, k) == v for k, v in f.items())]

    def get_network(self, nid):
        return next(n for n in self.nets if n.id == nid)

    def subnets(self, network_id):
        return [s for s in self.subs if s.network_id == network_id]


def site(fail=False):
    nets = [Obj(id="n-pub", name="public", is_shared=False), Obj(id="n-dual", name="internet", is_shared=True),
            Obj(id="n-proj", name="placed", is_shared=False), Obj(id="n-v6", name="v6only", is_shared=False)]
    subs = [Obj(network_id="n-pub", ip_version=4), Obj(network_id="n-dual", ip_version=6),
            Obj(network_id="n-dual", ip_version=4), Obj(network_id="n-dual", ip_version=4),
            Obj(network_id="n-proj", ip_version=4), Obj(network_id="n-v6", ip_version=6)]
    ext = "access_as_external"
    rbacs = [Obj(object_id="n-pub", object_type="network", action=ext, target_project_id="*"),
             Obj(object_id="n-dual", object_type="network", action=ext, target_project_id="*"),
             Obj(object_id="n-dual", object_type="network", action="access_as_shared", target_project_id="*"),
             # external for one project only (a placement's router attachments): never listed
             Obj(object_id="n-proj", object_type="network", action=ext, target_project_id="proj-tenant"),
             Obj(object_id="n-v6", object_type="network", action=ext, target_project_id="*")]
    return Obj(network=FakeNetwork(nets, subs, rbacs, fail))


class GlobalExternalNetworks(unittest.TestCase):
    def test_wildcard_policies_listed_per_project_not(self):
        notes = []
        out = S.global_external_networks(site(), notes)
        self.assertEqual(out, [{"name": "internet", "id": "n-dual", "families": [4, 6], "shared": True},
                               {"name": "public", "id": "n-pub", "families": [4], "shared": False},
                               {"name": "v6only", "id": "n-v6", "families": [6], "shared": False}])
        self.assertEqual(notes, [])

    def test_the_server_is_asked_for_wildcard_external_policies(self):
        conn = site()
        S.global_external_networks(conn, [])
        self.assertEqual(conn.network.filters, [{"action": "access_as_external", "target_project_id": "*"}])

    def test_a_server_that_ignores_the_target_filter_still_lists_only_wildcards(self):
        conn = site()
        conn.network.rbac_policies = lambda **f: list(conn.network.rbacs)
        names = [n["name"] for n in S.global_external_networks(conn, [])]
        self.assertEqual(names, ["internet", "public", "v6only"])

    def test_unreadable_list_is_left_out_with_a_note(self):
        notes = []
        self.assertIsNone(S.global_external_networks(site(fail=True), notes))
        self.assertEqual(notes, ["external networks not listed: PermissionError"])


@unittest.skipIf(yaml is None or not HAVE_ANSIBLE, "PyYAML or ansible-playbook missing")
class SiteStatusExternalNetworks(unittest.TestCase):
    """The site job's status.site.externalNetworks: the three tasks of openstack.yml in ansible-playbook."""

    STEPS = ["The site's global external networks", "Note an external network list the probe could not read",
             "Publish the global external networks"]

    def run_cases(self, cases):
        tasks = load(OS_TASKS)
        steps = [{"ansible.builtin.set_fact": {"ci_probe": "{{ case.probe }}", "ci_os": "{{ case.os }}",
                                               "ci_notes": ["earlier note"], "ci_site_out": {"region": "r1"}}}]
        steps += [by_name(tasks, n) for n in self.STEPS]
        return run_cases(steps, cases, "{'site': ci_site_out, 'notes': ci_notes}")

    def test_cases(self):
        two = [{"name": "internet", "id": "n-dual", "families": [4, 6], "shared": True},
               {"name": "public", "id": "n-pub", "families": [4], "shared": False}]
        found = {"found": True, "external": True, "id": "n-pub"}
        cases = [
            # the named network is listed: it is the default
            {"probe": {"external_networks": two, "external_network": found, "notes": []},
             "os": {"externalNetwork": "public"}},
            # no named network: no default
            {"probe": {"external_networks": two[1:], "notes": []}, "os": {}},
            # the named network is external for some projects only (not listed): no default
            {"probe": {"external_networks": two[1:], "notes": [],
                       "external_network": {"found": True, "external": True, "id": "n-proj"}},
             "os": {"externalNetwork": "placed"}},
            # an empty list is published (the site has none)
            {"probe": {"external_networks": [], "notes": []}, "os": {}},
            # the probe could not read the list: nothing published, the note joins the message
            {"probe": {"notes": ["volume types not listed: X", "external networks not listed: PermissionError"]},
             "os": {"externalNetwork": "public"}},
        ]
        res = self.run_cases(cases)
        self.assertEqual(res[0]["site"], {"region": "r1", "externalNetworks": [
            {"name": "internet", "id": "n-dual", "families": [4, 6], "default": False},
            {"name": "public", "id": "n-pub", "families": [4], "default": True}]})
        self.assertEqual(res[1]["site"]["externalNetworks"],
                         [{"name": "public", "id": "n-pub", "families": [4], "default": False}])
        self.assertEqual([n["default"] for n in res[2]["site"]["externalNetworks"]], [False])
        self.assertEqual(res[3]["site"], {"region": "r1", "externalNetworks": []})
        self.assertEqual(res[4]["site"], {"region": "r1"})
        self.assertEqual(res[4]["notes"], ["earlier note", "external networks not listed: PermissionError"])
        self.assertEqual([r["notes"] for r in res[:4]], [["earlier note"]] * 4)

if __name__ == "__main__":
    unittest.main()
