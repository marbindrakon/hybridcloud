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
RANGE = "198.51.100.0/28"
NODES = ["bgw-1", "bgw-2"]


def spec(nodes=None):
    return {"domainAsn": 65010, "vniPool": {"start": 50000, "end": 50999}, "l2vniPool": {"start": 60000, "end": 60999},
            "borderGateway": {"nodes": [{"name": n} for n in (NODES if nodes is None else nodes)]}}


def real(**over):
    r = copy.deepcopy(DEFAULTS)
    r["attachments"]["enabled"] = True
    r["infrastructure"] = {"hub": [HUB]}
    r["service"].update({"enabled": True, "attachableRanges": ["192.0.2.0/24", "203.0.113.0/24"], "range": [RANGE],
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
                         ["192.0.0.8/32", "10.255.0.0/24", "10.255.1.0/24", "10.255.128.0/20", "169.254.0.0/16",
                          # Review R4 H1: the special-purpose space (link-local is egress.linkPool's already)
                          "0.0.0.0/8", "127.0.0.0/8", "224.0.0.0/4", "240.0.0.0/4"])

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
                                               "range": [], "ports": [6443, 443], "hubEndpoints": [],
                                               "attachableRanges": [], "maxPrefixes": 1000})
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
             "realization service.hubEndpoints[0].prefix 192.0.2.10/32 is not inside service.range"),
            # inside the hub's range, outside the service range
            (real(service={"hubEndpoints": [{"prefix": "198.51.100.200/32", "ports": [6443]}]}),
             "realization service.hubEndpoints[0].prefix 198.51.100.200/32 is not inside service.range"),
            (real(service={"range": []}), "realization service.range is required with service.enabled"),
            (real(service={"range": None}), "realization service.range is required with service.enabled"),
            (real(service={"range": ["192.0.2.0/28"]}),
             "realization service.range[0] 192.0.2.0/28 is not inside an IPv4 range of infrastructure.hub"),
            (real(service={"range": ["198.51.100.1/28"]}), "realization service.range[0] '198.51.100.1/28' is not an IPv4"),
            (real(service={"range": ["2001:db8:5::/64"]}), "realization service.range[0] '2001:db8:5::/64' is not an IPv4"),
            (real(service={"maxPrefixes": 0}), "realization service.maxPrefixes 0 must be a whole number from 1"),
            (real(service={"maxPrefixes": "many"}), "realization service.maxPrefixes 'many' must be a whole number"),
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

    def test_interface_names_the_service_filter_would_catch(self):
        # Review R3 N5 (node): the service table drops by wildcard (devices named so*, se*); an interface the realization
        # names (and so the VLAN devices on it) may not be so named while the service is on
        r = real(nodes={"trunkInterface": "se0"}, core={"bridge": "so-core"},
                 egress={"parentInterface": "sonet.100"},
                 attachments={"providerEdges": {"pe1": {"bearers": {"b1": {"interface": "se1.20", "mtu": 9000}}}}})
        e = self.errs(r)
        for key, name in (("nodes.trunkInterface", "se0"), ("core.bridge", "so-core"),
                          ("egress.parentInterface", "sonet.100"),
                          ("attachments.providerEdges.pe1.bearers.b1.interface", "se1.20")):
            want = (f"realization {key} {name!r}: the service VRF's filter drops the traffic of devices named so* or se*"
                    "; rename the interface")
            self.assertIn(want, e)
        # the service off: nothing drops by those names
        r["service"]["enabled"] = False
        self.assertFalse([x for x in self.errs(r) if "so* or se*" in x])
        # other names pass
        self.assertFalse([x for x in self.errs(real(nodes={"trunkInterface": "bond1"})) if "so* or se*" in x])

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
        self.assertNotIn("hub", n)
        self.assertEqual(n["range"], [RANGE])
        self.assertEqual(n["accept"], ["198.51.100.0/28 le 32"])
        self.assertEqual(n["deny"], ["0.0.0.0/0", "10.255.128.0/20 le 32", "10.255.0.0/24 le 32", "10.255.1.0/24 le 32",
                                     "169.254.0.0/16 le 32", "192.0.2.15/32", "192.0.2.16/32"])
        self.assertEqual(n["advertise"], ["192.0.2.15/32", "192.0.2.16/32"])
        self.assertEqual(n["attachable"], ["192.0.2.0/24 le 32", "203.0.113.0/24 le 32"])
        # what is announced is guarded as the hub's routers filter it: never inside a published range
        self.assertEqual(n["refuse"][:2], ["0.0.0.0/0", "198.51.100.0/24 le 32"])
        self.assertEqual(n["circuits"][0]["address"], "10.255.128.9/29")
        self.assertEqual(out["nodes"]["bgw-2"]["circuits"][0]["address"], "10.255.128.10/29")
        st = out["status"]
        self.assertEqual(st["networks"], [{"network": "acme/n1", "vpnTarget": 10005, "index": 4,
                                           "prefixes": ["192.0.2.15/32", "192.0.2.16/32"]}])
        self.assertEqual(st["refused"], [])
        self.assertEqual((st["enabled"], st["vrf"], st["range"], st["sources"], st["ports"]),
                         (True, "service", [RANGE], [RANGE], [443, 6443]))
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
        # the ledger is the order: what the previous status realized does not change it (a run without a platform
        # circuit leaves that status empty; the networks keep their entries through it)
        prev = {"networks": [{"network": "tenant-a/a", "prefixes": ["192.0.2.15/32"]}]}
        out = plan(real(), nets, alloc, previous=prev)
        self.assertEqual([r["network"] for r in out["status"]["refused"]], ["tenant-a/a"])
        out = plan(real(), nets, alloc, previous={})
        self.assertEqual([r["network"] for r in out["status"]["refused"]], ["tenant-a/a"])

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
        # without attachable ranges any prefix that passes the other checks is accepted (first come first served),
        # and the announcement is guarded by the published ranges only; 0.0.0.0/0 written out reads the same
        alloc["servicePrefix"]["HybridNetwork/acme/n1/100.64.10.5/32"] = 2
        for att in ([], None, ["0.0.0.0/0"]):
            with self.subTest(attachableRanges=att):
                r = real(service={"attachableRanges": att})
                out = plan(r, nets, alloc)
                self.assertEqual(out["errors"], [])
                self.assertEqual(out["status"]["networks"][0]["prefixes"], ["100.64.10.5/32", "192.0.2.15/32"])
                self.assertEqual(out["nodes"]["bgw-1"]["attachable"], ["0.0.0.0/0 le 32"])
                self.assertEqual(out["status"]["attachableRanges"], att or [])
                # the hub's range stays refused whatever the attachable ranges say
                self.assertEqual({x["prefix"]: x["reason"] for x in out["status"]["refused"]}["198.51.100.20/32"],
                                 "PrefixReserved")
        self.assertTrue(set(out["status"]["refused"][0].keys()) >= {"network", "prefix", "reason"})
        self.assertTrue(all(r["reason"] in F.SERVICE_PREFIX_REASONS for r in out["status"]["refused"]))

    def test_only_the_accepted_address_space_counts(self):
        # a prefix the tenant wrote into spec.prefixes but the network job did not accept (status.addressSpace) does
        # not make a cluster prefix inside it the network's
        nets = [network("n1", ["192.0.2.0/24", "203.0.113.0/24"], [("203.0.113.15/32", {})])]
        nets[0]["status"]["addressSpace"] = [{"prefix": "192.0.2.0/24", "family": 4}]
        alloc = ledger({("acme/n1", "203.0.113.15/32"): 1}, {"acme/n1": 10005}, {"acme/n1": 0})
        out = plan(real(), nets, alloc)
        self.assertEqual([(r["prefix"], r["reason"]) for r in out["status"]["refused"]],
                         [("203.0.113.15/32", "OutsideAddressSpace")])

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


class ServiceRange(unittest.TestCase):
    """The service filter after the owner's word on it: service.range required and narrow (the only sources toward the
    clusters, the only destinations and routes from the hub), attachableRanges optional (absent = any range the
    platform does not refuse, first come first served), a maximum-prefix figure the platform keeps to."""

    def test_valid_without_attachable_ranges(self):
        for att in (None, []):
            r = real(service={"attachableRanges": att})
            if att is None:
                del r["service"]["attachableRanges"]
            self.assertEqual(F.check_fabric({"spec": spec(), "realization": r})["errors"], [])

    def test_range_may_equal_the_hub(self):
        self.assertEqual(F.check_fabric({"spec": spec(), "realization": real(service={"range": [HUB]})})["errors"], [])
        # an address is a /32; several ranges, each inside some hub range
        r = real(infrastructure={"hub": [HUB, "203.0.113.0/24"]}, service={"range": ["198.51.100.10/32", "203.0.113.0/28"],
                                                                            "attachableRanges": ["192.0.2.0/24"]})
        self.assertEqual(F.check_fabric({"spec": spec(), "realization": r})["errors"], [])

    def test_the_service_off_needs_no_range(self):
        r = copy.deepcopy(DEFAULTS)
        r["infrastructure"] = {"hub": [HUB]}
        del r["service"]["range"]
        self.assertEqual(F.check_fabric({"spec": spec(), "realization": r})["errors"], [])

    def test_in_out_and_the_guard(self):
        nets = [network("n1", ["192.0.2.0/24"], [("192.0.2.15/32", {})])]
        alloc = ledger({("acme/n1", "192.0.2.15/32"): 1}, {"acme/n1": 10005}, {"acme/n1": 4})
        r = real(service={"attachableRanges": []}, network={"exclusiveRanges": ["100.127.0.0/16"]})
        n = plan(r, nets, alloc)["nodes"]["bgw-1"]
        # in: the service range only (never the rest of the hub's range)
        self.assertEqual(n["accept"], ["198.51.100.0/28 le 32"])
        self.assertEqual(n["range"], [RANGE])
        # out, guarded: refuse every published range (any length), then anything up to /32
        self.assertEqual(n["refuse"][:3], ["0.0.0.0/0", "198.51.100.0/24 le 32", "100.127.0.0/16 le 32"])
        self.assertIn("192.0.0.8/32", n["refuse"])
        self.assertTrue(all(":" not in e for e in n["refuse"]))
        self.assertEqual(n["attachable"], ["0.0.0.0/0 le 32"])

    def test_at_most_max_prefixes_in_the_ledger_order(self):
        nets = [network("a", ["192.0.2.0/24"], [("192.0.2.15/32", {}), ("192.0.2.16/32", {})], ns="t-a"),
                network("b", ["203.0.113.0/24"], [("203.0.113.5/32", {})], ns="t-b")]
        alloc = ledger({("t-a/a", "192.0.2.15/32"): 3, ("t-a/a", "192.0.2.16/32"): 9, ("t-b/b", "203.0.113.5/32"): 5},
                       {"t-a/a": 10001, "t-b/b": 10002}, {"t-a/a": 1, "t-b/b": 2})
        out = plan(real(service={"maxPrefixes": 2}), nets, alloc)
        self.assertEqual([(w["network"], w["prefixes"]) for w in out["status"]["networks"]],
                         [("t-a/a", ["192.0.2.15/32"]), ("t-b/b", ["203.0.113.5/32"])])
        self.assertEqual(out["status"]["refused"], [])
        self.assertEqual([(p["prefix"], p["why"]) for p in out["pending"] if p.get("prefix")],
                         [("192.0.2.16/32", "the platform announces at most service.maxPrefixes 2 prefixes")])
        self.assertEqual(out["nodes"]["bgw-1"]["advertise"], ["192.0.2.15/32", "203.0.113.5/32"])
        # the default holds them all
        out = plan(real(), nets, alloc)
        self.assertEqual(sum(len(w["prefixes"]) for w in out["status"]["networks"]), 3)

    def test_ce_config_in_the_hub_routers_order(self):
        r = real(service={"attachableRanges": [], "maxPrefixes": 200})
        ce = F.check_circuit({"ceKind": "platform", "namespace": "platform", "fabricNamespace": "platform",
                              "realization": r})["ceConfig"]
        self.assertEqual(list(ce), ["acceptedRanges", "refusedRanges", "advertisedRanges", "maxPrefixes"])
        self.assertEqual(ce["acceptedRanges"], ["198.51.100.0/28 le 32"])
        self.assertEqual(ce["refusedRanges"][:2], ["0.0.0.0/0", "198.51.100.0/24 le 32"])
        self.assertEqual(ce["advertisedRanges"], ["0.0.0.0/0 le 32"])
        self.assertEqual(ce["maxPrefixes"], 200)
        ce = F.check_circuit({"ceKind": "platform", "namespace": "platform", "fabricNamespace": "platform",
                              "realization": real(service={"range": ["198.51.100.10/32"]})})["ceConfig"]
        self.assertEqual((ce["acceptedRanges"], ce["advertisedRanges"], ce["maxPrefixes"]),
                         (["198.51.100.10/32"], ["192.0.2.0/24 le 32", "203.0.113.0/24 le 32"], 1000))

    def test_prefix_check_without_attachable_ranges(self):
        out = F.service_prefix_check({"realization": real(service={"attachableRanges": []}),
                                      "space": ["100.64.0.0/24", "198.51.100.0/24"],
                                      "prefixes": ["100.64.0.5/32", "198.51.100.200/32"]})
        self.assertEqual([(p["state"], p["reason"]) for p in out["prefixes"]], [("pending", ""), ("refused", "PrefixReserved")])
        rows = F.reserved_ranges(real())
        out = F.service_prefix_check({"ranges": rows, "space": ["100.64.0.0/24"], "prefixes": ["100.64.0.5/32"]})
        self.assertEqual(out["prefixes"][0]["reason"], "")


class ServiceRefusal(unittest.TestCase):
    """Review R4 H1: every range a service prefix may not overlap, one set (service_refusal_rows) for the planner's
    verdict, the node guard and the hub router's refusedRanges, and in the published list: every site's own ranges
    whichever site the cluster is on (a service prefix is announced to the hub's network, where every site is routed),
    the platform's pools and hand-off, the service range, the special-purpose space."""

    SITES = [{"name": "site-a", "kind": "openstack", "ranges": ["10.30.0.0/16"]},
             {"name": "site-b", "kind": "openshift", "ranges": ["10.40.0.0/16"]}]
    EGRESS = {"enabled": True, "parentInterface": "bond1.100", "gateway": "203.0.113.1", "prefixLength": 24,
              "linkPool": "169.254.0.0/16",
              "nodes": {"bgw-1": {"address": "203.0.113.11", "natPool": "203.0.113.64/26"},
                        "bgw-2": {"address": "203.0.113.12", "natPool": "203.0.113.128/26"}}}

    def real(self):
        return real(service={"attachableRanges": []}, egress=self.EGRESS)

    def plan_one(self, prefix, space):
        nets = [network("n1", [space], [(prefix, {})])]
        alloc = ledger({("acme/n1", prefix): 1}, {"acme/n1": 10005}, {"acme/n1": 4})
        out = F.service_plan({"realization": self.real(), "nodes": NODES, "fabric": "lab", "domainAsn": 65010,
                              "networks": nets, "ledger": alloc, "circuits": [circuit()], "sites": self.SITES,
                              "vrfs": {n: [] for n in NODES}, "previous": {}})
        self.assertEqual(out["errors"], [])
        return out

    def test_another_sites_underlay_is_refused(self):
        # the review's case: a network placed on site A with an address of site B's underlay
        out = self.plan_one("10.40.0.20/32", "10.40.0.0/24")
        self.assertEqual(out["status"]["networks"], [])
        r = out["status"]["refused"][0]
        self.assertEqual((r["prefix"], r["reason"]), ("10.40.0.20/32", "PrefixReserved"))
        self.assertIn("10.40.0.0/16", r["message"])
        self.assertIn("site-b", r["message"])
        # and its site-reserved ranges (site B is OpenShift Virtualization: placement.siteReserved.CloudVirt)
        out = self.plan_one("100.64.0.9/32", "100.64.0.0/24")
        self.assertEqual(out["status"]["refused"][0]["reason"], "PrefixReserved")

    def test_the_hand_off_and_the_special_purpose_space_are_refused(self):
        for prefix, space in (("203.0.113.70/32", "203.0.113.64/27"), ("203.0.113.40/32", "203.0.113.32/27"),
                              ("240.0.0.5/32", "240.0.0.0/24"), ("127.0.0.5/32", "127.0.0.0/24"),
                              ("224.0.0.5/32", "224.0.0.0/24"), ("0.0.0.5/32", "0.0.0.0/24")):
            with self.subTest(prefix=prefix):
                self.assertEqual(self.plan_one(prefix, space)["status"]["refused"][0]["reason"], "PrefixReserved")

    def test_three_users_one_set(self):
        out = self.plan_one("192.0.2.15/32", "192.0.2.0/24")
        self.assertEqual(out["status"]["networks"][0]["prefixes"], ["192.0.2.15/32"])
        n = out["nodes"]["bgw-1"]
        ce = F.check_circuit({"ceKind": "platform", "namespace": "platform", "fabricNamespace": "platform",
                              "realization": self.real(), "ranges": F.reserved_ranges(self.real(), self.SITES)})["ceConfig"]
        self.assertEqual(ce["refusedRanges"], n["refuse"])
        srows = F.service_refusal_rows(F.rows_of(F.reserved_ranges(self.real(), self.SITES)))
        self.assertEqual(["0.0.0.0/0"] + [F.prefix_entry(x[0]) for x in srows], n["refuse"])
        for want in ("10.30.0.0/16 le 32", "10.40.0.0/16 le 32", "100.64.0.0/16 le 32", "203.0.113.64/26 le 32",
                     "203.0.113.0/24 le 32", "198.51.100.0/24 le 32", "127.0.0.0/8 le 32", "240.0.0.0/4 le 32"):
            self.assertIn(want, n["refuse"])
        # the service range lies inside the hub's range (check-fabric): covered by it, listed once
        self.assertNotIn("198.51.100.0/28 le 32", n["refuse"])

    def test_published_and_the_tenant_network_check_keep_their_rules(self):
        rows = F.reserved_ranges(self.real(), self.SITES)
        by = {(r["prefix"], r.get("site", "")): r["rule"] for r in rows}
        self.assertEqual(by[("10.40.0.0/16", "site-b")], "SteppedAround")
        self.assertEqual(by[("203.0.113.64/26", "")], "Refused")
        self.assertEqual(by[("127.0.0.0/8", "")], "Refused")
        self.assertNotIn(("198.51.100.0/28", ""), by)  # inside the hub's row
        # a tenant network on site A may hold site B's range (a site's own ranges only constrain placements there)
        out = F.check_network({"prefixes": ["10.40.0.0/24"], "ranges": rows, "site": "site-a"})
        self.assertEqual(out.get("reason", ""), "")
        # the per-prefix check (the network job's helper) with the published rows refuses it for the service
        out = F.service_prefix_check({"prefixes": ["10.40.0.20/32"], "space": ["10.40.0.0/24"], "ranges": rows})
        self.assertEqual(out["prefixes"][0]["reason"], "PrefixReserved")


class DefaultRouteRefused(unittest.TestCase):
    def test_the_hub_router_refuses_a_default_route_first(self):
        # Review R4 L3: "0.0.0.0/0 le 32" would also admit a default route at the hub's router; it is refused first
        ce = F.check_circuit({"ceKind": "platform", "namespace": "platform", "fabricNamespace": "platform",
                              "realization": real(service={"attachableRanges": []})})["ceConfig"]
        self.assertEqual(ce["refusedRanges"][0], "0.0.0.0/0")
        nets = [network("n1", ["192.0.2.0/24"], [("192.0.2.15/32", {})])]
        alloc = ledger({("acme/n1", "192.0.2.15/32"): 1}, {"acme/n1": 10005}, {"acme/n1": 4})
        n = plan(real(service={"attachableRanges": []}), nets, alloc)["nodes"]["bgw-1"]
        self.assertEqual(n["refuse"], ce["refusedRanges"])


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
