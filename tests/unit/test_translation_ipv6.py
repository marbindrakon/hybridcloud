"""Translation for IPv6-only placements (design/IPV6-PLAN.md §7.1, §7.2, §11.8): the egress filter's resolver list
(shc_fabric.py egress-plan: resolverDestinations6 plus every site's translation dns64Resolvers), the placement's
status.ipv6.translation (networkplacement_provision main.yml "Describe the placement's IPv6": only for an IPv6-only
placement of a mode-Platform network with internet access, on a fabric with IPv6 egress, at a site that reports
capabilities.ipv6Translation; null otherwise), the network's status.egress.ipv4Translation (egress_status.yml), the
CloudOSO placement-network request (the DNS64 resolvers on the IPv6-only placement's subnet, never on a dual-stack
one) and PlatformOpenshift (refused on a network without IPv4 on every backend that can name a network). The plays
run in ansible-playbook (skipped without it). Documentation prefixes and generic names only."""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, write_out  # noqa: E402
from test_placement_ipv6_status import CRDS, real, status_schema  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "eda", "common", "files"))
import shc_fabric as F  # noqa: E402

ROLES = os.path.join(ROOT, "eda", "rulebooks", "roles")
NP = os.path.join(ROLES, "networkplacement_provision", "tasks")
HN = os.path.join(ROLES, "hybridnetwork_provision", "tasks")
PO = os.path.join(ROLES, "platformopenshift_provision", "tasks")
PO_CRD = os.path.join(ROOT, "operator", "config", "crd", "bases", "crd-platformopenshift.yaml")
HN_CRDS = [os.path.join(ROOT, "operator", "config", "crd", "bases", "crd-hybridnetwork.yaml"),
           os.path.join(ROOT, "operator", "primary", "helm", "templates", "crds.yaml")]

PREF64 = "2001:db8:64::/96"
DNS64 = ["2001:db8:53::11", "2001:db8:53::10"]
P6 = "2001:db8:4005:4200::/64"
READY_MSG = ("IPv6-only placements reach IPv4-only destinations through the platform's translator on the sites that "
             "offer it; each placement's status names its translation prefix and resolvers")
REFUSAL = "the platform reaches clusters over IPv4, so the network needs IPv4 space"


# ---- 1. the egress filter admits the DNS64 resolvers ----

class Resolvers6(unittest.TestCase):
    def test_configured_and_translation_resolvers_sorted_once(self):
        e6 = {"resolverDestinations6": ["2001:db8:53::53", "2001:db8:53::10"]}
        tr = {"site-b": {"pref64": PREF64, "dns64Resolvers": DNS64},
              "site-a": {"pref64": "2001:db8:65::/96", "dns64Resolvers": ["2001:db8:53::10/128", "2001:db8:53::9"]},
              "site-c": {}, "site-d": None}
        self.assertEqual(F.resolvers6(e6, tr),
                         ["2001:db8:53::9", "2001:db8:53::10", "2001:db8:53::11", "2001:db8:53::53"])

    def test_nothing_without_entries_and_bad_entries_left_out(self):
        self.assertEqual(F.resolvers6({}, None), [])
        self.assertEqual(F.resolvers6({}, {"s": {"pref64": PREF64}}), [])
        self.assertEqual(F.resolvers6({}, {"s": {"dns64Resolvers": ["192.0.2.53", "2001:db8:53::/64", "nope",
                                                                     "2001:db8:53::1"]}}),
                         ["2001:db8:53::1"])

    def test_egress_plan_puts_them_on_every_node(self):
        import test_egress_ipv6 as E
        req = E.plan_request(translation={"site1": {"pref64": PREF64, "dns64Resolvers": DNS64}})
        r = F.egress_plan(req)
        self.assertEqual(r["errors"], [])
        for node in ("bgw-1", "bgw-2"):
            v6 = r["nodes"][node]["ipv6"]
            self.assertEqual(v6["resolvers6"], ["2001:db8:53::10", "2001:db8:53::11", "2001:db8:53::53"])
            # the deny list is still derived from the prefix, the allow list unchanged
            self.assertTrue(all(p.startswith("2001:db8:64::") for p in v6["platDeny6"]))
            self.assertEqual(v6["allow6"], E.E6["allowDestinations6"])

    def test_the_filter_rule_comes_before_the_deny_list(self):
        import test_egress_ipv6 as E
        req = E.plan_request(translation={"site1": {"pref64": PREF64, "dns64Resolvers": DNS64}})
        n = F.egress_plan(req)["nodes"]["bgw-1"]["ipv6"]
        self.assertIn("2001:db8:53::11", n["resolvers6"])
        apply = open(os.path.join(ROOT, "eda", "common", "files", "bgw", "shc-fabric-apply")).read()
        self.assertLess(apply.index("@resolvers6 meta l4proto"), apply.index("@deny6 counter drop"))


# ---- 2. the placement's status.ipv6.translation ----

def net(mode="Platform", internet=True):
    return {"spec": {"egress": {"internet": internet}}, "status": {"ipv6": {"mode": mode}}}


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class PlacementTranslation(unittest.TestCase):
    TR = {"site1": {"pref64": PREF64, "dns64Resolvers": DNS64}}

    def run_task(self, v4=(), mode="Platform", internet=True, egress6=True, cap=True, translation=None, site="site1",
                 mode_from_network=False):
        task = by_name(load(os.path.join(NP, "main.yml")), "Describe the placement's IPv6")
        fab_real = real(**{"egress.ipv6.enabled": egress6})
        fab_real["translation"] = copy.deepcopy(self.TR if translation is None else translation)
        start = {"ansible.builtin.set_fact": {"np_status": "{{ seed_status }}"}}
        variables = {"seed_status": {"entity": "acme", "prefixes": list(v4) + [P6]},
                     "np_prefixes_v4": list(v4), "np_prefixes_v6": [P6], "np_backend_kind": "CloudOSO",
                     "np_site": site, "np_network": net(mode, internet), "fab_real": fab_real,
                     "ci_capabilities": {"ipv6": True, "ipv6Only": True, "ipv6Translation": cap}}
        if not mode_from_network:
            variables["np_ipv6_mode"] = mode
        out = run_play([start, task, write_out("{'status': np_status, 'translation': np_translation}")], variables)
        return out["status"]["ipv6"], out["translation"]

    def test_ipv6_only_platform_with_internet_gets_the_site_entry(self):
        ipv6, kept = self.run_task()
        self.assertEqual(ipv6["translation"], {"pref64": PREF64, "resolvers": DNS64})
        self.assertEqual(kept, {"pref64": PREF64, "resolvers": DNS64})
        # the rest of status.ipv6 is unchanged by it
        self.assertEqual(ipv6["addressing"], "DHCPv6")

    def test_mode_from_the_network_status_when_the_job_has_none(self):
        ipv6, _ = self.run_task(mode_from_network=True)
        self.assertEqual(ipv6["translation"]["pref64"], PREF64)

    def test_every_missing_condition_writes_null(self):
        cases = {"dual-stack": dict(v4=["10.60.1.0/24"]),
                 "tenant prefixes": dict(mode="Tenant"),
                 "no internet": dict(internet=False),
                 "no IPv6 egress": dict(egress6=False),
                 "site reports no translation": dict(cap=False),
                 "no entry for the site": dict(translation={"other": {"pref64": PREF64, "dns64Resolvers": DNS64}}),
                 "entry without a prefix": dict(translation={"site1": {"dns64Resolvers": DNS64}})}
        for what, kw in cases.items():
            ipv6, kept = self.run_task(**kw)
            self.assertIn("translation", ipv6, what)
            self.assertIsNone(ipv6["translation"], what)
            self.assertEqual(kept, {}, what)

    def test_entry_without_resolvers_still_names_the_prefix(self):
        ipv6, _ = self.run_task(translation={"site1": {"pref64": PREF64}})
        self.assertEqual(ipv6["translation"], {"pref64": PREF64, "resolvers": []})


class PlacementTranslationSchema(unittest.TestCase):
    def test_only_fields_the_crd_declares(self):
        for path in CRDS:
            tr = status_schema(path)["ipv6"]["properties"]["translation"]
            self.assertEqual(tr["type"], "object", path)
            self.assertEqual(set(tr["properties"]), {"pref64", "resolvers"}, path)
            self.assertEqual(tr["properties"]["resolvers"]["items"]["type"], "string", path)

    def test_hybridnetwork_ipv4_translation_fields(self):
        import yaml
        for path in HN_CRDS:
            with open(path) as f:
                lines = f.read().split("\n")
            start = next(i for i, ln in enumerate(lines) if ln.startswith("apiVersion:"))
            text = "\n".join(ln for ln in lines[start:] if not ln.startswith("{{"))
            doc = next(d for d in yaml.safe_load_all(text)
                       if isinstance(d, dict) and (d.get("metadata") or {}).get("name") == "hybridnetworks.hybridsovereign.redhat")
            st = doc["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["status"]["properties"]
            tr = st["egress"]["properties"]["ipv4Translation"]["properties"]
            # the network job writes state, reason and message only
            self.assertEqual(set(tr), {"state", "reason", "message"}, path)


# ---- 3. the network's status.egress.ipv4Translation ----

@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class NetworkTranslation(unittest.TestCase):
    def egress(self, **vars_):
        t = load(os.path.join(HN, "egress_status.yml"))[0]
        base = {"hn_v4": [], "hn_mode6": "Platform", "hn_egress_want": True, "hn_egress_want6": True,
                "hn_egress_offered": True, "hn_egress_offered6": True, "hn_fabric_name": "fab",
                "hn_egress_carried": {"network": "acme/v6", "sourceAddresses": [], "internet": False, "ipv6": True},
                "hn_egress6_degraded": False, "hn_egress6_degraded_nodes": [], "hn_translation_offered": True}
        base.update(vars_)
        return run_play([t, write_out("hn_egress")], base)

    def test_ready(self):
        e = self.egress()
        self.assertEqual(e["ipv4Translation"], {"state": "ready", "message": READY_MSG})

    def test_pending_while_the_ipv6_egress_is_being_added(self):
        e = self.egress(hn_egress_carried={"network": "acme/v6", "sourceAddresses": [], "internet": False, "ipv6": False})
        self.assertEqual(e["ipv4Translation"], {"state": "pending"})

    def test_ready_also_when_degraded(self):
        e = self.egress(hn_egress6_degraded=True, hn_egress6_degraded_nodes=["bgw-2"])
        self.assertEqual(e["ipv4Translation"]["state"], "ready")

    def test_not_offered_without_ipv6_egress_or_without_any_translation_entry(self):
        for kw in ({"hn_egress_offered6": False}, {"hn_translation_offered": False}, {"hn_translation_offered": None}):
            e = self.egress(**kw)
            self.assertEqual(e["ipv4Translation"], {"state": "unavailable", "reason": "NotOffered",
                                                    "message": "translation to IPv4 is not offered on this fabric yet"},
                             kw)
        # an older caller that doesn't pass the input: not offered
        t = load(os.path.join(HN, "egress_status.yml"))[0]
        base = {"hn_v4": [], "hn_mode6": "Platform", "hn_egress_want": True, "hn_egress_want6": True,
                "hn_egress_offered": True, "hn_egress_offered6": True, "hn_fabric_name": "fab",
                "hn_egress_carried": {"network": "acme/v6", "ipv6": True, "internet": False},
                "hn_egress6_degraded": False, "hn_egress6_degraded_nodes": []}
        self.assertEqual(run_play([t, write_out("hn_egress")], base)["ipv4Translation"]["reason"], "NotOffered")

    def test_tenant_prefixes_address_choice(self):
        e = self.egress(hn_mode6="Tenant", hn_egress_want6=False)
        self.assertEqual(e["ipv4Translation"]["reason"], "AddressChoice")

    def test_no_row_with_ipv4_or_without_internet(self):
        e = self.egress(hn_v4=["10.65.0.0/16"],
                        hn_egress_carried={"network": "acme/v6", "sourceAddresses": ["198.51.100.64"], "ipv6": True})
        self.assertIsNone(e["ipv4Translation"])
        e = self.egress(hn_egress_want=False, hn_egress_want6=False)
        self.assertIsNone(e["ipv4Translation"])

    def test_offered_input_from_the_realization(self):
        task = by_name(load(os.path.join(HN, "main.yml")), "What the fabric realizes for the network's egress")
        expr = task["ansible.builtin.set_fact"]["hn_translation_offered"]
        for tr, want in (({}, False), ({"s": {}}, False), ({"s": {"dns64Resolvers": DNS64}}, False),
                         ({"s": None, "t": {"pref64": PREF64}}, True)):
            out = run_play([{"ansible.builtin.set_fact": {"_o": expr}}, write_out("_o")], {"fab_real": {"translation": tr}})
            self.assertEqual(out, want, tr)


# ---- 4. the CloudOSO placement network carries the DNS64 resolvers ----

@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class CloudOSORequest(unittest.TestCase):
    def request(self, translation, v4=(), by_site=None):
        task = by_name(load(os.path.join(NP, "backend_cloudoso.yml")), "The placement's network, router and share")
        fab_real = real(**{"placement.resolversBySite": by_site or {}})
        fab_real["openstack"] = {"platformProject": "platform", "projectDomain": "tenants"}
        variables = {"ci_spec": {"openstack": {"region": "r1"}}, "fab_real": fab_real,
                     "np_names": {"osNetwork": "hn51001-site1", "tag": "shc.np/51001"}, "fab_namespace": "acme",
                     "fab_name": "v6-site1", "fab_spec": {"network": "v6"}, "np_real": {"mtu": 8950, "l3Vni": 51001},
                     "np_prefixes_v4": list(v4), "np_prefixes_v6": [P6], "np_site": "site1",
                     "np_backend": {"status": {"projectId": "p-tenant"}}}
        if translation is not None:
            variables["np_translation"] = translation
        return run_play([{"ansible.builtin.set_fact": {"_req": task["vars"]["os_request"]}}, write_out("_req")], variables)

    def test_ipv6_only_with_translation_puts_the_dns64_resolvers_first(self):
        req = self.request({"pref64": PREF64, "resolvers": DNS64},
                           by_site={"site1": ["192.0.2.53", "2001:db8:53::53", DNS64[0]]})
        self.assertEqual(req["resolvers"], DNS64 + ["192.0.2.53", "2001:db8:53::53"])
        self.assertEqual(req["prefixes"], [P6])

    def test_without_translation_the_site_resolvers_only(self):
        for tr in (None, {}):
            req = self.request(tr, v4=["10.60.1.0/24"], by_site={"site1": ["192.0.2.53"]})
            self.assertEqual(req["resolvers"], ["192.0.2.53"])
            self.assertEqual(req["prefixes"], ["10.60.1.0/24", P6])
        self.assertEqual(self.request(None)["resolvers"], [])

    def test_the_ipv6_subnet_gets_them(self):
        # the request's resolvers are split per family by placement-network (test_shc_openstack_placement)
        import test_shc_openstack_placement as O
        req = self.request({"pref64": PREF64, "resolvers": DNS64}, by_site={"site1": ["192.0.2.53"]})
        conn = O.FakeConn()
        O.run(conn, prefixes=req["prefixes"], resolvers=req["resolvers"])
        created = [d for op, d in conn.calls if op == "create_subnet"]
        self.assertEqual([(c["ip_version"], c["dns_nameservers"]) for c in created], [(6, DNS64)])


# ---- 5. PlatformOpenshift needs IPv4 on the network ----

class PlatformOpenshiftRefusal(unittest.TestCase):
    def test_every_backend_that_names_a_network_refuses_one_without_ipv4(self):
        spec = load(PO_CRD)["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]
        with_network = sorted(k for k in spec.get("type", {}).get("enum", [])
                              if "network" in ((spec.get(k) or {}).get("properties") or {}))
        # an OpenStack cluster gets a network of its own (no spec.openstack.network): nothing to refuse there; a
        # backend that gains a network field must refuse an IPv6-only placement like these
        self.assertEqual(with_network, ["aws", "gcp"])
        for kind in with_network:
            tasks = load(os.path.join(PO, f"{kind}_clusterbuild_network.yml"))
            t = by_name(tasks, "Refuse a placement without IPv4")
            self.assertIn(REFUSAL, t["ansible.builtin.fail"]["msg"], kind)
            self.assertEqual(t["when"], "cb_machine_cidr | trim | length == 0", kind)
            # the machine network is the placement's family-4 subnet, never an IPv6 prefix
            facts = by_name(tasks, "The placement's VPC")
            self.assertIn("selectattr('family', 'equalto', 4)", facts["ansible.builtin.set_fact"]["cb_machine_cidr"])
            self.assertIn("reject('search', ':')", facts["ansible.builtin.set_fact"]["cb_machine_cidr"])


if __name__ == "__main__":
    unittest.main()
