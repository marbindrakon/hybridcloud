"""The fabric job's steps for the service VRF (hybridfabric_provision main.yml, node_root.yml), run in ansible-playbook
step by step (both ansible-core versions: the workstation's and the execution environment's):

- the platform reach annotation is no longer read: a network without internet egress never joins the egress;
- the hub's ranges by family, the service-plan request as the job templates it (the planner's answer from it), the
  attachment plan kept with the service links in its root VRFs;
- the ranges tenants may not use joining the offer the Entities were told (so a change wakes them);
- the status: realization.service and reservedRanges, the egress rows without platform;
- the wakes: the networks whose service rows changed, every network of the fabric when the service comes or goes,
  the platform circuits whose entry changed;
- the node: root.json gains the service block only on a node with a platform circuit, the BGP instance of the service
  VRF is removed before the apply on a node that no longer has it, the readiness check reads the service VRF (and is
  valid bash).

Documentation and private ranges, example names only."""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, write_out  # noqa: E402
import test_service_vrf as S  # noqa: E402

F = S.F
HF = os.path.join(ROOT, "eda", "rulebooks", "roles", "hybridfabric_provision", "tasks")


def main_task(name):
    return copy.deepcopy(by_name(load(os.path.join(HF, "main.yml")), name))


def node_task(name):
    return copy.deepcopy(by_name(load(os.path.join(HF, "node_root.yml")), name))


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class FabricJob(unittest.TestCase):
    def test_annotation_not_read(self):
        t = main_task("Set the networks with internet egress")
        n = S.hn("lab", ["192.0.2.0/24"], internet=False)
        n["metadata"]["annotations"] = {"shc.signal9.gg/platform-reach": '["192.0.2.0/25"]'}
        out = run_play([t, write_out("hf_egress_networks")],
                       {"fab_name": "lab", "fab_ledger_result": {"ledger": {"alloc": S.LEDGER}},
                        "_hf_networks": {"resources": [n, S.hn("core", ["10.60.0.0/16"])]},
                        "_hf_placements": {"resources": []}, "fab_real": {"egress": {"enabled": True}}, "fab_st": {}})
        self.assertEqual(out, [{"network": "acme/core", "vpnTarget": 10000, "index": 0, "internet": True, "ipv4": True}])

    def test_hub_by_family(self):
        t = main_task("The hub's ranges")
        out = run_play([t, write_out("[hf_hub4, hf_hub6]")],
                       {"fab_real": {"infrastructure": {"hub": [S.HUB, "2001:db8:5::/48"]}}})
        self.assertEqual(out, [[S.HUB], ["2001:db8:5::/48"]])
        out = run_play([t, write_out("[hf_hub4, hf_hub6]")], {"fab_real": {}})
        self.assertEqual(out, [[], []])

    def test_service_plan_request(self):
        real, (egp, att, vrfs, svc) = S.service_setup()
        t = main_task("Plan the service VRF on the nodes")
        nets = [S.hn("lab", ["192.0.2.0/24"], internet=False, rows=["192.0.2.15/32"])]
        led = copy.deepcopy(S.LEDGER)
        led["servicePrefix"] = {"HybridNetwork/acme/lab/192.0.2.15/32": 1}
        a = F.attachments_plan({"fabric": "lab", "attachments": real["attachments"], "nodes": S.NODES,
                                "tableBase": 100000, "domainAsn": 65010, "circuits": [S.platform()], "passwords": [],
                                "networks": nets, "placements": [], "ledger": led, "egress": {}, "egressNodes": {},
                                "reserved": [], "platformSources": [S.HUB], "service": real["service"],
                                "fabricNamespace": "platform"})
        out = run_play([{"ansible.builtin.set_fact": {"_r": t["vars"]["fab_check"]}}, write_out("_r")],
                       {"fab_real": real, "fab_spec": {"domainAsn": 65010, "borderGateway": {"nodes": [{"name": n} for n in S.NODES]}},
                        "fab_name": "lab", "_hf_networks": {"resources": nets}, "hf_alloc": led, "hf_attach_plan": a,
                        "hf_sites": [{"name": "site-b", "kind": "openshift", "ranges": ["10.40.0.0/16"]}],
                        "fab_st": {"realization": {"service": {"networks": [{"network": "acme/lab", "prefixes": []}]}}}})
        self.assertEqual(out["action"], "service-plan")
        # the fabric's sites reach the plan (Review R4 H1: every site's ranges refuse a cluster prefix)
        self.assertEqual(out["sites"], [{"name": "site-b", "kind": "openshift", "ranges": ["10.40.0.0/16"]}])
        self.assertIsInstance(out["realization"]["service"], dict)
        r = F.service_plan(out)
        self.assertEqual(r["errors"], [])
        self.assertEqual(r["status"]["networks"][0]["prefixes"], ["192.0.2.15/32"])
        # without platform circuits (the attachments plan has no platform key): no service VRF, nothing realized
        out = run_play([{"ansible.builtin.set_fact": {"_r": t["vars"]["fab_check"]}}, write_out("_r")],
                       {"fab_real": real, "fab_spec": {"domainAsn": 65010, "borderGateway": {"nodes": []}},
                        "fab_name": "lab", "_hf_networks": {"resources": nets}, "hf_alloc": led,
                        "hf_attach_plan": {"vrfs": {}}, "fab_st": {}, "hf_sites": []})
        self.assertEqual(out["circuits"], [])
        self.assertEqual(out["previous"], {})

    def test_attachment_plan_kept_with_the_service_links(self):
        t = main_task("Keep the attachment plan")
        plan = {"circuits": [], "nodes": {}, "status": [], "skipped": [],
                "vrfs": {"bgw-1": [{"vrf": "e10000", "vpnTarget": 10000, "circuits": []}]}}
        service = {"vrfs": {"bgw-1": [{"vrf": "e10000", "vpnTarget": 10000, "circuits": [], "service": {"soIp": "x"}},
                                      {"vrf": "e10002", "vpnTarget": 10002, "circuits": [], "service": {"soIp": "y"}}]}}
        out = run_play([t, write_out("hf_attach")], {"hf_attach_plan": plan, "hf_service": service, "fab_real": {},
                                                     "fab_st": {}})
        self.assertEqual([(v["vrf"], v["families"], v["service"]["soIp"]) for v in out["vrfs"]["bgw-1"]],
                         [("e10000", [4], "x"), ("e10002", [4], "y")])

    def test_ranges_join_the_tenants_offer(self):
        keep = main_task("Keep the ranges tenants may not use")
        t = main_task("Tell the Entities of the ranges tenants may not use")
        rows = F.reserved_ranges(S.realization(S.CORE, service=True))
        # (pools.yml sets the offer as a fact; an extra variable would win over the step's set_fact)
        pre = {"ansible.builtin.set_fact": {"hf_tenant_offer": "{{ _offer }}"}}
        pre = [pre, keep]
        base = {"fab_check_result": {"ranges": rows}, "_offer": "/48 /56 default /56",
                "hf_service": {"status": {"attachableRanges": ["192.0.2.0/24"]}}}
        a = run_play(pre + [t, write_out("[hf_reserved, hf_tenant_offer]")], base)
        self.assertEqual(a[0], rows)
        self.assertRegex(a[1], r"^/48 /56 default /56; ranges [0-9a-f]{12}$")
        b = run_play(pre + [t, write_out("hf_tenant_offer")], dict(base, hf_service={"status": {"attachableRanges": []}}))
        self.assertNotEqual(a[1], b)
        c = run_play(pre + [t, write_out("hf_tenant_offer")], base)
        self.assertEqual(a[1], c)

    def test_a_service_not_realized_is_reported_once_and_named(self):
        # Review R4 M2: an event per change of the reason, and the Ready message names it
        t = main_task("Report the service routing domain not realized")
        probe = {"name": t["name"], "when": t["when"], "vars": t.get("vars", {}),
                 "ansible.builtin.set_fact": {"evented": True}}
        st = {"enabled": False, "reason": "InvalidSpec", "message": "realization service.range is required"}
        for before, want in (({}, True), ({"enabled": True}, True), (dict(st), False)):
            out = run_play([probe, write_out("evented | default(false)")],
                           {"hf_service": {"status": st}, "hf_service_before": before})
            self.assertEqual(out, want, before)
        out = run_play([probe, write_out("evented | default(false)")],
                       {"hf_service": {"status": {"enabled": True}}, "hf_service_before": {}})
        self.assertFalse(out)
        msg = main_task("Report the fabric ready")["vars"]["fab_message"]
        v = {"fab_name": "lab", "hf_node_results": [], "hf_changed_nodes": [], "hf_egress": {"networks": []},
             "hf_egress_status": [], "hf_egress6_status": {"enabled": False}, "hf_attach": {"status": [], "skipped": []},
             "hf_ipam": {"refused": ""}, "fab_real": {"egress": {}, "attachments": {}},
             "hf_service": {"enabled": True, "pending": [], "status": dict(st, circuits=[], networks=[], refused=[])},
             "_svc_capped": []}
        text = run_play([{"ansible.builtin.set_fact": {"m": msg}}, write_out("m")], v)
        self.assertIn("service VRF not realized: realization service.range is required", text)

    def test_a_wrong_setting_keeps_the_service_loudly(self):
        # Review R4 NM2: the nodes keep the last applied service; the event once per change; the condition
        # ServiceRouting False InvalidSpec naming the setting (the alert reads it); the Ready message says it
        t = main_task("Report the service routing domain kept while a setting is wrong")
        probe = {"name": t["name"], "when": t["when"], "vars": t.get("vars", {}),
                 "ansible.builtin.set_fact": {"evented": True, "why": "{{ _why }}"}}
        st = {"enabled": True, "circuits": [{"circuit": "p/hub-a"}], "networks": [], "refused": [],
              "settingsError": "realization service.tenantShare '0,5' must be a fraction"}
        for before, want in (({"enabled": True}, True), (dict(st), False)):
            out = run_play([probe, write_out("evented | default(false)")],
                           {"hf_service": {"status": st, "kept": True}, "hf_service_before": before})
            self.assertEqual(out, want, before)
        rt = main_task("Report the fabric ready")
        ct = main_task("The service routing domain's condition")
        v = {"fab_name": "lab", "hf_node_results": [], "hf_changed_nodes": [], "hf_egress": {"networks": []},
             "hf_egress_status": [], "hf_egress6_status": {"enabled": False}, "hf_attach": {"status": [], "skipped": []},
             "hf_ipam": {"refused": ""}, "fab_real": {"egress": {}, "attachments": {}}, "fab_st": {},
             "hf_service": {"enabled": True, "kept": True, "pending": [], "status": st}}
        play = [ct, {"ansible.builtin.set_fact": {"m": rt["vars"]["fab_message"], "c": rt["vars"]["fab_conditions"]},
                     "vars": {k: rt["vars"][k] for k in rt["vars"] if k.startswith("_")}}]
        out = run_play(play + [write_out("[m, c]")], v)
        self.assertIn("kept as last applied: realization service.tenantShare", out[0])
        cond = next(c for c in out[1] if c["type"] == "ServiceRouting")
        self.assertEqual((cond["status"], cond["reason"]), ("False", "InvalidSpec"))
        self.assertIn("service.tenantShare", cond["message"])
        self.assertIn("keep the service as last applied", cond["message"])
        # Review R4 V2-M1, V2-L2: a node with nothing to keep is named; what waits for the fix is said
        self.assertIn("a network or cluster leaving", cond["message"])
        out = run_play(play + [write_out("c")], dict(v, hf_service_unkept=["bgw-2"]))
        cond = next(c for c in out if c["type"] == "ServiceRouting")
        self.assertIn("no service on bgw-2 (nothing applied there to keep)", cond["message"])
        # the service off and never on: no condition at all (nothing changes)
        v["hf_service"] = {"enabled": False, "status": {"enabled": False}}
        out = run_play(play + [write_out("c")], v)
        self.assertEqual([c for c in out if c["type"] == "ServiceRouting"], [])

    def test_a_node_keeps_the_service_it_runs(self):
        # Review R4 NM2: inside the node loop, the node's own root.json service block is kept (marked keep)
        import base64
        real, (egp, att, vrfs, svc) = S.service_setup()
        names = ["{{ bgw_node.name }}: read the service the node runs, to keep it while a setting is wrong",
                 "{{ bgw_node.name }}: keep the service the node runs", "{{ bgw_node.name }}: the kept service"]
        tasks = [node_task(n) for n in names]
        doc = json.dumps({"type": "root", "service": svc["bgw-1"]})
        tasks[0] = {"name": names[0], "ansible.builtin.set_fact": {"_hf_root_now": {
            "content": base64.b64encode(doc.encode()).decode()}}, "when": tasks[0]["when"]}
        v = {"role_path": HF.rsplit("/tasks", 1)[0], "bgw_node": {"name": "bgw-1"}, "hf_service": {"kept": True}}
        # (node_root.yml sets these as facts; an extra variable would win over the step's set_fact)
        tasks = [{"ansible.builtin.set_fact": {"hf_node_service": {}, "hf_node_vrfs": []}}] + tasks
        out = run_play(tasks + [write_out("[hf_node_service, hf_node_vrfs]")], v)
        self.assertIs(out[0]["keep"], True)
        self.assertEqual(out[0]["networks"], svc["bgw-1"]["networks"])
        self.assertEqual([x["vrf"] for x in out[1]], [w["vrf"] for w in svc["bgw-1"]["networks"]])
        # not kept: nothing read, nothing changed
        out = run_play(tasks + [write_out("hf_node_service")], dict(v, hf_service={}))
        self.assertEqual(out, {})

    def test_a_node_with_nothing_to_keep_is_named(self):
        # Review R4 V2-M1: a rebuilt or new node has no root.json while a setting is wrong: no service there, and it
        # says so (hf_service_unkept for the fabric's condition, serviceMissing in its root.json for its health)
        names = ["{{ bgw_node.name }}: read the service the node runs, to keep it while a setting is wrong",
                 "{{ bgw_node.name }}: keep the service the node runs", "{{ bgw_node.name }}: the kept service",
                 "{{ bgw_node.name }}: a node with no service to keep"]
        tasks = [node_task(n) for n in names]
        tasks[0] = {"name": names[0], "ansible.builtin.set_fact": {"_hf_root_now": {"failed": True}},
                    "when": tasks[0]["when"]}
        v = {"role_path": HF.rsplit("/tasks", 1)[0], "bgw_node": {"name": "bgw-2"},
             "hf_service": {"kept": True, "status": {"circuits": [{"circuit": "p/hub-a", "nodes": ["bgw-1", "bgw-2"]}]}}}
        pre = [{"ansible.builtin.set_fact": {"hf_node_service": {}, "hf_node_vrfs": [], "hf_service_unkept": ["bgw-1"]}}]
        out = run_play(pre + tasks + [write_out("[hf_node_service, hf_service_unkept]")], v)
        self.assertEqual(out, [{}, ["bgw-1", "bgw-2"]])
        conf = node_task("{{ bgw_node.name }}: root instance config")["ansible.builtin.copy"]["content"]
        self.assertIn("serviceMissing", conf)

    def test_status(self):
        t = main_task("Report the fabric ready")
        real, (egp, att, vrfs, svc) = S.service_setup()
        sp = {"status": {"enabled": True, "vrf": "service", "networks": [], "refused": [], "circuits": []}}
        common = {"fab_name": "lab", "hf_ledger": {"allocated": {}, "available": {}}, "hf_node_results": [],
                  "hf_egress_status": [], "hf_egress6_status": {"enabled": False},
                  "hf_attach": {"status": [], "skipped": []}, "hf_ipam": {}, "fab_real": real,
                  "hf_pe": F.pe_addresses({"core": S.CORE, "index": 1}), "hf_service": sp,
                  "hf_reserved": [{"prefix": S.HUB, "purpose": "x", "rule": "Refused"}]}
        out = run_play([{"ansible.builtin.set_fact": {"_s": t["vars"]["fab_status"]}}, write_out("_s")], common)
        self.assertEqual(out["realization"]["service"], sp["status"])
        self.assertEqual(out["reservedRanges"], common["hf_reserved"])

    def test_egress_rows_without_platform(self):
        t = main_task("Set the egress status")
        out = run_play([t, write_out("hf_egress_status")],
                       {"hf_egress": {"networks": {"acme/core": {"vpnTarget": 10000, "sourceAddresses": ["203.0.113.64"],
                                                                  "internet": True}}}})
        self.assertEqual(out, [{"network": "acme/core", "vpnTarget": 10000, "sourceAddresses": ["203.0.113.64"],
                                "internet": True, "ipv6": False, "prefixes6": [], "primary": ""}])

    def wakes(self, now, before, networks=()):
        t = main_task("Wake the networks whose service prefixes changed")
        t.pop("ansible.builtin.include_tasks")
        return run_play([{"vars": t["vars"], "ansible.builtin.set_fact": {"_w": t["vars"]["nudge_crs"]}}, write_out("_w")],
                        {"hf_service": {"status": now}, "hf_service_before": before, "fab_name": "lab",
                         "_hf_networks": {"resources": list(networks)}})

    def test_network_wakes(self):
        on = {"enabled": True, "circuits": [{"circuit": "platform/hub-a"}], "networks": [], "refused": []}
        row = {"network": "acme/lab", "vpnTarget": 10002, "index": 2, "prefixes": ["192.0.2.15/32"]}
        self.assertEqual(self.wakes(dict(on, networks=[row]), on),
                         [{"kind": "HybridNetwork", "namespace": "acme", "name": "lab"}])
        self.assertEqual(self.wakes(dict(on, networks=[row]), dict(on, networks=[row])), [])
        ref = {"network": "tenant-b/web", "prefix": "192.0.2.15/32", "reason": "ServicePrefixInUse", "message": "m"}
        self.assertEqual(self.wakes(dict(on, refused=[ref]), on),
                         [{"kind": "HybridNetwork", "namespace": "tenant-b", "name": "web"}])
        # the service (or its last circuit) came or went: every network of the fabric reads it again
        nets = [S.hn("lab", ["192.0.2.0/24"]), dict(S.hn("other", ["192.0.2.0/24"]), status={"fabric": "elsewhere"})]
        self.assertEqual(self.wakes(on, {}, nets), [{"kind": "HybridNetwork", "namespace": "acme", "name": "lab"}])
        self.assertEqual(self.wakes(dict(on, circuits=[]), on, nets),
                         [{"kind": "HybridNetwork", "namespace": "acme", "name": "lab"}])

    def test_circuit_wakes(self):
        t = main_task("Wake the circuits whose realization changed")
        t.pop("ansible.builtin.include_tasks")
        c = {"circuit": "platform/hub-a", "vlan": 3099, "nodes": S.NODES, "rev": "r1", "policy": "p1", "device": "d"}
        out = run_play([{"vars": t["vars"], "ansible.builtin.set_fact": {"_w": t["vars"]["nudge_crs"]}}, write_out("_w")],
                       {"hf_attach": {"status": []}, "hf_attach_before": [],
                        "hf_service": {"status": {"circuits": [dict(c, policy="p2")]}},
                        "hf_service_before": {"circuits": [c]}})
        self.assertEqual(out, [{"kind": "AttachmentCircuit", "namespace": "platform", "name": "hub-a"}])


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class NodeSteps(unittest.TestCase):
    def root_json(self, svc):
        t = node_task("{{ bgw_node.name }}: root instance config")
        t = {"name": t["name"], "vars": t.get("vars", {}),
             "ansible.builtin.set_fact": {"_c": t["ansible.builtin.copy"]["content"]}}
        v = {"bgw_node": {"name": "bgw-1"}, "hf_root": {"type": "root"}, "hf_node_egress": {}, "hf_node_attach": {},
             "hf_pe": F.pe_addresses({"core": S.CORE, "index": 1}), "fab_real": {"core": S.CORE},
             "fab_check_result": {}}
        if svc is not None:
            v["hf_node_service"] = svc
        return json.loads(run_play([t, write_out("_c")], v))

    def test_root_json(self):
        real, (egp, att, vrfs, svc) = S.service_setup()
        self.assertEqual(self.root_json(svc["bgw-1"])["service"], svc["bgw-1"])
        self.assertNotIn("service", self.root_json({}))
        self.assertNotIn("service", self.root_json(None))

    def render(self, name, extra):
        t = node_task(name)
        body = t["ansible.builtin.shell"]
        body = body["cmd"] if isinstance(body, dict) else body
        return run_play([{"vars": t.get("vars", {}), "ansible.builtin.set_fact": {"_t": body}}, write_out("_t")], extra)

    def test_bgp_instance_removal(self):
        # the step before the apply takes out of FRR what the run removes (files/shc_frr_leaving.py, which compares the
        # running configuration with the rendered file, test_frr_leaving.py): the networks' VRFs by pattern and the
        # service VRF by the realization's name, also on a node that no longer has it
        name = "{{ bgw_node.name }}: take out of FRR what this run removes (before the kernel objects)"
        t = node_task(name)
        role = os.path.join(ROOT, "eda", "rulebooks", "roles", "hybridfabric_provision")

        def argv(extra):
            out = run_play([{"vars": t.get("vars", {}), "ansible.builtin.set_fact": {"_a": t["ansible.builtin.command"]["argv"]}},
                            write_out("_a")], dict({"role_path": role}, **extra))
            out = json.loads(out) if isinstance(out, str) else out
            self.assertEqual(out[:6], ["timeout", "-k", "5", "60", "python3", "-c"])
            with open(os.path.join(ROOT, "eda", "common", "files", "shc_frr_leaving.py")) as f:
                self.assertEqual(out[6].strip(), f.read().strip())
            return out[7:]
        real, (egp, att, vrfs, svc) = S.service_setup()
        self.assertEqual(argv({"fab_real": real}), ["--pattern", "e[0-9]+", "--name", "service"])
        # without the realization's service the instances of the networks' VRFs only, as before
        self.assertEqual(argv({"fab_real": {}}), ["--pattern", "e[0-9]+"])
        # the role default off (a job launched by hand; a string from a survey or extra vars): the instances only
        self.assertEqual(argv({"fab_real": {}, "fabric_frr_remove_first": "false"}), ["--pattern", "e[0-9]+", "--no-neighbours"])
        self.assertEqual(argv({"fab_real": {}, "fabric_frr_remove_first": "True"}), ["--pattern", "e[0-9]+"])
        self.assertEqual(t["ansible.builtin.command"]["stdin"], "{{ _hf_frr_text }}")

    def test_readiness_check(self):
        real, (egp, att, vrfs, svc) = S.service_setup()
        extra = {"fab_spec": {"domainAsn": 65010}, "fab_real": real, "hf_node_vrfs": [], "hf_node_egress": {},
                 "hf_node_service": svc["bgw-1"], "hf_root": {"bridge": "br-core", "link": "10.255.1.1/24"},
                 "fab_node_transport": "mpls", "hf_pe": F.pe_addresses({"core": S.CORE, "index": 1})}
        text = self.render("{{ bgw_node.name }}: wait for BGP and IS-IS", extra)
        self.assertIn("nft list table ip shc_service >/dev/null", text)
        self.assertIn("jq -r '.[0].linkinfo.info_data.table')\" = 9001 ]", text)
        self.assertIn("ip -j link show dev so10002 | jq -r '.[0].master')\" = service ]", text)
        self.assertIn("show bgp vrf service neighbors %s json' | grep -q '\"remoteAs\":64600'"
                      % svc["bgw-1"]["circuits"][0]["ce"], text)
        # the session over each network's service link, configured on both sides
        self.assertIn("show bgp vrf service neighbors 169.254.128.5 json' | grep -q '\"remoteAs\":65010'", text)
        self.assertIn("show bgp vrf e10002 neighbors 169.254.128.4 json' | grep -q '\"remoteAs\":65010'", text)
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
            f.write(text)
        self.addCleanup(os.remove, f.name)
        self.assertEqual(subprocess.run(["bash", "-n", f.name], capture_output=True).returncode, 0)
        text = self.render("{{ bgw_node.name }}: wait for BGP and IS-IS", dict(extra, hf_node_service={}))
        self.assertNotIn("shc_service", text)


if __name__ == "__main__":
    unittest.main()
