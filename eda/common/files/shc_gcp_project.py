#!/usr/bin/env python3
"""shc (design/P11-GCP-PLAN.md §3.1, P11.2): tenant projects of the Google Cloud realization. Prints one JSON line.

  shc_gcp_project.py check --project ID [--enable-apis] [--region R]
      From AAP (the shc-cloud container group): enter the project's member account through the platform identity
      and report it: labels (shc-managed must be true), the member account, the APIs the platform needs (enabled
      with --enable-apis), the region's instance quota. Changes nothing without --enable-apis.

  shc_gcp_project.py external --project ID --key-file F --hub-project HUB --hub-number N [--member shc-automation]
      From a workstation with the gcloud CLI, once per external project (e.g. an RHDP open environment) with the
      project owner's initial credentials: creates the member account with roles/owner, lets the hub's automation
      account mint its tokens (serviceAccountTokenCreator) and the hub's Hive pool impersonate it
      (workloadIdentityUser), labels the project. The initial credentials are activated in gcloud for the run and
      revoked at the end; they are never printed or stored by this script. Idempotent.

Both read the identity from the environment (shc_gcp_api.py).
"""
import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shc_gcp_api as g  # noqa: E402

NEEDED_APIS = ["compute.googleapis.com", "dns.googleapis.com", "iam.googleapis.com", "iamcredentials.googleapis.com",
               "secretmanager.googleapis.com", "cloudresourcemanager.googleapis.com", "serviceusage.googleapis.com"]
LABELS = {"shc-managed": "true", "shc-purpose": "tenant-environment"}


def check(a):
    ident = g.Identity()
    out = {"ok": True, "project": a.project, "changed": []}
    try:
        tok = ident.project(a.project)
        p = g.http("GET", "%s/projects/%s" % (g.CRM, a.project), token=tok)
        out["projectNumber"] = (p.get("name") or "").split("/")[-1]
        out["state"] = p.get("state")
        out["labels"] = p.get("labels", {})
        out["parent"] = p.get("parent")
        out["memberAccount"] = ident.member_email(a.project)
        out["principal"] = g.tokeninfo(tok).get("email", "?")
        if out["labels"].get("shc-managed") != "true":
            g.fail("project %s is not labelled shc-managed=true" % a.project, project=a.project, labels=out["labels"])
        enabled = {s["config"]["name"] for s in g.list_all("%s/projects/%s/services" % (g.SERVICEUSAGE, a.project),
                                                           "services", tok, {"filter": "state:ENABLED", "pageSize": 200})}
        out["apisMissing"] = [s for s in NEEDED_APIS if s not in enabled]
        if out["apisMissing"] and a.enable_apis:
            out["changed"] += ["api " + s for s in g.enable_services(a.project, NEEDED_APIS, tok)]
            out["apisMissing"] = []
        if a.region and "compute.googleapis.com" in enabled:
            r = g.http("GET", "%s/projects/%s/regions/%s" % (g.COMPUTE, a.project, a.region), token=tok)
            out["quotas"] = {q["metric"]: {"limit": q["limit"], "usage": q["usage"]} for q in r.get("quotas", [])
                             if q["metric"] in ("CPUS", "N2_CPUS", "E2_CPUS", "STATIC_ADDRESSES", "VPN_GATEWAYS")}
            out["networks"] = [n["name"] for n in g.list_all("%s/projects/%s/global/networks" % (g.COMPUTE, a.project),
                                                             "items", tok)]
    except g.GcpError as e:
        g.fail(str(e), code=e.code, project=a.project)
    print(json.dumps(out))


def gcloud(args, account, check=True):
    r = subprocess.run(["gcloud"] + args + ["--account", account, "--quiet", "--format=json"], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError("gcloud %s: %s" % (" ".join(args[:3]), r.stderr.strip()[-400:]))
    return json.loads(r.stdout) if r.stdout.strip() else None


def external(a):
    out = {"ok": True, "project": a.project, "changed": []}
    sa_email = "%s@%s.iam.gserviceaccount.com" % (a.member, a.project)
    hub_sa = "%s@%s.iam.gserviceaccount.com" % (a.hub_automation, a.hub_project)
    hive_set = "principalSet://iam.googleapis.com/projects/%s/locations/global/workloadIdentityPools/%s/*" % (a.hub_number, a.hive_pool)
    key = json.load(open(a.key_file))
    account = key["client_email"]
    r = subprocess.run(["gcloud", "auth", "activate-service-account", "--key-file", a.key_file, "--quiet"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        g.fail("could not activate the initial credentials: %s" % r.stderr.strip()[-300:])
    try:
        p = gcloud(["projects", "describe", a.project], account)
        out["projectNumber"] = p.get("projectNumber")
        labels = p.get("labels", {})
        if any(labels.get(k) != v for k, v in LABELS.items()):
            # gcloud projects update has no label flag in GA: Resource Manager v3 PATCH
            tok = subprocess.run(["gcloud", "auth", "print-access-token", "--account", account], capture_output=True,
                                 text=True, check=True).stdout.strip()
            g.http("PATCH", "%s/projects/%s?updateMask=labels" % (g.CRM, a.project), {"labels": dict(labels, **LABELS)}, token=tok)
            out["changed"].append("labels")
        sas = gcloud(["iam", "service-accounts", "list", "--project", a.project], account) or []
        if not any(s.get("email") == sa_email for s in sas):
            gcloud(["iam", "service-accounts", "create", a.member, "--project", a.project,
                    "--display-name", "SHC platform automation (tenant)",
                    "--description", "Entered by the SHC platform through impersonation; owns the environment's SHC resources"], account)
            out["changed"].append("service account " + sa_email)
        pol = gcloud(["projects", "get-iam-policy", a.project], account) or {}
        owners = [b for b in pol.get("bindings", []) if b["role"] == "roles/owner"]
        if not any("serviceAccount:" + sa_email in b.get("members", []) for b in owners):
            gcloud(["projects", "add-iam-policy-binding", a.project, "--member", "serviceAccount:" + sa_email,
                    "--role", "roles/owner", "--condition=None"], account)
            out["changed"].append("roles/owner")
        spol = gcloud(["iam", "service-accounts", "get-iam-policy", sa_email, "--project", a.project], account) or {}
        have = {(b["role"], m) for b in spol.get("bindings", []) for m in b.get("members", [])}
        # the self-binding: the OpenShift installer (running as the member account through the keyless chain)
        # signs the bootstrap ignition URL with iam.serviceAccounts.signBlob, which roles/owner does not carry
        for role, member in (("roles/iam.serviceAccountTokenCreator", "serviceAccount:" + hub_sa),
                             ("roles/iam.serviceAccountTokenCreator", "serviceAccount:" + sa_email),
                             ("roles/iam.workloadIdentityUser", hive_set)):
            if (role, member) not in have:
                gcloud(["iam", "service-accounts", "add-iam-policy-binding", sa_email, "--project", a.project,
                        "--member", member, "--role", role], account)
                out["changed"].append("%s -> %s" % (role, member.split("/")[-1] if member.startswith("principalSet") else member))
        enabled = {s["config"]["name"] for s in gcloud(["services", "list", "--enabled", "--project", a.project], account) or []}
        missing = [s for s in NEEDED_APIS if s not in enabled]
        if missing:
            gcloud(["services", "enable"] + missing + ["--project", a.project], account)
            out["changed"] += ["api " + s for s in missing]
        out["memberAccount"], out["hubAutomation"], out["hivePrincipals"] = sa_email, hub_sa, hive_set
    except (RuntimeError, g.GcpError, subprocess.CalledProcessError) as e:
        out = {"ok": False, "error": str(e)[-600:], "project": a.project}
    finally:
        subprocess.run(["gcloud", "auth", "revoke", account, "--quiet"], capture_output=True)
    print(json.dumps(out))
    sys.exit(0 if out["ok"] else 1)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--project", required=True)
    c.add_argument("--enable-apis", action="store_true")
    c.add_argument("--region")
    e = sub.add_parser("external")
    e.add_argument("--project", required=True)
    e.add_argument("--key-file", required=True)
    e.add_argument("--hub-project", required=True)
    e.add_argument("--hub-number", required=True)
    e.add_argument("--member", default="shc-automation")
    e.add_argument("--hub-automation", default="shc-platform-automation")
    e.add_argument("--hive-pool", default="shc-central")
    a = ap.parse_args()
    (check if a.cmd == "check" else external)(a)


if __name__ == "__main__":
    main()
