"""IPv6 per family in the network, placement and site jobs (design/IPV6-PLAN.md §2.1-§2.4, §11.4; OD63, OD65, OD66,
OD68, OD69, OD73): the mode derivation, the refusals and the status shapes. The task files run in ansible-playbook
(skipped when it is absent); the wiring checks always run."""
import copy
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, write_out  # noqa: E402

ROLES = os.path.join(ROOT, "eda", "rulebooks", "roles")
HN = os.path.join(ROLES, "hybridnetwork_provision", "tasks")
NP = os.path.join(ROLES, "networkplacement_provision", "tasks")
CI = os.path.join(ROLES, "cloudinfrastructure_provision")
COMMON = os.path.join(ROOT, "eda", "common")
REAL = load(os.path.join(COMMON, "vars", "fabric_realization.yml"))["fabric_realization_defaults"]


def merged(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merged(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def task_file(*parts):
    return load(os.path.join(*parts))


# ---- network: the mode from the fields ---------------------------------------------------------------------------

@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class NetworkModeTest(unittest.TestCase):
    def mode(self, spec):
        tasks = task_file(HN, "ipv6_mode.yml") + [write_out("{'mode': hn_mode6, 'v4': hn_v4, 't6': hn_tenant6, 'p6': hn_platform6}")]
        return run_play(tasks, {"fab_spec": spec})

    def test_modes(self):
        # the stored baseline network: IPv4 plus a ULA = Tenant (§2.9, gate 1)
        core = self.mode({"prefixes": ["10.60.0.0/16", "fd12:3456:789a::/48"]})
        self.assertEqual(core["mode"], "Tenant")
        self.assertEqual(core["v4"], ["10.60.0.0/16"])
        self.assertEqual(core["t6"], ["fd12:3456:789a::/48"])
        self.assertEqual(self.mode({"prefixes": ["10.60.0.0/16"]})["mode"], "None")
        self.assertEqual(self.mode({"prefixes": ["2001:db8:abcd::/48"]})["mode"], "Tenant")
        p = self.mode({"prefixes": ["10.60.0.0/16"], "platformPrefixes": [{"family": 6, "prefixLength": 56}]})
        self.assertEqual(p["mode"], "Platform")
        self.assertEqual(p["p6"], {"family": 6, "prefixLength": 56})
        self.assertEqual(self.mode({"platformPrefixes": [{"family": 6}]})["mode"], "Platform")
        # spec.prefixes may be absent now
        self.assertEqual(self.mode({"platformPrefixes": [{"family": 6}]})["v4"], [])

    def realized(self, held):
        tasks = task_file(HN, "ipv6_realized.yml") + [write_out("hn_mode6_realized")]
        return run_play(tasks, {"hn_held": held})

    def test_realized_mode(self):
        self.assertEqual(self.realized({"exists": False}), "")
        self.assertEqual(self.realized({"exists": True, "prefixes": ["10.60.0.0/16"], "mode6": "Platform"}), "Platform")
        # spaces stored before modes existed: derived from what is stored
        self.assertEqual(self.realized({"exists": True, "prefixes": ["10.60.0.0/16", "fd12:3456:789a::/48"]}), "Tenant")
        self.assertEqual(self.realized({"exists": True, "prefixes": ["10.60.0.0/16", "2001:db8:4000::/56"],
                                        "platform": ["2001:db8:4000::/56"]}), "Platform")
        self.assertEqual(self.realized({"exists": True, "prefixes": ["10.60.0.0/16"]}), "None")


# ---- network: IPv6ModeInUse, the IPv6 status, the per-family egress ----------------------------------------------

@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class NetworkRefusalTest(unittest.TestCase):
    def refuse(self, realized, mode, holders):
        t = copy.deepcopy(by_name(task_file(HN, "main.yml"), "Refuse a change of the IPv6 mode while the network has placements"))
        tasks = [{"ansible.builtin.set_fact": {"fab_fail_reason": ""}}, t, write_out("fab_fail_reason")]
        return run_play(tasks, {"fab_name": "n1", "hn_mode6_realized": realized, "hn_mode6": mode, "hn_holders": holders})

    def test_mode_change_with_placements_refused(self):
        self.assertEqual(self.refuse("Tenant", "Platform", ["NetworkPlacement/entity-a/p1"]), "IPv6ModeInUse")
        self.assertEqual(self.refuse("None", "Tenant", ["NetworkPlacement/entity-a/p1"]), "IPv6ModeInUse")
        self.assertEqual(self.refuse("Platform", "None", ["NetworkPlacement/entity-a/p1"]), "IPv6ModeInUse")

    def test_mode_change_allowed(self):
        self.assertEqual(self.refuse("Tenant", "Platform", []), "")          # no placements: a re-create
        self.assertEqual(self.refuse("Tenant", "Tenant", ["NetworkPlacement/entity-a/p1"]), "")
        self.assertEqual(self.refuse("", "Platform", ["NetworkPlacement/entity-a/p1"]), "")  # nothing realized yet

    def ipv6_status(self, block, tenant6, platform6):
        t = copy.deepcopy(by_name(task_file(HN, "main.yml"), "Keep the address space"))
        tasks = [t, write_out("hn_ipv6_status")]
        return run_play(tasks, {"fab_ipam_result": {"addressSpace": [], "changed": False}, "hn_block": block,
                                "hn_tenant6": tenant6, "hn_platform6": platform6, "hn_mode6": "Platform" if block else
                                ("Tenant" if tenant6 else "None")})

    def test_ipv6_status_shapes(self):
        st = self.ipv6_status("2001:db8:4000::/56", [], {"family": 6})
        self.assertEqual(st, {"mode": "Platform", "prefixes": ["2001:db8:4000::/56"],
                              "block": {"prefix": "2001:db8:4000::/56", "prefixLength": 56, "source": "Allocated"},
                              "holdDownUntil": None})
        st = self.ipv6_status("2001:db8:4100::/56", [], {"family": 6, "prefix": "2001:db8:4100::/56"})
        self.assertEqual(st["block"]["source"], "Pinned")
        st = self.ipv6_status("", ["fd12:3456:789a::/48"], {})
        self.assertEqual(st, {"mode": "Tenant", "prefixes": ["fd12:3456:789a::/48"], "block": None, "holdDownUntil": None})
        self.assertEqual(self.ipv6_status("", [], {})["mode"], "None")


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class EgressStatusTest(unittest.TestCase):
    def egress(self, v4, mode, want, offered=True, carried=None):
        tasks = task_file(HN, "egress_status.yml") + [write_out("{'egress': hn_egress, 'note': hn_egress_note}")]
        return run_play(tasks, {"hn_v4": v4, "hn_mode6": mode, "hn_egress_want": want, "hn_egress_offered": offered,
                                "hn_egress_carried": carried or {}, "hn_fabric_name": "fab"})

    def test_platform_dual_stack_reads_not_offered(self):
        out = self.egress(["10.60.0.0/16"], "Platform", True, carried={"network": "entity-a/n1", "sourceAddresses": ["192.0.2.10"]})
        eg = out["egress"]
        self.assertEqual(eg["internet"], "ready")                     # the IPv4 meaning, unchanged
        self.assertEqual(eg["sourceAddresses"], ["192.0.2.10"])
        self.assertEqual(eg["families"][0], {"family": 4, "state": "ready", "mode": "SourceNAT"})
        self.assertEqual(eg["families"][1]["family"], 6)
        self.assertEqual(eg["families"][1]["state"], "unavailable")
        self.assertEqual(eg["families"][1]["reason"], "NotOffered")
        self.assertIsNone(eg["ipv4Translation"])

    def test_tenant_reads_address_choice(self):
        eg = self.egress(["10.60.0.0/16"], "Tenant", True)["egress"]
        self.assertEqual(eg["internet"], "pending")
        self.assertEqual(eg["families"][1]["reason"], "AddressChoice")
        self.assertEqual(eg["families"][1]["message"], "IPv6 internet: not offered for tenant-provided prefixes")

    def test_none_has_no_ipv6_row(self):
        eg = self.egress(["10.60.0.0/16"], "None", True)["egress"]
        self.assertEqual([f["family"] for f in eg["families"]], [4])

    def test_off_writes_nothing(self):
        self.assertEqual(self.egress(["10.60.0.0/16"], "Platform", False)["egress"], {})

    def test_ipv6_only(self):
        eg = self.egress([], "Platform", True)["egress"]
        self.assertEqual(eg["internet"], "unavailable")
        self.assertEqual([f["family"] for f in eg["families"]], [6])
        self.assertIsNone(eg["sourceAddresses"])
        self.assertEqual(eg["ipv4Translation"]["reason"], "NotOffered")
        self.assertEqual(self.egress([], "Tenant", True)["egress"]["ipv4Translation"]["reason"], "AddressChoice")

    def test_ipv4_egress_not_offered(self):
        eg = self.egress(["10.60.0.0/16"], "None", True, offered=False)["egress"]
        self.assertEqual(eg["internet"], "unavailable")
        self.assertEqual(eg["families"][0]["reason"], "NotOffered")

    def test_removed_ipv4_still_carried_reads_removing(self):
        eg = self.egress([], "Tenant", False, carried={"network": "entity-a/n1"})["egress"]
        self.assertEqual(eg["internet"], "removing")


# ---- placement: families, sources, refusals ----------------------------------------------------------------------

V3_NET_P = {"spec": {"prefixes": ["10.60.0.0/16"], "platformPrefixes": [{"family": 6}]},
            "status": {"ipv6": {"mode": "Platform"}}}
V3_NET_T = {"spec": {"prefixes": ["10.60.0.0/16", "fd12:3456:789a::/48"]}, "status": {}}
V3_NET_N = {"spec": {"prefixes": ["10.60.0.0/16"]}, "status": {"ipv6": {"mode": "None"}}}
SITE_OFF = {"api": True, "evpn": True, "ipv6": False, "ipv6Only": False, "ipv6NetworkSource": False,
            "ipv6ProviderSource": False, "ipv6Translation": False}
SITE_ON = {"api": True, "evpn": True, "ipv6": True, "ipv6Only": True, "ipv6NetworkSource": True,
           "ipv6ProviderSource": True, "ipv6Translation": False}


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class PlacementFamiliesTest(unittest.TestCase):
    def resolve(self, spec, kind="CloudVirt", site="iaas1", network=V3_NET_P, caps=None, real=None):
        tasks = task_file(NP, "families.yml") + [write_out(
            "{'want': np_want, 'refuse': np_refuse, 'source': np_ipv6_source, 'mode': np_ipv6_mode, 'families': np_families}")]
        return run_play(tasks, {"fab_spec": spec, "fab_name": "p1", "np_backend_kind": kind, "np_site": site,
                                "np_network": network, "ci_capabilities": SITE_OFF if caps is None else caps,
                                "fab_real": merged(REAL, real)})

    def test_ipv4_only_placement_unchanged(self):
        out = self.resolve({"ipv4": {"prefixLength": 24}})
        self.assertEqual(out["refuse"], {})
        self.assertEqual(out["want"], [{"prefixLength": 24, "family": 4}])        # only the families present (B5)

    def test_v3_every_ipv6_placement_refused_at_the_site(self):
        for kind, site in (("CloudVirt", "iaas1"), ("CloudOSO", "atuin"), ("CloudAWS", "aws-use2")):
            out = self.resolve({"ipv4": {"prefixLength": 24}, "ipv6": {"prefixLength": 64 if kind != "CloudAWS" else 60}},
                               kind=kind, site=site)
            self.assertEqual(out["refuse"]["reason"], "FamilyUnavailable", kind)
            self.assertEqual(out["refuse"]["msg"], f"IPv6 is not available on this environment's site ({site})")

    def test_switch_without_probe_still_refused(self):
        out = self.resolve({"ipv4": {"prefixLength": 24}, "ipv6": {"prefixLength": 64}},
                           real={"placement": {"ipFamiliesBySite": {"iaas1": [4, 6]}}})
        self.assertEqual(out["refuse"]["msg"], "IPv6 is not available on this environment's site (iaas1)")

    def test_mode_none_network(self):
        out = self.resolve({"ipv4": {"prefixLength": 24}, "ipv6": {"prefixLength": 64}}, network=V3_NET_N)
        self.assertEqual(out["refuse"], {"reason": "FamilyUnavailable", "msg": "the network has no IPv6 address space"})

    def test_mode_from_spec_when_status_has_none(self):
        out = self.resolve({"ipv6": {"prefixLength": 64}}, network={"spec": {"prefixes": ["10.60.0.0/16"]}})
        self.assertEqual(out["mode"], "None")
        self.assertEqual(out["refuse"]["msg"], "the network has no IPv6 address space")
        self.assertEqual(self.resolve({"ipv4": {"prefixLength": 24}}, network=V3_NET_T)["mode"], "Tenant")

    def test_gcp_rules_come_before_the_site(self):
        out = self.resolve({"ipv4": {"prefixLength": 24}, "ipv6": {}}, kind="CloudGCP", site="gcp-use4", network=V3_NET_T)
        self.assertEqual(out["refuse"]["msg"], "this site can't carry tenant-provided IPv6 prefixes")
        out = self.resolve({"ipv4": {"prefixLength": 24}, "ipv6": {"source": "Network", "prefixLength": 64}},
                           kind="CloudGCP", site="gcp-use4")
        self.assertEqual(out["refuse"]["msg"], "this site offers IPv6 only from the cloud provider; use source Provider")
        # the default source of GCP is Provider: then the site decides
        out = self.resolve({"ipv4": {"prefixLength": 24}, "ipv6": {}}, kind="CloudGCP", site="gcp-use4")
        self.assertEqual(out["source"], "Provider")
        self.assertEqual(out["refuse"]["msg"], "IPv6 is not available on this environment's site (gcp-use4)")

    def test_provider_on_tenant_mode_is_prefix_invalid(self):
        out = self.resolve({"ipv4": {"prefixLength": 24}, "ipv6": {"source": "Provider"}}, kind="CloudAWS",
                           site="aws-use2", network=V3_NET_T)
        self.assertEqual(out["refuse"], {"reason": "PrefixInvalid", "msg": "provider IPv6 needs a network with IPv6 from the platform"})

    def test_aws_defaults(self):
        on = {"placement": {"ipFamiliesBySite": {"aws-use2": [4, 6]}}}
        out = self.resolve({"ipv4": {"prefixLength": 24}, "ipv6": {}}, kind="CloudAWS", site="aws-use2", caps=SITE_ON, real=on)
        self.assertEqual(out["refuse"], {})
        self.assertEqual(out["source"], "Network")                                   # OD68
        self.assertEqual(out["want"][1], {"family": 6, "source": "Network", "prefixLength": 60})
        # mode Tenant on AWS is fabric-carried (Network), never the default source
        out = self.resolve({"ipv4": {"prefixLength": 24}, "ipv6": {"prefixLength": 56}}, kind="CloudAWS", site="aws-use2",
                           caps=SITE_ON, real=on, network=V3_NET_T)
        self.assertEqual(out["source"], "Network")
        out = self.resolve({"ipv4": {"prefixLength": 24}, "ipv6": {"source": "Provider"}}, kind="CloudAWS",
                           site="aws-use2", caps=SITE_ON, real=on)
        self.assertEqual(out["want"][1], {"family": 6, "source": "Provider"})

    def test_switched_site(self):
        on = {"placement": {"ipFamiliesBySite": {"iaas1": [4, 6]}}}
        out = self.resolve({"ipv4": {"prefixLength": 24}, "ipv6": {"prefixLength": 64}}, caps=SITE_ON, real=on)
        self.assertEqual(out["refuse"], {})
        self.assertEqual(out["families"], [4, 6])
        self.assertEqual(out["want"], [{"prefixLength": 24, "family": 4},
                                       {"family": 6, "source": "Network", "prefixLength": 64}])
        # IPv6-only needs its own switch
        out = self.resolve({"ipv6": {"prefixLength": 64}}, caps=SITE_ON, real=on)
        self.assertEqual(out["refuse"]["msg"], "IPv6-only placements are not offered on this site (iaas1)")
        on["placement"]["ipv6OnlyBySite"] = {"iaas1": True}
        self.assertEqual(self.resolve({"ipv6": {"prefixLength": 64}}, caps=SITE_ON, real=on)["refuse"], {})
        # a source the site doesn't list
        on["placement"]["ipv6SourcesBySite"] = {"iaas1": []}
        out = self.resolve({"ipv6": {"prefixLength": 64}}, caps=SITE_ON, real=on)
        self.assertEqual(out["refuse"]["msg"], "IPv6 source Network is not offered on this site (iaas1)")

    def test_ipv4_switched_off_at_a_site(self):
        out = self.resolve({"ipv4": {"prefixLength": 24}}, real={"placement": {"ipFamiliesBySite": {"iaas1": [6]}}})
        self.assertEqual(out["refuse"]["reason"], "FamilyUnavailable")


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class PlacementSubnetsTest(unittest.TestCase):
    def subnets(self, result):
        # np_status is input and output: a fact, not an extra var (extra vars can't be overridden)
        tasks = ([{"ansible.builtin.set_fact": {"np_status": {"entity": "a"}}}] + task_file(NP, "subnets.yml")
                 + [write_out("{'status': np_status, 'v4': np_prefixes_v4}")])
        return run_play(tasks, {"fab_ipam_result": result})

    def test_rows_per_family(self):
        out = self.subnets({"prefixes": ["10.60.4.0/24", "2001:db8:4000:1::/64"]})
        self.assertEqual(out["status"]["prefixes"], ["10.60.4.0/24", "2001:db8:4000:1::/64"])
        self.assertEqual(out["status"]["subnets"], [
            {"family": 4, "prefix": "10.60.4.0/24", "source": "Network", "gateway": "10.60.4.1"},
            {"family": 6, "prefix": "2001:db8:4000:1::/64", "source": "Network"}])
        self.assertEqual(out["v4"], ["10.60.4.0/24"])

    def test_ipam_rows_used(self):
        out = self.subnets({"prefixes": ["10.60.8.0/29"], "subnets": [
            {"family": 6, "prefix": "2001:db8:1f16::/56", "source": "Provider"},
            {"family": 4, "prefix": "10.60.8.0/29", "source": "Network"}]})
        self.assertEqual(out["status"]["subnets"][0], {"family": 4, "prefix": "10.60.8.0/29", "source": "Network", "gateway": "10.60.8.1"})
        self.assertEqual(out["status"]["subnets"][1]["source"], "Provider")


# ---- site: the IPv6 capabilities ---------------------------------------------------------------------------------

@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class SiteCapabilitiesTest(unittest.TestCase):
    def caps(self, ctype, name="iaas1", cno=None, real=None, base=None):
        tasks = ([{"ansible.builtin.set_fact": {"ci_capabilities_out": "{{ _base_caps }}"}}]
                 + task_file(CI, "tasks", "ipv6_capabilities.yml") + [write_out("ci_capabilities_out")])
        v = {"role_path": CI, "ci_cr_type": ctype, "cloudinfrastructure_cr_name": name,
             "_base_caps": base or {"api": True, "evpn": True}}
        if cno is not None:
            v["_ci_cno"] = {"resources": [{"spec": {"clusterNetwork": [{"cidr": c} for c in cno]}}]}
        if real is not None:
            v["fabric_realization"] = real
        return run_play(tasks, v)

    KEYS = ("ipv6", "ipv6Only", "ipv6NetworkSource", "ipv6ProviderSource", "ipv6Translation")

    def test_v3_every_site_reports_false(self):
        # a dual-stack cluster (the probe says yes) still reports false while the switches are off
        out = self.caps("openshift", cno=["10.128.0.0/14", "fd01::/48"])
        self.assertTrue(all(out[k] is False for k in self.KEYS), out)
        self.assertTrue(out["evpn"])                                                # the probe's own keys stay
        for t, n in (("openstack", "atuin"), ("aws", "aws-use2"), ("gcp", "gcp-use4"), ("external", "x")):
            out = self.caps(t, name=n)
            self.assertTrue(all(out[k] is False for k in self.KEYS), (t, out))

    def test_switches_on(self):
        real = {"placement": {"ipFamiliesBySite": {"iaas1": [4, 6]}, "ipv6OnlyBySite": {"iaas1": True}}}
        out = self.caps("openshift", cno=["10.128.0.0/14", "fd01::/48"], real=real)
        self.assertEqual({k: out[k] for k in self.KEYS},
                         {"ipv6": True, "ipv6Only": True, "ipv6NetworkSource": True, "ipv6ProviderSource": False,
                          "ipv6Translation": False})
        # single-stack cluster: the probe says no
        out = self.caps("openshift", cno=["10.128.0.0/14"], real=real)
        self.assertFalse(out["ipv6"])
        out = self.caps("gcp", name="gcp-use4", real={"placement": {"ipFamiliesBySite": {"gcp-use4": [4, 6]}}})
        self.assertTrue(out["ipv6"] and out["ipv6ProviderSource"] and not out["ipv6NetworkSource"])


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class TenantSummaryTest(unittest.TestCase):
    def test_ipv6_values(self):
        tasks = task_file(COMMON, "tasks", "site_tenant_summary.yml") + [write_out("site_tenant_summaries")]
        site = {"metadata": {"name": "iaas1"}, "spec": {"type": "openshift"},
                "status": {"status": "ready", "capabilities": {"api": True, "evpn": True, "virtualization": True,
                                                               "ipv6": True, "ipv6Only": False, "ipv6NetworkSource": True}}}
        out = run_play(tasks, {"sts_sites": [site]})
        self.assertEqual(out[0]["capabilities"], ["Virtualization", "HybridNetworking", "IPv6", "IPv6NetworkSource"])


# ---- circuit: PeerAsnReserved ------------------------------------------------------------------------------------

@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class PeerAsnTest(unittest.TestCase):
    def refuse(self, asn, reserved):
        t = copy.deepcopy(by_name(task_file(ROLES, "attachmentcircuit_provision", "tasks", "main.yml"),
                                  "Refuse an AS number the platform keeps for itself"))
        tasks = [{"ansible.builtin.set_fact": {"fab_fail_reason": ""}}, t, write_out("fab_fail_reason")]
        return run_play(tasks, {"fab_name": "c1", "ac_bgp": {"peerAsn": asn},
                                "fab_real": merged(REAL, {"attachments": {"reservedAsns": reserved}})})

    def test_reserved(self):
        self.assertEqual(self.refuse(65011, [65011]), "PeerAsnReserved")
        self.assertEqual(self.refuse(65100, [65011]), "")
        self.assertEqual(self.refuse(65011, []), "")


# ---- check-network: exclusive ranges -----------------------------------------------------------------------------

class ExclusiveRangesTest(unittest.TestCase):
    def check(self, prefixes, exclusive):
        req = {"action": "check-network", "prefixes": prefixes, "reserved": ["10.255.0.0/24"], "exclusive": exclusive}
        p = subprocess.run([sys.executable, "-I", os.path.join(COMMON, "files", "shc_fabric.py")], input=json.dumps(req),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_overlap_either_direction(self):
        ex = ["2001:db8:4005::/48", "64:ff9b::/96"]
        self.assertEqual(len(self.check(["2001:db8:4005:4000::/56"], ex)["reserved"]), 1)   # inside
        self.assertEqual(len(self.check(["2001:db8::/32"], ex)["reserved"]), 1)              # around
        self.assertEqual(self.check(["2001:db8:4006::/48", "10.60.0.0/16"], ex)["errors"], [])  # beside: accepted
        self.assertEqual(self.check(["fd12:3456:789a::/48"], ex)["errors"], [])
        self.assertEqual(self.check(["10.60.0.0/16"], [])["errors"], [])

    def test_no_exclusive_key_is_the_old_behaviour(self):
        req = {"action": "check-network", "prefixes": ["10.255.0.0/25"], "reserved": ["10.255.0.0/24"]}
        p = subprocess.run([sys.executable, "-I", os.path.join(COMMON, "files", "shc_fabric.py")], input=json.dumps(req),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(len(json.loads(p.stdout)["reserved"]), 1)


# ---- wiring ------------------------------------------------------------------------------------------------------

class WiringTest(unittest.TestCase):
    def text(self, *parts):
        with open(os.path.join(*parts)) as f:
            return f.read()

    def test_network_job(self):
        t = self.text(HN, "main.yml")
        for s in ("ipv6_mode.yml", "ipv6_realized.yml", "platform_block.yml", "egress_status.yml", "IPv6ModeInUse",
                  "exclusive:", "ipam-pool-release", "mode6:", "holders:", "tenant:", "'ipv6': hn_ipv6_status"):
            self.assertIn(s, t)
        # the egress index is held for IPv4 only
        self.assertIn("hn_egress_want4 | bool or hn_platform_reach", t)
        b = self.text(HN, "platform_block.yml")
        for s in ("fabric_ipam_pools.yml", "ipam-pool-allocate", "WaitingForAddressSpace", "FamilyUnavailable",
                  "PrefixInvalid", "holdDownUntil"):
            self.assertIn(s, b)

    def test_teardown_releases_the_block(self):
        t = self.text(ROLES, "hybridnetwork_teardown", "tasks", "main.yml")
        self.assertLess(t.index("ipam-drop"), t.index("ipam-pool-release"))
        self.assertIn("wake_waiting_networks.yml", t)
        # the release uses the hold-down realization has now; a refused pool realization doesn't stop the delete
        self.assertLess(t.index("ipam-drop"), t.index("fabric_ipam_pools.yml"))
        self.assertLess(t.index("fabric_ipam_pools.yml"), t.index("ipam-pool-release"))
        self.assertIn("fab_ipam_pools_tolerant: true", t)

    def test_leaving_platform_ensures_before_the_release(self):
        t = self.text(HN, "main.yml")
        # (the give-back of a block taken for a refused space follows platform_block.yml, which ensured the pools)
        i = t.index("The fabric's platform address pools before a release")
        j = t.index("- name: Give a platform block back")
        self.assertLess(i, j)
        self.assertIn("fab_ipam_pools_tolerant: true", t[i:j])
        self.assertIn("action: ipam-pool-release", t[j:j + 300])

    def test_fabric_job_keeps_the_pools(self):
        t = self.text(ROLES, "hybridfabric_provision", "tasks", "main.yml")
        # after the ledger exists, before the status; never fails the fabric
        self.assertLess(t.index("action: ledger-ensure"), t.index("fabric_ipam_pools.yml"))
        i = t.index("fabric_ipam_pools.yml")
        self.assertIn("fab_ipam_pools_tolerant: true", t[i:i + 200])
        self.assertIn("'ipam': hf_ipam", t)
        self.assertIn("'pools': fab_ipam_pools_usage", t)
        self.assertLess(t.index("Report the fabric ready"), t.index("wake_waiting_networks.yml"))
        self.assertIn("when: hf_ipam_changed | bool", t)

    def test_placement_job(self):
        t = self.text(NP, "main.yml")
        self.assertIn("families.yml", t)
        self.assertIn("subnets.yml", t)
        self.assertLess(t.index("families.yml"), t.index("action: ipam-allocate"))   # refused before the IPAM
        self.assertNotIn("[fab_spec.ipv4 | combine({'family': 4})]\n", t)

    def test_site_job(self):
        self.assertIn("ipv6_capabilities.yml", self.text(CI, "tasks", "main.yml"))

    def test_prefixes0_readers_moved(self):
        for f in ("aws_clusterbuild_network.yml", "gcp_clusterbuild_network.yml"):
            t = self.text(ROLES, "platformopenshift_provision", "tasks", f)
            self.assertNotIn("status.prefixes[0]", t)
            self.assertIn("status.subnets", t)

    def test_realization_defaults_off(self):
        pl = REAL["placement"]
        self.assertEqual({k: v for k, v in pl["ipFamilies"].items()}, {"CloudVirt": [4], "CloudOSO": [4], "CloudAWS": [4], "CloudGCP": [4]})
        self.assertEqual(pl["ipFamiliesBySite"], {})
        self.assertEqual(pl["ipv6OnlyBySite"], {})
        self.assertEqual(REAL["network"]["exclusiveRanges"], [])
        self.assertEqual(REAL["attachments"]["reservedAsns"], [])
        self.assertFalse(REAL["egress"]["ipv6"]["enabled"])


if __name__ == "__main__":
    unittest.main()
