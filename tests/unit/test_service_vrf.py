"""The service VRF on the border gateway nodes' root instance, end to end through the planner (files/shc_fabric.py),
the root FRR template (hybridfabric_provision frr-root.conf.j2) and the node script (files/bgw/shc-fabric-apply,
against the fake kernel of test_shc_fabric_apply).

- Inert by default: for a fabric without the service VRF (the realization's defaults: service.enabled off, no
  infrastructure.hub) the planner, the template and the node script give, byte for byte, what the release before this
  round gave (BASE, read from Git; skipped without it) for networks with internet egress, IPv6 egress and attachment
  circuits, on the MPLS and the SRv6 core: the same frr.conf, the same calls on the node in the same order and the
  same state; root.json differs only by the retired platform reach keys (egress.platformSources, platformPorts,
  networks[].platform), which the node script never reads any more. The new script on a node the old one left reads
  changed=no.
- With the service VRF: the template's service VRF instance, its policy (the hub's ranges in, the accepted prefixes
  out, the attachable ranges guarding the redistribution), the network VRFs' routes to the hub over their service
  links and SHC-VRF-STATIC (a VRF without a link keeps SHC-EGRESS-DEFAULT), no VPN for the service VRF, a SID or label
  for a service-only network VRF on either core; the node script's devices and table, a second run that changes
  nothing, the removal of a network's link and of the whole service, the interplay with the circuits' devices, the
  root VRFs' input filters, the IPv4 ICMP source and the SRv6 settings, and the snapshot; the ruleset in a real kernel
  (an unprivileged user and network namespace; skipped where that is not possible): the hub reaches a cluster's
  prefix on the service ports only, a cluster reaches the hub endpoints only, a reply that crosses this node without
  its request passes (stateless: the two directions may cross different nodes), nothing passes between two networks.

Documentation and private ranges, example names only."""
import copy
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_frr_templates as T  # noqa: E402
import test_shc_fabric_apply as A  # noqa: E402
from play_helper import ROOT  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "eda", "common", "files"))
import shc_fabric as F  # noqa: E402

# the release before the service VRF (this round's base)
BASE = "669b781a"
NODES = ["bgw-1", "bgw-2"]
HUB = "198.51.100.0/24"
with open(os.path.join(ROOT, "eda", "common", "vars", "fabric_realization.yml")) as fh:
    DEFAULTS = yaml.safe_load(fh)["fabric_realization_defaults"]


def git_show(rel):
    try:
        p = subprocess.run(["git", "-C", ROOT, "show", f"{BASE}:{rel}"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def base_module():
    text = git_show("eda/common/files/shc_fabric.py")
    if text is None:
        return None
    path = os.path.join(tempfile.mkdtemp(prefix="shc-fabric-base-"), "shc_fabric_base_svc.py")
    with open(path, "w") as f:
        f.write(text)
    spec = importlib.util.spec_from_file_location("shc_fabric_base_svc", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


OLD = base_module()
EGRESS = {"enabled": True, "vrf": {"name": "egress", "table": 9000}, "networkTableBase": 100000,
          "linkPool": "169.254.0.0/16", "parentInterface": "bond1.100", "gateway": "203.0.113.1", "prefixLength": 24,
          "mtu": 9000, "nodes": {"bgw-1": {"address": "203.0.113.13", "natPool": "203.0.113.64/26"},
                                 "bgw-2": {"address": "203.0.113.14", "natPool": "203.0.113.128/26"}},
          "denyDestinations": ["10.0.0.0/8", "172.16.0.0/12"], "allowDestinations": []}
E6 = {"enabled": True, "routedPrefixes": ["2001:db8:4005::/48"], "asn": 65011,
      "upstream": [{"address": "2001:db8:237::2", "asn": 65002}],
      "nodes": {"bgw-1": {"address": "2001:db8:237::13/64"}, "bgw-2": {"address": "2001:db8:237::14/64"}},
      "denyDestinations6": ["fc00::/7"], "allowDestinations6": [], "resolverDestinations6": []}
AT = {"enabled": True, "vlanPool": {"start": 3000, "end": 3099}, "linkPool": "10.255.128.0/20", "linkPrefixLength": 29,
      "providerEdges": {"edge": {"bearers": {"trunk": {"interface": "bond1", "mtu": 1500}}}}}
CORE = {"bridge": "br-core", "loopbackPool": "10.255.0.0/24", "linkSubnet": "10.255.1.0/24",
        "srgb": {"start": 16000, "end": 23999}, "sidOffset6": 1000, "icmpSource4": "192.0.0.8", "transport": "mpls",
        "loopbackPool6": "", "icmpSource6": "", "srv6": {"block": ""}}
CORE6 = dict(CORE, loopbackPool6="2001:db8:4005:ffff::/64", icmpSource6="2001:db8:4005:1::/64")
CORE_SRV6 = dict(CORE6, transport="srv6", srv6={"block": "5f00:1::/32"})


def hn(name, prefixes, internet=True, rows=()):
    return {"metadata": {"namespace": "acme", "name": name},
            "spec": {"prefixes": list(prefixes), "egress": {"internet": internet}},
            "status": {"fabric": "lab", "addressSpace": [{"prefix": p, "family": 4} for p in prefixes],
                       "platformAccess": {"prefixes": [{"prefix": p, "cluster": "c1", "state": "pending"} for p in rows]}}}


def customer(name, net, vlan, index, target):
    p = F.circuit_plan({"attachments": AT, "nodes": NODES, "index": index})
    return {"metadata": {"namespace": "acme", "name": name}, "spec": {"network": net},
            "status": {"fabric": "lab", "realization": {
                "vlan": vlan, "linkIndex": index, "link": p["link"], "nodes": p["nodes"], "ce": p["ce"],
                "vrf": f"e{target}", "vpnTarget": target, "interface": "bond1", "mtu": 1500, "peerAsn": 64512,
                "authentication": "none", "maxPrefixes": 10, "maxPrefixAction": "teardown", "bfd": True,
                "accept": ["10.50.0.0/16 le 32"], "advertiseNetworkPrefixes": True, "advertiseDefault": False,
                "rev": f"r{index}"}}}


def platform(index=9, vlan=3099):
    p = F.circuit_plan({"attachments": AT, "nodes": NODES, "index": index})
    return {"metadata": {"namespace": "platform", "name": "hub-a"}, "spec": {"ceKind": "platform"},
            "status": {"fabric": "lab", "realization": {
                "vlan": vlan, "linkIndex": index, "link": p["link"], "nodes": p["nodes"], "ce": p["ce"],
                "interface": "bond1", "mtu": 1500, "peerAsn": 64600, "authentication": "none", "maxPrefixes": 50,
                "maxPrefixAction": "teardown", "bfd": False, "rev": "r9"}}}


NETS = [hn("core", ["10.60.0.0/16"]), hn("batch", ["10.61.0.0/16"], internet=False),
        hn("lab", ["192.0.2.0/24"], internet=False)]
CIRCUITS = [customer("ce1", "batch", 3000, 0, 10001)]
LEDGER = {"vpnTarget": {"HybridNetwork/acme/core": 10000, "HybridNetwork/acme/batch": 10001,
                        "HybridNetwork/acme/lab": 10002},
          "egress": {"HybridNetwork/acme/core": 0, "HybridNetwork/acme/lab": 2},
          "attachVlan": {"AttachmentCircuit/acme/ce1": 3000, "AttachmentCircuit/platform/hub-a": 3099},
          "attachLink": {"AttachmentCircuit/acme/ce1": 0, "AttachmentCircuit/platform/hub-a": 9}}


def realization(core, v6egress=False, service=False):
    r = copy.deepcopy(DEFAULTS)
    r["core"].update(copy.deepcopy(core))
    r["egress"].update(copy.deepcopy(EGRESS))
    r["egress"]["ipv6"] = copy.deepcopy(E6) if v6egress else {"enabled": False}
    r["attachments"].update(copy.deepcopy(AT))
    if service:
        r["infrastructure"] = {"hub": [HUB]}
        r["service"].update({"enabled": True, "attachableRanges": ["192.0.2.0/24"],
                             "hubEndpoints": [{"prefix": "198.51.100.10/32", "ports": [6443]}]})
    return r


def pipeline(mod, real, new, nets=NETS, circuits=CIRCUITS, ledger=LEDGER):
    """What the fabric job computes for the nodes (main.yml): egress-plan, attachments-plan, (new) service-plan, the
    root VRFs' families. new: the request shapes of this release (no platform reach keys, the hub and the service)."""
    eg = copy.deepcopy(real["egress"])
    if not new:
        eg.update(platformSources=[], platformPorts=[6443, 443])
    egress_nets = []
    for n in nets:
        key, owner = f"{n['metadata']['namespace']}/{n['metadata']['name']}", f"HybridNetwork/acme/{n['metadata']['name']}"
        if n["spec"]["egress"]["internet"] and owner in ledger["egress"]:
            w = {"network": key, "vpnTarget": ledger["vpnTarget"][owner], "index": ledger["egress"][owner],
                 "internet": True, "ipv4": True}
            if eg["ipv6"].get("enabled") and key == "acme/core":
                w["ipv6"] = {"block": "2001:db8:4005:4200::/56", "prefixes": ["2001:db8:4005:4200::/64"]}
            if not new:
                w["platform"] = []
            egress_nets.append(w)
    req = {"egress": eg, "nodes": NODES, "networks": egress_nets, "domainAsn": 65010, "core": real["core"],
           "translation": {}}
    if new:
        req["service"] = bool(real["service"]["enabled"])
    egp = mod.egress_plan(req)
    assert not egp["errors"], egp["errors"]
    hub4 = [h for h in (real.get("infrastructure") or {}).get("hub") or [] if ":" not in h]
    areq = {"fabric": "lab", "attachments": real["attachments"], "nodes": NODES, "tableBase": 100000,
            "domainAsn": 65010, "circuits": circuits, "passwords": [], "networks": nets, "placements": [],
            "ledger": ledger, "egress": egp["networks"], "egressNodes": egp["nodes"],
            "reserved": [real["attachments"]["linkPool"], real["core"]["loopbackPool"], real["core"]["linkSubnet"],
                         real["egress"]["linkPool"]] + hub4,
            "platformSources": hub4, "core": real["core"], "translation": {}}
    if new:
        areq.update(hub6=[], service=real["service"], fabricNamespace="platform")
    att = mod.attachments_plan(areq)
    vrfs, svc = att["vrfs"], {}
    if new:
        sp = F.service_plan({"realization": real, "nodes": NODES, "fabric": "lab", "domainAsn": 65010,
                             "networks": nets, "ledger": ledger, "circuits": att.get("platform", []), "vrfs": att["vrfs"],
                             "previous": {}})
        assert not sp["errors"], sp["errors"]
        vrfs, svc = sp["vrfs"], sp["nodes"]
    fam = {n: [dict(v, families=[4, 6] if v.get("ipv6") is not None or any("ipv6" in c for c in v["circuits"])
                    else [4]) for v in vs] for n, vs in vrfs.items()}
    return egp, att, fam, svc


def template_vars(real, egp, att, vrfs, svc, node, new):
    core = real["core"]
    v6 = bool(core["loopbackPool6"])
    pe = T.pe(1, v6)
    srv6 = core.get("transport") == "srv6"
    if srv6:
        pe.update(locator="5f00:1:101::/48", locatorNode=257)
    out = {"fab_name": "lab", "bgw_node": {"name": node}, "hf_root": {"bridge": "br-core", "link": "10.255.1.1/24"},
           "fab_spec": {"domainAsn": 65010}, "fab_real": real, "hf_pe": pe,
           "hf_node_egress": egp["nodes"].get(node, {}), "hf_node_vrfs": vrfs.get(node, []),
           "hf_circuit_passwords": {}, "hf_attach_restart": 2, "fab_node_transport": core.get("transport"),
           "fab_frr_major": 8}
    if new:
        out["hf_node_service"] = svc.get(node, {})
    return out


def root_json(real, egp, att, svc, node, new):
    """root.json as node_root.yml writes it (the IPv4-only core's keys)."""
    core = real["core"]
    e = egp["nodes"].get(node, {})
    if e.get("ipv6") is not None:
        e = dict(e, ipv6=dict(e["ipv6"], icmp6=""))
    out = {"type": "root", "bridge": "br-core", "mtu": "9100", "loopback": "10.255.0.1/32",
           "loopbackPool": core["loopbackPool"], "link": "10.255.1.1/24"}
    if e:
        out["egress"] = e
    if att["nodes"].get(node):
        out["attachments"] = att["nodes"][node]
    if new and svc.get(node):
        out["service"] = svc[node]
    out.update({"loopback6": "", "icmp6": "", "loopbackPool6": core["loopbackPool6"], "icmpSource6": core["icmpSource6"],
                "icmp4": core["icmpSource4"]})
    if core.get("transport") == "srv6":
        out.update({"loopback6": "2001:db8:4005:ffff::1/128", "transport": "srv6",
                    "srv6": {"block": "5f00:1::/32", "locator": "5f00:1:101::/48"}})
    return out


VARIANTS = (("mpls", CORE, False), ("mpls-ipv6-egress", CORE6, True), ("srv6", CORE_SRV6, False))


@unittest.skipIf(OLD is None, f"release {BASE} not readable from Git")
class InertWithoutTheService(unittest.TestCase):
    def test_frr_conf_byte_for_byte(self):
        old_t = git_show(os.path.relpath(T.RT, ROOT))
        for name, core, v6egress in VARIANTS:
            real = realization(core, v6egress)
            o, n = pipeline(OLD, real, False), pipeline(F, real, True)
            for node in NODES:
                with self.subTest(variant=name, node=node):
                    new = T.render(T.RT, template_vars(real, n[0], n[1], n[2], n[3], node, True))
                    old = T.render_text(old_t, template_vars(real, o[0], o[1], o[2], o[3], node, False))
                    T.save(f"inert-{name}-{node}.conf", new)
                    self.assertEqual(new, old)
                    self.assertIn("route-map SHC-EGRESS-DEFAULT permit 10", new)
                    self.assertNotIn("SHC-VRF-STATIC", new)

    def test_plans_and_root_json(self):
        for name, core, v6egress in VARIANTS:
            real = realization(core, v6egress)
            o, n = pipeline(OLD, real, False), pipeline(F, real, True)
            self.assertEqual(n[3], {})
            for node in NODES:
                with self.subTest(variant=name, node=node):
                    old, new = root_json(real, *o[:2], o[3], node, False), root_json(real, *n[:2], n[3], node, True)
                    # the retired platform reach keys, which the node script no longer reads, and nothing else
                    self.assertEqual((old["egress"].pop("platformSources"), old["egress"].pop("platformPorts")),
                                     ([], [6443, 443]))
                    for w in old["egress"]["networks"]:
                        self.assertEqual(w.pop("platform"), [])
                    self.assertEqual(new, old)
                    self.assertNotIn("service", new)
            # the egress status loses networks[].platform only
            for k, w in o[0]["networks"].items():
                self.assertEqual(w.pop("platform"), [])
            self.assertEqual(n[0]["networks"], o[0]["networks"])

    def test_node_calls_and_state(self):
        base = git_show(A.SCRIPT_REL)
        if base is None:
            self.skipTest(f"{BASE} not in this checkout")
        for name, core, v6egress in VARIANTS:
            real = realization(core, v6egress)
            o, n = pipeline(OLD, real, False), pipeline(F, real, True)
            with self.subTest(variant=name):
                tmp = tempfile.mkdtemp(prefix="svc-inert-")
                self.addCleanup(__import__("shutil").rmtree, tmp, True)
                h = A.Harness(tmp, baseline=base)
                h.set_state(blade())
                h.conf("root", root_json(real, *o[:2], o[3], "bgw-1", False))
                rc, out_old, err, log_old = h.run("root", script="old")
                self.assertEqual(rc, 0, err)
                st_old = h.get_state()
                rc, out, err, log_old2 = h.run("root", script="old")
                self.assertTrue(out.strip().endswith("changed=no"), out)
                h.set_state(st_old)
                h.conf("root", root_json(real, *n[:2], n[3], "bgw-1", True))
                # the new script on the node the old one left: nothing changes, the same calls as the old one's second
                # run (each run loads its tables again)
                rc, out, err, log = h.run("root")
                self.assertEqual(rc, 0, err)
                self.assertTrue(out.strip().endswith("changed=no"), out)
                self.assertEqual(log, log_old2)
                self.assertEqual(h.get_state(), st_old)
                # and on a fresh node: the same calls in the same order, the same state
                h.set_state(blade())
                for f in os.listdir(h.etc):
                    if not f.endswith(".json"):
                        os.remove(os.path.join(h.etc, f))
                rc, out_new, err, log_new = h.run("root")
                self.assertEqual(rc, 0, err)
                self.assertEqual(log_new, log_old)
                self.assertEqual(h.get_state(), st_old)
                self.assertEqual(out_new, out_old)


def service_setup(core=CORE, v6egress=False, nets=None, ledger=None):
    real = realization(core, v6egress, service=True)
    nets = nets if nets is not None else [hn("core", ["10.60.0.0/16"]), hn("batch", ["10.61.0.0/16"], internet=False),
                                           hn("lab", ["192.0.2.0/24"], internet=False, rows=["192.0.2.15/32", "192.0.2.16/32"]),
                                           hn("web", ["192.0.2.0/24"], rows=[])]
    led = copy.deepcopy(ledger or LEDGER)
    led.setdefault("servicePrefix", {}).update({"HybridNetwork/acme/lab/192.0.2.15/32": 1,
                                                "HybridNetwork/acme/lab/192.0.2.16/32": 2})
    return real, pipeline(F, real, True, nets=nets, circuits=CIRCUITS + [platform()], ledger=led)


class ServiceTemplate(unittest.TestCase):
    def render(self, core=CORE, **kw):
        real, (egp, att, vrfs, svc) = service_setup(core, **kw)
        return T.render(T.RT, template_vars(real, egp, att, vrfs, svc, "bgw-1", True)), svc

    def test_service_vrf(self):
        out, svc = self.render()
        T.save("service-bgw-1.conf", out)
        self.assertIn("vrf service\n ip route 192.0.2.15/32 169.254.128.5\n ip route 192.0.2.16/32 169.254.128.5\n"
                      "exit-vrf\n", out)
        self.assertIn("ip prefix-list SVC-HUB seq 5 permit 198.51.100.0/24 le 32\n", out)
        self.assertIn("ip prefix-list SVC-DENY seq 5 permit 0.0.0.0/0\n", out)
        self.assertIn("ip prefix-list SVC-ACCEPTED seq 5 permit 192.0.2.15/32\nip prefix-list SVC-ACCEPTED seq 10 permit "
                      "192.0.2.16/32\n", out)
        self.assertIn("ip prefix-list SVC-ATTACHABLE seq 5 permit 192.0.2.0/24 le 32\n", out)
        self.assertIn("route-map SVC-STATIC permit 10\n match ip address prefix-list SVC-ATTACHABLE\nexit\n", out)
        self.assertIn("route-map AC3099-IN deny 10\n match ip address prefix-list SVC-DENY\nexit\n"
                      "route-map AC3099-IN permit 20\n match ip address prefix-list SVC-HUB\nexit\n"
                      "route-map AC3099-OUT permit 10\n match ip address prefix-list SVC-ACCEPTED\nexit\n", out)
        ce = svc["bgw-1"]["circuits"][0]["ce"]
        inst = out.split("router bgp 65010 vrf service\n")[1].split("\nexit\n")[0]
        self.assertEqual(inst.splitlines(), [
            " bgp router-id 10.255.0.1", " no bgp default ipv4-unicast",
            f" neighbor {ce} remote-as 64600", f" neighbor {ce} description AC3099 platform/hub-a",
            f" neighbor {ce} timers 10 30", " !", " address-family ipv4 unicast",
            "  redistribute static route-map SVC-STATIC", f"  neighbor {ce} activate",
            f"  neighbor {ce} route-map AC3099-IN in", f"  neighbor {ce} route-map AC3099-OUT out",
            f"  neighbor {ce} maximum-prefix 50 restart 2", " exit-address-family"])
        # no VPN for the service VRF
        self.assertNotRegex(inst, r"rd vpn|rt vpn|label vpn|sid vpn|export vpn|import vpn")

    def test_network_vrfs(self):
        out, _ = self.render()
        # the service-only network: its own vrf stanza with the route to the hub, SHC-VRF-STATIC, a label
        self.assertIn("vrf e10002\n ip route 198.51.100.0/24 169.254.128.4\nexit-vrf\n", out)
        lab = out.split("router bgp 65010 vrf e10002\n")[1].split("\nexit\n")[0]
        self.assertIn("  redistribute static route-map SHC-VRF-STATIC\n", lab)
        self.assertIn("  label vpn export auto\n", lab)
        self.assertIn("  nexthop vpn export 10.255.0.1\n", lab)
        self.assertIn("route-map SHC-VRF-STATIC permit 10\n match ip address prefix-list SHC-DEFAULT4\nexit\n"
                      "route-map SHC-VRF-STATIC permit 20\n match ip address prefix-list SHC-HUB4\nexit\n", out)
        self.assertIn("ip prefix-list SHC-HUB4 seq 5 permit 198.51.100.0/24 le 32\n", out)
        self.assertIn("! HybridNetwork acme/lab: clusters the platform reaches (192.0.2.15/32,192.0.2.16/32)", out)
        # networks without clusters: as without the service
        core = out.split("router bgp 65010 vrf e10000\n")[1].split("\nexit\n")[0]
        self.assertIn("  redistribute static route-map SHC-EGRESS-DEFAULT\n", core)
        self.assertNotIn("ip route 198.51.100.0/24", out.split("vrf e10000\n")[1].split("exit-vrf")[0])
        self.assertNotIn("vrf e10003", out)

    def test_egress_network_with_clusters(self):
        nets = [hn("core", ["10.60.0.0/16"], rows=["10.60.1.15/32"])]
        led = copy.deepcopy(LEDGER)
        led["servicePrefix"] = {"HybridNetwork/acme/core/10.60.1.15/32": 1}
        real, (egp, att, vrfs, svc) = service_setup(nets=nets, ledger=led)
        real["service"]["attachableRanges"] = ["0.0.0.0/0"]
        egp, att, vrfs, svc = pipeline(F, real, True, nets=nets, circuits=[platform()], ledger=led)
        out = T.render(T.RT, template_vars(real, egp, att, vrfs, svc, "bgw-1", True))
        # one vrf stanza: the default to the egress and the hub over the service link
        self.assertIn("vrf e10000\n ip route 0.0.0.0/0 169.254.0.0\n ip route 198.51.100.0/24 169.254.128.0\nexit-vrf\n", out)
        self.assertEqual(out.count("vrf e10000\n"), 2)  # the stanza and the BGP instance
        self.assertIn("  redistribute static route-map SHC-VRF-STATIC\n", out.split("router bgp 65010 vrf e10000\n")[1])
        self.assertIn("ip prefix-list SVC-ATTACHABLE seq 5 permit 0.0.0.0/0 le 32\n", out)

    def test_srv6(self):
        out, _ = self.render(core=CORE_SRV6)
        lab = out.split("router bgp 65010 vrf e10002\n")[1].split("\nexit\n")[0]
        self.assertIn(" sid vpn per-vrf export auto\n", lab)
        self.assertNotIn("label vpn", lab)
        self.assertNotIn("sid vpn", out.split("router bgp 65010 vrf service\n")[1].split("\nexit\n")[0])

    def test_no_accepted_prefixes(self):
        nets = [hn("lab", ["192.0.2.0/24"], internet=False)]
        real, (egp, att, vrfs, svc) = service_setup(nets=nets)
        out = T.render(T.RT, template_vars(real, egp, att, vrfs, svc, "bgw-1", True))
        self.assertNotIn("\nvrf service\n", out)
        self.assertIn("ip prefix-list SVC-ACCEPTED seq 5 deny 0.0.0.0/0 le 32\n", out)
        self.assertIn("route-map AC3099-OUT deny 10\nexit\n", out)
        self.assertIn("router bgp 65010 vrf service\n", out)
        self.assertNotIn("SHC-VRF-STATIC\n", out.split("route-map SHC-VRF-STATIC")[0])

    def test_password(self):
        real, (egp, att, vrfs, svc) = service_setup()
        svc["bgw-1"]["circuits"][0]["authentication"] = "md5"
        v = template_vars(real, egp, att, vrfs, svc, "bgw-1", True)
        v["hf_circuit_passwords"] = {"platform/hub-a": "dummy"}
        out = T.render(T.RT, v)
        self.assertIn(" neighbor %s password dummy\n" % svc["bgw-1"]["circuits"][0]["ce"], out)


def blade():
    """The fake blade of test_shc_fabric_apply with the egress parent these tests name (a VLAN device of the trunk)."""
    st = A.blade()
    k = A.F.Kernel(st)
    k.add_link("", "bond1.100", "vlan", mtu=9000, address="00:02:c9:2f:3a:c0", parent="bond1", vlan=100)["up"] = True
    k.refresh6("", "bond1.100")
    return st


def service_conf(real, egp, att, svc, node="bgw-1"):
    return root_json(real, egp, att, svc, node, True)


class ServiceApply(A.ApplyCase):
    def setUp(self):
        super().setUp()
        self.h.set_state(blade())
        self.real, (self.egp, self.att, self.vrfs, self.svc) = service_setup()

    def conf(self, **over):
        c = service_conf(self.real, self.egp, self.att, self.svc)
        c.update(over)
        return c

    def test_up_twice_and_objects(self):
        h = self.h
        h.conf("root", self.conf())
        out, log = h.ok(self, "root")
        self.keep("service-up", log, out)
        st = h.get_state()["ns"][""]
        L = st["links"]
        self.assertEqual(L["service"]["kind"], "vrf")
        self.assertEqual(L["bond1.3099"]["master"], "service")
        self.assertEqual((L["se10002"]["master"], L["so10002"]["master"]), ("e10002", "service"))
        self.assertEqual(L["e10002"]["kind"], "vrf")
        self.assertIn("ip shc_service", st["nft"])
        # no service link for networks without clusters
        self.assertNotIn("se10000", L)
        self.assertNotIn("se10001", L)
        # the table before any service device, the VRF before its circuit and links
        last = max(i for i, l in enumerate(log) if l == "W [root] modprobe 8021q")
        load = A.index(log, r"^W \[root\] nft -f -$", last)
        self.assertLess(load, A.index(log, r"^W \[root\] ip link add service type vrf table 9001$"))
        self.assertLess(A.index(log, r"ip link add service type vrf"), A.index(log, r"ip link add link bond1 name bond1.3099"))
        # IPv4 only, strict reverse path filtering on the circuit
        self.assertEqual(h.sysctl("", "net/ipv6/conf/bond1.3099/disable_ipv6"), "1")
        self.assertEqual(h.sysctl("", "net/ipv4/conf/bond1.3099/rp_filter"), "1")
        self.assertEqual(h.sysctl("", "net/ipv6/conf/se10002/disable_ipv6"), "1")
        self.assertEqual(h.sysctl("", "net/ipv6/conf/so10002/disable_ipv6"), "1")
        # the input filter: the service VRF and the network's VRF in the list, the platform circuit a leg
        rin = st["nft"]["ip shc_root_in"]
        self.assertIn('iifname { "e10000", "e10001", "e10002", "egress", "service" } meta sdifname { "xe10000", '
                      '"bond1.3000", "bond1.3099" } accept', rin)
        # the IPv4 ICMP source on the service VRF too
        self.assertIn("192.0.0.8", [a["local"] for a in L["service"]["addrs"]])
        self.assertTrue(os.path.exists(os.path.join(h.etc, "root.service")))
        out, log = h.ok(self, "root")
        self.assertTrue(out.strip().endswith("changed=no"), out)
        # the snapshot names them
        out, _ = h.ok(self, "root", "snapshot")
        self.assertIn("table ip shc_service", out)
        self.assertRegex(out, r"so10002\s+UP\s+169\.254\.128\.4/31")

    def test_customer_circuits_untouched(self):
        h = self.h
        h.conf("root", self.conf())
        h.ok(self, "root")
        st = h.get_state()["ns"][""]
        self.assertEqual(st["links"]["bond1.3000"]["master"], "e10001")
        self.assertNotIn("bond1.3099", st["nft"]["ip shc_attach"])
        self.assertNotIn("so10002", st["nft"]["ip shc_attach"])

    def test_link_and_service_removed(self):
        h = self.h
        h.conf("root", self.conf())
        h.ok(self, "root")
        c = self.conf()
        c["service"] = dict(c["service"], networks=[])
        h.conf("root", c)
        out, log = h.ok(self, "root")
        L = h.get_state()["ns"][""]["links"]
        self.assertNotIn("se10002", L)
        self.assertNotIn("so10002", L)
        self.assertNotIn("e10002", L)  # the network's VRF had nothing else
        self.assertIn("service", L)
        c = self.conf()
        c.pop("service")
        h.conf("root", c)
        out, log = h.ok(self, "root")
        self.keep("service-removed", log, out)
        st = h.get_state()["ns"][""]
        self.assertFalse([d for d in st["links"] if d in ("service", "bond1.3099") or re.match(r"s[eo]\d+$", d)])
        self.assertNotIn("ip shc_service", st["nft"])
        self.assertFalse(os.path.exists(os.path.join(h.etc, "root.service")))
        self.assertIn("bond1.3000", st["links"])
        out, log = h.ok(self, "root")
        self.assertTrue(out.strip().endswith("changed=no"), out)
        self.assertEqual([w for w in A.writes(log) if re.search(r"service|s[eo]\d+|3099", w)], [])

    def test_down(self):
        h = self.h
        h.conf("root", self.conf())
        h.ok(self, "root")
        h.ok(self, "root", "down")
        st = h.get_state()["ns"][""]
        self.assertNotIn("service", st["links"])
        self.assertNotIn("ip shc_service", st["nft"])

    def test_srv6(self):
        real, (egp, att, vrfs, svc) = service_setup(core=CORE_SRV6)
        c = service_conf(real, egp, att, svc)
        c.update(A.ROOT6)
        c["loopback6"] = "2001:db8:4005:ffff::1/128"
        self.h.conf("root", c)
        out, log = self.h.ok(self, "root")
        # the network VRF without reverse path filtering (decapsulation), the service VRF and its devices without a
        # routing header
        self.assertEqual(self.h.sysctl("", "net/ipv4/conf/e10002/rp_filter"), "0")
        for d in ("service", "se10002", "so10002", "bond1.3099"):
            self.assertNotEqual(self.h.sysctl("", f"net/ipv6/conf/{d}/seg6_enabled"), "1", d)
        out, _ = self.h.ok(self, "root")
        self.assertTrue(out.strip().endswith("changed=no"), out)


class StopHarness(A.Harness):
    """The apply stopped after the k-th write of a run, two ways: kill (the script and everything it started are killed
    right there: the job's time limit, a node that goes down) and refuse (every later call fails, the script goes on
    as its error handling says: a failed command under set -e, a step that fails, a bearer that never comes; the
    calls that only read still answer)."""
    def __init__(self, tmp):
        super().__init__(tmp)
        for t in A.TOOLS:
            with open(os.path.join(self.bin, t), "w") as f:
                f.write('#!/bin/sh\nif [ -n "${STOP_AFTER:-}" ] && [ "$(grep -c "^W " "$FAKE_LOG")" -ge "$STOP_AFTER" ]; then\n'
                        '  [ "$STOP_MODE" = kill ] && kill -KILL 0\n  FAKE_FAIL="$WRITES"; export FAKE_FAIL\nfi\n'
                        'exec "%s" "%s" %s "$@"\n' % (sys.executable, A.FAKE, t))

    def run_stopped(self, instance, k, mode):
        with open(self.log) as f:
            done = sum(1 for l in f if l.startswith("W "))
        env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ.get("PATH", ""), FAKE_STATE=self.state,
                   FAKE_LOG=self.log, STOP_AFTER=str(done + k), STOP_MODE=mode,
                   WRITES=r"\] (ip( -[0-9a-z]+)* (link|addr|route|rule|neigh|netns) (add|del|set|replace|flush|change)( |$)|"
                          r"sysctl .*-q?w|nft (-f|delete|add|flush)|modprobe|wg |udevadm)")
        for v in ("FAKE_NS", "FAKE_FAIL"):
            env.pop(v, None)
        p = subprocess.run(["bash", self.scripts["new"], instance, "up"], env=env, capture_output=True, text=True,
                           timeout=300, start_new_session=True)
        return p.returncode


def unfiltered(st, vrf="service"):
    """What of the service VRF's links would pass unfiltered in this state: a service link so<t> that exists while no
    drop of table ip shc_service covers it (by name or by the wildcard), or that is up outside the service VRF; a device
    in the service VRF that the table's last drop does not name."""
    ns = st["ns"][""]
    table = ns["nft"].get("ip shc_service", "")
    lines = table.splitlines()

    def drop(comment):
        return next((l for l in lines if f'counter drop comment "{comment}' in l), "")
    between, last = drop("no network reaches another"), drop("nothing else through the service VRF")
    bad = []
    for d, l in sorted(ns["links"].items()):
        if re.fullmatch(r"so\d+", d):
            for what, line in (("between networks", between), ("last", last)):
                if '"so*"' not in line and f'"{d}"' not in line:
                    bad.append(f"{d}: no {what} drop covers it")
            if l.get("up") and l.get("master") != vrf:
                bad.append(f"{d} is up outside the service VRF")
        elif l.get("master") == vrf and l.get("kind") == "vlan" and f'"{d}"' not in last:
            bad.append(f"{d} (in the service VRF): the last drop does not name it")
    return bad


class ServiceInterrupted(unittest.TestCase):
    """The service section of the node script stopped at every step boundary (StopHarness): no state it leaves has a
    service link without its filter. Up from a fresh node, a network leaving (the review's case: its link was pruned
    only after the new table without its name was loaded), and the whole service leaving; then a complete run
    repairs it."""
    def setUp(self):
        if not __import__("shutil").which("jq"):
            self.skipTest("jq not installed")
        nets = [hn("lab", ["192.0.2.0/24"], internet=False, rows=["192.0.2.15/32"]),
                hn("lab2", ["192.0.2.0/24"], internet=False, rows=["192.0.2.99/32"])]
        led = copy.deepcopy(LEDGER)
        led["vpnTarget"]["HybridNetwork/acme/lab2"] = 10003
        led["egress"]["HybridNetwork/acme/lab2"] = 3
        led["servicePrefix"] = {"HybridNetwork/acme/lab/192.0.2.15/32": 1, "HybridNetwork/acme/lab2/192.0.2.99/32": 2}
        self.real = realization(CORE, service=True)
        egp, att, vrfs, svc = pipeline(F, self.real, True, nets=nets, circuits=[platform()], ledger=led)
        self.both = service_conf(self.real, egp, att, svc)
        self.one = copy.deepcopy(self.both)
        self.one["service"]["networks"] = [w for w in self.one["service"]["networks"] if w["vrf"] == "e10002"]
        self.none = copy.deepcopy(self.both)
        self.none.pop("service")

    def harness(self):
        tmp = tempfile.mkdtemp(prefix="svc-stop-")
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        return StopHarness(tmp)

    def walk(self, start_conf, conf):
        h = self.harness()
        h.set_state(blade())
        if start_conf is not None:
            h.conf("root", start_conf)
            rc, out, err, _ = h.run("root")
            self.assertEqual(rc, 0, err)
        before = h.get_state()
        before_files = {f: open(os.path.join(h.etc, f)).read() for f in os.listdir(h.etc)}
        h.conf("root", conf)
        rc, out, err, log = h.run("root")
        self.assertEqual(rc, 0, err)
        final = h.get_state()
        self.assertEqual(unfiltered(final), [])
        w = A.writes(log)
        # the service section: from the first write that touches it (service_up begins with modprobe vrf, 8021q) to
        # the end of the run (the root VRFs' prune removes a leaving network's VRF with its link)
        first = min(i for i, l in enumerate(w) if re.search(r"modprobe 8021q|shc_service|service|s[eo]\d+|3099", l))
        first = max(first - 1, 0)
        self.assertGreater(len(w) - first, 3)

        def one(job):
            k, mode = job
            hk = self.harness()
            hk.set_state(before)
            for f, text in before_files.items():
                with open(os.path.join(hk.etc, f), "w") as fh:
                    fh.write(text)
            hk.conf("root", conf)
            hk.run_stopped("root", k, mode)
            bad = unfiltered(hk.get_state())
            rc, _, err, _ = hk.run("root")
            after = hk.get_state()
            return k, mode, bad, (rc, unfiltered(after), "ip shc_service" in after["ns"][""]["nft"],
                                  sorted(d for d in after["ns"][""]["links"] if re.fullmatch(r"s[eo]\d+", d)))
        want_links = sorted(d for d in final["ns"][""]["links"] if re.fullmatch(r"s[eo]\d+", d))
        from concurrent.futures import ThreadPoolExecutor
        jobs = [(k, m) for k in range(first, len(w)) for m in ("kill", "refuse")]
        with ThreadPoolExecutor(max_workers=16) as ex:
            results = list(ex.map(one, jobs))
        for k, mode, bad, repaired in results:
            with self.subTest(stopped_after=w[k], write=k, mode=mode):
                self.assertEqual(bad, [])
                self.assertEqual(repaired[:2], (0, []))
                self.assertEqual(repaired[3], want_links)
                if mode == "kill" or "service" in conf:
                    # (refused: a run that went on past a refused table removal may leave the table, without links)
                    self.assertEqual(repaired[2], "service" in conf)

    def test_fresh_node(self):
        self.walk(None, self.both)

    def test_a_network_leaves(self):
        self.walk(self.both, self.one)

    def test_the_service_leaves(self):
        self.walk(self.both, self.none)


PROBE = r'''set -e
mount -t tmpfs tmpfs /run; mkdir -p /run/netns
ip netns add hub; ip netns add cl; ip netns add cl2
H="ip netns exec hub"; C="ip netns exec cl"; C2="ip netns exec cl2"
for X in "" "$H" "$C" "$C2"; do $X ip link set lo up; done
sysctl -qw net.ipv4.ip_forward=1 net.ipv4.conf.all.rp_filter=0 net.ipv4.conf.default.rp_filter=0
ip link add service type vrf table 9001; ip link set service up
ip link add e10002 type vrf table 110002; ip link set e10002 up
ip link add e10003 type vrf table 110003; ip link set e10003 up
# the platform circuit (to the hub's router) in the service VRF
ip link add bond1.3099 type veth peer name hubc; ip link set hubc netns hub
ip link set bond1.3099 master service up; ip addr add 10.255.128.9/29 dev bond1.3099
$H ip addr add 10.255.128.11/29 dev hubc; $H ip link set hubc up
$H ip addr add 198.51.100.5/32 dev lo; $H ip addr add 198.51.100.10/32 dev lo
$H ip route add default via 10.255.128.9
# two networks' service links, and each network's "core" (the cluster)
for t in 10002:4:cl:192.0.2.15 10003:6:cl2:192.0.2.99; do
  IFS=: read -r n o ns cip <<<"$t"
  ip link add se$n type veth peer name so$n
  ip link set se$n master e$n up; ip link set so$n master service up
  ip addr add 169.254.128.$((o + 1))/31 dev se$n; ip addr add 169.254.128.$o/31 dev so$n
  ip link add core$n type veth peer name clc; ip link set clc netns $ns
  ip link set core$n master e$n up; ip addr add 10.66.$o.1/30 dev core$n
  ip netns exec $ns ip addr add 10.66.$o.2/30 dev clc; ip netns exec $ns ip link set clc up
  ip netns exec $ns ip addr add $cip/32 dev lo; ip netns exec $ns ip route add default via 10.66.$o.1
  ip route add $cip/32 via 10.66.$o.2 dev core$n table 1100${n#100}
  ip route add 198.51.100.0/24 via 169.254.128.$o dev se$n table 1100${n#100}
  ip route add $cip/32 via 169.254.128.$((o + 1)) dev so$n table 9001
done
# routes between the two networks through the service VRF, both ways (what the filter must stop)
ip route add 192.0.2.99/32 via 169.254.128.4 dev se10002 table 110002
ip route add 192.0.2.15/32 via 169.254.128.6 dev se10003 table 110003
ip route add 198.51.100.0/24 via 10.255.128.11 dev bond1.3099 table 9001
sleep 1
cat > /tmp/svc-l.py <<'PY'
import socket, sys, time
ls = []
for a in sys.argv[1:]:
    ip, port = a.rsplit(":", 1)
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind((ip, int(port))); s.listen(8)
    ls.append(s)
time.sleep(40)
PY
cat > /tmp/svc-c.py <<'PY'
import socket, sys
src, dst, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
s = socket.socket(); s.settimeout(1.5)
try:
    s.bind((src, 0)); s.connect((dst, port)); print("connected")
except socket.timeout: print("timeout")
except ConnectionRefusedError: print("reset")
except OSError as e: print("error", e)
PY
cat > /tmp/svc-raw.py <<'PY'
import socket, struct, sys
def csum(b):
    if len(b) % 2: b += b"\0"
    s = sum(struct.unpack("!%dH" % (len(b) // 2), b)); s = (s >> 16) + (s & 0xffff); s += s >> 16
    return ~s & 0xffff
src, dst, sport, dport, flags = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5], 16)
tcp = struct.pack("!HHIIBBHHH", sport, dport, 1000, 2000, 5 << 4, flags, 65535, 0, 0)
ph = socket.inet_aton(src) + socket.inet_aton(dst) + struct.pack("!BBH", 0, 6, len(tcp))
tcp = tcp[:16] + struct.pack("!H", csum(ph + tcp)) + tcp[18:]
ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(tcp), 1, 0, 64, 6, 0, socket.inet_aton(src), socket.inet_aton(dst))
ip = ip[:10] + struct.pack("!H", csum(ip)) + ip[12:]
s = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_RAW); s.sendto(ip + tcp, (dst, 0))
PY
cat > /tmp/svc-cap.py <<'PY'
import socket, sys, time
s = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_TCP); s.settimeout(0.3)
seen, end = [], time.time() + float(sys.argv[1])
while time.time() < end:
    try:
        d = s.recv(2048)
    except socket.timeout:
        continue
    sport, dport, flags = int.from_bytes(d[20:22], "big"), int.from_bytes(d[22:24], "big"), d[33]
    seen.append("%s:%d>%d %02x" % (socket.inet_ntoa(d[12:16]), sport, dport, flags))
print(" ".join(seen))
PY
probe() {
  $C python3 /tmp/svc-l.py 192.0.2.15:6443 192.0.2.15:22 & $C2 python3 /tmp/svc-l.py 192.0.2.99:6443 &
  $H python3 /tmp/svc-l.py 198.51.100.10:6443 198.51.100.5:22 198.51.100.10:22 & sleep 1
  echo "hub-api: $($H python3 /tmp/svc-c.py 198.51.100.5 192.0.2.15 6443)"
  echo "hub-ssh: $($H python3 /tmp/svc-c.py 198.51.100.5 192.0.2.15 22)"
  echo "cl-endpoint: $($C python3 /tmp/svc-c.py 192.0.2.15 198.51.100.10 6443)"
  echo "cl-other: $($C python3 /tmp/svc-c.py 192.0.2.15 198.51.100.10 22)"
  echo "cl-cl2: $($C python3 /tmp/svc-c.py 192.0.2.15 192.0.2.99 6443)"
  echo "ping: $($H ping -c 1 -W 1 -I 198.51.100.5 192.0.2.15 >/dev/null 2>&1 && echo reply || echo none)"
  # a reply that crosses this node without its request (stateless), and a request forged with the service port
  $H python3 /tmp/svc-cap.py 2.5 > /tmp/svc-cap.out & sleep 0.5
  $C python3 /tmp/svc-raw.py 192.0.2.15 198.51.100.5 6443 40000 12
  $C python3 /tmp/svc-raw.py 192.0.2.15 198.51.100.5 6443 40001 02
  wait %4 2>/dev/null || true; sleep 2.5
  echo "raw: $(cat /tmp/svc-cap.out)"
  kill %1 %2 %3 2>/dev/null || true; wait 2>/dev/null || true
}
echo "--- before"; probe
nft -f "$1"
echo "--- after"; probe
nft -s list table ip shc_service | grep -c counter >/dev/null
nft list table ip shc_service | grep 'counter packets' | sed 's/^\s*/rule: /'
'''


class RealKernel(A.ApplyCase):
    def test_probes(self):
        import test_icmp_source_announce as G
        if not G.can_probe():
            self.skipTest("no unprivileged user and network namespace with VRF devices, nft and ip here")
        probe = subprocess.run(["unshare", "-rnm", "sh", "-c", "mount -t tmpfs tmpfs /run"], capture_output=True)
        if probe.returncode != 0:
            self.skipTest("no unprivileged mount namespace here")
        nets = [hn("lab", ["192.0.2.0/24"], internet=False, rows=["192.0.2.15/32"]),
                hn("lab2", ["192.0.2.0/24"], internet=False, rows=["192.0.2.99/32"])]
        led = copy.deepcopy(LEDGER)
        led["vpnTarget"]["HybridNetwork/acme/lab2"] = 10003
        led["egress"]["HybridNetwork/acme/lab2"] = 3
        led["servicePrefix"] = {"HybridNetwork/acme/lab/192.0.2.15/32": 1, "HybridNetwork/acme/lab2/192.0.2.99/32": 2}
        real = realization(CORE, service=True)
        egp, att, vrfs, svc = pipeline(F, real, True, nets=nets, circuits=[platform()], ledger=led)
        self.h.set_state(blade())
        self.h.conf("root", service_conf(real, egp, att, svc))
        self.h.ok(self, "root")
        f = os.path.join(self.tmp, "svc.nft")
        with open(f, "w") as fh:
            fh.write(self.h.get_state()["ns"][""]["nft"]["ip shc_service"])
        script = os.path.join(self.tmp, "probe.sh")
        with open(script, "w") as fh:
            fh.write(PROBE)
        p = subprocess.run(["unshare", "-rnm", "bash", script, f], capture_output=True, text=True, timeout=180)
        self.assertEqual(p.returncode, 0, p.stdout[-1500:] + p.stderr[-2000:])
        if self.evidence:
            with open(os.path.join(self.evidence, "service-real-kernel-probes.txt"), "w") as fh:
                fh.write(p.stdout)
        before, after = p.stdout.split("--- after")
        res = {s: dict(re.findall(r"^([a-z0-9-]+): (.*)$", t, re.M)) for s, t in (("before", before), ("after", after))}
        self.assertEqual({k: res["before"][k] for k in ("hub-api", "hub-ssh", "cl-endpoint", "cl-other", "cl-cl2", "ping")},
                         {"hub-api": "connected", "hub-ssh": "connected", "cl-endpoint": "connected",
                          "cl-other": "connected", "cl-cl2": "connected", "ping": "reply"})
        self.assertEqual({k: res["after"][k] for k in ("hub-api", "hub-ssh", "cl-endpoint", "cl-other", "cl-cl2", "ping")},
                         {"hub-api": "connected", "hub-ssh": "timeout", "cl-endpoint": "connected",
                          "cl-other": "timeout", "cl-cl2": "timeout", "ping": "reply"})
        # the SYN-ACK without its SYN on this node passes (stateless), the bare SYN from the service port does not
        self.assertIn("192.0.2.15:6443>40000 12", res["after"]["raw"])
        self.assertNotIn(">40001", res["after"]["raw"])
        self.assertIn("192.0.2.15:6443>40001 02", res["before"]["raw"])
        counts = dict((c, int(n)) for n, c in re.findall(r'counter packets (\d+) bytes \d+ drop comment "([^"]+)"', p.stdout))
        self.assertGreaterEqual(counts.get("no network reaches another through the service VRF", 0), 1, p.stdout)
        self.assertGreaterEqual(counts.get("nothing else through the service VRF", 0), 2, p.stdout)


if __name__ == "__main__":
    unittest.main()
