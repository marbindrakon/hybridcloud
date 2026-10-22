#!/usr/bin/env python3
"""Behaviour of shc-access-field-ladder (Team/Project access lists) in a throwaway API server.

Run by tests/access/test-access-fields.sh through `tests/schema-replay/replay.py --exec`: KUBECONFIG points at an
envtest API server (RBAC authorization) with the candidate CRDs and the rendered policy; REPLAY_KUBECTL is the
kubectl to use. Users are impersonated (the replay admin is system:masters); each holds a resource-scoped Role like
the ones eda/common/tasks/scope_role.yml renders. Every change is a server-side dry run.

Rows (plan be-tenantprofile-catalogue section 4): R10b teamMember-like editor cannot add itself to a Project's
admins; R10c teamAdmin (edit on the Team) cannot change the Team's lists; R10d projectAdmin (holds delete) can change
the Project's lists; plus: other spec changes stay allowed for editors, a reordered list is no change, platform
automation is exempt, creation is not matched.

usage: access_field_checks.py [--mode Deny|Warn]
"""
import argparse
import json
import os
import subprocess
import sys

GROUP = "hybridsovereign.redhat"
NS = "entity-ladder"
AUTOMATION = "system:serviceaccount:open-cluster-management-agent-addon:shc-automation"
RESULTS = []


def kubectl(args, as_user=None, body=None, as_groups=()):
    cmd = ([os.environ.get("REPLAY_KUBECTL", "kubectl")] + args + ([f"--as={as_user}"] if as_user else [])
           + [f"--as-group={g}" for g in as_groups])
    r = subprocess.run(cmd, input=json.dumps(body) if body is not None else None, capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def check(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'} {name}{(': ' + detail.strip()) if detail and not ok else ''}")


def role(name, resource, obj, verbs, user):
    return [
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": {"name": name, "namespace": NS},
         "rules": [{"apiGroups": [GROUP], "resources": [resource], "resourceNames": [obj], "verbs": verbs}]},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": {"name": name, "namespace": NS},
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": name},
         "subjects": [{"apiGroup": "rbac.authorization.k8s.io", "kind": "User", "name": user}]},
    ]


def apply(docs):
    rc, _, err = kubectl(["apply", "-f", "-"], body={"apiVersion": "v1", "kind": "List", "items": docs})
    if rc:
        sys.exit(f"access_field_checks: setup: {err}")


def patch(kind, name, spec, user, groups=()):
    # groups: the requester's sign-in groups (<entity>.<access group>); the entity of NS is "ladder"
    return kubectl(["-n", NS, "patch", f"{kind}.{GROUP}", name, "--type=merge", "--dry-run=server",
                    "-p", json.dumps({"spec": spec})], as_user=user, as_groups=groups)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("Deny", "Warn"), default="Deny")
    a = ap.parse_args()
    edit = ["get", "list", "watch", "update", "patch"]
    apply([{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NS}}])
    apply([
        {"apiVersion": f"{GROUP}/v1alpha1", "kind": "Team", "metadata": {"name": "platform", "namespace": NS},
         "spec": {"features": {"argo": False}, "teamAdmin": ["admins", "leads"], "teamMemberRbac": ["devs"]}},
        {"apiVersion": f"{GROUP}/v1alpha1", "kind": "Project", "metadata": {"name": "webshop", "namespace": NS},
         "spec": {"description": "shop", "projectAdmin": ["admins"], "projectDeveloperRbac": ["devs"]}},
    ])
    apply(role("t-edit", "teams", "platform", edit, "team-admin-user")
          + role("p-edit", "projects", "webshop", edit, "team-member-user")
          + role("p-admin", "projects", "webshop", edit + ["delete"], "project-admin-user")
          + role("t-automation", "teams", "platform", edit, AUTOMATION))

    MSG = "can change who has access to it"
    denied = (lambda rc, err: rc != 0 and MSG in err) if a.mode == "Deny" else (lambda rc, err: rc == 0 and MSG in err)
    mode = "refused" if a.mode == "Deny" else "warned"
    # team-admin-user holds edit on the Team through the teamAdmin grant; which sign-in groups it carries decides
    # whether it is a current team admin (ladder.admins / ladder.leads) or only an editor (ladder.devs).
    rc, _, err = patch("teams", "platform", {"teamAdmin": ["admins", "leads", "devs"]}, "team-admin-user",
                       groups=["ladder.devs"])
    check(f"R10c an editor of the Team whose group is not in teamAdmin changing teamAdmin is {mode}",
          denied(rc, err), err)
    rc, _, err = patch("teams", "platform", {"teamViewerRbac": ["devs"]}, "team-admin-user", groups=["ladder.devs"])
    check(f"R10c the same editor adding teamViewerRbac is {mode}", denied(rc, err), err)
    rc, _, err = patch("teams", "platform", {"teamAdmin": ["admins", "leads", "devs"]}, "team-admin-user",
                       groups=["ladder.admins"])
    check("R10e a current team admin (group in teamAdmin) may change the Team's teamAdmin (OD28)",
          rc == 0 and MSG not in err, err)
    rc, _, err = patch("teams", "platform", {"teamViewerRbac": ["devs"]}, "team-admin-user", groups=["ladder.leads"])
    check("R10e a current team admin may change teamViewerRbac", rc == 0 and MSG not in err, err)
    rc, _, err = patch("teams", "platform", {"teamAdmin": ["leads"]}, "team-admin-user", groups=["other.admins"])
    check(f"R10e the same access group of another tenant is not a team admin here: {mode}", denied(rc, err), err)
    rc, _, err = patch("teams", "platform", {"features": {"argo": True}}, "team-admin-user")
    check("a team editor may change other Team fields", rc == 0 and MSG not in err, err)
    rc, _, err = patch("teams", "platform", {"teamAdmin": ["leads", "admins"]}, "team-admin-user")
    check("reordering a list is no change", rc == 0 and MSG not in err, err)
    rc, _, err = patch("projects", "webshop", {"projectAdmin": ["admins", "devs"]}, "team-member-user",
                       groups=["ladder.devs"])
    check(f"R10b a group with edit on the Project adding itself to projectAdmin is {mode}", denied(rc, err), err)
    rc, _, err = patch("projects", "webshop", {"description": "new"}, "team-member-user")
    check("a project editor may change the description", rc == 0 and MSG not in err, err)
    rc, _, err = patch("projects", "webshop", {"projectDeveloperRbac": ["devs", "ops"]}, "project-admin-user")
    check("R10d a project admin (holds delete) may change the Project's lists", rc == 0 and MSG not in err, err)
    rc, _, err = patch("teams", "platform", {"teamAdmin": ["admins"]}, AUTOMATION)
    check("platform automation is exempt", rc == 0 and MSG not in err, err)
    rc, _, err = kubectl(["-n", NS, "create", "--dry-run=server", "-f", "-"], body={
        "apiVersion": f"{GROUP}/v1alpha1", "kind": "Team", "metadata": {"name": "other", "namespace": NS},
        "spec": {"teamAdmin": ["x"]}})
    check("creating a Team with access lists is not matched", rc == 0, err)
    bad = [n for n, ok in RESULTS if not ok]
    print(f"access_field_checks ({a.mode}): {len(RESULTS) - len(bad)}/{len(RESULTS)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
