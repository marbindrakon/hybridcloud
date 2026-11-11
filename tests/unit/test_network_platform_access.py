"""HybridNetwork platform access (spec.platformAccess, status.platformAccess, condition PlatformReachable).

The network job (hybridnetwork_provision platform_access.yml with files/shc_platform_access.py) decides whether the
platform's management can reach the OpenShift clusters it builds on the network, checks each prefix such a cluster
asks for (PlatformOpenshift status.platformPrefixes) and records the accepted ones in the fabric's ledger (kind
servicePrefix, first come first served). The fabric realizes them (status.realization.service, a fake here). A network
that cannot join stays Ready: only the condition says why. The ranges the platform's management connects from
(infrastructure.hub) are exclusive for new prefixes; a network that predates the rule keeps working. The retired
platform reach annotation is removed. Documentation ranges and example names only."""
import copy
import importlib.util
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, walk, write_out  # noqa: E402

ROLE = os.path.join(ROOT, "eda", "rulebooks", "roles", "hybridnetwork_provision")
HN = os.path.join(ROLE, "tasks")
HND = os.path.join(ROOT, "eda", "rulebooks", "roles", "hybridnetwork_teardown", "tasks")
HELPER = os.path.join(ROLE, "files", "shc_platform_access.py")
CRD = os.path.join(ROOT, "operator", "config", "crd", "bases", "crd-hybridnetwork.yaml")

_spec = importlib.util.spec_from_file_location("shc_platform_access", HELPER)
PA = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(PA)

HUB = ["192.0.2.0/24"]
SERVICE = {"enabled": True, "ports": [6443, 443], "attachableRanges": ["10.0.0.0/8"]}
CIRCUITS = {"circuits": [{"circuit": "platform-ns/hub-a", "vlan": 3001}], "networks": [], "refused": []}
RESERVED = [{"prefix": "192.0.2.0/24", "purpose": "Networks the platform's management connects from", "rule": "Refused"},
            {"prefix": "198.51.100.0/24", "purpose": "Addresses inside the platform's network nodes", "rule": "SteppedAround"},
            {"prefix": "10.20.200.0/24", "purpose": "Addresses the site uses itself", "rule": "SteppedAround", "site": "site-a"}]


def req(**kw):
    r = {"action": "platform-access", "network": "tenant-a/net", "fabric": "fab", "platformAccess": True,
         "space": ["10.20.0.0/16"], "overlaps": [], "hub": HUB, "service": SERVICE, "serviceStatus": CIRCUITS,
         "reserved": RESERVED, "clusters": [], "ledger": {}}
    r.update(kw)
    return r


def cl(name, *prefixes, deleting=False):
    return {"name": name, "prefixes": list(prefixes), "deleting": deleting}


def run(**kw):
    return PA.platform_access(req(**kw))


class Eligibility(unittest.TestCase):
    def cond(self, out):
        return out["condition"]["status"], out["condition"]["reason"], out["state"]

    def test_attachable(self):
        out = run()
        self.assertEqual(self.cond(out), ("True", "Attachable", "attachable"))
        self.assertEqual(out["reason"], "")
        self.assertEqual(out["sources"], ["192.0.2.0/24"])
        self.assertEqual(out["ports"], [6443, 443])
        self.assertEqual((out["prefixes"], out["accepted"], out["allocate"], out["release"]), ([], [], [], []))
        self.assertFalse(out["wakeFabric"])

    def test_not_requested(self):
        out = run(platformAccess=False)
        self.assertEqual(self.cond(out), ("False", "NotRequested", "off"))
        self.assertEqual(out["reason"], "NotRequested")
        self.assertIn("spec.platformAccess", out["message"])

    def test_service_not_offered_is_the_default(self):
        out = run(service={}, serviceStatus={})
        self.assertEqual(self.cond(out), ("False", "ServiceNotOffered", "unavailable"))
        self.assertIn("platform team", out["message"])
        self.assertFalse(out["wakeFabric"])

    def test_circuit_not_deployed(self):
        out = run(serviceStatus={"circuits": []})
        self.assertEqual(self.cond(out), ("False", "CircuitNotDeployed", "unavailable"))

    def test_platform_reasons_come_before_the_network_ones(self):
        # with the service on and no circuit yet, every network reads CircuitNotDeployed, whatever its space
        self.assertEqual(run(serviceStatus={"circuits": []}, space=["2001:db8:5::/48"])["condition"]["reason"],
                         "CircuitNotDeployed")

    def test_no_ipv4(self):
        out = run(space=["2001:db8:5::/48"])
        self.assertEqual(self.cond(out), ("False", "NoIPv4", "unavailable"))
        self.assertIn("IPv4", out["message"])

    def test_infrastructure_overlap(self):
        ov = [{"prefix": "192.0.2.0/25", "range": "192.0.2.0/24", "purpose": "Networks the platform's management connects from"}]
        out = run(space=["10.20.0.0/16", "192.0.2.0/25"], overlaps=ov)
        self.assertEqual(self.cond(out), ("False", "InfrastructureOverlap", "unavailable"))
        self.assertIn("192.0.2.0/24", out["message"])
        self.assertIn("connects from", out["message"])

    def test_outside_attachable_ranges(self):
        out = run(space=["172.16.0.0/16"])
        self.assertEqual(self.cond(out), ("False", "OutsideAttachableRanges", "unavailable"))
        self.assertIn("10.0.0.0/8", out["message"])
        self.assertIn("managedClusterRanges", out["message"])

    def test_one_prefix_inside_is_enough(self):
        self.assertEqual(run(space=["172.16.0.0/16", "10.20.0.0/16"])["condition"]["reason"], "Attachable")

    def test_empty_attachable_ranges_means_any(self):
        self.assertEqual(run(space=["172.16.0.0/16"], service=dict(SERVICE, attachableRanges=[]))["condition"]["reason"],
                         "Attachable")


class Prefixes(unittest.TestCase):
    def test_a_new_prefix_waits_and_is_recorded(self):
        out = run(clusters=[cl("c1", "10.20.1.5/32", "10.20.1.6/32")])
        self.assertEqual(out["accepted"], ["10.20.1.5/32", "10.20.1.6/32"])
        self.assertEqual(out["allocate"], ["HybridNetwork/tenant-a/net/10.20.1.5/32", "HybridNetwork/tenant-a/net/10.20.1.6/32"])
        self.assertEqual([r["state"] for r in out["prefixes"]], ["pending", "pending"])
        self.assertEqual((out["condition"]["status"], out["condition"]["reason"]), ("False", "AttachPending"))
        self.assertEqual(out["state"], "attachable")
        self.assertTrue(out["wakeFabric"])

    def test_attached_when_the_fabric_realized_them(self):
        led = {"HybridNetwork/tenant-a/net/10.20.1.5/32": 1}
        st = dict(CIRCUITS, networks=[{"network": "tenant-a/net", "vpnTarget": 10005, "index": 4, "prefixes": ["10.20.1.5/32"]}])
        out = run(clusters=[cl("c1", "10.20.1.5/32")], ledger=led, serviceStatus=st)
        self.assertEqual(out["state"], "attached")
        self.assertEqual((out["condition"]["status"], out["condition"]["reason"]), ("True", "Attached"))
        self.assertEqual(out["prefixes"][0]["state"], "ready")
        self.assertEqual((out["allocate"], out["release"]), ([], []))
        self.assertFalse(out["wakeFabric"])

    def test_refusals_per_prefix(self):
        out = run(space=["10.20.0.0/16", "198.51.100.0/24"],
                  clusters=[cl("c1", "2001:db8:5::1/128", "10.30.0.1/32", "198.51.100.7/32", "bad")],
                  service=dict(SERVICE, attachableRanges=["10.0.0.0/8", "198.51.100.0/24"]))
        reasons = {r["prefix"]: (r["state"], r["reason"]) for r in out["prefixes"]}
        self.assertEqual(reasons["2001:db8:5::1/128"], ("refused", "FamilyUnavailable"))
        self.assertEqual(reasons["10.30.0.1/32"], ("refused", "OutsideAddressSpace"))
        self.assertEqual(reasons["198.51.100.7/32"], ("refused", "PrefixReserved"))
        self.assertEqual(reasons["bad"], ("refused", "InvalidSpec"))
        msg = next(r["message"] for r in out["prefixes"] if r["reason"] == "PrefixReserved")
        self.assertIn("198.51.100.0/24", msg)
        self.assertIn("inside the platform's network nodes", msg)
        # refused rows do not change the condition
        self.assertEqual(out["condition"]["reason"], "Attachable")
        self.assertEqual(out["accepted"], [])

    def test_outside_the_attachable_ranges(self):
        out = run(space=["10.20.0.0/16", "172.16.0.0/16"], clusters=[cl("c1", "172.16.0.9/32")])
        self.assertEqual((out["prefixes"][0]["state"], out["prefixes"][0]["reason"]), ("refused", "OutsideAttachableRanges"))

    def test_a_site_row_does_not_refuse(self):
        self.assertEqual(run(clusters=[cl("c1", "10.20.200.9/32")])["accepted"], ["10.20.200.9/32"])

    def test_the_hub_refuses_without_a_published_row(self):
        out = run(space=["192.0.2.0/24", "10.20.0.0/16"], reserved=[], clusters=[cl("c1", "192.0.2.9/32")],
                  service=dict(SERVICE, attachableRanges=[]))
        self.assertEqual(out["prefixes"][0]["reason"], "PrefixReserved")
        self.assertIn("connects from", out["prefixes"][0]["message"])

    def test_first_come_first_served(self):
        other = {"HybridNetwork/tenant-b/web/10.20.1.0/24": 1}
        out = run(clusters=[cl("c1", "10.20.1.5/32")], ledger=other)
        row = out["prefixes"][0]
        self.assertEqual((row["state"], row["reason"]), ("refused", "ServicePrefixInUse"))
        # never names the other tenant's network
        self.assertNotIn("tenant-b", row["message"])
        self.assertNotIn("web", row["message"])
        self.assertEqual((out["accepted"], out["allocate"]), ([], []))

    def test_the_earlier_ledger_entry_wins_a_race(self):
        both = {"HybridNetwork/tenant-b/web/10.20.1.0/24": 3, "HybridNetwork/tenant-a/net/10.20.1.5/32": 2}
        out = run(clusters=[cl("c1", "10.20.1.5/32")], ledger=both)
        self.assertEqual(out["accepted"], ["10.20.1.5/32"])
        later = {"HybridNetwork/tenant-b/web/10.20.1.0/24": 1, "HybridNetwork/tenant-a/net/10.20.1.5/32": 2}
        out = run(clusters=[cl("c1", "10.20.1.5/32")], ledger=later)
        self.assertEqual(out["prefixes"][0]["reason"], "ServicePrefixInUse")
        self.assertEqual(out["release"], ["HybridNetwork/tenant-a/net/10.20.1.5/32"])

    def test_a_prefix_the_fabric_realizes_for_another_network_counts(self):
        st = dict(CIRCUITS, networks=[{"network": "tenant-b/web", "prefixes": ["10.20.1.5/32"]}])
        self.assertEqual(run(clusters=[cl("c1", "10.20.1.5/32")], serviceStatus=st)["prefixes"][0]["reason"],
                         "ServicePrefixInUse")

    def test_the_fabric_refusal_is_shown(self):
        st = dict(CIRCUITS, refused=[{"network": "tenant-a/net", "prefix": "10.20.1.5/32", "reason": "ServicePrefixInUse"}])
        out = run(clusters=[cl("c1", "10.20.1.5/32")], serviceStatus=st, ledger={"HybridNetwork/tenant-a/net/10.20.1.5/32": 1})
        self.assertEqual(out["prefixes"][0]["reason"], "ServicePrefixInUse")
        self.assertEqual(out["release"], ["HybridNetwork/tenant-a/net/10.20.1.5/32"])

    def test_two_clusters_of_one_network_share_a_subnet(self):
        out = run(clusters=[cl("b", "10.20.4.0/24"), cl("a", "10.20.4.0/24")])
        self.assertEqual(out["accepted"], ["10.20.4.0/24"])
        self.assertEqual([r["cluster"] for r in out["prefixes"]], ["a", "b"])
        self.assertEqual(len(out["allocate"]), 1)

    def test_a_cleared_prefix_goes_back(self):
        led = {"HybridNetwork/tenant-a/net/10.20.1.5/32": 1, "HybridNetwork/tenant-b/web/10.30.0.0/24": 2}
        st = dict(CIRCUITS, networks=[{"network": "tenant-a/net", "prefixes": ["10.20.1.5/32"]}])
        out = run(clusters=[], ledger=led, serviceStatus=st)
        self.assertEqual(out["release"], ["HybridNetwork/tenant-a/net/10.20.1.5/32"])
        self.assertTrue(out["wakeFabric"])
        self.assertEqual(out["condition"]["reason"], "Attachable")

    def test_ineligible_networks_accept_nothing(self):
        out = run(serviceStatus={"circuits": []}, clusters=[cl("c1", "10.20.1.5/32")])
        self.assertEqual((out["prefixes"][0]["state"], out["prefixes"][0]["reason"]), ("pending", "CircuitNotDeployed"))
        ov = [{"prefix": "10.20.0.0/16", "range": "10.20.0.0/24", "purpose": "x"}]
        out = run(overlaps=ov, clusters=[cl("c1", "10.20.1.5/32")])
        self.assertEqual((out["prefixes"][0]["state"], out["prefixes"][0]["reason"]), ("refused", "InfrastructureOverlap"))
        self.assertEqual(out["accepted"], [])

    def test_ineligible_keeps_the_ledger_entry(self):
        led = {"HybridNetwork/tenant-a/net/10.20.1.5/32": 4}
        for why, kw in (("CircuitNotDeployed", {"serviceStatus": {"circuits": []}}),
                        ("ServiceNotOffered", {"service": dict(SERVICE, enabled=False)}),
                        ("InfrastructureOverlap", {"overlaps": [{"prefix": "10.20.0.0/16", "range": "10.20.0.0/24",
                                                                  "purpose": "x"}]}),
                        ("OutsideAttachableRanges", {"service": dict(SERVICE, attachableRanges=["10.30.0.0/16"])})):
            with self.subTest(reason=why):
                out = run(clusters=[cl("c1", "10.20.1.5/32")], ledger=led, **kw)
                row = out["prefixes"][0]
                self.assertEqual((row["state"], row["reason"]), ("pending", why))
                self.assertIn("keeps its place", row["message"])
                self.assertEqual((out["accepted"], out["kept"], out["release"]), ([], ["10.20.1.5/32"], []))
        # the tenant's own reasons give it back, and so does a cluster that no longer asks for it
        out = run(clusters=[cl("c1", "10.20.1.5/32")], ledger=led, space=["10.30.0.0/16"])
        self.assertEqual(out["release"], ["HybridNetwork/tenant-a/net/10.20.1.5/32"])
        out = run(clusters=[], ledger=led, serviceStatus={"circuits": []})
        self.assertEqual((out["kept"], out["release"]), ([], ["HybridNetwork/tenant-a/net/10.20.1.5/32"]))

    def test_a_conflict_is_refused_during_an_outage(self):
        led = {"HybridNetwork/tenant-b/web/10.20.1.5/32": 1}
        out = run(clusters=[cl("c1", "10.20.1.5/32")], ledger=led, serviceStatus={"circuits": []})
        self.assertEqual((out["prefixes"][0]["state"], out["prefixes"][0]["reason"]), ("refused", "ServicePrefixInUse"))
        self.assertEqual((out["kept"], out["allocate"]), ([], []))

    def test_turning_off_waits_for_the_clusters(self):
        out = run(platformAccess=False, clusters=[cl("c2", "10.20.1.5/32"), cl("c1")])
        self.assertNotEqual(out["state"], "off")
        self.assertEqual(out["accepted"], ["10.20.1.5/32"])
        self.assertEqual(out["held"], ["c1", "c2"])
        self.assertIn("c1, c2", out["message"])
        self.assertIn("c1, c2", out["condition"]["message"])
        self.assertEqual(run(platformAccess=False)["state"], "off")

    def test_the_cli(self):
        p = subprocess.run([sys.executable, "-I", HELPER], input=json.dumps(req(clusters=[cl("c1", "10.20.1.5/32")])),
                           capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(p.stdout)["accepted"], ["10.20.1.5/32"])
        p = subprocess.run([sys.executable, "-I", HELPER], input='{"action": "nope"}', capture_output=True, text=True)
        self.assertEqual(p.returncode, 1)
        self.assertIn("error", json.loads(p.stdout))


class HubCheck(unittest.TestCase):
    def check(self, prefixes, held):
        return PA.hub_check({"prefixes": prefixes, "held": held, "hub": HUB + ["2001:db8:f::/48"], "reserved": RESERVED})

    def test_a_new_overlapping_prefix_is_refused_with_the_purpose(self):
        out = self.check(["10.20.0.0/16", "192.0.2.0/25"], ["10.20.0.0/16"])
        self.assertEqual(len(out["refused"]), 1)
        self.assertIn("192.0.2.0/25 overlaps 192.0.2.0/24, reserved by the platform: Networks the platform's management connects from",
                      out["refused"][0])
        self.assertEqual(out["overlaps"], [])

    def test_a_held_prefix_keeps_working(self):
        out = self.check(["192.0.0.0/16"], ["192.0.0.0/16"])
        self.assertEqual(out["refused"], [])
        self.assertEqual(out["overlaps"], [{"prefix": "192.0.0.0/16", "range": "192.0.2.0/24",
                                            "purpose": "Networks the platform's management connects from"}])

    def test_ipv6_hub_ranges_are_reserved_too(self):
        self.assertEqual(len(self.check(["2001:db8:f:1::/64"], [])["refused"]), 1)

    def test_clear_prefixes(self):
        self.assertEqual(self.check(["10.20.0.0/16", "2001:db8:5::/48"], []), {"refused": [], "overlaps": []})


def tasks_of(path):
    return load(path)


def text_of(path):
    with open(path) as f:
        return f.read()


def replaced(tasks, repl):
    """The task list with the named tasks replaced (repl: {name: task or None to drop})."""
    out = []
    for t in tasks:
        n = t.get("name")
        if n in repl:
            if repl[n] is not None:
                keep = {"when": t["when"]} if "when" in t and "when" not in repl[n] else {}
                out.append(dict(keep, **repl[n], name=n))
            continue
        out.append(t)
    return out


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class PlatformAccessTasks(unittest.TestCase):
    """platform_access.yml with the API calls replaced by facts."""

    def run_tasks(self, clusters=(), ledger=None, fabric_status=None, spec=None, st=None, ledger_fails=False,
                  real=None, hub=None, overlaps=None, placements=None):
        tasks = replaced(tasks_of(os.path.join(HN, "platform_access.yml")), {
            "List the clusters of the namespace": {"ansible.builtin.set_fact": {"_hn_clusters": {"resources": list(clusters)}}},
            "Read the fabric's ledger": {"ansible.builtin.set_fact": {
                "fab_ledger_result": {"exists": True, "ledger": {"alloc": {"servicePrefix": ledger or {}}}},
                "ledger_read": True}},
            "Record the accepted prefixes in the ledger": {"block": [
                {"ansible.builtin.fail": {"msg": "ledger refused"}, "when": ledger_fails},
                {"ansible.builtin.set_fact": {"recorded": "{{ hn_access_result.allocate }}",
                                              "released": "{{ hn_access_result.release }}"}}],
                "rescue": [{"ansible.builtin.set_fact": {"hn_access_error": "ledger refused"}}]},
            "Report a network overlapping the ranges the platform's management connects from": {
                "ansible.builtin.set_fact": {"evented": True},
                "when": by_name(tasks_of(os.path.join(HN, "platform_access.yml")),
                                "Report a network overlapping the ranges the platform's management connects from")["when"],
                "vars": by_name(tasks_of(os.path.join(HN, "platform_access.yml")),
                                "Report a network overlapping the ranges the platform's management connects from")["vars"]},
        })
        v = {"role_path": ROLE, "fabric_platform_namespace": "platform-ns", "fab_name": "net", "fab_namespace": "tenant-a",
             "fab_spec": spec if spec is not None else {"prefixes": ["10.20.0.0/16"]}, "fab_st": st or {},
             "fab_real": real if real is not None else {"service": SERVICE},
             "hn_fabric": {"status": fabric_status if fabric_status is not None else
                           {"realization": {"service": CIRCUITS}, "reservedRanges": RESERVED}},
             "hn_fabric_name": "fab", "hn_space": [{"prefix": "10.20.0.0/16"}], "hn_hub": HUB if hub is None else hub,
             "hn_hub_check": {"refused": [], "overlaps": overlaps or []},
             "_hn_placements": {"resources": placements if placements is not None else [
                 {"spec": {"network": "net"}, "status": {"subnets": [{"prefix": "10.20.2.0/24", "family": 4},
                                                                     {"prefix": "2001:db8:2::/64", "family": 6}]}},
                 {"spec": {"network": "net"}, "status": {"prefixes": ["10.20.3.0/24", "2001:db8:3::/64"]}},
                 {"spec": {"network": "other"}, "status": {"prefixes": ["10.20.9.0/24"]}}]}}
        return run_play(tasks + [write_out(
            "{'access': hn_access, 'cond': hn_access_condition, 'hold': hn_service_hold | bool, "
            "'wake': hn_access_result.wakeFabric | bool, 'carried': hn_service_carried, "
            "'recorded': recorded | default(none), 'released': released | default(none), "
            "'ledger_read': ledger_read | default(false), 'evented': evented | default(false)}")], v)

    def poc(self, name, network, prefixes=(), kind="openstack", complete=False):
        st = {"platformPrefixes": list(prefixes)}
        if complete:
            st["deletionComplete"] = True
        return {"metadata": {"name": name, "namespace": "tenant-a"},
                "spec": {"type": kind, kind: {"network": network}}, "status": st}

    def test_an_existing_network_gets_the_default_and_nothing_else(self):
        # the realization without the service (the default): no cluster, no ledger read, no fabric wake, no index
        out = self.run_tasks(real={}, fabric_status={}, hub=[])
        self.assertEqual(out["access"]["state"], "unavailable")
        self.assertEqual(out["cond"]["reason"], "ServiceNotOffered")
        self.assertEqual(out["access"]["prefixes"], [])
        self.assertFalse(out["hold"])
        self.assertFalse(out["wake"])
        self.assertFalse(out["ledger_read"])
        self.assertEqual(out["recorded"], [])

    def test_accepted_rows_carry_no_reason(self):
        out = self.run_tasks([self.poc("os1", "net", ["10.20.1.5/32", "10.30.0.1/32"])])
        rows = {r["prefix"]: r for r in out["access"]["prefixes"]}
        self.assertEqual((rows["10.20.1.5/32"]["state"], rows["10.20.1.5/32"]["reason"]), ("pending", ""))
        self.assertEqual((rows["10.30.0.1/32"]["state"], rows["10.30.0.1/32"]["reason"]), ("refused", "OutsideAddressSpace"))

    def test_clusters_of_every_provider_naming_the_network(self):
        cs = [self.poc("os1", "net", ["10.20.1.5/32"]), self.poc("aws1", "net", ["10.20.2.0/24"], kind="aws"),
              self.poc("gcp1", "net", ["10.20.3.0/24"], kind="gcp"), self.poc("x", "other", ["10.20.9.9/32"]),
              self.poc("gone", "net", ["10.20.8.8/32"], complete=True)]
        out = self.run_tasks(cs)
        self.assertEqual(sorted(r["cluster"] for r in out["access"]["prefixes"]), ["aws1", "gcp1", "os1"])
        self.assertEqual(out["recorded"], ["HybridNetwork/tenant-a/net/10.20.1.5/32", "HybridNetwork/tenant-a/net/10.20.2.0/24",
                                           "HybridNetwork/tenant-a/net/10.20.3.0/24"])
        self.assertTrue(out["hold"])
        self.assertTrue(out["wake"])
        self.assertTrue(out["ledger_read"])
        self.assertEqual(out["cond"]["reason"], "AttachPending")

    def test_each_provider_announces_only_its_shape(self):
        # an OpenStack cluster: /32s only; AWS and Google Cloud: exactly a subnet of the network's placements
        cs = [self.poc("os1", "net", ["10.20.1.0/30"]), self.poc("aws1", "net", ["10.20.2.0/25"], kind="aws"),
              self.poc("gcp1", "net", ["10.20.9.0/24"], kind="gcp"), self.poc("x1", "net", ["10.20.4.4/32"], kind="other")]
        out = self.run_tasks(cs)
        rows = {r["cluster"]: r for r in out["access"]["prefixes"]}
        self.assertEqual({k: (r["state"], r["reason"]) for k, r in rows.items()},
                         {k: ("refused", "InvalidSpec") for k in ("os1", "aws1", "gcp1", "x1")})
        self.assertIn("/32", rows["os1"]["message"])
        self.assertIn("placement's subnet", rows["aws1"]["message"])
        self.assertEqual(out["recorded"], [])

    def test_a_ledger_error_never_fails_the_network(self):
        out = self.run_tasks([self.poc("os1", "net", ["10.20.1.5/32"])], ledger_fails=True)
        self.assertEqual(out["access"]["prefixes"][0]["state"], "pending")
        # not accepted: the fabric realizes only rows without a reason
        self.assertEqual(out["access"]["prefixes"][0]["reason"], "AttachPending")
        self.assertIn("ledger refused", out["access"]["prefixes"][0]["message"])
        self.assertIn("could not record", out["access"]["message"])
        self.assertFalse(out["hold"])
        self.assertFalse(out["wake"])

    def test_a_network_the_fabric_still_carries_keeps_its_index(self):
        st = {"realization": {"service": dict(CIRCUITS, networks=[{"network": "tenant-a/net", "prefixes": ["10.20.1.5/32"]}])},
              "reservedRanges": RESERVED}
        out = self.run_tasks([], ledger={"HybridNetwork/tenant-a/net/10.20.1.5/32": 1}, fabric_status=st)
        self.assertTrue(out["hold"])
        self.assertEqual(out["carried"]["network"], "tenant-a/net")
        self.assertEqual(out["released"], ["HybridNetwork/tenant-a/net/10.20.1.5/32"])
        self.assertTrue(out["wake"])

    def test_spec_off_without_clusters(self):
        out = self.run_tasks(spec={"prefixes": ["10.20.0.0/16"], "platformAccess": False})
        self.assertEqual((out["access"]["state"], out["cond"]["reason"]), ("off", "NotRequested"))

    def test_the_overlap_event_once(self):
        ov = [{"prefix": "10.20.0.0/16", "range": "10.20.0.0/24", "purpose": "p"}]
        self.assertTrue(self.run_tasks(overlaps=ov)["evented"])
        seen = {"conditions": [{"type": "PlatformReachable", "status": "False", "reason": "InfrastructureOverlap"}]}
        self.assertFalse(self.run_tasks(overlaps=ov, st=seen)["evented"])
        self.assertFalse(self.run_tasks()["evented"])


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class NetworkJobWiring(unittest.TestCase):
    def main(self):
        return tasks_of(os.path.join(HN, "main.yml"))

    def test_hub_check_task(self):
        t = by_name(self.main(), "Check the address space against the ranges the platform's management connects from")
        t = copy.deepcopy(t)
        out = run_play([t, by_name(self.main(), "Keep the hub check"), write_out("hn_hub_check")],
                       {"role_path": ROLE, "hn_hub": HUB, "fab_spec": {"prefixes": ["10.20.0.0/16", "192.0.2.0/26"]},
                        "fab_st": {"addressSpace": [{"prefix": "10.20.0.0/16"}]}, "hn_fabric": {}})
        self.assertEqual(len(out["refused"]), 1)
        out = run_play([t, by_name(self.main(), "Keep the hub check"), write_out("hn_hub_check")],
                       {"role_path": ROLE, "hn_hub": [], "fab_spec": {"prefixes": ["192.0.2.0/26"]}, "fab_st": {}, "hn_fabric": {}})
        self.assertEqual(out, {"refused": [], "overlaps": []})

    def test_the_hub_replaces_the_platform_sources(self):
        t = by_name(self.main(), "The platform's ranges")
        real = {"attachments": {"linkPool": "169.254.64.0/18"}, "core": {"loopbackPool": "198.51.100.0/26",
                "linkSubnet": "198.51.100.64/26"}, "egress": {"linkPool": "169.254.0.0/18", "platformSources": ["203.0.113.0/24"]},
                "infrastructure": {"hub": HUB}}
        out = run_play([t, write_out("hn_platform_ranges")], {"fab_real": real})
        self.assertIn("192.0.2.0/24", out)
        self.assertNotIn("203.0.113.0/24", out)
        real.pop("infrastructure")
        self.assertEqual(len(run_play([t, write_out("hn_platform_ranges")], {"fab_real": real})), 4)

    def address_checks(self, prefixes, held):
        """The network job's address checks in its order: the platform's ranges, check-network, the hub check."""
        m = self.main()
        check = copy.deepcopy(by_name(m, "Check the address space against the platform's ranges and the circuits"))
        hub = copy.deepcopy(by_name(m, "Check the address space against the ranges the platform's management connects from"))
        real = {"attachments": {"linkPool": "169.254.64.0/18"}, "core": {"loopbackPool": "198.51.100.0/26",
                "linkSubnet": "198.51.100.64/26"}, "egress": {"linkPool": "169.254.0.0/18"},
                "infrastructure": {"hub": ["10.30.0.0/16"]}}
        return run_play([by_name(m, "The platform's ranges"), check, hub, by_name(m, "Keep the hub check"),
                         write_out("{'check': fab_check_result.errors, 'hub': hn_hub_check}")],
                        {"role_path": ROLE, "fab_real": real, "fab_spec": {"prefixes": prefixes},
                         "fab_st": {"addressSpace": [{"prefix": p} for p in held]}, "hn_fabric": {},
                         "hn_exclusive": [], "_hn_circuits": {"resources": []}, "fab_name": "net"})

    def test_an_existing_network_inside_the_hub_keeps_working(self):
        # wholly inside a hub range, held by the address space before the hub grew: no refusal anywhere, reported
        out = self.address_checks(["10.30.4.0/24"], ["10.30.4.0/24"])
        self.assertEqual((out["check"], out["hub"]["refused"]), ([], []))
        self.assertEqual([o["prefix"] for o in out["hub"]["overlaps"]], ["10.30.4.0/24"])
        # a new prefix inside or around a hub range is refused (by the hub check, with the range's purpose)
        for new in ("10.30.5.0/24", "10.0.0.0/8"):
            with self.subTest(prefix=new):
                out = self.address_checks(["10.30.4.0/24", new], ["10.30.4.0/24"])
                self.assertEqual(out["check"], [])
                self.assertEqual(len(out["hub"]["refused"]), 1)
                self.assertIn(new, out["hub"]["refused"][0])
        # the platform's own ranges are still refused by check-network
        out = self.address_checks(["169.254.64.0/24"], ["169.254.64.0/24"])
        self.assertEqual(len(out["check"]), 1)

    def egress_hold(self, **kw):
        t = by_name(self.main(), "Hold an egress index")
        expr = t["when"]
        v = {"hn_egress_want4": False, "hn_egress_want6": False, "hn_egress_offered6": False, "hn_egress_offered": True,
             "hn_service_hold": False}
        v.update(kw)
        return run_play([{"ansible.builtin.set_fact": {"x": "{{ %s }}" % expr.strip()}}, write_out("x | bool")], v)

    def test_the_index_follows_egress_or_service_prefixes(self):
        self.assertFalse(self.egress_hold())
        self.assertTrue(self.egress_hold(hn_egress_want4=True))
        self.assertFalse(self.egress_hold(hn_egress_want4=True, hn_egress_offered=False))
        self.assertTrue(self.egress_hold(hn_service_hold=True, hn_egress_offered=False))

    def egress_release(self, **kw):
        t = by_name(self.main(), "Give the egress index back")
        v = {"hn_egress_want4": False, "hn_egress_want6": False, "hn_egress_offered6": False, "hn_egress_offered": True,
             "hn_service_hold": False, "hn_egress_carried": {}, "hn_service_carried": {}}
        v.update(kw)
        return run_play([{"ansible.builtin.set_fact": {"x": "{{ %s }}" % t["when"].strip()}}, write_out("x | bool")], v)

    def test_the_index_goes_back_only_when_the_fabric_carries_neither(self):
        self.assertTrue(self.egress_release())
        self.assertFalse(self.egress_release(hn_service_carried={"network": "a/b"}))
        self.assertFalse(self.egress_release(hn_egress_carried={"network": "a/b"}))
        self.assertFalse(self.egress_release(hn_service_hold=True))

    def test_the_ready_write_carries_the_access_and_the_condition(self):
        t = by_name(self.main(), "Report the network ready")
        self.assertIn("'platformAccess': hn_access", t["vars"]["fab_status"])
        self.assertEqual(t["vars"]["fab_conditions"], "{{ [hn_access_condition] }}")

    def test_the_annotation_is_gone(self):
        text = text_of(os.path.join(HN, "main.yml"))
        self.assertNotIn("hn_platform_reach", text)
        self.assertNotIn("platformSources", text)
        t = by_name(self.main(), "Remove the retired platform reach annotation")
        self.assertIsNone(t["kubernetes.core.k8s"]["definition"]["metadata"]["annotations"]["shc.signal9.gg/platform-reach"])
        self.assertEqual(t["kubernetes.core.k8s"]["merge_type"], "merge")
        for flag, ann in ((True, {"shc.signal9.gg/platform-reach": "[]"}), (False, {"x": "y"}), (False, None)):
            out = run_play([{"ansible.builtin.set_fact": {"x": "{{ %s }}" % t["when"].strip('"')}}, write_out("x | bool")],
                           {"fab_obj": {"metadata": {"annotations": ann} if ann is not None else {}}})
            self.assertEqual(out, flag)

    def test_the_fabric_wake(self):
        t = by_name(self.main(), "Wake the fabric")
        self.assertIn("hn_access_result.wakeFabric", t["when"])


class TeardownWiring(unittest.TestCase):
    def test_teardown_waits_for_clusters_then_the_fabric_then_releases(self):
        text = text_of(os.path.join(HND, "main.yml"))
        i_cl = text.index("Wait for the clusters on the network")
        i_svc = text.index("Take the network out of the service routing domain first")
        i_rel = text.index("Release the network's service prefixes")
        i_vpn = text.index("Release the VPN route target")
        self.assertLess(i_cl, i_svc)
        self.assertLess(i_svc, i_rel)
        self.assertLess(i_rel, i_vpn)
        self.assertIn("kind: servicePrefix", text)
        self.assertIn("platformAccess: {prefixes: []}", text)

    @unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
    def test_the_clusters_that_block(self):
        t = by_name(load(os.path.join(HND, "main.yml")), "The clusters on the network")
        cs = [{"metadata": {"name": "b"}, "spec": {"type": "aws", "aws": {"network": "net"}}},
              {"metadata": {"name": "a"}, "spec": {"type": "openstack", "openstack": {"network": "net"}}},
              {"metadata": {"name": "c"}, "spec": {"type": "gcp", "gcp": {}}},
              {"metadata": {"name": "d"}, "spec": {"type": "openstack", "openstack": {"network": "net"}},
               "status": {"deletionComplete": True}}]
        self.assertEqual(run_play([t, write_out("hnd_clusters")], {"fab_name": "net", "_hnd_clusters": {"resources": cs}}),
                         ["a", "b"])

    @unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
    def test_only_this_networks_prefixes_are_released(self):
        t = copy.deepcopy(by_name(load(os.path.join(HND, "main.yml")), "Release the network's service prefixes"))
        t.pop("ansible.builtin.include_tasks")
        t["ansible.builtin.set_fact"] = {"seen": "{{ seen | default([]) + [hnd_owner] }}"}
        led = {"HybridNetwork/tenant-a/net/10.20.1.5/32": 1, "HybridNetwork/tenant-a/net2/10.20.1.6/32": 2,
               "HybridNetwork/tenant-ab/net/10.20.1.7/32": 3, "HybridNetwork/tenant-a/net/10.20.4.0/24": 4}
        out = run_play([t, write_out("seen | default([])")],
                       {"fab_namespace": "tenant-a", "fab_name": "net", "fab_st": {"fabric": "fab"},
                        "fab_ledger_result": {"ledger": {"alloc": {"servicePrefix": led}}}})
        self.assertEqual(sorted(out), ["HybridNetwork/tenant-a/net/10.20.1.5/32", "HybridNetwork/tenant-a/net/10.20.4.0/24"])


class CrdText(unittest.TestCase):
    def test_spec_and_status(self):
        import yaml
        doc = load(CRD)
        props = doc["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]
        pa = props["spec"]["properties"]["platformAccess"]
        self.assertEqual((pa["type"], pa["default"]), ("boolean", True))
        st = props["status"]["properties"]["platformAccess"]["properties"]
        self.assertEqual(sorted(st), ["message", "ports", "prefixes", "reason", "sources", "state"])
        self.assertEqual(sorted(st["prefixes"]["items"]["properties"]), ["cluster", "message", "prefix", "reason", "state"])
        # API text only
        text = json.dumps(pa) + json.dumps(props["status"]["properties"]["platformAccess"])
        for word in ("DESIGN", "OD1", "2026", "fabric_realization", "infrastructure.hub", "service VRF"):
            self.assertNotIn(word, text)



class FirstComeFirstServedSurvivesOutages(unittest.TestCase):
    """Two tenants with the same address space (separate VPNs). Tenant A's cluster has 10.20.1.5/32 accepted; tenant
    B's cluster asks for the same address. The platform's side goes away for a while (no platform circuit realized,
    the service turned off) and comes back, with B's network job running first and fabric runs in between that
    planned nothing: A keeps the prefix, B is refused, and the fabric's plan agrees (the order lives in the ledger)."""
    A, B = "tenant-a/net", "tenant-b/web"
    P = "10.20.1.5/32"

    def setUp(self):
        self.ledger = {}
        self.realized = []  # the fabric's networks[] (previous status)
        self.next = 1

    def job(self, who, service_status, service=SERVICE):
        cluster = cl("c1" if who == self.A else "d1", self.P)
        out = run(network=who, clusters=[cluster], ledger=dict(self.ledger), serviceStatus=service_status,
                  service=service)
        for o in out["allocate"]:
            self.ledger[o] = self.next
            self.next += 1
        for o in out["release"]:
            self.ledger.pop(o, None)
        return out

    def fabric(self, rows_by_net, circuits=True):
        """the fabric's plan (shc_fabric.service_plan) over the networks' rows"""
        sys.path.insert(0, os.path.join(ROOT, "eda", "common", "files"))
        import shc_fabric as F
        nets = []
        for key, rows in rows_by_net.items():
            ns, name = key.split("/")
            nets.append({"metadata": {"namespace": ns, "name": name},
                         "spec": {"prefixes": ["10.20.0.0/16"]},
                         "status": {"fabric": "fab", "addressSpace": [{"prefix": "10.20.0.0/16", "family": 4}],
                                    "platformAccess": {"prefixes": rows}}})
        real = {"infrastructure": {"hub": HUB}, "attachments": {"enabled": True, "linkPool": "100.127.0.0/20"},
                "core": {"loopbackPool": "100.126.0.0/24", "linkSubnet": "100.126.1.0/24"},
                "egress": {"linkPool": "169.254.0.0/16", "networkTableBase": 100000, "vrf": {"name": "egress", "table": 9000}},
                "service": {"enabled": True, "vrf": {"name": "service", "table": 9001}, "ports": [6443, 443],
                            "hubEndpoints": [], "attachableRanges": ["10.0.0.0/8"]}}
        alloc = {"vpnTarget": {"HybridNetwork/" + self.A: 10001, "HybridNetwork/" + self.B: 10002},
                 "egress": {"HybridNetwork/" + self.A: 1, "HybridNetwork/" + self.B: 2}, "servicePrefix": dict(self.ledger)}
        circ = [{"circuit": "platform-ns/hub-a", "vlan": 3001, "nodes": {"bgw-1": "100.127.0.2"}, "device": "bond1.3001",
                 "link": "100.127.0.0/29", "ce": "100.127.0.1", "rev": "r"}] if circuits else []
        out = F.service_plan({"realization": real, "nodes": ["bgw-1"], "fabric": "fab", "domainAsn": 65010,
                              "networks": nets, "ledger": alloc, "circuits": circ, "vrfs": {},
                              "previous": {"networks": self.realized}})
        self.assertEqual(out["errors"], [])
        self.realized = out["status"]["networks"]
        return out["status"]

    def status(self, circuits=True):
        return {"circuits": [{"circuit": "platform-ns/hub-a", "vlan": 3001}] if circuits else [],
                "networks": self.realized, "refused": []}

    def walk(self, outage):
        # A first: accepted and realized; B refused
        a = self.job(self.A, self.status())
        self.assertEqual(a["accepted"], [self.P])
        st = self.fabric({self.A: a["prefixes"]})
        self.assertEqual([w["network"] for w in st["networks"]], [self.A])
        b = self.job(self.B, self.status())
        self.assertEqual(b["prefixes"][0]["reason"], "ServicePrefixInUse")
        # the outage, with a fabric run that planned nothing in between
        a = self.job(self.A, *outage())
        self.assertEqual((a["release"], a["kept"]), ([], [self.P]))
        st = self.fabric({self.A: a["prefixes"], self.B: b["prefixes"]}, circuits=False)
        self.assertEqual(st["networks"], [])
        b = self.job(self.B, *outage())
        self.assertEqual((b["prefixes"][0]["state"], b["allocate"]), ("refused", []))
        # the platform's side returns: B's network job runs first
        b = self.job(self.B, self.status())
        self.assertEqual((b["prefixes"][0]["reason"], b["allocate"]), ("ServicePrefixInUse", []))
        a = self.job(self.A, self.status())
        self.assertEqual((a["accepted"], a["allocate"], a["release"]), ([self.P], [], []))
        st = self.fabric({self.B: b["prefixes"], self.A: a["prefixes"]})
        self.assertEqual([(w["network"], w["prefixes"]) for w in st["networks"]], [(self.A, [self.P])])
        self.assertEqual(list(self.ledger), ["HybridNetwork/" + self.A + "/" + self.P])

    def test_no_platform_circuit_for_a_while(self):
        self.walk(lambda: (self.status(circuits=False),))

    def test_the_service_turned_off_and_on(self):
        self.walk(lambda: ({"circuits": [], "networks": [], "refused": []}, dict(SERVICE, enabled=False)))

    def test_a_fabric_run_that_planned_nothing(self):
        # the fabric's status lost the network (a run without circuits) while the circuits are listed again
        a = self.job(self.A, self.status())
        self.fabric({self.A: a["prefixes"]})
        self.realized = []
        b = self.job(self.B, self.status())
        self.assertEqual((b["prefixes"][0]["reason"], b["allocate"]), ("ServicePrefixInUse", []))
        a = self.job(self.A, self.status())
        self.assertEqual((a["accepted"], a["release"]), ([self.P], []))
        st = self.fabric({self.B: b["prefixes"], self.A: a["prefixes"]})
        self.assertEqual([w["network"] for w in st["networks"]], [self.A])

if __name__ == "__main__":
    unittest.main()
