"""The service VRF's planning in files/shc_fabric.py: the reserved ranges (reserved_ranges, the reserved-ranges action),
the realization's infrastructure and service keys (service_check through check-fabric, with each refusal's message),
the root index pool with the service on, the service plan (service-plan: service links in the upper half of
egress.linkPool, per-network objects only with accepted prefixes, first come first served by the ledger, the
attachable ranges, the refusals), one network's prefixes on their own (service-prefix-check) and a cluster's own
networks (check-cluster-networks). Documentation and private ranges, example names only."""
import copy
import ipaddress
import json
import os
import subprocess
import sys
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "eda", "common", "files"))
import shc_fabric as F  # noqa: E402

with open(os.path.join(ROOT, "eda", "common", "vars", "fabric_realization.yml")) as fh:
    DEFAULTS = yaml.safe_load(fh)["fabric_realization_defaults"]

HUB = "198.51.100.0/24"
NODES = ["bgw-1", "bgw-2"]


def spec(nodes=None):
    return {"domainAsn": 65010, "vniPool": {"start": 50000, "end": 50999}, "l2vniPool": {"start": 60000, "end": 60999},
            "borderGateway": {"nodes": [{"name": n} for n in (NODES if nodes is None else nodes)]}}


def real(**over):
    r = copy.deepcopy(DEFAULTS)
    r["attachments"]["enabled"] = True
    r["infrastructure"] = {"hub": [HUB]}
    r["service"].update({"enabled": True, "attachableRanges": ["192.0.2.0/24", "203.0.113.0/24"],
                         "hubEndpoints": [{"prefix": "198.51.100.10/32", "ports": [6443]}]})
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(r.get(k), dict):
            r[k].update(v)
        else:
            r[k] = v
    return r


def network(name, prefixes, rows, ns="acme", fabric="lab", space=None):
    return {"metadata": {"namespace": ns, "name": name},
            "spec": {"prefixes": list(space if space is not None else prefixes)},
            "status": {"fabric": fabric,
                       "platformAccess": {"prefixes": [dict({"prefix": p, "cluster": "c1", "state": "pending"}, **x)
                                                       for p, x in rows]}}}


def circuit(vlan=3099, nodes=("bgw-1", "bgw-2")):
    return {"circuit": "platform/hub-a", "kind": "platform", "id": f"AC{vlan}", "vrf": "service", "table": 9001,
            "vlan": vlan, "device": f"bond1.{vlan}", "parent": "bond1", "mtu": 1500, "link": "10.255.128.8/29",
            "ce": "10.255.128.11", "peerAsn": 64600, "authentication": "none", "passwordPath": "", "maxPrefixes": 50,
            "maxPrefixAction": "teardown", "bfd": False, "rev": "r1",
            "nodes": {n: f"10.255.128.{9 + i}" for i, n in enumerate(nodes)}}


def ledger(entries, targets, index):
    return {"vpnTarget": {f"HybridNetwork/{k}": v for k, v in targets.items()},
            "egress": {f"HybridNetwork/{k}": v for k, v in index.items()},
            "servicePrefix": {f"HybridNetwork/{k}/{p}": v for (k, p), v in entries.items()}}


def plan(r, networks, alloc, circuits=None, previous=None, vrfs=None):
    return F.service_plan({"realization": r, "nodes": NODES, "fabric": "lab", "domainAsn": 65010,
                           "networks": networks, "ledger": alloc,
                           "circuits": [circuit()] if circuits is None else circuits,
                           "vrfs": vrfs or {n: [] for n in NODES}, "previous": previous or {}})


class ReservedRanges(unittest.TestCase):
    def test_rows_purposes_and_sites(self):
        r = real(network={"exclusiveRanges": ["2001:db8:4000::/40"]})
        r["ipam"]["publicPools"] = {"v6": {"family": 6, "prefixes": ["2001:db8:4000::/44"]}}
        r["core"].update(srv6={"block": "5f00:1::/32"}, icmpSource6="2001:db8:ffff::/96")
        rows = F.reserved_ranges(r, [{"name": "site-a", "kind": "openshift", "ranges": ["192.0.2.192/26"]},
                                     {"name": "site-b", "kind": "CloudOSO", "ranges": []}])
        self.assertEqual(rows[0], {"prefix": HUB, "purpose": "Networks the platform's management connects from",
                                   "rule": "Refused"})
        by = {(x["prefix"], x.get("site", "")): x for x in rows}
        self.assertEqual(by[("2001:db8:4000::/40", "")]["purpose"], "Address space the platform keeps for itself")
        self.assertEqual(by[("2001:db8:4000::/44", "")]["purpose"], "The platform's assignable IPv6 space")
        self.assertEqual(by[("5f00:1::/32", "")]["purpose"], "The platform's transport addresses")
        self.assertEqual(by[("192.0.0.8/32", "")], {"prefix": "192.0.0.8/32", "rule": "Refused",
                                                     "purpose": "Addresses of the platform's network nodes"})
        self.assertEqual(by[("2001:db8:ffff::/96", "")]["rule"], "Refused")
        for p in ("10.255.0.0/24", "10.255.1.0/24", "10.255.128.0/20", "169.254.0.0/16"):
            self.assertEqual(by[(p, "")], {"prefix": p, "rule": "SteppedAround",
                                           "purpose": "Addresses inside the platform's network nodes"})
        # a site's underlay and the siteReserved ranges of its kind (an OpenShift site: CloudVirt's)
        self.assertEqual(by[("192.0.2.192/26", "site-a")], {"prefix": "192.0.2.192/26", "rule": "SteppedAround",
                                                             "site": "site-a",
                                                             "purpose": "Addresses the site uses itself"})
        self.assertIn(("100.64.0.0/16", "site-a"), by)
        self.assertFalse([k for k in by if k[1] == "site-b"])  # CloudOSO reserves nothing by default
        # no realization key names in the published rows
        self.assertEqual({k for x in rows for k in x}, {"prefix", "purpose", "rule", "site"})

    def test_defaults(self):
        rows = F.reserved_ranges(copy.deepcopy(DEFAULTS))
        self.assertEqual([x["prefix"] for x in rows],
                         ["192.0.0.8/32", "10.255.0.0/24", "10.255.1.0/24", "10.255.128.0/20", "169.254.0.0/16"])

    def test_action(self):
        p = subprocess.run([sys.executable, os.path.join(ROOT, "eda/common/files/shc_fabric.py")],
                           input=json.dumps({"action": "reserved-ranges", "realization": real(), "sites": []}),
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["ranges"][0]["prefix"], HUB)


class Realization(unittest.TestCase):
    def errs(self, r):
        return F.check_fabric({"spec": spec(), "realization": r})["errors"]

    def test_defaults_are_inert(self):
        r = F.check_fabric({"spec": spec(), "realization": copy.deepcopy(DEFAULTS)})
        self.assertEqual(r["errors"], [])
        self.assertEqual(r["poolSize"]["egressPool"], 0)
        self.assertEqual(DEFAULTS["service"], {"enabled": False, "vrf": {"name": "service", "table": 9001},
                                               "ports": [6443, 443], "hubEndpoints": [], "attachableRanges": []})
        self.assertEqual(DEFAULTS["infrastructure"], {"hub": []})
        self.assertNotIn("platformSources", DEFAULTS["egress"])
        self.assertNotIn("platformPorts", DEFAULTS["egress"])

    def test_valid_service(self):
        r = F.check_fabric({"spec": spec(), "realization": real()})
        self.assertEqual(r["errors"], [])
        # egress off: the root indexes are the /31s of the link pool's upper half
        self.assertEqual(r["poolSize"]["egressPool"], 65536 // 4)

    def test_pool_with_egress(self):
        e = {"enabled": True, "parentInterface": "bond1.100", "gateway": "203.0.113.1", "linkPool": "169.254.0.0/24",
             "nodes": {"bgw-1": {"address": "203.0.113.11", "natPool": "203.0.113.128/26"},
                       "bgw-2": {"address": "203.0.113.12", "natPool": "203.0.113.192/26"}}}
        r = copy.deepcopy(DEFAULTS)
        r["egress"].update(e)
        self.assertEqual(F.check_fabric({"spec": spec(), "realization": r})["poolSize"]["egressPool"], 64)
        r = real(egress=e)
        self.assertEqual(F.check_fabric({"spec": spec(), "realization": r})["poolSize"]["egressPool"], 64)
        r["egress"]["linkPool"] = "169.254.0.0/27"
        # the upper half holds the service links: a quarter of the pool
        self.assertEqual(F.check_fabric({"spec": spec(), "realization": r})["poolSize"]["egressPool"], 8)

    def test_refusals_name_the_setting(self):
        cases = [
            (real(attachments={"enabled": False}), "realization service.enabled needs attachments.enabled"),
            (real(infrastructure={"hub": []}), "realization service.enabled needs an IPv4 range in infrastructure.hub"),
            (real(infrastructure={"hub": ["2001:db8:5::/48"]}), "needs an IPv4 range in infrastructure.hub"),
            (real(infrastructure={"hub": ["198.51.100.1/24"]}), "realization infrastructure.hub: 198.51.100.1/24"),
            (real(service={"vrf": {"name": "egress", "table": 9001}}), "realization service.vrf.name 'egress'"),
            (real(service={"vrf": {"name": "e10005", "table": 9001}}), "realization service.vrf.name 'e10005'"),
            (real(service={"vrf": {"name": "", "table": 9001}}), "realization service.vrf.name is required"),
            (real(service={"vrf": {"name": "a-much-too-long-name", "table": 9001}}), "at most 15 characters"),
            (real(service={"vrf": {"name": "service", "table": 254}}), "realization service.vrf.table 254"),
            (real(service={"vrf": {"name": "service", "table": 9000}}), "realization service.vrf.table 9000"),
            (real(service={"vrf": {"name": "service", "table": 100000}}), "below egress.networkTableBase 100000"),
            (real(service={"ports": []}), "realization service.ports must hold TCP ports"),
            (real(service={"hubEndpoints": [{"prefix": "192.0.2.10/32", "ports": [6443]}]}),
             "realization service.hubEndpoints[0].prefix 192.0.2.10/32 is not inside infrastructure.hub"),
            (real(service={"hubEndpoints": [{"prefix": "198.51.100.10/32", "ports": []}]}),
             "realization service.hubEndpoints[0].ports must hold TCP ports"),
            (real(service={"attachableRanges": ["2001:db8::/32"]}),
             "realization service.attachableRanges[0] '2001:db8::/32' is not an IPv4 network"),
            (real(service={"attachableRanges": ["198.51.0.0/16"]}),
             "realization service.attachableRanges[0] 198.51.0.0/16 overlaps 198.51.100.0/24 (networks the platform's "
             "management connects from)"),
            (real(service={"attachableRanges": ["10.255.0.0/25"]}),
             "realization service.attachableRanges[0] 10.255.0.0/25 overlaps 10.255.0.0/24"),
        ]
        for r, want in cases:
            with self.subTest(want=want):
                self.assertTrue(any(want in e for e in self.errs(r)), self.errs(r))
        # an attachable range holding a stepped-around range is fine (the address management steps around it)
        self.assertEqual(self.errs(real(service={"attachableRanges": ["10.0.0.0/8"]})), [])

    def test_pools_never_overlap_the_hub(self):
        r = real(infrastructure={"hub": ["10.255.0.0/16"]})
        e = self.errs(r)
        self.assertIn("realization core.loopbackPool 10.255.0.0/24 overlaps infrastructure.hub 10.255.0.0/16", e)
        self.assertIn("realization attachments.linkPool 10.255.128.0/20 overlaps infrastructure.hub 10.255.0.0/16", e)
        # also with the service off: the hub is exclusive whenever it is declared
        r = copy.deepcopy(DEFAULTS)
        r["infrastructure"] = {"hub": ["192.0.0.0/24"]}
        self.assertIn("realization core.icmpSource4 192.0.0.8/32 overlaps infrastructure.hub 192.0.0.0/24", self.errs(r))

    def test_retired_platform_reach_keys(self):
        for k, v in (("platformSources", []), ("platformPorts", [6443, 443])):
            r = copy.deepcopy(DEFAULTS)
            r["egress"][k] = v
            self.assertIn(f"realization egress.{k} is replaced by infrastructure.hub and service.ports: remove it",
                          self.errs(r))


class Plan(unittest.TestCase):
    def test_off(self):
        r = copy.deepcopy(DEFAULTS)
        vrfs = {"bgw-1": [{"vrf": "e10000", "vpnTarget": 10000}], "bgw-2": []}
        out = F.service_plan({"realization": r, "nodes": NODES, "networks": [network("n1", ["192.0.2.0/24"],
                                                                                      [("192.0.2.15/32", {})])],
                              "vrfs": vrfs})
        self.assertEqual(out["nodes"], {})
        self.assertEqual(out["vrfs"], vrfs)
        self.assertEqual(out["status"], {"enabled": False, "vrf": "", "sources": [], "ports": [], "hubEndpoints": [],
                                         "attachableRanges": [], "circuits": [], "networks": [], "refused": []})

    def test_links_in_the_upper_half_and_objects_only_with_prefixes(self):
        nets = [network("n1", ["192.0.2.0/24"], [("192.0.2.15/32", {}), ("192.0.2.16/32", {})]),
                network("n2", ["203.0.113.0/24"], []),
                network("n3", ["203.0.113.0/25"], [("203.0.113.5/32", {"state": "refused"})])]
        alloc = ledger({("acme/n1", "192.0.2.15/32"): 1, ("acme/n1", "192.0.2.16/32"): 2,
                        ("acme/n3", "203.0.113.5/32"): 3},
                       {"acme/n1": 10005, "acme/n2": 10006, "acme/n3": 10007}, {"acme/n1": 4, "acme/n2": 5, "acme/n3": 6})
        out = plan(real(), nets, alloc)
        self.assertEqual(out["errors"], [])
        n = out["nodes"]["bgw-1"]
        self.assertEqual([w["network"] for w in n["networks"]], ["acme/n1"])
        w = n["networks"][0]
        # root index 4: the 5th /31 of 169.254.128.0/17 (the upper half of 169.254.0.0/16)
        self.assertEqual((w["soAddr"], w["seAddr"], w["vrf"], w["table"], w["se"], w["so"]),
                         ("169.254.128.8/31", "169.254.128.9/31", "e10005", 110005, "se10005", "so10005"))
        self.assertEqual(w["prefixes"], ["192.0.2.15/32", "192.0.2.16/32"])
        self.assertEqual(n["hub"], [HUB])
        self.assertEqual(n["accept"], ["198.51.100.0/24 le 32"])
        self.assertEqual(n["deny"], ["0.0.0.0/0", "10.255.128.0/20 le 32", "10.255.0.0/24 le 32", "10.255.1.0/24 le 32",
                                     "169.254.0.0/16 le 32", "192.0.2.15/32", "192.0.2.16/32"])
        self.assertEqual(n["advertise"], ["192.0.2.15/32", "192.0.2.16/32"])
        self.assertEqual(n["attachable"], ["192.0.2.0/24 le 32", "203.0.113.0/24 le 32"])
        self.assertEqual(n["circuits"][0]["address"], "10.255.128.9/29")
        self.assertEqual(out["nodes"]["bgw-2"]["circuits"][0]["address"], "10.255.128.10/29")
        st = out["status"]
        self.assertEqual(st["networks"], [{"network": "acme/n1", "vpnTarget": 10005, "index": 4,
                                           "prefixes": ["192.0.2.15/32", "192.0.2.16/32"]}])
        self.assertEqual(st["refused"], [])
        self.assertEqual((st["enabled"], st["vrf"], st["sources"], st["ports"]), (True, "service", [HUB], [443, 6443]))
        self.assertEqual(st["hubEndpoints"], [{"prefix": "198.51.100.10/32", "ports": [6443]}])
        self.assertEqual(st["circuits"][0]["circuit"], "platform/hub-a")
        self.assertEqual(st["circuits"][0]["nodes"], NODES)
        # the network's root VRF on each node carries the link; a network without egress or circuits gets one
        v = out["vrfs"]["bgw-1"]
        self.assertEqual([x["vrf"] for x in v], ["e10005"])
        self.assertEqual(v[0]["service"]["soIp"], "169.254.128.8")
        self.assertFalse(v[0]["egress"])

    def test_existing_vrf_gains_the_link(self):
        nets = [network("n1", ["192.0.2.0/24"], [("192.0.2.15/32", {})])]
        alloc = ledger({("acme/n1", "192.0.2.15/32"): 1}, {"acme/n1": 10005}, {"acme/n1": 0})
        vrfs = {n: [{"network": "acme/n1", "vpnTarget": 10005, "vrf": "e10005", "table": 110005, "egress": True,
                     "nat": "203.0.113.64", "circuits": []}] for n in NODES}
        out = plan(real(), nets, alloc, vrfs=vrfs)
        self.assertEqual(len(out["vrfs"]["bgw-1"]), 1)
        self.assertTrue(out["vrfs"]["bgw-1"][0]["egress"])
        self.assertEqual(out["vrfs"]["bgw-1"][0]["service"]["seIp"], "169.254.128.1")

    def test_first_come_first_served(self):
        nets = [network("a", ["192.0.2.0/24"], [("192.0.2.15/32", {})], ns="tenant-a"),
                network("b", ["192.0.2.0/24"], [("192.0.2.15/32", {}), ("192.0.2.20/32", {})], ns="tenant-b")]
        targets, index = {"tenant-a/a": 10001, "tenant-b/b": 10002}, {"tenant-a/a": 1, "tenant-b/b": 2}
        # b's entry came first (lower ledger value): a's overlapping prefix is refused, never b's
        alloc = ledger({("tenant-a/a", "192.0.2.15/32"): 7, ("tenant-b/b", "192.0.2.15/32"): 3,
                        ("tenant-b/b", "192.0.2.20/32"): 8}, targets, index)
        out = plan(real(), nets, alloc)
        self.assertEqual([(w["network"], w["prefixes"]) for w in out["status"]["networks"]],
                         [("tenant-b/b", ["192.0.2.15/32", "192.0.2.20/32"])])
        self.assertEqual(out["status"]["refused"], [
            {"network": "tenant-a/a", "prefix": "192.0.2.15/32", "reason": "ServicePrefixInUse",
             "message": "192.0.2.15/32 is already in use by another network on the platform's service routing domain"}])
        # the message never names the other tenant's network
        self.assertNotIn("tenant-b", json.dumps(out["status"]["refused"]))
        # a prefix realized before keeps its place against a later (or reused lower) ledger value
        prev = {"networks": [{"network": "tenant-a/a", "prefixes": ["192.0.2.15/32"]}]}
        out = plan(real(), nets, alloc, previous=prev)
        self.assertEqual([r["network"] for r in out["status"]["refused"]], ["tenant-b/b"])
        self.assertEqual(sorted(w["network"] for w in out["status"]["networks"]), ["tenant-a/a", "tenant-b/b"])

    def test_refusals_per_prefix(self):
        nets = [network("n1", ["192.0.2.0/24", "198.51.100.0/24", "100.64.10.0/24"],
                        [("192.0.2.15/32", {}), ("198.51.100.20/32", {}), ("100.64.10.5/32", {}), ("10.1.0.5/32", {}),
                         ("2001:db8::1/128", {})])]
        alloc = ledger({("acme/n1", "192.0.2.15/32"): 1}, {"acme/n1": 10005}, {"acme/n1": 0})
        out = plan(real(), nets, alloc)
        by = {r["prefix"]: r for r in out["status"]["refused"]}
        self.assertEqual(by["198.51.100.20/32"]["reason"], "PrefixReserved")
        self.assertEqual(by["198.51.100.20/32"]["message"], "prefix 198.51.100.20/32 overlaps 198.51.100.0/24, reserved by "
                                                            "the platform: networks the platform's management connects from")
        self.assertEqual(by["100.64.10.5/32"]["reason"], "OutsideAttachableRanges")
        self.assertEqual(by["10.1.0.5/32"]["reason"], "OutsideAddressSpace")
        self.assertEqual(by["2001:db8::1/128"]["reason"], "FamilyUnavailable")
        self.assertEqual(out["status"]["networks"][0]["prefixes"], ["192.0.2.15/32"])
        # without attachable ranges any prefix that passes the other checks
        r = real(service={"attachableRanges": []})
        alloc["servicePrefix"]["HybridNetwork/acme/n1/100.64.10.5/32"] = 2
        out = plan(r, nets, alloc)
        self.assertEqual(out["status"]["networks"][0]["prefixes"], ["100.64.10.5/32", "192.0.2.15/32"])
        self.assertEqual(out["nodes"]["bgw-1"]["attachable"], ["0.0.0.0/0 le 32"])
        self.assertTrue(set(out["status"]["refused"][0].keys()) >= {"network", "prefix", "reason"})
        self.assertTrue(all(r["reason"] in F.SERVICE_PREFIX_REASONS for r in out["status"]["refused"]))

    def test_pending(self):
        nets = [network("n1", ["192.0.2.0/24"], [("192.0.2.15/32", {})]),
                network("n2", ["203.0.113.0/24"], [("203.0.113.15/32", {})])]
        # n1's prefix is not in the ledger yet; n2 holds no root index yet: neither realized nor refused
        alloc = ledger({}, {"acme/n1": 10005, "acme/n2": 10006}, {"acme/n1": 0})
        out = plan(real(), nets, alloc)
        self.assertEqual(out["status"]["networks"], [])
        self.assertEqual(out["status"]["refused"], [])
        self.assertEqual(sorted(p["network"] for p in out["pending"]), ["acme/n1", "acme/n2"])
        self.assertEqual(out["nodes"]["bgw-1"]["networks"], [])
        self.assertEqual(out["vrfs"]["bgw-1"], [])

    def test_other_fabric_and_no_circuit(self):
        nets = [network("n1", ["192.0.2.0/24"], [("192.0.2.15/32", {})], fabric="other")]
        alloc = ledger({("acme/n1", "192.0.2.15/32"): 1}, {"acme/n1": 10005}, {"acme/n1": 0})
        self.assertEqual(plan(real(), nets, alloc)["status"]["networks"], [])
        nets = [network("n1", ["192.0.2.0/24"], [("192.0.2.15/32", {})])]
        out = plan(real(), nets, alloc, circuits=[])
        # no platform circuit: no service VRF on any node, nothing realized
        self.assertEqual(out["nodes"], {})
        self.assertEqual(out["status"]["networks"], [])
        self.assertEqual(out["vrfs"], {n: [] for n in NODES})
        out = plan(real(), nets, alloc, circuits=[circuit(nodes=("bgw-1",))])
        self.assertEqual(list(out["nodes"]), ["bgw-1"])
        self.assertEqual([v["vrf"] for v in out["vrfs"]["bgw-1"]], ["e10005"])
        self.assertEqual(out["vrfs"]["bgw-2"], [])

    def test_index_outside_the_service_links(self):
        r = real(egress={"linkPool": "169.254.0.0/28"})
        nets = [network("n1", ["192.0.2.0/24"], [("192.0.2.15/32", {})])]
        alloc = ledger({("acme/n1", "192.0.2.15/32"): 1}, {"acme/n1": 10005}, {"acme/n1": 4})
        out = plan(r, nets, alloc)
        self.assertEqual(out["status"]["networks"], [])
        self.assertIn("outside the service links (0-3)", out["pending"][0]["why"])

    def test_invalid_realization_fails_the_plan(self):
        out = plan(real(infrastructure={"hub": []}), [], {})
        self.assertTrue(out["errors"])


class PrefixCheck(unittest.TestCase):
    def test_rows(self):
        out = F.service_prefix_check({"realization": real(), "space": ["192.0.2.0/24", "100.64.0.0/24"],
                                      "accepted": ["192.0.2.16/30"],
                                      "prefixes": [{"prefix": "192.0.2.15/32", "cluster": "c1"}, "192.0.2.17/32",
                                                   "100.64.0.5/32"]})
        self.assertEqual([(p["prefix"], p["state"], p["reason"]) for p in out["prefixes"]],
                         [("192.0.2.15/32", "pending", ""), ("192.0.2.17/32", "refused", "ServicePrefixInUse"),
                          ("100.64.0.5/32", "refused", "OutsideAttachableRanges")])
        self.assertEqual(out["prefixes"][0]["cluster"], "c1")
        # from the published rows instead of the realization
        rows = F.reserved_ranges(real())
        out = F.service_prefix_check({"ranges": rows, "attachableRanges": [], "space": ["198.51.100.0/24"],
                                      "prefixes": ["198.51.100.7/32"]})
        self.assertEqual(out["prefixes"][0]["reason"], "PrefixReserved")


class ClusterNetworks(unittest.TestCase):
    def check(self, **kw):
        req = {"realization": real(), "provider": "openstack", "network": True, "space": ["192.0.2.0/24"],
               "clusterNetwork": "10.128.0.0/14", "serviceNetwork": "172.30.0.0/16", "machineNetwork": "10.0.0.0/16"}
        req.update(kw)
        return F.check_cluster_networks(req)

    def test_defaults_pass(self):
        self.assertEqual(self.check(), {"errors": [], "reason": "", "reserved": [], "conflicts": []})

    def test_reserved(self):
        r = real(infrastructure={"hub": ["172.16.0.0/12"]})
        out = self.check(realization=r)
        self.assertEqual(out["reason"], "PrefixReserved")
        self.assertEqual(out["errors"], ["serviceNetwork 172.30.0.0/16 overlaps 172.16.0.0/12, reserved by the "
                                         "platform: networks the platform's management connects from"])

    def test_conflicts(self):
        out = self.check(serviceNetwork="10.128.0.0/16")
        self.assertEqual(out["reason"], "AddressConflict")
        self.assertIn("spec.networking.clusterNetwork 10.128.0.0/14 overlaps spec.networking.serviceNetwork 10.128.0.0/16",
                      out["errors"])
        out = self.check(machineNetwork="192.0.2.0/25")
        self.assertIn("spec.networking.machineNetwork 192.0.2.0/25 overlaps the network's address space 192.0.2.0/24",
                      out["errors"])
        out = self.check(clusterNetwork="192.0.0.0/16")
        self.assertTrue(any("clusterNetwork 192.0.0.0/16 overlaps the network's address space" in e for e in out["errors"]))

    def test_aws_on_a_network(self):
        # the machine network is the placement's subnet: inside the network's space by design, reserved ranges only
        out = self.check(provider="aws", machineNetwork="192.0.2.0/25")
        self.assertEqual(out["errors"], [])
        out = self.check(provider="aws", machineNetwork="198.51.100.0/25")
        self.assertEqual(out["reason"], "PrefixReserved")
        # without a network the machine network is the cluster's own (its pairs only)
        out = self.check(provider="gcp", network=False, space=[], machineNetwork="198.51.100.0/25")
        self.assertEqual(out["errors"], [])

    def test_site_rows(self):
        rows = F.reserved_ranges(real(), [{"name": "site-a", "kind": "openshift", "ranges": []}])
        out = F.check_cluster_networks({"ranges": rows, "site": "site-a", "provider": "openstack",
                                        "serviceNetwork": "100.64.0.0/16"})
        self.assertEqual(out["reason"], "PrefixReserved")
        self.assertIn("addresses the site uses itself", out["errors"][0])
        out = F.check_cluster_networks({"ranges": rows, "site": "site-b", "provider": "openstack",
                                        "serviceNetwork": "100.64.0.0/16"})
        self.assertEqual(out["errors"], [])

    def test_invalid(self):
        out = self.check(clusterNetwork="10.128.0.1/14")
        self.assertEqual(out["reason"], "InvalidSpec")


class NamedRanges(unittest.TestCase):
    def test_check_network(self):
        rows = F.reserved_ranges(real())
        out = F.check_network({"prefixes": ["198.51.0.0/16", "10.255.0.0/25", "192.0.2.0/24"], "ranges": rows})
        self.assertEqual(out["reason"], "PrefixReserved")
        self.assertIn("prefix 198.51.0.0/16 overlaps 198.51.100.0/24, reserved by the platform: networks the "
                      "platform's management connects from", out["reserved"])
        self.assertIn("prefix 10.255.0.0/25 overlaps 10.255.0.0/24, reserved by the platform: addresses inside the "
                      "platform's network nodes", out["reserved"])
        self.assertEqual(out["overlaps"], [{"prefix": "198.51.0.0/16", "range": HUB, "rule": "Refused",
                                            "purpose": "Networks the platform's management connects from"}])
        # a network holding a stepped-around range is fine
        self.assertEqual(F.check_network({"prefixes": ["10.0.0.0/8"], "ranges": rows})["errors"], [])
        # without ranges: the answer of before (no new keys)
        self.assertEqual(set(F.check_network({"prefixes": ["10.0.0.0/8"]})), {"errors", "reserved", "circuits"})

    def test_check_circuit(self):
        rows = F.reserved_ranges(real())
        out = F.check_circuit({"accept": ["198.51.100.0/25", "198.51.0.0/16"], "space": ["192.0.2.0/24"],
                               "ranges": rows})
        self.assertEqual(out["reason"], "PrefixReserved")
        self.assertEqual(out["errors"], ["accept entry '198.51.100.0/25' lies inside 198.51.100.0/24, reserved by the "
                                         "platform: networks the platform's management connects from"])
        self.assertIn("routes overlapping 198.51.100.0/24 (networks the platform's management connects from) are never "
                      "accepted from the CE", out["notes"])


if __name__ == "__main__":
    unittest.main()
