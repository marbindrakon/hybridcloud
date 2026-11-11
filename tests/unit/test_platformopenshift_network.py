"""PlatformOpenshift on a hybrid network or a global external network (DESIGN cnet §2.2, §4.2-§4.4, §5).

- files/po_preflight.py: the build name, the ranges in effect, the network judge (every Preflight reason), the external
  network in effect, the floating IP check, the wait on the service routing domain, the router's gateway address.
- The task files end to end with fakes (po_harness.py): the OpenStack preflight on both paths, the build's record,
  floating IPs and prefixes in order, refusals before anything is created and what a refusal after the allocation
  gives back, the wait; the AWS / Google Cloud network files; the teardown's withdrawal and release.
- Renders: install-config (ranges, 0 workers, the external network in effect, the floating IPs), build values.
- Structure: no platform-reach annotation, no namespace fallback, the order of the teardown.
Documentation ranges and example names only.
"""
import importlib.util
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import po_harness as H  # noqa: E402
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, walk, write_out  # noqa: E402

ROLE = os.path.join(ROOT, "eda", "rulebooks", "roles", "platformopenshift_provision")
TD = os.path.join(ROOT, "eda", "rulebooks", "roles", "platformopenshift_teardown")
TASKS = os.path.join(ROLE, "tasks")
spec = importlib.util.spec_from_file_location("po_preflight", os.path.join(ROLE, "files", "po_preflight.py"))
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)

NS = "acme-ns"
PLAT = "platform-ns"
HUB = "198.18.0.0/15"


def network_obj(reach=("True", "Attachable", ""), space=("192.0.2.0/24",)):
    st = {"fabric": "f1", "addressSpace": [{"prefix": p, "family": 6 if ":" in p else 4} for p in space],
          "conditions": []}
    if reach:
        st["conditions"].append({"type": "PlatformReachable", "status": reach[0], "reason": reach[1], "message": reach[2]})
    return {"metadata": {"name": "n1", "namespace": NS}, "spec": {"prefixes": list(space)}, "status": st}


def placement(kind="CloudOSO", env="env1", ready=True, ra=True, ra_state="ready", subnets=(("4", "192.0.2.0/25"),),
              site=None):
    return {"metadata": {"name": "p1", "namespace": NS},
            "spec": {"network": "n1", "backend": {"kind": kind, "name": env}, **({"use": {"routerAttachments": ra}} if ra is not None else {})},
            "status": {"ready": ready, "subnets": [{"family": int(f), "prefix": p} for f, p in subnets],
                       "use": {"routerAttachments": {"state": ra_state}},
                       "realization": {"site": site if site is not None else {"networkId": "net-1", "networkName": "shc-n1"}}}}


def fabric(enabled=True, ranges=("192.0.2.0/24",), endpoints=({"prefix": "198.18.0.10/32", "ports": [6443]},),
           networks=(), refused=()):
    return {"metadata": {"name": "f1", "namespace": PLAT},
            "status": {"reservedRanges": [{"prefix": HUB, "purpose": "Networks the platform's management connects from",
                                           "rule": "Refused"},
                                          {"prefix": "100.64.0.0/16", "purpose": "Addresses the site uses itself",
                                           "rule": "SteppedAround", "site": "other-site"}],
                       "realization": {"service": {"enabled": enabled, "sources": [HUB], "ports": [6443, 443],
                                                   "hubEndpoints": list(endpoints), "attachableRanges": list(ranges),
                                                   "networks": list(networks), "refused": list(refused)}}}}


def judge(**kw):
    req = dict(action="network", kind="CloudOSO", environment="env1", namespace=NS, network="n1",
               networkObj=network_obj(), placements=[placement()], fabric=fabric(), needPlatform=True,
               needRouterAttachments=True, hubApi="https://198.18.0.10:6443")
    req.update(kw)
    return P.network(req)


# ---- the helper ---------------------------------------------------------------------------------------------------

class BuildName(unittest.TestCase):
    def test_tenant_qualified(self):
        self.assertEqual(P.build_name({"prefix": "shc-oso", "entity": "acme", "name": "web1"})["buildName"],
                         "shc-oso-acme-web1")

    def test_long_names_are_cut_with_a_hash(self):
        a = P.build_name({"prefix": "shc-oso", "entity": "a" * 30, "name": "b" * 40})["buildName"]
        b = P.build_name({"prefix": "shc-oso", "entity": "a" * 30, "name": "b" * 41})["buildName"]
        self.assertLessEqual(len(a), 53)
        self.assertNotEqual(a, b)
        self.assertRegex(a, r"^shc-oso-a+-b*-?[0-9a-f]{8}$")
        self.assertEqual(a, P.build_name({"prefix": "shc-oso", "entity": "a" * 30, "name": "b" * 40})["buildName"])

    def test_two_tenants_same_cluster_name_differ(self):
        self.assertNotEqual(P.build_name({"entity": "acme", "name": "c1"}), P.build_name({"entity": "beta", "name": "c1"}))

    def test_needs_entity(self):
        with self.assertRaises(ValueError):
            P.build_name({"entity": "", "name": "c1"})


DEFAULTS = {"clusterNetwork": "10.128.0.0/14", "hostPrefix": 23, "serviceNetwork": "172.30.0.0/16",
            "machineNetwork": "10.0.0.0/16"}


class Networking(unittest.TestCase):
    def test_defaults_when_unset(self):
        r = P.networking({"spec": {}, "defaults": DEFAULTS})
        self.assertEqual((r["reason"], r["clusterNetwork"], r["hostPrefix"], r["serviceNetwork"], r["machineNetwork"]),
                         ("", "10.128.0.0/14", 23, "172.30.0.0/16", "10.0.0.0/16"))

    def test_spec_wins_and_strings_from_2_16(self):
        r = P.networking({"spec": {"clusterNetwork": "10.200.0.0/16", "hostPrefix": "24"}, "defaults": DEFAULTS})
        self.assertEqual((r["clusterNetwork"], r["hostPrefix"]), ("10.200.0.0/16", 24))

    def test_placement_subnet_is_the_machine_network(self):
        r = P.networking({"spec": {}, "defaults": DEFAULTS, "machineFromPlacement": "192.0.2.0/25"})
        self.assertEqual(r["machineNetwork"], "192.0.2.0/25")

    def test_host_prefix_against_the_default_cluster_network(self):
        r = P.networking({"spec": {"hostPrefix": 16}, "defaults": dict(DEFAULTS, clusterNetwork="10.128.0.0/16")})
        self.assertEqual(r["reason"], "InvalidSpec")
        self.assertIn("hostPrefix 16", r["message"])

    def test_bad_installation_default_names_its_setting(self):
        r = P.networking({"spec": {}, "defaults": dict(DEFAULTS, serviceNetwork="")})
        self.assertEqual(r["reason"], "InvalidSpec")
        self.assertIn("platformopenshift_defaults.networking.serviceNetwork", r["message"])


class NetworkJudge(unittest.TestCase):
    def test_passes(self):
        r = judge()
        self.assertEqual(r["reason"], "", r["message"])
        self.assertEqual((r["placement"], r["subnet4"], r["networkId"], r["networkName"], r["fabric"]),
                         (f"{NS}/p1", "192.0.2.0/25", "net-1", "shc-n1", "f1"))
        self.assertEqual(r["space"], ["192.0.2.0/24"])
        self.assertEqual(len(r["reserved"]), 2)

    def cases(self):
        return [
            ("NetworkNotFound", dict(networkObj={})),
            ("NetworkNotFound", dict(placements=[placement(env="other")])),
            ("NetworkNotFound", dict(placements=[placement(kind="CloudAWS")])),
            ("WaitingForPlacement", dict(placements=[placement(ready=False)])),
            ("RouterAttachmentsOff", dict(placements=[placement(ra=False)])),
            ("RouterAttachmentsOff", dict(placements=[placement(ra=None)])),
            ("WaitingForPlacement", dict(placements=[placement(ra_state="pending")])),
            ("WaitingForPlacement", dict(placements=[placement(site={})])),
            ("PlacementHasNoIPv4", dict(placements=[placement(subnets=(("6", "2001:db8::/64"),))])),
            ("PlatformAccessOff", dict(networkObj=network_obj(reach=("False", "NotRequested", "off")))),
            ("PlacementHasNoIPv4", dict(networkObj=network_obj(reach=("False", "NoIPv4", "")))),
            ("OutsideAttachableRanges", dict(networkObj=network_obj(reach=("False", "OutsideAttachableRanges", "")))),
            ("PrefixReserved", dict(networkObj=network_obj(reach=("False", "InfrastructureOverlap", "")))),
            ("ServiceNotOffered", dict(networkObj=network_obj(reach=("False", "ServiceNotOffered", "")))),
            ("CircuitNotDeployed", dict(networkObj=network_obj(reach=("False", "CircuitNotDeployed", "")))),
            ("WaitingForPlatformAccess", dict(networkObj=network_obj(reach=None))),
            ("WaitingForPlatformAccess", dict(networkObj=network_obj(reach=("False", "SomethingNew", "")))),
            ("WaitingForPlatformAccess", dict(fabric={})),
            ("ServiceNotOffered", dict(fabric=fabric(enabled=False))),
            ("OutsideAttachableRanges", dict(fabric=fabric(ranges=("203.0.113.0/24",)))),
            ("HubEndpointNotAdmitted", dict(fabric=fabric(endpoints=()))),
            ("HubEndpointNotAdmitted", dict(fabric=fabric(endpoints=({"prefix": "198.18.0.10/32", "ports": [443]},)))),
        ]

    def test_every_reason(self):
        for want, kw in self.cases():
            with self.subTest(want=want, kw=list(kw)):
                r = judge(**kw)
                self.assertEqual(r["reason"], want, r["message"])
                self.assertTrue(r["message"])
                self.assertEqual(r["wait"], want in ("WaitingForPlacement", "WaitingForPlatformAccess"))

    def test_attach_pending_does_not_hold_a_new_cluster(self):
        self.assertEqual(judge(networkObj=network_obj(reach=("False", "AttachPending", "")))["reason"], "")

    def test_any_range_when_none_listed(self):
        self.assertEqual(judge(fabric=fabric(ranges=()))["reason"], "")

    def test_hub_outside_its_ranges_needs_no_endpoint(self):
        self.assertEqual(judge(fabric=fabric(endpoints=()), hubApi="https://203.0.113.9:6443")["reason"], "")

    def test_without_platform_access_only_the_placement_counts(self):
        # AWS / GCP External: no PlatformReachable, no router attachments
        r = judge(kind="CloudAWS", placements=[placement(kind="CloudAWS", ra=None, ra_state="", site={})],
                  networkObj=network_obj(reach=("False", "NotRequested", "")), fabric={}, needPlatform=False,
                  needRouterAttachments=False)
        self.assertEqual(r["reason"], "", r["message"])

    def test_flags_sent_as_strings(self):
        # ansible-core 2.16 renders a templated boolean inside a mapping as "True"/"False"
        r = judge(networkObj=network_obj(reach=("False", "NotRequested", "")), needPlatform="False",
                  needRouterAttachments="False", placements=[placement(ra=False)])
        self.assertEqual(r["reason"], "")
        self.assertEqual(judge(needPlatform="True", networkObj=network_obj(reach=("False", "NotRequested", "")))["reason"],
                         "PlatformAccessOff")

    def test_ipv6_only_placement(self):
        r = judge(placements=[placement(subnets=(("6", "2001:db8:1::/64"),))])
        self.assertIn("the platform reaches clusters over IPv4, so the network needs IPv4 space", r["message"])


SITE_NETS = [{"name": "public", "id": "ext-1", "families": [4, 6], "default": True},
             {"name": "dmz", "id": "ext-2", "families": [4], "default": False},
             {"name": "v6only", "id": "ext-3", "families": [6], "default": False}]


class External(unittest.TestCase):
    def ext(self, choice="", site="site1", nets=SITE_NETS):
        return P.external({"choice": choice, "site": site, "networks": nets})

    def test_default(self):
        self.assertEqual(self.ext()["network"], {"name": "public", "id": "ext-1"})

    def test_named(self):
        self.assertEqual(self.ext("dmz")["network"], {"name": "dmz", "id": "ext-2"})

    def test_refusals(self):
        self.assertEqual(self.ext(nets=[n for n in SITE_NETS if not n["default"]])["reason"], "NetworkChoiceRequired")
        self.assertEqual(self.ext("lab-internal")["reason"], "ExternalNetworkNotListed")
        self.assertIn("lab-internal is not an external network of site site1", self.ext("lab-internal")["message"])
        self.assertEqual(self.ext("dmz", nets=SITE_NETS + [dict(SITE_NETS[1], id="ext-9")])["reason"],
                         "ExternalNetworkNotListed")
        self.assertEqual(self.ext("v6only")["reason"], "PlacementHasNoIPv4")
        self.assertEqual(self.ext(site="")["reason"], "NetworkChoiceRequired")
        self.assertEqual(self.ext("public", site="")["reason"], "ExternalNetworkNotListed")


class FipsAndWait(unittest.TestCase):
    SVC = {"networks": [{"network": f"{NS}/n1", "prefixes": ["192.0.2.15/32"]},
                        {"network": "beta-ns/m", "prefixes": ["192.0.2.0/29"]}],
           "refused": [{"network": f"{NS}/n1", "prefix": "192.0.2.99/32", "reason": "ServicePrefixInUse"}]}

    def test_fip_inside_another_networks_prefix(self):
        r = P.fips_check({"addresses": ["192.0.2.20", "192.0.2.5"], "network": f"{NS}/n1", "service": self.SVC})
        self.assertEqual(r["reason"], "ServicePrefixInUse")
        self.assertEqual(P.fips_check({"addresses": ["192.0.2.20"], "network": f"{NS}/n1", "service": self.SVC})["reason"], "")
        self.assertEqual(P.fips_check({"addresses": ["192.0.2.15"], "network": f"{NS}/n1", "service": self.SVC})["reason"], "")

    def test_wait(self):
        w = lambda p, rows=(): P.service_wait({"network": f"{NS}/n1", "prefixes": p, "service": self.SVC, "rows": list(rows)})
        self.assertTrue(w(["192.0.2.15/32"])["done"])
        r = w(["192.0.2.15/32", "192.0.2.16/32"])
        self.assertEqual((r["done"], r["reason"], r["wait"]), (False, "WaitingForPlatformAccess", True))
        r = w(["192.0.2.99/32"])
        self.assertEqual((r["done"], r["reason"], r["wait"]), (False, "ServicePrefixInUse", False))
        r = w(["192.0.2.16/32"], [{"prefix": "192.0.2.16/32", "state": "refused", "reason": "PrefixReserved",
                                   "message": "192.0.2.16/32 overlaps a reserved range"}])
        self.assertEqual((r["reason"], r["message"]), ("PrefixReserved", "192.0.2.16/32 overlaps a reserved range"))

    def test_router_gateway(self):
        routers = [{"name": "infra-x-external-router", "external_gateway_info": {"external_fixed_ips": [
            {"ip_address": "2001:db8::5"}, {"ip_address": "192.0.2.30"}]}}, {"name": "other"}]
        self.assertEqual(P.router_gateway({"routers": routers, "name": "infra-x-external-router"}), {"address": "192.0.2.30"})
        self.assertEqual(P.router_gateway({"routers": routers, "name": "infra-y-external-router"}), {"address": ""})


# ---- task files end to end ----------------------------------------------------------------------------------------

CR = {"name": "c1", "namespace": NS}
BASE = {"ansible_operator_meta": CR, "services_api_host": "https://services.example.org:6443",
        "services_bearer_token": "token-not-in-data-xyz", "central_api_host": "https://198.18.0.10:6443",
        "central_bearer_token": "token-not-in-data-xyz",
        "fabric_platform_namespace": PLAT, "platformopenshift_networking": DEFAULTS,
        "platformopenshift_service_wait_retries": 1, "platformopenshift_service_wait_delay": 0}


def k8s(net=None, places=None, fab=None, extra=None):
    d = {f"HybridNetwork/{NS}/n1": net if net is not None else network_obj(),
         f"NetworkPlacement/{NS}/": places if places is not None else [placement()],
         f"HybridFabric/{PLAT}/f1": fab if fab is not None else fabric()}
    d.update(extra or {})
    return {k: v for k, v in d.items() if v not in ({}, None)}


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class PreflightNetworkFile(unittest.TestCase):
    def run_pn(self, kube, **kw):
        v = dict(BASE, pn_kind="CloudOSO", pn_env="env1", pn_network="n1", pn_need_platform=True,
                 pn_need_router_attachments=True, **kw)
        return H.run("platformopenshift_provision", "preflight_network.yml", v, k8s=kube, result="pf")

    def test_passes_and_reads_only(self):
        rc, tr, out = self.run_pn(k8s())
        self.assertEqual(rc, 0, out)
        self.assertEqual(tr[-1]["result"]["reason"], "")
        self.assertFalse([t for t in tr if "patch" in t or "k8s" in t or "os" in t or "nudge" in t])

    def test_refusal_fails_with_its_message_and_writes_nothing(self):
        rc, tr, out = self.run_pn(k8s(places=[placement(ra=False)]))
        self.assertNotEqual(rc, 0)
        self.assertIn("does not allow router attachments", out)
        self.assertFalse([t for t in tr if "patch" in t or "os" in t])

    def test_wait_records_waiting_and_ends_the_job(self):
        rc, tr, out = self.run_pn(k8s(places=[placement(ready=False)]))
        self.assertEqual(rc, 0, out)
        p = H.patches(tr)
        self.assertEqual(len(p), 1)
        b = p[0]["body"]
        self.assertEqual((b["status"], b["reason"]), ("pending", "WaitingForPlacement"))
        self.assertEqual(b["conditions"][0]["type"], "Preflight")
        self.assertEqual(b["conditions"][0]["status"], "False")
        self.assertNotIn("result", tr[-1])

    def test_missing_platform_namespace_names_the_setting(self):
        rc, tr, out = self.run_pn(k8s(), fabric_platform_namespace="")
        self.assertNotEqual(rc, 0)
        self.assertIn("fabric_platform_namespace", out)


def os_preflight_vars(**kw):
    v = dict(BASE, oso_env_name="env1", oso_network="n1", oso_external_choice="", cb_oso_site="site1",
             cb_app_name="shc-oso-acme-c1", platform_spec={"type": "openstack", "openstack": {"environment": "env1"}},
             po_cr_current={"resources": [{"status": {}}]})
    v.update(kw)
    return v


SITE = {"metadata": {"name": "site1"}, "spec": {"type": "openstack", "credentialsRef": {"vaultPath": "sites/site1"}},
        "status": {"capabilities": {"api": True}, "site": {"externalNetworks": SITE_NETS}}}
TPROFILE = {"status": {"reservedRanges": [{"prefix": HUB, "purpose": "x", "rule": "Refused"},
                                          {"prefix": "100.64.0.0/16", "purpose": "y", "rule": "SteppedAround", "site": "site1"},
                                          {"prefix": "100.65.0.0/16", "purpose": "z", "rule": "SteppedAround", "site": "site2"}]}}


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class OpenstackPreflightFile(unittest.TestCase):
    RESULT = ("{'ext': cb_external_network, 'net': cb_fip_network_id, 'publish': cb_publish, 'platform': cb_platform_path,"
              " 'record': cb_network_record, 'ranges': [cb_cluster_network, cb_host_prefix, cb_service_network, cb_machine_network]}")

    def run_op(self, kube, **kw):
        return H.run("platformopenshift_provision", "openstack_preflight.yml", os_preflight_vars(**kw), k8s=kube,
                     result=self.RESULT)

    def test_hybrid_network_path(self):
        rc, tr, out = self.run_op(k8s())
        self.assertEqual(rc, 0, out)
        r = tr[-1]["result"]
        self.assertEqual((r["ext"], r["net"], r["publish"], r["platform"]), ("shc-n1", "net-1", "Internal", True))
        self.assertEqual(r["record"], {"network": "n1", "externalNetwork": None, "placement": f"{NS}/p1", "publish": "Internal"})
        # ansible-core 2.16 keeps a set_fact number as a string: cast at the comparison (the request casts too)
        self.assertEqual([str(x) for x in r["ranges"]], ["10.128.0.0/14", "23", "172.30.0.0/16", "10.0.0.0/16"])
        fab = [t["fabric"] for t in tr if "fabric" in t][0]
        self.assertEqual(fab["action"], "check-cluster-networks")
        self.assertEqual(fab["hostPrefix"], 23)
        self.assertEqual(fab["space"], ["192.0.2.0/24"])
        self.assertEqual([x["prefix"] for x in fab["ranges"]], [HUB])   # another site's own rows do not count
        self.assertEqual((fab["provider"], fab["site"]), ("openstack", "site1"))
        self.assertFalse([t for t in tr if "patch" in t or "os" in t])

    def test_external_default(self):
        rc, tr, out = self.run_op(k8s(extra={f"CloudInfrastructure/{PLAT}/site1": SITE, f"TenantProfile/{NS}/shc": TPROFILE}),
                                  oso_network="")
        self.assertEqual(rc, 0, out)
        r = tr[-1]["result"]
        self.assertEqual((r["ext"], r["net"], r["publish"], r["platform"]), ("public", "ext-1", "External", False))
        self.assertEqual(r["record"]["externalNetwork"], "public")
        fab = [t["fabric"] for t in tr if "fabric" in t][0]
        self.assertEqual(fab["space"], [])
        self.assertEqual([x["prefix"] for x in fab["ranges"]], [HUB, "100.64.0.0/16"])

    def test_external_refusals_before_anything(self):
        nodefault = dict(SITE, status={"capabilities": {"api": True}, "site": {"externalNetworks": SITE_NETS[1:]}})
        for kube, choice, want in ((k8s(extra={f"CloudInfrastructure/{PLAT}/site1": nodefault}), "", "has no default external network"),
                                   (k8s(extra={f"CloudInfrastructure/{PLAT}/site1": SITE}), "nope", "nope is not an external network")):
            rc, tr, out = self.run_op(kube, oso_network="", oso_external_choice=choice)
            self.assertNotEqual(rc, 0)
            self.assertIn(want, out)
            self.assertFalse([t for t in tr if "patch" in t or "os" in t or "fabric" in t])

    def test_range_conflict(self):
        # shc_fabric.py check-cluster-networks answers {errors: [messages], reason}
        for err, reason, want in (("serviceNetwork 198.18.0.0/16 overlaps 198.18.0.0/15, reserved by the platform: x",
                                   "PrefixReserved", "overlaps 198.18.0.0/15, reserved by the platform"),
                                  ("spec.networking.clusterNetwork 192.0.2.0/24 overlaps the network's address space "
                                   "192.0.2.0/24", "AddressConflict", "overlaps the network's")):
            rc, tr, out = H.run("platformopenshift_provision", "openstack_preflight.yml",
                                dict(os_preflight_vars(), fake_fabric={"check-cluster-networks": {"errors": [err],
                                                                                                  "reason": reason}}),
                                k8s=k8s(), result="1")
            self.assertNotEqual(rc, 0)
            self.assertIn(want, out)

    def test_build_name_taken(self):
        ns = {f"Namespace//shc-oso-acme-c1": {"metadata": {"name": "shc-oso-acme-c1"}}}
        rc, tr, out = self.run_op(k8s(extra=ns))
        self.assertNotEqual(rc, 0)
        self.assertIn("the build name shc-oso-acme-c1 is already in use", out)
        rc, tr, out = self.run_op(k8s(extra=ns), po_cr_current={"resources": [{"status": {"buildName": "shc-oso-acme-c1"}}]})
        self.assertEqual(rc, 0, out)


def addr_vars(**kw):
    v = dict(BASE, oso_network="n1", cb_app_name="shc-oso-acme-c1", cb_platform_path=True, cb_oso_project_id="proj-1",
             cb_fip_network_id="net-1", cb_fip_account="sites/site1", cb_external_network="shc-n1",
             cb_network_record={"network": "n1", "externalNetwork": None, "placement": f"{NS}/p1", "publish": "Internal"},
             cb_cluster_network="10.128.0.0/14", cb_service_network="172.30.0.0/16", cb_machine_network="10.0.0.0/16",
             pf={"fabric": "f1", "service": fabric()["status"]["realization"]["service"]},
             po_cr_current={"resources": [{"status": {}}]},
             fake_os={"cluster-fips/present": {"network": "net-1", "api": {"address": "192.0.2.15", "id": "fip-a"},
                                               "ingress": {"address": "192.0.2.16", "id": "fip-i"}}})
    v.update(kw)
    return v


CARRIED = fabric(networks=[{"network": f"{NS}/n1", "prefixes": ["192.0.2.15/32", "192.0.2.16/32"]}])


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class OpenstackAddressesFile(unittest.TestCase):
    def run_a(self, kube, **kw):
        return H.run("platformopenshift_provision", "openstack_cluster_addresses.yml", addr_vars(**kw), k8s=kube,
                     result="[cb_api_vip, cb_ingress_vip]")

    @staticmethod
    def steps(tr):
        out = []
        for t in tr:
            if "patch" in t:
                b = t["body"]
                out.append("patch:" + ",".join(sorted(k for k in b if k not in ("conditions",)))
                           + (":" + b["conditions"][0]["type"] + "=" + b["conditions"][0]["status"] if b.get("conditions") else ""))
            elif "os" in t:
                out.append("os:" + t["os"]["action"] + "/" + t["os"].get("state", ""))
            elif "nudge" in t:
                out.append("nudge")
        return out

    def test_hybrid_order(self):
        rc, tr, out = self.run_a(k8s(fab=CARRIED))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.steps(tr), [
            "patch:buildName,clusterNetwork,externalNetwork,machineNetwork,network,placement,publish,serviceNetwork",
            "os:cluster-fips/present", "patch:floatingIPs", "patch:platformPrefixes", "nudge",
            "patch::Preflight=True"])
        rec = H.patches(tr)[0]["body"]
        self.assertEqual((rec["buildName"], rec["publish"], rec["externalNetwork"]), ("shc-oso-acme-c1", "Internal", None))
        req = H.os_calls(tr, "cluster-fips")[0]
        self.assertEqual((req["project_id"], req["network_id"], req["descriptions"]),
                         ("proj-1", "net-1", {"api": "shc-oso-acme-c1-api", "ingress": "shc-oso-acme-c1-ingress"}))
        self.assertEqual(H.patches(tr)[1]["body"]["floatingIPs"],
                         {"network": "net-1", "api": {"address": "192.0.2.15", "id": "fip-a"},
                          "ingress": {"address": "192.0.2.16", "id": "fip-i"}})
        self.assertEqual(H.patches(tr)[2]["body"]["platformPrefixes"], ["192.0.2.15/32", "192.0.2.16/32"])
        self.assertEqual([t for t in tr if "nudge" in t][0]["nudge"], [{"kind": "HybridNetwork", "namespace": NS, "name": "n1"}])
        self.assertEqual(tr[-1]["result"], ["192.0.2.15", "192.0.2.16"])

    def test_external_path_has_no_prefixes(self):
        rc, tr, out = self.run_a(k8s(), cb_platform_path=False)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.steps(tr)[1:], ["os:cluster-fips/present", "patch:floatingIPs", "patch::Preflight=True"])

    def test_address_of_another_network_is_given_back(self):
        svc = fabric(networks=[{"network": "beta-ns/m", "prefixes": ["192.0.2.0/28"]}])["status"]["realization"]["service"]
        rc, tr, out = self.run_a(k8s(), pf={"fabric": "f1", "service": svc})
        self.assertNotEqual(rc, 0)
        self.assertIn("already reaches for another network", out)
        self.assertEqual(self.steps(tr)[1:], ["os:cluster-fips/present", "patch:floatingIPs", "os:cluster-fips/absent",
                                              "patch:floatingIPs"])
        rel = H.os_calls(tr, "cluster-fips")[1]
        self.assertEqual((rel["ids"], rel["descriptions"]),
                         (["fip-a", "fip-i"], {"api": "shc-oso-acme-c1-api", "ingress": "shc-oso-acme-c1-ingress"}))
        self.assertIsNone(H.patches(tr)[-1]["body"]["floatingIPs"])

    def test_refused_prefix_withdrawn_and_released(self):
        fab = fabric(refused=[{"network": f"{NS}/n1", "prefix": "192.0.2.16/32", "reason": "ServicePrefixInUse"}])
        rc, tr, out = self.run_a(k8s(fab=fab))
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.steps(tr)[1:], ["os:cluster-fips/present", "patch:floatingIPs", "patch:platformPrefixes",
                                              "nudge", "patch:platformPrefixes", "nudge", "os:cluster-fips/absent",
                                              "patch:floatingIPs"])
        self.assertIsNone(H.patches(tr)[3]["body"]["platformPrefixes"])

    def test_not_carried_yet_waits_and_keeps_the_record(self):
        rc, tr, out = self.run_a(k8s())
        self.assertEqual(rc, 0, out)
        steps = self.steps(tr)
        self.assertEqual(steps[-1], "patch:lastReconciledAt,message,ready,reason,status:Preflight=False")
        self.assertNotIn("os:cluster-fips/absent", steps)
        self.assertEqual(H.patches(tr)[-1]["body"]["reason"], "WaitingForPlatformAccess")
        self.assertNotIn("result", tr[-1])

    def test_installed_cluster_keeps_its_gateway_prefix(self):
        cur = {"resources": [{"status": {"clusterInstalled": True,
                                          "platformPrefixes": ["192.0.2.15/32", "192.0.2.16/32", "192.0.2.30/32"]}}]}
        fab = fabric(networks=[{"network": f"{NS}/n1", "prefixes": ["192.0.2.15/32", "192.0.2.16/32", "192.0.2.30/32"]}])
        rc, tr, out = self.run_a(k8s(fab=fab), po_cr_current=cur)
        self.assertEqual(rc, 0, out)
        self.assertEqual(H.patches(tr)[2]["body"]["platformPrefixes"], ["192.0.2.15/32", "192.0.2.16/32", "192.0.2.30/32"])


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class RouterGatewayFile(unittest.TestCase):
    def test_gateway_joins_the_prefixes(self):
        fab = fabric(networks=[{"network": f"{NS}/n1", "prefixes": ["192.0.2.15/32", "192.0.2.16/32", "192.0.2.30/32"]}])
        v = dict(addr_vars(), rg_infra_id="c1-x7k2p", cb_api_vip="192.0.2.15", cb_ingress_vip="192.0.2.16")
        routers = [{"name": "c1-x7k2p-external-router",
                    "external_gateway_info": {"external_fixed_ips": [{"ip_address": "192.0.2.30"}]}}]
        rc, tr, out = H.run("platformopenshift_provision", "router_gateway.yml", v, k8s=k8s(fab=fab), routers=routers,
                            result="rg_address")
        self.assertEqual(rc, 0, out)
        self.assertEqual([t for t in tr if "routers_info" in t][0], {"routers_info": "c1-x7k2p-external-router",
                                                                     "project": "proj-1", "scope": []})
        self.assertEqual(H.patches(tr)[0]["body"]["platformPrefixes"], ["192.0.2.15/32", "192.0.2.16/32", "192.0.2.30/32"])
        self.assertEqual(len(H.patches(tr)), 1)

    def test_refused_gateway_is_taken_out_not_fatal(self):
        fab = fabric(refused=[{"network": f"{NS}/n1", "prefix": "192.0.2.30/32", "reason": "ServicePrefixInUse"}])
        v = dict(addr_vars(), rg_infra_id="c1-x7k2p", cb_api_vip="192.0.2.15", cb_ingress_vip="192.0.2.16")
        routers = [{"name": "c1-x7k2p-external-router",
                    "external_gateway_info": {"external_fixed_ips": [{"ip_address": "192.0.2.30"}]}}]
        rc, tr, out = H.run("platformopenshift_provision", "router_gateway.yml", v, k8s=k8s(fab=fab), routers=routers,
                            result="rg_address")
        self.assertEqual(rc, 0, out)
        self.assertEqual(H.patches(tr)[-1]["body"]["platformPrefixes"], ["192.0.2.15/32", "192.0.2.16/32"])


def cloud_vars(cloud, publish, **kw):
    v = dict(BASE, **{f"{cloud}_env_name": "env1", f"{cloud}_network": "n1", f"{cloud}_publish": publish,
                      f"{cloud}_site": "site1", f"{cloud}_account": "111111111111", f"{cloud}_region": "region-1",
                      f"{cloud}_entity": "acme"},
             cb_member_role="automation", platform_spec={"type": cloud})
    v.update(kw)
    return v


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class CloudNetworkFiles(unittest.TestCase):
    def placements(self, kind):
        site = {"vpc": "vpc-1", "subnets": [{"id": "subnet-1", "az": "az-a"}]}
        return [placement(kind=kind, ra=None, site=site)]

    def test_internal_carries_the_subnet(self):
        for cloud, kind in (("aws", "CloudAWS"), ("gcp", "CloudGCP")):
            with self.subTest(cloud=cloud):
                fab = fabric(networks=[{"network": f"{NS}/n1", "prefixes": ["192.0.2.0/25"]}])
                rc, tr, out = H.run("platformopenshift_provision", f"{cloud}_clusterbuild_network.yml",
                                    cloud_vars(cloud, "Internal"), k8s=k8s(places=self.placements(kind), fab=fab),
                                    result="cb_machine_cidr")
                self.assertEqual(rc, 0, out)
                self.assertEqual(tr[-1]["result"], "192.0.2.0/25")
                pfx = [p["body"]["platformPrefixes"] for p in H.patches(tr) if "platformPrefixes" in p["body"]]
                self.assertEqual(pfx, [["192.0.2.0/25"]])
                self.assertFalse([t for t in tr if "k8s" in t])   # no annotation patch any more
                fab_req = [t["fabric"] for t in tr if "fabric" in t][0]
                self.assertEqual((fab_req["provider"], fab_req["network"]), (cloud, True))
                self.assertEqual(H.patches(tr)[-1]["body"]["conditions"][0]["reason"], "PreflightPassed")

    def test_internal_refused_before_the_prefix(self):
        for cloud, kind in (("aws", "CloudAWS"), ("gcp", "CloudGCP")):
            with self.subTest(cloud=cloud):
                rc, tr, out = H.run("platformopenshift_provision", f"{cloud}_clusterbuild_network.yml",
                                    cloud_vars(cloud, "Internal"),
                                    k8s=k8s(net=network_obj(reach=("False", "CircuitNotDeployed", "no circuit")),
                                            places=self.placements(kind)))
                self.assertNotEqual(rc, 0)
                self.assertIn("CircuitNotDeployed", out)
                self.assertFalse(H.patches(tr))

    def test_external_needs_no_platform_access(self):
        rc, tr, out = H.run("platformopenshift_provision", "gcp_clusterbuild_network.yml", cloud_vars("gcp", "External"),
                            k8s=k8s(net=network_obj(reach=("False", "NotRequested", "")),
                                    places=self.placements("CloudGCP"), fab={}),
                            result="cb_machine_cidr")
        self.assertEqual(rc, 0, out)
        self.assertFalse([p for p in H.patches(tr) if "platformPrefixes" in p["body"]])

    def test_internal_waits_while_not_carried(self):
        rc, tr, out = H.run("platformopenshift_provision", "aws_clusterbuild_network.yml", cloud_vars("aws", "Internal"),
                            k8s=k8s(places=self.placements("CloudAWS")), result="1")
        self.assertEqual(rc, 0, out)
        self.assertEqual(H.patches(tr)[-1]["body"]["reason"], "WaitingForPlatformAccess")
        self.assertNotIn("result", tr[-1])


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class OwnNetwork(unittest.TestCase):
    def test_ranges_from_spec_and_profile(self):
        v = dict(BASE, pon_site="site1", platform_spec={"type": "aws", "networking": {"machineNetwork": "10.20.0.0/16"}})
        rc, tr, out = H.run("platformopenshift_provision", "preflight_own_network.yml", v,
                            k8s={f"TenantProfile/{NS}/shc": TPROFILE}, result="cb_machine_cidr")
        self.assertEqual(rc, 0, out)
        self.assertEqual(tr[-1]["result"], "10.20.0.0/16")
        fab = [t["fabric"] for t in tr if "fabric" in t][0]
        self.assertEqual((fab["space"], fab["provider"], fab["network"]), ([], "aws", False))
        self.assertEqual([x["prefix"] for x in fab["ranges"]], [HUB, "100.64.0.0/16"])


# ---- teardown -----------------------------------------------------------------------------------------------------

TD_BASE = dict(BASE, platformopenshift_cr_name="c1", platformopenshift_cr_namespace=NS, po_network="n1",
               po_platform_prefixes=["192.0.2.15/32", "192.0.2.16/32"])


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class TeardownWithdraw(unittest.TestCase):
    def test_withdrawn(self):
        rc, tr, out = H.run("platformopenshift_teardown", "withdraw_prefixes.yml", TD_BASE, k8s=k8s(), result="1")
        self.assertEqual(rc, 0, out)
        self.assertIsNone(H.patches(tr)[0]["body"]["platformPrefixes"])
        self.assertTrue([t for t in tr if "nudge" in t])

    def test_still_carried_fails_the_step(self):
        rc, tr, out = H.run("platformopenshift_teardown", "withdraw_prefixes.yml", TD_BASE, k8s=k8s(fab=CARRIED), result="1")
        self.assertNotEqual(rc, 0)
        self.assertIn("The platform still reaches 192.0.2.15/32, 192.0.2.16/32", out)

    def test_network_gone(self):
        rc, tr, out = H.run("platformopenshift_teardown", "withdraw_prefixes.yml", TD_BASE,
                            k8s={f"HybridFabric/{PLAT}/f1": CARRIED}, result="1")
        self.assertEqual(rc, 0, out)
        self.assertFalse([t for t in tr if "nudge" in t])


OSO = {"metadata": {"name": "env1", "namespace": NS}, "spec": {"cloudRef": {"name": "site1"}},
       "status": {"projectId": "proj-1", "cloudInfrastructure": "site1"}}


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class TeardownRelease(unittest.TestCase):
    def run_r(self, kube, **kw):
        v = dict(TD_BASE, po_oso_env="env1", po_fip_ids=["fip-a", "fip-i"], po_build_name="shc-oso-acme-c1")
        v.update(kw)
        return H.run("platformopenshift_teardown", "release_fips.yml", v, k8s=kube, result="1")

    def test_release_by_record_and_descriptions(self):
        rc, tr, out = self.run_r({f"CloudOSO/{NS}/env1": OSO, f"CloudInfrastructure/sovereign-cloud/site1": SITE})
        self.assertEqual(rc, 0, out)
        req = H.os_calls(tr, "cluster-fips")[0]
        self.assertEqual((req["state"], req["project_id"], req["ids"], req["descriptions"]),
                         ("absent", "proj-1", ["fip-a", "fip-i"],
                          {"api": "shc-oso-acme-c1-api", "ingress": "shc-oso-acme-c1-ingress"}))
        self.assertIsNone(H.patches(tr)[-1]["body"]["floatingIPs"])

    def test_environment_gone(self):
        rc, tr, out = self.run_r({})
        self.assertEqual(rc, 0, out)
        self.assertFalse(H.os_calls(tr))

    def test_environment_without_project(self):
        oso = dict(OSO, status={"cloudInfrastructure": "site1"})
        rc, tr, out = self.run_r({f"CloudOSO/{NS}/env1": oso, f"CloudInfrastructure/sovereign-cloud/site1": SITE})
        self.assertNotEqual(rc, 0)
        self.assertIn("Cannot release the cluster's floating IPs fip-a, fip-i", out)


# ---- renders --------------------------------------------------------------------------------------------------------

@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class Renders(unittest.TestCase):
    V = {"cb_ocp_name": "c1", "cb_cluster_name": "shc-oso-acme-c1", "cb_cluster_domain": "env1.acme.tenants.example.org",
         "cb_control_plane_count": "3", "cb_worker_count": "0", "cb_controller_flavor": "m.large",
         "cb_worker_flavor": "m.large", "cb_cluster_network": "10.200.0.0/16", "cb_host_prefix": "24",
         "cb_service_network": "172.31.0.0/16", "cb_machine_network": "10.10.0.0/16", "cb_external_network": "shc-n1",
         "cb_api_vip": "192.0.2.15", "cb_ingress_vip": "192.0.2.16", "cb_glance_image": "", "cb_external_dns": [],
         "cb_trust_bundle": "", "cb_pull_secret_json": "{}", "cb_ssh_public_key": "ssh-ed25519 AAAA example",
         "platformopenshift_managed_cluster_set": "tenant-clusters", "cb_image_set": "img4", "cb_pull_secret_vault": "p",
         "cb_pull_secret_vault_property": "p", "cb_ssh_key_vault": "s", "cb_provider_creds_vault": "c",
         "cb_controller_flavor_": "", "cb_install_config_vault": "i"}

    def render(self, name, **kw):
        import yaml
        out = run_play([{"ansible.builtin.set_fact": {"_r": "{{ lookup('template', '%s') }}" % os.path.join(ROLE, "templates", name)}},
                        write_out("_r")], dict(self.V, **kw))
        return yaml.safe_load(out)

    def test_install_config_openstack(self):
        ic = self.render("install-config-openstack.yaml.j2")
        self.assertEqual(ic["metadata"]["name"], "c1")
        self.assertEqual(ic["compute"][0]["replicas"], 0)
        self.assertEqual(ic["networking"]["clusterNetwork"], [{"cidr": "10.200.0.0/16", "hostPrefix": 24}])
        self.assertEqual(ic["networking"]["serviceNetwork"], ["172.31.0.0/16"])
        self.assertEqual(ic["networking"]["machineNetwork"], [{"cidr": "10.10.0.0/16"}])
        self.assertEqual(ic["publish"], "External")
        osp = ic["platform"]["openstack"]
        self.assertEqual((osp["externalNetwork"], osp["apiFloatingIP"], osp["ingressFloatingIP"]),
                         ("shc-n1", "192.0.2.15", "192.0.2.16"))

    def test_build_values_name_and_cluster_name(self):
        bv = self.render("build-values.yaml.j2")
        self.assertEqual((bv["cluster"]["name"], bv["cluster"]["clusterName"], bv["cluster"]["workerCount"]),
                         ("shc-oso-acme-c1", "c1", 0))
        self.assertEqual(bv["provider"]["openstack"]["externalNetwork"], "shc-n1")

    def test_aws_and_gcp_ranges(self):
        for name in ("install-config.yaml.j2", "install-config-gcp.yaml.j2"):
            ic = self.render(name, cb_install_base_domain="example.org", cb_machine_cidr="10.10.0.0/16", cb_region="r1",
                             cb_app_name="shc-aws-acme-c1", cb_project="p1")
            self.assertEqual(ic["networking"]["clusterNetwork"], [{"cidr": "10.200.0.0/16", "hostPrefix": 24}], name)
            self.assertEqual(ic["networking"]["machineNetwork"], [{"cidr": "10.10.0.0/16"}], name)
            self.assertEqual(ic["compute"][0]["replicas"], 0, name)


# ---- structure --------------------------------------------------------------------------------------------------------

def text(*p):
    with open(os.path.join(*p)) as fh:
        return fh.read()


def names(path):
    return [t.get("name") for t in walk(load(path))]


class Structure(unittest.TestCase):
    def test_no_platform_reach_annotation_and_no_namespace_fallback(self):
        for d in (os.path.join(ROLE, "tasks"), os.path.join(TD, "tasks")):
            for f in os.listdir(d):
                t = text(d, f)
                self.assertNotIn("shc.signal9.gg/platform-reach", t, f)
                self.assertNotIn("default('sovereign-cloud')", t, f)
        self.assertEqual(load(os.path.join(ROLE, "defaults", "main.yml"))["fabric_platform_namespace"], "sovereign-cloud")

    def test_openstack_preflight_before_the_build(self):
        n = names(os.path.join(TASKS, "openstack.yml"))
        self.assertLess(n.index("Check the cluster before anything is created"), n.index("Run OpenStack cluster build"))
        cb = names(os.path.join(TASKS, "openstack_clusterbuild.yml"))
        for later in ("Create Route53 records for the cluster", "Create Designate DNS records for internet-facing cluster",
                      "Write install-config to Vault", "Deploy mce-cluster-build chart"):
            self.assertLess(cb.index("The cluster's recorded build and addresses"), cb.index(later), later)
        self.assertLess(cb.index("Deploy mce-cluster-build chart"),
                        cb.index("The cluster router's gateway address joins the cluster's prefixes"))

    def test_no_environment_floating_ips_read(self):
        for f in ("openstack.yml", "openstack_clusterbuild.yml", "designate_dns.yml"):
            t = text(TASKS, f)
            self.assertNotIn("apiVIP", t.replace("CloudOSO apiVIP", ""), f)
            self.assertNotIn("_oso_st.externalNetwork", t, f)

    def test_teardown_reverse_order(self):
        steps = [t["ansible.builtin.set_fact"]["teardown_step"] for t in walk(load(os.path.join(TD, "tasks", "main.yml")))
                 if "teardown_step" in (t.get("ansible.builtin.set_fact") or {})]
        order = ["acm-detach", "deprovision", "dns", "service-prefixes", "floating-ips", "central-namespace", "vault"]
        self.assertEqual([s for s in steps if s in order], order)

    def test_teardown_has_no_cr_name_vault_paths(self):
        t = text(TD, "tasks", "main.yml")
        self.assertNotIn("'cluster/oso/' ~ platformopenshift_cr_name", t)

    def test_rescue_writes_the_refusal_reason(self):
        main = load(os.path.join(TASKS, "main.yml"))
        rescue = main[0]["rescue"][0]["vars"]["cr_status_body"]
        self.assertIn("po_fail_reason", rescue["reason"])
        self.assertIn("po_preflight_condition", rescue["conditions"])

    def test_counts_keep_zero(self):
        os_facts = by_name(load(os.path.join(TASKS, "openstack.yml")), "Set OpenStack spec facts")["ansible.builtin.set_fact"]
        self.assertNotIn(", true", os_facts["oso_worker_count"])
        self.assertNotIn(", true", os_facts["oso_control_plane_count"])
        for f in ("aws.yml", "gcp.yml"):
            facts = by_name(load(os.path.join(TASKS, f)), f"Set {f[:3].upper()} spec facts")["ansible.builtin.set_fact"]
            self.assertNotIn(", true", facts[f"{f[:3]}_worker_count"])


# ---- the environment (CloudOSO) holds no pair; the teardown's build name and Vault paths --------------------------------

OSO_ROLE = os.path.join(ROOT, "eda", "rulebooks", "roles", "cloudoso_provision")


def oso_legacy_tasks():
    t = load(os.path.join(OSO_ROLE, "tasks", "environmentprep.yml"))
    return [by_name(t, "The environment's legacy floating IP pair"), by_name(t, "List the clusters of this namespace"),
            by_name(t, "Release the legacy pair when no cluster names the environment")]


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class EnvironmentPair(unittest.TestCase):
    def run_e(self, status, clusters):
        v = dict(BASE, cloudoso_cr_name="env1", cloudoso_cr_namespace=NS,
                 cloudoso_cr_lookup={"resources": [{"status": status}]}, ep_os={"project": {"id": "proj-1"}})
        return H.run("cloudoso_provision", "environmentprep.yml", v, tasks=oso_legacy_tasks(),
                     k8s={f"PlatformOpenshift/{NS}/": clusters}, result="1")

    LEGACY = {"apiVIP": "203.0.113.5", "ingressVIP": "203.0.113.6", "externalNetwork": "public",
              "apiFloatingIPId": "fip-1", "ingressFloatingIPId": "fip-2"}

    def test_released_when_no_cluster_names_the_environment(self):
        other = {"spec": {"type": "openstack", "openstack": {"environment": "env2"}}}
        aws = {"spec": {"type": "aws", "aws": {"environment": "env1"}}}
        rc, tr, out = self.run_e(self.LEGACY, [other, aws])
        self.assertEqual(rc, 0, out)
        req = H.os_calls(tr, "cluster-fips")[0]
        self.assertEqual((req["state"], req["project_id"], req["ids"], req["descriptions"]),
                         ("absent", "proj-1", ["fip-1", "fip-2"],
                          {"api": "sovereign-env1-api", "ingress": "sovereign-env1-ingress"}))
        body = H.patches(tr, "cloudosos")[0]["body"]
        self.assertEqual(body, {k: None for k in self.LEGACY})

    def test_kept_while_a_cluster_names_it(self):
        rc, tr, out = self.run_e(self.LEGACY, [{"spec": {"type": "openstack", "openstack": {"environment": "env1"}}}])
        self.assertEqual(rc, 0, out)
        self.assertFalse(H.os_calls(tr))
        self.assertFalse(H.patches(tr, "cloudosos"))

    def test_nothing_to_do_without_a_pair(self):
        rc, tr, out = self.run_e({"projectId": "proj-1", "ready": True}, [])
        self.assertEqual(rc, 0, out)
        self.assertFalse(H.os_calls(tr))
        self.assertFalse([t for t in tr if "k8s_info" in t])


class EnvironmentStructure(unittest.TestCase):
    def test_provision_asks_for_no_floating_ips_or_external_network(self):
        t = load(os.path.join(OSO_ROLE, "tasks", "environmentprep.yml"))
        req = by_name(t, "Create or reconcile the project and its application credential")["vars"]["os_request"]
        self.assertNotIn("fip_descriptions", req)
        self.assertNotIn("external_network", req)
        assert_ = by_name(t, "Validate the CloudOSO environment settings")["ansible.builtin.assert"]["that"]
        self.assertFalse([x for x in assert_ if "external_network" in x])
        main = text(OSO_ROLE, "tasks", "main.yml")
        for key in ("apiVIP:", "ingressVIP:", "externalNetwork:", "apiFloatingIPId:", "ingressFloatingIPId:"):
            self.assertNotIn(key, main)


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class TeardownPaths(unittest.TestCase):
    def paths(self, cr, **kw):
        t = by_name(load(os.path.join(ROOT, "eda", "common", "tasks", "resolve_platformopenshift_paths.yml")),
                    "Derive the cluster's Vault paths")
        return run_play([t, write_out("[rpp_build_name_in_effect, rpp_vault_paths]")], dict({"rpp_cr": cr}, **kw))

    def test_openstack_build_paths(self):
        cr = {"metadata": {"name": "c1"}, "spec": {"type": "openstack", "openstack": {"environment": "env1"}},
              "status": {"buildName": "shc-oso-acme-c1"}}
        build, paths = self.paths(cr)
        self.assertEqual(build, "shc-oso-acme-c1")
        self.assertIn("cluster/oso/shc-oso-acme-c1/admin-kubeconfig", paths)
        self.assertIn("oso/projects/env1/clusters/c1/install-config", paths)
        self.assertFalse([p for p in paths if p.startswith("cluster/oso/c1/")])

    def test_openstack_without_record_uses_the_derived_name(self):
        cr = {"metadata": {"name": "c1"}, "spec": {"type": "openstack", "openstack": {"environment": "env1"}}, "status": {}}
        build, paths = self.paths(cr, rpp_build_name="shc-oso-acme-c1")
        self.assertEqual(build, "shc-oso-acme-c1")
        build, paths = self.paths(cr)   # no record, nothing derived: no cluster paths at all, never the CR name
        self.assertEqual(build, "")
        self.assertFalse([p for p in paths if p.startswith("cluster/oso/")])

    def test_aws_falls_back_to_the_cr_name(self):
        cr = {"metadata": {"name": "c1"}, "spec": {"type": "aws"}, "status": {}}
        self.assertEqual(self.paths(cr)[0], "c1")


if __name__ == "__main__":
    unittest.main()
