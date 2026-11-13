"""AttachmentCircuit of ceKind platform: the circuit job's branch (attachmentcircuit_provision platform_circuit.yml and
the record in main.yml), its teardown, and the ProviderEdge's capabilities.

A platform circuit hands the fabric's service routing domain to the router of the network the platform's management
runs on: only in the platform namespace (a tenant cannot create one: the job refuses NotPlatformNamespace elsewhere),
only while the realization offers the service with its range, IPv4 only, no network; it accepts the realization's
service.range and advertises the clusters' prefixes, and status.ceConfig gives the hub's router its filters as
files/shc_fabric.py check-circuit computes them (announce the range, refuse the published ranges, accept the
attachable ranges or any other IPv4 prefix up to /32, a maximum-prefix figure).
Customer circuits are untouched: same record and revision as the release before, same policy request when
infrastructure.hub holds what egress.platformSources held. The fabric's side (status.realization.service.circuits) is
a fake. Documentation ranges and example names only."""
import copy
import os
import subprocess
import sys
import unittest

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, walk, write_out  # noqa: E402

ROLES = os.path.join(ROOT, "eda", "rulebooks", "roles")
AC = os.path.join(ROLES, "attachmentcircuit_provision", "tasks")
ACD = os.path.join(ROLES, "attachmentcircuit_teardown", "tasks")
PE = os.path.join(ROLES, "provideredge_provision", "tasks")
CRD = os.path.join(ROOT, "operator", "config", "crd", "bases", "crd-attachmentcircuit.yaml")
PE_CRD = os.path.join(ROOT, "operator", "config", "crd", "bases", "crd-provideredge.yaml")
BASE = "669b781a"
SAMPLES = os.path.join(ROOT, "samples", "attachmentcircuit")

HUB = ["192.0.2.0/24", "198.51.100.17/32", "2001:db8:f::/48"]
SERVICE = {"enabled": True, "vrf": {"name": "service", "table": 9001}, "ports": [6443, 443],
           "range": ["192.0.2.0/28", "198.51.100.17/32"], "attachableRanges": ["10.64.0.0/12"]}
sys.path.insert(0, os.path.join(ROOT, "eda", "common", "files"))
import shc_fabric as F  # noqa: E402

# the special-purpose IPv4 space (Review R4 H1), never announced: refused at the hub's router after the realization's
SPECIAL = ["0.0.0.0/8 le 32", "127.0.0.0/8 le 32", "169.254.0.0/16 le 32", "224.0.0.0/4 le 32", "240.0.0.0/4 le 32"]
PLAN = {"link": "169.254.200.0/29", "ce": "169.254.200.3", "ceSecond": "169.254.200.4", "prefixLength": 29,
        "nodes": [{"name": "node-1", "address": "169.254.200.1"}, {"name": "node-2", "address": "169.254.200.2"}],
        "errors": []}


def base_text(rel):
    try:
        p = subprocess.run(["git", "-C", ROOT, "show", f"{BASE}:{rel}"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def edge(bearer_kinds=("customer", "platform"), ready=True):
    return {"metadata": {"name": "edge-a", "namespace": "platform-ns"},
            "spec": {"fabricRef": {"name": "fab"}, "bearers": [{"name": "b1", "ceKinds": list(bearer_kinds)}]},
            "status": {"ready": ready, "status": "ready" if ready else "pending"}}


def spec(**kw):
    s = {"ceKind": "platform", "edgeRef": {"name": "edge-a", "bearer": "b1"},
         "routing": {"protocol": "bgp", "bgp": {"peerAsn": 64512}}}
    s.update(kw)
    return s


def front(namespace="platform-ns", e=None, real=None, sp=None, fabric_status=None):
    """platform_circuit.yml with the API reads and includes replaced; returns the refusal or the facts."""
    out = []
    for t in load(os.path.join(AC, "platform_circuit.yml")):
        n = t.get("name")
        if n == "Read the edge":
            t = {"name": n, "ansible.builtin.set_fact": {"_ac_edge": {"resources": [e if e is not None else edge()]}}}
        elif n == "Read the platform circuit's fabric":
            t = {"name": n, "ansible.builtin.set_fact": {"_ac_fabric": {"resources": [
                dict({"metadata": {"name": "fab"}, "spec": {"domainAsn": 65010}},
                     **({"status": fabric_status} if fabric_status else {}))]}}}
        elif n == "The platform's realization parameters for the platform circuit":
            t = {"name": n, "ansible.builtin.set_fact": {"fab_real": real if real is not None else
                                                         {"service": SERVICE, "infrastructure": {"hub": HUB},
                                                          "attachments": {"reservedAsns": [64999]}}}}
        elif n == "End the job while the edge isn't usable":
            t = dict({k: v for k, v in t.items() if k != "ansible.builtin.meta"}, **{"ansible.builtin.fail": {"msg": "ended"}})
        elif n == "Report what the platform circuit waits for":
            t = dict({k: v for k, v in t.items() if k not in ("ansible.builtin.include_tasks", "vars")},
                     **{"ansible.builtin.set_fact": {"waited": "{{ ac_waiting }}"}})
        out.append(t)
    # facts the job set before (extra vars would win over every set_fact)
    init = {"ansible.builtin.set_fact": {"ac_waiting": [], "ac_status": {}, "ac_bgp": (sp or spec())["routing"]["bgp"]}}
    play = [init, {"block": out + [{"ansible.builtin.set_fact": {"done": True}}],
             "rescue": [{"ansible.builtin.set_fact": {"refused": True}}]},
            write_out("{'refused': refused | default(false), 'reason': fab_fail_reason | default(''), "
                      "'msg': fab_fail_msg | default(ansible_failed_result.msg | default('')) | default(''), "
                      "'done': done | default(false), 'waited': waited | default([]), "
                      "'policy': ac_policy | default({}), 'hub4': ac_hub4 | default([]), 'status': ac_status, "
                      "'range': ac_range | default([]), 'ce': ac_platform_ce | default({})}")]
    v = {"fab_name": "hub-a", "fab_namespace": namespace, "fabric_platform_namespace": "platform-ns",
         "fab_spec": sp if sp is not None else spec(),
         "role_path": os.path.join(ROLES, "attachmentcircuit_provision")}
    return run_play(play, v)


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class Refusals(unittest.TestCase):
    def test_a_tenant_namespace_is_refused(self):
        out = front(namespace="tenant-a")
        self.assertTrue(out["refused"])
        self.assertEqual(out["reason"], "NotPlatformNamespace")
        self.assertIn("ceKind customer", out["msg"])
        self.assertFalse(out["done"])

    def test_a_bearer_without_platform(self):
        out = front(e=edge(("customer",)))
        self.assertEqual((out["refused"], out["reason"]), (True, "CeKindNotAccepted"))

    def test_an_unknown_bearer(self):
        out = front(sp=spec(edgeRef={"name": "edge-a", "bearer": "nope"}))
        self.assertEqual(out["reason"], "BearerNotFound")

    def test_entity_refs_do_not_apply(self):
        e = edge()
        e["spec"]["entityRefs"] = [{"name": "acme"}]
        self.assertTrue(front(e=e)["done"])

    def test_service_not_offered(self):
        out = front(real={"service": {"enabled": False}, "infrastructure": {"hub": HUB}})
        self.assertEqual(out["reason"], "ServiceNotOffered")
        self.assertIn("service.enabled", out["msg"])
        self.assertEqual(front(real={"infrastructure": {"hub": HUB}})["reason"], "ServiceNotOffered")

    def test_ipv4_only(self):
        out = front(real={"service": SERVICE, "infrastructure": {"hub": ["2001:db8:f::/48"]}})
        self.assertEqual(out["reason"], "FamilyUnavailable")

    def test_the_hub_router_refuses_every_published_range(self):
        # Review R4 H1: the fabric's published rows (every site's own among them) are what the hub's router refuses
        rows = F.reserved_ranges({"service": SERVICE, "infrastructure": {"hub": HUB}},
                                 [{"name": "site-b", "kind": "openstack", "ranges": ["10.40.0.0/16"]}])
        out = front(fabric_status={"reservedRanges": rows})
        self.assertTrue(out["done"], out["msg"])
        self.assertIn("10.40.0.0/16 le 32", out["ce"]["refusedRanges"])
        self.assertEqual(out["ce"]["refusedRanges"],
                         ["0.0.0.0/0"] + [F.prefix_entry(r[0]) for r in F.service_refusal_rows(F.rows_of(rows))])

    def test_a_service_the_fabric_does_not_realize(self):
        # Review R4 M2: the fabric realizes no service for a wrong setting; the circuit says so by name
        out = front(fabric_status={"realization": {"service": {"enabled": False, "reason": "InvalidSpec",
                                                               "message": "realization service.vrf.table 254 must be ..."}}})
        self.assertEqual((out["refused"], out["reason"]), (True, "ServiceNotOffered"))
        self.assertIn("service.vrf.table", out["msg"])

    def test_the_service_range_is_required(self):
        for svc in (dict(SERVICE, range=[]), {k: v for k, v in SERVICE.items() if k != "range"},
                    dict(SERVICE, range=["2001:db8:f::/64"])):
            with self.subTest(range=str(svc.get("range"))):
                out = front(real={"service": svc, "infrastructure": {"hub": HUB}})
                self.assertEqual((out["refused"], out["reason"]), (True, "ServiceNotOffered"))
                self.assertIn("service.range", out["msg"])

    def test_the_vrf_name_is_required(self):
        out = front(real={"service": {"enabled": True}, "infrastructure": {"hub": HUB}})
        self.assertTrue(out["refused"])
        self.assertIn("service.vrf.name", out["msg"])

    def test_asn_checks(self):
        self.assertEqual(front(sp=spec(routing={"protocol": "bgp", "bgp": {"peerAsn": 65010}}))["reason"], "AsnConflict")
        self.assertEqual(front(sp=spec(routing={"protocol": "bgp", "bgp": {"peerAsn": 64999}}))["reason"], "PeerAsnReserved")

    def test_waits_for_the_edge(self):
        out = front(e=edge(ready=False))
        self.assertEqual(out["waited"], ["ProviderEdge edge-a (pending)"])

    def test_accepted(self):
        out = front()
        self.assertTrue(out["done"])
        self.assertEqual(out["hub4"], ["192.0.2.0/24", "198.51.100.17/32"])
        self.assertEqual(out["range"], ["192.0.2.0/28", "198.51.100.17/32"])
        # the hub router's filters come from check-circuit on the same realization (one rule for both sides)
        self.assertEqual(out["ce"], F.check_circuit({"ceKind": "platform", "namespace": "platform-ns",
                                                      "fabricNamespace": "platform-ns", "accept": [], "realization":
                                                      {"service": SERVICE, "infrastructure": {"hub": HUB},
                                                       "attachments": {"reservedAsns": [64999]}}})["ceConfig"])
        self.assertEqual(out["ce"]["acceptedRanges"], ["192.0.2.0/28 le 32", "198.51.100.17/32"])
        self.assertEqual(out["ce"]["advertisedRanges"], ["10.64.0.0/12 le 32"])
        self.assertEqual(out["policy"], {"ipv6": False, "acceptsDefault": False, "notes": []})
        self.assertEqual(out["status"], {"fabric": "fab"})
        out = front(sp=spec(routing={"protocol": "bgp", "bgp": {"peerAsn": 64512}, "advertise": {"default": True}}))
        self.assertIn("routing.advertise does not apply", out["policy"]["notes"][0])


def platform_ce(service=SERVICE):
    """status.ceConfig's filters as platform_circuit.yml keeps them (check-circuit on the realization)."""
    return F.check_circuit({"ceKind": "platform", "namespace": "platform-ns", "fabricNamespace": "platform-ns",
                            "accept": [], "realization": {"service": service, "infrastructure": {"hub": HUB}}})["ceConfig"]


def record_vars(platform=True, service=SERVICE):
    return {"ac_platform": platform, "ac_vlan": 3001, "ac_link_index": 2, "ac_plan": PLAN, "ac_asn": 65010,
            "ac_auth_type": "md5", "ac_vault_path": "fabric/circuits/_platform/hub-a", "ac_secret_name": "hub-a-bgp",
            "ac_bgp": {"peerAsn": 64512, "bfd": True}, "ac_bearer_real": {"interface": "bond1", "mtu": 9000},
            "ac_policy": {"acceptsDefault": False, "ipv6": False, "notes": []}, "ac_service": service,
            "ac_hub4": ["192.0.2.0/24", "198.51.100.17/32"], "ac_platform_ce": platform_ce(service),
            "ac_network": {"status": {"realization": {"vpnTarget": 10005}}},
            "fab_spec": {"edgeRef": {"name": "edge-a", "bearer": "b1"}, "routing": {"accept": ["10.50.0.0/16 le 24"]}},
            "fab_st": {}}


RECORD = ["The circuit's realization and the CE's parameters",
          "The platform circuit's realization and the hub router's parameters",
          "The circuit's IPv6 link and the CE's IPv6 parameters", "The realization's revision"]


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class Record(unittest.TestCase):
    def record(self, tasks, v):
        picked = [copy.deepcopy(t) for t in walk(tasks) if t.get("name") in RECORD]
        return run_play(picked + [write_out("{'real': ac_real, 'ce': ac_ce_config}")], v)

    def test_the_platform_record(self):
        out = self.record(load(os.path.join(AC, "main.yml")), record_vars())
        r, ce = out["real"], out["ce"]
        self.assertEqual(r["ceKind"], "platform")
        self.assertEqual(r["vrf"], "service")
        self.assertNotIn("vpnTarget", r)
        # the service range in (never the rest of the hub's ranges), the attachable ranges out
        self.assertEqual(r["accept"], ["192.0.2.0/28 le 32", "198.51.100.17/32"])
        self.assertEqual(r["advertise"], ["10.64.0.0/12 le 32"])
        self.assertEqual((r["vlan"], r["linkIndex"], r["ce"], r["interface"], r["mtu"]), (3001, 2, "169.254.200.3", "bond1", 9000))
        self.assertEqual(len(r["rev"]), 12)
        self.assertEqual(ce["acceptedRanges"], ["192.0.2.0/28 le 32", "198.51.100.17/32"])
        self.assertEqual(ce["refusedRanges"], ["0.0.0.0/0", "192.0.2.0/24 le 32", "198.51.100.17/32"] + SPECIAL)
        self.assertEqual(ce["advertisedRanges"], ["10.64.0.0/12 le 32"])
        self.assertEqual(ce["maxPrefixes"], 1000)
        self.assertEqual(ce["neighbors"], [{"name": "node-1", "address": "169.254.200.1", "asn": 65010},
                                           {"name": "node-2", "address": "169.254.200.2", "asn": 65010}])
        self.assertEqual(ce["authentication"], {"type": "md5", "secretName": "hub-a-bgp"})
        self.assertNotIn("address6", ce)

    def test_any_after_the_refused_ranges(self):
        # without attachableRanges the hub's router refuses the published ranges at any length, then accepts any other
        # IPv4 prefix up to /32; written out as 0.0.0.0/0 it reads the same
        out = self.record(load(os.path.join(AC, "main.yml")), record_vars(service=dict(SERVICE, attachableRanges=[])))
        self.assertEqual((out["ce"]["advertisedRanges"], out["real"]["advertise"]), (["0.0.0.0/0 le 32"],) * 2)
        self.assertEqual(out["ce"]["refusedRanges"], ["0.0.0.0/0", "192.0.2.0/24 le 32", "198.51.100.17/32"] + SPECIAL)
        out = self.record(load(os.path.join(AC, "main.yml")),
                          record_vars(service=dict(SERVICE, attachableRanges=[], maxPrefixes=40)))
        self.assertEqual(out["ce"]["maxPrefixes"], 40)
        out = self.record(load(os.path.join(AC, "main.yml")),
                          record_vars(service=dict(SERVICE, attachableRanges=["0.0.0.0/0"])))
        self.assertEqual((out["ce"]["advertisedRanges"], out["real"]["advertise"]), (["0.0.0.0/0 le 32"],) * 2)

    def test_a_customer_circuit_records_what_the_release_did(self):
        text = base_text("eda/rulebooks/roles/attachmentcircuit_provision/tasks/main.yml")
        if text is None:
            self.skipTest(f"{BASE} not in this checkout")
        v = record_vars(platform=False)
        v.pop("ac_platform")  # the old job had no such fact
        new = self.record(load(os.path.join(AC, "main.yml")), dict(v, ac_platform=False))
        old = self.record(yaml.safe_load(text), v)
        self.assertEqual(new, old)
        # also when the fact is missing (a task picked out on its own)
        self.assertEqual(self.record(load(os.path.join(AC, "main.yml")), v), old)


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class CustomerUntouched(unittest.TestCase):
    """The customer circuits' policy request: infrastructure.hub replaces egress.platformSources in the reserved list,
    so with the same ranges the request is the same as the release's; the hub's ranges refuse with PrefixReserved."""

    def fab_check(self, tasks, real):
        t = copy.deepcopy(by_name(tasks, "Check the accept list"))
        t.pop("ansible.builtin.include_tasks")
        t["ansible.builtin.set_fact"] = {"req": "{{ fab_check }}"}
        v = {"fab_spec": {"routing": {"accept": ["10.50.0.0/16 le 24"], "advertise": {}}},
             "ac_network": {"spec": {"prefixes": ["10.20.0.0/16"], "egress": {"internet": True}},
                            "status": {"addressSpace": [{"prefix": "10.20.0.0/16"}]}},
             "fab_real": real}
        return run_play([t, write_out("req")], v)

    def test_same_request_as_the_release(self):
        text = base_text("eda/rulebooks/roles/attachmentcircuit_provision/tasks/main.yml")
        if text is None:
            self.skipTest(f"{BASE} not in this checkout")
        common = {"attachments": {"linkPool": "169.254.192.0/18"}, "core": {"loopbackPool": "198.51.100.0/26",
                  "linkSubnet": "198.51.100.64/26"}}
        old = self.fab_check(yaml.safe_load(text), dict(common, egress={"linkPool": "169.254.0.0/18",
                                                                        "platformSources": ["192.0.2.0/24"]}))
        new = self.fab_check(load(os.path.join(AC, "main.yml")), dict(common, egress={"linkPool": "169.254.0.0/18"},
                                                                      infrastructure={"hub": ["192.0.2.0/24"]}))
        self.assertEqual(new, old)

    def test_the_hub_refusal(self):
        tasks = load(os.path.join(AC, "main.yml"))
        t = by_name(tasks, "Refuse an accept entry inside the hub's ranges")
        out = run_play([t, write_out("[fab_fail_reason, fab_fail_msg]")],
                       {"fab_name": "c1", "fab_real": {"infrastructure": {"hub": ["192.0.2.0/24"]}},
                        "fab_check_result": {"errors": ["accept entry '192.0.2.0/25 le 32' lies inside the platform range 192.0.2.0/24"]}})
        self.assertEqual(out[0], "PrefixReserved")
        self.assertEqual(out[1], "AttachmentCircuit c1: accept entry '192.0.2.0/25 le 32' lies inside 192.0.2.0/24, "
                                 "reserved by the platform: networks the platform's management connects from")

    def test_an_empty_accept_list_is_refused_by_the_job(self):
        t = by_name(load(os.path.join(AC, "main.yml")), "Refuse a customer circuit without an accept entry")
        out = run_play([t, write_out("fab_fail_reason | default('')")], {"fab_name": "c1", "fab_spec": {"routing": {}}})
        self.assertEqual(out, "InvalidSpec")
        out = run_play([t, write_out("fab_fail_reason | default('')")],
                       {"fab_name": "c1", "fab_spec": {"routing": {"accept": ["10.50.0.0/16"]}}})
        self.assertEqual(out, "")

    def test_the_samples_take_the_customer_path(self):
        t = by_name(load(os.path.join(AC, "main.yml")), "Start with nothing to wait for")
        for name in sorted(os.listdir(SAMPLES)):
            with open(os.path.join(SAMPLES, name)) as f:
                for doc in yaml.safe_load_all(f):
                    if not doc:
                        continue
                    out = run_play([t, write_out("[ac_platform | bool, ac_status]")],
                                   {"fab_spec": doc["spec"], "fab_entity": "acme", "fab_namespace": "entity-acme", "fab_name": "x"})
                    self.assertEqual(out, [False, {"entity": "acme"}], name)

    def test_the_entity_is_read_for_customer_circuits_only(self):
        for path in (os.path.join(AC, "main.yml"), os.path.join(ACD, "main.yml")):
            t = by_name(load(path), "Read the AttachmentCircuit")
            expr = t["vars"]["fab_read_entity"]
            for sp, want in (({"ceKind": "platform"}, False), ({"ceKind": "customer"}, True), ({}, True), (None, True)):
                v = {} if sp is None else {"fab_spec": sp}
                self.assertEqual(run_play([{"ansible.builtin.set_fact": {"x": expr}}, write_out("x | bool")], v), want)


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class FabricReport(unittest.TestCase):
    def entry(self, platform, real):
        t = by_name(load(os.path.join(AC, "main.yml")), "Does the fabric hold this revision on its nodes")
        return run_play([t, write_out("ac_entry")], {"ac_platform": platform, "fab_namespace": "platform-ns",
                                                      "fab_name": "hub-a", "ac_fabric": {"status": {"realization": real}}})

    def test_a_platform_circuit_is_read_from_the_service_report(self):
        real = {"service": {"circuits": [{"circuit": "platform-ns/hub-a", "vlan": 3001, "nodes": ["node-1"], "rev": "abc"}]}}
        self.assertEqual(self.entry(True, real)["rev"], "abc")
        self.assertEqual(self.entry(False, real), {})

    def test_teardown_sees_both_reports(self):
        t = by_name(load(os.path.join(ACD, "main.yml")), "Is the circuit still on the border nodes")
        for real, want in (({"service": {"circuits": [{"circuit": "platform-ns/hub-a"}]}}, True),
                           ({"attachments": [{"circuit": "platform-ns/hub-a"}]}, True), ({}, False)):
            out = run_play([t, write_out("acd_on_nodes | bool")], {"acd_key": "platform-ns/hub-a",
                                                                    "acd_fabric": {"status": {"realization": real}}})
            self.assertEqual(out, want)

    def test_the_vault_path_of_a_platform_circuit(self):
        t = by_name(load(os.path.join(AC, "main.yml")), "The password's Vault path")
        v = {"fab_real": {"attachments": {"secretPath": "fabric/circuits/{entity}/{name}"}}, "fab_name": "hub-a",
             "fab_entity": "", "ac_bgp": {}}
        self.assertEqual(run_play([t, write_out("ac_vault_path")], dict(v, ac_platform=True)), "fabric/circuits/_platform/hub-a")
        self.assertEqual(run_play([t, write_out("ac_vault_path")], dict(v, ac_platform=False, fab_entity="acme")),
                         "fabric/circuits/acme/hub-a")


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class EdgeCapabilities(unittest.TestCase):
    def kinds(self, bearers, service):
        t = copy.deepcopy(by_name(load(os.path.join(PE, "main.yml")), "Count the circuits per bearer"))
        v = {"fab_name": "edge-a", "fab_spec": {"platform": "frr", "bearers": bearers},
             "_pe_circuits": {"resources": []}, "pe_real": {"bearers": {b["name"]: {} for b in bearers}}, "pe_nodes": [],
             "fab_real": {"model": "linux-pe", "attachments": {"vlanPool": {}, "linkPool": "x", "linkPrefixLength": 29},
                          "service": service}}
        return run_play([{"ansible.builtin.set_fact": {"pe_status": {}}}, t, write_out("pe_status.capabilities.ceKinds")], v)

    def test_platform_when_offered_and_a_bearer_takes_it(self):
        self.assertEqual(self.kinds([{"name": "b1", "ceKinds": ["customer", "platform"]}], {"enabled": True}),
                         ["customer", "platform"])
        self.assertEqual(self.kinds([{"name": "b1", "ceKinds": ["customer", "platform"]}], {"enabled": False}), ["customer"])
        self.assertEqual(self.kinds([{"name": "b1", "ceKinds": ["customer"]}], {"enabled": True}), ["customer"])
        self.assertEqual(self.kinds([{"name": "b1", "ceKinds": ["customer"]}], {}), ["customer"])


class CrdText(unittest.TestCase):
    def schema(self, path):
        return load(path)["spec"]["versions"][0]["schema"]["openAPIV3Schema"]

    def test_circuit(self):
        sp = self.schema(CRD)["properties"]["spec"]
        self.assertEqual(sp["required"], ["edgeRef", "routing"])
        self.assertEqual(sp["properties"]["ceKind"]["enum"], ["customer", "platform"])
        rules = [r["rule"] for r in sp["x-kubernetes-validations"]]
        self.assertIn("(self.ceKind == 'platform') != has(self.network)", rules)
        self.assertIn("self.ceKind != 'platform' || !has(self.routing.accept)", rules)
        self.assertEqual([r["rule"] for r in sp["properties"]["routing"]["x-kubernetes-validations"]],
                         ["self.protocol != 'bgp' || has(self.bgp)"])
        ce = self.schema(CRD)["properties"]["status"]["properties"]["ceConfig"]["properties"]
        for k in ("acceptedRanges", "refusedRanges", "advertisedRanges", "maxPrefixes"):
            self.assertIn(k, ce)

    def test_edge(self):
        b = self.schema(PE_CRD)["properties"]["spec"]["properties"]["bearers"]["items"]["properties"]["ceKinds"]
        self.assertEqual(b["items"]["enum"], ["customer", "platform"])

    def test_api_text_only(self):
        for path in (CRD, PE_CRD):
            with open(path) as f:
                text = f.read()
            for word in ("DESIGN", "OD16", "2026-", "fabric_realization", "VRF", "infrastructure.hub", "sovereign-cloud"):
                self.assertNotIn(word, text, path)


if __name__ == "__main__":
    unittest.main()
