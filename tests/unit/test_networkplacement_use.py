"""The placement job's handling of spec.use on a CloudOSO (networkplacement_provision): the request always carries
both uses with their defaults (booleans in either ansible-core), status.use per use (ready, off, removing with reason
InUse and up to ten users named), the site record (networkName, the policy ids, routerExternalForProject), and the
final write of a placement whose use waits to be removed: Ready stays True, condition Degraded/InUse, status.status
ready (what is true; the operator runs a ready placement whose use waits again on its reconcile period, while it
skips other ready placements), and the Degraded condition cleared once the use is gone. The CRD's CEL rules for spec.use are checked as text here (the
envtest harness, tests/cel-ipam, runs them on an API server). The tasks run in ansible-playbook (skipped when
absent)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_cases  # noqa: E402

LIB = os.path.join(ROOT, "eda", "rulebooks", "collections", "ansible_collections", "hybridsovereign", "shc",
                   "plugins", "module_utils")
sys.path.insert(0, LIB)
import shc_status as ST  # noqa: E402

TASKS = os.path.join(ROOT, "eda", "rulebooks", "roles", "networkplacement_provision", "tasks")
BACKEND = os.path.join(TASKS, "backend_cloudoso.yml")
MAIN = os.path.join(TASKS, "main.yml")
CRD = os.path.join(ROOT, "operator", "config", "crd", "bases", "crd-networkplacement.yaml")


def users(n):
    return [f"floating IP 192.0.2.{i}" for i in range(1, n + 1)]


def os_result(vm="ready", ra="off", vm_users=(), ra_users=(), prune=()):
    return {"use": {"virtualMachines": {"state": vm, "policyId": "rbac-1" if vm != "off" else "", "inUse": list(vm_users)},
                    "routerAttachments": {"state": ra, "policyId": "rbac-2" if ra != "off" else "",
                                          "inUse": list(ra_users)}},
            "shareId": "rbac-1" if vm != "off" else "", "externalId": "rbac-2" if ra != "off" else "",
            "routerExternalForProject": ra != "off", "pruneInUse": list(prune),
            "networkId": "net-1", "routerId": "rtr-1", "subnets": {}, "addressMode": "", "created": [], "changed": []}


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class Request(unittest.TestCase):
    def test_use_is_sent_with_its_defaults_as_booleans(self):
        task = by_name(load(BACKEND), "The placement's network, router and share")
        use = task["vars"]["os_request"]["use"]
        steps = [{"ansible.builtin.set_fact": {"fab_spec": "{{ case }}"}},
                 {"ansible.builtin.set_fact": {"_use": use}}]
        res = run_cases(steps, [{}, {"use": {}}, {"use": {"routerAttachments": True}},
                                {"use": {"virtualMachines": False, "routerAttachments": True}},
                                {"use": None}], "_use")
        self.assertEqual(res, [{"virtualMachines": True, "routerAttachments": False},
                               {"virtualMachines": True, "routerAttachments": False},
                               {"virtualMachines": True, "routerAttachments": True},
                               {"virtualMachines": False, "routerAttachments": True},
                               {"virtualMachines": True, "routerAttachments": False}])


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class UseStatus(unittest.TestCase):
    def run_status(self, results):
        tasks = load(BACKEND)
        steps = [{"ansible.builtin.set_fact": {"os_result": "{{ case }}", "np_status": {"entity": "acme"},
                                               "np_backend": {"status": {"projectId": "proj-tenant"}},
                                               "fab_real": {"openstack": {"platformProject": "platform"}},
                                               "np_names": {"osNetwork": "hn51001"}, "np_ra_failed": ""}},
                 by_name(tasks, "Record the site side"),
                 by_name(tasks, "The environment's uses of the network")]
        return run_cases(steps, results, "{'status': np_status, 'removing': np_use_removing, 'note': np_use_note, "
                                         "'site': np_backend_status}")

    def test_cases(self):
        res = self.run_status([
            os_result(),
            os_result(vm="off", ra="ready"),
            os_result(vm="removing", ra="ready", vm_users=["port of server vm-1"]),
            os_result(ra="removing", ra_users=users(12)),
            os_result(ra="ready", prune=["project proj-b: floating IP 192.0.2.17"]),
        ])
        ready = {"state": "ready", "reason": "", "message": ""}
        off = {"state": "off", "reason": "", "message": ""}
        self.assertEqual(res[0]["status"]["use"], {"virtualMachines": ready, "routerAttachments": off})
        self.assertEqual((res[0]["removing"], res[0]["note"]), ([], ""))
        self.assertEqual(res[0]["status"]["entity"], "acme")
        self.assertEqual(res[1]["status"]["use"], {"virtualMachines": off, "routerAttachments": ready})
        vm = res[2]["status"]["use"]["virtualMachines"]
        self.assertEqual(vm, {"state": "removing", "reason": "InUse",
                              "message": "Turning this off waits until the environment no longer uses it: "
                                         "port of server vm-1"})
        self.assertEqual(res[2]["removing"], ["virtualMachines"])
        self.assertEqual(res[2]["note"], "; use for virtual machines turned off, waiting: port of server vm-1")
        ra = res[3]["status"]["use"]["routerAttachments"]
        self.assertEqual(ra["message"], "Turning this off waits until the environment no longer uses it: "
                         + ", ".join(users(10)) + " and 2 more")
        self.assertEqual(res[3]["removing"], ["routerAttachments"])
        self.assertEqual(res[4]["note"], "; external access of other projects kept while used: "
                                         "project proj-b: floating IP 192.0.2.17")

    def test_site_record(self):
        res = self.run_status([os_result(ra="ready"), os_result(vm="off")])
        site = res[0]["site"]
        self.assertEqual(site["networkName"], "hn51001")
        self.assertEqual(site["rbac"], {"shared": "rbac-1", "external": "rbac-2"})
        self.assertIs(site["routerExternalForProject"], True)
        self.assertEqual(site["sharedTo"], "proj-tenant")
        self.assertEqual(res[1]["site"]["rbac"], {"shared": "", "external": ""})
        self.assertIs(res[1]["site"]["routerExternalForProject"], False)


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class FinalWrite(unittest.TestCase):
    """The vars of "Report the placement ready" as the job hands them to fabric_finish.yml."""

    def render(self, cases):
        task = by_name(load(MAIN), "Report the placement ready")
        v = task["vars"]
        steps = [{"ansible.builtin.set_fact": {"np_final": {"entity": "acme", "use": "{{ case.use }}"},
                                               "np_status": {"use": "{{ case.use }}"},
                                               "np_use_removing": "{{ case.removing }}",
                                               "np_use_wait_reason": "{{ case.reason | default('InUse') }}",
                                               "np_use_wait_messages": "{{ case.messages | default([]) }}",
                                               "np_backend_kind": "{{ case.kind }}",
                                               "fab_st": "{{ case.before }}"}},
                 {"ansible.builtin.set_fact": {"_status": v["fab_status"], "_conds": v["fab_conditions"]},
                  "vars": {"_np_degraded_before": v["_np_degraded_before"]}}]
        return run_cases(steps, cases, "{'status': _status, 'conds': _conds | map('dict2items') | map('rejectattr', "
                                       "'key', 'equalto', 'lastTransitionTime') | map('items2dict') | list}")

    def test_cases(self):
        msg = "Turning this off waits until the environment no longer uses it: floating IP 192.0.2.15"
        removing = {"virtualMachines": {"state": "ready", "reason": "", "message": ""},
                    "routerAttachments": {"state": "removing", "reason": "InUse", "message": msg}}
        steady = {"virtualMachines": {"state": "ready", "reason": "", "message": ""},
                  "routerAttachments": {"state": "off", "reason": "", "message": ""}}
        degraded = {"conditions": [{"type": "Degraded", "status": "True", "reason": "InUse"}]}
        res = self.render([
            {"use": removing, "removing": ["routerAttachments"], "kind": "CloudOSO", "before": {}, "messages": [msg]},
            {"use": steady, "removing": [], "kind": "CloudOSO", "before": degraded},
            {"use": steady, "removing": [], "kind": "CloudOSO", "before": {}},
            {"use": {}, "removing": [], "kind": "CloudVirt", "before": degraded},
            {"use": steady, "removing": ["routerAttachments"], "kind": "CloudOSO", "before": {},
             "reason": "RouterAdvertisementFailed", "messages": ["the service reports NotYourPort: x"]},
        ])
        self.assertNotIn("status", res[0]["status"])   # the job's ready write: status.status ready
        self.assertEqual(res[0]["conds"], [{"type": "Degraded", "status": "True", "reason": "InUse", "message": msg}])
        self.assertNotIn("status", res[1]["status"])
        self.assertEqual(res[1]["conds"], [{"type": "Degraded", "status": "False", "reason": "Ready", "message": ""}])
        self.assertEqual(res[2]["conds"], [])
        self.assertNotIn("status", res[2]["status"])
        self.assertEqual(res[3]["conds"], [])
        self.assertNotIn("status", res[4]["status"])   # ready; the operator re-runs it (OperatorRerunsAWaitingPlacement)
        self.assertEqual(res[4]["conds"], [{"type": "Degraded", "status": "True", "reason": "RouterAdvertisementFailed",
                                            "message": "the service reports NotYourPort: x"}])

    def test_previous_wait_is_recognised(self):
        task = by_name(load(MAIN), "Start with nothing to wait for")
        expr = task["ansible.builtin.set_fact"]["np_prev_use_wait"]
        steps = [{"ansible.builtin.set_fact": {"fab_st": "{{ case }}", "fab_entity": "acme"}},
                 {"ansible.builtin.set_fact": {"_w": expr}}]
        def deg(reason, status="True"):
            return [{"type": "Ready", "status": "True"}, {"type": "Degraded", "status": status, "reason": reason}]
        res = run_cases(steps, [
            {},
            {"ready": True, "conditions": [{"type": "Ready", "status": "True"}]},
            {"ready": True, "conditions": deg("InUse")},
            {"ready": True, "conditions": deg("RouterAdvertisementFailed")},
            {"ready": True, "conditions": deg("Ready", "False")},
            {"ready": False, "conditions": deg("InUse")},
        ], "_w | bool")
        self.assertEqual(res, [False, False, True, True, False, False])


URL = "https://router-advertisement.example:8443"
RA_TASK = os.path.join(TASKS, "router_advertisement.yml")
TEARDOWN = os.path.join(ROOT, "eda", "rulebooks", "roles", "networkplacement_teardown", "tasks", "main.yml")


def ra_file():
    """router_advertisement.yml with the helper call replaced by a stub that records the request it would send and
    answers case.answer (the helper's JSON), under the same condition."""
    tasks = load(RA_TASK)
    i = next(n for n, t in enumerate(tasks) if t.get("name", "").startswith("Ask the router advertisement service"))
    call = tasks[i]
    tasks[i] = {"name": "stub", "when": call["when"], "vars": call["vars"],
                "ansible.builtin.set_fact": {"os_result": "{{ case.answer }}",
                                             "_calls": "{{ _calls | default([]) + [os_request] }}"}}
    return tasks


def ra_answer(nat=True, wanted=True, reason="", message="", **kw):
    return {"service": "found", "port_id": "port-v4", "nat": nat, "wanted": wanted, "changed": False, "restores": 1,
            "lastRestoredAt": "2026-11-11T00:00:00Z", "reason": reason, "message": message, **kw}


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class RouterAdvertisementInTheJob(unittest.TestCase):
    """backend_cloudoso.yml: when the job asks the site's service, with which state, what it records, and how a
    failing service shows (the helper itself: test_shc_openstack_router_advertisement.py)."""

    def run_job(self, cases):
        tasks = load(BACKEND)
        steps = [{"ansible.builtin.set_fact": {
                    "os_result": "{{ case.place }}", "np_status": {"entity": "acme"}, "fab_spec": "{{ case.spec }}",
                    "fab_st": "{{ case.before | default({}) }}", "np_site": "site-a", "ci_spec": {},
                    "np_backend": {"status": {"projectId": "proj-tenant"}}, "np_names": {"osNetwork": "hn51001"},
                    "fab_real": {"openstack": {"platformProject": "platform", "projectDomain": "shc"},
                                 "placement": {"routerAdvertisementBySite": "{{ case.bysite | default({}) }}"}},
                    "_calls": []}}]
        steps += [by_name(tasks, n) for n in (
            "Record the site side", "Decide on the router advertisement", "The router advertisement on the placed network",
            "Record the router advertisement", "Nothing to advertise on a placement without IPv4",
            "No router advertisement asked", "The environment's uses of the network")]
        return run_cases(steps, cases, "{'calls': _calls, 'site': np_backend_status, 'use': np_status.use, "
                                       "'waiting': np_use_removing, 'reason': np_use_wait_reason, "
                                       "'messages': np_use_wait_messages, 'failed': np_ra_failed, 'note': np_use_note}",
                         files={"router_advertisement.yml": ra_file()})

    def test_cases(self):
        site = {"site-a": {"url": URL}}
        ra_on = {"ipv4": {"prefixLength": 24}, "use": {"routerAttachments": True}}
        # gate F3: the IPv6-only placement of the gate (router attachments only)
        v6_ra = {"ipv6": {"prefixLength": 64}, "use": {"virtualMachines": False, "routerAttachments": True}}
        held = {"realization": {"site": {"routerAdvertisement": {"mode": "Service", "wanted": True}}}}
        res = self.run_job([
            # 0 a placement without router attachments: no call at all, nothing recorded (with or without a service)
            {"place": os_result(), "spec": {}, "bysite": site, "answer": {}},
            # 1 router attachments on: present, confirmed
            {"place": os_result(ra="ready"), "spec": ra_on, "bysite": site, "answer": ra_answer()},
            # 2 the same again (a plain re-run asks again: Neutron may have rewritten the option)
            {"place": os_result(ra="ready"), "spec": ra_on, "bysite": site, "answer": ra_answer(), "before": held},
            # 3 router attachments on, the site has no service: Assumed, no call
            {"place": os_result(ra="ready"), "spec": ra_on, "bysite": {}, "answer": {}},
            # 4 turned off while still in use: kept (present)
            {"place": os_result(ra="removing", ra_users=["floating IP 192.0.2.15"]), "spec": {}, "bysite": site,
             "answer": ra_answer(), "before": held},
            # 5 turned off and unused, the service held it: absent
            {"place": os_result(), "spec": {}, "bysite": site, "answer": ra_answer(nat=False, wanted=False),
             "before": held},
            # 6 off, nothing held any more: no call
            {"place": os_result(), "spec": {},  "bysite": site, "answer": {},
             "before": {"realization": {"site": {"routerAdvertisement": {"mode": "Service", "wanted": False}}}}},
            # 7 the service refuses: the placement stays usable, router attachments pending, re-run
            {"place": os_result(ra="ready"), "spec": ra_on, "bysite": site,
             "answer": ra_answer(nat=False, wanted=False, reason="NotAnAdvertisingPort", message="no option")},
            # 8 absent fails: still held in the record, asked again next run
            {"place": os_result(), "spec": {}, "bysite": site, "before": held,
             "answer": ra_answer(nat=True, wanted=False, reason="OvnUnavailable", message="ovn down")},
            # 9 present answered without nat: not confirmed
            {"place": os_result(ra="ready"), "spec": ra_on, "bysite": site, "answer": ra_answer(nat=False)},
            # 10 the helper itself failed (soft call)
            {"place": os_result(ra="ready"), "spec": ra_on, "bysite": site,
             "answer": {"error": "RuntimeError: platform project platform does not exist"}},
            # 11 an http URL: the helper refuses, the placement stays usable
            {"place": os_result(ra="ready"), "spec": ra_on, "bysite": {"site-a": {"url": "http://ra.example"}},
             "answer": ra_answer(nat=False, wanted=False, reason="InvalidSetting",
                                 message="placement.routerAdvertisementBySite[site-a].url must be an https:// URL")},
            # 12 Review R2 L1: present fails while the last record held it: the record keeps holding it
            {"place": os_result(ra="ready"), "spec": ra_on, "bysite": site, "before": held,
             "answer": ra_answer(nat=True, wanted=False, reason="ServiceUnavailable", message="no answer")},
            # 13 the next run with router attachments off reads that record: absent is asked
            {"place": os_result(), "spec": {}, "bysite": site, "before": held, "answer": ra_answer(nat=False, wanted=False)},
            # 14 a present that fails without an earlier hold records no hold
            {"place": os_result(ra="ready"), "spec": ra_on, "bysite": site,
             "answer": ra_answer(nat=False, wanted=False, reason="ServiceUnavailable", message="no answer")},
            # 15 gate F3: IPv6-only, router attachments on, the site has a service: not asked, NotApplicable, ready
            {"place": os_result(vm="off", ra="ready"), "spec": v6_ra, "bysite": site,
             "answer": ra_answer(nat=False, wanted=False, reason="NoRouterInterface",
                                 message="router rtr-1 has no IPv4 interface on network net-1")},
            # 16 the same without a service
            {"place": os_result(vm="off", ra="ready"), "spec": v6_ra, "bysite": {}, "answer": {}},
        ])
        for r in (res[15], res[16]):
            self.assertEqual(r["calls"], [])
            rec = r["site"]["routerAdvertisement"]
            self.assertEqual((rec["mode"], rec["nat"], rec["wanted"]), ("NotApplicable", False, False))
            self.assertIn("no IPv4 subnet", rec["message"])
            self.assertEqual(r["use"]["routerAttachments"], {"state": "ready", "reason": "", "message": ""})
            self.assertEqual((r["failed"], r["waiting"], r["messages"]), ("", [], []))   # no Degraded, no re-run
        self.assertIs(res[12]["site"]["routerAdvertisement"]["wanted"], True)
        self.assertEqual([c["state"] for c in res[13]["calls"]], ["absent"])
        self.assertIs(res[14]["site"]["routerAdvertisement"]["wanted"], False)
        self.assertEqual(res[0]["calls"], [])
        self.assertNotIn("routerAdvertisement", res[0]["site"])
        self.assertEqual((res[0]["failed"], res[0]["waiting"]), ("", []))

        req = res[1]["calls"][0]
        self.assertEqual({k: req[k] for k in ("action", "state", "router_id", "network_id", "service_url", "site",
                                              "domain", "platformProject")},
                         {"action": "router-advertisement", "state": "present", "router_id": "rtr-1",
                          "network_id": "net-1", "service_url": URL, "site": "site-a", "domain": "shc",
                          "platformProject": "platform"})
        rec = res[1]["site"]["routerAdvertisement"]
        self.assertEqual({k: rec[k] for k in ("mode", "nat", "wanted", "restores", "lastRestoredAt")},
                         {"mode": "Service", "nat": True, "wanted": True, "restores": 1,
                          "lastRestoredAt": "2026-11-11T00:00:00Z"})
        self.assertRegex(rec["checkedAt"], r"^\d{4}-\d\d-\d\dT")
        self.assertEqual(res[1]["use"]["routerAttachments"]["state"], "ready")
        self.assertEqual(res[1]["waiting"], [])
        self.assertEqual([c["state"] for c in res[2]["calls"]], ["present"])

        self.assertEqual(res[3]["calls"], [])
        self.assertEqual(res[3]["site"]["routerAdvertisement"]["mode"], "Assumed")
        self.assertEqual(res[3]["failed"], "")

        self.assertEqual([c["state"] for c in res[4]["calls"]], ["present"])
        self.assertEqual(res[4]["use"]["routerAttachments"]["reason"], "InUse")
        self.assertEqual([c["state"] for c in res[5]["calls"]], ["absent"])
        self.assertIs(res[5]["site"]["routerAdvertisement"]["wanted"], False)
        self.assertEqual(res[5]["waiting"], [])
        self.assertEqual(res[6]["calls"], [])

        r7 = res[7]
        self.assertEqual(r7["failed"], "NotAnAdvertisingPort")
        self.assertEqual(r7["use"]["virtualMachines"]["state"], "ready")
        self.assertEqual(r7["use"]["routerAttachments"]["state"], "pending")
        self.assertEqual(r7["use"]["routerAttachments"]["reason"], "RouterAdvertisementFailed")
        self.assertIn("NotAnAdvertisingPort: no option", r7["use"]["routerAttachments"]["message"])
        self.assertEqual((r7["waiting"], r7["reason"]), (["routerAttachments"], "RouterAdvertisementFailed"))
        self.assertEqual(r7["site"]["rbac"]["shared"], "rbac-1")  # the share is untouched

        r8 = res[8]
        self.assertEqual(r8["failed"], "OvnUnavailable")
        self.assertIs(r8["site"]["routerAdvertisement"]["wanted"], True)
        self.assertEqual(r8["use"]["routerAttachments"]["state"], "off")
        self.assertEqual(r8["waiting"], ["routerAttachments"])
        self.assertEqual(len(r8["messages"]), 1)
        self.assertIn("OvnUnavailable: ovn down", r8["messages"][0])

        self.assertEqual(res[9]["failed"], "NotConfirmed")
        self.assertEqual(res[10]["failed"], "HelperFailed")
        self.assertIn("does not exist", res[10]["use"]["routerAttachments"]["message"])
        self.assertEqual(res[11]["failed"], "InvalidSetting")
        self.assertIn("routerAdvertisementBySite[site-a].url", res[11]["use"]["routerAttachments"]["message"])


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class RouterAdvertisementAtTeardown(unittest.TestCase):
    """networkplacement_teardown: the placement-network absent request carries the service only for a placement that
    used router attachments (the helper asks it after the busy check, before the interface goes)."""

    def test_request(self):
        task = by_name(load(TEARDOWN), "Delete the share, router, subnets and network")
        expr = task["vars"]["os_request"]["routerAdvertisement"]
        steps = [{"ansible.builtin.set_fact": {"fab_spec": "{{ case.spec }}", "fab_st": "{{ case.st }}",
                                               "fab_real": {"placement": {"routerAdvertisementBySite":
                                                                          {"site-a": {"url": URL}}}}}},
                 {"ansible.builtin.set_fact": {"_ra": expr}}]
        held = {"site": "site-a", "realization": {"site": {"routerAdvertisement": {"wanted": True}}}}
        res = run_cases(steps, [
            {"spec": {}, "st": {"site": "site-a"}},
            {"spec": {"use": {"routerAttachments": True}}, "st": {"site": "site-a"}},
            {"spec": {}, "st": held},
            {"spec": {"use": {"routerAttachments": True}}, "st": {"site": "site-b"}},
        ], "_ra")
        self.assertEqual(res[0], {})
        self.assertEqual(res[1], {"service_url": URL, "site": "site-a"})
        self.assertEqual(res[2], {"service_url": URL, "site": "site-a"})
        self.assertEqual(res[3], {"service_url": "", "site": "site-b"})


class PhaseOfAWaitingUse(unittest.TestCase):
    """What the status writer makes of the final write: Ready stays True, phase Degraded, ready not re-run-skipped."""

    def test_ready_kept_and_phase_degraded(self):
        obj = {"metadata": {"name": "p", "generation": 2, "resourceVersion": "7"},
               "status": {"ready": True, "status": "ready", "observedGeneration": 2,
                          "conditions": [{"type": "Ready", "status": "True", "reason": "Ready", "message": "ok"}]}}
        body = {"status": "ready", "ready": True, "message": "m", "observedGeneration": 2,
                "conditions": [{"type": "Ready", "status": "True", "reason": "Ready", "message": "m"},
                               {"type": "Degraded", "status": "True", "reason": "InUse", "message": "waits"}]}
        patch, info = ST.compute_status_patch(obj, body, {"action": "ready"})
        st = patch["status"]
        self.assertEqual(info["phase"], "Degraded")
        self.assertTrue(st["ready"])
        ready = next(c for c in st["conditions"] if c["type"] == "Ready")
        self.assertEqual(ready["status"], "True")
        self.assertEqual(st["status"], "ready")


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class OperatorRerunsAWaitingPlacement(unittest.TestCase):
    """operator/namespace/roles/networkplacement: a ready placement is skipped on the reconcile period unless a use
    waits (status.use removing, or condition Degraded True with reason InUse)."""

    def test_skip_decision(self):
        role = load(os.path.join(ROOT, "operator", "namespace", "roles", "networkplacement", "tasks", "main.yml"))
        facts = by_name(role, "Set NetworkPlacement CR facts")["ansible.builtin.set_fact"]
        skip = by_name(role, "Set event action and reason")["ansible.builtin.set_fact"]["networkplacement_skip_reconcile"]
        steps = [{"ansible.builtin.set_fact": {"_hybridsovereign_redhat_networkplacement": "{{ case }}",
                                               "reconcile_now": False}},
                 {"ansible.builtin.set_fact": facts},
                 {"ansible.builtin.set_fact": {"_skip": skip}}]
        ready = {"metadata": {"generation": 2}, "spec": {},
                 "status": {"status": "ready", "ready": True, "observedGeneration": 2,
                            "use": {"virtualMachines": {"state": "ready"}, "routerAttachments": {"state": "off"}},
                            "conditions": [{"type": "Ready", "status": "True", "reason": "Ready"}]}}
        removing = {**ready, "status": {**ready["status"], "use": {"routerAttachments": {"state": "removing"}}}}
        degraded = {**ready, "status": {**ready["status"], "conditions": ready["status"]["conditions"] + [
            {"type": "Degraded", "status": "True", "reason": "InUse"}]}}
        cleared = {**ready, "status": {**ready["status"], "conditions": ready["status"]["conditions"] + [
            {"type": "Degraded", "status": "False", "reason": "Ready"}]}}
        legacy = {"metadata": {"generation": 1}, "spec": {},
                  "status": {"status": "ready", "ready": True, "observedGeneration": 1}}
        advert = {**ready, "status": {**ready["status"], "conditions": ready["status"]["conditions"] + [
            {"type": "Degraded", "status": "True", "reason": "RouterAdvertisementFailed"}]}}
        other = {**ready, "status": {**ready["status"], "conditions": ready["status"]["conditions"] + [
            {"type": "Degraded", "status": "True", "reason": "SomethingElse"}]}}
        self.assertEqual(run_cases(steps, [ready, removing, degraded, cleared, legacy, advert, other], "_skip | bool"),
                         [True, False, False, True, True, False, True])


class CelRulesAsText(unittest.TestCase):
    def test_spec_use_rules_and_defaults(self):
        import yaml
        with open(CRD) as f:
            crd = yaml.safe_load(f)
        spec = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]
        rules = {r["message"]: " ".join(r["rule"].split()) for r in spec["x-kubernetes-validations"]}
        self.assertEqual(rules["spec.use needs virtualMachines, routerAttachments or both"],
                         "!has(self.use) || self.use.virtualMachines || self.use.routerAttachments")
        self.assertEqual(rules["spec.use is offered only on CloudOSO; other environments use the defaults"],
                         "!has(self.use) || self.backend.kind == 'CloudOSO' || "
                         "(self.use.virtualMachines && !self.use.routerAttachments)")
        use = spec["properties"]["use"]["properties"]
        self.assertEqual((use["virtualMachines"]["default"], use["routerAttachments"]["default"]), (True, False))
        status = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["status"]["properties"]
        self.assertEqual(sorted(status["use"]["properties"]), ["routerAttachments", "virtualMachines"])


if __name__ == "__main__":
    unittest.main()
