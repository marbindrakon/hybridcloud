"""The whole state space of a cluster's installation run: what one run decides for each recorded state, each thing
the hub can hold and each kind of asking, checked against what the code decides (the decision functions of
module_utils shc_install, the role's own conditions read from its task files, and the release step's conditions run
in Ansible for every combination). On top of the table, invariants over all cells: nothing is removed beside a live
or unknown build; nobody asked starts nothing; no dead end; no Stalled on a running state; impossible cells listed.

Example names only.
"""
import copy
import datetime
import json
import os
import re
import sys
import unittest

import yaml

sys.path.insert(0, os.path.dirname(__file__))
import po_harness as H  # noqa: E402

LIB = os.path.join(H.EDA, "rulebooks", "collections", "ansible_collections", "hybridsovereign", "shc", "plugins",
                   "module_utils")
sys.path.insert(0, LIB)
import shc_install as I  # noqa: E402

TASKS = os.path.join(H.EDA, "rulebooks", "roles", "platformopenshift_provision", "tasks")
NOW = datetime.datetime.now(datetime.timezone.utc)
SETTINGS = {"windowMinutes": 120, "deadlineMinutes": 360, "writeEveryMinutes": 5, "provider": "openstack"}
PENDING_MINUTES = 10


def ago(minutes):
    return I.fmt_time(NOW - datetime.timedelta(minutes=minutes))


# ---- the axes -------------------------------------------------------------------------------------------------------
RECORDS = ("NONE", "PREPARING", "CLEANINGUP", "INSTALLING", "FINISHING", "INSTALLED", "STOPPED", "OVERDUE")
HUBS = ("NO_RELEASE", "RELEASE_FAILED", "PENDING_YOUNG", "PENDING_OLD", "PENDING_NO_TIME", "DEPLOYED_NO_CD",
        "PROVISIONING", "RETRYING", "ALL_FAILED", "STOPPED", "INSTALLED", "DELETING", "UNREADABLE")
ASKED = ("nobody", "retry", "setting", "other", "both")

# outcomes of one run
RELEASE = "make the release (then watch)"
REMOVE = "remove the stopped build, then install"
WATCH = "watch the running installation"
STOP = "write the stop"
READY = "installed path, then Ready"
RESUME = "resume: the hub's state replaces the recorded stop"
KEEP = "keep the stop and complete the event"
STARTING = "say the installation is starting and hand over"
HANDOVER = "hand over (event completed, nothing changed)"
OVERDUE_CHECK = "one check of the overdue installation, then end"
WAIT_HUB = "watch with nothing to conclude (hub unreadable), then hand over"
X = None   # impossible

# ---- the expected table (rows: recorded state; columns: the hub) ----------------------------------------------------
# For STOPPED, an entry is (nobody asked or another spec change, a Retry or a setting change or both).
NEW = {"NO_RELEASE": RELEASE, "UNREADABLE": WAIT_HUB}
RUNNING = {"NO_RELEASE": RELEASE, "RELEASE_FAILED": RELEASE, "PENDING_YOUNG": STARTING, "PENDING_OLD": RELEASE,
           "PENDING_NO_TIME": RELEASE, "DEPLOYED_NO_CD": RELEASE, "PROVISIONING": WATCH, "RETRYING": WATCH,
           "ALL_FAILED": WATCH, "STOPPED": STOP, "INSTALLED": READY, "UNREADABLE": WAIT_HUB}
TABLE = {
    "NONE": NEW,
    "PREPARING": NEW,
    "CLEANINGUP": {"NO_RELEASE": REMOVE, "RELEASE_FAILED": REMOVE, "PENDING_YOUNG": STARTING, "PENDING_OLD": REMOVE,
                   "PENDING_NO_TIME": REMOVE, "DEPLOYED_NO_CD": REMOVE, "STOPPED": REMOVE, "DELETING": HANDOVER,
                   "UNREADABLE": HANDOVER},
    "INSTALLING": RUNNING,
    "OVERDUE": dict(RUNNING, PROVISIONING=OVERDUE_CHECK, RETRYING=OVERDUE_CHECK, ALL_FAILED=OVERDUE_CHECK,
                    UNREADABLE=HANDOVER),
    "FINISHING": {"INSTALLED": READY, "UNREADABLE": WAIT_HUB},
    "INSTALLED": {"INSTALLED": READY, "UNREADABLE": WAIT_HUB},
    "STOPPED": {"NO_RELEASE": (KEEP, REMOVE), "RELEASE_FAILED": (KEEP, REMOVE), "PENDING_YOUNG": (KEEP, STARTING),
                "PENDING_OLD": (KEEP, REMOVE), "PENDING_NO_TIME": (KEEP, REMOVE), "DEPLOYED_NO_CD": (KEEP, REMOVE),
                "PROVISIONING": RESUME, "RETRYING": RESUME, "ALL_FAILED": RESUME, "STOPPED": (KEEP, REMOVE),
                "INSTALLED": RESUME, "DELETING": KEEP, "UNREADABLE": KEEP},
}
# why a cell is impossible (a missing entry above)
IMPOSSIBLE_WHY = {
    "NONE": "a new object has no release and no build: the release comes after the run records Installing",
    "PREPARING": "the same: no release or build exists before Installing is recorded",
    "CLEANINGUP": "a removal records CleaningUp beside a stopped build and nothing makes a new one until it ends",
    "INSTALLING": "a build is deleted only by a removal (recorded CleaningUp) or the object's delete (no provision run)",
    "OVERDUE": "the same as Installing",
    "FINISHING": "Finishing is recorded only from an installed build, and Hive never uninstalls one",
    "INSTALLED": "the same as Finishing",
}
# what a waiting cell waits on (it ends by itself)
WAITS_ON = {
    STARTING: "an older job's release: it makes the ClusterDeployment, or after pendingReleaseMinutes (10) the next "
              "run makes the release again",
    WATCH: "Hive: the installation ends, stops, or passes the deadline (360 minutes)",
    OVERDUE_CHECK: "Hive: checked at every periodic run until it ends or stops",
    WAIT_HUB: "the hub answering again (the next periodic run)",
    HANDOVER: "Hive's deprovision of the build being deleted, or the hub answering again",
    KEEP: "a Retry or a setting change (the tenant), or the hub answering / the build's deletion ending",
}


def expected(rec, hub, asked):
    entry = TABLE[rec].get(hub, X)
    if isinstance(entry, tuple):
        return entry[1] if asked in ("retry", "setting", "both") else entry[0]
    return entry


# ---- fixtures -------------------------------------------------------------------------------------------------------
SPEC = {"type": "openstack", "openstack": {"environment": "env", "workerCount": 3}}
SMALLER = {"type": "openstack", "openstack": {"environment": "env", "workerCount": 0}}
BUILD = "shc-oso-example-demo-0a1b2c3d"


def cond(ctype, status, reason="", message=""):
    return {"type": ctype, "status": status, "reason": reason, "message": message}


def obj_for(rec, asked, stalled=False):
    install = {
        "NONE": None,
        "PREPARING": {"stage": "Preparing", "run": 1},
        "CLEANINGUP": {"stage": "CleaningUp", "run": 2, "observedAt": ago(2)},
        "INSTALLING": {"stage": "Installing", "run": 1, "startedAt": ago(20), "observedAt": ago(1)},
        "OVERDUE": {"stage": "Installing", "run": 1, "startedAt": ago(400), "observedAt": ago(10)},
        "FINISHING": {"stage": "Finishing", "run": 1, "startedAt": ago(50), "observedAt": ago(1)},
        "INSTALLED": {"stage": "Installed", "run": 1, "endedAt": ago(100)},
        "STOPPED": {"stage": "Stopped", "run": 1, "startedAt": ago(60), "endedAt": ago(30), "stoppedGeneration": 2,
                    "stoppedRequest": "req-1", "stoppedSpec": I.spec_digest(SPEC),
                    "stoppedSpecVersion": I.DIGEST_VERSION, "attempt": 3, "attemptsLimit": 3,
                    "lastFailure": {"attempt": 3, "reason": "QuotaExceeded", "message": "m", "time": ago(31)}},
    }[rec]
    spec = copy.deepcopy(SPEC)
    generation = 2
    handled = "req-1"
    if asked in ("setting", "both"):
        spec, generation = copy.deepcopy(SMALLER), 3
    if asked == "other":
        spec["toolRbac"] = {"clusterAdminRbac": ["admins"]}
        generation = 3
    if asked in ("retry", "both"):
        handled = "req-2"
    status = {"lastHandledReconcileRequest": handled, "buildName": BUILD,
              "delivery": {"eventId": "ev-1", "acknowledgedEventId": "ev-1", "jobStartedAt": ago(1),
                           "publishedAt": ago(2), "reason": "PlatformOpenshiftCreateRequested"}}
    if install is not None:
        status["install"] = install
        status["installStarted"] = rec not in ("PREPARING",)
    if rec == "INSTALLED":
        status["clusterInstalled"] = True
    conds = []
    if rec == "STOPPED" or stalled:
        conds.append(cond("Stalled", "True", "QuotaExceeded", "m"))
    status["conditions"] = conds
    return {"metadata": {"name": "demo", "namespace": "entity-example", "uid": "uid-1", "generation": generation},
            "spec": spec, "status": status}


def cd(installed=False, stopped=False, failed=False, restarts=0, deleting=False):
    conds = [cond("Provisioned", "True" if installed else "False", "Provisioned" if installed else "Provisioning")]
    if stopped:
        conds.append(cond("ProvisionStopped", "True", "InstallAttemptsLimitReached"))
    if failed:
        conds.append(cond("ProvisionFailed", "True", "UnknownError", "x"))
    meta = {"name": BUILD, "namespace": BUILD}
    if deleting:
        meta["deletionTimestamp"] = ago(1)
    return {"metadata": meta, "spec": {"installed": installed, "installAttemptsLimit": 3},
            "status": {"installRestarts": restarts, "conditions": conds}}


def hub_for(name):
    """(ClusterDeployment or None, helm status info or None, the hub readable)."""
    return {
        "NO_RELEASE": (None, None, True),
        "RELEASE_FAILED": (None, {"status": "failed", "last_deployed": ago(30)}, True),
        "PENDING_YOUNG": (None, {"status": "pending-install", "last_deployed": ago(2)}, True),
        "PENDING_OLD": (None, {"status": "pending-install", "last_deployed": ago(PENDING_MINUTES + 20)}, True),
        "PENDING_NO_TIME": (None, {"status": "pending-upgrade"}, True),
        "DEPLOYED_NO_CD": (None, {"status": "deployed", "last_deployed": ago(1)}, True),
        "PROVISIONING": (cd(), {"status": "deployed", "last_deployed": ago(20)}, True),
        "RETRYING": (cd(failed=True, restarts=1), {"status": "deployed", "last_deployed": ago(20)}, True),
        "ALL_FAILED": (cd(failed=True, restarts=3), {"status": "deployed", "last_deployed": ago(40)}, True),
        "STOPPED": (cd(stopped=True, failed=True, restarts=3), {"status": "deployed", "last_deployed": ago(40)}, True),
        "INSTALLED": (cd(installed=True), {"status": "deployed", "last_deployed": ago(60)}, True),
        "DELETING": (cd(stopped=True, failed=True, restarts=3, deleting=True),
                     {"status": "deployed", "last_deployed": ago(40)}, True),
        # neither the ClusterDeployment nor the release can be read (helm status fails: no release known)
        "UNREADABLE": (None, None, False),
    }[name]


# ---- what the code decides -------------------------------------------------------------------------------------------
def _yaml(name):
    with open(os.path.join(TASKS, name)) as fh:
        return yaml.safe_load(fh)


def _states_in(when, name):
    m = re.search(r"%s\.hub \| default\(''\)\)? (not )?in \[([^\]]*)\]" % re.escape(name), json.dumps(when))
    return (m.group(1) or "", set(re.findall(r"'(\w+)'", m.group(2)))) if m else ("", set())


def main_first_look():
    """main.yml: which hub states resume a recorded stop, which keep it (read from its conditions)."""
    block = _yaml("main.yml")[0]["block"]
    resume = next(t for t in block if t.get("name") == "The hub's build runs or is installed")
    keep = next(t for t in block if t.get("name") == "The hub's build is being removed or cannot be read")
    return _states_in(resume["when"], "po_stopped_hub")[1], _states_in(keep["when"], "po_stopped_hub")[1]


def removal_branches():
    tasks = _yaml("remove_stopped_build.yml")
    named = {t.get("name"): t for t in tasks}
    resume = named["The hub's build runs or is installed"]
    starting = named["The hub's release of the build is still being made"]
    handover = named["Hand a removal under way over to the next run"]
    return (_states_in(resume["when"], "rsb_decision")[1], json.dumps(starting["when"]),
            json.dumps(handover["when"]))


def flag_combos():
    """Every (file, stage, release, ClusterDeployment exists) the release step meets in the table."""
    out = set()
    for rec in RECORDS:
        for hub in HUBS:
            if TABLE[rec].get(hub) is X:
                continue
            cdx, rel, ok = hub_for(hub)
            stage = (obj_for(rec, "nobody")["status"].get("install") or {}).get("stage", "")
            for f in ("openstack_clusterbuild.yml", "aws_clusterbuild_deploy.yml"):
                out.add((f, stage, json.dumps(rel, sort_keys=True), cdx is not None or not ok, not ok))
    return sorted(out, key=str)


class Release:
    """The release step's own condition ("Set values changed flag"), run in Ansible for every combination: whether
    the release is made, and whether a younger pending release hands over."""
    results = None

    @classmethod
    def load(cls):
        if cls.results is not None:
            return cls.results
        combos = flag_combos()
        tasks = []
        for i, (f, stage, rel, cd_exists, unreadable) in enumerate(combos):
            task = copy.deepcopy(next(t for t in _yaml(f) if t.get("name") == "Set values changed flag"))
            relv = json.loads(rel)
            status = {"resources": [{"status": {"install": {"stage": stage}}}]}
            task["vars"].update({
                "_po_now": status, "platformopenshift_cr": status, "po_cd_exists": cd_exists, "po_adopted": False,
                "po_hub_unreadable": unreadable,
                "cb_helm_release_exists": relv is not None, "cb_helm_release_failed": (relv or {}).get("status") == "failed",
                "values_diff_result": {"stdout": "false"},
                "helm_release_status": {"stdout": json.dumps({"info": relv}) if relv else ""},
                "platformopenshift_install": {"pendingReleaseMinutes": PENDING_MINUTES}})
            tasks.append(task)
            tasks.append({"ansible.builtin.set_fact": {"_r%d" % i: "{{ [cb_values_changed | bool, "
                                                                     "cb_release_young | bool] }}"}})
        rc, tr, out = H.run("platformopenshift_provision", "main.yml", {}, tasks=tasks,
                            result="[" + ", ".join("_r%d" % i for i in range(len(combos))) + "]")
        assert rc == 0, out
        cls.results = dict(zip(combos, tr[-1]["result"]))
        return cls.results


def removal(obj, cdx, rel, ok):
    young = I.release_young((rel or {}).get("status"), (rel or {}).get("last_deployed"), NOW, PENDING_MINUTES)
    allowed, _ = I.may_remove_build(obj, cdx, ok, release_failed=(rel or {}).get("status") == "failed",
                                    release_exists=rel is not None, release_young=young)
    hub = I.hub_state(cdx, ok)
    if hub == "none" and young:
        hub = "starting"
    stage = obj["status"]["install"]["stage"]
    resume_states, starting_when, handover_when = removal_branches()
    if allowed:
        return REMOVE
    if hub == "starting" and "starting" in starting_when:
        return STARTING
    if stage in ("Stopped", "CleaningUp") and hub in resume_states:
        return RESUME
    if stage == "Stopped":
        return KEEP
    if stage == "CleaningUp" and "CleaningUp" in handover_when:
        return HANDOVER
    return "removal: no branch"


def watch(obj, cdx, ok, once=False):
    res = I.observe(obj, {"uid": "uid-1", "eventId": "ev-1", "startedAt": ago(1)}, cdx, None, NOW, SETTINGS,
                    hub_error=not ok)
    if res["outcome"] == "installed":
        return READY, res
    if res["outcome"] == "stopped":
        return STOP, res
    if res["outcome"] == "overdue":
        return OVERDUE_CHECK, res
    if not ok:
        return (HANDOVER if once else WAIT_HUB), res
    return WATCH, res


def actual(rec, hub, asked):
    obj = obj_for(rec, asked)
    cdx, rel, ok = hub_for(hub)
    if rec == "STOPPED":
        resume_states, keep_states = main_first_look()
        state = I.hub_state(cdx, ok)
        if state in resume_states:
            return RESUME
        if state in keep_states:
            return KEEP
        if not I.asked_since_stop(obj)[0]:
            return KEEP
        return removal(obj, cdx, rel, ok)
    if rec == "CLEANINGUP":
        return removal(obj, cdx, rel, ok)
    cd_exists = cdx is not None or not ok
    if rec == "OVERDUE" and cd_exists:
        return watch(obj, cdx, ok, once=True)[0]
    stage = (obj["status"].get("install") or {}).get("stage", "")
    results = Release.load()
    outs = set()
    for f in ("openstack_clusterbuild.yml", "aws_clusterbuild_deploy.yml"):
        changed, young = results[(f, stage, json.dumps(rel, sort_keys=True), cd_exists, not ok)]
        if young:
            outs.add(STARTING)
        elif changed and cdx is None:
            outs.add(RELEASE)
        else:
            outs.add(watch(obj, cdx, ok)[0])
    assert len(outs) == 1, (rec, hub, outs)   # the two providers' release steps decide the same
    return outs.pop()


def cells():
    for rec in RECORDS:
        for hub in HUBS:
            for asked in ASKED:
                yield rec, hub, asked


@unittest.skipUnless(H.HAVE_ANSIBLE, "ansible-playbook not installed")
class StateSpace(unittest.TestCase):
    def test_every_cell_is_decided_as_the_table_says(self):
        wrong = []
        for rec, hub, asked in cells():
            want = expected(rec, hub, asked)
            if want is X:
                continue
            got = actual(rec, hub, asked)
            if got != want:
                wrong.append((rec, hub, asked, want, got))
        self.assertEqual(wrong, [])

    def test_impossible_cells_are_listed(self):
        """Every missing entry has its reason; STOPPED has none (a recorded stop meets every hub state)."""
        for rec in RECORDS:
            missing = [h for h in HUBS if TABLE[rec].get(h) is X]
            if missing:
                self.assertIn(rec, IMPOSSIBLE_WHY, rec)
        self.assertEqual([h for h in HUBS if TABLE["STOPPED"].get(h) is X], [])
        self.assertEqual(sorted(h for h in HUBS if TABLE["INSTALLING"].get(h) is X), ["DELETING"])

    def test_nothing_is_removed_beside_a_live_or_unknown_build(self):
        for rec, hub, asked in cells():
            if expected(rec, hub, asked) is X:
                continue
            if hub in ("PROVISIONING", "RETRYING", "ALL_FAILED", "INSTALLED", "DELETING", "UNREADABLE"):
                self.assertNotEqual(actual(rec, hub, asked), REMOVE, (rec, hub, asked))

    def test_nobody_asked_starts_nothing(self):
        """A recorded stop that nobody asked to start again (or only another spec change) is kept or resumed;
        nothing else removes a build, and only a create (no install block, Preparing) or an installation recorded as
        running (Installing, overdue: the release made again) makes a release."""
        for rec, hub, asked in cells():
            if expected(rec, hub, asked) is X:
                continue
            got = actual(rec, hub, asked)
            if rec == "STOPPED" and asked in ("nobody", "other"):
                self.assertIn(got, (KEEP, RESUME), (hub, asked))
            if rec in ("INSTALLING", "OVERDUE", "FINISHING", "INSTALLED", "NONE", "PREPARING"):
                self.assertNotEqual(got, REMOVE, (rec, hub))
            if got == RELEASE:
                self.assertIn(rec, ("NONE", "PREPARING", "INSTALLING", "OVERDUE"), (rec, hub, asked))

    def test_no_dead_end(self):
        """In every cell where the cluster is not installed the run moves the state forward, a Retry in that cell
        does, or the cell waits on something that ends by itself (named in WAITS_ON)."""
        forward = {RELEASE, REMOVE, RESUME, STOP, READY}
        for rec, hub, asked in cells():
            want = expected(rec, hub, asked)
            if want is X or hub == "INSTALLED" or rec == "INSTALLED":
                continue
            got = actual(rec, hub, asked)
            if got in forward:
                continue
            self.assertIn(got, WAITS_ON, (rec, hub, asked, got))
            if got == KEEP:
                retry = actual(rec, hub, "retry")
                self.assertTrue(retry in forward or retry == STARTING or hub in ("DELETING", "UNREADABLE"),
                                (rec, hub, asked, retry))
            if got == STARTING:
                # after pendingReleaseMinutes the same cell is old: the run moves forward
                self.assertIn(actual(rec, "PENDING_OLD", asked), forward, (rec, asked))

    def test_no_stalled_on_a_running_state(self):
        """A run that treats the installation as running never leaves Stalled=True (a stale one is cleared)."""
        for rec in ("INSTALLING", "OVERDUE", "FINISHING"):
            for hub in ("PROVISIONING", "RETRYING", "ALL_FAILED", "INSTALLED"):
                if expected(rec, hub, "nobody") is X:
                    continue
                cdx, rel, ok = hub_for(hub)
                _, res = watch(obj_for(rec, "nobody", stalled=True), cdx, ok)
                stalled = [c for c in res["conditions"] if c["type"] == "Stalled"]
                self.assertEqual([c["status"] for c in stalled], ["False"], (rec, hub))
        resume = _yaml("install_resume.yml")[0]["vars"]["cr_status_body"]["conditions"]
        self.assertIn({"type": "Stalled", "status": "False", "reason": "AsExpected", "message": ""}, resume)
        cleaning = next(t for t in _yaml("remove_stopped_build.yml")
                        if t.get("name") == "Remove the stopped build before a new installation")["block"]
        for t in cleaning[:2]:
            self.assertIn({"type": "Stalled", "status": "False", "reason": "AsExpected", "message": ""},
                          t["vars"]["cr_status_body"]["conditions"])

    def test_an_adopted_build_gets_no_release(self):
        """After a resume (the hub's build runs or is installed) the release step leaves the release alone."""
        for f in ("openstack_clusterbuild.yml", "aws_clusterbuild_deploy.yml"):
            task = copy.deepcopy(next(t for t in _yaml(f) if t.get("name") == "Set values changed flag"))
            status = {"resources": [{"status": {"install": {"stage": "Stopped"}}}]}
            task["vars"].update({"_po_now": status, "platformopenshift_cr": status, "po_cd_exists": True,
                                 "po_adopted": True, "cb_helm_release_exists": True, "cb_helm_release_failed": False,
                                 "values_diff_result": {"stdout": "true"}, "helm_release_status": {"stdout": ""},
                                 "platformopenshift_install": {"pendingReleaseMinutes": PENDING_MINUTES}})
            rc, tr, out = H.run("platformopenshift_provision", "main.yml", {}, tasks=[task],
                                result="cb_values_changed | bool")
            self.assertEqual((rc, tr[-1]["result"]), (0, False), f)


def table_markdown():
    """The table as it stands in the record (python3 -c 'import test_install_states as T; print(T.table_markdown())')."""
    head = "| recorded \\ hub | " + " | ".join(HUBS) + " |"
    lines = [head, "|" + "---|" * (len(HUBS) + 1)]
    for rec in RECORDS:
        row = []
        for hub in HUBS:
            e = TABLE[rec].get(hub, X)
            if e is X:
                row.append("impossible")
            elif isinstance(e, tuple):
                row.append("nobody/other: %s; Retry/setting: %s" % e)
            else:
                row.append(e)
        lines.append("| %s | %s |" % (rec, " | ".join(row)))
    return "\n".join(lines)


if __name__ == "__main__":
    unittest.main()
