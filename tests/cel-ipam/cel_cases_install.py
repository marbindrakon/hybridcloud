#!/usr/bin/env python3
"""CEL envtest for the PlatformOpenshift install-time rule: a change of the provider block (sizes, flavors, release,
image and the rest the installer reads once) or of spec.type is refused while the cluster is installing or installed,
and accepted before the installation starts or after it stopped. Runner of cel_cases.py (same arguments; group
"uxb2-install").

  python3 -I tests/cel-ipam/cel_cases_install.py --crds <crds.yaml or a file of the CRD bases> --expect candidate

Example names only.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cel_cases as K  # noqa: E402

G = "uxb2-install"
C, upd, setk, delk = K.C, K.upd, K.setk, K.delk
MSG = "deny:The cluster is being installed or is installed"


def po(type_="openstack", **blocks):
    spec = {"type": type_}
    if not blocks:
        blocks = {"openstack": {"environment": "env", "workerCount": 3}} if type_ == "openstack" else \
            {type_: {"environment": "env", "workerCount": 2}}
    spec.update(blocks)
    return {"apiVersion": K.API, "kind": "PlatformOpenshift", "metadata": {"name": "c"}, "spec": spec}


def status(st):
    return ("status", ("PlatformOpenshift", "c", st), "ok")


def change(path, value, expect):
    return (*upd("PlatformOpenshift", "c", setk(path, value))[:2], expect)


INSTALLING = {"installStarted": True, "install": {"stage": "Installing", "run": 1, "attempt": 1}}
INSTALLED = {"installStarted": True, "clusterInstalled": True, "install": {"stage": "Installed", "run": 1}}
STOPPED = {"installStarted": True, "install": {"stage": "Stopped", "run": 1, "stoppedGeneration": 1}}

K.CASES[:] = [
    C("IT-01", G, "no status: workers can change", [("create", po(), "ok"), change(["openstack", "workerCount"], 0, "ok")]),
    C("IT-02", G, "Preparing: a refused cluster can be corrected",
      [("create", po(), "ok"), status({"install": {"stage": "Preparing", "run": 1}}),
       change(["openstack", "workerFlavor"], "example.large", "ok")]),
    C("IT-03", G, "Installing: workers refused", [("create", po(), "ok"), status(INSTALLING),
                                                  change(["openstack", "workerCount"], 0, MSG)]),
    C("IT-04", G, "Installing: flavor refused", [("create", po(), "ok"), status(INSTALLING),
                                                 change(["openstack", "controlPlaneFlavor"], "example.xlarge", MSG)]),
    C("IT-05", G, "Installing: release refused", [("create", po(), "ok"), status(INSTALLING),
                                                  change(["openstack", "ocpImageSet"], "example-4.99", MSG)]),
    C("IT-06", G, "Installing: image refused", [("create", po(), "ok"), status(INSTALLING),
                                                change(["openstack", "glanceImageOverride"], "rhcos-example", MSG)]),
    C("IT-07", G, "Finishing: refused", [("create", po(), "ok"),
                                         status({"installStarted": True, "install": {"stage": "Finishing"}}),
                                         change(["openstack", "workerCount"], 5, MSG)]),
    C("IT-08", G, "CleaningUp: refused", [("create", po(), "ok"),
                                          status({"installStarted": True, "install": {"stage": "CleaningUp"}}),
                                          change(["openstack", "workerCount"], 5, MSG)]),
    C("IT-09", G, "Installed: refused", [("create", po(), "ok"), status(INSTALLED),
                                         change(["openstack", "workerCount"], 5, MSG)]),
    C("IT-10", G, "Stopped: accepted (starts a new installation)",
      [("create", po(), "ok"), status(STOPPED), change(["openstack", "workerCount"], 0, "ok")]),
    C("IT-11", G, "an older cluster without status.install, installed: refused",
      [("create", po(), "ok"), status({"clusterInstalled": True, "installStarted": True}),
       change(["openstack", "workerCount"], 5, MSG)]),
    C("IT-12", G, "an older cluster without status.install, started: refused",
      [("create", po(), "ok"), status({"installStarted": True}), change(["openstack", "workerCount"], 5, MSG)]),
    C("IT-13", G, "Stopped but recorded installed: refused",
      [("create", po(), "ok"), status(dict(STOPPED, clusterInstalled=True)), change(["openstack", "workerCount"], 0, MSG)]),
    C("IT-14", G, "Installed: access groups still change",
      [("create", po(), "ok"), status(INSTALLED), change(["toolRbac", "clusterAdminRbac"], ["admins"], "ok"),
       change(["clusterViewerRbac"], ["readers"], "ok"), change(["deletionProtection"], True, "ok")]),
    C("IT-15", G, "Installed: spec.type refused",
      [("create", po(), "ok"), status(INSTALLED), change(["type"], "aws", MSG)]),
    C("IT-16", G, "AWS installing: workers and instance type refused",
      [("create", po("aws"), "ok"), status(INSTALLING), change(["aws", "workerCount"], 0, MSG),
       change(["aws", "workerFlavor"], "m5.example", MSG)]),
    C("IT-17", G, "AWS installed: region refused", [("create", po("aws"), "ok"), status(INSTALLED),
                                                     change(["aws", "region"], "example-region-1", MSG)]),
    C("IT-18", G, "GCP installing: machine type refused",
      [("create", po("gcp"), "ok"), status(INSTALLING), change(["gcp", "controllerFlavor"], "n2-example", MSG)]),
    C("IT-19", G, "GCP stopped: accepted", [("create", po("gcp"), "ok"), status(STOPPED),
                                            change(["gcp", "workerCount"], 0, "ok")]),
    C("IT-20", G, "the platform's own status writes are never refused",
      [("create", po(), "ok"), status(INSTALLED), status(dict(INSTALLED, install={"stage": "Installed", "run": 2}))]),
    C("IT-21", G, "status.install keys survive (no pruning)",
      [("create", po(), "ok"),
       status({"installStarted": True, "install": {
           "stage": "Stopped", "attempt": 3, "attemptsLimit": 3, "run": 1, "startedAt": "2026-11-18T10:00:00Z",
           "endedAt": "2026-11-18T10:09:00Z", "observedAt": "2026-11-18T10:09:00Z", "stoppedGeneration": 1,
           "stoppedRequest": "", "lastFailure": {"attempt": 3, "reason": "QuotaExceeded", "message": "m",
                                                 "time": "2026-11-18T10:09:00Z"}}}),
       ("check", ("PlatformOpenshift", "c",
                  lambda o: None if (o.get("status", {}).get("install", {}).get("lastFailure", {}).get("reason")
                                     == "QuotaExceeded" and o["status"]["install"].get("stoppedGeneration") == 1)
                  else "status.install came back as %r" % o.get("status", {}).get("install"), ""), "ok")]),
    C("IT-22", G, "an unknown stage is refused", [("create", po(), "ok"),
                                                  ("status", ("PlatformOpenshift", "c", {"install": {"stage": "Odd"}}),
                                                   "deny")]),
]

if __name__ == "__main__":
    sys.exit(K.main())
