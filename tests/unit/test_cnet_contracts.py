"""Contracts between the streams of the cnet round, with the real code on both sides (no fake of the callee):

  network job (shc_platform_access.py) -> fabric (shc_fabric.py service-plan) -> network job: the fabric realizes the
      rows the network accepted (ready, or pending with no reason) and nothing else; first come first served by the
      ledger's order (shc_ledger.py servicePrefix); the network reads the fabric's report back as ready rows and
      PlatformReachable Attached;
  circuit job (platform circuit record) -> fabric (attachments-plan, service-plan) -> circuit job: the fabric plans
      the circuit S2 recorded into the service VRF and reports it in status.realization.service.circuits with the
      record's revision, which the circuit job reads back;
  cluster job (po_preflight.py) <- network job: PlatformReachable reasons become the cluster's Preflight reasons,
      the network's address space is read as the CRD declares it; the cluster's wait reads the fabric's report and the
      network's rows;
  cluster job (preflight_ranges.yml) -> fabric (check-cluster-networks), through the real fabric_check.yml: the
      request's ranges, provider and site are applied, the reason comes back as Preflight reason;
  cluster job (cluster_fips.yml, cluster_fips_release.yml) and environment job -> shc_openstack.py cluster-fips
      (against S3's fake Neutron): present creates and finds by description, absent releases by id and description.

Documentation and private ranges, example names only."""
import copy
import importlib.util
import json
import os
import sys
import unittest
from unittest import mock

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "eda", "common", "files"))
import shc_fabric as F  # noqa: E402
import shc_ledger as L  # noqa: E402
from play_helper import HAVE_ANSIBLE, by_name, load, run_play, walk, write_out  # noqa: E402
import po_harness as H  # noqa: E402

ROLES = os.path.join(ROOT, "eda", "rulebooks", "roles")


def _module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


PA = _module("shc_platform_access", os.path.join(ROLES, "hybridnetwork_provision", "files", "shc_platform_access.py"))
P = _module("po_preflight_contract", os.path.join(ROLES, "platformopenshift_provision", "files", "po_preflight.py"))

with open(os.path.join(ROOT, "eda", "common", "vars", "fabric_realization.yml")) as fh:
    DEFAULTS = yaml.safe_load(fh)["fabric_realization_defaults"]

HUB = "198.51.100.0/24"
NODES = ["bgw-1", "bgw-2"]
FABRIC, PLAT = "lab", "platform"
SPACE = "192.0.2.0/24"
CIRCUIT_STATUS = [{"circuit": f"{PLAT}/hub-a", "vlan": 3099, "nodes": NODES, "rev": "r1"}]


def realization(attachable=(SPACE,)):
    r = copy.deepcopy(DEFAULTS)
    r["attachments"]["enabled"] = True
    r["infrastructure"] = {"hub": [HUB]}
    r["service"].update({"enabled": True, "attachableRanges": list(attachable), "hubEndpoints": []})
    return r


def platform_circuit_plan_entry():
    """attachments-plan's platform entry for one circuit (as the fabric job hands it to service-plan)."""
    return {"circuit": f"{PLAT}/hub-a", "kind": "platform", "id": "AC3099", "vrf": "service", "table": 9001,
            "vlan": 3099, "device": "bond1.3099", "parent": "bond1", "mtu": 1500, "link": "10.255.128.8/29",
            "ce": "10.255.128.11", "peerAsn": 64600, "authentication": "none", "passwordPath": "", "maxPrefixes": 50,
            "maxPrefixAction": "teardown", "bfd": False, "rev": "r1",
            "nodes": {n: f"10.255.128.{9 + i}" for i, n in enumerate(NODES)}}


class Ledger:
    """The fabric's ledger as the network job writes it (ledger-allocate of shc_ledger.py, kind servicePrefix)."""

    def __init__(self):
        self.alloc = {"vpnTarget": {}, "egress": {}, "servicePrefix": {}}

    def network(self, key, target, index):
        self.alloc["vpnTarget"][f"HybridNetwork/{key}"] = target
        self.alloc["egress"][f"HybridNetwork/{key}"] = index

    def apply(self, result):
        sp = self.alloc["servicePrefix"]
        for o in result["allocate"]:
            if o not in sp:
                sp[o] = L.ordered_value(sp, 1, 1048575, "servicePrefix")
        for o in result["release"]:
            sp.pop(o, None)

    def of(self, key):
        return {o: v for o, v in self.alloc["servicePrefix"].items() if o.startswith(f"HybridNetwork/{key}/")}


def network_decision(key, prefixes, ledger, service_status, real, space=SPACE, platform_access=True):
    """The network job's decision (platform-access) for one network with one cluster c1."""
    return PA.platform_access({
        "network": key, "fabric": FABRIC, "platformAccess": platform_access, "space": [space], "overlaps": [],
        "hub": real["infrastructure"]["hub"],
        "service": {"enabled": real["service"]["enabled"], "ports": real["service"]["ports"],
                    "attachableRanges": real["service"]["attachableRanges"]},
        "serviceStatus": service_status, "reserved": F.reserved_ranges(real),
        # a network off with no cluster on it (a cluster that names it holds platform access on)
        "clusters": [{"name": "c1", "prefixes": list(prefixes), "deleting": False}] if platform_access or prefixes else [],
        "ledger": {o: v for o, v in ledger.alloc["servicePrefix"].items()}})


def network_object(key, rows, space=SPACE):
    ns, name = key.split("/")
    return {"metadata": {"namespace": ns, "name": name}, "spec": {"prefixes": [space]},
            "status": {"fabric": FABRIC, "addressSpace": [{"prefix": space, "family": 4}],
                       "platformAccess": {"prefixes": rows}}}


def fabric_plan(real, networks, ledger, previous=None):
    return F.service_plan({"realization": real, "nodes": NODES, "fabric": FABRIC, "domainAsn": 65010,
                           "networks": networks, "ledger": ledger.alloc, "circuits": [platform_circuit_plan_entry()],
                           "vrfs": {n: [] for n in NODES}, "previous": previous or {}})


class NetworkRowsAndTheServicePlan(unittest.TestCase):
    def test_the_round_trip(self):
        real, led = realization(), Ledger()
        led.network("acme/n1", 10005, 3)
        prefixes = ["192.0.2.15/32", "192.0.2.16/32"]
        first = network_decision("acme/n1", prefixes, led, {"circuits": CIRCUIT_STATUS}, real)
        self.assertEqual(first["condition"]["reason"], "Attachable" if not first["prefixes"] else "AttachPending")
        self.assertEqual([(r["state"], r["reason"]) for r in first["prefixes"]], [("pending", "")] * 2)
        led.apply(first)
        self.assertEqual(len(led.of("acme/n1")), 2)
        plan = fabric_plan(real, [network_object("acme/n1", first["prefixes"])], led)
        self.assertEqual(plan["errors"], [])
        st = plan["status"]
        self.assertEqual(st["networks"], [{"network": "acme/n1", "vpnTarget": 10005, "index": 3, "prefixes": prefixes}])
        self.assertEqual(st["refused"], [])
        self.assertEqual([c["circuit"] for c in st["circuits"]], [f"{PLAT}/hub-a"])
        # the network reads the report back: ready rows, Attached, nothing to wake
        again = network_decision("acme/n1", prefixes, led, st, real)
        self.assertEqual([r["state"] for r in again["prefixes"]], ["ready", "ready"])
        self.assertEqual((again["condition"]["status"], again["condition"]["reason"]), ("True", "Attached"))
        self.assertEqual((again["allocate"], again["release"], again["wakeFabric"]), ([], [], False))

    def test_rows_with_a_reason_are_not_realized(self):
        """Pending with a reason (the network waits, e.g. no platform circuit yet, or its ledger write failed) and
        refused rows stay out of the service VRF, even when the ledger still holds an entry for them."""
        real, led = realization(), Ledger()
        led.network("acme/n1", 10005, 3)
        waiting = network_decision("acme/n1", ["192.0.2.15/32"], led, {"circuits": []}, real)
        self.assertEqual([(r["state"], r["reason"]) for r in waiting["prefixes"]], [("pending", "CircuitNotDeployed")])
        self.assertEqual(waiting["allocate"], [])
        led.alloc["servicePrefix"]["HybridNetwork/acme/n1/192.0.2.15/32"] = 1   # left over
        rows = waiting["prefixes"] + [{"prefix": "192.0.2.16/32", "cluster": "c1", "state": "pending",
                                       "reason": "AttachPending", "message": "not recorded yet"},
                                      {"prefix": "192.0.2.17/32", "cluster": "c1", "state": "refused",
                                       "reason": "PrefixReserved", "message": "x"}]
        for p in ("192.0.2.16/32", "192.0.2.17/32"):
            led.alloc["servicePrefix"][f"HybridNetwork/acme/n1/{p}"] = 2 if p.endswith("16/32") else 3
        plan = fabric_plan(real, [network_object("acme/n1", rows)], led)
        self.assertEqual(plan["status"]["networks"], [])
        self.assertFalse(any(w["prefixes"] for n in plan["nodes"].values() for w in n["networks"]))

    def test_first_come_first_served_both_sides(self):
        """Two networks of two tenants with the same space: the prefix recorded first keeps its place on both sides;
        the later one is refused ServicePrefixInUse by the network job and never realized by the fabric."""
        real, led = realization(), Ledger()
        led.network("acme/n1", 10005, 3)
        led.network("beta/n2", 10006, 4)
        a = network_decision("acme/n1", ["192.0.2.15/32"], led, {"circuits": CIRCUIT_STATUS}, real)
        led.apply(a)
        b = network_decision("beta/n2", ["192.0.2.15/32"], led, {"circuits": CIRCUIT_STATUS}, real)
        self.assertEqual([(r["state"], r["reason"]) for r in b["prefixes"]], [("refused", "ServicePrefixInUse")])
        self.assertNotIn("acme", b["prefixes"][0]["message"])
        self.assertEqual(b["allocate"], [])
        # even had the later network accepted it (a race of two network runs), the fabric keeps the earlier one
        led.alloc["servicePrefix"]["HybridNetwork/beta/n2/192.0.2.15/32"] = L.ordered_value(
            led.alloc["servicePrefix"], 1, 1048575, "servicePrefix")
        raced = [{"prefix": "192.0.2.15/32", "cluster": "c1", "state": "pending", "reason": "", "message": ""}]
        plan = fabric_plan(real, [network_object("acme/n1", a["prefixes"]), network_object("beta/n2", raced)], led)
        self.assertEqual([w["network"] for w in plan["status"]["networks"]], ["acme/n1"])
        self.assertEqual([(r["network"], r["reason"]) for r in plan["status"]["refused"]], [("beta/n2", "ServicePrefixInUse")])
        # the loser's network reads the fabric's refusal; its cluster's wait fails with the same reason
        back = network_decision("beta/n2", ["192.0.2.15/32"], led, plan["status"], real)
        self.assertEqual(back["prefixes"][0]["reason"], "ServicePrefixInUse")
        wait = P.service_wait({"network": "beta/n2", "prefixes": ["192.0.2.15/32"], "service": plan["status"],
                               "rows": back["prefixes"]})
        self.assertEqual((wait["done"], wait["reason"]), (False, "ServicePrefixInUse"))

    def test_the_cluster_wait_reads_the_report(self):
        real, led = realization(), Ledger()
        led.network("acme/n1", 10005, 3)
        prefixes = ["192.0.2.15/32", "192.0.2.16/32"]
        d = network_decision("acme/n1", prefixes, led, {"circuits": CIRCUIT_STATUS}, real)
        wait = P.service_wait({"network": "acme/n1", "prefixes": prefixes, "service": {}, "rows": d["prefixes"]})
        self.assertEqual((wait["done"], wait["reason"]), (False, "WaitingForPlatformAccess"))
        led.apply(d)
        st = fabric_plan(real, [network_object("acme/n1", d["prefixes"])], led)["status"]
        wait = P.service_wait({"network": "acme/n1", "prefixes": prefixes, "service": st, "rows": d["prefixes"]})
        self.assertEqual((wait["done"], wait["reason"]), (True, ""))

    def test_a_prefix_outside_the_attachable_ranges_on_both_sides(self):
        real, led = realization(attachable=("192.0.2.0/25",)), Ledger()
        led.network("acme/n1", 10005, 3)
        d = network_decision("acme/n1", ["192.0.2.200/32"], led, {"circuits": CIRCUIT_STATUS}, real)
        self.assertEqual([(r["state"], r["reason"]) for r in d["prefixes"]], [("refused", "OutsideAttachableRanges")])
        verdict = F.service_prefix_check({"prefixes": ["192.0.2.200/32"], "space": [SPACE], "realization": real})
        self.assertEqual(verdict["prefixes"][0]["reason"], "OutsideAttachableRanges")


class PlatformReachableAndThePreflight(unittest.TestCase):
    """The network job's condition, as the cluster job's preflight reads it."""

    def judge(self, decision, space=SPACE):
        net = network_object("acme/n1", decision["prefixes"], space)
        net["status"]["conditions"] = [dict(decision["condition"], lastTransitionTime="2001-01-01T00:00:00Z")]
        placement = {"metadata": {"name": "p1", "namespace": "acme"},
                     "spec": {"network": "n1", "backend": {"kind": "CloudOSO", "name": "env1"},
                              "use": {"routerAttachments": True}},
                     "status": {"ready": True, "subnets": [{"family": 4, "prefix": "192.0.2.0/25"}],
                                "use": {"routerAttachments": {"state": "ready"}},
                                "realization": {"site": {"networkId": "net-1", "networkName": "shc-n1"}}}}
        real = realization()
        fab = {"metadata": {"name": FABRIC},
               "status": {"reservedRanges": F.reserved_ranges(real),
                          "realization": {"service": dict(F.service_settings(real), enabled=True, sources=[HUB],
                                                          attachableRanges=real["service"]["attachableRanges"],
                                                          hubEndpoints=[], circuits=CIRCUIT_STATUS)}}}
        return P.network({"kind": "CloudOSO", "environment": "env1", "namespace": "acme", "network": "n1",
                          "networkObj": net, "placements": [placement], "fabric": fab, "needPlatform": True,
                          "needRouterAttachments": True, "hubApi": ""})

    def test_reasons(self):
        real, led = realization(), Ledger()
        cases = (
            (dict(service_status={"circuits": CIRCUIT_STATUS}), ""),
            (dict(service_status={"circuits": []}), "CircuitNotDeployed"),
            (dict(service_status={"circuits": CIRCUIT_STATUS}, platform_access=False, prefixes=[]), "PlatformAccessOff"),
            (dict(service_status={"circuits": CIRCUIT_STATUS}, space="198.51.100.0/25"), "PrefixReserved"),
        )
        for kw, want in cases:
            with self.subTest(want=want):
                kw = dict(kw)
                prefixes = kw.pop("prefixes", [])
                pa = kw.pop("platform_access", True)
                space = kw.pop("space", SPACE)
                d = network_decision("acme/n1", prefixes, led, kw["service_status"], real, space=space,
                                     platform_access=pa)
                if want == "PrefixReserved":
                    d = PA.platform_access(dict(network="acme/n1", fabric=FABRIC, platformAccess=True, space=[space],
                                                overlaps=[{"prefix": space, "range": HUB, "purpose": "x"}],
                                                hub=[HUB], service={"enabled": True, "ports": [6443],
                                                                    "attachableRanges": ["0.0.0.0/0"]},
                                                serviceStatus=kw["service_status"], reserved=[], clusters=[],
                                                ledger={}))
                    self.assertEqual(d["condition"]["reason"], "InfrastructureOverlap")
                self.assertEqual(self.judge(d, space)["reason"], want)

    def test_a_first_cluster_waiting_does_not_hold_the_next(self):
        real, led = realization(), Ledger()
        led.network("acme/n1", 10005, 3)
        d = network_decision("acme/n1", ["192.0.2.15/32"], led, {"circuits": CIRCUIT_STATUS}, real)
        self.assertEqual(d["condition"]["reason"], "AttachPending")
        out = self.judge(d)
        self.assertEqual(out["reason"], "")
        # the space comes from spec.prefixes and status.addressSpace[].prefix (rows, not strings)
        self.assertEqual(out["space"], [SPACE])


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class PlatformCircuitRecordAndThePlan(unittest.TestCase):
    """The circuit job's record of a platform circuit, planned by the fabric and read back by the circuit job."""

    def test_record_plan_report(self):
        import test_attachment_ipv6 as T
        import test_circuit_platform_job as C
        plan = F.circuit_plan({"attachments": T.AT, "nodes": NODES, "index": 2})
        v = C.record_vars()
        v.update(ac_vlan=3099, ac_link_index=2, ac_plan=plan, ac_auth_type="none",
                 ac_bearer_real={"interface": "bond1", "mtu": 1500})
        picked = [copy.deepcopy(t) for t in walk(load(os.path.join(ROLES, "attachmentcircuit_provision", "tasks",
                                                                   "main.yml"))) if t.get("name") in C.RECORD]
        rec = run_play(picked + [write_out("ac_real")], v)
        circuit = {"metadata": {"namespace": PLAT, "name": "hub-a"}, "spec": {"ceKind": "platform"},
                   "status": {"fabric": FABRIC, "realization": rec}}
        real = realization()
        req = T.plan_req([circuit], networks=[], placements=[])
        owner = f"AttachmentCircuit/{PLAT}/hub-a"
        req["ledger"]["attachVlan"] = {owner: 3099}
        req["ledger"]["attachLink"] = {owner: 2}
        req.update({"service": real["service"], "fabricNamespace": PLAT, "passwords": [], "egress": {},
                    "platformSources": [HUB]})
        at = F.attachments_plan(req)
        self.assertEqual(at["errors"], [])
        self.assertEqual(at["skipped"], [])
        entry = at["platform"][0]
        self.assertEqual((entry["circuit"], entry["vrf"], entry["vlan"], entry["device"], entry["ce"], entry["peerAsn"]),
                         (f"{PLAT}/hub-a", "service", 3099, "bond1.3099", rec["ce"], rec["peerAsn"]))
        self.assertEqual(sorted(entry["nodes"]), NODES)
        self.assertNotIn(f"{PLAT}/hub-a", json.dumps(at["circuits"]))   # never a customer circuit
        sp = F.service_plan({"realization": real, "nodes": NODES, "fabric": FABRIC, "domainAsn": 65010, "networks": [],
                             "ledger": {}, "circuits": at["platform"], "vrfs": {n: [] for n in NODES}, "previous": {}})
        self.assertEqual(sp["errors"], [])
        t = by_name(load(os.path.join(ROLES, "attachmentcircuit_provision", "tasks", "main.yml")),
                    "Does the fabric hold this revision on its nodes")
        got = run_play([t, write_out("ac_entry")], {"ac_platform": True, "fab_namespace": PLAT, "fab_name": "hub-a",
                                                    "ac_fabric": {"status": {"realization": {"service": sp["status"]}}}})
        self.assertEqual((got["rev"], sorted(got["nodes"])), (rec["rev"], NODES))


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class ClusterRangesThroughTheRealCheck(unittest.TestCase):
    """preflight_ranges.yml with the real fabric_check.yml and shc_fabric.py check-cluster-networks."""

    RESERVED = [{"prefix": HUB, "purpose": "Networks the platform's management connects from", "rule": "Refused"},
                {"prefix": "100.64.0.0/16", "purpose": "Addresses the site uses itself", "rule": "SteppedAround",
                 "site": "site1"}]

    def run_ranges(self, provider, networking, space=(), machine="", site="site1"):
        import test_platformopenshift_network as PO
        trace_reason = {"name": "trace the reason", "ansible.builtin.shell": "cat >> {{ trace_file }}",
                        "args": {"stdin": "{{ {'reason': po_fail_reason | default('')} | to_json }}"}, "changed_when": False}
        tasks = [{"block": [{"ansible.builtin.include_tasks": os.path.join(ROLES, "platformopenshift_provision", "tasks",
                                                                           "preflight_ranges.yml")},
                            dict(trace_reason, name="trace passed")],
                  "rescue": [trace_reason]}]
        v = dict(PO.BASE, platform_spec={"type": provider, "networking": networking}, pr_space=list(space),
                 pr_reserved=self.RESERVED, pr_site=site, pr_machine_from_placement=machine)
        rc, tr, out = H.run("platformopenshift_provision", "preflight_ranges.yml", v, tasks=tasks,
                            real=("fabric_check.yml",))
        self.assertEqual(rc, 0, out)
        return [t for t in tr if "reason" in t][-1]["reason"]

    def test_reasons_from_the_helper(self):
        self.assertEqual(self.run_ranges("openstack", {}, space=[SPACE]), "")
        self.assertEqual(self.run_ranges("openstack", {"serviceNetwork": "198.51.100.0/24"}, space=[SPACE]), "PrefixReserved")
        self.assertEqual(self.run_ranges("openstack", {"clusterNetwork": "192.0.0.0/16"}, space=[SPACE]), "AddressConflict")
        # a site's own range counts on that site only
        self.assertEqual(self.run_ranges("openstack", {"machineNetwork": "100.64.0.0/24"}), "PrefixReserved")
        self.assertEqual(self.run_ranges("openstack", {"machineNetwork": "100.64.0.0/24"}, site="site2"), "")
        # AWS on a network: the machine network is the placement's subnet, the network's space is not a conflict
        self.assertEqual(self.run_ranges("aws", {}, space=[SPACE], machine="192.0.2.0/25"), "")
        self.assertEqual(self.run_ranges("aws", {}, space=["198.51.100.0/24"], machine="198.51.100.0/25"),
                         "PrefixReserved")


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class ClusterFloatingIPsAgainstTheHelper(unittest.TestCase):
    """The requests the cluster and environment jobs send, run through shc_openstack.py cluster-fips."""

    def requests(self, task_file, role="platformopenshift_provision", **v):
        import test_platformopenshift_network as PO
        rc, tr, out = H.run(role, task_file, dict(PO.BASE, **v))
        self.assertEqual(rc, 0, out)
        return H.os_calls(tr, "cluster-fips")

    def helper(self, conn, req):
        import test_shc_openstack_cluster_fips as S3
        req = dict(req, clouds_yaml="/dev/null")
        acct = mock.Mock()
        acct.project.return_value = conn
        with mock.patch.object(S3.S, "Account", return_value=acct):
            out = S3.S.ACTIONS["cluster-fips"](req)
        acct.project.assert_called_with(req["project_id"])
        return out

    def test_present_then_released(self):
        import test_shc_openstack_cluster_fips as S3
        conn = S3.FakeConn()
        present = self.requests("cluster_fips.yml", cb_app_name="shc-oso-acme-c1", cf_project_id=S3.PROJECT,
                                cf_network_id=S3.NET, cf_account="sites/site1",
                                fake_os={"cluster-fips/present": {"api": {"address": "x", "id": "y"},
                                                                  "ingress": {"address": "x", "id": "y"}}})[0]
        out = self.helper(conn, present)
        self.assertEqual(sorted(i.description for i in conn.network.fips), ["shc-oso-acme-c1-api", "shc-oso-acme-c1-ingress"])
        again = self.helper(conn, present)
        self.assertEqual((again["api"]["id"], again["api"]["created"]), (out["api"]["id"], False))
        # the release by the record and by description (a floating IP created by a run that did not record it)
        absent = self.requests("cluster_fips_release.yml", cfr_project_id=S3.PROJECT, cfr_account="sites/site1",
                               cfr_ids=[out["api"]["id"]], cfr_build="shc-oso-acme-c1", cfr_cr_name="c1",
                               cfr_cr_namespace="acme")[0]
        gone = self.helper(conn, absent)
        self.assertEqual(sorted(gone["released"]), sorted([out["api"]["id"], out["ingress"]["id"]]))
        self.assertEqual(conn.network.fips, [])

    def test_the_environment_releases_a_legacy_pair(self):
        import test_shc_openstack_cluster_fips as S3
        text = load(os.path.join(ROLES, "cloudoso_provision", "tasks", "environmentprep.yml"))
        t = next(x for x in walk(text) if (x.get("vars") or {}).get("os_request", {}).get("action") == "cluster-fips")
        conn = S3.FakeConn()
        for d in ("sovereign-env1-api", "sovereign-env1-ingress"):
            conn.network.create_ip(floating_network_id=S3.NET, description=d)
        req = run_play([{"ansible.builtin.set_fact": {"r": t["vars"]["os_request"]}}, write_out("r")],
                       {"ep_os": {"project": {"id": S3.PROJECT}}, "ep_legacy_fip_ids": [], "cloudoso_cr_name": "env1"})
        gone = self.helper(conn, req)
        self.assertEqual(len(gone["released"]), 2)
        self.assertEqual(conn.network.fips, [])


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class RefusedClusterTeardown(unittest.TestCase):
    """eda/common/tasks/teardown_unrealized.yml: a cluster refused by its preflight before anything was built is
    deleted without the central teardown; one that recorded floating IPs or prefixes is not."""

    def nothing_realized(self, status):
        decide = load(os.path.join(ROOT, "eda", "common", "tasks", "teardown_unrealized.yml"))[0]
        cr = {"apiVersion": "hybridsovereign.redhat/v1alpha1", "kind": "PlatformOpenshift",
              "metadata": {"name": "c1", "namespace": "acme"}, "status": status}
        return run_play([decide, write_out("teardown_nothing_realized | bool")], {"shc_live_cr": cr})

    def refused(self, reason, **st):
        return dict({"status": "failed", "ready": False, "reason": reason,
                     "conditions": [{"type": "Ready", "status": "False", "reason": reason, "message": "m"},
                                    {"type": "Preflight", "status": "False", "reason": reason, "message": "m"}]}, **st)

    def test_preflight_refusals(self):
        for reason in ("NetworkNotFound", "RouterAttachmentsOff", "PlacementHasNoIPv4", "PlatformAccessOff",
                       "OutsideAttachableRanges", "AddressConflict", "PrefixReserved", "NetworkChoiceRequired",
                       "ExternalNetworkNotListed"):
            with self.subTest(reason=reason):
                self.assertIs(self.nothing_realized(self.refused(reason)), True)
        # the platform team's reasons and a refusal after the allocation take the full teardown
        self.assertIs(self.nothing_realized(self.refused("CircuitNotDeployed")), False)
        fips = {"network": "net-1", "api": {"address": "192.0.2.15", "id": "a"}, "ingress": {"address": "192.0.2.16", "id": "b"}}
        self.assertIs(self.nothing_realized(self.refused("ServicePrefixInUse", floatingIPs=fips)), False)
        self.assertIs(self.nothing_realized(self.refused("AddressConflict", platformPrefixes=["192.0.2.15/32"])), False)


class RouterAdvertisementSetting(unittest.TestCase):
    """The realization's placement.routerAdvertisementBySite: empty by default, an https:// URL per site, a wrong one
    refused by check-fabric with the setting's name; the placement job reads the same key."""

    SPEC = {"domainAsn": 65010, "vniPool": {"start": 50000, "end": 50999}, "l2vniPool": {"start": 60000, "end": 60999},
            "borderGateway": {"nodes": [{"name": n} for n in NODES]}}

    def errors(self, by):
        r = copy.deepcopy(DEFAULTS)
        r["placement"]["routerAdvertisementBySite"] = by
        return [e for e in F.check_fabric({"spec": self.SPEC, "realization": r})["errors"] if "routerAdvertisement" in e]

    def test_default_and_validation(self):
        self.assertEqual(DEFAULTS["placement"]["routerAdvertisementBySite"], {})
        self.assertEqual(self.errors({}), [])
        self.assertEqual(self.errors({"site-a": {"url": "https://ra.site-a.example.org:8443"}}), [])
        self.assertEqual(self.errors({"site-a": {"url": "http://ra.site-a.example.org"}}),
                         ["realization placement.routerAdvertisementBySite[site-a].url must be an https:// URL, not "
                          "'http://ra.site-a.example.org'"])
        self.assertIn("placement.routerAdvertisementBySite[site-b].url is required", self.errors({"site-b": {}})[0])
        self.assertIn("must be a mapping", self.errors(["https://x.example.org"])[0])


class AttachableRangesRequired(unittest.TestCase):
    """service.attachableRanges is required with the service and 0.0.0.0/0 is the explicit "any": the fabric's
    check, the network job's decision, the fabric's plan and the cluster preflight all read an empty list as "not
    offered", never as "any"."""

    SPEC = {"domainAsn": 65010, "vniPool": {"start": 50000, "end": 50999}, "l2vniPool": {"start": 60000, "end": 60999},
            "borderGateway": {"nodes": [{"name": n} for n in NODES]}}

    def test_empty_is_not_offered_everywhere(self):
        real = realization(attachable=())
        errs = F.check_fabric({"spec": self.SPEC, "realization": real})["errors"]
        self.assertTrue([e for e in errs if "service.attachableRanges is required" in e], errs)
        led = Ledger()
        led.network("acme/n1", 10005, 3)
        d = network_decision("acme/n1", ["192.0.2.15/32"], led, {"circuits": CIRCUIT_STATUS}, real)
        self.assertEqual((d["condition"]["status"], d["condition"]["reason"]), ("False", "ServiceNotOffered"))
        self.assertEqual(d["accepted"], [])
        verdict = F.service_prefix_check({"prefixes": ["192.0.2.15/32"], "space": [SPACE], "ranges": [],
                                          "attachableRanges": []})
        self.assertEqual(verdict["prefixes"][0]["reason"], "OutsideAttachableRanges")
        judged = PlatformReachableAndThePreflight().judge(
            network_decision("acme/n1", [], led, {"circuits": CIRCUIT_STATUS}, realization()), SPACE)
        self.assertEqual(judged["reason"], "")
        fab = {"metadata": {"name": FABRIC}, "status": {"reservedRanges": [], "realization": {"service": {
            "enabled": True, "sources": [HUB], "attachableRanges": [], "hubEndpoints": [], "circuits": CIRCUIT_STATUS}}}}
        net = network_object("acme/n1", [])
        net["status"]["conditions"] = [{"type": "PlatformReachable", "status": "True", "reason": "Attachable"}]
        placement = {"metadata": {"name": "p1", "namespace": "acme"},
                     "spec": {"network": "n1", "backend": {"kind": "CloudOSO", "name": "env1"},
                              "use": {"routerAttachments": True}},
                     "status": {"ready": True, "subnets": [{"family": 4, "prefix": "192.0.2.0/25"}],
                                "use": {"routerAttachments": {"state": "ready"}},
                                "realization": {"site": {"networkId": "net-1", "networkName": "shc-n1"}}}}
        out = P.network({"kind": "CloudOSO", "environment": "env1", "namespace": "acme", "network": "n1",
                         "networkObj": net, "placements": [placement], "fabric": fab, "needPlatform": True,
                         "needRouterAttachments": True, "hubApi": ""})
        self.assertEqual(out["reason"], "ServiceNotOffered")

    def test_any_written_out(self):
        real = realization(attachable=("0.0.0.0/0",))
        self.assertEqual([e for e in F.check_fabric({"spec": self.SPEC, "realization": real})["errors"]
                          if "attachableRanges" in e], [])
        led = Ledger()
        led.network("acme/n1", 10005, 3)
        d = network_decision("acme/n1", ["192.0.2.15/32"], led, {"circuits": CIRCUIT_STATUS}, real)
        self.assertEqual([(r["state"], r["reason"]) for r in d["prefixes"]], [("pending", "")])
        led.apply(d)
        plan = fabric_plan(real, [network_object("acme/n1", d["prefixes"])], led)
        self.assertEqual(plan["errors"], [])
        self.assertEqual(plan["status"]["networks"][0]["prefixes"], ["192.0.2.15/32"])
        self.assertEqual(plan["status"]["attachableRanges"], ["0.0.0.0/0"])


class PlatformSideDown(unittest.TestCase):
    """While the platform's side is down (no platform circuit) the network keeps its clusters' prefixes in place
    (rows pending with the platform's reason, the ledger entry kept, PlatformReachable False CircuitNotDeployed):
    the cluster that holds them waits in its preflight and its wait, it is not refused; a new cluster is refused."""

    def outage(self):
        real, led = realization(), Ledger()
        led.network("acme/n1", 10005, 3)
        up = network_decision("acme/n1", ["192.0.2.15/32"], led, {"circuits": CIRCUIT_STATUS}, real)
        led.apply(up)
        down = network_decision("acme/n1", ["192.0.2.15/32"], led, {"circuits": []}, real)
        led.apply(down)
        return led, down

    def test_rows_keep_their_place(self):
        led, down = self.outage()
        self.assertEqual([(r["state"], r["reason"]) for r in down["prefixes"]], [("pending", "CircuitNotDeployed")])
        self.assertEqual(down["condition"]["reason"], "CircuitNotDeployed")
        self.assertEqual(list(led.of("acme/n1")), ["HybridNetwork/acme/n1/192.0.2.15/32"])
        self.assertEqual(fabric_plan(realization(), [network_object("acme/n1", down["prefixes"])], led)["status"]["networks"], [])

    def judge(self, down, cluster):
        net = network_object("acme/n1", down["prefixes"])
        net["status"]["conditions"] = [dict(down["condition"], lastTransitionTime="2001-01-01T00:00:00Z")]
        real = realization()
        placement = {"metadata": {"name": "p1", "namespace": "acme"},
                     "spec": {"network": "n1", "backend": {"kind": "CloudOSO", "name": "env1"},
                              "use": {"routerAttachments": True}},
                     "status": {"ready": True, "subnets": [{"family": 4, "prefix": "192.0.2.0/25"}],
                                "use": {"routerAttachments": {"state": "ready"}},
                                "realization": {"site": {"networkId": "net-1", "networkName": "shc-n1"}}}}
        fab = {"metadata": {"name": FABRIC},
               "status": {"reservedRanges": F.reserved_ranges(real),
                          "realization": {"service": dict(F.service_settings(real), enabled=True, sources=[HUB],
                                                          attachableRanges=real["service"]["attachableRanges"],
                                                          hubEndpoints=[], circuits=[])}}}
        return P.network({"kind": "CloudOSO", "environment": "env1", "namespace": "acme", "network": "n1",
                          "networkObj": net, "placements": [placement], "fabric": fab, "needPlatform": True,
                          "needRouterAttachments": True, "hubApi": "", "cluster": cluster})

    def test_the_holder_waits_a_new_cluster_is_refused(self):
        _, down = self.outage()
        mine = self.judge(down, "c1")
        self.assertEqual((mine["reason"], mine["wait"]), ("WaitingForPlatformAccess", True))
        self.assertIn("192.0.2.15/32", mine["message"])
        other = self.judge(down, "c2")
        self.assertEqual((other["reason"], other["wait"]), ("CircuitNotDeployed", False))
        wait = P.service_wait({"network": "acme/n1", "prefixes": ["192.0.2.15/32"], "service": {"networks": [], "refused": []},
                               "rows": down["prefixes"]})
        self.assertEqual((wait["done"], wait["reason"]), (False, "WaitingForPlatformAccess"))


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class ClusterPrefixShapes(unittest.TestCase):
    """The prefixes the cluster job really writes (its task expressions: the floating IPs /32, the router's gateway
    /32 after the install, an AWS/GCP Internal cluster's placement subnet), listed by the network job's own task with
    the clusters' type and the placements' subnets, through the network job's decision (cnet-fix1: OpenStack /32 only,
    AWS/GCP exactly a placement subnet, only the accepted address space)."""

    PO = os.path.join(ROLES, "platformopenshift_provision", "tasks")

    def prefixes(self, file, name, v):
        t = by_name(load(os.path.join(self.PO, file)), name)
        return run_play([{"ansible.builtin.set_fact": {"x": t["vars"]["pp_prefixes"]}}, write_out("x")], v)

    def listed(self, clusters, placements):
        t = by_name(load(os.path.join(ROLES, "hybridnetwork_provision", "tasks", "platform_access.yml")),
                    "The clusters on the network")
        v = {"_hn_clusters": {"resources": clusters}, "_hn_placements": {"resources": placements}, "fab_name": "n1",
             "fab_real": {"service": {}}, "hn_fabric": {}}
        return run_play([t, write_out("{'clusters': hn_clusters, 'subnets': hn_placement_subnets}")], v)

    def decide(self, clusters, placements):
        lst = self.listed(clusters, placements)
        real, led = realization(attachable=("0.0.0.0/0",)), Ledger()
        led.network("acme/n1", 10005, 3)
        return PA.platform_access({
            "network": "acme/n1", "fabric": FABRIC, "platformAccess": True, "space": [SPACE], "overlaps": [],
            "hub": [HUB], "service": {"enabled": True, "ports": [6443], "attachableRanges": ["0.0.0.0/0"]},
            "serviceStatus": {"circuits": CIRCUIT_STATUS}, "reserved": F.reserved_ranges(real),
            "clusters": lst["clusters"], "placements": lst["subnets"], "ledger": {}})

    def cluster(self, ty, prefixes, name="c1"):
        return {"metadata": {"name": name, "namespace": "acme"}, "spec": {"type": ty, ty: {"network": "n1"}},
                "status": {"platformPrefixes": prefixes}}

    def placement(self, kind, prefix="192.0.2.0/25"):
        return {"metadata": {"name": "p-" + kind.lower(), "namespace": "acme"},
                "spec": {"network": "n1", "backend": {"kind": kind, "name": "env1"}},
                "status": {"subnets": [{"family": 4, "prefix": prefix}, {"family": 6, "prefix": "2001:db8:1::/64"}]}}

    def test_openstack_floating_ips_and_the_router_gateway(self):
        st = {"resources": [{"status": {"clusterInstalled": False}}]}
        fips = self.prefixes("openstack_cluster_addresses.yml", "Carry the floating IPs as the cluster's prefixes",
                             {"cb_api_vip": "192.0.2.15", "cb_ingress_vip": "192.0.2.16", "po_cr_current": st})
        after = self.prefixes("router_gateway.yml", "Carry the gateway address with the floating IPs",
                              {"cb_api_vip": "192.0.2.15", "cb_ingress_vip": "192.0.2.16", "rg_address": "192.0.2.40"})
        for got in (fips, after):
            d = self.decide([self.cluster("openstack", got)], [self.placement("CloudOSO")])
            self.assertEqual([(r["prefix"], r["state"], r["reason"]) for r in d["prefixes"]],
                             [(p, "pending", "") for p in got])
        self.assertEqual(after, ["192.0.2.15/32", "192.0.2.16/32", "192.0.2.40/32"])
        # a subnet is not an OpenStack cluster's shape
        d = self.decide([self.cluster("openstack", ["192.0.2.0/25"])], [self.placement("CloudOSO")])
        self.assertEqual(d["prefixes"][0]["state"], "refused")

    def test_aws_and_gcp_internal_placement_subnet(self):
        for ty, kind in (("aws", "CloudAWS"), ("gcp", "CloudGCP")):
            with self.subTest(ty=ty):
                place = self.placement(kind)
                machine = P.placement_subnet4(place)   # what the preflight takes as the machine network
                got = self.prefixes("cloud_internal_reach.yml", "Carry the placement's subnet as the cluster's prefix",
                                    {"cb_machine_cidr": machine})
                d = self.decide([self.cluster(ty, got)], [place])
                self.assertEqual([(r["prefix"], r["state"], r["reason"]) for r in d["prefixes"]],
                                 [("192.0.2.0/25", "pending", "")])
                # a /32 or another range is not the placement's subnet
                d = self.decide([self.cluster(ty, ["192.0.2.15/32"])], [place])
                self.assertEqual(d["prefixes"][0]["state"], "refused")


# ---- the UI's reads and the CRD bases -----------------------------------------------------------------------------
BASES = os.path.join(ROOT, "operator", "config", "crd", "bases")


def crd_schema(kind):
    for name in os.listdir(BASES):
        with open(os.path.join(BASES, name)) as fh:
            text = "\n".join(ln for ln in fh.read().splitlines() if "{{" not in ln)
        for doc in yaml.safe_load_all(text):
            if doc and doc.get("kind") == "CustomResourceDefinition" and doc["spec"]["names"]["kind"] == kind:
                v = [x for x in doc["spec"]["versions"] if x.get("storage", True)][0]
                return v["schema"]["openAPIV3Schema"]
    raise KeyError(kind)


def declared(schema, path):
    """The schema node at a dotted path ([] steps into array items), or None when a step is not declared."""
    node = schema
    for step in path.split("."):
        if step == "[]":
            node = node.get("items")
        else:
            props = node.get("properties") or {}
            if step not in props:
                return None if not node.get("x-kubernetes-preserve-unknown-fields") else {}
            node = props[step]
        if node is None:
            return None
    return node


class UiReadsAreDeclared(unittest.TestCase):
    """Every status key the UI reads (types/crds.ts, types/tenantProfile.ts, the network, placement and cluster
    pages, tenantSafe.js's allow-list for clusters) is declared in the CRD bases, with the type the UI expects."""

    READS = {
        "HybridNetwork": [("spec.platformAccess", "boolean"), ("status.platformAccess.state", "string"),
                          ("status.platformAccess.sources.[]", "string"), ("status.platformAccess.ports.[]", "integer"),
                          ("status.platformAccess.prefixes.[].prefix", "string"),
                          ("status.platformAccess.prefixes.[].cluster", "string"),
                          ("status.platformAccess.prefixes.[].state", "string"),
                          ("status.platformAccess.prefixes.[].reason", "string"),
                          ("status.platformAccess.prefixes.[].message", "string"),
                          ("status.platformAccess.reason", "string"), ("status.platformAccess.message", "string")],
        "NetworkPlacement": [("spec.use.virtualMachines", "boolean"), ("spec.use.routerAttachments", "boolean")]
        + [(f"status.use.{u}.{k}", "string") for u in ("virtualMachines", "routerAttachments")
           for k in ("state", "reason", "message")],
        "TenantProfile": [("status.reservedRanges.[].prefix", "string"), ("status.reservedRanges.[].purpose", "string"),
                          ("status.reservedRanges.[].rule", "string"), ("status.reservedRanges.[].site", "string"),
                          ("status.managedClusterRanges.[]", "string"),
                          ("status.sites.[].externalNetworks.[].name", "string"),
                          ("status.sites.[].externalNetworks.[].id", "string"),
                          ("status.sites.[].externalNetworks.[].families.[]", "integer"),
                          ("status.sites.[].externalNetworks.[].default", "boolean")],
        "PlatformOpenshift": [("spec.openstack.network", "string"), ("spec.openstack.externalNetwork", "string"),
                              ("spec.aws.network", "string"), ("spec.aws.publish", "string"),
                              ("spec.gcp.network", "string"), ("spec.gcp.publish", "string"),
                              ("spec.networking.clusterNetwork", "string"), ("spec.networking.hostPrefix", "integer"),
                              ("spec.networking.serviceNetwork", "string"), ("spec.networking.machineNetwork", "string"),
                              ("spec.aws.controlPlaneCount", "integer"), ("spec.aws.workerCount", "integer"),
                              ("spec.gcp.controlPlaneCount", "integer"), ("spec.gcp.workerCount", "integer"),
                              ("spec.openstack.controlPlaneCount", "integer"), ("spec.openstack.workerCount", "integer"),
                              ("status.buildName", "string"), ("status.network", "string"),
                              ("status.externalNetwork", "string"), ("status.placement", "string"),
                              ("status.publish", "string"), ("status.machineNetwork", "string"),
                              ("status.clusterNetwork", "string"), ("status.serviceNetwork", "string"),
                              ("status.platformPrefixes.[]", "string"), ("status.floatingIPs.network", "string"),
                              ("status.floatingIPs.api.address", "string"), ("status.floatingIPs.ingress.id", "string")],
        "CloudAWS": [("status.awsRegion", "string")],
        "CloudGCP": [("status.region", "string")],
    }

    def test_declared_with_their_types(self):
        missing = []
        for kind, reads in self.READS.items():
            schema = crd_schema(kind)
            for path, typ in reads:
                node = declared(schema, path)
                if node is None:
                    missing.append(f"{kind} {path}: not declared")
                elif node and node.get("type") not in (typ, None):
                    missing.append(f"{kind} {path}: {node.get('type')}, the UI reads {typ}")
        self.assertEqual(missing, [])

    def test_tenant_safe_keeps_the_cluster_keys(self):
        with open(os.path.join(ROOT, "ui", "packages", "shared", "server", "tenantSafe.js")) as fh:
            text = fh.read()
        block = text[text.index("PlatformOpenshift: ["):]
        block = block[:block.index("],\n};")]
        for key in ("buildName", "network", "externalNetwork", "placement", "publish", "machineNetwork",
                    "clusterNetwork", "serviceNetwork", "platformPrefixes", "floatingIPs"):
            self.assertIn(f"'{key}'", block)


if __name__ == "__main__":
    unittest.main()
