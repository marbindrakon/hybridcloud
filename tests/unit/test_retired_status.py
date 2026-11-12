"""Status nothing writes any more is removed, not left behind (the status keeps unknown fields): the status library
drops retired condition types on every write (HybridFabric IPv6Translation), the fabric job's ready write removes
status.realization.translation (a null in the merge patch), and the site job removes the CloudInfrastructure's
retired probe record with a partial write while it is there and writes nothing once it is gone. Once clean, a write
changes nothing more, and nothing starts a job for it. The plays run in ansible-playbook (skipped without it)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, walk, write_out  # noqa: E402

LIB = os.path.join(ROOT, "eda", "rulebooks", "collections", "ansible_collections", "hybridsovereign", "shc",
                   "plugins", "module_utils")
sys.path.insert(0, LIB)
import shc_status as S  # noqa: E402

ROLES = os.path.join(ROOT, "eda", "rulebooks", "roles")
FABRIC = os.path.join(ROLES, "hybridfabric_provision", "tasks", "main.yml")
SITE = os.path.join(ROLES, "cloudinfrastructure_provision", "tasks", "main.yml")
CLEAR = "Remove the retired probe record from the site's status"
NOW = "2026-11-04T12:00:00Z"
# the site's record an earlier release wrote (status.realization.<RECORD>), and the fabric's
RECORD = "ipv6Canary"
OLD_SITE = {RECORD: {"placement": "acme/p-a", "vrf": "t51001", "address": "2001:db8:4005:4200::10",
                     "workload": "acme-vms/probe"},
            "dns": {"zone": "example.org"}}
OLD_FABRIC = {"readAt": NOW, "alertMinutes": 5, "staleAfterMinutes": 45,
              "canaries": {"site-a": {"healthy": False, "stale": True, "checks": {}}}}


def cond(ctype, status="True", reason="Ready"):
    return {"type": ctype, "status": status, "reason": reason, "message": "", "lastTransitionTime": NOW}


def obj(status, generation=3):
    return {"metadata": {"name": "lab", "namespace": "platform", "generation": generation, "resourceVersion": "7"},
            "status": status}


def write(o, body, **opts):
    """One status write as the cr_status module does it: the patch, and the status after it."""
    patch, _ = S.compute_status_patch(o, body, dict({"action": body.get("status") or "reconcile"}, **opts), NOW)
    return patch["status"], S._merge_patch(o.get("status") or {}, patch["status"])


class RetiredConditions(unittest.TestCase):
    def fabric(self):
        return obj({"status": "ready", "ready": True, "observedGeneration": 3,
                    "conditions": [cond("Ready"), cond("IPv6EgressUpstream"),
                                   cond("IPv6Translation", "False", "CanaryFailed")],
                    "realization": {"model": "m", "translation": OLD_FABRIC}})

    def test_a_job_write_drops_a_retired_condition(self):
        body = {"status": "ready", "ready": True, "message": "ok",
                "conditions": [cond("Ready"), cond("IPv6EgressUpstream")],
                "realization": {"model": "m", "translation": None}}
        _, after = write(self.fabric(), body)
        types = [c["type"] for c in after["conditions"]]
        self.assertNotIn("IPv6Translation", types)
        self.assertEqual(types[:2], ["Ready", "IPv6EgressUpstream"])
        self.assertNotIn("translation", after["realization"])
        self.assertEqual(after["realization"]["model"], "m")
        # the next write of the same body: nothing more goes, the conditions are the same
        _, after2 = write(obj(after), body)
        self.assertEqual(after2["conditions"], after["conditions"])
        self.assertEqual(after2["realization"], after["realization"])

    def test_a_partial_write_drops_it_too_and_once_clean_leaves_the_conditions_alone(self):
        patch, after = write(self.fabric(), {"realization": {"x": 1}}, partial=True)
        self.assertNotIn("IPv6Translation", [c["type"] for c in after["conditions"]])
        patch2, _ = write(obj(after), {"realization": {"x": 1}}, partial=True)
        self.assertNotIn("conditions", patch2)

    def test_a_write_never_brings_it_back(self):
        body = {"status": "ready", "conditions": [cond("Ready"), cond("IPv6Translation", "False", "CanaryFailed")]}
        _, after = write(obj({"conditions": [cond("Ready")]}), body)
        self.assertNotIn("IPv6Translation", [c["type"] for c in after["conditions"]])
        self.assertIn("IPv6Translation", S.RETIRED_TYPES)
        # the legacy operator types are still dropped as before
        self.assertTrue(set(S.LEGACY_TYPES) <= set(S.DROPPED_TYPES))


class FabricJobWiring(unittest.TestCase):
    def test_the_only_event_left_is_the_egress_outage(self):
        """The fabric job reports two Warning events (the IPv6 egress upstream outage; the service routing domain not
        realized for a wrong setting, review R4 M2, once per change); nothing else."""
        reasons = {t["kubernetes.core.k8s"]["definition"]["reason"] for t in walk(load(FABRIC))
                   if (t.get("kubernetes.core.k8s") or {}).get("definition", {}).get("kind") == "Event"}
        self.assertEqual(reasons, {"IPv6EgressUpstreamDown", "ServiceNotRealized", "ServiceSettingInvalid"})

    def test_every_included_task_file_exists(self):
        tasks_dir = os.path.dirname(FABRIC)
        for t in walk(load(FABRIC)):
            inc = t.get("ansible.builtin.include_tasks")
            if isinstance(inc, str) and "/" not in inc and "{{" not in inc:
                self.assertTrue(os.path.exists(os.path.join(tasks_dir, inc)), inc)


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class FabricReadyWrite(unittest.TestCase):
    STUBS = {"fab_name": "lab", "hf_ledger": {"allocated": {}, "available": {}}, "hf_node_results": [],
             "fab_real": {"model": "m", "core": {"loopbackPool": "10.0.0.0/24", "linkSubnet": "10.0.1.0/24"},
                          "egress": {"enabled": False}},
             "hf_pe": {"loopbackIp": "10.0.0.1", "sid": 1, "loopbackIp6": ""},
             "hf_egress_status": [], "hf_attach": {"status": [], "skipped": []}, "hf_ipam": {}}

    def render(self, egress6):
        t = by_name(load(FABRIC), "Report the fabric ready")
        step = {"ansible.builtin.set_fact": {"_st": t["vars"]["fab_status"], "_c": t["vars"]["fab_conditions"]}}
        return run_play([step, write_out("{'status': _st, 'conditions': _c}")],
                        dict(self.STUBS, hf_egress6_status=egress6))

    def test_the_ready_write_removes_the_translation_record(self):
        out = self.render({"enabled": False})
        self.assertIn("translation", out["status"]["realization"])
        self.assertIsNone(out["status"]["realization"]["translation"])
        self.assertEqual(out["conditions"], [])
        out = self.render({"enabled": True, "degraded": False, "degradedNodes": [], "degradedSince": ""})
        self.assertEqual([c["type"] for c in out["conditions"]], ["IPv6EgressUpstream"])
        # as the status write applies it: the record goes, the rest of the realization stays; a no-op once gone
        old = obj({"status": "ready", "realization": {"model": "m", "translation": OLD_FABRIC},
                   "conditions": [cond("Ready"), cond("IPv6Translation", "False", "CanaryFailed")]})
        body = dict(out["status"], status="ready", conditions=out["conditions"])
        _, after = write(old, body)
        self.assertNotIn("translation", after["realization"])
        self.assertEqual([c["type"] for c in after["conditions"]][:2], ["Ready", "IPv6EgressUpstream"])
        _, again = write(obj(after), body)
        self.assertEqual(again["realization"], after["realization"])


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class SiteRecord(unittest.TestCase):
    def clear(self, status):
        """The site job's clear, its status write replaced by a fact; None when it writes nothing."""
        t = dict(by_name(load(SITE), CLEAR))
        args = t.pop("hybridsovereign.shc.cr_status")
        for k in ("register", "failed_when"):
            t.pop(k)
        t["ansible.builtin.set_fact"] = {"written": args["status"]}
        cr = {"metadata": {"name": "site-a"}}
        if status is not None:
            cr["status"] = status
        return run_play([t, write_out("written | default(none)")],
                        {"cloudinfrastructure_cr_lookup": {"resources": [cr]}})

    def test_removed_while_it_is_there(self):
        written = self.clear({"realization": dict(OLD_SITE)})
        self.assertEqual(written, {"realization": {RECORD: None}})
        # applied as the partial write: the record goes, the site's other records stay
        _, after = write(obj({"realization": dict(OLD_SITE), "conditions": [cond("Ready")]}), written, partial=True)
        self.assertEqual(after["realization"], {"dns": {"zone": "example.org"}})

    def test_nothing_written_once_it_is_gone(self):
        self.assertIsNone(self.clear({"realization": {"dns": {"zone": "example.org"}}}))
        self.assertIsNone(self.clear({"realization": None}))
        self.assertIsNone(self.clear({}))
        self.assertIsNone(self.clear(None))

    def test_wiring(self):
        tasks = load(SITE)
        names = [t.get("name", "") for t in tasks[0]["block"]]
        # before the paused block: a paused site is cleared too (a status write only)
        self.assertLess(names.index("Set spec/generation facts from the live CR"), names.index(CLEAR))
        self.assertLess(names.index(CLEAR), names.index("A paused site is recorded as paused and left alone"))
        t = by_name(tasks, CLEAR)
        args = t["hybridsovereign.shc.cr_status"]
        self.assertEqual((args["partial"], args["ignore_not_found"], args["action"]), (True, True, "reconcile"))
        self.assertIs(t["failed_when"], False)


if __name__ == "__main__":
    unittest.main()
