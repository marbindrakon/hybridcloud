#!/usr/bin/env python3
"""OpenStack steps of the CloudOSO / PlatformOpenshift jobs (shc LAB-PLAN §11.6 phase C).

Reads one JSON request on stdin and prints one JSON result on stdout; exits non-zero with
{"error": ...} on failure. The account is a clouds.yaml entry: the site's cloud-admin account from its
CloudInfrastructure (P7.1, design/P7-PLAN.md decision 14), or the legacy tenant admin (a password user
with `manager` on the tenant Keystone domain). This script picks the scope per call (domain for
Keystone, the project for its resources), so the account's own scope keys are dropped; site-probe uses
the account's own scope. A private CA comes as request["cacert"] (a file path) or clouds.yaml `cacert`.

Actions (request["action"]):
  provision     project in the domain, `member` for the account, API/ingress floating IPs found or
                created by description, the project's application credential (reused by id or
                recreated); returns every id it touched so teardown can work from the record.
  check-empty   lists what is left in a project besides the recorded floating IPs (and DNS
                records under a suffix); teardown refuses to delete a project that isn't empty.
  teardown      deletes the recorded floating IPs, application credentials and the project.
  dns-upsert    creates or updates A records in a Designate zone (scoped to the zone's project).
  dns-delete    deletes recordsets by name/type from a Designate zone; reports what was missing.
  site-probe    CloudInfrastructure probe (cloud admin): authenticates, reports the catalog, Neutron's
                extensions (evpn) and the external network; ensures the tenant project domain (created
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
                interface per network), and the network shared to the tenant project (RBAC access_as_shared).
                state absent removes them in order (interfaces, router, subnets, network), refused while other
                ports (the tenant's VMs) use the network.
                recreateRouter: delete and recreate the router (the site fills its gateway chassis at creation).
  platform-inventory  read only: the networks (with their RBAC shares), routers (with evpn_vni) and ports of other
                owners in the platform project, for the fabric orphan check.
"""
import ipaddress
import json
import sys

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

    def connect(self, **scope):
        return openstack.connect(auth={**self.auth, **scope}, region_name=self.region,
                                 interface=self.interface, verify=self.verify,
                                 identity_api_version="3")

    def own_scope(self):
        return openstack.connect(auth=self.own_auth, region_name=self.region, interface=self.interface,
                                 verify=self.verify, identity_api_version="3")

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
    net = conn_p.network.find_network(req["external_network"], ignore_missing=False)
    fips = {}
    for key, desc in req["fip_descriptions"].items():
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


def placement_network(req):
    """The cloud admin's own scope: a placement's network, subnets, EVPN router and share, in the platform project.
    prefixes carries both families (one subnet each); addressMode ("" = the site's default) is the IPv6 subnets' RA
    and address mode; resolvers (addresses of either family) become each subnet's dns_nameservers by family."""
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
            busy = [f"port {describe(p, 'name', 'device_owner')}" for p in conn.network.ports(network_id=net.id)
                    if not (p.device_owner or "").startswith("network:")]
            if busy:
                return {**out, "blocked": busy}
            for r in conn.network.rbac_policies(object_id=net.id):
                conn.network.delete_rbac_policy(r)
                out["deleted"].append(f"rbac {r.id}")
        if router is not None:
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
    subnets, applied_mode = {}, ""
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
    rbac = None
    if req.get("shareProjectId"):
        for rp in conn.network.rbac_policies(object_id=net.id, action="access_as_shared"):
            if rp.target_project_id == req["shareProjectId"]:
                rbac = rp
        if rbac is None:
            rbac = conn.network.create_rbac_policy(object_type="network", object_id=net.id, action="access_as_shared",
                                                   target_project_id=req["shareProjectId"])
            created.append("share")
        for rp in conn.network.rbac_policies(object_id=net.id, action="access_as_shared"):
            if rp.target_project_id != req["shareProjectId"]:
                conn.network.delete_rbac_policy(rp)
                changed.append(f"share to {rp.target_project_id} removed")
    evpn = conn.network.get(f"/routers/{router.id}").json()["router"].get("evpn_vni")
    return {"name": name, "projectId": proj.id, "networkId": net.id, "routerId": router.id, "subnets": subnets,
            "addressMode": applied_mode, "evpnVni": evpn, "shareId": rbac.id if rbac else "", "mtu": net.mtu,
            "created": created, "changed": changed}


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


ACTIONS = {"provision": provision, "check-empty": check_empty, "teardown": teardown,
           "dns-upsert": dns_upsert, "dns-delete": dns_delete, "site-probe": site_probe,
           "platform-project": platform_project, "placement-network": placement_network,
           "platform-inventory": platform_inventory}

if __name__ == "__main__":
    request = json.load(sys.stdin)
    try:
        print(json.dumps(ACTIONS[request["action"]](request)))
    except Exception as e:  # one line for the job log; the role turns it into status.deletion/message
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        sys.exit(1)
