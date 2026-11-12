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
RANGE = "198.51.100.0/28"
NODES = ["bgw-1", "bgw-2"]
FABRIC, PLAT = "lab", "platform"
SPACE = "192.0.2.0/24"
CIRCUIT_STATUS = [{"circuit": f"{PLAT}/hub-a", "vlan": 3099, "nodes": NODES, "rev": "r1"}]


def realization(attachable=(SPACE,), rng=(RANGE,)):
    r = copy.deepcopy(DEFAULTS)
    r["attachments"]["enabled"] = True
    r["infrastructure"] = {"hub": [HUB]}
    r["service"].update({"enabled": True, "attachableRanges": list(attachable), "hubEndpoints": [], "range": list(rng)})
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
                    "range": real["service"].get("range") or [],
                    "attachableRanges": real["service"].get("attachableRanges") or []},
        "serviceStatus": service_status, "reserved": F.reserved_ranges(real),
        # a network off with no cluster on it (a cluster that names it holds platform access on)
        "clusters": [{"name": "c1", "prefixes": list(prefixes), "deleting": False}] if platform_access or prefixes else [],
        "ledger": {o: v for o, v in ledger.alloc["servicePrefix"].items()}})


def network_object(key, rows, space=SPACE):
    ns, name = key.split("/")
    return {"metadata": {"namespace": ns, "name": name}, "spec": {"prefixes": [space]},
            "status": {"fabric": FABRIC, "addressSpace": [{"prefix": space, "family": 4}],
                       "platformAccess": {"prefixes": rows}}}


def fabric_plan(real, networks, ledger, previous=None, sites=None):
    return F.service_plan({"realization": real, "nodes": NODES, "fabric": FABRIC, "domainAsn": 65010,
                           "networks": networks, "ledger": ledger.alloc, "circuits": [platform_circuit_plan_entry()],
                           "vrfs": {n: [] for n in NODES}, "previous": previous or {}, "sites": sites or []})


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
                                                                    "range": [RANGE],
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


def placement_obj():
    return {"metadata": {"name": "p1", "namespace": "acme"},
            "spec": {"network": "n1", "backend": {"kind": "CloudOSO", "name": "env1"}, "use": {"routerAttachments": True}},
            "status": {"ready": True, "subnets": [{"family": 4, "prefix": "192.0.2.0/25"}],
                       "use": {"routerAttachments": {"state": "ready"}},
                       "realization": {"site": {"networkId": "net-1", "networkName": "shc-n1"}}}}


def preflight_on_the_plan(real, plan, decision, hub_api="", cluster="c2"):
    """The cluster preflight (po_preflight.py network) reading the fabric's status as service-plan wrote it and the
    network's condition as platform-access wrote it."""
    net = network_object("acme/n1", decision["prefixes"])
    net["status"]["conditions"] = [dict(decision["condition"], lastTransitionTime="2001-01-01T00:00:00Z")]
    fab = {"metadata": {"name": FABRIC}, "status": {"reservedRanges": F.reserved_ranges(real),
                                                    "realization": {"service": plan["status"]}}}
    return P.network({"kind": "CloudOSO", "environment": "env1", "namespace": "acme", "network": "n1",
                      "networkObj": net, "placements": [placement_obj()], "fabric": fab, "needPlatform": True,
                      "needRouterAttachments": True, "hubApi": hub_api, "cluster": cluster})


class AttachableRangesOptional(unittest.TestCase):
    """service.attachableRanges is optional: absent or empty = any range the platform does not refuse, the same in the
    fabric's check, the network job's decision, the fabric's plan, the per-prefix verdict, the cluster preflight and
    what the platform circuit tells the hub's router; set, it restricts on every side alike."""

    SPEC = {"domainAsn": 65010, "vniPool": {"start": 50000, "end": 50999}, "l2vniPool": {"start": 60000, "end": 60999},
            "borderGateway": {"nodes": [{"name": n} for n in NODES]}}

    def round_trip(self, real, prefix, space=SPACE):
        led = Ledger()
        led.network("acme/n1", 10005, 3)
        d = network_decision("acme/n1", [prefix], led, {"circuits": CIRCUIT_STATUS}, real, space=space)
        led.apply(d)
        plan = fabric_plan(real, [network_object("acme/n1", d["prefixes"], space=space)], led)
        return d, plan

    def test_absent_or_empty_means_any_everywhere(self):
        for att in ((), None, ("0.0.0.0/0",)):
            with self.subTest(attachableRanges=att):
                real = realization(attachable=att or ())
                if att is None:
                    del real["service"]["attachableRanges"]
                self.assertEqual(F.check_fabric({"spec": self.SPEC, "realization": real})["errors"], [])
                d, plan = self.round_trip(real, "203.0.113.15/32", space="203.0.113.0/24")
                self.assertEqual(d["condition"]["reason"], "AttachPending")
                self.assertEqual([(r["state"], r["reason"]) for r in d["prefixes"]], [("pending", "")])
                self.assertEqual(plan["errors"], [])
                self.assertEqual(plan["status"]["networks"][0]["prefixes"], ["203.0.113.15/32"])
                verdict = F.service_prefix_check({"prefixes": ["203.0.113.15/32"], "space": ["203.0.113.0/24"],
                                                  "realization": real})
                self.assertEqual(verdict["prefixes"][0]["reason"], "")
                ce = F.check_circuit({"ceKind": "platform", "namespace": PLAT, "fabricNamespace": PLAT,
                                      "realization": real})["ceConfig"]
                self.assertEqual(ce["advertisedRanges"], ["0.0.0.0/0 le 32"])
                self.assertEqual(plan["nodes"]["bgw-1"]["attachable"], ce["advertisedRanges"])
                pf = preflight_on_the_plan(real, plan, d)
                self.assertEqual(pf["reason"], "", pf["message"])

    def test_the_reserved_ranges_still_refuse_without_them(self):
        real = realization(attachable=())
        d, plan = self.round_trip(real, "198.51.100.20/32", space="198.51.100.0/24")
        self.assertEqual(d["prefixes"][0]["reason"], "PrefixReserved")
        self.assertEqual(plan["status"]["networks"], [])

    def test_set_they_restrict_on_every_side(self):
        real = realization(attachable=("192.0.2.0/26",))
        d, plan = self.round_trip(real, "192.0.2.99/32")
        self.assertEqual((d["prefixes"][0]["state"], d["prefixes"][0]["reason"]), ("refused", "OutsideAttachableRanges"))
        self.assertEqual(plan["status"]["networks"], [])
        verdict = F.service_prefix_check({"prefixes": ["192.0.2.99/32"], "space": [SPACE], "realization": real})
        self.assertEqual(verdict["prefixes"][0]["reason"], "OutsideAttachableRanges")
        # the preflight: the placement's subnet 192.0.2.0/25 is not inside 192.0.2.0/26
        pf = preflight_on_the_plan(real, fabric_plan(real, [], Ledger()), network_decision(
            "acme/n1", [], Ledger(), {"circuits": CIRCUIT_STATUS}, real))
        self.assertEqual(pf["reason"], "OutsideAttachableRanges")
        ce = F.check_circuit({"ceKind": "platform", "namespace": PLAT, "fabricNamespace": PLAT,
                              "realization": real})["ceConfig"]
        self.assertEqual(ce["advertisedRanges"], ["192.0.2.0/26 le 32"])


class ServiceRange(unittest.TestCase):
    """service.range on every side: the fabric's check names it, the plan carries it in status (range, sources), the
    network job reads it (the real task's request), the preflight's hub endpoint check reads the plan's status, the
    platform circuit job (the real tasks, through fabric_check.yml) tells the hub's router exactly what the nodes
    accept and announce."""

    def test_the_network_job_sends_the_range(self):
        if not HAVE_ANSIBLE:
            self.skipTest("ansible-playbook not installed")
        t = by_name(load(os.path.join(ROLES, "hybridnetwork_provision", "tasks", "platform_access.yml")),
                    "Decide the network's platform access")
        real = realization(attachable=())
        v = {"fab_namespace": "acme", "fab_name": "n1", "hn_fabric_name": FABRIC, "fab_spec": {"prefixes": [SPACE]},
             "hn_space": [], "hn_hub_check": {"overlaps": []}, "hn_hub": [HUB], "hn_service_real": real["service"],
             "hn_service_status": {"circuits": CIRCUIT_STATUS}, "hn_fabric": {"status": {"reservedRanges": F.reserved_ranges(real)}},
             "hn_clusters": [{"name": "c1", "prefixes": ["192.0.2.15/32"]}], "hn_placement_subnets": [],
             "hn_service_ledger": {}}
        sent = run_play([{"ansible.builtin.set_fact": {"x": t["ansible.builtin.command"]["stdin"]}}, write_out("x")], v)
        sent = sent if isinstance(sent, dict) else json.loads(sent)
        self.assertEqual(sent["service"]["range"], [RANGE])
        self.assertEqual(sent["service"]["attachableRanges"], [])
        out = PA.platform_access(sent)
        self.assertEqual(out["sources"], [RANGE])
        self.assertEqual(out["accepted"], ["192.0.2.15/32"])

    def test_the_plan_status_and_the_preflight(self):
        real = realization()
        real["service"]["hubEndpoints"] = [{"prefix": "198.51.100.10/32", "ports": [6443]}]
        plan = fabric_plan(real, [], Ledger())
        self.assertEqual((plan["status"]["range"], plan["status"]["sources"]), ([RANGE], [RANGE]))
        d = network_decision("acme/n1", [], Ledger(), {"circuits": CIRCUIT_STATUS}, real)
        self.assertEqual(d["sources"], plan["status"]["sources"])
        # the hub's API inside the service range: admitted by an endpoint, or refused; outside it (in the hub's range):
        # not through the service VRF, no endpoint needed
        self.assertEqual(preflight_on_the_plan(real, plan, d, hub_api="https://198.51.100.10:6443")["reason"], "")
        self.assertEqual(preflight_on_the_plan(real, plan, d, hub_api="https://198.51.100.9:6443")["reason"],
                         "HubEndpointNotAdmitted")
        self.assertEqual(preflight_on_the_plan(real, plan, d, hub_api="https://198.51.100.99:6443")["reason"], "")

    def test_the_hub_router_is_told_what_the_nodes_do(self):
        if not HAVE_ANSIBLE:
            self.skipTest("ansible-playbook not installed")
        tasks = load(os.path.join(ROLES, "attachmentcircuit_provision", "tasks", "platform_circuit.yml"))
        picked = [copy.deepcopy(t) for t in tasks if t.get("name") in ("The hub router's filters",
                                                                        "Keep the hub router's filters")]
        for att, mp in (((), 1000), ((SPACE,), 1)):
            with self.subTest(attachableRanges=att):
                real = realization(attachable=att)
                real["service"]["maxPrefixes"] = mp
                v = {"role_path": os.path.join(ROLES, "attachmentcircuit_provision"), "fab_namespace": PLAT,
                     "fabric_platform_namespace": PLAT, "fab_real": real}
                ce = run_play(picked + [write_out("ac_platform_ce")], v)
                led = Ledger()
                led.network("acme/n1", 10005, 3)
                led.alloc["servicePrefix"] = {"HybridNetwork/acme/n1/192.0.2.15/32": 1,
                                              "HybridNetwork/acme/n1/192.0.2.16/32": 2}
                rows = [{"prefix": p, "cluster": "c1", "state": "pending"} for p in ("192.0.2.15/32", "192.0.2.16/32")]
                plan = fabric_plan(real, [network_object("acme/n1", rows)], led)
                n = plan["nodes"]["bgw-1"]
                # what the nodes accept from the hub's router = what it is told to announce; what they guard their own
                # announcement with = what it is told to refuse, then accept; never more prefixes than its limit
                self.assertEqual(ce["acceptedRanges"], n["accept"])
                self.assertEqual(ce["refusedRanges"], n["refuse"])
                self.assertEqual(ce["advertisedRanges"], n["attachable"])
                self.assertEqual(ce["maxPrefixes"], mp)
                self.assertLessEqual(len(n["advertise"]), ce["maxPrefixes"])


class ServiceRefusalSet(unittest.TestCase):
    """Review R4 H1: one set of ranges a service prefix may not overlap (shc_fabric.service_refusal_rows), read by the
    planner's verdict, the node guard (the root's service.refuse, SVC-ATTACHABLE), the hub router's
    ceConfig.refusedRanges (the platform circuit's real tasks, from the fabric's published rows) and the network job
    (the published rows): byte for byte the same list, the same verdict per prefix."""

    SITES = [{"name": "site-a", "kind": "openstack", "ranges": ["10.30.0.0/16"]},
             {"name": "site-b", "kind": "openshift", "ranges": ["10.40.0.0/16"]}]

    def real(self):
        r = realization(attachable=())
        r["egress"].update({"enabled": True, "parentInterface": "bond1.100", "gateway": "203.0.113.1",
                            "prefixLength": 24, "linkPool": "169.254.0.0/16",
                            "nodes": {"bgw-1": {"address": "203.0.113.11", "natPool": "203.0.113.64/26"},
                                      "bgw-2": {"address": "203.0.113.12", "natPool": "203.0.113.128/26"}}})
        return r

    def test_the_lists_are_one(self):
        if not HAVE_ANSIBLE:
            self.skipTest("ansible-playbook not installed")
        real = self.real()
        published = F.reserved_ranges(real, self.SITES)
        plan = fabric_plan(real, [], Ledger(), sites=self.SITES)
        guard = plan["nodes"]["bgw-1"]["refuse"]
        tasks = load(os.path.join(ROLES, "attachmentcircuit_provision", "tasks", "platform_circuit.yml"))
        picked = [copy.deepcopy(t) for t in tasks if t.get("name") in ("The hub router's filters",
                                                                        "Keep the hub router's filters")]
        ce = run_play(picked + [write_out("ac_platform_ce")],
                      {"role_path": os.path.join(ROLES, "attachmentcircuit_provision"), "fab_namespace": PLAT,
                       "fabric_platform_namespace": PLAT, "fab_real": real,
                       "ac_fabric": {"status": {"reservedRanges": published}}})
        verdict = [F.prefix_entry(r[0]) for r in F.service_rows(real, self.SITES)]
        self.assertEqual(json.dumps(ce["refusedRanges"]), json.dumps(guard))
        self.assertEqual(guard, ["0.0.0.0/0"] + verdict)
        for want in ("10.30.0.0/16 le 32", "10.40.0.0/16 le 32", "203.0.113.64/26 le 32", "127.0.0.0/8 le 32"):
            self.assertIn(want, guard)

    def test_one_verdict_per_prefix(self):
        real = self.real()
        published = F.reserved_ranges(real, self.SITES)
        for prefix, space, want in (("10.40.0.20/32", "10.40.0.0/24", "PrefixReserved"),
                                    ("10.30.5.5/32", "10.30.5.0/24", "PrefixReserved"),
                                    ("203.0.113.70/32", "203.0.113.64/27", "PrefixReserved"),
                                    ("240.0.0.9/32", "240.0.0.0/24", "PrefixReserved"),
                                    ("192.0.2.15/32", SPACE, "")):
            with self.subTest(prefix=prefix):
                led = Ledger()
                led.network("acme/n1", 10005, 3)
                d = PA.platform_access({
                    "network": "acme/n1", "fabric": FABRIC, "platformAccess": True, "space": [space], "overlaps": [],
                    "hub": [HUB], "service": {"enabled": True, "ports": [6443], "range": [RANGE], "attachableRanges": []},
                    "serviceStatus": {"circuits": CIRCUIT_STATUS}, "reserved": published,
                    "clusters": [{"name": "c1", "prefixes": [prefix]}], "ledger": {}})
                self.assertEqual(d["prefixes"][0]["reason"], want, d["prefixes"][0]["message"])
                # the plan, on a row the network accepted, says the same
                led.alloc["servicePrefix"][f"HybridNetwork/acme/n1/{prefix}"] = 1
                plan = fabric_plan(real, [network_object("acme/n1", [{"prefix": prefix, "cluster": "c1", "state": "pending"}],
                                                          space=space)], led, sites=self.SITES)
                got = plan["status"]["refused"][0]["reason"] if plan["status"]["refused"] else ""
                self.assertEqual(got, want)
                verdict = F.service_prefix_check({"prefixes": [prefix], "space": [space], "ranges": published})
                self.assertEqual(verdict["prefixes"][0]["reason"], want)


class UpgradeWithTheServiceOn(unittest.TestCase):
    """A deployment that had the service on with attachableRanges set keeps what it had once service.range is set
    (compared with this round's base, 8acb5037); one with the service on and no service.range is refused by name on
    every side."""

    BASE = "8acb5037"

    def base(self):
        import subprocess
        import tempfile
        try:
            text = subprocess.run(["git", "-C", ROOT, "show", f"{self.BASE}:eda/common/files/shc_fabric.py"],
                                  capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            text = None
        if text is None or text.returncode != 0:
            self.skipTest(f"{self.BASE} not readable from Git")
        path = os.path.join(tempfile.mkdtemp(prefix="shc-fabric-8acb-"), "shc_fabric_8acb.py")
        with open(path, "w") as f:
            f.write(text.stdout)
        return _module("shc_fabric_8acb", path)

    def test_attachable_ranges_set_keep_working(self):
        old = self.base()
        nets, led = [], Ledger()
        for i, (key, prefixes) in enumerate((("acme/n1", ["192.0.2.15/32", "192.0.2.16/32"]),
                                             ("acme/n2", ["192.0.2.15/32", "192.0.2.40/32"]),
                                             ("acme/n3", ["192.0.2.200/32", "198.51.100.7/32"]))):
            led.network(key, 10005 + i, 3 + i)
            for p in prefixes:
                led.alloc["servicePrefix"][f"HybridNetwork/{key}/{p}"] = len(led.alloc["servicePrefix"]) + 1
            nets.append(network_object(key, [{"prefix": p, "cluster": "c1", "state": "pending"} for p in prefixes]))
        before = realization(attachable=("192.0.2.0/25",))
        del before["service"]["range"]
        req = {"nodes": NODES, "fabric": FABRIC, "domainAsn": 65010, "networks": nets, "ledger": led.alloc,
               "circuits": [platform_circuit_plan_entry()], "vrfs": {n: [] for n in NODES}, "previous": {}}
        was = old.service_plan(dict(req, realization=before))
        self.assertEqual(was["errors"], [])
        # the same deployment with the range set to its hub: the same prefixes realized and refused, the same routes
        # taken from the hub's routers
        after = copy.deepcopy(before)
        after["service"]["range"] = [HUB]
        now = F.service_plan(dict(req, realization=after))
        self.assertEqual(now["errors"], [])
        self.assertEqual(now["status"]["networks"], was["status"]["networks"])
        self.assertEqual(now["status"]["refused"], was["status"]["refused"])
        self.assertTrue(now["status"]["refused"])
        self.assertEqual(now["nodes"]["bgw-1"]["accept"], was["nodes"]["bgw-1"]["accept"])
        self.assertEqual(now["nodes"]["bgw-1"]["advertise"], was["nodes"]["bgw-1"]["advertise"])
        self.assertEqual(now["nodes"]["bgw-1"]["attachable"], was["nodes"]["bgw-1"]["attachable"])
        self.assertEqual(now["nodes"]["bgw-1"]["networks"], was["nodes"]["bgw-1"]["networks"])

    def test_no_range_is_refused_by_name(self):
        real = realization()
        del real["service"]["range"]
        errs = F.check_fabric({"spec": AttachableRangesOptional.SPEC, "realization": real})["errors"]
        self.assertTrue([e for e in errs if e.startswith("realization service.range is required with service.enabled")],
                        errs)
        plan = fabric_plan(real, [], Ledger())
        self.assertTrue([e for e in plan["errors"] if "service.range" in e])
        self.assertEqual(plan["nodes"], {})
        d = network_decision("acme/n1", ["192.0.2.15/32"], Ledger(), {"circuits": CIRCUIT_STATUS}, real)
        self.assertEqual(d["condition"]["reason"], "ServiceNotOffered")
        self.assertIn("service.range", d["condition"]["message"])
        # a fabric status written before the range (sources, no range): the preflight refuses a new cluster by name
        old_status = {k: v for k, v in fabric_plan(realization(), [], Ledger())["status"].items() if k != "range"}
        pf = preflight_on_the_plan(real, {"status": old_status},
                                   network_decision("acme/n1", [], Ledger(), {"circuits": CIRCUIT_STATUS}, realization()))
        self.assertEqual(pf["reason"], "ServiceNotOffered")
        self.assertIn("service.range", pf["message"])


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
            "hub": [HUB], "service": {"enabled": True, "ports": [6443], "range": [RANGE], "attachableRanges": ["0.0.0.0/0"]},
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


class ServiceSessionsAndTheTemplate(unittest.TestCase):
    """With a cluster present, the rendered root configuration has two kinds of BGP neighbours in the service VRF:
    the platform circuit's CE (eBGP) and one iBGP neighbour per network with clusters (its service link). The health
    reader judges ServiceSessionsDown on the CEs root.json names, which are exactly the CE neighbours the template
    renders; the networks' iBGP sessions are not counted there (they are checked at apply time by the readiness
    check, which names both). The cluster side reads nothing of the network VRF's routes."""

    def setUp(self):
        import test_service_vrf as V
        self.V = V
        real, (egp, att, vrfs, svc) = V.service_setup()
        self.out = V.T.render(V.T.RT, V.template_vars(real, egp, att, vrfs, svc, "bgw-1", True))
        self.conf = V.service_conf(real, egp, att, svc, "bgw-1")

    def neighbours(self):
        inst = self.out.split("router bgp 65010 vrf service\n")[1].split("\nexit\n")[0]
        ce, ibgp = [], []
        for line in inst.splitlines():
            parts = line.split()
            if len(parts) == 4 and parts[0] == "neighbor" and parts[2] == "remote-as":
                (ibgp if parts[3] == "65010" else ce).append(parts[1])
        return ce, ibgp

    def summary(self, states):
        return json.dumps({"ipv4Unicast": {"peers": {a: {"state": st} for a, st in states.items()}}})

    def test_the_health_reader_counts_the_ces_the_template_renders(self):
        import importlib.util as iu
        spec = iu.spec_from_file_location("shc_node_health", os.path.join(ROOT, "eda", "common", "files", "shc_node_health.py"))
        NH = iu.module_from_spec(spec)
        spec.loader.exec_module(NH)
        ce, ibgp = self.neighbours()
        self.assertTrue(ce and ibgp, self.out)
        self.assertEqual(sorted(str(c["ce"]) for c in self.conf["service"]["circuits"]), sorted(ce))
        # CE up, the networks' sessions down: the platform circuit is healthy (the network sessions are not its)
        states = dict({a: "Established" for a in ce}, **{a: "Idle" for a in ibgp})
        got = NH.service_sessions(lambda argv: (0, self.summary(states)), self.conf)
        self.assertEqual((got["established"], got["known"]), (len(ce), len(ce)))
        self.assertEqual(sorted(r["address"] for r in got["peers"]), sorted(ce))
        # CE down: counted as down, whatever the networks' sessions say
        states = dict({a: "Active" for a in ce}, **{a: "Established" for a in ibgp})
        got = NH.service_sessions(lambda argv: (0, self.summary(states)), self.conf)
        self.assertEqual(got["established"], 0)

    def test_the_health_reader_reads_each_networks_session(self):
        # Review R3 N6: the networks' iBGP sessions (the service VRF's end of each service link) are read per network
        import importlib.util as iu
        spec = iu.spec_from_file_location("shc_node_health", os.path.join(ROOT, "eda", "common", "files", "shc_node_health.py"))
        NH = iu.module_from_spec(spec)
        spec.loader.exec_module(NH)
        jspec = iu.spec_from_file_location("shc_fabric_health", os.path.join(ROOT, "eda", "common", "files", "shc_fabric_health.py"))
        J = iu.module_from_spec(jspec)
        jspec.loader.exec_module(J)
        ce, ibgp = self.neighbours()
        states = dict({a: "Established" for a in ce}, **{a: "Idle" for a in ibgp})
        got = NH.service_sessions(lambda argv: (0, self.summary(states)), self.conf)
        nets = self.conf["service"]["networks"]
        self.assertEqual(sorted(l["address"] for l in got["links"]), sorted(ibgp))
        self.assertEqual(sorted(l["network"] for l in got["links"]), sorted(w["network"] for w in nets))
        self.assertTrue(all(l["state"] == "Idle" for l in got["links"]))
        cfg = J.settings({"checks": {k: k == "serviceSessions" for k in J.CHECKS}})
        found = J.instance_findings({"serviceSessions": got}, "root", cfg)
        self.assertEqual([f[0] for f in found], ["ServiceLinkDown"])
        # sessions missing from the summary count as down; all established: nothing
        got = NH.service_sessions(lambda argv: (0, self.summary({a: "Established" for a in ce})), self.conf)
        self.assertTrue(all(l["state"] == "Missing" for l in got["links"]))
        got = NH.service_sessions(lambda argv: (0, self.summary(dict({a: "Established" for a in ce + ibgp}))), self.conf)
        self.assertEqual(J.instance_findings({"serviceSessions": got}, "root", cfg), [])

    def test_the_readiness_check_names_both_kinds(self):
        with open(os.path.join(ROLES, "hybridfabric_provision", "tasks", "node_root.yml")) as fh:
            text = fh.read()
        self.assertIn("show bgp vrf {{ hf_node_service.vrf }} neighbors {{ c.ce }} json", text)
        self.assertIn("show bgp vrf {{ hf_node_service.vrf }} neighbors {{ w.seIp }} json", text)
        self.assertIn("show bgp vrf {{ w.vrf }} neighbors {{ w.soIp }} json", text)
        ce, ibgp = self.neighbours()
        # the template's network-side neighbour is the service link's other end (so<t> in the service VRF)
        nets = self.conf["service"]["networks"]
        self.assertEqual(sorted(w["seIp"] for w in nets), sorted(ibgp))

    def test_no_static_hub_route_the_cluster_side_could_rely_on(self):
        self.assertNotIn("SHC-VRF-STATIC", self.out)
        for role in ("platformopenshift_provision", "platformopenshift_teardown"):
            for dirpath, _, files in os.walk(os.path.join(ROLES, role)):
                for f in files:
                    with open(os.path.join(dirpath, f), errors="replace") as fh:
                        body = fh.read()
                    for word in ("SHC-VRF-STATIC", "SVC-STATIC", "ip route", "show ip route"):
                        self.assertNotIn(word, body, f"{role}/{f}")


class ClusterJobNamesAreKnown(unittest.TestCase):
    """cnet-fix's new names reach every reader: each reason the cluster preflight helper and the name check return is
    registered; the hub label is spelled the same by the claim, the preflight's owner check and the teardown; the
    site template's ROUTE_ALLOWED_SOURCES is required and documented."""

    def test_preflight_reasons_registered(self):
        import re
        sys.path.insert(0, os.path.join(ROOT, "eda", "rulebooks", "collections", "ansible_collections", "hybridsovereign",
                                        "shc", "plugins", "module_utils"))
        import shc_status_reasons as R
        texts = []
        for rel in (("files", "po_preflight.py"), ("tasks", "preflight_network_name.yml")):
            with open(os.path.join(ROLES, "platformopenshift_provision", *rel)) as fh:
                texts.append(fh.read())
        found = set()
        for t in texts:
            found |= set(re.findall(r'result\(\s*"([A-Z][A-Za-z]+)"', t))
            found |= set(re.findall(r"ps_reason:\s*([A-Z][A-Za-z]+)", t))
        found |= {v for v in P.PLATFORM_REASONS.values()} | P.WAITING
        self.assertIn("NetworkNameNotUnique", texts[1])
        missing = sorted(r for r in found if R.lookup(r) is None)
        self.assertEqual(missing, [])
        self.assertIsNotNone(R.lookup("NetworkNameNotUnique"))

    def test_hub_label_spelled_once(self):
        label = "hybridsovereign.redhat/platformopenshift-uid"
        for rel in (("platformopenshift_provision", "tasks", "claim_build.yml"),
                    ("platformopenshift_provision", "files", "po_preflight.py"),
                    ("platformopenshift_teardown", "tasks", "hub_ownership.yml")):
            with open(os.path.join(ROLES, *rel)) as fh:
                self.assertIn(label, fh.read(), "/".join(rel))
        with open(os.path.join(ROOT, "docs", "platform", "platform-access.md")) as fh:
            self.assertIn(label, fh.read())

    def test_route_allowed_sources(self):
        base = os.path.join(ROOT, "site-components", "router-advertisement", "deploy", "rhoso")
        with open(os.path.join(base, "router-advertisement.yaml")) as fh:
            doc = yaml.safe_load(fh)
        params = {p["name"]: p for p in doc.get("parameters") or []}
        self.assertTrue(params["ROUTE_ALLOWED_SOURCES"].get("required"))
        for rel in (os.path.join(base, "README.md"), os.path.join(ROOT, "docs", "platform", "router-advertisement-service.md")):
            with open(rel) as fh:
                self.assertIn("ROUTE_ALLOWED_SOURCES", fh.read(), rel)


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
                              ("status.installStarted", "boolean"),
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
                    "clusterNetwork", "serviceNetwork", "platformPrefixes", "floatingIPs", "installStarted"):
            self.assertIn(f"'{key}'", block)


if __name__ == "__main__":
    unittest.main()
