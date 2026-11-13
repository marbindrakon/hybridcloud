#!/usr/bin/env python3
"""OpenStack steps of the CloudOSO / PlatformOpenshift jobs (shc LAB-PLAN §11.6 phase C).

Reads one JSON request on stdin and prints one JSON result on stdout; exits non-zero with
{"error": ...} on failure. The account is a clouds.yaml entry: the site's cloud-admin account from its
CloudInfrastructure (P7.1, design/P7-PLAN.md decision 14), or the legacy tenant admin (a password user
with `manager` on the tenant Keystone domain). This script picks the scope per call (domain for
Keystone, the project for its resources), so the account's own scope keys are dropped; site-probe uses
the account's own scope. A private CA comes as request["cacert"] (a file path) or clouds.yaml `cacert`.

Actions (request["action"]):
  provision     project in the domain, `member` for the account, the project's application credential
                (reused by id or recreated); returns every id it touched so teardown can work from the record.
                fip_descriptions (optional, a request of an earlier release): floating IPs found or created by
                description on external_network. Without it no external network is looked up.
  check-empty   lists what is left in a project besides the recorded floating IPs (and DNS
                records under a suffix); teardown refuses to delete a project that isn't empty.
  teardown      deletes the recorded floating IPs, application credentials and the project.
  dns-upsert    creates or updates A records in a Designate zone (scoped to the zone's project).
  dns-delete    deletes recordsets by name/type from a Designate zone; reports what was missing.
  site-probe    CloudInfrastructure probe (cloud admin): authenticates, reports the catalog, Neutron's
                extensions (evpn), the named external network and the site's global external networks (those
                with an access_as_external RBAC policy for every project, target "*"; a network external for some
                projects only is never listed); ensures the tenant project domain (created
                only when asked) with the account's admin role on it, and the site's DNS project (created
                only when asked) with the account's member role on it.
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
import ipaddress
import json
import ssl
import sys
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


def provision(req):
    acct = Account(req)
    conn_d = acct.domain(req["domain"])
    user_id, domain_id, _ = token_info(conn_d)
    project = find_project(conn_d, domain_id, name=req["project"])
    created = project is None
    if created:
        project = conn_d.identity.create_project(
            name=req["project"], domain_id=domain_id, description=req.get("description", ""))
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

    app_cred = None
    name = req["app_cred_name"]
    same_name = list(conn_p.identity.application_credentials(user=user_id, name=name))
    reuse = req.get("reuse_app_cred_id")
    if reuse and any(c.id == reuse for c in same_name):
        app_cred = {"id": reuse, "created": False}
    else:
        for old in same_name:
            conn_p.identity.delete_application_credential(user_id, old.id)
        c = conn_p.identity.create_application_credential(
            user=user_id, name=name, description=req.get("app_cred_description", ""))
        app_cred = {"id": c.id, "secret": c.secret, "created": True}

    zone = None
    if req.get("dns_zone_id"):
        conn_z = acct.project(req["dns_project_id"])
        z = conn_z.dns.get_zone(req["dns_zone_id"])
        zone = {"id": z.id, "name": z.name}

    return {"project": {"id": project.id, "name": project.name, "domain_id": domain_id,
                        "domain_name": req["domain"], "created": created},
            "user_id": user_id, "role": role.name, "fips": fips, "app_cred": app_cred,
            "auth_url": acct.auth.get("auth_url", ""), "region": acct.region, "dns_zone": zone}


def describe(r, *fields):
    parts = [str(getattr(r, f, None)) for f in fields if getattr(r, f, None)]
    return " ".join(parts) or r.id


def resolve_project(conn_d, domain_id, req):
    """The recorded project id, or (provision failed before recording it) the project name."""
    if req.get("project_id"):
        return find_project(conn_d, domain_id, project_id=req["project_id"])
    if req.get("project"):
        return find_project(conn_d, domain_id, name=req["project"])
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
        # Keystone refuses application credential calls on a domain-scoped token, and deletes a
        # project's credentials with the project, so they go here while the project exists.
        creds = set(req.get("app_cred_ids", []))
        if req.get("app_cred_name"):
            creds.update(c.id for c in conn.identity.application_credentials(user=user_id, name=req["app_cred_name"]))
        for cid in sorted(creds):
            conn.identity.delete_application_credential(user_id, cid, ignore_missing=True)
            result["app_creds"].append(cid)
        conn_d.identity.delete_project(project, ignore_missing=True)
        result["project"] = pid
        if find_project(conn_d, domain_id, project_id=pid) is not None:
            raise RuntimeError(f"project {pid} still exists after delete")
    return result


def dns_upsert(req):
    conn = Account(req).project(req["dns_project_id"])
    zone = conn.dns.get_zone(req["dns_zone_id"])
    out = []
    for rec in req["records"]:
        name = rec["name"].rstrip(".") + "."
        if not name.endswith(zone.name):
            raise RuntimeError(f"{name} is outside zone {zone.name}")
        existing = list(conn.dns.recordsets(zone, name=name, type=rec.get("type", "A")))
        if existing and sorted(existing[0].records) == sorted(rec["records"]):
            state, rs = "unchanged", existing[0]
        elif existing:
            state, rs = "updated", conn.dns.update_recordset(existing[0], records=rec["records"])
        else:
            state, rs = "created", conn.dns.create_recordset(zone, name=name, type=rec.get("type", "A"),
                                                             ttl=rec.get("ttl", 300), records=rec["records"])
        out.append({"zoneId": zone.id, "zone": zone.name, "name": name, "type": rs.type, "state": state})
    return {"records": out}


def dns_delete(req):
    conn = Account(req).project(req["dns_project_id"])
    try:
        zone = conn.dns.get_zone(req["dns_zone_id"])
    except openstack.exceptions.NotFoundException:
        return {"deleted": [], "missing": [r["name"] for r in req["records"]], "zone_missing": True}
    deleted, missing = [], []
    for rec in req["records"]:
        name = rec["name"].rstrip(".") + "."
        found = list(conn.dns.recordsets(zone, name=name, type=rec.get("type", "A")))
        for rs in found:
            conn.dns.delete_recordset(rs, zone, ignore_missing=True)
        (deleted if found else missing).append(f"{name} {rec.get('type', 'A')}")
    return {"deleted": deleted, "missing": missing}


def public_volume_types(conn, services, notes):
    """The public Cinder volume types (what any project of the cloud may use), [{name, default}]; read only.
    Private types belong to other consumers of a shared cloud and are never listed."""
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
    except Exception as e:  # noqa: BLE001 - a missing list must not fail the site probe
        notes.append(f"volume types not listed: {type(e).__name__}")
        return []


def global_external_networks(conn, notes):
    """The site's global external networks, [{name, id, families, shared}] sorted by name; read only. Global = an
    access_as_external RBAC policy with target "*" (Neutron writes one when a network's router:external flag is set);
    a network external for some projects only (per-project policies) is never listed. None when the list cannot be
    read (a note says why; the caller keeps what it had)."""
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
    out["volume_types"] = public_volume_types(conn, out["services"], out["notes"])
    if req.get("external_network"):
        net = conn.network.find_network(req["external_network"])
        out["external_network"] = {"found": net is not None,
                                   "external": bool(net and net.is_router_external),
                                   "id": net.id if net else ""}
    nets = global_external_networks(conn, out["notes"])
    if nets is not None:
        out["external_networks"] = nets
    if req.get("project_domain"):
        name = req["project_domain"]
        dom = conn.identity.find_domain(name)
        created = False
        if dom is None and req.get("create_project_domain"):
            dom = conn.identity.create_domain(name=name, description=req.get("domain_description", ""),
                                              is_enabled=True)
            created = True
        if dom is None:
            out["project_domain"] = {"name": name, "found": False}
        else:
            admin = conn.identity.find_role("admin", ignore_missing=False)
            granted = False
            if not conn.identity.validate_user_has_domain_role(dom, access.user_id, admin):
                conn.identity.assign_domain_role_to_user(dom, access.user_id, admin)
                granted = True
            out["project_domain"] = {"name": name, "found": True, "id": dom.id, "enabled": dom.is_enabled,
                                     "created": created, "admin_granted": granted}
    if req.get("dns_project"):
        # the site's DNS project (tenant DNS, design/TENANT-DNS.md): owns the platform's parent zone and the
        # environment zones; the account needs `member` there to create and share them
        dp = req["dns_project"]
        dom = conn.identity.find_domain(dp["domain"], ignore_missing=False)
        proj = conn.identity.find_project(dp["name"], domain_id=dom.id)
        created = False
        if proj is None and dp.get("create"):
            proj = conn.identity.create_project(name=dp["name"], domain_id=dom.id,
                                                description=dp.get("description", ""))
            created = True
        if proj is None:
            out["dns_project"] = {"name": dp["name"], "found": False}
        else:
            member = conn.identity.find_role("member", ignore_missing=False)
            granted = False
            if not conn.identity.validate_user_has_project_role(proj, access.user_id, member):
                conn.identity.assign_project_role_to_user(proj, access.user_id, member)
                granted = True
            quota = None
            if dp.get("zone_quota"):
                # every environment zone counts against the DNS project's zone quota (Designate default 10)
                cur = conn.dns.get_quota(proj.id)
                quota = {"zones": cur.zones, "raised": False}
                if (cur.zones or 0) < int(dp["zone_quota"]):
                    conn.dns.update_quota(proj.id, zones=int(dp["zone_quota"]))
                    quota = {"zones": int(dp["zone_quota"]), "raised": True}
            out["dns_project"] = {"name": dp["name"], "found": True, "id": proj.id, "created": created,
                                  "member_granted": granted, "zone_quota": quota}
    return out


def platform_project(req):
    """The account's own scope (cloud admin): ensure or remove a platform-owned project."""
    acct = Account(req)
    conn = acct.own_scope()
    access = conn.session.auth.get_access(conn.session)
    dom = conn.identity.find_domain(req["domain"], ignore_missing=False)
    proj = conn.identity.find_project(req["name"], domain_id=dom.id)
    if req.get("state", "present") == "absent":
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
    created = False
    if proj is None:
        proj = conn.identity.create_project(name=req["name"], domain_id=dom.id,
                                            description=req.get("description", ""))
        created = True
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
    net = next(iter(conn.network.networks(name=name, project_id=proj.id)), None)
    router = next(iter(conn.network.routers(name=name, project_id=proj.id)), None)
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
    if net is None:
        net = conn.network.create_network(name=name, project_id=proj.id, mtu=int(req["mtu"]),
                                          description=req.get("description", ""))
        created.append("network")
    elif int(req["mtu"]) != net.mtu:
        net = conn.network.update_network(net, mtu=int(req["mtu"]))
        changed.append("mtu")
    if tag not in (net.tags or []):
        conn.network.set_tags(net, sorted(set((net.tags or []) + [tag])))
    have = {sn.cidr: sn for sn in conn.network.subnets(network_id=net.id)}
    mode, resolvers = req.get("addressMode") or "", req.get("resolvers") or []
    subnets, gateways, applied_mode = {}, {}, ""
    for i, cidr in enumerate(req["prefixes"]):
        family = 6 if ":" in cidr else 4
        dns = _family_resolvers(resolvers, family)
        sn = have.get(cidr)
        if sn is None:
            # the RA and address modes are fixed at creation (Neutron can't change them on a subnet)
            v6 = {"ipv6_ra_mode": mode, "ipv6_address_mode": mode} if family == 6 and mode else {}
            sn = conn.network.create_subnet(name=f"{name}-{i}", network_id=net.id, project_id=proj.id,
                                            ip_version=family, cidr=cidr, enable_dhcp=True, dns_nameservers=dns, **v6)
            created.append(f"subnet {cidr}")
        elif _canon(sn.dns_nameservers or []) != _canon(dns):
            sn = conn.network.update_subnet(sn, dns_nameservers=dns)
            changed.append(f"subnet {cidr} resolvers")
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
        r = conn.network.post("/routers", json={"router": {"name": name, "project_id": proj.id,
                                                           "evpn_vni": int(req["l3Vni"])}})
        if r.status_code >= 300:
            raise RuntimeError(f"router create: HTTP {r.status_code} {r.text[:300]}")
        router = conn.network.get_router(r.json()["router"]["id"])
        created.append("router")
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
    return {"name": name, "project_id": scoped, "visible": visible,
            "unique": len(ids) == 1 and (not want or ids[0] == want),
            "external": bool(mine) and mine[0]["external"]}


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
        routers.append({"id": r.id, "name": r.name, "evpnVni": vni})
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


ACTIONS = {"provision": provision, "check-empty": check_empty, "teardown": teardown,
           "dns-upsert": dns_upsert, "dns-delete": dns_delete, "site-probe": site_probe,
           "platform-project": platform_project, "placement-network": placement_network,
           "cluster-fips": cluster_fips, "router-advertisement": router_advertisement,
           "network-name-check": network_name_check,
           "platform-inventory": platform_inventory, "project-routers": project_routers}

if __name__ == "__main__":
    request = json.load(sys.stdin)
    try:
        print(json.dumps(ACTIONS[request["action"]](request)))
    except Exception as e:  # one line for the job log; the role turns it into status.deletion/message
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        sys.exit(1)
