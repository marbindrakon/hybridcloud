#!/usr/bin/env python3
"""Tenant cloud orphan check (shc LAB-PLAN §11.6 phase D, design/TENANT-TEARDOWN.md §3.5).

Report-only: compares the tenant Keystone domain and the tenant DNS zone with the CloudOSO and
PlatformOpenshift CRs on this cluster and exits 1 when something has no owner, so the ops-job
alert (ShcBackupJobFailed) fires. Scope is the tenant domain only (owner, 10-11): the platform's
account is that domain's manager, not a cloud admin, so resources of already-deleted projects
(e.g. their default security groups) are out of sight by design.

Checks:
  1. projects in the domain without a CloudOSO (config ignoreProjects excepted);
  2. CloudOSOs whose recorded project no longer exists;
  3. unattached floating IPs in a CloudOSO's project other than its recorded API/ingress pair;
  4. records in the tenant zone that no PlatformOpenshift recorded and that are not the NS delegation
     a CloudOSO recorded (status.dns.zone/nameservers) (zone apex SOA/NS excepted).

Checks 1-3 look at one cloud: the account's. With config cloudInfrastructure set, only CloudOSOs on
that CloudInfrastructure (status.cloudInfrastructure, else spec.cloudRef.name) are compared with its
projects; CloudOSOs on other clouds are counted as "elsewhere" and not reported. The tenant DNS zone
is shared by every cloud, so check 4 uses the CloudOSOs of all clouds.
"""
import argparse
import sys

import openstack
import yaml
from kubernetes import client, config

GROUP, VERSION = "hybridsovereign.redhat", "v1alpha1"
SCOPE_KEYS = ("project_name", "project_id", "project_domain_name", "project_domain_id",
              "domain_name", "domain_id", "system_scope")


def crs(api, plural):
    return api.list_cluster_custom_object(GROUP, VERSION, plural).get("items", [])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--clouds", required=True)
    args = ap.parse_args()
    with open(args.config) as f:
        cfg = yaml.safe_load(f)
    with open(args.clouds) as f:
        cloud = yaml.safe_load(f)["clouds"]["openstack"]
    auth = {k: v for k, v in cloud["auth"].items() if k not in SCOPE_KEYS}

    def connect(**scope):
        return openstack.connect(auth={**auth, **scope}, region_name=cloud.get("region_name"),
                                 interface=cloud.get("interface", "public"), identity_api_version="3")

    try:
        config.load_incluster_config()
    except config.ConfigException:  # outside a pod (manual run): KUBECONFIG
        config.load_kube_config()
    api = client.CustomObjectsApi()
    all_osos = crs(api, "cloudosos")
    pos = crs(api, "platformopenshifts")
    cloud_name = cfg.get("cloudInfrastructure") or ""

    def oso_cloud(o):
        st = o.get("status") or {}
        return st.get("cloudInfrastructure") or ((o.get("spec") or {}).get("cloudRef") or {}).get("name") or ""

    osos = [o for o in all_osos if not cloud_name or oso_cloud(o) == cloud_name]

    conn_d = connect(domain_name=cfg["domain"])
    domain_id = conn_d.session.auth.get_access(conn_d.session).domain_id
    projects = {p.id: p.name for p in conn_d.identity.projects(domain_id=domain_id)}
    ignore = set(cfg.get("ignoreProjects", []))
    findings = []

    oso_by_project = {}
    for o in osos:
        st = o.get("status") or {}
        ref = f"{o['metadata']['namespace']}/{o['metadata']['name']}"
        pid = st.get("projectId")
        if not pid:
            continue
        oso_by_project[pid] = (ref, st)
        if pid not in projects:
            findings.append(f"CloudOSO {ref}: recorded project {pid} no longer exists")

    for pid, name in sorted(projects.items(), key=lambda x: x[1]):
        if name in ignore or pid in ignore:
            continue
        if pid not in oso_by_project:
            findings.append(f"project {name} ({pid}) in domain {cfg['domain']} has no CloudOSO")
            continue
        ref, st = oso_by_project[pid]
        recorded = {st.get("apiFloatingIPId"), st.get("ingressFloatingIPId")}
        try:
            conn_p = connect(project_id=pid)
            for ip in conn_p.network.ips(project_id=pid):
                if ip.id not in recorded and not ip.port_id:
                    findings.append(f"CloudOSO {ref}: unattached floating IP {ip.floating_ip_address} ({ip.id})")
        except openstack.exceptions.SDKException as e:
            findings.append(f"CloudOSO {ref}: project {name} not readable by the account: {e}")

    if cfg.get("dnsZoneId"):
        recorded_names = set()
        for p in pos:
            for r in ((p.get("status") or {}).get("dns") or {}).get("records") or []:
                recorded_names.add((r.get("name", "").rstrip(".") + ".", r.get("type", "A")))
        # NS delegations of CloudOSO sub-zones (any cloud) into the tenant zone
        for o in all_osos:
            dns = (o.get("status") or {}).get("dns") or {}
            if dns.get("zone") and dns.get("delegated") and dns.get("globalZoneId", cfg["dnsZoneId"]) == cfg["dnsZoneId"]:
                recorded_names.add((dns["zone"].rstrip(".") + ".", "NS"))
        conn_z = connect(project_id=cfg["dnsProjectId"])
        zone = conn_z.dns.get_zone(cfg["dnsZoneId"])
        for rs in conn_z.dns.recordsets(zone):
            if rs.name == zone.name and rs.type in ("SOA", "NS"):
                continue
            if (rs.name, rs.type) not in recorded_names:
                findings.append(f"DNS {rs.name} {rs.type} {','.join(rs.records or [])}: no PlatformOpenshift or CloudOSO recorded it")

    elsewhere = len(all_osos) - len(osos)
    print(f"tenant-orphan-check: domain {cfg['domain']}"
          f"{(' on ' + cloud_name) if cloud_name else ''}: {len(projects)} projects, {len(osos)} CloudOSO"
          f"{f' ({elsewhere} on other clouds)' if elsewhere else ''}, "
          f"{len(pos)} PlatformOpenshift; {len(findings)} finding(s)")
    for line in findings:
        print("  " + line)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
