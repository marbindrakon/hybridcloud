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
    """H1: a build name is unique for every pair (tenant, cluster name), at most the limit, stable, and readable."""

    @staticmethod
    def name(entity, cluster, **kw):
        return P.build_name(dict({"prefix": "shc-oso", "entity": entity, "name": cluster}, **kw))["buildName"]

    def test_tenant_qualified_and_readable(self):
        self.assertRegex(self.name("acme", "web1"), r"^shc-oso-acme-web1-[0-9a-f]{8}$")

    def test_a_hyphen_from_either_side_does_not_collide(self):
        # the pair of the review: tenant acme-corp's prod and tenant acme's corp-prod read alike
        names = {self.name("acme-corp", "prod"), self.name("acme", "corp-prod"), self.name("acme", "corp.prod")}
        self.assertEqual(len(names), 3)
        self.assertTrue(all(n.startswith("shc-oso-acme-corp-prod-") for n in names))

    def test_one_tenant_dot_and_hyphen_do_not_collide(self):
        self.assertNotEqual(self.name("acme", "prod.1"), self.name("acme", "prod-1"))

    def test_truncation_does_not_collide(self):
        # two pairs whose readable parts are equal after the cut
        a, b = self.name("a" * 30, "b" * 40), self.name("a" * 30, "b" * 41)
        c, d = self.name("a" * 30 + "-b", "b" * 39), self.name("a" * 30, "b-" + "b" * 39)
        for n in (a, b, c, d):
            self.assertLessEqual(len(n), 53)
            self.assertRegex(n, r"^shc-oso-[a-z0-9-]*[a-z0-9]-[0-9a-f]{8}$")
        self.assertEqual(a[:-9], b[:-9])
        self.assertEqual(len({a, b, c, d}), 4)

    def test_stable(self):
        self.assertEqual(self.name("acme", "c1"), self.name("acme", "c1"))
        self.assertEqual(self.name("a" * 30, "b" * 40), self.name("a" * 30, "b" * 40))

    def test_cloud_limit(self):
        n = self.name("acme-corp", "a-rather-long-cluster-name", prefix="shc-aws", max=40)
        self.assertLessEqual(len(n), 40)
        self.assertTrue(n.startswith("shc-aws-acme-corp-a-rather"))
        self.assertNotEqual(n, self.name("acme-corp", "a-rather-long-cluster-name2", prefix="shc-aws", max=40))

    def test_two_tenants_same_cluster_name_differ(self):
        self.assertNotEqual(self.name("acme", "c1"), self.name("beta", "c1"))

    def test_needs_entity(self):
        with self.assertRaises(ValueError):
            P.build_name({"entity": "", "name": "c1"})

    def test_task_file_keeps_a_recorded_name(self):
        if not HAVE_ANSIBLE:
            self.skipTest("ansible-playbook not installed")
        rc, tr, out = H.run("platformopenshift_provision", "build_name.yml",
                            dict(BASE, bn_entity="acme", bn_name="c1", bn_recorded="shc-oso-acme-c1"),
                            result="[bn_build_name, bn_derived]")
        self.assertEqual(rc, 0, out)
        self.assertEqual(tr[-1]["result"][0], "shc-oso-acme-c1")
        self.assertEqual(tr[-1]["result"][1], self.name("acme", "c1"))
        rc, tr, out = H.run("platformopenshift_provision", "build_name.yml",
                            dict(BASE, bn_entity="acme", bn_name="c1", bn_prefix="shc-gcp", bn_max=40),
                            result="bn_build_name")
        self.assertEqual(rc, 0, out)
        self.assertEqual(tr[-1]["result"], self.name("acme", "c1", prefix="shc-gcp", max=40))


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

    def test_any_range_only_when_written_out(self):
        # the ranges are required with the service: 0.0.0.0/0 is "any", an empty list is not offered
        self.assertEqual(judge(fabric=fabric(ranges=("0.0.0.0/0",)))["reason"], "")
        self.assertEqual(judge(fabric=fabric(ranges=()))["reason"], "ServiceNotOffered")

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

    def test_a_refusal_wins_over_a_wait(self):
        # gate F3 (r10): the IPv6-only placement with router attachments only, Ready with router attachments pending
        # (the job's RouterAdvertisementFailed): refused PlacementHasNoIPv4, never WaitingForPlacement
        v6 = placement(ra_state="pending", subnets=(("6", "2001:db8:1::/64"),))
        v6["spec"].update(ipv6={"prefixLength": 64}, use={"virtualMachines": False, "routerAttachments": True})
        r = judge(placements=[v6])
        self.assertEqual((r["reason"], r["wait"]), ("PlacementHasNoIPv4", False), r["message"])
        # the same while the placement is not ready yet (no subnet in its status at all)
        v6["status"].update(ready=False, subnets=[])
        self.assertEqual(judge(placements=[v6])["reason"], "PlacementHasNoIPv4")
        # other permanent refusals while the placement is not ready
        self.assertEqual(judge(placements=[placement(ready=False, ra=False)])["reason"], "RouterAttachmentsOff")
        self.assertEqual(judge(placements=[placement(ready=False)],
                               networkObj=network_obj(reach=("False", "NotRequested", "off")))["reason"],
                         "PlatformAccessOff")
        self.assertEqual(judge(placements=[placement(ready=False)],
                               fabric=fabric(ranges=("203.0.113.0/24",)))["reason"], "OutsideAttachableRanges")
        self.assertEqual(judge(placements=[placement(ra_state="pending")], fabric=fabric(endpoints=()))["reason"],
                         "HubEndpointNotAdmitted")
        # a placement being deleted does not come back: refused, not waited for
        gone = placement(ready=False)
        gone["metadata"]["deletionTimestamp"] = "2026-11-11T19:50:00Z"
        self.assertEqual(judge(placements=[gone])["reason"], "NetworkNotFound")
        # two waits: the first is reported, with the placement's facts
        r = judge(placements=[placement(ready=False)], networkObj=network_obj(reach=None))
        self.assertEqual((r["reason"], r["wait"], r["placement"]), ("WaitingForPlacement", True, f"{NS}/p1"))
        self.assertEqual(r["space"], ["192.0.2.0/24"])

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
        routers = [{"name": "infra-x-external-router", "external_gateway_info": {"network_id": "net-1", "external_fixed_ips": [
            {"ip_address": "2001:db8::5"}, {"ip_address": "192.0.2.30"}]}}, {"name": "other"}]
        gw = lambda rs, n="infra-x-external-router": P.router_gateway({"routers": rs, "name": n, "network_id": "net-1"})["address"]  # noqa: E731
        self.assertEqual(gw(routers), "192.0.2.30")
        self.assertEqual(gw(routers, "infra-y-external-router"), "")

    def test_router_gateway_a_planted_router_counts_for_nothing(self):
        # Review R2 L9: the tenant's project can hold another router of the installer's router name
        mine = {"name": "infra-x-external-router",
                "external_gateway_info": {"network_id": "net-1", "external_fixed_ips": [{"ip_address": "192.0.2.30"}]}}
        planted = {"name": "infra-x-external-router",
                   "external_gateway_info": {"network_id": "net-1", "external_fixed_ips": [{"ip_address": "192.0.2.99"}]}}
        r = P.router_gateway({"routers": [planted, mine], "name": "infra-x-external-router", "network_id": "net-1"})
        self.assertEqual(r["address"], "")
        self.assertIn("2 routers", r["note"])
        elsewhere = dict(planted, external_gateway_info={"network_id": "net-9", "external_fixed_ips": [{"ip_address": "203.0.113.9"}]})
        r = P.router_gateway({"routers": [elsewhere], "name": "infra-x-external-router", "network_id": "net-1"})
        self.assertEqual(r["address"], "")
        self.assertIn("another network", r["note"])


# ---- task files end to end ----------------------------------------------------------------------------------------

CR = {"name": "c1", "namespace": NS}
BASE = {"ansible_operator_meta": CR, "services_api_host": "https://services.example.org:6443",
        "services_bearer_token": "token-not-in-data-xyz", "central_api_host": "https://198.18.0.10:6443",
        "central_bearer_token": "token-not-in-data-xyz", "po_uid": "uid-c1",
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


NAME_OK = {"network-name-check": {"name": "x", "visible": [{"id": "net-1", "project_id": "proj-platform", "shared": False,
                                                             "external": True}], "unique": True, "external": True}}


def os_preflight_vars(**kw):
    v = dict(BASE, oso_env_name="env1", oso_network="n1", oso_external_choice="", cb_oso_site="site1",
             cb_app_name="shc-oso-acme-c1", platform_spec={"type": "openstack", "openstack": {"environment": "env1"}},
             po_cr_current={"resources": [{"status": {}}]}, cb_oso_project_id="proj-1", cb_fip_account="sites/site1",
             fake_os=NAME_OK)
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
        self.assertFalse([t for t in tr if "patch" in t])
        # the only OpenStack call is the read of the network's name (Review R2 M3)
        self.assertEqual(H.os_calls(tr), [{"action": "network-name-check", "project_id": "proj-1", "name": "shc-n1",
                                           "network_id": "net-1"}])

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

    def test_router_attachments_off_while_holding_addresses_names_them(self):
        # Review R2 L10: the refusal after the floating IPs were allocated says they are kept and how to go on
        rc, tr, out = self.run_op(k8s(places=[placement(ra=False)]), cb_api_vip="192.0.2.15", cb_ingress_vip="192.0.2.16")
        self.assertNotEqual(rc, 0)
        self.assertIn("keeps its floating IPs 192.0.2.15 and 192.0.2.16", out)
        self.assertIn("turn router attachments back on", out)
        self.assertFalse(H.os_calls(tr))
        rc, tr, out = self.run_op(k8s(places=[placement(ra=False)]))
        self.assertNotIn("keeps its floating IPs", out)

    def test_network_name_must_lead_to_the_network_alone(self):
        # Review R2 M3: the installer takes the external network by name
        two = {"network-name-check": {"visible": [{"id": "net-1", "project_id": "proj-platform", "external": True},
                                                  {"id": "net-9", "project_id": "proj-1", "external": False}],
                                      "unique": False, "external": True}}
        rc, tr, out = self.run_op(k8s(), fake_os=two)
        self.assertNotEqual(rc, 0)
        self.assertIn("sees 2 network(s) named shc-n1 (net-1; net-9, your own)", out)
        self.assertFalse([t for t in tr if "patch" in t or "create" in t or "fabric" in t])
        # a placed network the project does not see yet: wait
        none = {"network-name-check": {"visible": [], "unique": False, "external": False}}
        rc, tr, out = self.run_op(k8s(), fake_os=none)
        self.assertEqual(rc, 0, out)
        self.assertEqual(H.patches(tr)[-1]["body"]["reason"], "WaitingForPlacement")
        # the global external network path: the name the tenant chose, unique in what the project sees
        ext = k8s(extra={f"CloudInfrastructure/{PLAT}/site1": SITE, f"TenantProfile/{NS}/shc": TPROFILE})
        rc, tr, out = self.run_op(ext, oso_network="", fake_os=two)
        self.assertNotEqual(rc, 0)
        self.assertEqual(H.os_calls(tr)[0]["name"], "public")
        self.assertEqual(H.os_calls(tr)[0]["network_id"], "ext-1")
        rc, tr, out = self.run_op(ext, oso_network="", fake_os=none)
        self.assertNotEqual(rc, 0)
        self.assertIn("does not see network public (ext-1)", out)

    def test_name_listed_with_the_installers_credential(self):
        # gate F1: the site's cloud-wide external network `internet` (external, not shared, owned by the admin project,
        # no policy the project can read) as Neutron lists it to the environment's credential: accepted
        inet = {"network-name-check": {"name": "public", "project_id": "proj-1", "visible": [
            {"id": "ext-1", "project_id": "proj-admin", "shared": False, "external": True}], "unique": True,
            "external": True}}
        ext = k8s(extra={f"CloudInfrastructure/{PLAT}/site1": SITE, f"TenantProfile/{NS}/shc": TPROFILE})
        rc, tr, out = self.run_op(ext, oso_network="", fake_os=inet, fake_cacert={"sites/site1": "/tmp/site1-ca.crt"})
        self.assertEqual(rc, 0, out)
        self.assertEqual(tr[-1]["result"]["ext"], "public")
        call = [t for t in tr if "os" in t][0]
        # listed as the environment's project, with the credential the installer gets, and the site's CA bundle
        self.assertEqual((call["as"], call["cacert"]), ("oso/projects/env1/clouds-config", "/tmp/site1-ca.crt"))
        self.assertEqual(call["os"]["project_id"], "proj-1")
        # seen, but not as an external network: refused on the external path, a wait on a placed network
        seen = {"network-name-check": {"visible": [{"id": "ext-1", "project_id": "proj-admin", "shared": True,
                                                    "external": False}], "unique": True, "external": False}}
        rc, tr, out = self.run_op(ext, oso_network="", fake_os=seen)
        self.assertNotEqual(rc, 0)
        self.assertIn("does not see network public (ext-1) as an external network", out)
        rc, tr, out = self.run_op(k8s(), fake_os={"network-name-check": dict(
            seen["network-name-check"], visible=[{"id": "net-1", "project_id": "proj-platform", "external": False}])})
        self.assertEqual(rc, 0, out)
        self.assertEqual(H.patches(tr)[-1]["body"]["reason"], "WaitingForPlacement")
        self.assertIn("as an external network yet", H.patches(tr)[-1]["body"]["message"])

    def test_a_permanent_refusal_wins_over_a_wait(self):
        # the placement is not ready yet (a wait), but the cluster's ranges overlap a reserved range (spec.networking
        # cannot change): refused at once, not after the wait
        err = {"check-cluster-networks": {"errors": ["serviceNetwork 198.18.0.0/16 overlaps 198.18.0.0/15, reserved by "
                                                     "the platform: x"], "reason": "PrefixReserved"}}
        rc, tr, out = self.run_op(k8s(places=[placement(ready=False, site={})]), fake_fabric=err)
        self.assertNotEqual(rc, 0)
        self.assertIn("overlaps 198.18.0.0/15", out)
        self.assertFalse(H.patches(tr))
        self.assertFalse(H.os_calls(tr))                  # no network id yet: the name is not looked up
        # the build name taken while the placement waits: refused
        ns = {f"Namespace//shc-oso-acme-c1": {"metadata": {"name": "shc-oso-acme-c1"}}}
        rc, tr, out = self.run_op(k8s(places=[placement(ready=False)], extra=ns))
        self.assertNotEqual(rc, 0)
        self.assertIn("the build name shc-oso-acme-c1 is already in use", out)
        # another network of the name while router attachments are still pending: refused, not waited for
        two = {"network-name-check": {"visible": [{"id": "net-1", "project_id": "proj-platform", "external": False},
                                                  {"id": "net-9", "project_id": "proj-1", "external": False}],
                                      "unique": False, "external": False}}
        rc, tr, out = self.run_op(k8s(places=[placement(ra_state="pending")]), fake_os=two)
        self.assertNotEqual(rc, 0)
        self.assertIn("sees 2 network(s) named shc-n1", out)
        # nothing refuses: the wait is recorded once, after every check ran, and nothing after it runs
        rc, tr, out = self.run_op(k8s(places=[placement(ready=False)]))
        self.assertEqual(rc, 0, out)
        p = H.patches(tr)
        self.assertEqual([x["body"]["reason"] for x in p], ["WaitingForPlacement"])
        self.assertTrue([t for t in tr if "fabric" in t])          # the ranges were checked first
        self.assertNotIn("result", tr[-1])

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
             pf={"fabric": "f1", "networkId": "net-1", "service": fabric()["status"]["realization"]["service"]},
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
            elif "create" in t:
                out.append("create:" + t["create"])
        return out

    def test_hybrid_order(self):
        rc, tr, out = self.run_a(k8s(fab=CARRIED))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.steps(tr), [
            "patch:buildName,clusterNetwork,externalNetwork,machineNetwork,network,placement,publish,serviceNetwork",
            "create:/api/v1/namespaces/shc-oso-acme-c1", "os:cluster-fips/present", "patch:floatingIPs", "patch:platformPrefixes", "nudge",
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
        self.assertEqual(self.steps(tr)[2:], ["os:cluster-fips/present", "patch:floatingIPs", "patch::Preflight=True"])

    def test_address_of_another_network_is_given_back(self):
        svc = fabric(networks=[{"network": "beta-ns/m", "prefixes": ["192.0.2.0/28"]}])["status"]["realization"]["service"]
        rc, tr, out = self.run_a(k8s(), pf={"fabric": "f1", "service": svc})
        self.assertNotEqual(rc, 0)
        self.assertIn("already reaches for another network", out)
        self.assertEqual(self.steps(tr)[2:], ["os:cluster-fips/present", "patch:floatingIPs", "os:cluster-fips/absent",
                                              "patch:floatingIPs"])
        rel = H.os_calls(tr, "cluster-fips")[1]
        self.assertEqual((rel["ids"], rel["descriptions"]),
                         (["fip-a", "fip-i"], {"api": "shc-oso-acme-c1-api", "ingress": "shc-oso-acme-c1-ingress"}))
        self.assertIsNone(H.patches(tr)[-1]["body"]["floatingIPs"])

    def test_refused_prefix_withdrawn_and_released(self):
        fab = fabric(refused=[{"network": f"{NS}/n1", "prefix": "192.0.2.16/32", "reason": "ServicePrefixInUse"}])
        rc, tr, out = self.run_a(k8s(fab=fab))
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.steps(tr)[2:], ["os:cluster-fips/present", "patch:floatingIPs", "patch:platformPrefixes",
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

    def test_installed_or_started_cluster_keeps_its_addresses_on_a_refusal(self):
        # Review R2 M1: a refused prefix (here the platform narrowed its attachable ranges) for a cluster whose install
        # was started or is done reports the refusal and gives nothing back
        fab = fabric(refused=[{"network": f"{NS}/n1", "prefix": "192.0.2.16/32", "reason": "OutsideAttachableRanges"}])
        for st in ({"clusterInstalled": True, "platformPrefixes": ["192.0.2.15/32", "192.0.2.16/32"]},
                   {"installStarted": True}):
            rc, tr, out = self.run_a(k8s(fab=fab), po_cr_current={"resources": [{"status": dict(st, buildName="shc-oso-acme-c1")}]})
            self.assertNotEqual(rc, 0)
            steps = self.steps(tr)
            self.assertNotIn("os:cluster-fips/absent", steps, st)
            self.assertFalse([p for p in H.patches(tr) if "platformPrefixes" in p["body"] and p["body"]["platformPrefixes"] is None])
            self.assertFalse([p for p in H.patches(tr) if "floatingIPs" in p["body"] and p["body"]["floatingIPs"] is None])
            self.assertIn("keeps its floating IPs 192.0.2.15 and 192.0.2.16", out)
        # the same refusal before the install still gives them back
        rc, tr, out = self.run_a(k8s(fab=fab))
        self.assertIn("os:cluster-fips/absent", self.steps(tr))
        # Review R3 (test quality, R2 M1): the realistic trigger, a floating IP that another network's accepted prefix
        # now covers (fips-check ServicePrefixInUse): an installed cluster keeps its addresses and its prefixes
        other = fabric(networks=[{"network": f"{NS}/n2", "prefixes": ["192.0.2.0/28"]}])
        pf = {"fabric": "f1", "networkId": "net-1", "service": other["status"]["realization"]["service"]}
        rc, tr, out = self.run_a(k8s(fab=other), pf=pf, po_cr_current={"resources": [{"status": {
            "clusterInstalled": True, "platformPrefixes": ["192.0.2.15/32", "192.0.2.16/32"], "buildName": "shc-oso-acme-c1"}}]})
        self.assertNotEqual(rc, 0)
        self.assertIn("Floating IP 192.0.2.15 lies in 192.0.2.0/28, which the platform already reaches for another "
                      "network. The cluster keeps", out)
        self.assertNotIn("new addresses are chosen", out)
        self.assertNotIn("os:cluster-fips/absent", self.steps(tr))
        self.assertFalse([p for p in H.patches(tr) if "platformPrefixes" in p["body"] and p["body"]["platformPrefixes"] is None])
        self.assertIn("keeps its floating IPs 192.0.2.15 and 192.0.2.16", out)

    def test_install_started_is_recorded_before_the_helm_release(self):
        names = [t.get("name") for t in load(os.path.join(TASKS, "openstack_clusterbuild.yml"))]
        rec = [i for i, t in enumerate(load(os.path.join(TASKS, "openstack_clusterbuild.yml")))
               if "installStarted" in str(t.get("vars", {}).get("cr_status_body", {}))]
        self.assertTrue(rec)
        self.assertLess(rec[0], names.index("Deploy mce-cluster-build chart"))

    def test_installed_cluster_keeps_its_gateway_prefix(self):
        cur = {"resources": [{"status": {"clusterInstalled": True,
                                          "platformPrefixes": ["192.0.2.15/32", "192.0.2.16/32", "192.0.2.30/32"]}}]}
        fab = fabric(networks=[{"network": f"{NS}/n1", "prefixes": ["192.0.2.15/32", "192.0.2.16/32", "192.0.2.30/32"]}])
        rc, tr, out = self.run_a(k8s(fab=fab), po_cr_current=cur)
        self.assertEqual(rc, 0, out)
        self.assertEqual(H.patches(tr)[2]["body"]["platformPrefixes"], ["192.0.2.15/32", "192.0.2.16/32", "192.0.2.30/32"])


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class DnsRecordedBeforeCreated(unittest.TestCase):
    """Review R2 M4: the cluster's DNS records are in status.dns before they are created."""

    def test_designate(self):
        v = dict(BASE, cb_api_vip="192.0.2.15", cb_ingress_vip="192.0.2.16", cb_ocp_name="c1",
                 cb_cluster_domain="env1.example.org", cb_cluster_name="shc-oso-acme-c1", use_designate_dns=True,
                 designate_zone_id="zone-1", designate_project_id="dnsproj-1", designate_admin_vault_path="sites/site1",
                 fake_os={"dns-upsert": {"records": [{"zoneId": "zone-1", "zone": "env1.example.org.",
                                                      "name": "api.c1.env1.example.org.", "type": "A", "state": "created"}]}})
        rc, tr, out = H.run("platformopenshift_provision", "designate_dns.yml", v, result="cb_dns_records")
        self.assertEqual(rc, 0, out)
        order = [("patch" if "patch" in t else "os:" + t["os"]["action"]) for t in tr if "patch" in t or "os" in t]
        self.assertEqual(order, ["patch", "os:dns-upsert"])
        dns = H.patches(tr)[0]["body"]["dns"]
        self.assertEqual((dns["provider"], dns["projectId"]), ("designate", "dnsproj-1"))
        self.assertEqual([(r["zoneId"], r["name"], r["type"]) for r in dns["records"]],
                         [("zone-1", "api.c1.env1.example.org.", "A"), ("zone-1", "*.apps.c1.env1.example.org.", "A")])
        self.assertTrue(H.patches(tr)[0]["partial"])

    def test_route53(self):
        tasks = load(os.path.join(TASKS, "openstack_clusterbuild.yml"))
        block = by_name(tasks, "Create Route53 records for the cluster")["block"]
        names = [t["name"] for t in block]
        self.assertLess(names.index("Record the Route53 records before they are created"),
                        names.index("Create Route53 A records for the API and apps wildcard"))


ACCOUNT_FILE_FAKES = {
    # the account writes /tmp/eda-<tag>-clouds.yaml as the real one does; the call fails
    "openstack_account.yml": """- ansible.builtin.copy:
    dest: "/tmp/eda-{{ os_account_tag }}-clouds.yaml"
    content: "clouds: {}"
- ansible.builtin.set_fact:
    os_account_clouds_yaml: "/tmp/eda-{{ os_account_tag }}-clouds.yaml"
    os_account_cacert: ""
""",
    "openstack_call.yml": "- ansible.builtin.fail:\n    msg: the cloud did not answer\n",
}


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class AccountFilesGoOnFailure(unittest.TestCase):
    """Review R2 L11: the site account's clouds.yaml in /tmp is removed also when the call with it fails."""

    def check(self, task_file, tag, *more, **kw):
        paths = [f"/tmp/eda-{t}-clouds.yaml" for t in (tag,) + more]
        for path in paths:
            if os.path.exists(path):
                os.unlink(path)
        rc, tr, out = H.run("platformopenshift_provision", task_file, dict(BASE, **kw), fakes=ACCOUNT_FILE_FAKES)
        self.assertNotEqual(rc, 0)
        self.assertIn("the cloud did not answer", out)
        for path in paths:
            self.assertFalse(os.path.exists(path), f"{task_file} left {path}")

    def test_files(self):
        b = f"shc-oso-l11-{os.getpid()}"
        self.check("cluster_fips.yml", f"po-fips-{b}", cb_app_name=b, cf_project_id="proj-1", cf_network_id="net-1",
                   cf_account="sites/site1")
        self.check("cluster_fips_release.yml", f"po-fips-release-l11-{os.getpid()}", cfr_project_id="proj-1",
                   cfr_account="sites/site1", cfr_ids=["fip-a"], cfr_build=b, cfr_cr_name=f"l11-{os.getpid()}",
                   cfr_cr_namespace=NS)
        self.check("designate_dns.yml", f"po-{b}", cb_cluster_name=b, cb_api_vip="192.0.2.15", cb_ingress_vip="192.0.2.16",
                   cb_ocp_name="c1", cb_cluster_domain="env1.example.org", use_designate_dns=True,
                   designate_zone_id="zone-1", designate_project_id="dnsproj-1", designate_admin_vault_path="sites/site1")
        # the site account (its CA bundle) and the environment's credential (the installer's) both go
        self.check("preflight_network_name.yml", f"po-netname-{b}", f"po-netname-env-{b}", cb_app_name=b,
                   cb_oso_project_id="proj-1", cb_fip_account="sites/site1", pnn_name="hn1", pnn_id="net-1",
                   pnn_hybrid=True, pnn_credential="oso/projects/env1/clouds-config")


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class RouterGatewayFile(unittest.TestCase):
    def test_gateway_joins_the_prefixes(self):
        fab = fabric(networks=[{"network": f"{NS}/n1", "prefixes": ["192.0.2.15/32", "192.0.2.16/32", "192.0.2.30/32"]}])
        v = dict(addr_vars(), rg_infra_id="c1-x7k2p", cb_api_vip="192.0.2.15", cb_ingress_vip="192.0.2.16")
        routers = [{"name": "c1-x7k2p-external-router",
                    "external_gateway_info": {"network_id": "net-1", "external_fixed_ips": [{"ip_address": "192.0.2.30"}]}}]
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
                    "external_gateway_info": {"network_id": "net-1", "external_fixed_ips": [{"ip_address": "192.0.2.30"}]}}]
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

    def test_installed_or_started_cluster_keeps_its_prefix_on_a_refusal(self):
        # Review R3 N2 (R2 M1 on AWS/GCP): a refused row for a cluster whose install was started or is done reports the
        # refusal and keeps the prefix (and with it its place); before the install the prefix is withdrawn
        fab = fabric(refused=[{"network": f"{NS}/n1", "prefix": "192.0.2.0/25", "reason": "ServicePrefixInUse"}])
        for cloud, kind in (("aws", "CloudAWS"), ("gcp", "CloudGCP")):
            for st in ({"clusterInstalled": True, "platformPrefixes": ["192.0.2.0/25"]}, {"installStarted": True}):
                with self.subTest(cloud=cloud, st=st):
                    rc, tr, out = H.run("platformopenshift_provision", f"{cloud}_clusterbuild_network.yml",
                                        cloud_vars(cloud, "Internal",
                                                   platformopenshift_cr={"resources": [{"status": dict(st)}]}),
                                        k8s=k8s(places=self.placements(kind), fab=fab))
                    self.assertNotEqual(rc, 0)
                    self.assertFalse([p for p in H.patches(tr)
                                      if "platformPrefixes" in p["body"] and p["body"]["platformPrefixes"] is None])
                    self.assertIn("keeps its prefix 192.0.2.0/25", out)
            with self.subTest(cloud=cloud, st="before the install"):
                rc, tr, out = H.run("platformopenshift_provision", f"{cloud}_clusterbuild_network.yml",
                                    cloud_vars(cloud, "Internal"), k8s=k8s(places=self.placements(kind), fab=fab))
                self.assertNotEqual(rc, 0)
                self.assertTrue([p for p in H.patches(tr)
                                 if "platformPrefixes" in p["body"] and p["body"]["platformPrefixes"] is None])

    def test_cloud_install_started_is_recorded_before_the_helm_release(self):
        tasks = load(os.path.join(TASKS, "aws_clusterbuild_deploy.yml"))
        names = [t.get("name") for t in tasks]
        rec = [i for i, t in enumerate(tasks) if "installStarted" in str(t.get("vars", {}).get("cr_status_body", {}))]
        self.assertTrue(rec)
        self.assertLess(rec[0], names.index("Deploy mce-cluster-build chart"))

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

    def test_aws_without_record_owns_no_cluster_path(self):
        # Review R2 H1: no fallback to the CR's name (a CR named like a hub namespace would have lost it)
        cr = {"metadata": {"name": "c1"}, "spec": {"type": "aws"}, "status": {}}
        build, paths = self.paths(cr)
        self.assertEqual((build, paths), ("", []))
        cr["status"]["buildName"] = "shc-aws-acme-c1-0a1b2c3d"
        build, paths = self.paths(cr, rpp_build_name="")   # recorded, but the hub says it is not this cluster's
        self.assertEqual((build, paths), ("", []))


class HubOwner(unittest.TestCase):
    """Review R2 H1: whose is the build's hub namespace (po_preflight.py hub-owner)."""
    B = "shc-oso-acme-c1-0a1b2c3d"

    def owner(self, ns, recorded="", claim=False, uid="uid-c1"):
        return P.hub_owner({"namespace": ns, "uid": uid, "recorded": recorded, "buildName": self.B, "claim": claim})

    def ns(self, uid=None):
        return {"metadata": {"name": self.B, **({"labels": {P.OWNER_LABEL: uid}} if uid else {})}}

    def test_marked_for_this_cluster(self):
        for claim in (True, False):
            self.assertTrue(self.owner(self.ns("uid-c1"), claim=claim)["owned"])

    def test_marked_for_another_cluster(self):
        r = self.owner(self.ns("uid-other"), recorded="", claim=True)
        self.assertEqual((r["owned"], r["reason"]), (False, "NamespaceTaken"))

    def test_a_restored_object_is_told_about_the_mismatch(self):
        # Review R3 N4: the object recorded the build but the namespace carries another uid (a restored or moved object
        # gets a new uid): never adopted, and the refusal names the namespace and both uids
        for claim in (True, False):
            r = self.owner(self.ns("uid-other"), recorded=self.B, claim=claim)
            self.assertEqual((r["owned"], r["adopt"], r["reason"]), (False, False, "BuildOwnerMismatch"))
            for word in (self.B, "uid-other", "uid-c1", P.OWNER_LABEL):
                self.assertIn(word, r["message"])

    def test_unmarked_namespace_only_for_its_recorded_build(self):
        r = self.owner(self.ns(), recorded=self.B, claim=True)
        self.assertEqual((r["owned"], r["adopt"]), (True, True))
        self.assertEqual((self.owner(self.ns(), recorded=self.B)["owned"], self.owner(self.ns(), recorded=self.B)["adopt"]),
                         (True, False))
        for recorded in ("", "shc-oso-acme-c1-ffffffff"):
            self.assertEqual(self.owner(self.ns(), recorded=recorded, claim=True)["reason"], "NamespaceTaken")

    def test_gone_namespace(self):
        self.assertTrue(self.owner({}, recorded=self.B)["owned"])           # a teardown's leftovers of its record
        self.assertFalse(self.owner({}, recorded="")["owned"])               # nothing recorded: nothing owned
        self.assertEqual(self.owner({}, claim=True)["reason"], "NotClaimed")  # a build creates it first

    def test_no_build_name_owns_nothing(self):
        r = P.hub_owner({"namespace": self.ns("uid-c1"), "uid": "uid-c1", "recorded": "", "buildName": "", "claim": False})
        self.assertFalse(r["owned"])


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class ClaimBuild(unittest.TestCase):
    """Review R2 H1: the build's namespace is claimed (create-only, marked with the object's uid) right after the
    record and before any floating IP; two clusters cannot share a release or a pair of floating IPs."""
    B = "shc-oso-acme-c1"

    def test_first_claim_creates_the_marked_namespace(self):
        rc, tr, out = H.run("platformopenshift_provision", "openstack_cluster_addresses.yml", addr_vars(),
                            k8s=k8s(fab=CARRIED), result="1")
        self.assertEqual(rc, 0, out)
        c = [t for t in tr if "create" in t][0]
        self.assertEqual(c["body"]["metadata"]["labels"]["hybridsovereign.redhat/platformopenshift-uid"], "uid-c1")
        self.assertEqual(c["body"]["kind"], "Namespace")

    def test_race_the_second_cluster_is_refused_before_its_floating_ips(self):
        # both passed the preflight (no namespace yet); the other cluster's create won (409 here)
        other = {f"Namespace//{self.B}": {"metadata": {"name": self.B,
                                                        "labels": {"hybridsovereign.redhat/platformopenshift-uid": "uid-other"}}}}
        rc, tr, out = H.run("platformopenshift_provision", "openstack_cluster_addresses.yml",
                            addr_vars(fake_create={f"/api/v1/namespaces/{self.B}": 409}),
                            k8s=k8s(fab=CARRIED, extra=other), result="1")
        self.assertNotEqual(rc, 0)
        self.assertIn(f"the build name {self.B} is already in use", out)
        self.assertFalse(H.os_calls(tr), "no floating IP was looked up or created")
        self.assertFalse([t for t in tr if "k8s" in t], "nothing was written on the hub")
        self.assertIn({"buildName": None}, [p["body"] for p in H.patches(tr)])

    def test_a_restored_object_keeps_its_record_and_is_refused_with_the_mismatch(self):
        # Review R3 N4: the record stays (it is the object's own), nothing is built, the condition names the mismatch
        marked = {f"Namespace//{self.B}": {"metadata": {"name": self.B, "labels": {
            "hybridsovereign.redhat/platformopenshift-uid": "uid-old"}}}}
        cur = {"resources": [{"status": {"buildName": self.B}}]}
        rc, tr, out = H.run("platformopenshift_provision", "openstack_cluster_addresses.yml",
                            addr_vars(fake_create={f"/api/v1/namespaces/{self.B}": 409}, po_cr_current=cur),
                            k8s=k8s(fab=CARRIED, extra=marked), result="1")
        self.assertNotEqual(rc, 0)
        self.assertFalse(H.os_calls(tr))
        self.assertFalse([t for t in tr if "k8s" in t])
        self.assertNotIn({"buildName": None}, [p["body"] for p in H.patches(tr)])
        self.assertIn(f"namespace {self.B} on the hub carries hybridsovereign.redhat/platformopenshift-uid=uid-old, but "
                      "this PlatformOpenshift has uid uid-c1", out)
        stop = [t for t in walk(load(os.path.join(TASKS, "claim_build.yml")))
                if t.get("ansible.builtin.include_tasks") == "preflight_stop.yml"]
        self.assertEqual(stop[0]["vars"]["ps_reason"], "BuildOwnerMismatch")

    def test_a_namespace_this_cluster_recorded_before_the_label_is_adopted(self):
        legacy = {f"Namespace//{self.B}": {"metadata": {"name": self.B}}}
        cur = {"resources": [{"status": {"buildName": self.B}}]}
        rc, tr, out = H.run("platformopenshift_provision", "openstack_cluster_addresses.yml",
                            addr_vars(fake_create={f"/api/v1/namespaces/{self.B}": 409}, po_cr_current=cur),
                            k8s=k8s(fab=CARRIED, extra=legacy), result="1")
        self.assertEqual(rc, 0, out)
        self.assertEqual([t["k8s"] for t in tr if "k8s" in t],
                         [{"state": "patched", "kind": "Namespace", "name": self.B, "namespace": None}])
        # but not one it did not record
        rc, tr, out = H.run("platformopenshift_provision", "openstack_cluster_addresses.yml",
                            addr_vars(fake_create={f"/api/v1/namespaces/{self.B}": 409}),
                            k8s=k8s(fab=CARRIED, extra=legacy), result="1")
        self.assertNotEqual(rc, 0)
        self.assertFalse(H.os_calls(tr))

    def test_the_stale_build_cleanup_runs_only_after_the_claim(self):
        # Review R2, unconfirmed item 4: cleanup_stale_clusterbuild.yml deletes the ManagedCluster and the
        # ClusterDeployment of the build name; it runs after openstack_cluster_addresses.yml (whose claim refuses a
        # build name another object holds), so it only ever cleans this cluster's own failed build
        names = [t.get("name") for t in load(os.path.join(TASKS, "openstack_clusterbuild.yml"))]
        self.assertLess(names.index("The cluster's recorded build and addresses"),
                        names.index("Clean up failed or stale Hive cluster build before reprovisioning"))
        addr = [t.get("name") for t in load(os.path.join(TASKS, "openstack_cluster_addresses.yml"))]
        self.assertLess(addr.index("Claim the build on the hub"), addr.index("The cluster's floating IPs"))

    def test_aws_and_gcp_claim_right_after_their_record(self):
        for f in ("aws_clusterbuild_sts.yml", "gcp_clusterbuild_wif.yml"):
            names = [t.get("name") or "" for t in load(os.path.join(TASKS, f))]
            rec = [i for i, n in enumerate(names) if n.startswith("Record the build on the status")][0]
            self.assertEqual(names[rec + 1], "Claim the build on the hub", f)


TD_FAKES = {
    "teardown_unrealized.yml": "[]\n",
    "resolve_services_cluster_connection.yml": "[]\n",
    "resolve_central_cluster_sa_from_vault.yml": "[]\n",
    "vault_kv_delete.yml": H.TRACE % "{'vault_delete': vault_kv_delete_paths}",
    "resolve_cloudoso_accounts.yml": "- ansible.builtin.set_fact:\n    rca_account_vault_path: sites/site1\n",
}


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class TeardownOwnsOnlyItsBuild(unittest.TestCase):
    """Review R2 H1: the whole teardown (platformopenshift_teardown main.yml) with fakes: a cluster that never recorded
    a build, or whose build name is marked for another object on the hub, deletes nothing on the hub."""
    HUB_KINDS = {"ManagedCluster", "Namespace", "ClusterDeployment", "Policy", "PlacementBinding", "Placement", "ConfigMap"}

    def teardown(self, status, extra=None):
        po = {"apiVersion": "hybridsovereign.redhat/v1alpha1", "kind": "PlatformOpenshift",
              "metadata": {"name": "corp-prod", "namespace": "acme", "uid": "uid-y"},
              "spec": {"type": "openstack", "openstack": {"environment": "env1"}}, "status": status}
        kube = {"PlatformOpenshift/acme/corp-prod": po,
                "Namespace//acme": {"metadata": {"name": "acme", "labels": {"hybridsovereign.redhat/entity": "acme"}}},
                "CloudOSO/acme/env1": {"status": {"projectId": "proj-y"}}}
        kube.update(extra or {})
        v = dict(BASE, event_payload={"regarding": {"name": "corp-prod", "namespace": "acme"}},
                 shc_live_spec={"type": "openstack"}, clusterbuild_uninstall_timeout="1m")
        return H.run("platformopenshift_teardown", "main.yml", v, k8s=kube, fakes=TD_FAKES)

    def assert_nothing_on_the_hub(self, tr, out):
        hub = [t["k8s"] for t in tr if "k8s" in t and t["k8s"]["kind"] in self.HUB_KINDS]
        self.assertEqual(hub, [], out)
        deleted = [p for t in tr if "vault_delete" in t for p in t["vault_delete"]]
        self.assertFalse([p for p in deleted if p.startswith("cluster/")], deleted)
        rel = H.os_calls(tr, "cluster-fips")
        self.assertTrue(all(r["descriptions"] == {} for r in rel), rel)
        self.assertTrue([p for p in H.patches(tr) if p["body"].get("deletionComplete")], out)

    def test_never_recorded_deletes_nothing_on_the_hub(self):
        # tenant acme's corp-prod stopped before its record; tenant acme-corp's prod is running under the name the old
        # scheme derived for both
        taken = {"Namespace//shc-oso-acme-corp-prod": {"metadata": {"name": "shc-oso-acme-corp-prod"}}}
        rc, tr, out = self.teardown({"reason": "WaitingForPlacement"}, taken)
        self.assertEqual(rc, 0, out)
        self.assert_nothing_on_the_hub(tr, out)

    def test_recorded_but_marked_for_another_object(self):
        b = "shc-oso-acme-corp-prod-0a1b2c3d"
        taken = {f"Namespace//{b}": {"metadata": {"name": b, "labels": {
            "hybridsovereign.redhat/platformopenshift-uid": "uid-x"}}}}
        rc, tr, out = self.teardown({"buildName": b, "floatingIPs": {"api": {"id": "fip-y1"}}}, taken)
        self.assertEqual(rc, 0, out)
        self.assert_nothing_on_the_hub(tr, out)
        self.assertEqual(H.os_calls(tr, "cluster-fips")[0]["ids"], ["fip-y1"])   # its own recorded ids still go

    def events(self, tr):
        return [t["definition"] for t in tr if "k8s" in t and t["k8s"]["kind"] == "Event"]

    def test_the_teardown_says_what_it_left_on_the_hub(self):
        # Review R3 N4 and unconfirmed 1: a teardown that removes nothing on the hub says so in an event on the cluster
        # (and in its log), naming the namespace and the mismatch, or that nothing was recorded
        b = "shc-oso-acme-corp-prod-0a1b2c3d"
        taken = {f"Namespace//{b}": {"metadata": {"name": b, "labels": {
            "hybridsovereign.redhat/platformopenshift-uid": "uid-x"}}}}
        rc, tr, out = self.teardown({"buildName": b}, taken)
        self.assertEqual(rc, 0, out)
        ev = self.events(tr)
        self.assertEqual([e["reason"] for e in ev], ["BuildOwnerMismatch"])
        self.assertEqual((ev[0]["type"], ev[0]["regarding"]["name"], ev[0]["regarding"]["uid"]),
                         ("Warning", "corp-prod", "uid-y"))
        for word in (b, "uid-x", "uid-y", "Nothing"):
            self.assertIn(word, ev[0]["note"])
        rc, tr, out = self.teardown({"reason": "WaitingForPlacement"})
        self.assertEqual(rc, 0, out)
        ev = self.events(tr)
        self.assertEqual([e["reason"] for e in ev], ["NoBuildRecorded"])
        self.assertIn("status.buildName", ev[0]["note"])
        # an owned build: no such event
        mine = {f"Namespace//{b}": {"metadata": {"name": b, "labels": {
            "hybridsovereign.redhat/platformopenshift-uid": "uid-y"}}}}
        rc, tr, out = self.teardown({"buildName": b}, mine)
        self.assertEqual(self.events(tr), [])

    def test_ownership_file(self):
        b = "shc-oso-acme-c1-0a1b2c3d"
        for labels, recorded, want in (({"hybridsovereign.redhat/platformopenshift-uid": "uid-c1"}, b, [True, b]),
                                       (None, b, [True, b]), ("gone", b, [True, b]),
                                       ({"hybridsovereign.redhat/platformopenshift-uid": "uid-x"}, b, [False, ""]),
                                       ({"hybridsovereign.redhat/platformopenshift-uid": "uid-c1"}, "", [False, ""])):
            kube = {} if labels == "gone" else {f"Namespace//{b}": {"metadata": dict({"name": b}, **({"labels": labels} if labels else {}))}}
            cr = {"resources": [{"metadata": {"uid": "uid-c1"}, "status": {"buildName": recorded}}]}
            rc, tr, out = H.run("platformopenshift_teardown", "hub_ownership.yml", dict(BASE, po_cr=cr), k8s=kube,
                                result="[po_hub_owned | bool, po_hub_build]")
            self.assertEqual(rc, 0, out)
            self.assertEqual(tr[-1]["result"], want, (labels, recorded))

    def test_every_hub_step_is_gated(self):
        main = load(os.path.join(TD, "tasks", "main.yml"))
        for t in walk(main):
            text = str({k: v for k, v in t.items() if k not in ("name", "block", "rescue", "always")})
            hub = ("delete_acm.yml" in text or "delete_clusterbuild.yml" in text or "kind': 'Namespace'" in text
                   or "oidc-static-" in text or "sts-down" in text or "wif-down" in text
                   or "kind': 'ManagedCluster'" in text)
            if hub and "po_cr_current" not in text:
                self.assertIn("po_hub_owned", str(t.get("when", "")), t.get("name"))

@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class RouterAdvertisementPreflight(unittest.TestCase):
    """The cluster preflight asks the site's router advertisement service (where the realization names one for the
    environment's site) to hold the floating IPs' advertisement on the placed network, last before the build: no
    service = nothing called and the build goes on; found and set = goes on; a failing or unconfirming service =
    refused with RouterAdvertisementFailed and the service's message, before anything is created."""

    REAL = {"placement": {"routerAdvertisementBySite": {"site1": {"url": "https://ra.site1.example.org"}}},
            "openstack": {"platformProject": "platform-project", "projectDomain": "platform-domain"}}

    def run_ra(self, real, answer=None):
        place = placement(site={"networkId": "net-1", "networkName": "shc-n1", "routerId": "router-1"})
        v = os_preflight_vars(fabric_realization=real, cb_fip_account="sites/site1",
                              fake_os=dict(NAME_OK, **{"router-advertisement/present": answer or {}}))
        # the role's rescue records po_fail_reason (main.yml); here a rescue traces it
        trace = {"name": "trace the refusal", "ansible.builtin.shell": "cat >> {{ trace_file }}",
                 "args": {"stdin": "{{ {'refused': po_fail_reason} | to_json }}"}, "changed_when": False}
        tasks = [{"block": [{"ansible.builtin.include_tasks": os.path.join(TASKS, "openstack_preflight.yml")}],
                  "rescue": [trace, {"ansible.builtin.fail": {"msg": "{{ ansible_failed_result.msg | default('') }}"}}]}]
        return H.run("platformopenshift_provision", "openstack_preflight.yml", v, tasks=tasks,
                     k8s=k8s(places=[place], extra={f"CloudInfrastructure/{PLAT}/site1": SITE}), result="1")

    def test_no_service_calls_nothing(self):
        rc, tr, out = self.run_ra({})
        self.assertEqual(rc, 0, out)
        self.assertEqual(tr[-1], {"result": 1})
        self.assertFalse(H.os_calls(tr, "router-advertisement"))

    def test_found_and_set(self):
        rc, tr, out = self.run_ra(self.REAL, {"service": "found", "port_id": "port-1", "nat": True, "wanted": True,
                                              "changed": True, "restores": 0, "lastRestoredAt": "", "reason": "",
                                              "message": ""})
        self.assertEqual(rc, 0, out)
        self.assertEqual(tr[-1], {"result": 1})
        req = H.os_calls(tr, "router-advertisement")[0]
        self.assertEqual((req["state"], req["router_id"], req["network_id"], req["site"], req["service_url"],
                          req["platformProject"], req["domain"]),
                         ("present", "router-1", "net-1", "site1", "https://ra.site1.example.org", "platform-project",
                          "platform-domain"))
        self.assertFalse(H.patches(tr))   # nothing recorded or created by the preflight

    def test_failing_or_unconfirmed(self):
        for answer, want in (({"service": "found", "nat": False, "reason": "NotYourPort",
                               "message": "the port is not in the token's project"}, "NotYourPort"),
                             ({"service": "found", "nat": False, "reason": "", "message": ""}, "NotConfirmed")):
            with self.subTest(want=want):
                rc, tr, out = self.run_ra(self.REAL, answer)
                self.assertNotEqual(rc, 0)
                self.assertIn({"refused": "RouterAdvertisementFailed"}, tr)
                self.assertIn(want, out)
                self.assertNotIn({"result": 1}, tr)
                self.assertFalse([t for t in tr if "os" in t and t["os"]["action"] == "cluster-fips"])


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class PlatformSideDownWaits(unittest.TestCase):
    """The preflight of a cluster whose prefixes the network keeps while the platform's side is down (rows pending
    with the platform's reason, PlatformReachable False CircuitNotDeployed): the job ends Waiting, nothing is
    released or created; a cluster the network holds nothing for is refused."""

    def net(self, cluster):
        n = network_obj(reach=("False", "CircuitNotDeployed", "no platform circuit"))
        n["status"]["platformAccess"] = {"prefixes": [{"prefix": "192.0.2.15/32", "cluster": cluster, "state": "pending",
                                                       "reason": "CircuitNotDeployed", "message": "keeps its place"}]}
        return n

    def test_holder_waits(self):
        rc, tr, out = H.run("platformopenshift_provision", "openstack_preflight.yml", os_preflight_vars(),
                            k8s=k8s(net=self.net("c1")), result="1")
        self.assertEqual(rc, 0, out)
        self.assertNotIn({"result": 1}, tr)            # the job ended there
        body = H.patches(tr)[-1]["body"]
        self.assertEqual((body["status"], body["reason"]), ("pending", "WaitingForPlatformAccess"))
        # only the read of the network's name ran (a refusal there would win over the wait): nothing released or made
        self.assertEqual([c["action"] for c in H.os_calls(tr)], ["network-name-check"])

    def test_new_cluster_refused(self):
        rc, tr, out = H.run("platformopenshift_provision", "openstack_preflight.yml", os_preflight_vars(),
                            k8s=k8s(net=self.net("another")), result="1")
        self.assertNotEqual(rc, 0)
        self.assertIn("CircuitNotDeployed", out)
        self.assertFalse(H.os_calls(tr))


if __name__ == "__main__":
    unittest.main()
