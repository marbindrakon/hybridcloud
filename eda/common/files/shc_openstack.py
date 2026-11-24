#!/usr/bin/env python3
"""OpenStack steps of the CloudOSO / PlatformOpenshift jobs (shc LAB-PLAN §11.6 phase C).

Reads one JSON request on stdin and prints one JSON result on stdout; exits non-zero with
{"error": ...} on failure. The account is a clouds.yaml entry: the site's cloud-admin account from its
CloudInfrastructure (P7.1, design/P7-PLAN.md decision 14), or the legacy tenant admin (a password user
with `manager` on the tenant Keystone domain). This script picks the scope per call (domain for
Keystone, the project for its resources), so the account's own scope keys are dropped; site-probe uses
the account's own scope. A private CA comes as request["cacert"] (a file path) or clouds.yaml `cacert`.

Actions (request["action"]):
  provision     project in the domain, `member` for the account; returns every id it touched so teardown can work
                from the record. legacy_app_cred_ids / legacy_app_cred_name (optional): application credentials an
                earlier release made as the account for the environment are deleted (the account owns them).
                fip_descriptions (optional, a request of an earlier release): floating IPs found or created by
                description on external_network. Without it no external network is looked up.
  check-empty   lists what is left in a project besides the recorded floating IPs (and DNS
                records under a suffix); teardown refuses to delete a project that isn't empty.
  teardown      deletes the recorded floating IPs, legacy application credentials of the account and the project.
  access-groups the environment's access groups on the site (Keystone groups in the sign-in group domain, marked as
                the platform's, with roles on the project): state present ensures and grants the wanted ones and
                reports the platform's groups with a role on the project that are no longer wanted; state absent
                revokes the platform's roles of the named groups on the project and deletes a group that holds no
                role anywhere any more (when asked to).
  cluster-identity  an OpenShift cluster's own Keystone user in the environment's domain (marked as the platform's,
                no stored password, the user options below) with roles on the project, and its restricted
                application credential: kept while the one in Vault still signs in, else made again (password set,
                wait, sign-in as the user, the user's credentials deleted, one created, a second password set; the
                unit is retried on HTTP 401). The clouds.yaml goes to a private file (a Vault write body), never to
                stdout.
  cluster-identity-delete  deletes a cluster's user (its credentials go with it).
  leftover-users  deletes the platform's users of the domain that hold a role on the project (what a failed cluster
                teardown left), before the project goes.
  dns-upsert    creates or updates A records in a Designate zone (scoped to the zone's project), each marked with the
                description shc:<owner>; a recordset of the name without that mark is refused (NameTakenOnSite)
                before anything is written.
  dns-delete    deletes recordsets by name/type from a Designate zone, only those with the owner's mark; reports
                what was missing and what was kept (not the platform's).
  site-probe    CloudInfrastructure probe (cloud admin): authenticates, reports the catalog, Neutron's
                extensions (evpn), the named external network and the site's global external networks (those
                with an access_as_external RBAC policy for every project, target "*"; a network external for some
                projects only is never listed); ensures the tenant project domain (created
                only when asked; the account's admin role on it only when the account holds admin already, else
                its roles there are reported), and the site's DNS project (created only when asked) with the
                account's member role on it. sign_in (optional): the declared identity provider, its protocol and
                mapping are read where the account may (403 = not confirmed), the group domain is looked up and
                the account's own role assignments are reported. Nothing of the site's federation is changed.
  platform-project  a platform-owned project (CloudGateway, design/P7-PLAN.md §12.3.2) in a domain, with
                `member` for the account: state present ensures it; state absent deletes it only when it holds
                no networks, routers, ports or RBAC policies (refused otherwise).
  placement-network  a NetworkPlacement's objects in the platform project (design/P7-PLAN.md §12.3.2), cloud
                admin scope: network <name> (MTU, tag), a subnet per prefix of either family (IPv6 subnets with
                addressMode as RA and address mode, resolvers of the subnet's family as dns_nameservers), router
                <name> with evpn_vni (the site advertises the subnets as EVPN type-5 routes under that VNI), the
                subnets on the router (the first interface with advertise_host; the plugin takes one advertised
                interface per network), and the tenant project's uses of the network (use: virtualMachines =
                RBAC access_as_shared, default on; routerAttachments = RBAC access_as_external to that project
                only, default off; the network's own router:external flag is never set, and a placed network
                that is external for every project is refused). A use turned off while the project still uses
                it that way (its ports; its router gateways and floating IPs) is left in place and reported
                (state removing, inUse). Policies of either action for other projects are pruned.
                state absent removes them in order (RBAC policies, interfaces, router, subnets, network), refused
                while the network is used: ports of other projects, router gateways and floating IPs of any
                project (listed in blocked).
                recreateRouter: delete and recreate the router (the site fills its gateway chassis at creation).
  cluster-fips  a cluster's API and ingress floating IPs in the environment's project (site account scoped
                to project_id): state present finds each by description (descriptions {api, ingress}) on
                network_id, else creates it; state absent releases them by recorded ids and by description (a
                missing id counts as released).
  network-name-check  read only, with the environment's own credential (the one the installer runs with, scoped to
                the environment's project): the networks of a name as Neutron lists them to that project, each with
                router:external as the project sees it; unique when only the expected network id is among them (the
                installer takes the external network by name), external when the project may use it as one.
                With flavors (names): flavors_missing lists those the project does not see; with images (names):
                images_missing likewise.
  router-advertisement  the site's router advertisement service (an optional site component that switches the `nat`
                token of an EVPN router port, so the site advertises floating IPs on a placed network): state
                present asks it to hold `nat` on the router's IPv4 interface port on the network, absent to drop
                it; service_url empty = the site has no such service ({service: absent}, nothing else happens).
                The calls carry a Keystone token of the router's project (never logged or returned).
  platform-inventory  read only: the networks (with their RBAC shares), routers (with evpn_vni) and ports of other
                owners in the platform project, for the fabric orphan check.
  project-routers  read only, the site account scoped to the environment's project (project_id): the routers of
                that project carrying `tag` (the installer tags its resources openshiftClusterID=<infraID>), each with
                its tags and external gateway (network and fixed addresses), for the PlatformOpenshift role's router
                gateway step (files/po_preflight.py router-gateway judges them).
"""
import hashlib
import ipaddress
import json
import os
import random
import secrets
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

import openstack
import yaml

SCOPE_KEYS = ("project_name", "project_id", "project_domain_name", "project_domain_id",
              "domain_name", "domain_id", "system_scope")


class Account:
    def __init__(self, req):
        with open(req["clouds_yaml"]) as f:
            cfg = yaml.safe_load(f)["clouds"][req.get("cloud", "openstack")]
        self.own_auth = dict(cfg["auth"])
        self.auth = {k: v for k, v in cfg["auth"].items() if k not in SCOPE_KEYS}
        self.region = req.get("region") or cfg.get("region_name")
        self.interface = cfg.get("interface", "public")
        self.verify = req.get("cacert") or cfg.get("cacert") or cfg.get("verify", True)
        self.auth_type = cfg.get("auth_type") or ""

    def connect(self, **scope):
        return openstack.connect(auth={**self.auth, **scope}, region_name=self.region,
                                 interface=self.interface, verify=self.verify,
                                 identity_api_version="3")

    def own_scope(self):
        return openstack.connect(auth=self.own_auth, region_name=self.region, interface=self.interface,
                                 verify=self.verify, identity_api_version="3")

    def as_configured(self):
        """The clouds.yaml entry as written, its auth type included (an application credential: its scope is its
        own and cannot be changed)."""
        return openstack.connect(auth=self.own_auth, region_name=self.region, interface=self.interface,
                                 verify=self.verify, identity_api_version="3",
                                 **({"auth_type": self.auth_type} if self.auth_type else {}))

    def domain(self, name):
        return self.connect(domain_name=name)

    def project(self, project_id):
        return self.connect(project_id=project_id)


def token_info(conn):
    access = conn.session.auth.get_access(conn.session)
    return access.user_id, access.domain_id, access.project_id


def find_project(conn_d, domain_id, name=None, project_id=None):
    # A domain manager gets 403, not 404, for a project id that no longer exists (Keystone can't
    # match the target's domain), so look projects up by listing the domain.
    for p in conn_d.identity.projects(domain_id=domain_id, **({"name": name} if name else {})):
        if (project_id and p.id == project_id) or (not project_id and p.name == name):
            return p
    return None


# The platform's mark on a Keystone project it made: a project tag naming the owner. Keystone keeps tags on projects
# (keystone/resource/schema.py, _project_tag_name_properties: at most 255 characters, no "/" or ","; at most 80 tags
# per project; project_create and project_update both take "tags"). A project of the wanted name without this tag
# is someone else's: refused, never given a role, never changed, never deleted.
PROJECT_TAG_PREFIX = "hybridsovereign.redhat:"
PROJECT_TAG_MAX = 255
# the owners of the site-wide projects the platform makes (one each per site)
PLATFORM_PROJECT_OWNER = "platform-networks"
DNS_PROJECT_OWNER = "tenant-dns"


def owner_tag(owner):
    """The tag for an owner such as "CloudOSO/<namespace>/<name>" or "platform-networks" ("/" becomes ":")."""
    owner = str(owner or "").strip()
    if not owner or "," in owner:
        raise ValueError(f"refused: the project's owner {owner!r} cannot be a Keystone tag")
    tag = PROJECT_TAG_PREFIX + owner.replace("/", ":")
    if len(tag) > PROJECT_TAG_MAX:
        raise ValueError(f"refused: the project's owner tag has {len(tag)} characters (Keystone takes {PROJECT_TAG_MAX})")
    return tag


def _tags(p):
    return list(getattr(p, "tags", None) or [])


def name_taken(what, name):
    return Refusal("NameTakenOnSite",
                   f"The site already has an OpenStack project named {name} that the platform did not make for this "
                   f"{what}; the platform does not take it over. Ask the platform team to rename or remove that "
                   f"project, or use another name.")


def owned_project(conn_d, domain_id, name, tag, recorded_id="", what="environment"):
    """The project that belongs to this owner, or None when there is none yet (create it).

    Found by the recorded id first (the record is ours: a project made before the mark existed gets the mark), then by
    name, and then only with the owner's tag. A project of the name without the tag, or one recorded but marked for
    another owner, raises NameTakenOnSite. Returns (project or None, how): how is recorded | marked | absent."""
    if recorded_id:
        p = find_project(conn_d, domain_id, project_id=recorded_id)
        if p is not None:
            if any(t.startswith(PROJECT_TAG_PREFIX) and t != tag for t in _tags(p)):
                raise name_taken(what, p.name)
            return p, "recorded"
    p = find_project(conn_d, domain_id, name=name)
    if p is None:
        return None, "absent"
    if tag in _tags(p):
        return p, "marked"
    raise name_taken(what, name)


def ensure_owned_project(conn_d, domain_id, name, tag, description, recorded_id="", what="environment"):
    """Find (owned_project) or create the owner's project; its tag and description refreshed on each run.
    Returns (project, created, how)."""
    project, how = owned_project(conn_d, domain_id, name, tag, recorded_id, what)
    if project is None:
        project = conn_d.identity.create_project(name=name, domain_id=domain_id, description=description, tags=[tag])
        return project, True, "created"
    tags, have = _tags(project), getattr(project, "description", None) or ""
    if tag not in tags or (description and have != description):
        project = conn_d.identity.update_project(project, description=description or have,
                                                 tags=tags + ([tag] if tag not in tags else []))
    return project, False, how


def provision(req):
    acct = Account(req)
    conn_d = acct.domain(req["domain"])
    user_id, domain_id, _ = token_info(conn_d)
    project, created, how = ensure_owned_project(conn_d, domain_id, req["project"], owner_tag(req["owner"]),
                                                 req.get("description", ""), req.get("project_id") or "")
    role = conn_d.identity.find_role(req.get("role", "member"), ignore_missing=False)
    conn_d.identity.assign_project_role_to_user(project, user_id, role)

    conn_p = acct.project(project.id)
    fips = {}
    # floating IPs belong to the clusters (cluster-fips); a request without fip_descriptions needs no external network
    descs = req.get("fip_descriptions") or {}
    net = conn_p.network.find_network(req["external_network"], ignore_missing=False) if descs else None
    for key, desc in descs.items():
        existing = list(conn_p.network.ips(description=desc, project_id=project.id))
        if existing:
            fips[key] = {"id": existing[0].id, "ip": existing[0].floating_ip_address, "created": False}
        else:
            ip = conn_p.network.create_ip(floating_network_id=net.id, description=desc)
            fips[key] = {"id": ip.id, "ip": ip.floating_ip_address, "created": True}

    # an earlier release made the environment's application credential as the account (which owns it): it goes, by
    # the recorded id and by its name (a credential whose id never reached the record); Keystone refuses application
    # credential calls on a domain-scoped token, hence the project scope
    legacy = delete_legacy_app_creds(conn_p, user_id, req)

    zone = None
    if req.get("dns_zone_id"):
        conn_z = acct.project(req["dns_project_id"])
        z = conn_z.dns.get_zone(req["dns_zone_id"])
        zone = {"id": z.id, "name": z.name}

    return {"project": {"id": project.id, "name": project.name, "domain_id": domain_id,
                        "domain_name": req["domain"], "created": created, "found": how},
            "user_id": user_id, "role": role.name, "fips": fips, "legacy_app_creds_deleted": legacy,
            "auth_url": acct.auth.get("auth_url", ""), "region": acct.region, "dns_zone": zone}


def delete_legacy_app_creds(conn_p, user_id, req):
    """The account's own application credentials of an earlier release (recorded ids, and those of the legacy name);
    a missing one counts as deleted. Returns the ids deleted."""
    ids = {i for i in (req.get("legacy_app_cred_ids") or []) if i}
    if req.get("legacy_app_cred_name"):
        ids.update(c.id for c in conn_p.identity.application_credentials(user=user_id, name=req["legacy_app_cred_name"]))
    for cid in sorted(ids):
        conn_p.identity.delete_application_credential(user_id, cid, ignore_missing=True)
    return sorted(ids)


def describe(r, *fields):
    parts = [str(getattr(r, f, None)) for f in fields if getattr(r, f, None)]
    return " ".join(parts) or r.id


def resolve_project(conn_d, domain_id, req):
    """The recorded project id, or (provision failed before recording it) the project of that name, and then only
    when it carries the owner's tag: a project the platform did not make is never checked or deleted (None)."""
    if req.get("project_id"):
        return find_project(conn_d, domain_id, project_id=req["project_id"])
    if req.get("project"):
        p = find_project(conn_d, domain_id, name=req["project"])
        return p if p is not None and owner_tag(req.get("owner")) in _tags(p) else None
    raise ValueError("project_id or project is required")


def check_empty(req):
    acct = Account(req)
    conn_d = acct.domain(req["domain"])
    _, domain_id, _ = token_info(conn_d)
    project = resolve_project(conn_d, domain_id, req)
    if project is None:
        return {"project_exists": False, "empty": True, "found": {}}
    pid = project.id
    conn = acct.project(pid)
    allowed_fips = set(req.get("allowed_fip_ids", []))
    found = {}

    def add(kind, items):
        items = list(items)
        if items:
            found[kind] = items

    add("servers", (describe(s, "name", "id") for s in conn.compute.servers()))
    add("server_groups", (describe(g, "name", "id") for g in conn.compute.server_groups()))
    add("floating_ips", (describe(i, "floating_ip_address", "id")
                         for i in conn.network.ips(project_id=pid) if i.id not in allowed_fips))
    add("ports", (describe(p, "name", "device_owner", "id") for p in conn.network.ports(project_id=pid)
                  if not (p.device_owner or "").startswith("network:")))
    add("routers", (describe(r, "name", "id") for r in conn.network.routers(project_id=pid)))
    add("networks", (describe(n, "name", "id") for n in conn.network.networks(project_id=pid)))
    add("security_groups", (describe(g, "name", "id")
                            for g in conn.network.security_groups(project_id=pid) if g.name != "default"))
    add("volumes", (describe(v, "name", "id") for v in conn.block_storage.volumes()))
    add("volume_snapshots", (describe(s, "name", "id") for s in conn.block_storage.snapshots()))
    add("images", (describe(i, "name", "id") for i in conn.image.images(owner=pid)))
    # Octavia and Designate are optional services (rhoso-bm runs neither).
    if conn.has_service("load-balancer"):
        try:
            add("load_balancers", (describe(lb, "name", "id")
                                   for lb in conn.load_balancer.load_balancers(project_id=pid)))
        except openstack.exceptions.ForbiddenException:
            found["load_balancers"] = ["not checked: the account may not list Octavia load balancers"]
    if conn.has_service("dns"):
        # only zones the project owns: zones shared with it (its environment zone, design/TENANT-DNS.md) are
        # listed too, but they belong to the platform's DNS project and go with the environment's teardown
        add("dns_zones", (describe(z, "name", "id") for z in conn.dns.zones() if z.project_id == pid))
    if req.get("dns_zone_id") and req.get("dns_suffix"):
        conn_z = acct.project(req["dns_project_id"])
        suffix = req["dns_suffix"].rstrip(".") + "."
        add("dns_records", (f"{rs.name} {rs.type}" for rs in conn_z.dns.recordsets(req["dns_zone_id"])
                            if rs.name == suffix or rs.name.endswith("." + suffix)))
    return {"project_exists": True, "project_id": pid, "empty": not found, "found": found}


def teardown(req):
    acct = Account(req)
    conn_d = acct.domain(req["domain"])
    user_id, domain_id, _ = token_info(conn_d)
    project = resolve_project(conn_d, domain_id, req)
    result = {"floating_ips": [], "app_creds": [], "project": None, "default_security_groups": []}
    if project is not None:
        pid = project.id
        conn = acct.project(pid)
        fip_ids = set(req.get("fip_ids", []))
        for desc in req.get("fip_descriptions", []):
            fip_ids.update(i.id for i in conn.network.ips(description=desc, project_id=pid))
        for fid in sorted(fip_ids):
            conn.network.delete_ip(fid, ignore_missing=True)
            result["floating_ips"].append(fid)
        # Neutron only lets cloud admins delete a project's default security group; report it.
        result["default_security_groups"] = [g.id for g in conn.network.security_groups(project_id=pid)
                                             if g.name == "default"]
        # an environment an earlier release made and that was never reconciled since still has the account's own
        # application credential: it goes while the project exists (a domain-scoped token may not delete it)
        result["app_creds"] = delete_legacy_app_creds(conn, user_id, req)
        conn_d.identity.delete_project(project, ignore_missing=True)
        result["project"] = pid
        if find_project(conn_d, domain_id, project_id=pid) is not None:
            raise RuntimeError(f"project {pid} still exists after delete")
    return result


# A cluster's records in its environment's zone carry the description shc:<owner> (shc_dns.py owner_description: the
# platform's mark; Designate keeps no tags on recordsets, designate/objects/recordset.py, and the description takes
# 160 characters). The tenant manages records in that zone too: a recordset of the name without the owner's mark is
# never overwritten or deleted.
RECORD_DESCRIPTION_MAX = 160


def record_mark(owner):
    owner = str(owner or "").strip()
    if not owner:
        raise ValueError("refused: owner is empty; it marks the cluster's DNS records as the platform's")
    d = f"shc:{owner}"
    if len(d) > RECORD_DESCRIPTION_MAX:
        d = d[:RECORD_DESCRIPTION_MAX - 11] + "~" + hashlib.sha1(owner.encode()).hexdigest()[:10]
    return d


def dns_upsert(req):
    conn = Account(req).project(req["dns_project_id"])
    zone = conn.dns.get_zone(req["dns_zone_id"])
    mark = record_mark(req.get("owner"))
    out, wanted = [], []
    for rec in req["records"]:
        name = rec["name"].rstrip(".") + "."
        if not name.endswith(zone.name):
            raise RuntimeError(f"{name} is outside zone {zone.name}")
        existing = [rs for rs in conn.dns.recordsets(zone, name=name, type=rec.get("type", "A")) if rs.name == name]
        if existing and (getattr(existing[0], "description", None) or "") != mark:
            raise Refusal("NameTakenOnSite",
                          f"The DNS name {name} already has a {rec.get('type', 'A')} record that the platform did not "
                          f"make for this cluster; the platform does not change it. Remove that record from the zone, "
                          f"or use another cluster name.")
        wanted.append((rec, name, existing))
    # every record checked before the first write
    for rec, name, existing in wanted:
        if existing and sorted(existing[0].records) == sorted(rec["records"]):
            state, rs = "unchanged", existing[0]
        elif existing:
            state, rs = "updated", conn.dns.update_recordset(existing[0], records=rec["records"])
        else:
            state, rs = "created", conn.dns.create_recordset(zone, name=name, type=rec.get("type", "A"),
                                                             ttl=rec.get("ttl", 300), records=rec["records"],
                                                             description=mark)
        out.append({"zoneId": zone.id, "zone": zone.name, "name": name, "type": rs.type, "state": state})
    return {"records": out}


def dns_delete(req):
    """Deletes the cluster's recordsets (those with the owner's mark); one of the name without it is kept."""
    conn = Account(req).project(req["dns_project_id"])
    try:
        zone = conn.dns.get_zone(req["dns_zone_id"])
    except openstack.exceptions.NotFoundException:
        return {"deleted": [], "missing": [r["name"] for r in req["records"]], "zone_missing": True}
    mark = record_mark(req.get("owner"))
    deleted, missing, kept = [], [], []
    for rec in req["records"]:
        name = rec["name"].rstrip(".") + "."
        label = f"{name} {rec.get('type', 'A')}"
        found = [rs for rs in conn.dns.recordsets(zone, name=name, type=rec.get("type", "A")) if rs.name == name]
        mine = [rs for rs in found if (getattr(rs, "description", None) or "") == mark]
        for rs in mine:
            conn.dns.delete_recordset(rs, zone, ignore_missing=True)
        (deleted if mine else kept if found else missing).append(label)
    return {"deleted": deleted, "missing": missing, "kept": kept}


def public_volume_types(conn, services, notes):
    """The public Cinder volume types (what any project of the cloud may use), [{name, default}]; read only.
    Private types belong to other consumers of a shared cloud and are never listed. None when the list cannot be read
    (an account that may not read volume types, e.g. a domain manager): a note says so and the site keeps what it
    had."""
    if not ({"volumev3", "block-storage", "volume"} & set(services)):
        return []
    try:
        default = None
        try:
            default = conn.block_storage.get("/types/default").json().get("volume_type", {}).get("name")
        except Exception:  # noqa: BLE001 - the default type is optional information
            default = None
        out = []
        for t in conn.block_storage.types():
            if getattr(t, "is_public", True) is False:
                continue
            out.append({"name": t.name, "default": bool(default) and t.name == default})
        return sorted(out, key=lambda x: x["name"])
    except openstack.exceptions.ForbiddenException:
        notes.append("volume types not listed: the account may not read them (an administrator of the cloud may); "
                     "the last list stands")
        return None
    except Exception as e:  # noqa: BLE001 - a missing list must not fail the site probe
        notes.append(f"volume types not listed: {type(e).__name__}; the last list stands")
        return None


def global_external_networks(conn, notes, admin=True):
    """The site's global external networks, [{name, id, families, shared}] sorted by name; read only. Global = an
    access_as_external RBAC policy with target "*" (Neutron writes one when a network's router:external flag is set);
    a network external for some projects only (per-project policies) is never listed. None when the list cannot be
    read (a note says why; the caller keeps what it had). An account that is not an administrator of the cloud (e.g.
    a domain manager) is shown only its own project's policies, so what it sees may be part of the list or nothing:
    not listed (None) either way, never published as the site's list."""
    if not admin:
        notes.append("external networks not listed: the account is not an administrator of the cloud, and the site "
                      "shows such an account only its own project's network policies, so the list could be "
                      "incomplete; the last list stands")
        return None
    try:
        out, seen = [], set()
        for rp in conn.network.rbac_policies(action="access_as_external", target_project_id="*"):
            if rp.target_project_id != "*" or getattr(rp, "object_type", "network") != "network" or rp.object_id in seen:
                continue
            seen.add(rp.object_id)
            net = conn.network.get_network(rp.object_id)
            families = sorted({int(sn.ip_version) for sn in conn.network.subnets(network_id=net.id)})
            out.append({"name": net.name, "id": net.id, "families": families, "shared": bool(net.is_shared)})
        return sorted(out, key=lambda n: (n["name"] or "", n["id"]))
    except Exception as e:  # noqa: BLE001 - a missing list must not fail the site probe
        notes.append(f"external networks not listed: {type(e).__name__}")
        return None


def site_probe(req):
    """CloudInfrastructure type openstack. Read-only apart from the tenant domain and the account's
    grants on it (and on the Designate project), which the tenant jobs' domain-scoped calls need."""
    acct = Account(req)
    conn = acct.own_scope()
    conn.authorize()
    access = conn.session.auth.get_access(conn.session)
    out = {"auth": True, "user_id": access.user_id, "project_id": access.project_id,
           "roles": sorted(access.role_names), "endpoint": acct.own_auth.get("auth_url", ""),
           "region": acct.region or "", "notes": []}
    out["services"] = sorted({s["type"] for s in access.service_catalog.catalog})
    aliases = sorted(e.alias for e in conn.network.extensions())
    out["evpn_extension"] = "evpn" in aliases
    out["network_extensions"] = [a for a in aliases if a.startswith(("bgp", "evpn"))]
    types = public_volume_types(conn, out["services"], out["notes"])
    if types is not None:
        out["volume_types"] = types
    if req.get("external_network"):
        net = conn.network.find_network(req["external_network"])
        out["external_network"] = {"found": net is not None,
                                   "external": bool(net and net.is_router_external),
                                   "id": net.id if net else ""}
    nets = global_external_networks(conn, out["notes"], "admin" in out["roles"])
    if nets is not None:
        out["external_networks"] = nets
    if req.get("project_domain"):
        name = req["project_domain"]
        # a domain manager reads its domain with a token scoped to it (a project-scoped one is refused the read)
        dconn = identity_conn(acct, conn, "admin" in out["roles"], name, out["notes"])
        dom = dconn.identity.find_domain(name)
        created = False
        if dom is None and req.get("create_project_domain"):
            dom = conn.identity.create_domain(name=name, description=req.get("domain_description", ""),
                                              is_enabled=True)
            created = True
        if dom is None:
            out["project_domain"] = {"name": name, "found": False}
        else:
            # the domain-scoped calls of the tenant jobs need a role on the domain: an account that holds admin
            # already gets admin there; any other account (a domain manager) is only reported, never widened
            granted = False
            if "admin" in out["roles"]:
                admin = conn.identity.find_role("admin", ignore_missing=False)
                if not conn.identity.validate_user_has_domain_role(dom, access.user_id, admin):
                    conn.identity.assign_domain_role_to_user(dom, access.user_id, admin)
                    granted = True
            out["project_domain"] = {"name": name, "found": True, "id": dom.id, "enabled": dom.is_enabled,
                                     "created": created, "admin_granted": granted}
    if req.get("sign_in"):
        ks = Keystone(acct)
        admin = "admin" in out["roles"]
        token = identity_token(ks, conn.session.get_token(), admin, req.get("project_domain"), out["notes"])
        out["sign_in"] = sign_in_reads(ks, token, access.user_id,
                                       dict(req["sign_in"], project_domain=req.get("project_domain") or "",
                                            admin=admin), out["notes"])
    if req.get("dns_project"):
        out["dns_project"] = probe_dns_project(acct, conn, access.user_id, "admin" in out["roles"],
                                               req["dns_project"], out["notes"])
    return out


def identity_conn(acct, conn, admin, domain, notes):
    """The connection for identity calls in a domain: an administrator's own; any other account's scoped to the
    domain (spike B: a domain manager's project-scoped token is refused every identity read)."""
    if admin:
        return conn
    try:
        dconn = acct.domain(domain)
        dconn.authorize()
        return dconn
    except Exception as e:  # noqa: BLE001 - the calls below say what is missing
        notes.append(f"the account could not sign in to domain {domain}: {type(e).__name__}")
        return conn


def probe_dns_project(acct, conn, user_id, admin, dp, notes):
    """The site's DNS project (tenant DNS, design/TENANT-DNS.md): owns the platform's parent zone and the environment
    zones; the account needs `member` there to create and share them. Identity calls with identity_conn."""
    iconn = identity_conn(acct, conn, admin, dp["domain"], notes)
    dom = iconn.identity.find_domain(dp["domain"], ignore_missing=False)
    created = False
    if dp.get("create"):
        # the platform makes it: found by the recorded id or by the platform's tag; a project of the name the
        # platform did not make is refused (NameTakenOnSite)
        proj, created, _ = ensure_owned_project(iconn, dom.id, dp["name"], owner_tag(DNS_PROJECT_OWNER),
                                                dp.get("description", ""), dp.get("project_id") or "",
                                                "site's tenant DNS")
    else:
        # the site's operator names a project of their own: used as it is, never changed
        proj = iconn.identity.find_project(dp["name"], domain_id=dom.id)
    if proj is None:
        return {"name": dp["name"], "found": False}
    member = iconn.identity.find_role("member", ignore_missing=False)
    granted = False
    if not iconn.identity.validate_user_has_project_role(proj, user_id, member):
        iconn.identity.assign_project_role_to_user(proj, user_id, member)
        granted = True
    quota = None
    if dp.get("zone_quota"):
        # every environment zone counts against the DNS project's zone quota (Designate default 10)
        cur = conn.dns.get_quota(proj.id)
        quota = {"zones": cur.zones, "raised": False}
        if (cur.zones or 0) < int(dp["zone_quota"]):
            conn.dns.update_quota(proj.id, zones=int(dp["zone_quota"]))
            quota = {"zones": int(dp["zone_quota"]), "raised": True}
    return {"name": dp["name"], "found": True, "id": proj.id, "created": created, "member_granted": granted,
            "zone_quota": quota}


def platform_project(req):
    """The account's own scope (cloud admin): ensure or remove a platform-owned project. Found by the recorded id
    (project_id) or by the platform's tag; a project of the name the platform did not make is refused on ensure and
    left alone on remove."""
    acct = Account(req)
    conn = acct.own_scope()
    access = conn.session.auth.get_access(conn.session)
    dom = conn.identity.find_domain(req["domain"], ignore_missing=False)
    tag = owner_tag(req.get("owner") or PLATFORM_PROJECT_OWNER)
    if req.get("state", "present") == "absent":
        try:
            proj, _ = owned_project(conn, dom.id, req["name"], tag, req.get("project_id") or "", "site's networks")
        except Refusal:
            proj = None                     # not ours: never deleted
        if proj is None:
            return {"name": req["name"], "deleted": False, "found": False}
        left = [f"network {describe(n, 'name')}" for n in conn.network.networks(project_id=proj.id)]
        left += [f"router {describe(r, 'name')}" for r in conn.network.routers(project_id=proj.id)]
        left += [f"port {describe(p, 'name', 'device_owner')}" for p in conn.network.ports(project_id=proj.id)]
        left += [f"rbac {r.id}" for r in conn.network.rbac_policies(project_id=proj.id)]
        if left:
            raise RuntimeError(f"project {req['name']} still holds: {', '.join(left[:10])}")
        conn.identity.delete_project(proj)
        return {"name": req["name"], "id": proj.id, "deleted": True}
    proj, created, _ = ensure_owned_project(conn, dom.id, req["name"], tag, req.get("description", ""),
                                            req.get("project_id") or "", "site's networks")
    member = conn.identity.find_role("member", ignore_missing=False)
    granted = False
    if not conn.identity.validate_user_has_project_role(proj, access.user_id, member):
        conn.identity.assign_project_role_to_user(proj, access.user_id, member)
        granted = True
    return {"name": proj.name, "id": proj.id, "domain_id": dom.id, "created": created, "member_granted": granted}


_IFACE_OWNERS = ("network:router_interface", "network:router_interface_distributed",
                 "network:ha_router_replicated_interface")


def _router_ports(conn, router_id):
    """The router's interface ports (not a gateway port)."""
    return [p for p in conn.network.ports(device_id=router_id) if p.device_owner in _IFACE_OWNERS]


def _delete_router(conn, router):
    """Every interface off the router (a port carries one IPv4 subnet or all the IPv6 subnets), then the router."""
    for p in _router_ports(conn, router.id):
        for ip in p.fixed_ips:
            conn.network.remove_interface_from_router(router, subnet_id=ip["subnet_id"])
    conn.network.delete_router(router)


def _add_router_interface(conn, router, cidr, sid, advertise):
    """Attach a subnet to the EVPN router. The evpn plugin allows one advertised interface per network, so an
    advertised attach that conflicts (409) is retried plainly; anything else raises (a plain attach is only ever
    asked of a router that already holds the advertised interface, so there is nothing to retry)."""
    def put(adv):
        body = {"subnet_id": sid, **({"advertise_host": True} if adv else {})}
        return conn.network.put(f"/routers/{router.id}/add_router_interface", json=body)

    r = put(advertise)
    if advertise and r.status_code == 409 and "RouterInterfaceAttachmentConflict" in r.text:
        r = put(False)
    if r.status_code >= 300:
        raise RuntimeError(f"router interface {cidr}: HTTP {r.status_code} {r.text[:300]}")


def _canon(addrs):
    """Addresses in their compressed form, sorted (a resolver written long-hand is not a change)."""
    return sorted(ipaddress.ip_address(a).compressed for a in addrs)


def _family_resolvers(resolvers, family):
    return [a for a in resolvers if (":" in a) == (family == 6)]


_GATEWAY, _FLOATING_IP = "network:router_gateway", "network:floatingip"
# ports Neutron deletes with their network
_AUTO_DELETED = ("network:dhcp", "network:distributed")


def _flag(value, default):
    """A boolean setting from the request (a job may send "True"/"false" strings); absent = the default."""
    if value is None or value == "":
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def _use(req):
    """The placement's uses (spec.use): virtualMachines default on, routerAttachments default off."""
    use = req.get("use") or {}
    return {"virtualMachines": _flag(use.get("virtualMachines"), True),
            "routerAttachments": _flag(use.get("routerAttachments"), False)}


def _router_name(conn, router_id):
    try:
        r = conn.network.get_router(router_id)
        return getattr(r, "name", None) or router_id
    except Exception:  # noqa: BLE001 - the name only makes the message easier to read
        return router_id


def _server_label(conn, server_id):
    """'<name> (<id>)' when Nova gives the server's name to this account, else the id (the name only makes the message
    easier to read: a server the account may not read is named by its id, nothing more is asked)."""
    names = conn.__dict__.setdefault("_shc_server_names", {}) if hasattr(conn, "__dict__") else {}
    if server_id not in names:
        try:
            names[server_id] = getattr(conn.compute.get_server(server_id), "name", None) or ""
        except Exception:  # noqa: BLE001 - not readable, not found, no compute service: the id says it
            names[server_id] = ""
    return f"{names[server_id]} ({server_id})" if names[server_id] else server_id


def _port_user(conn, p):
    """What a port on a placed network is, in the words of the placement's messages."""
    owner = p.device_owner or ""
    if owner == _GATEWAY:
        return f"router gateway of router {_router_name(conn, p.device_id)}"
    if owner == _FLOATING_IP:
        addrs = [ip.get("ip_address") for ip in (p.fixed_ips or []) if ip.get("ip_address")]
        return f"floating IP {addrs[0] if addrs else p.device_id}"
    if owner.startswith("compute:") and p.device_id:
        return f"port of server {_server_label(conn, p.device_id)}"
    return f"port {describe(p, 'name', 'device_owner')}"


def _blocks_delete(p, platform_project_id):
    owner = p.device_owner or ""
    if owner in (_GATEWAY, _FLOATING_IP):
        return True
    return getattr(p, "project_id", None) != platform_project_id and owner not in _AUTO_DELETED


def _shared_users(conn, net, project_id):
    """What keeps an access_as_shared policy in use: the project's ports on the network."""
    return [_port_user(conn, p) for p in conn.network.ports(network_id=net.id, project_id=project_id)]


def _external_users(conn, net, project_id):
    """What keeps an access_as_external policy in use: the project's router gateways and floating IPs on the network."""
    users = [f"router gateway of router {r.name or r.id}" for r in conn.network.routers(project_id=project_id)
             if ((r.external_gateway_info or {}).get("network_id")) == net.id]
    users += [f"floating IP {ip.floating_ip_address}"
              for ip in conn.network.ips(floating_network_id=net.id, project_id=project_id)]
    return users


def _use_off(conn, net, tenant, action, users_of, label, changed):
    """A use turned off: its policy for the tenant goes, unless the tenant still uses the network that way (Neutron
    would refuse the delete); then it stays and the users are reported (state removing)."""
    mine = [rp for rp in conn.network.rbac_policies(object_id=net.id, action=action) if rp.target_project_id == tenant]
    if not mine:
        return {"state": "off", "policyId": "", "inUse": []}
    users = users_of(conn, net, tenant)
    if users:
        return {"state": "removing", "policyId": mine[0].id, "inUse": users}
    for rp in mine:
        conn.network.delete_rbac_policy(rp)
    changed.append(f"{label} removed")
    return {"state": "off", "policyId": "", "inUse": []}


def _router_attachments(conn, net, proj, tenant, use, external, created, changed):
    """routerAttachments: an access_as_external policy for the tenant's project only (never the network's own
    router:external flag, never target "*"), owned by the platform project; external policies of other projects
    are pruned unless their project still uses them (pruneInUse)."""
    if use["routerAttachments"]:
        mine = next((rp for rp in external if rp.target_project_id == tenant), None)
        if mine is None:
            mine = conn.network.create_rbac_policy(object_type="network", object_id=net.id, action="access_as_external",
                                                   target_project_id=tenant, project_id=proj.id)
            created.append("external")
        state = {"state": "ready", "policyId": mine.id, "inUse": []}
    else:
        state = _use_off(conn, net, tenant, "access_as_external", _external_users, "external", changed)
    prune_in_use = []
    for rp in external:
        if rp.target_project_id in (tenant, "*"):
            continue
        users = _external_users(conn, net, rp.target_project_id)
        if users:
            prune_in_use += [f"project {rp.target_project_id}: {u}" for u in users]
            continue
        conn.network.delete_rbac_policy(rp)
        changed.append(f"external to {rp.target_project_id} removed")
    return state, prune_in_use


def _external_for_project(conn, net, ra):
    """Verify: the network reads router:external with the tenant's policy in place (read back from Neutron)."""
    if ra["state"] not in ("ready", "removing"):
        return False
    return bool(getattr(conn.network.get_network(net.id), "is_router_external", False))


def placement_network(req):
    """The cloud admin's own scope: a placement's network, subnets, EVPN router and share, in the platform project.
    prefixes carries both families (one subnet each); addressMode ("" = the site's default) is the IPv6 subnets' RA
    and address mode; resolvers (addresses of either family) become each subnet's dns_nameservers by family. The
    result's gateways maps every subnet's prefix to its gateway_ip as the site reports it ("" when it has none)."""
    acct = Account(req)
    conn = acct.own_scope()
    dom = conn.identity.find_domain(req["domain"], ignore_missing=False)
    proj = conn.identity.find_project(req["platformProject"], domain_id=dom.id)
    if proj is None:
        raise RuntimeError(f"platform project {req['platformProject']} does not exist (the CloudGateway creates it)")
    name, tag = req["name"], req["tag"]
    _require(req, "tag")
    # found by the recorded id or by the placement's tag, never by name: a network or router of that name may be
    # anybody's (Neutron does not keep names unique), and an object an earlier release named hn<number> is renamed
    net = _placed(conn.network.networks(project_id=proj.id), req.get("networkId"), tag, "network")
    router = _placed(conn.network.routers(project_id=proj.id), req.get("routerId"), tag, "router")
    if req.get("state", "present") == "absent":
        out = {"name": name, "found": bool(net or router), "deleted": []}
        if net is not None:
            # busy: every port of another project (Neutron deletes only its own DHCP and metadata ports with the
            # network), and router gateways and floating IPs of any project (their ports belong to no project)
            busy = [_port_user(conn, p) for p in conn.network.ports(network_id=net.id) if _blocks_delete(p, proj.id)]
            if busy:
                return {**out, "blocked": busy}
            for r in conn.network.rbac_policies(object_id=net.id):
                conn.network.delete_rbac_policy(r)
                out["deleted"].append(f"rbac {r.id}")
        if router is not None:
            ra = req.get("routerAdvertisement") or {}
            if net is not None and (ra.get("service_url") or "").strip():
                # before the interface goes: the service drops what it holds for the port (its marker would go
                # with the port anyway); never blocks the teardown
                try:
                    out["routerAdvertisement"] = _router_advertisement(acct, {
                        "state": "absent", "router_id": router.id, "network_id": net.id, "project_id": proj.id,
                        "service_url": ra["service_url"], "site": ra.get("site", ""), "timeout": ra.get("timeout")})
                except Exception as e:  # noqa: BLE001 - reported, the deletion goes on
                    out["routerAdvertisement"] = {"service": "found", "reason": "ServiceUnavailable",
                                                  "message": f"{type(e).__name__}: {e}"}
            _delete_router(conn, router)
            out["deleted"].append(f"router {router.id}")
        if net is not None:
            for sn in conn.network.subnets(network_id=net.id):
                conn.network.delete_subnet(sn)
                out["deleted"].append(f"subnet {sn.cidr}")
            conn.network.delete_network(net)
            out["deleted"].append(f"network {net.id}")
        return out

    created, changed = [], []
    desc = req.get("description", "")
    if net is None:
        net = conn.network.create_network(name=name, project_id=proj.id, mtu=int(req["mtu"]), description=desc)
        created.append("network")
    else:
        # names and descriptions are refreshed in place (Neutron's name and description are writable)
        upd = {k: v for k, v in (("mtu", int(req["mtu"])), ("name", name), ("description", desc))
               if getattr(net, k, None) != v}
        if upd:
            net = conn.network.update_network(net, **upd)
            changed.extend(sorted(upd))
    if tag not in (net.tags or []):
        conn.network.set_tags(net, sorted(set((net.tags or []) + [tag])))
    have = {sn.cidr: sn for sn in conn.network.subnets(network_id=net.id)}
    mode, resolvers = req.get("addressMode") or "", req.get("resolvers") or []
    subnets, gateways, applied_mode, seen = {}, {}, "", {4: 0, 6: 0}
    for cidr in req["prefixes"]:
        family = 6 if ":" in cidr else 4
        seen[family] += 1
        sn_name = f"{name}_v{family}" + (f"_{seen[family]}" if seen[family] > 1 else "")
        dns = _family_resolvers(resolvers, family)
        sn = have.get(cidr)
        if sn is None:
            # the RA and address modes are fixed at creation (Neutron can't change them on a subnet)
            v6 = {"ipv6_ra_mode": mode, "ipv6_address_mode": mode} if family == 6 and mode else {}
            sn = conn.network.create_subnet(name=sn_name, network_id=net.id, project_id=proj.id, description=desc,
                                            ip_version=family, cidr=cidr, enable_dhcp=True, dns_nameservers=dns, **v6)
            created.append(f"subnet {cidr}")
        else:
            upd = {}
            if _canon(sn.dns_nameservers or []) != _canon(dns):
                upd["dns_nameservers"] = dns
                changed.append(f"subnet {cidr} resolvers")
            for k, v in (("name", sn_name), ("description", desc)):
                if getattr(sn, k, None) != v:
                    upd[k] = v
                    changed.append(f"subnet {cidr} {k}")
            if upd:
                sn = conn.network.update_subnet(sn, **upd)
        if tag not in (getattr(sn, "tags", None) or []):
            conn.network.set_tags(sn, sorted(set((getattr(sn, "tags", None) or []) + [tag])))
        if family == 6:
            applied_mode = getattr(sn, "ipv6_address_mode", None) or ""
        subnets[cidr] = sn.id
        gateways[cidr] = getattr(sn, "gateway_ip", None) or ""
    if router is not None and str(req.get("recreateRouter", "")).lower() in ("true", "1", "yes"):
        _delete_router(conn, router)
        router = None
        changed.append("router recreated")
    if router is not None:
        rv = conn.network.get(f"/routers/{router.id}").json()["router"].get("evpn_vni")
        if int(rv or 0) != int(req["l3Vni"]):
            raise RuntimeError(f"router {name} has evpn_vni {rv}, expected {req['l3Vni']}")
    if router is None:
        r = conn.network.post("/routers", json={"router": {"name": name, "project_id": proj.id, "description": desc,
                                                           "evpn_vni": int(req["l3Vni"])}})
        if r.status_code >= 300:
            raise RuntimeError(f"router create: HTTP {r.status_code} {r.text[:300]}")
        router = conn.network.get_router(r.json()["router"]["id"])
        created.append("router")
    else:
        upd = {k: v for k, v in (("name", name), ("description", desc)) if getattr(router, k, None) != v}
        if upd:
            router = conn.network.update_router(router, **upd)
            changed.extend(f"router {k}" for k in sorted(upd))
    if tag not in (getattr(router, "tags", None) or []):
        conn.network.set_tags(router, sorted(set((getattr(router, "tags", None) or []) + [tag])))
    attached = {ip["subnet_id"] for p in _router_ports(conn, router.id) for ip in p.fixed_ips}
    for cidr, sid in subnets.items():
        if sid not in attached:
            # the first interface on the router carries advertise_host (a per-network setting on the plugin side:
            # the host routes of every subnet are advertised); the others attach plainly
            _add_router_interface(conn, router, cidr, sid, advertise=not attached)
            attached.add(sid)
            created.append(f"interface {cidr}")
    rbac, uses = None, {}
    tenant = req.get("shareProjectId")
    if tenant:
        use = _use(req)
        # a network external for every project (its router:external flag, set outside the platform) is not the
        # platform's to place: refused before any policy is written
        external = list(conn.network.rbac_policies(object_id=net.id, action="access_as_external"))
        if any(rp.target_project_id == "*" for rp in external):
            raise RuntimeError(f"network {name} is external for every project (its router:external flag is set outside "
                               "the platform); the platform does not place such a network")
        if use["virtualMachines"]:
            for rp in conn.network.rbac_policies(object_id=net.id, action="access_as_shared"):
                if rp.target_project_id == tenant:
                    rbac = rp
            if rbac is None:
                # owned by the platform project like the network itself (Neutron would otherwise own it by the
                # account's own project and count it against that project's rbac_policy quota)
                rbac = conn.network.create_rbac_policy(object_type="network", object_id=net.id,
                                                       action="access_as_shared", target_project_id=tenant,
                                                       project_id=proj.id)
                created.append("share")
            uses["virtualMachines"] = {"state": "ready", "policyId": rbac.id, "inUse": []}
        else:
            uses["virtualMachines"] = _use_off(conn, net, tenant, "access_as_shared", _shared_users, "share", changed)
            rbac = None
        for rp in conn.network.rbac_policies(object_id=net.id, action="access_as_shared"):
            if rp.target_project_id != tenant:
                conn.network.delete_rbac_policy(rp)
                changed.append(f"share to {rp.target_project_id} removed")
        uses["routerAttachments"], prune_in_use = _router_attachments(conn, net, proj, tenant, use, external,
                                                                      created, changed)
    evpn = conn.network.get(f"/routers/{router.id}").json()["router"].get("evpn_vni")
    out = {"name": name, "projectId": proj.id, "networkId": net.id, "routerId": router.id, "subnets": subnets,
           "gateways": gateways, "addressMode": applied_mode, "evpnVni": evpn,
           "shareId": rbac.id if rbac else (uses.get("virtualMachines") or {}).get("policyId", ""),
           "mtu": net.mtu, "created": created, "changed": changed}
    if tenant:
        ra = uses["routerAttachments"]
        out.update({"use": uses, "externalId": ra["policyId"] if ra["state"] in ("ready", "removing") else "",
                    "routerExternalForProject": _external_for_project(conn, net, ra),
                    "inUse": uses["virtualMachines"]["inUse"] + ra["inUse"], "pruneInUse": prune_in_use})
    return out


def _placed(objs, recorded_id, tag, what):
    """The placement's network or router among objs: the recorded id, else the one carrying the placement's tag
    (one at most); None when there is none. One recorded but tagged for another placement is refused."""
    objs = list(objs)
    if recorded_id:
        o = next((x for x in objs if x.id == recorded_id), None)
        if o is not None:
            others = [t for t in (getattr(o, "tags", None) or []) if t.startswith(PLACEMENT_TAG) and t != tag]
            if others:
                raise RuntimeError(f"{what} {recorded_id} carries another placement's tag ({others[0]})")
            return o
    tagged = [x for x in objs if tag in (getattr(x, "tags", None) or [])]
    if len(tagged) > 1:
        raise RuntimeError(f"{len(tagged)} {what}s in the platform project carry the tag {tag}; one is expected")
    return tagged[0] if tagged else None


# the tag every object of a placement in the platform project carries: shc-placement=<namespace>/<name>
PLACEMENT_TAG = "shc-placement="


def cluster_fips(req):
    """A cluster's floating IPs in the environment's project, with the site account scoped to that project.
    present: {project_id, network_id, descriptions: {api, ingress}} -> {network, api: {address, id, created},
    ingress: {...}, released: [ids of duplicates]}; each found by description first (a re-run after a failure
    creates nothing twice), else created on network_id. One found on another network is refused (the cluster's
    network is fixed). absent: {project_id, ids: [recorded ids], descriptions (optional)} -> {released, missing}; a
    recorded id that no longer exists counts as released (missing); floating IPs of other projects are never
    touched."""
    acct = Account(req)
    pid = req["project_id"]
    conn = acct.project(pid)
    descs = req.get("descriptions") or {}
    if req.get("state", "present") == "absent":
        ids = {i for i in (req.get("ids") or []) if i}
        for desc in descs.values():
            ids.update(i.id for i in conn.network.ips(description=desc, project_id=pid))
        released, missing = [], []
        for fid in sorted(ids):
            try:
                ip = conn.network.get_ip(fid)
            except openstack.exceptions.NotFoundException:
                ip = None
            if ip is None or getattr(ip, "project_id", pid) != pid:
                missing.append(fid)
                continue
            conn.network.delete_ip(fid, ignore_missing=True)
            released.append(fid)
        return {"released": released, "missing": missing}
    net_id = req["network_id"]
    if set(descs) != {"api", "ingress"}:
        raise ValueError("descriptions needs api and ingress")
    out = {"network": net_id, "released": []}
    for key in ("api", "ingress"):
        desc = descs[key]
        found = list(conn.network.ips(description=desc, project_id=pid))
        other = [i for i in found if i.floating_network_id != net_id]
        if other:
            raise RuntimeError(f"floating IP {desc} ({other[0].floating_ip_address}) is on network "
                               f"{other[0].floating_network_id}, not {net_id}")
        if found:
            # one per description: the associated one (or the first by id) stays, unassociated duplicates go
            found.sort(key=lambda i: (not getattr(i, "port_id", None), i.id))
            keep = found[0]
            for extra in found[1:]:
                if not getattr(extra, "port_id", None):
                    conn.network.delete_ip(extra.id, ignore_missing=True)
                    out["released"].append(extra.id)
            out[key] = {"address": keep.floating_ip_address, "id": keep.id, "created": False}
        else:
            ip = conn.network.create_ip(floating_network_id=net_id, description=desc)
            out[key] = {"address": ip.floating_ip_address, "id": ip.id, "created": True}
    return out


def network_name_check(req):
    """The networks named `name` as the installer finds them (install-config platform.openstack.externalNetwork takes a
    name only; the installer lists the networks of that name with the environment's credential and fails on two).
    clouds_yaml is the environment's own credential (the installer's: an application credential of the environment's
    project), so the list is what Neutron itself gives that project: its own networks, the shared ones, and those an
    RBAC policy exposes to it or to every project, whether or not the project can read the policy (a project never
    reads another project's policies, so counting policies cannot tell). router:external is the network's flag as
    Neutron computes it for that project. {project_id (the environment's project: the credential must be scoped to
    it), name, network_id} -> {name, project_id (the credential's), visible: [{id, project_id, shared, external}],
    unique: exactly one is visible and it is network_id, external: network_id is visible and external for the
    project}."""
    acct = Account(req)
    pid = req.get("project_id") or ""
    name = req["name"]
    want = req.get("network_id") or ""
    conn = acct.as_configured()
    _, _, scoped = token_info(conn)
    if pid and scoped != pid:
        raise RuntimeError(f"the environment's credential is scoped to project {scoped}, not to the environment's "
                           f"project {pid}")
    visible = []
    for n in conn.network.networks(name=name):
        if n.name != name:
            continue
        visible.append({"id": n.id, "project_id": n.project_id, "shared": bool(getattr(n, "is_shared", False)),
                        "external": bool(getattr(n, "is_router_external", False))})
    ids = [v["id"] for v in visible]
    mine = [v for v in visible if want and v["id"] == want]
    out = {"name": name, "project_id": scoped, "visible": visible,
           "unique": len(ids) == 1 and (not want or ids[0] == want),
           "external": bool(mine) and mine[0]["external"]}
    # the flavors the cluster asks for, as the project sees them (the installer finds a flavor by name with the same
    # credential and, without it, skips its own checks and fails much later)
    flavors = [f for f in (req.get("flavors") or []) if isinstance(f, str) and f]
    if flavors:
        out["flavors_missing"] = [f for f in dict.fromkeys(flavors)
                                  if conn.compute.find_flavor(f, ignore_missing=True) is None]
    # the image the cluster's nodes boot from, when one is set (the installer finds it by name with the same
    # credential, and without it fails after its whole run)
    images = [i for i in (req.get("images") or []) if isinstance(i, str) and i]
    if images:
        out["images_missing"] = [i for i in dict.fromkeys(images)
                                 if conn.image.find_image(i, ignore_missing=True) is None]
    return out


RA_KEY = "placement.routerAdvertisementBySite"
RA_TIMEOUT = 10


class RaRedirect(Exception):
    """The router advertisement service answered with a redirect: never followed (the token would go along)."""

    def __init__(self, code, location):
        super().__init__(f"HTTP {code} redirect to {location}")
        self.code, self.location = code, location


class _RaNoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RaRedirect(code, urllib.parse.urlsplit(newurl)._replace(query="", fragment="").geturl())


def _ra_verify_off(verify):
    """The site credential turns certificate checking off (verify: false, also as a string)."""
    return verify is False or (isinstance(verify, str) and verify.strip().lower() in ("false", "no", "off", "0"))


def _ra_origin(url):
    parts = urllib.parse.urlsplit(url)
    return parts.scheme.lower(), (parts.hostname or "").lower(), parts.port or (443 if parts.scheme == "https" else 80)


def _ra_http(method, url, token, verify, timeout, body=None, origin=None):
    """One call to the router advertisement service: (HTTP status, parsed JSON or {}). TLS is always verified, with
    the site credential's CA bundle (a path) or the system's trust; a redirect is never followed (RaRedirect), and the
    token goes only to the configured origin (scheme, host and port of the service URL)."""
    if _ra_verify_off(verify):
        raise ValueError("certificate verification is off")
    if origin is not None and _ra_origin(url) != origin:
        raise ValueError(f"{url} is not at the service's origin")
    if isinstance(verify, str) and verify:
        ctx = ssl.create_default_context(cafile=verify)
    else:
        ctx = ssl.create_default_context()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"X-Auth-Token": token, "Accept": "application/json",
                                          **({"Content-Type": "application/json"} if data is not None else {})})
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx), _RaNoRedirect())
    try:
        with opener.open(req, timeout=timeout) as r:
            status, text = r.status, r.read()
    except urllib.error.HTTPError as e:
        with e:
            status, text = e.code, e.read()
    try:
        return status, json.loads(text or b"{}")
    except ValueError:
        return status, {}


def _ra_router_port(conn, router_id, network_id):
    """The router's interface port on the network that carries IPv4 (the EVPN plugin's advertised interface)."""
    for p in conn.network.ports(device_id=router_id, network_id=network_id):
        if p.device_owner in _IFACE_OWNERS and any(":" not in (ip.get("ip_address") or ":")
                                                  for ip in (p.fixed_ips or [])):
            return p
    return None


def _ra_scrub(text, token):
    text = str(text)
    return text.replace(token, "<token>") if token else text


def router_advertisement(req):
    """state present|absent, router_id, network_id, service_url (empty = no service), project_id or
    domain + platformProject (the router's project: the token is scoped to it, as the service authorizes by the
    port's project), site (names the realization key in messages), timeout (seconds, default 10).
    -> {service: found|absent, port_id, nat, wanted, changed, restores, lastRestoredAt, reason, message, read}; reason
    "" = done; read = the service answered with the port's state (nat, wanted, restores and lastRestoredAt are its
    answer; without read they are only defaults: the caller must not report them as the port's state). Service errors come back with the service's reason (Unauthenticated, NotProjectScoped, NotYourPort,
    NotARouterInterface, NoSuchRouterPort, NotAnAdvertisingPort, BadRequest, OvnUnavailable, IdentityUnavailable,
    NetworkUnavailable, and NotFound, MethodNotAllowed, InternalError); ServiceUnavailable = no answer (or a redirect,
    which is never followed); InvalidSetting = not an https URL, or a site credential that turns certificate
    verification off (the call always verifies, and sends the token only to the URL's origin); NoRouterInterface = the router has no IPv4 interface on the network (present only: for absent there is nothing to drop). absent on a port the
    service answers NotAnAdvertisingPort for (no redistribute option: nothing advertised) is done, not a failure. Never raises for the service: a failing service is the caller's
    status, not an error. absent drops nat only where the service holds it (wanted): a nat the site set itself
    stays."""
    return _router_advertisement(None, req)


def _router_advertisement(acct, req):
    out = {"service": "absent", "port_id": "", "nat": False, "wanted": False, "changed": False, "restores": 0,
           "lastRestoredAt": "", "reason": "", "message": "", "read": False}
    url = (req.get("service_url") or "").strip().rstrip("/")
    if not url:
        return out
    out["service"] = "found"
    key = f"{RA_KEY}[{req['site']}].url" if req.get("site") else f"{RA_KEY}[<site>].url"
    if not url.lower().startswith("https://"):
        return {**out, "reason": "InvalidSetting", "message": f"{key} must be an https:// URL"}
    state = req.get("state", "present")
    if state not in ("present", "absent"):
        raise ValueError("state must be present or absent")
    acct = acct or Account(req)
    if _ra_verify_off(acct.verify):
        # the token is the site account's: never sent over a connection whose certificate is not checked
        return {**out, "reason": "InvalidSetting",
                "message": (f"the site credential of site {req.get('site') or '<site>'} turns certificate verification "
                            "off (verify: false); the router advertisement service is called only with verification: "
                            "give the credential a CA bundle (cacert) instead")}
    origin = _ra_origin(url)
    pid = req.get("project_id")
    if not pid:
        own = acct.own_scope()
        dom = own.identity.find_domain(req["domain"], ignore_missing=False)
        proj = own.identity.find_project(req["platformProject"], domain_id=dom.id)
        if proj is None:
            raise RuntimeError(f"platform project {req['platformProject']} does not exist")
        pid = proj.id
    conn = acct.project(pid)
    port = _ra_router_port(conn, req["router_id"], req["network_id"])
    if port is None and state == "absent":
        # no IPv4 interface: no port that could carry nat, so nothing to drop (an IPv6-only placement, or an
        # interface already removed)
        return {**out, "read": True}
    if port is None:
        return {**out, "reason": "NoRouterInterface",
                "message": f"router {req['router_id']} has no IPv4 interface on network {req['network_id']}"}
    out["port_id"] = port.id
    # the site account's token scoped to the router's project: the roles the account holds there (the platform gives
    # it member; the service needs no more). A narrower token would need a credential of its own (Review R2 L5).
    token = conn.session.get_token()
    timeout = float(req.get("timeout") or RA_TIMEOUT)
    base = f"{url}/v1/router-ports/{urllib.parse.quote(str(port.id), safe='')}"

    def call(method, path="", body=None):
        try:
            return _ra_http(method, base + path, token, acct.verify, timeout, body, origin=origin)
        except RaRedirect as e:
            return 0, {"error": "ServiceUnavailable",
                       "message": f"router advertisement service at {url} answered with a redirect ({e}); the platform "
                                  "does not follow redirects of this service"}
        except Exception as e:  # noqa: BLE001 - transport errors (refused, timeout, TLS) are the service's state
            reason = getattr(e, "reason", e)
            return 0, {"error": "ServiceUnavailable",
                       "message": f"router advertisement service at {url}: {_ra_scrub(reason, token)}"}

    def failed(status, body):
        return {**out, "reason": str(body.get("error") or "ServiceUnavailable"),
                "message": _ra_scrub(body.get("message") or f"HTTP {status} from {url}", token)}

    def nothing_to_drop(status, body):
        # the service answers 409 NotAnAdvertisingPort for a port without the redistribute option (GET and PUT
        # false alike): the port advertises nothing, so for absent there is nothing to do
        return state == "absent" and status == 409 and body.get("error") == "NotAnAdvertisingPort"

    status, body = call("GET")
    if nothing_to_drop(status, body):
        return {**out, "read": True}
    if status != 200:
        return failed(status, body)
    out.update({"read": True, "nat": bool(body.get("nat")), "wanted": bool(body.get("wanted")),
                "restores": int(body.get("restores") or 0), "lastRestoredAt": str(body.get("lastRestoredAt") or "")})
    enable = state == "present"
    # present: nat held by the service (wanted); absent: only what the service holds is dropped
    if (enable and out["nat"] and out["wanted"]) or (not enable and not out["wanted"]):
        return out
    status, body = call("PUT", "/nat", {"enabled": enable})
    if nothing_to_drop(status, body):
        return {**out, "nat": False, "wanted": False}
    if status != 200:
        return failed(status, body)
    after = body.get("after") or []
    return {**out, "nat": "nat" in after, "wanted": enable, "changed": bool(body.get("changed"))}


def platform_inventory(req):
    """The cloud admin's own scope, read only: what the platform project holds."""
    acct = Account(req)
    conn = acct.own_scope()
    dom = conn.identity.find_domain(req["domain"], ignore_missing=False)
    proj = conn.identity.find_project(req["platformProject"], domain_id=dom.id)
    if proj is None:
        return {"found": False, "networks": [], "routers": []}
    nets = []
    for n in conn.network.networks(project_id=proj.id):
        shares = [r.target_project_id for r in conn.network.rbac_policies(object_id=n.id, action="access_as_shared")]
        nets.append({"id": n.id, "name": n.name, "tags": n.tags or [], "sharedTo": shares})
    routers = []
    for r in conn.network.routers(project_id=proj.id):
        vni = conn.network.get(f"/routers/{r.id}").json()["router"].get("evpn_vni")
        routers.append({"id": r.id, "name": r.name, "tags": getattr(r, "tags", None) or [], "evpnVni": vni})
    return {"found": True, "projectId": proj.id, "networks": nets, "routers": routers}


def project_routers(req):
    """Read only. {project_id, tag} -> {routers: [{id, name, project_id, tags, external_gateway_info: {network_id,
    external_fixed_ips: [{subnet_id, ip_address}]}}]}: the routers of the environment's project that carry the tag,
    with the site account scoped to that project. The installer names its router by its generation (Terraform:
    <infraID>-external-router; Cluster API: k8s-clusterapi-cluster-openshift-cluster-api-guests-<infraID>) but tags it
    openshiftClusterID=<infraID> in both, as its destroy finds it. Neutron filters by tag and project; both are
    checked again here (routers of other projects are never listed, the account may see them all)."""
    pid = str(req.get("project_id") or "")
    tag = str(req.get("tag") or "")
    if not pid or not tag:
        raise ValueError("project-routers needs project_id and tag")
    conn = Account(req).project(pid)
    out = []
    for r in conn.network.routers(tags=tag, project_id=pid):
        tags = list(getattr(r, "tags", None) or [])
        if tag not in tags or (getattr(r, "project_id", None) or pid) != pid:
            continue
        gw = getattr(r, "external_gateway_info", None) or {}
        out.append({"id": r.id, "name": r.name, "project_id": getattr(r, "project_id", None) or pid, "tags": tags,
                    "external_gateway_info": {
                        "network_id": gw.get("network_id") or "",
                        "external_fixed_ips": [{"subnet_id": (f or {}).get("subnet_id") or "",
                                                "ip_address": (f or {}).get("ip_address") or ""}
                                               for f in gw.get("external_fixed_ips") or []]}})
    return {"routers": out}


# ---- Keystone: access groups, cluster users, federation reads --------------------------------------------------
# Raw v3 calls with the shapes proven on a site's Keystone. The site account's token comes from its clouds.yaml entry
# (openstacksdk, any auth type); a cluster user's token from a password sign-in. Passwords and credential secrets live
# in this process only: they are never returned, printed, put into an exception text or written anywhere but the
# private Vault write body of cluster-identity.

# Every object the platform creates in Keystone carries this text in its description; an object of the same name
# without it is someone else's and is never adopted, changed or deleted.
MARKER = "Managed by the sovereign cloud platform; do not change."
# The cluster user's options: it never signs in by itself (no inactivity or expiry), a wrong password sent by anybody
# cannot lock the platform out, and only an administrator sets its password.
USER_OPTIONS = {"ignore_user_inactivity": True, "ignore_password_expiry": True,
                "ignore_change_password_upon_first_use": True, "ignore_lockout_failure_attempts": True,
                "lock_password": True}
# Keystone revokes the tokens of a user issued up to the second of a password set: a token issued in that second is
# refused on its first use. Sign in only this long after a set.
PASSWORD_SETTLE_SECONDS = 2.0
UNIT_ATTEMPTS = 5
UNIT_RETRY_WAIT = (1.0, 4.0)
# what the site's identity service accepts for a name (longer names are refused with HTTP 400)
USER_NAME_MAX = 255
GROUP_NAME_MAX = 64
_SECRETS = []


def _hide(value):
    """Remember a secret so that no message of this process can carry it."""
    if value:
        _SECRETS.append(str(value))
    return value


def scrub(text):
    text = str(text)
    for s in _SECRETS:
        if s and s in text:
            text = text.replace(s, "<secret>")
    return text


def _password():
    # 48 random characters from three classes plus one of each (a site's password rule may ask for them)
    return _hide(secrets.token_urlsafe(36)[:44] + "aZ9!")


class KsError(Exception):
    """A Keystone answer other than the expected one: the status and Keystone's own message (never a request body)."""

    def __init__(self, status, message):
        super().__init__(f"HTTP {status}: {message}")
        self.status, self.message = status, message


class Refusal(Exception):
    """The site refused a step of the job: reason and message go to the object's condition."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason, self.message = reason, message


class NotAnswered(Exception):
    """The site did not give an answer the step can act on (HTTP 5xx, a timeout, no connection): nothing was changed
    for it, and the next run asks again. Never a refusal."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


def transient(e):
    """True for a fault of the moment (the site or the network), not an answer about the request."""
    if isinstance(e, NotAnswered):
        return True
    if isinstance(e, KsError):
        return e.status >= 500 or e.status in (408, 429)
    status = getattr(e, "http_status", None) or getattr(e, "status_code", None)
    if isinstance(status, int):
        return status >= 500 or status in (408, 429)
    names = {c.__name__ for c in type(e).__mro__}
    return bool(names & {"ConnectionError", "Timeout", "ConnectFailure", "ConnectTimeout", "RetriableConnectionFailure",
                         "ServiceUnavailable", "SSLError", "UnknownConnectionError", "DiscoveryFailure",
                         "TimeoutError", "ConnectionRefusedError", "ConnectionResetError"})


def _plain(e):
    """A message for a person: Keystone's own message, else what happened (never a request path or body)."""
    if isinstance(e, (KsError, NotAnswered, Refusal)):
        return e.message
    status = getattr(e, "http_status", None) or getattr(e, "status_code", None)
    if isinstance(status, int):
        return f"the site answered HTTP {status}"
    return f"the site could not be reached ({type(e).__name__})"


def _require(req, *keys):
    """A step that changes or deletes runs only with every filter it is given: an empty value would widen it."""
    for k in keys:
        if not str(req.get(k) or "").strip():
            raise ValueError(f"refused: {k} is empty; the {req.get('action', 'step')} step needs it")


def _keystone_base(auth_url):
    base = (auth_url or "").rstrip("/")
    return base if base.endswith("/v3") else base + "/v3"


class Keystone:
    """Raw Keystone v3 calls against the account's auth_url."""

    def __init__(self, acct, timeout=60):
        import requests  # keystoneauth's dependency; imported here so the other actions do not need it
        self.acct = acct
        self.base = _keystone_base(acct.own_auth.get("auth_url", ""))
        self.http = requests.Session()
        verify = acct.verify
        if isinstance(verify, str) and verify.strip().lower() in ("false", "no", "off", "0"):
            verify = False
        self.http.verify = verify
        self.timeout = timeout
        self._site, self._roles, self._own = {}, {}, None

    def request(self, token, method, path, body=None, headers=None):
        url = path if path.startswith("http") else self.base + path
        h = dict(headers or {})
        if token:
            h["X-Auth-Token"] = token
        r = self.http.request(method, url, json=body, headers=h, timeout=self.timeout, allow_redirects=False)
        try:
            data = r.json() if r.content else {}
        except ValueError:
            data = {}
        return r.status_code, data, r

    def call(self, token, method, path, body=None, ok=(200, 201, 204)):
        status, data, _ = self.request(token, method, path, body)
        if status not in ok:
            # Keystone's own message, else the status in plain words (a page without JSON: never "None")
            msg = ((data.get("error") or {}).get("message")) if isinstance(data, dict) else None
            raise KsError(status, scrub(msg) if msg else f"the site answered HTTP {status}")
        return data

    def site(self, domain_name):
        """The site account's token scoped to a domain (cached per domain), its user id and the domain's id. Every
        identity read of a domain manager needs a domain-scoped token: a project-scoped one is refused them all."""
        if domain_name not in self._site:
            conn = self.acct.domain(domain_name)
            token = conn.session.get_token()
            user_id, domain_id, _ = token_info(conn)
            self._site[domain_name] = (token, user_id, domain_id)
            self._roles[domain_name] = set(conn.session.auth.get_access(conn.session).role_names or [])
        return self._site[domain_name]

    def site_roles(self, domain_name):
        """The roles of the account's token scoped to the domain."""
        self.site(domain_name)
        return self._roles[domain_name]

    def own(self):
        """The account's token in its own scope and its roles (an administrator's reads are not filtered to a
        domain)."""
        if self._own is None:
            conn = self.acct.own_scope()
            self._own = (conn.session.get_token(), set(conn.session.auth.get_access(conn.session).role_names or []))
        return self._own

    def site_project(self, project_id):
        conn = self.acct.project(project_id)
        return conn.session.get_token()

    def role_ids(self, token, names, missing_ok=False):
        out = {}
        for n in names:
            roles = [r for r in self.call(token, "GET", "/roles?name=" + urllib.parse.quote(n)).get("roles", [])
                     if r.get("name") == n and not r.get("domain_id")]
            if not roles:
                if missing_ok:
                    continue
                raise KsError(404, f"role {n} does not exist on the site")
            out[n] = roles[0]["id"]
        return out

    def password_token(self, user_id, password, project_id):
        """(status, token, body) of a password sign-in as the user, scoped to the project."""
        status, data, r = self.request(None, "POST", "/auth/tokens", {"auth": {
            "identity": {"methods": ["password"], "password": {"user": {"id": user_id, "password": password}}},
            "scope": {"project": {"id": project_id}}}})
        return status, r.headers.get("X-Subject-Token"), data

    def app_cred_token(self, cred_id, secret):
        status, data, r = self.request(None, "POST", "/auth/tokens", {"auth": {
            "identity": {"methods": ["application_credential"],
                         "application_credential": {"id": cred_id, "secret": secret}}}})
        return status, r.headers.get("X-Subject-Token"), data


def _marked(obj):
    return MARKER in ((obj or {}).get("description") or "")


def _assigned(ks, token, project_id, kind, oid):
    """The role ids the user or group holds directly on the project."""
    data = ks.call(token, "GET", f"/role_assignments?{kind}.id={oid}&scope.project.id={project_id}")
    return {a["role"]["id"] for a in data.get("role_assignments", []) if "role" in a}


def _find_by_name(ks, token, kind, name, domain_id):
    """The object of that name in the domain, or None. A name longer than the site allows cannot exist there: None,
    and no call (Keystone 2026.2 answers a lookup by such a name with HTTP 500)."""
    limit = {"groups": GROUP_NAME_MAX, "users": USER_NAME_MAX, "projects": GROUP_NAME_MAX}.get(kind)
    if limit and len(name or "") > limit:
        return None
    q = urllib.parse.urlencode({"name": name, "domain_id": domain_id})
    items = [o for o in ks.call(token, "GET", f"/{kind}?{q}").get(kind, [])
             if o.get("name") == name and o.get("domain_id") == domain_id]
    return items[0] if items else None


def _group_domain_tokens(ks, req):
    """(token for the group domain, its id, token for the project's domain). A group domain other than the project's
    (the identity provider's own domain) needs the admin role: a domain manager may grant only to its own domain's
    groups. The account's missing right is said plainly, never as a raw Unauthorized."""
    ptoken, _, _ = ks.site(req["domain"])
    if req["group_domain"] == req["domain"]:
        gtoken, _, gdom = ks.site(req["group_domain"])
        return gtoken, gdom, ptoken
    try:
        gtoken, _, gdom = ks.site(req["group_domain"])
        roles = ks.site_roles(req["group_domain"])
    except Exception as e:  # noqa: BLE001 - classified: a fault of the moment goes up, anything else is the right
        if transient(e):
            raise
        raise Refusal("GrantRefused", f"The site account holds no role on domain {req['group_domain']}; sign-in "
                                      "groups in a domain other than the project's need the admin role there") from None
    if "admin" not in roles:
        raise Refusal("GrantRefused", f"The site account is not an administrator; sign-in groups in domain "
                                      f"{req['group_domain']}, which is not the project's domain, need the admin "
                                      "role")
    return gtoken, gdom, ptoken


def _roles_elsewhere(ks, req, gtoken, gid):
    """(trusted, assignments) of a group anywhere on the site. Inside the project's domain a domain-scoped token
    lists them all (a domain manager sees its domain's projects); a group of another domain is read with the account's
    own token as an administrator. Anything else cannot be trusted to be complete: the group is not deleted."""
    if req["group_domain"] == req["domain"]:
        token = gtoken
    else:
        token, roles = ks.own()
        if "admin" not in roles:
            return False, "its roles on other projects could not be read (the account is not an administrator)"
    status, body, _ = ks.request(token, "GET", f"/role_assignments?group.id={gid}")
    if status != 200:
        return False, f"its roles on other projects could not be read (HTTP {status})"
    return True, body.get("role_assignments", [])


def access_groups(req):
    """state present: {domain (the project's), project_id, group_domain, entity, managed_roles, groups: [{group, roles,
    description}], keep: [names wanted but not ready]} -> {groupDomainId, groups: [{group, id, roles, state
    Granted|Failed|Unchecked, message, created, held}], unwanted: [names]}. Failed = the site refused (Keystone's
    message); Unchecked = the site did not answer (5xx, no connection): nothing is known, the group keeps what it had.
    held = the group held its roles on the project before this run. state absent: {..., groups: [{group, delete}]} ->
    {groups: [{group, id, revoked, deleted, kept, error}]}; both report changed (anything written): each group on its
    own (one that cannot be removed now is reported with error and the others go on)."""
    _require(req, "domain", "project_id", "group_domain")
    acct = Account(req)
    ks = Keystone(acct)
    gtoken, gdom, ptoken = _group_domain_tokens(ks, req)
    pid = req["project_id"]
    managed = ks.role_ids(ptoken, req.get("managed_roles") or [], missing_ok=True)
    by_id = {v: k for k, v in managed.items()}
    changed = False   # anything written on the site (the job reports "changed" only then)
    if req.get("state", "present") == "absent":
        out = []
        for g in req.get("groups") or []:
            name = str(g.get("group") or "")
            rec = {"group": name, "id": "", "revoked": [], "deleted": False, "kept": "", "error": ""}
            out.append(rec)
            if not name.strip():
                rec["error"] = "no group name"
                continue
            try:
                grp = _find_by_name(ks, gtoken, "groups", name, gdom)
                if grp is None:
                    rec["kept"] = "not found"
                    continue
                rec["id"] = grp["id"]
                if not _marked(grp):
                    rec["kept"] = "not the platform's"
                    continue
                for rid in sorted(_assigned(ks, ptoken, pid, "group", grp["id"]) & set(by_id)):
                    ks.call(ptoken, "DELETE", f"/projects/{pid}/groups/{grp['id']}/roles/{rid}", ok=(204, 404))
                    rec["revoked"].append(by_id[rid])
                    changed = True
                if g.get("delete"):
                    trusted, left = _roles_elsewhere(ks, req, gtoken, grp["id"])
                    if not trusted:
                        rec["kept"] = left
                    elif left:
                        rec["kept"] = "holds a role on another project"
                    else:
                        ks.call(gtoken, "DELETE", f"/groups/{grp['id']}", ok=(204, 404))
                        rec["deleted"] = True
                        changed = True
            except Exception as e:  # noqa: BLE001 - reported per group: the others go on
                rec["error"] = scrub(_plain(e))
        return {"groups": out, "changed": changed}

    wanted = {g["group"] for g in req.get("groups") or []} | set(req.get("keep") or [])
    out = []
    for g in req.get("groups") or []:
        name = g["group"]
        rec = {"group": name, "id": "", "roles": list(g.get("roles") or []), "state": "Granted", "message": "",
               "created": False, "held": False}
        try:
            if len(name) > GROUP_NAME_MAX:
                raise Refusal("GrantRefused", f"the group name {name} is longer than the site allows "
                                              f"({GROUP_NAME_MAX} characters)")
            grp = _find_by_name(ks, gtoken, "groups", name, gdom)
            if grp is not None and not _marked(grp):
                raise KsError(409, f"a group named {name} exists in domain {req['group_domain']} and is not the "
                                   "platform's")
            if grp is None:
                grp = ks.call(gtoken, "POST", "/groups", {"group": {
                    "name": name, "domain_id": gdom,
                    "description": f"{g.get('description') or 'Access group ' + name}. {MARKER}"}})["group"]
                rec["created"] = True
                changed = True
            rec["id"] = grp["id"]
            want = ks.role_ids(ptoken, rec["roles"])
            have = _assigned(ks, ptoken, pid, "group", grp["id"])
            rec["held"] = bool(want) and set(want.values()) <= have
            for rid in sorted(set(want.values()) - have):
                ks.call(ptoken, "PUT", f"/projects/{pid}/groups/{grp['id']}/roles/{rid}")
                changed = True
            # an access group that moved between edit and view keeps only the role it has now
            for rid in sorted((have & set(by_id)) - set(want.values())):
                ks.call(ptoken, "DELETE", f"/projects/{pid}/groups/{grp['id']}/roles/{rid}", ok=(204, 404))
                changed = True
        except Exception as e:  # noqa: BLE001 - per group: a refusal is the group's, a fault of the moment too
            if isinstance(e, (KsError, Refusal)) and not transient(e):
                rec.update({"state": "Failed", "message": scrub(_plain(e))})
            elif transient(e):
                rec.update({"state": "Unchecked", "message": scrub(_plain(e))})
            else:
                raise
        out.append(rec)
    unwanted = []
    prefix = (req.get("entity") or "") + "."
    if prefix != ".":
        data = ks.call(ptoken, "GET", f"/role_assignments?scope.project.id={pid}")
        for gid in sorted({a["group"]["id"] for a in data.get("role_assignments", []) if "group" in a
                           and a.get("role", {}).get("id") in by_id}):
            status, body, _ = ks.request(gtoken, "GET", f"/groups/{gid}")
            grp = body.get("group") if status == 200 else None
            if (grp and _marked(grp) and grp.get("domain_id") == gdom and grp.get("name", "").startswith(prefix)
                    and grp["name"] not in wanted):
                unwanted.append(grp["name"])
    return {"groupDomainId": gdom, "groups": out, "unwanted": sorted(set(unwanted)), "changed": changed}


def _step_refusal(reason, step, e):
    """The site's answer to a step: a refusal (4xx), or, for an answer that is not one (5xx, 408, 429, no
    connection), "the site did not answer" (nothing refused; the next run tries again)."""
    if transient(e):
        return NotAnswered(f"The site did not answer {step} ({scrub(_plain(e))}); nothing was refused, the next run "
                           "tries again.")
    return Refusal(reason, f"The site refused {step}: {e.message if isinstance(e, KsError) else scrub(e)}. "
                           "The cluster cannot be built.")


def _read_vault_credential(path):
    """The application credential id and secret of the cluster's Vault path, from a private file: the clouds.yaml
    itself (common/files/shc_vault_files.py writes it there) or a KV v2 answer; None when there is none."""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            body = yaml.safe_load(f) or {}
        if "clouds" not in body:
            body = yaml.safe_load(((body.get("data") or {}).get("data") or {}).get("clouds.yaml") or "") or {}
        auth = (body.get("clouds") or {}).get("openstack", {}).get("auth") or {}
    except (ValueError, yaml.YAMLError, AttributeError):
        return None
    cid, secret = auth.get("application_credential_id"), _hide(auth.get("application_credential_secret"))
    return (cid, secret) if cid and secret else None


def _write_private(path, data):
    """A file only this user can read, written in full or not at all."""
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".shc-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _utc_now():
    """Now in UTC, ISO 8601 / RFC 3339 to the second (2026-11-14T18:19:03Z)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def cluster_identity(req):
    """{domain (the environment project's domain name), project_id, user_name, description, roles, credential_name,
    credential_description, vault_file (the Vault read of the cluster's credential path, '' = none), out_file (where a
    new credential's Vault write body goes)} -> {user: {id, name, domainId, created}, roles: {granted}, credential: {id,
    name, kept, createdAt (UTC, when this run made it; "" for a kept credential)}, wrote}. A refusal exits with {reason,
    message} (UserRefused, RoleRefused, PasswordRefused, CredentialRefused, CredentialSecretMissing).
    recorded_credential_id: the credential the cluster recorded. The rule: (a) recorded, and Vault holds the same
    credential: the site answers 201 = kept, 401/404 = made again, anything else = stop, nothing changed; (b) recorded,
    and Vault holds another credential (a run whose record failed, a raced first write, a rolled-back version): kept
    (and recorded) only when it signs in as this cluster's user on this project, else CredentialSecretMissing with
    nothing changed (the recorded credential may be the one the running cluster uses); nothing usable in Vault:
    CredentialSecretMissing; (c) nothing recorded: a credential in Vault that signs in is kept, else one is made."""
    _require(req, "domain", "project_id", "user_name", "credential_name")
    have_cred = _read_vault_credential(req.get("vault_file"))
    recorded = str(req.get("recorded_credential_id") or "").strip()
    if recorded and not have_cred:
        raise Refusal("CredentialSecretMissing", f"The cluster's credential {recorded} is recorded, but its secret is "
                      "not in the platform's secret store. The cluster may still be using the credential, so nothing "
                      "was changed on the site. A platform administrator must restore the secret or replace the "
                      "credential in the cluster.")
    acct = Account(req)
    ks = Keystone(acct)
    pid = req["project_id"]
    name = req["user_name"]
    if len(name) > USER_NAME_MAX or len(req["credential_name"]) > USER_NAME_MAX:
        raise Refusal("UserRefused", f"The cluster's user name {name} is longer than the site's identity service "
                                     f"allows ({USER_NAME_MAX} characters). The cluster cannot be built.")
    token, _, dom = ks.site(req["domain"])
    if recorded and have_cred[0] != recorded:
        _check_other_credential(ks, token, req, recorded, have_cred, name, dom, pid)
    # the user
    try:
        user = _find_by_name(ks, token, "users", name, dom)
        created = False
        if user is not None and not _marked(user):
            raise KsError(409, f"a user named {name} exists in domain {req['domain']} and is not the platform's")
        if user is None:
            user = ks.call(token, "POST", "/users", {"user": {
                "name": name, "domain_id": dom, "enabled": True, "options": USER_OPTIONS,
                "description": f"{req.get('description') or 'OpenShift cluster user'}. {MARKER}"}})["user"]
            created = True
        elif any((user.get("options") or {}).get(k) is not v for k, v in USER_OPTIONS.items()):
            user = ks.call(token, "PATCH", f"/users/{user['id']}", {"user": {"options": USER_OPTIONS}})["user"]
    except KsError as e:
        raise _step_refusal("UserRefused", "the cluster's user " + name, e)
    uid = user["id"]
    # its roles on the environment's project
    granted = []
    try:
        want = ks.role_ids(token, req.get("roles") or ["member"])
        have = _assigned(ks, token, pid, "user", uid)
        for rname, rid in want.items():
            if rid not in have:
                try:
                    ks.call(token, "PUT", f"/projects/{pid}/users/{uid}/roles/{rid}")
                except KsError as e:
                    raise _step_refusal("RoleRefused", f"the role {rname} on the project for user {name}", e)
                granted.append(rname)
    except KsError as e:
        raise _step_refusal("RoleRefused", f"the roles on the project for user {name}", e)
    result = {"user": {"id": uid, "name": name, "domainId": dom, "created": created}, "roles": {"granted": granted}}
    # the credential in Vault still signs in as this user on this project: kept, no password touched. Only an answer
    # that says the credential is not valid (401, 404) or a token of another user or project makes a new one; any
    # other answer (5xx, a timeout, no connection) stops here with nothing changed: the cluster's working credential
    # is never replaced because the site had a bad moment
    if have_cred:
        try:
            status, _, body = ks.app_cred_token(*have_cred)
        except Exception as e:  # noqa: BLE001 - the site did not answer
            raise NotAnswered("The site's identity service did not answer the check of the cluster's credential "
                              f"({_plain(e)}); nothing was changed, the next run checks again.") from None
        tok = (body or {}).get("token") or {}
        if status == 201 and (tok.get("user") or {}).get("id") == uid and (tok.get("project") or {}).get("id") == pid:
            result["credential"] = {"id": have_cred[0], "name": req["credential_name"], "kept": True, "createdAt": ""}
            result["wrote"] = False
            return result
        if status not in (201, 401, 404):
            raise NotAnswered("The site's identity service did not answer the check of the cluster's credential "
                              f"(HTTP {status}); nothing was changed, the next run checks again.")
    cred = _credential_unit(ks, token, uid, name, pid, req)
    clouds = {"clouds": {"openstack": {
        "auth": {"auth_url": acct.own_auth.get("auth_url", ""), "application_credential_id": cred["id"],
                 "application_credential_secret": cred["secret"]},
        "region_name": acct.region or "", "interface": "public", "identity_api_version": 3,
        "auth_type": "v3applicationcredential"}}}
    _write_private(req["out_file"], {"data": {"clouds.yaml": yaml.safe_dump(clouds, default_flow_style=False)}})
    result["credential"] = {"id": cred["id"], "name": req["credential_name"], "kept": False,
                            "createdAt": cred.get("createdAt", "")}
    result["wrote"] = True
    return result


def _check_other_credential(ks, token, req, recorded, have_cred, name, dom, pid):
    """Vault holds another credential than the recorded one: before anything is written, it must sign in as this
    cluster's user (by name, the platform's) on this project; a site that does not answer stops with nothing changed;
    any other answer is CredentialSecretMissing (the recorded credential may still be the running cluster's)."""
    missing = Refusal("CredentialSecretMissing", f"The cluster's credential {recorded} is recorded, but the platform's "
                      f"secret store holds another credential ({have_cred[0]}) that does not sign in as this cluster. "
                      "The cluster may still be using the recorded credential, so nothing was changed on the site. A "
                      "platform administrator must restore the secret or replace the credential in the cluster.")
    try:
        user = _find_by_name(ks, token, "users", name, dom)
        status, _, body = ks.app_cred_token(*have_cred)
    except Exception as e:  # noqa: BLE001 - classified
        if isinstance(e, KsError) and not transient(e):
            raise _step_refusal("UserRefused", "the cluster's user " + name, e) from None
        raise NotAnswered("The site's identity service did not answer the check of the cluster's credential "
                          f"({_plain(e)}); nothing was changed, the next run checks again.") from None
    if status >= 500 or status in (408, 429):
        raise NotAnswered("The site's identity service did not answer the check of the cluster's credential "
                          f"(HTTP {status}); nothing was changed, the next run checks again.")
    tok = (body or {}).get("token") or {}
    if not (status == 201 and user is not None and _marked(user) and (tok.get("user") or {}).get("id") == user["id"]
            and (tok.get("project") or {}).get("id") == pid):
        raise missing


def _credential_unit(ks, token, uid, name, pid, req):
    """Password set, wait, sign-in as the user, the user's credentials deleted, one restricted credential created, a
    second password set (the used one is dead and its tokens revoked). Retried as a whole on HTTP 401: another job
    reset the same password, or the token was issued in the second of the set."""
    last = None
    for attempt in range(UNIT_ATTEMPTS):
        if attempt:
            time.sleep(random.uniform(*UNIT_RETRY_WAIT))
        used = _password()
        try:
            ks.call(token, "PATCH", f"/users/{uid}", {"user": {"password": used}})
        except KsError as e:
            raise _step_refusal("PasswordRefused", f"the password of user {name}", e)
        time.sleep(PASSWORD_SETTLE_SECONDS)
        status, utok, body = ks.password_token(uid, used, pid)
        if status == 401:
            last = KsError(401, "the user's sign-in was refused")
            continue
        if status != 201:
            raise _step_refusal("PasswordRefused", f"the sign-in of user {name}",
                                KsError(status, scrub(((body or {}).get("error") or {}).get("message") or "")
                                        or f"the site answered HTTP {status}"))
        try:
            mine = ks.call(utok, "GET", f"/users/{uid}/application_credentials").get("application_credentials", [])
            for c in mine:
                ks.call(utok, "DELETE", f"/users/{uid}/application_credentials/{c['id']}", ok=(204, 404))
            made = ks.call(utok, "POST", f"/users/{uid}/application_credentials", {"application_credential": {
                "name": req["credential_name"], "description": req.get("credential_description") or "",
                "unrestricted": False, "roles": [{"name": r} for r in (req.get("roles") or ["member"])]}},
                ok=(201,))["application_credential"]
        except KsError as e:
            if e.status == 401:
                last = e
                continue
            raise _step_refusal("CredentialRefused", f"the credential of user {name}", e)
        # the identity service reports no creation time for an application credential: the helper's own clock
        cred = {"id": made["id"], "secret": _hide(made["secret"]), "createdAt": _utc_now()}
        try:
            ks.call(token, "PATCH", f"/users/{uid}", {"user": {"password": _password()}})
        except KsError as e:
            raise _step_refusal("PasswordRefused", f"the second password of user {name}", e)
        # the next step signs in with the new credential: a token issued in the second of this reset is refused
        time.sleep(PASSWORD_SETTLE_SECONDS)
        return cred
    raise _step_refusal("CredentialRefused", f"the credential of user {name} after {UNIT_ATTEMPTS} attempts",
                        last or KsError(401, "unauthorized"))


def cluster_identity_delete(req):
    """{domain, user_id, user_name} -> {found, deleted, id}: the cluster's user (by id, else by name), only when it is
    the platform's, of that domain and, when both are given, of that name; Keystone deletes its application
    credentials with it."""
    _require(req, "domain")
    if not (str(req.get("user_id") or "").strip() or str(req.get("user_name") or "").strip()):
        raise ValueError("refused: user_id and user_name are empty; the cluster-identity-delete step needs one")
    acct = Account(req)
    ks = Keystone(acct)
    token, _, dom = ks.site(req["domain"])
    user = None
    if req.get("user_id"):
        status, body, _ = ks.request(token, "GET", f"/users/{req['user_id']}")
        user = body.get("user") if status == 200 else None
        if status not in (200, 404):
            raise KsError(status, f"the user {req['user_id']} could not be read (HTTP {status})")
    if user is None and req.get("user_name"):
        user = _find_by_name(ks, token, "users", req["user_name"], dom)
    if user is None:
        return {"found": False, "deleted": False, "id": ""}
    if not _marked(user) or user.get("domain_id") != dom:
        return {"found": True, "deleted": False, "id": user["id"], "kept": "not the platform's"}
    if req.get("user_name") and user.get("name") != req["user_name"]:
        return {"found": True, "deleted": False, "id": user["id"], "kept": "its name is not " + req["user_name"]}
    ks.call(token, "DELETE", f"/users/{user['id']}", ok=(204, 404))
    return {"found": True, "deleted": True, "id": user["id"]}


def leftover_users(req):
    """{domain, project_id} -> {deleted: [{id, name}]}: the platform's users of the domain that hold a role on the
    project (a cluster user a failed teardown left); run before the project goes (its assignments go with it)."""
    _require(req, "domain", "project_id")
    acct = Account(req)
    ks = Keystone(acct)
    token, own, dom = ks.site(req["domain"])
    data = ks.call(token, "GET", f"/role_assignments?scope.project.id={req['project_id']}")
    deleted = []
    for uid in sorted({a["user"]["id"] for a in data.get("role_assignments", []) if "user" in a}):
        if uid == own:
            continue
        status, body, _ = ks.request(token, "GET", f"/users/{uid}")
        user = body.get("user") if status == 200 else None
        if user and _marked(user) and user.get("domain_id") == dom:
            ks.call(token, "DELETE", f"/users/{uid}", ok=(204, 404))
            deleted.append({"id": uid, "name": user.get("name", "")})
    return {"deleted": deleted}


def _mapping_warnings(rules, group_domain, group_domain_id):
    """What in the site's mapping works against tenant sign-in (the mapping is the operator's: only reported)."""
    projects = groups_here = user_id = False
    for rule in rules or []:
        for el in rule.get("local") or []:
            if not isinstance(el, dict):
                continue
            projects = projects or "projects" in el
            if "groups" in el:
                d = el.get("domain") or {}
                if d.get("name") == group_domain or (group_domain_id and d.get("id") == group_domain_id):
                    groups_here = True
            if isinstance(el.get("user"), dict) and el["user"].get("id"):
                user_id = True
    out = []
    if projects:
        out.append("uses a project list")
    if not groups_here:
        out.append(f"does not look groups up in {group_domain}")
    if not user_id:
        out.append("has no stable user id")
    return out


def identity_token(ks, own_token, admin, project_domain, notes):
    """The token for the probe's identity reads: an administrator's own; a domain manager's scoped to the project
    domain (a project-scoped token of a non-admin is refused every identity read)."""
    if admin or not project_domain:
        return own_token
    try:
        return ks.site(project_domain)[0]
    except Exception as e:  # noqa: BLE001 - reported; the reads then say what they could not read
        notes.append(f"the account could not sign in to domain {project_domain}: {type(e).__name__}")
        return own_token


def sign_in_reads(ks, token, user_id, si, notes):
    """The declared sign-in, read where the account may: {identityProvider, protocol, mapping, groupDomain,
    userDomain, account, unreadable}; each part read True/False (False: refused, 403) and found. Only 404 (for the
    group domain: an empty list) means "does not exist"; any other answer (5xx, 401, no connection) is not a result:
    the part is not read and is named in unreadable, so the caller keeps what it found before. A group domain other
    than the project domain (si project_domain) is looked for only by an administrator (si admin): a domain manager's
    list of domains holds its own domain alone. Never writes."""
    out = {"unreadable": []}

    def get(path, part=None):
        try:
            status, body, _ = ks.request(token, "GET", path)
        except Exception as e:  # noqa: BLE001 - the site did not answer: not a result
            status, body = 0, {}
            notes.append(f"{part or 'a sign-in read'} did not answer: {type(e).__name__}")
        if part and status not in (200, 403, 404):
            out["unreadable"].append(f"{part} (HTTP {status})" if status else part)
        return status, (body if isinstance(body, dict) else {})

    idp = urllib.parse.quote(si.get("identity_provider") or "", safe="")
    proto = urllib.parse.quote(si.get("protocol") or "", safe="")
    status, body = get(f"/OS-FEDERATION/identity_providers/{idp}", "identityProvider")
    ip = body.get("identity_provider") or {}
    out["identityProvider"] = {"read": status in (200, 404), "found": status == 200, "status": status,
                               "enabled": bool(ip.get("enabled")), "domainId": ip.get("domain_id") or "",
                               "remoteIds": ip.get("remote_ids") or [],
                               "authorizationTTL": ip.get("authorization_ttl") if status == 200 else None}
    out["protocol"] = {"read": False, "found": False, "mappingId": ""}
    out["mapping"] = {"read": False, "found": False, "warnings": []}
    if status == 200:
        st, b = get(f"/OS-FEDERATION/identity_providers/{idp}/protocols/{proto}", "protocol")
        pr = b.get("protocol") or {}
        out["protocol"] = {"read": st in (200, 404), "found": st == 200, "mappingId": pr.get("mapping_id") or ""}
    # the group domain (a domain manager lists its own domain only: another one reads as an empty list)
    gd = si.get("group_domain") or ""
    if si.get("project_domain") and gd != si["project_domain"] and not si.get("admin"):
        out["groupDomain"] = {"name": gd, "read": False, "found": False, "id": ""}
        notes.append(f"the group domain {gd} is not the project domain: only an administrator account can find it "
                     "and grant its groups roles")
    else:
        st, b = get("/domains?" + urllib.parse.urlencode({"name": gd}), "groupDomain")
        doms = [d for d in (b.get("domains") or []) if d.get("name") == gd] if st == 200 else []
        out["groupDomain"] = {"name": gd, "read": st == 200, "found": bool(doms), "id": doms[0]["id"] if doms else ""}
    if out["protocol"]["found"] and out["protocol"]["mappingId"]:
        st, b = get(f"/OS-FEDERATION/mappings/{urllib.parse.quote(out['protocol']['mappingId'], safe='')}",
                    "mapping")
        mp = b.get("mapping") or {}
        out["mapping"] = {"read": st in (200, 404), "found": st == 200,
                          "warnings": _mapping_warnings(mp.get("rules"), gd, out["groupDomain"]["id"])
                          if st == 200 else []}
    out["userDomain"] = {"id": out["identityProvider"]["domainId"], "name": ""}
    if out["userDomain"]["id"]:
        st, b = get(f"/domains/{out['userDomain']['id']}")
        out["userDomain"]["name"] = (b.get("domain") or {}).get("name", "") if st == 200 else ""
    # the account's own role assignments (what it may do on the site)
    st, b = get(f"/role_assignments?user.id={user_id}&include_names=true")
    roles = []
    if st == 200:
        for a in b.get("role_assignments") or []:
            sc = a.get("scope") or {}
            where = ("system" if "system" in sc else
                     f"domain {(sc.get('domain') or {}).get('name') or (sc.get('domain') or {}).get('id', '')}"
                     if "domain" in sc else
                     f"project {(sc.get('project') or {}).get('name') or (sc.get('project') or {}).get('id', '')}")
            roles.append({"role": (a.get("role") or {}).get("name", ""), "scope": where})
    else:
        notes.append(f"the account's role assignments were not readable (HTTP {st})")
    out["account"] = {"read": st == 200, "roles": sorted(roles, key=lambda r: (r["scope"], r["role"]))}
    # the link environments show: the dashboard, or (start IdentityProvider) the identity service's per-provider
    # sign-in URL that returns to the dashboard's sign-in endpoint; that one answers with a redirect to the identity
    # provider (one unauthenticated GET, never followed)
    dash = (si.get("dashboard_url") or "").rstrip("/")
    url = dash
    if si.get("start") == "IdentityProvider" and dash:
        origin = urllib.parse.quote(dash + "/auth/websso/", safe="")
        url = f"{ks.base}/auth/OS-FEDERATION/identity_providers/{idp}/protocols/{proto}/websso?origin={origin}"
    out["startUrl"] = {"url": url, "checked": False, "redirects": False}
    if url and si.get("start") == "IdentityProvider":
        try:
            r = ks.http.get(url, timeout=15, allow_redirects=False)
            out["startUrl"] = {"url": url, "checked": True, "redirects": r.status_code in (301, 302, 303, 307, 308),
                               "status": r.status_code}
        except Exception as e:  # noqa: BLE001 - a note, never a failure of the probe
            notes.append(f"the sign-in start URL did not answer: {type(e).__name__}")
    return out


ACTIONS = {"provision": provision, "check-empty": check_empty, "teardown": teardown,
           "dns-upsert": dns_upsert, "dns-delete": dns_delete, "site-probe": site_probe,
           "platform-project": platform_project, "placement-network": placement_network,
           "cluster-fips": cluster_fips, "router-advertisement": router_advertisement,
           "network-name-check": network_name_check,
           "platform-inventory": platform_inventory, "project-routers": project_routers,
           "access-groups": access_groups, "cluster-identity": cluster_identity,
           "cluster-identity-delete": cluster_identity_delete, "leftover-users": leftover_users}


def main(stdin=sys.stdin, stdout=sys.stdout):
    request = json.load(stdin)
    try:
        print(scrub(json.dumps(ACTIONS[request["action"]](request))), file=stdout)
        return 0
    except Refusal as e:
        print(scrub(json.dumps({"error": e.message, "reason": e.reason, "message": e.message})), file=stdout)
        return 1
    except Exception as e:  # one line for the job log; the role turns it into status.deletion/message
        # transient: the site did not answer (nothing to refuse; the next run asks again)
        t = transient(e)
        print(scrub(json.dumps({"error": f"{type(e).__name__}: {e}", "transient": t,
                                **({"message": _plain(e)} if t else {})})), file=stdout)
        return 1


if __name__ == "__main__":
    sys.exit(main())
