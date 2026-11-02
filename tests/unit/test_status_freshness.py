"""Status that must not go stale: the network wakes its placements when its IPv6 internet row changes
(hybridnetwork_provision main.yml; the placements copy the row, ipv6_status.yml); the translation canary's reading
carries its read time and the operator re-runs a Ready fabric whose read is old and marks a too-old reading stale, never
healthy (operator/primary/roles/hybridfabric, hybridfabric_provision main.yml); the realization defaults document the
keys the code reads (eda/common/vars/fabric_realization.yml). The plays run in ansible-playbook (skipped without
it). Generic names only."""
import datetime
import os
import sys
import unittest

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, walk, write_out  # noqa: E402

ROLES = os.path.join(ROOT, "eda", "rulebooks", "roles")
HN = os.path.join(ROLES, "hybridnetwork_provision", "tasks")
HF = os.path.join(ROLES, "hybridfabric_provision", "tasks")
OP = os.path.join(ROOT, "operator", "primary", "roles", "hybridfabric")
DEFAULTS = os.path.join(ROOT, "eda", "common", "vars", "fabric_realization.yml")
WAKE = "Wake the placements whose IPv6 internet row changed"


def ago(minutes):
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def placement(name, network="n1", source="Network", family=6, deleting=False):
    md = {"name": name}
    if deleting:
        md["deletionTimestamp"] = "2026-01-01T00:00:00Z"
    subnets = [{"family": 4, "prefix": "10.60.1.0/24"}]
    if family == 6:
        subnets.append({"family": 6, "prefix": "2001:db8:1::/64", "source": source})
    return {"metadata": md, "spec": {"network": network}, "status": {"subnets": subnets}}


ROW = {"family": 6, "state": "ready", "mode": "Routed"}


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class PlacementWake(unittest.TestCase):
    def wake(self, before, after, placements):
        t = by_name(load(os.path.join(HN, "main.yml")), WAKE)
        probe = {"ansible.builtin.set_fact": {"_due": "{{ %s }}" % t["when"].strip(),
                                              "_crs": t["vars"]["nudge_crs"]}}
        out = run_play([dict(probe, vars={k: v for k, v in t["vars"].items() if k != "nudge_crs"}),
                        write_out("{'due': _due, 'crs': _crs}")],
                       {"fab_st": {"egress": {"families": before}} if before is not None else {},
                        "hn_egress": {"families": after} if after is not None else {},
                        "fab_name": "n1", "fab_namespace": "acme",
                        "_hn_placements": {"resources": placements}})
        return out["due"], out["crs"]

    def test_row_changes_wake_the_fabric_path_placements(self):
        pls = [placement("p-net"), placement("p-prov", source="Provider"), placement("p-v4", family=4),
               placement("p-other", network="n2"), placement("p-gone", deleting=True)]
        degraded = dict(ROW, reason="Degraded", message="m")
        for before, after in (([ROW], [degraded]), ([degraded], [ROW]), ([ROW], []), (None, [ROW]),
                              ([ROW], [dict(ROW, state="removing")])):
            due, crs = self.wake(before, after, pls)
            self.assertTrue(due, (before, after))
            self.assertEqual(crs, [{"kind": "NetworkPlacement", "namespace": "acme", "name": "p-net"}])

    def test_no_wake_while_the_row_stays(self):
        due, _ = self.wake([ROW, {"family": 4, "state": "ready"}], [{"family": 4, "state": "pending"}, ROW],
                           [placement("p-net")])
        self.assertFalse(due)
        self.assertFalse(self.wake(None, None, [])[0])

    def test_after_the_network_status_write(self):
        names = [t.get("name") for t in walk(load(os.path.join(HN, "main.yml")))]
        self.assertLess(names.index("Report the network ready"), names.index(WAKE))


class CanaryReadTime(unittest.TestCase):
    def test_the_fabric_job_records_its_read(self):
        with open(os.path.join(HF, "main.yml")) as fh:
            text = fh.read()
        self.assertIn("'readAt': hf_canary_now | default('', true)", text)
        self.assertIn("'alertMinutes': hf_canary_cfg.alertMinutes | default(5, true) | int", text)
        self.assertIn("'staleAfterMinutes': ([hf_canary_cfg.alertMinutes | default(5, true) | int,\n"
                      "                                                                     hf_canary_reread_min_minutes"
                      " | default(15) | int]\n", text)

    def test_the_job_and_the_operator_say_the_same_interval(self):
        """The job writes staleAfterMinutes from its own copy of the minimum re-read interval; the operator's default
        is the same, at least 900 s, and documented."""
        with open(os.path.join(HF, "..", "defaults", "main.yml")) as fh:
            job = yaml.safe_load(fh)
        with open(os.path.join(OP, "defaults", "main.yml")) as fh:
            text = fh.read()
        op = yaml.safe_load(text)
        self.assertGreaterEqual(op["hybridfabric_canary_reread_min_seconds"], 900)
        self.assertEqual(job["hf_canary_reread_min_minutes"] * 60, op["hybridfabric_canary_reread_min_seconds"])
        self.assertIn("Realization-free", text)
        with open(os.path.join(ROOT, "docs", "platform", "ipv6-realization.md")) as fh:
            doc = fh.read()
        self.assertIn("hybridfabric_canary_reread_min_seconds", doc)
        self.assertIn("staleAfterMinutes: 45", doc)


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class OperatorCanary(unittest.TestCase):
    TASKS = ("Age of the translation canary's last read", "Decide whether the canary is due or stale")

    def fabric(self, read_at, sites=None, cond=None, alert=5, completed=None, stale_after=None):
        tr = {"canaries": sites if sites is not None else
              {"site-a": {"stale": False, "healthy": True, "error": "", "failedSince": ""},
               "site-b": {"stale": False, "healthy": True, "error": "", "failedSince": ""}},
              "alertMinutes": alert, "staleAfterMinutes": stale_after if stale_after is not None else max(alert, 15) * 3}
        if read_at:
            tr["readAt"] = read_at
        st = {"realization": {"translation": tr}, "conditions": [cond] if cond else []}
        if completed:
            st["delivery"] = {"completedAt": completed}
        return {"metadata": {"name": "lab"}, "status": st}

    def decide(self, cr, extra_tasks=(), out="{'due': hybridfabric_canary_due, 'stale': hybridfabric_canary_stale, "
                                             "'marked': hybridfabric_canary_marked}", files=None):
        tasks = load(os.path.join(OP, "tasks", "main.yml"))
        ts = [by_name(tasks, n) for n in self.TASKS] + list(extra_tasks)
        with open(os.path.join(OP, "defaults", "main.yml")) as fh:
            defaults = yaml.safe_load(fh)
        return run_play(ts + [write_out(out)], dict(defaults, _hybridsovereign_redhat_hybridfabric=cr,
                                                    ansible_operator_meta={"name": "lab", "namespace": "platform"}),
                        files=files)

    def test_fresh_read_is_neither_due_nor_stale(self):
        self.assertEqual(self.decide(self.fabric(ago(1))), {"due": False, "stale": False, "marked": False})

    def test_due_after_the_interval_from_the_jobs_end(self):
        # at least 15 minutes (the operator default, 900 s) with alertMinutes 5
        self.assertFalse(self.decide(self.fabric(ago(6)))["due"])
        self.assertFalse(self.decide(self.fabric(ago(14)))["due"])
        self.assertEqual(self.decide(self.fabric(ago(16))), {"due": True, "stale": False, "marked": False})
        # alertMinutes above the minimum is the interval
        self.assertFalse(self.decide(self.fabric(ago(25), alert=30))["due"])
        self.assertTrue(self.decide(self.fabric(ago(31), alert=30))["due"])

    def test_the_interval_counts_from_the_jobs_completion(self):
        """A job that read at T and finished minutes later: the interval starts at its completion, so the next run never
        follows it back to back; a completion older than the read (a job before it) does not shorten anything."""
        self.assertFalse(self.decide(self.fabric(ago(20), completed=ago(10)))["due"])
        self.assertTrue(self.decide(self.fabric(ago(20), completed=ago(16)))["due"])
        self.assertFalse(self.decide(self.fabric(ago(10), completed=ago(40)))["due"])
        # no read recorded yet: due at once
        self.assertTrue(self.decide(self.fabric(None, completed=ago(1)))["due"])

    def test_stale_after_three_intervals(self):
        out = self.decide(self.fabric(ago(46)))
        self.assertEqual((out["due"], out["stale"]), (True, True))
        # the steady state's oldest read (interval + reconcilePeriod + a long job, about 30 min) is not stale
        self.assertEqual(self.decide(self.fabric(ago(31), completed=ago(29))), {"due": True, "stale": False,
                                                                                 "marked": False})
        # a job that wrote the older 3 x alertMinutes (15) does not make the operator mark early
        out = self.decide(self.fabric(ago(20), stale_after=15), out="{'stale': hybridfabric_canary_stale, "
                                                                    "'minutes': hybridfabric_canary_stale_minutes}")
        self.assertEqual((out["stale"], int(out["minutes"])), (False, 45))
        # a status written before the read time existed: stale at once (the re-run then records it)
        self.assertEqual(self.decide(self.fabric(None))["stale"], True)

    def test_no_canary_no_rerun(self):
        self.assertEqual(self.decide(self.fabric(ago(600), sites={})), {"due": False, "stale": False, "marked": False})

    def test_the_mark_never_leaves_a_healthy_site(self):
        tasks = load(os.path.join(OP, "tasks", "main.yml"))
        mark = dict(by_name(tasks, "Mark an old translation canary reading stale"))
        status = mark.pop("operator_sdk.util.k8s_status")["status"]
        mark.pop("failed_when", None)
        mark["ansible.builtin.set_fact"] = {"written": status}
        cr = self.fabric(ago(50), sites={"site-a": {"stale": False, "healthy": True, "error": "", "failedSince": ""},
                                         "site-b": {"stale": True, "healthy": False, "error": "x",
                                                    "failedSince": "2026-01-01T00:00:00Z"}})
        out = self.decide(cr, [mark], out="written")
        sites = out["realization"]["translation"]["canaries"]
        self.assertEqual(sorted(sites), ["site-a", "site-b"])
        for s in sites.values():
            self.assertEqual((s["stale"], s["healthy"]), (True, False))
            self.assertIn("the reading is stale", s["error"])
        self.assertEqual(sites["site-a"]["failedSince"], cr["status"]["realization"]["translation"]["readAt"])
        self.assertEqual(sites["site-b"]["failedSince"], "2026-01-01T00:00:00Z")
        cond = out["conditions"][0]
        self.assertEqual((cond["type"], cond["status"], cond["reason"]), ("IPv6Translation", "Unknown", "CanaryStale"))

    def test_marked_once(self):
        stale_sites = {"site-a": {"stale": True, "healthy": False, "error": "x", "failedSince": ago(60)}}
        cond = {"type": "IPv6Translation", "status": "Unknown", "reason": "CanaryStale"}
        self.assertTrue(self.decide(self.fabric(ago(60), sites=stale_sites, cond=cond))["marked"])
        cond_ok = {"type": "IPv6Translation", "status": "True", "reason": "Ready"}
        self.assertFalse(self.decide(self.fabric(ago(60), sites=stale_sites, cond=cond_ok))["marked"])

    def test_wiring(self):
        tasks = load(os.path.join(OP, "tasks", "main.yml"))
        names = [t.get("name") for t in walk(tasks)]
        mark = names.index("Mark an old translation canary reading stale")
        self.assertLess(names.index("Read the Retry-now request"), names.index(self.TASKS[0]))
        self.assertLess(mark, names.index("Skip when already processed"))
        self.assertLess(mark, names.index("Skip while the last event is in flight"))
        skip = by_name(tasks, "Set event action and reason")["ansible.builtin.set_fact"]["hybridfabric_skip_reconcile"]
        self.assertIn("not (hybridfabric_canary_due | bool)", skip)
        self.assertEqual(by_name(tasks, "Mark an old translation canary reading stale")["failed_when"], False)


class RealizationDefaults(unittest.TestCase):
    def test_documented_keys_keep_behaviour(self):
        with open(DEFAULTS) as fh:
            d = yaml.safe_load(fh)["fabric_realization_defaults"]
        # no platform reach unless sources are given; the ports the code falls back to; the site's own domain
        self.assertEqual(d["egress"]["platformSources"], [])
        self.assertEqual(d["egress"]["platformPorts"], [6443, 443])
        self.assertEqual(d["openstack"]["projectDomain"], "")
        sys.path.insert(0, os.path.join(ROOT, "eda", "common", "files"))
        import shc_fabric as F
        with open(F.__file__) as fh:
            src = fh.read()
        self.assertIn('eg.get("platformPorts") or [6443, 443]', src)
        with open(os.path.join(ROLES, "cloudgateway_provision", "tasks", "site_openstack.yml")) as fh:
            site = fh.read()
        self.assertIn("fab_real.openstack.projectDomain | default((ci_spec.openstack | default({})).projectDomain, true)",
                      site)


if __name__ == "__main__":
    unittest.main()
