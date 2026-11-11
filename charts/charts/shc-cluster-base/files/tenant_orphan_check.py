#!/usr/bin/env python3
"""Tenant cloud orphan check (report-only).

Compares the tenant Keystone domain and the tenant DNS zone with the CloudOSO and PlatformOpenshift CRs on
this cluster and exits non-zero when something has no owner, so the ops-job alert (ShcBackupJobFailed)
fires. Scope is the tenant domain only: the account is that domain's manager, not a cloud admin, so
resources of already-deleted projects (e.g. their default security groups) are out of sight by design.

Checks:
  1. projects in the domain without a CloudOSO (config ignoreProjects excepted);
  2. CloudOSOs whose recorded project no longer exists;
  3. unattached floating IPs in a CloudOSO's project other than its recorded API/ingress pair and the floating IPs
     the PlatformOpenshifts record (status.floatingIPs: a cluster's own pair, unattached during its install);
  4. records in the tenant zone that no PlatformOpenshift recorded, that are not the NS delegation
     a CloudOSO recorded (status.dns.zone/nameservers) and that are not a name the platform published for
     its own addresses (zone apex SOA/NS excepted). The platform's names are the AAAA records a CloudGateway
     or NetworkPlacement records in status.realization.reverseDns.names ({name, address}): a record counts
     only with the recorded name and only the recorded addresses, so the name of a gateway that no longer
     exists, or an address its gateway no longer has, is still a finding.

Checks 1-3 look at one cloud: the account's. With config cloudInfrastructure set, only CloudOSOs on
that CloudInfrastructure (status.cloudInfrastructure, else spec.cloudRef.name) are compared with its
projects; CloudOSOs on other clouds are counted as "elsewhere" and not reported. The tenant DNS zone
is shared by every cloud, so check 4 uses the CloudOSOs of all clouds.

Exit codes: 0 nothing found; 1 findings (something has no owner); 2 the check is incomplete (the account
was refused, or a project or the zone could not be read). Every refusal names the cloud, the account
reference (config accountRef, where the account comes from) and the Keystone that answered. The summary
is also written to the container's termination message, so the failed pod shows it without its log.
"""
import argparse
import ipaddress
import os
import sys
from urllib.parse import urlsplit

import openstack
import yaml

try:
    from keystoneauth1 import exceptions as ksa_exceptions
    ACCESS_ERRORS = (openstack.exceptions.SDKException, ksa_exceptions.ClientException)
except ImportError:  # keystoneauth1 comes with openstacksdk; tests may run without either
    ACCESS_ERRORS = (openstack.exceptions.SDKException,)

GROUP, VERSION = "hybridsovereign.redhat", "v1alpha1"
SCOPE_KEYS = ("project_name", "project_id", "project_domain_name", "project_domain_id",
              "domain_name", "domain_id", "system_scope")
TERMINATION_LOG = "/dev/termination-log"


def crs(api, plural):
    return api.list_cluster_custom_object(GROUP, VERSION, plural).get("items", [])


def http_status(e):
    """The HTTP status of an SDK or keystoneauth error, or None (connection errors, timeouts)."""
    for attr in ("http_status", "status_code"):
        v = getattr(e, attr, None)
        if isinstance(v, int):
            return v
    return None


def refusal(e):
    """One line for an access error: what Keystone or the service answered."""
    st = http_status(e)
    first = (str(e).strip().splitlines() or [type(e).__name__])[0]
    return f"HTTP {st}: {first}" if st else f"{type(e).__name__}: {first}"


class Account:
    """The account as the report names it: the cloud, where the account comes from, and its Keystone."""

    def __init__(self, cfg, cloud):
        self.cloud = cfg.get("cloudInfrastructure") or "(any cloud)"
        self.ref = cfg.get("accountRef") or "(account reference not configured)"
        self.keystone = urlsplit((cloud.get("auth") or {}).get("auth_url", "")).netloc or "(no auth_url)"

    def __str__(self):
        return f"account {self.ref} on cloud {self.cloud} (Keystone {self.keystone})"

    @staticmethod
    def hint(e, scope):
        st = http_status(e)
        if st == 401 and scope == "project":
            return "the account has no role on the project"
        if st == 401:
            return "the credential is wrong, expired or rotated, or the account has no role on the domain"
        if st == 403:
            return "the account's role does not allow this read"
        if st == 404:
            return "the cloud does not have it (deleted, or a wrong id in the configuration)"
        if st is None:
            return "no answer from the cloud (connection, TLS or endpoint error, or a missing resource), not a refusal"
        return "the cloud did not answer as expected"


def oso_cloud(o):
    st = o.get("status") or {}
    return st.get("cloudInfrastructure") or ((o.get("spec") or {}).get("cloudRef") or {}).get("name") or ""


def canon_ip(text):
    """An address in one textual form (so 2001:DB8:0::1 and 2001:db8::1 compare equal); other text lower-cased."""
    t = (text or "").strip()
    try:
        return str(ipaddress.ip_address(t))
    except ValueError:
        return t.lower()


def published_names(objs):
    """{name with trailing dot: (set of addresses, owner refs)} from status.realization.reverseDns.names."""
    out = {}
    for kind, items in objs:
        for o in items:
            rd = (((o.get("status") or {}).get("realization") or {}).get("reverseDns") or {})
            ref = f"{kind} {'/'.join(x for x in (o['metadata'].get('namespace'), o['metadata']['name']) if x)}"
            for n in rd.get("names") or []:
                name, addr = (n.get("name") or "").lower().rstrip("."), canon_ip(n.get("address"))
                if name and addr:
                    addrs, owners = out.setdefault(name + ".", (set(), set()))
                    addrs.add(addr)
                    owners.add(ref)
    return out


def check(cfg, acct, connect, all_osos, pos, platform_objs=()):
    """Run the checks. Returns (summary line, findings, problems, exit code).
    platform_objs: [(kind, objects)] of the platform's own objects that record published names
    (CloudGateway, NetworkPlacement)."""
    cloud_name = cfg.get("cloudInfrastructure") or ""
    osos = [o for o in all_osos if not cloud_name or oso_cloud(o) == cloud_name]
    findings, problems = [], []

    try:
        conn_d = connect(domain_name=cfg["domain"])
        domain_id = conn_d.session.auth.get_access(conn_d.session).domain_id
        projects = {p.id: p.name for p in conn_d.identity.projects(domain_id=domain_id)}
    except ACCESS_ERRORS as e:
        what = "refused a token for" if http_status(e) in (401, 403) else "could not give a token for"
        problems.append(f"{acct}: Keystone {what} domain {cfg['domain']} ({refusal(e)}): "
                        f"{acct.hint(e, 'domain')}; nothing was checked")
        return (f"tenant-orphan-check: domain {cfg['domain']} on {acct.cloud}: not checked, the account was refused",
                findings, problems, 2)

    ignore = set(cfg.get("ignoreProjects", []))
    # the floating IPs the clusters record (their ids are unique, so the cluster's environment need not be matched)
    cluster_fips = {((((p.get("status") or {}).get("floatingIPs") or {}).get(k) or {}).get("id"))
                    for p in pos for k in ("api", "ingress")} - {None, ""}
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
        recorded = {st.get("apiFloatingIPId"), st.get("ingressFloatingIPId")} | cluster_fips
        try:
            conn_p = connect(project_id=pid)
            for ip in conn_p.network.ips(project_id=pid):
                if ip.id not in recorded and not ip.port_id:
                    findings.append(f"CloudOSO {ref}: unattached floating IP {ip.floating_ip_address} ({ip.id})")
        except ACCESS_ERRORS as e:
            problems.append(f"CloudOSO {ref}: project {name} ({pid}) not readable with {acct} ({refusal(e)}): "
                            f"{acct.hint(e, 'project')}; its floating IPs were not checked")

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
        published = published_names(platform_objs)
        try:
            conn_z = connect(project_id=cfg["dnsProjectId"])
            zone = conn_z.dns.get_zone(cfg["dnsZoneId"])
            for rs in conn_z.dns.recordsets(zone):
                if rs.name == zone.name and rs.type in ("SOA", "NS"):
                    continue
                if (rs.name, rs.type) in recorded_names:
                    continue
                own = published.get((rs.name or "").lower()) if rs.type == "AAAA" else None
                if own and rs.records and {canon_ip(r) for r in rs.records} <= own[0]:
                    continue
                if own:
                    findings.append(f"DNS {rs.name} {rs.type} {','.join(rs.records or [])}: {', '.join(sorted(own[1]))} "
                                    f"recorded this name with {','.join(sorted(own[0]))} only")
                else:
                    findings.append(f"DNS {rs.name} {rs.type} {','.join(rs.records or [])}: no PlatformOpenshift, CloudOSO, "
                                    f"CloudGateway or NetworkPlacement recorded it")
        except ACCESS_ERRORS as e:
            problems.append(f"DNS zone {cfg['dnsZoneId']} (project {cfg['dnsProjectId']}) not readable with {acct} "
                            f"({refusal(e)}): {acct.hint(e, 'project')}; its records were not checked")

    elsewhere = len(all_osos) - len(osos)
    summary = (f"tenant-orphan-check: domain {cfg['domain']}"
               f"{(' on ' + cloud_name) if cloud_name else ''}: {len(projects)} projects, {len(osos)} CloudOSO"
               f"{f' ({elsewhere} on other clouds)' if elsewhere else ''}, "
               f"{len(pos)} PlatformOpenshift; {len(findings)} finding(s)"
               f"{f', {len(problems)} not checked' if problems else ''}")
    return summary, findings, problems, (2 if problems else (1 if findings else 0))


def report(summary, findings, problems, termination_log=TERMINATION_LOG):
    """Print the report; on a failed run also write it (problems first) as the termination message."""
    lines = [summary] + ["  not checked: " + p for p in problems] + ["  " + f for f in findings]
    for line in lines:
        print(line)
    if problems or findings:
        try:  # the kubelet keeps up to 4 KiB of it in the pod status
            with open(termination_log, "w") as f:
                f.write("\n".join(lines)[:4000])
        except OSError:
            pass
    return lines


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

    from kubernetes import client, config
    try:
        config.load_incluster_config()
    except config.ConfigException:  # outside a pod (manual run): KUBECONFIG
        config.load_kube_config()
    api = client.CustomObjectsApi()
    platform_objs = [("CloudGateway", crs(api, "cloudgateways")), ("NetworkPlacement", crs(api, "networkplacements"))]
    summary, findings, problems, rc = check(cfg, Account(cfg, cloud), connect,
                                            crs(api, "cloudosos"), crs(api, "platformopenshifts"), platform_objs)
    report(summary, findings, problems, os.environ.get("TERMINATION_LOG", TERMINATION_LOG))
    return rc


if __name__ == "__main__":
    sys.exit(main())
