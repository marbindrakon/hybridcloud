#!/usr/bin/env python3
"""Tenant DNS steps (shc design/TENANT-DNS.md D1). One JSON request on stdin, one JSON result on stdout;
exits non-zero with {"error": ...} on failure, like shc_openstack.py (whose Account it uses).

Model: one global tenant domain (the DNSConfig); every environment gets the zone
<environment>.<entity>.<domain>. On an OpenStack site with dns.mode native the zone lives in the site's
Designate, owned by the site's DNS project (which also holds the parent zone = the global domain, reserving the
namespace there) and shared with the environment's project; the tenant manages its own recordsets with the
site's normal tools. An environment zone served by other nameservers than the global zone is delegated from it
with NS records. Backend: designate (the interface leaves room for rfc2136/route53).

The account is a clouds.yaml (+ cacert): the site's cloud admin for zones on that site (it holds `member` on
the DNS project), the DNSConfig's DNS-only account for the global zone.

Actions (request["action"]):
  probe              the global zone (project_id, zone_id): name, status, NS records.
  ensure-parent      the parent zone `name` in owner_project_id: present, or created there. Fails when the
                     name is held by another project (then the namespace isn't reserved on this site).
  ensure-zone        the environment zone `name` in owner_project_id, shared with share_with (a project id):
                     created when missing, share added when missing. Returns id, NS records, share id.
  delete-zone        deletes zone_id (or `name`) in owner_project_id with its shares, tenant records included.
  ensure-delegation  NS records `name` -> nameservers in the global zone (project_id, zone_id).
  delete-delegation  removes them.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import openstack  # noqa: E402
from shc_openstack import Account  # noqa: E402


def fqdn(name):
    return name.rstrip(".") + "."


def ns_records(conn, zone):
    return sorted(r for rs in conn.dns.recordsets(zone, type="NS", name=zone.name) for r in rs.records)


def zone_in_project(conn, name):
    for z in conn.dns.zones(name=fqdn(name)):
        if z.name == fqdn(name):
            return z
    return None


def wait_active(conn, zone, timeout=60):
    end = time.time() + timeout
    z = conn.dns.get_zone(zone)
    while z.status == "PENDING" and time.time() < end:
        time.sleep(3)
        z = conn.dns.get_zone(zone)
    return z


def probe(req):
    conn = Account(req).project(req["project_id"])
    z = conn.dns.get_zone(req["zone_id"])
    return {"zone": z.name, "status": z.status, "nameservers": ns_records(conn, z), "project_id": req["project_id"]}


def ensure_parent(req):
    conn = Account(req).project(req["owner_project_id"])
    z = zone_in_project(conn, req["name"])
    created = False
    if z is None:
        try:
            z = conn.dns.create_zone(name=fqdn(req["name"]), email=req["email"],
                                     description=req.get("description", ""))
        except openstack.exceptions.HttpException as e:  # 409 duplicate, 403 inside another project's zone
            raise RuntimeError(f"zone {fqdn(req['name'])} is held by another project on this site: {e}")
        created = True
        z = wait_active(conn, z)
    return {"zone_id": z.id, "zone": z.name, "status": z.status, "created": created, "nameservers": ns_records(conn, z)}


def ensure_zone(req):
    conn = Account(req).project(req["owner_project_id"])
    z = zone_in_project(conn, req["name"])
    created = False
    if z is None:
        try:
            z = conn.dns.create_zone(name=fqdn(req["name"]), email=req["email"],
                                     description=req.get("description", ""))
        except openstack.exceptions.HttpException as e:  # 409 duplicate, 403 inside another project's zone
            raise RuntimeError(f"zone {fqdn(req['name'])} is held by another project on this site: {e}")
        created = True
    z = wait_active(conn, z)
    share = None
    if req.get("share_with"):
        for s in conn.dns.zone_shares(z):
            if s.target_project_id == req["share_with"]:
                share = s
                break
        if share is None:
            share = conn.dns.create_zone_share(z, target_project_id=req["share_with"])
    return {"zone_id": z.id, "zone": z.name, "status": z.status, "created": created,
            "nameservers": ns_records(conn, z), "share_id": share.id if share else "",
            "shared_with": req.get("share_with", "")}


def delete_zone(req):
    conn = Account(req).project(req["owner_project_id"])
    z = None
    if req.get("zone_id"):
        try:
            z = conn.dns.get_zone(req["zone_id"])
        except openstack.exceptions.NotFoundException:
            z = None
    elif req.get("name"):
        z = zone_in_project(conn, req["name"])
    if z is None:
        return {"deleted": False, "missing": True}
    conn.dns.delete_zone(z, ignore_missing=True, delete_shares=True)
    return {"deleted": True, "zone_id": z.id, "zone": z.name}


def ensure_delegation(req):
    conn = Account(req).project(req["project_id"])
    zone = conn.dns.get_zone(req["zone_id"])
    name = fqdn(req["name"])
    if not name.endswith("." + zone.name):
        raise RuntimeError(f"{name} is not below the global zone {zone.name}")
    want = sorted(fqdn(n) for n in req["nameservers"])
    existing = list(conn.dns.recordsets(zone, name=name, type="NS"))
    if existing and sorted(existing[0].records) == want:
        return {"state": "unchanged", "name": name, "nameservers": want}
    if existing:
        conn.dns.update_recordset(existing[0], records=want)
        return {"state": "updated", "name": name, "nameservers": want}
    conn.dns.create_recordset(zone, name=name, type="NS", ttl=req.get("ttl", 3600), records=want,
                              description=req.get("description", ""))
    return {"state": "created", "name": name, "nameservers": want}


def delete_delegation(req):
    conn = Account(req).project(req["project_id"])
    zone = conn.dns.get_zone(req["zone_id"])
    name = fqdn(req["name"])
    found = list(conn.dns.recordsets(zone, name=name, type="NS"))
    for rs in found:
        conn.dns.delete_recordset(rs, zone, ignore_missing=True)
    return {"deleted": bool(found), "name": name}


ACTIONS = {"probe": probe, "ensure-parent": ensure_parent, "ensure-zone": ensure_zone,
           "delete-zone": delete_zone, "ensure-delegation": ensure_delegation,
           "delete-delegation": delete_delegation}

if __name__ == "__main__":
    request = json.load(sys.stdin)
    try:
        print(json.dumps(ACTIONS[request["action"]](request)))
    except Exception as e:  # one line for the job log
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        sys.exit(1)
