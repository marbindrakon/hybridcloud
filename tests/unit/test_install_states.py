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
REMOVAL_MINUTES = 60


def ago(minutes):
    return I.fmt_time(NOW - datetime.timedelta(minutes=minutes))


# ---- the axes -------------------------------------------------------------------------------------------------------
# NONE: a new object (no status.install, nothing started); LEGACY_STARTED / LEGACY_INSTALLED: clusters from before
# installations were recorded (no status.install; installStarted, or clusterInstalled); CLEANINGUP_LONG: a removal
# that started more than removalLimitMinutes ago
RECORDS = ("NONE", "LEGACY_STARTED", "LEGACY_INSTALLED", "PREPARING", "CLEANINGUP", "CLEANINGUP_LONG", "INSTALLING",
           "FINISHING", "INSTALLED", "STOPPED", "OVERDUE")
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
RECORD_MISSING = "refuse: the build of an installed cluster is missing (nothing released or removed)"
REMOVAL_FAILED = "write the removal as failed for the platform team"
X = None   # impossible

# ---- the expected table (rows: recorded state; columns: the hub) ----------------------------------------------------
# For STOPPED, an entry is (nobody asked or another spec change, a Retry or a setting change or both).
NEW = {"NO_RELEASE": RELEASE, "UNREADABLE": HANDOVER}
RUNNING = {"NO_RELEASE": RELEASE, "RELEASE_FAILED": RELEASE, "PENDING_YOUNG": STARTING, "PENDING_OLD": RELEASE,
           "PENDING_NO_TIME": RELEASE, "DEPLOYED_NO_CD": RELEASE, "PROVISIONING": WATCH, "RETRYING": WATCH,
           "ALL_FAILED": WATCH, "STOPPED": STOP, "INSTALLED": READY, "UNREADABLE": HANDOVER}
NO_CD = ("NO_RELEASE", "RELEASE_FAILED", "PENDING_YOUNG", "PENDING_OLD", "PENDING_NO_TIME", "DEPLOYED_NO_CD")
# recorded installed: the installed path beside an installed build; never a release or a removal without one
INSTALLED_ROW = dict({h: RECORD_MISSING for h in NO_CD}, INSTALLED=READY, UNREADABLE=HANDOVER)
CLEANING = {"NO_RELEASE": REMOVE, "RELEASE_FAILED": REMOVE, "PENDING_YOUNG": STARTING, "PENDING_OLD": REMOVE,
            "PENDING_NO_TIME": REMOVE, "DEPLOYED_NO_CD": REMOVE, "STOPPED": REMOVE, "DELETING": HANDOVER,
            "UNREADABLE": HANDOVER}
TABLE = {
    "NONE": NEW,
    "LEGACY_STARTED": RUNNING,
    "LEGACY_INSTALLED": INSTALLED_ROW,
    "PREPARING": NEW,
    "CLEANINGUP": CLEANING,
    "CLEANINGUP_LONG": dict(CLEANING, DELETING=REMOVAL_FAILED),
    "INSTALLING": RUNNING,
    "OVERDUE": dict(RUNNING, PROVISIONING=OVERDUE_CHECK, RETRYING=OVERDUE_CHECK, ALL_FAILED=OVERDUE_CHECK),
    "FINISHING": INSTALLED_ROW,
    "INSTALLED": INSTALLED_ROW,
    "STOPPED": {"NO_RELEASE": (KEEP, REMOVE), "RELEASE_FAILED": (KEEP, REMOVE), "PENDING_YOUNG": (KEEP, STARTING),
                "PENDING_OLD": (KEEP, REMOVE), "PENDING_NO_TIME": (KEEP, REMOVE), "DEPLOYED_NO_CD": (KEEP, REMOVE),
                "PROVISIONING": RESUME, "RETRYING": RESUME, "ALL_FAILED": RESUME, "STOPPED": (KEEP, REMOVE),
                "INSTALLED": RESUME, "DELETING": KEEP, "UNREADABLE": KEEP},
}
# why a cell is impossible (a missing entry above)
IMPOSSIBLE_WHY = {
    "NONE": "a new object has no release and no build: the release comes after the run records Installing",
    "LEGACY_STARTED": "a build is deleted only by a removal or the object's delete (no provision run)",
    "LEGACY_INSTALLED": "Hive never uninstalls a build, and a build of an installed cluster is deleted only with it",
    "CLEANINGUP_LONG": "the same as CleaningUp",
    "PREPARING": "the same: no release or build exists before Installing is recorded",
    "CLEANINGUP": "a removal records CleaningUp beside a stopped build and nothing makes a new one until it ends",
    "INSTALLING": "a build is deleted only by a removal (recorded CleaningUp) or the object's delete (no provision run)",
    "OVERDUE": "the same as Installing",
    "FINISHING": "Finishing is recorded only from an installed build; Hive never uninstalls one, and a build of an "
                 "installed cluster is deleted only with it",
    "INSTALLED": "the same as Finishing",
}
# what a waiting cell waits on (it ends by itself)
WAITS_ON = {
    STARTING: "an older job's release: it makes the ClusterDeployment, or after pendingReleaseMinutes (10) the next "
              "run makes the release again",
    WATCH: "Hive: the installation ends, stops, or passes the deadline (360 minutes)",
    OVERDUE_CHECK: "Hive: checked at every periodic run until it ends or stops",
    HANDOVER: "Hive's deprovision of the build being deleted (at most removalLimitMinutes, 60), or the hub answering "
              "again (the next periodic run)",
    RECORD_MISSING: "the platform team restoring the build's record on the hub, or the object's deletion",
    REMOVAL_FAILED: "the platform team (the build Hive could not delete)",
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
        "LEGACY_STARTED": None,
        "LEGACY_INSTALLED": None,
        "CLEANINGUP_LONG": {"stage": "CleaningUp", "run": 2, "startedAt": ago(70), "observedAt": ago(70)},
        "PREPARING": {"stage": "Preparing", "run": 1},
        "CLEANINGUP": {"stage": "CleaningUp", "run": 2, "startedAt": ago(2), "observedAt": ago(2)},
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
    if rec in ("INSTALLED", "LEGACY_INSTALLED"):
        status["clusterInstalled"] = True
    if rec in ("LEGACY_STARTED", "LEGACY_INSTALLED"):
        status["installStarted"] = True
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
    failed = named["A removal that could not finish in time"]
    handover = named["Hand a removal under way over to the next run"]
    return (_states_in(resume["when"], "rsb_decision")[1], json.dumps(starting["when"]), json.dumps(failed["when"]),
            json.dumps(handover["when"]))


def _status_key(rec):
    st = obj_for(rec, "nobody")["status"]
    return json.dumps({"install": {"stage": (st.get("install") or {}).get("stage", "")} if st.get("install") else {},
                       "installStarted": bool(st.get("installStarted")),
                       "clusterInstalled": bool(st.get("clusterInstalled"))}, sort_keys=True)


def flag_combos():
    """Every (file, recorded status, release, ClusterDeployment exists, hub unreadable) the release step meets."""
    out = set()
    for rec in RECORDS:
        for hub in HUBS:
            if TABLE[rec].get(hub) is X:
                continue
            cdx, rel, ok = hub_for(hub)
            for f in ("openstack_clusterbuild.yml", "aws_clusterbuild_deploy.yml"):
                out.add((f, _status_key(rec), json.dumps(rel, sort_keys=True), cdx is not None or not ok, not ok))
    return sorted(out, key=str)


class Release:
    """The release step's own condition ("Set values changed flag"), run in Ansible for every combination: whether
    the release is made, whether a younger pending release hands over, whether an installed cluster's build is
    missing."""
    results = None

    @classmethod
    def load(cls):
        if cls.results is not None:
            return cls.results
        combos = flag_combos()
        tasks = []
        for i, (f, status_key, rel, cd_exists, unreadable) in enumerate(combos):
            legacy = copy.deepcopy(next(t for t in _yaml(f)
                                        if t.get("name") == "Whether the installation started before installations "
                                                            "were recorded"))
            task = copy.deepcopy(next(t for t in _yaml(f) if t.get("name") == "Set values changed flag"))
            relv = json.loads(rel)
            status = {"resources": [{"status": json.loads(status_key)}]}
            legacy["vars"].update({"_po_now": status, "platformopenshift_cr": status})
            tasks.append(legacy)
            task["vars"].update({
                "_po_now": status, "platformopenshift_cr": status, "po_cd_exists": cd_exists, "po_adopted": False,
                "po_hub_unreadable": unreadable,
                "cb_helm_release_exists": relv is not None, "cb_helm_release_failed": (relv or {}).get("status") == "failed",
                "values_diff_result": {"stdout": "false"},
                "helm_release_status": {"stdout": json.dumps({"info": relv}) if relv else ""},
                "platformopenshift_install": {"pendingReleaseMinutes": PENDING_MINUTES}})
            tasks.append(task)
            tasks.append({"ansible.builtin.set_fact": {"_r%d" % i: "{{ [cb_values_changed | bool, "
                                                                     "cb_release_young | bool, "
                                                                     "cb_record_missing | bool] }}"}})
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
    resume_states, starting_when, failed_when, handover_when = removal_branches()
    if allowed:
        return REMOVE
    if hub == "starting" and "starting" in starting_when:
        return STARTING
    if stage in ("Stopped", "CleaningUp") and hub in resume_states:
        return RESUME
    if stage == "Stopped":
        return KEEP
    if (stage == "CleaningUp" and hub == "deleting" and "removal_overdue" in failed_when
            and I.removal_overdue(obj["status"], NOW, REMOVAL_MINUTES)):
        return REMOVAL_FAILED
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
        return HANDOVER, res
    return WATCH, res


def actual(rec, hub, asked):
    obj = obj_for(rec, asked)
    cdx, rel, ok = hub_for(hub)
    # main.yml's first step for a cluster recorded as installed (the hub's build missing: nothing more happens)
    guard = main_installed_guard()
    if guard and I.recorded_installed(obj["status"]) and I.build_record_missing(obj["status"], cdx, ok):
        return RECORD_MISSING
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
    if rec in ("CLEANINGUP", "CLEANINGUP_LONG"):
        return removal(obj, cdx, rel, ok)
    cd_exists = cdx is not None or not ok
    if rec == "OVERDUE" and cd_exists:
        return watch(obj, cdx, ok, once=True)[0]
    results = Release.load()
    outs = set()
    for f in ("openstack_clusterbuild.yml", "aws_clusterbuild_deploy.yml"):
        changed, young, missing = results[(f, _status_key(rec), json.dumps(rel, sort_keys=True), cd_exists, not ok)]
        if missing:
            outs.add(RECORD_MISSING)
        elif not ok and release_step_hands_over(f):
            outs.add(HANDOVER)
        elif young:
            outs.add(STARTING)
        elif changed and cdx is None:
            outs.add(RELEASE)
        else:
            outs.add(watch(obj, cdx, ok)[0])
    assert len(outs) == 1, (rec, hub, outs)   # the two providers' release steps decide the same
    return outs.pop()


def release_step_hands_over(f):
    """The release step hands over when the hub cannot be read (read from the task file)."""
    t = next((t for t in _yaml(f) if t.get("name") == "Hand over when the hub cannot be read"), None)
    return t is not None and "po_hub_unreadable" in json.dumps(t.get("when"))


def main_installed_guard():
    """main.yml stops a run on an installed cluster whose build is missing (read from its tasks)."""
    block = _yaml("main.yml")[0]["block"]
    t = next((t for t in block if t.get("name") == "An installed cluster whose build the hub does not hold"), None)
    return t is not None and "record_missing" in json.dumps(t.get("when")) and any(
        x.get("ansible.builtin.meta") == "end_play" for x in t["block"])


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
        # recorded installed and no ClusterDeployment: real cells (the hub may lose a build's record)
        for rec in ("INSTALLED", "FINISHING", "LEGACY_INSTALLED"):
            for hub in NO_CD:
                self.assertEqual(TABLE[rec][hub], RECORD_MISSING, (rec, hub))
        # clusters from before installations were recorded: every hub state the running path meets
        self.assertEqual(TABLE["LEGACY_STARTED"], RUNNING)

    def test_an_installed_cluster_never_gets_a_release_or_a_removal_without_its_build(self):
        """A cluster recorded as installed (clusterInstalled, Finishing, Installed) whose ClusterDeployment the hub
        does not hold: whoever asked, nothing is released and nothing removed; main.yml's guard ends the run, and
        the release step of both deploy files refuses on its own as well."""
        for rec in ("INSTALLED", "FINISHING", "LEGACY_INSTALLED"):
            for hub in NO_CD:
                for asked in ASKED:
                    self.assertEqual(actual(rec, hub, asked), RECORD_MISSING, (rec, hub, asked))
                cdx, rel, ok = hub_for(hub)
                for f in ("openstack_clusterbuild.yml", "aws_clusterbuild_deploy.yml"):
                    changed, young, missing = Release.load()[(f, _status_key(rec), json.dumps(rel, sort_keys=True),
                                                              False, False)]
                    self.assertEqual((changed, young, missing), (False, False, True), (rec, hub, f))

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
            if rec not in ("STOPPED", "CLEANINGUP", "CLEANINGUP_LONG"):
                self.assertNotEqual(got, REMOVE, (rec, hub))
            if got == RELEASE:
                self.assertIn(rec, ("NONE", "PREPARING", "INSTALLING", "OVERDUE", "LEGACY_STARTED"),
                              (rec, hub, asked))

    def test_no_dead_end(self):
        """In every cell where the cluster is not installed the run moves the state forward, a Retry in that cell
        does, or the cell waits on something that ends by itself (named in WAITS_ON)."""
        forward = {RELEASE, REMOVE, RESUME, STOP, READY}
        for rec, hub, asked in cells():
            want = expected(rec, hub, asked)
            if want is X or hub == "INSTALLED" or rec in ("INSTALLED", "FINISHING", "LEGACY_INSTALLED"):
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
        for rec in ("INSTALLING", "OVERDUE", "FINISHING", "LEGACY_STARTED"):
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
