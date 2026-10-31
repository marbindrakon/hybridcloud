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
                     Optional `attributes` (dict) are set on a zone this call creates (Designate pool
                     scheduling, e.g. {"scope": "public"}); the same for ensure-zone.
  ensure-zone        the environment zone `name` in owner_project_id, shared with share_with (a project id):
                     created when missing, share added when missing. Returns id, NS records, share id.
  delete-zone        deletes zone_id (or `name`) in owner_project_id with its shares, tenant records included.
  ensure-delegation  NS records `name` -> nameservers in the global zone (project_id, zone_id).
  delete-delegation  removes them.

Platform records (reverse DNS of the platform's IPv6 space). Every recordset these write carries the description
`shc:<owner>` (the ownership marker; a long owner is shortened with a hash); a recordset of the same name and type
with another description is never changed or deleted ("held by another owner"). project_id is the project whose zones
the account works in (the DNSConfig's project for the global zone and the reverse zones).
  ensure-record      {project_id, zone (name), name, type, records [], ttl, owner}: the recordset in the zone `zone` of
                     the project, created or updated (records, ttl). Result {changed, id, name, type, zone, records}.
  delete-record      {project_id, zone, name, type, owner}: deletes it when it carries the owner's marker. A missing
                     zone or recordset is no change, and so is one another owner holds (left alone; the result says
                     kept: "held by another owner"). Result {changed, id, name, type}.
  ensure-ptr         {project_id, address, target, ttl, owner}: the PTR of an IPv6 address (full nibble name) -> target,
                     in the project's reverse zone (a zone under ip6.arpa) that covers the address, the longest one;
                     refused with "no platform reverse zone covers <address>" when none does. Result as ensure-record.
  delete-ptr         {project_id, address, owner}: deletes that PTR when owned (no covering zone: no change).
  ptr-name           {address, zones []} (no account used): the PTR name of the address and the longest of the given
                     reverse zones that covers it ("" when none). Result {name, zone}.
"""
import hashlib
import ipaddress
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import openstack  # noqa: E402
from shc_openstack import Account  # noqa: E402


def fqdn(name):
    return name.rstrip(".") + "."


# ---- reverse DNS of IPv6 addresses (pure helpers) ----
REVERSE6 = "ip6.arpa."
DESCRIPTION_MAX = 160  # Designate's limit on a recordset description
NIBBLES = "0123456789abcdef"


def ptr_name(address):
    """The full nibble name of an IPv6 address under ip6.arpa, with the trailing dot."""
    ip = ipaddress.ip_address(str(address).strip())
    if ip.version != 6:
        raise ValueError(f"{address} is not an IPv6 address")
    return ip.reverse_pointer + "."


def ptr_address(name):
    """The IPv6 address of a full nibble name (32 nibbles under ip6.arpa)."""
    net = zone_network(name)
    if net is None or net.prefixlen != 128:
        raise ValueError(f"{name} is not the full nibble name of an IPv6 address")
    return net.network_address


def zone_network(zone):
    """The IPv6 network a reverse zone under ip6.arpa covers (its nibbles, 4 bits each), or None for any other name."""
    labels = fqdn(str(zone).strip().lower()).rstrip(".").split(".")
    if labels[-2:] != ["ip6", "arpa"]:
        return None
    nibbles = labels[:-2]
    if not nibbles or len(nibbles) > 32 or any(len(n) != 1 or n not in NIBBLES for n in nibbles):
        return None
    value = int("".join(reversed(nibbles)).ljust(32, "0"), 16)
    return ipaddress.IPv6Network((value, 4 * len(nibbles)))


def covering_zone(address, zones):
    """The longest of the zone names that covers the IPv6 address (with its trailing dot), or None."""
    ip = ipaddress.ip_address(str(address).strip())
    best, best_len = None, -1
    for z in zones:
        net = zone_network(z)
        if net is not None and ip.version == 6 and ip in net and net.prefixlen > best_len:
            best, best_len = fqdn(str(z).strip().lower()), net.prefixlen
    return best


def owner_description(owner):
    """The ownership marker of a platform recordset: shc:<owner>, shortened with a hash beyond Designate's limit."""
    d = f"shc:{owner}"
    if len(d) > DESCRIPTION_MAX:
        d = d[:DESCRIPTION_MAX - 11] + "~" + hashlib.sha1(str(owner).encode()).hexdigest()[:10]
    return d


def canonical_records(rtype, records):
    out = []
    for r in records or []:
        r = str(r).strip()
        if rtype in ("A", "AAAA"):
            ip = ipaddress.ip_address(r)
            if ip.version != (4 if rtype == "A" else 6):
                raise ValueError(f"{rtype} record {r!r} is not an IPv{4 if rtype == 'A' else 6} address")
            r = str(ip)
        elif rtype in ("PTR", "CNAME", "NS"):
            r = fqdn(r.lower())
        out.append(r)
    if not out:
        raise ValueError("no records")
    return sorted(set(out))


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


def zone_attributes(req):
    """Designate zone attributes from req["attributes"] (pool scheduling, e.g. {"scope": "public"}): a create_zone
    kwarg when set, nothing otherwise (the service picks its default pool)."""
    a = req.get("attributes") or {}
    return {"attributes": {str(k): str(v) for k, v in a.items()}} if a else {}


def ensure_parent(req):
    conn = Account(req).project(req["owner_project_id"])
    z = zone_in_project(conn, req["name"])
    created = False
    if z is None:
        try:
            z = conn.dns.create_zone(name=fqdn(req["name"]), email=req["email"],
                                     description=req.get("description", ""), **zone_attributes(req))
        except openstack.exceptions.HttpException as e:  # 409 duplicate, 403 inside another project's zone
            raise RuntimeError(f"zone {fqdn(req['name'])} is held by another project on this site: {e}")
        created = True
        z = wait_active(conn, z)
    return {"zone_id": z.id, "zone": z.name, "status": z.status, "created": created, "nameservers": ns_records(conn, z),
            "attributes": z.attributes or {}}


def ensure_zone(req):
    conn = Account(req).project(req["owner_project_id"])
    z = zone_in_project(conn, req["name"])
    created = False
    if z is None:
        try:
            z = conn.dns.create_zone(name=fqdn(req["name"]), email=req["email"],
                                     description=req.get("description", ""), **zone_attributes(req))
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
            "nameservers": ns_records(conn, z), "attributes": z.attributes or {}, "share_id": share.id if share else "",
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


# ---- platform records ----
def project_zone(conn, project_id, name):
    """The zone `name` the project owns (zones shared with the project from elsewhere are not its own)."""
    for z in conn.dns.zones(name=fqdn(name)):
        if z.name == fqdn(name) and (getattr(z, "project_id", None) or project_id) == project_id:
            return z
    return None


def reverse_zones(conn, project_id):
    """The project's own zones under ip6.arpa (names)."""
    return [z for z in conn.dns.zones(name="*." + REVERSE6)
            if z.name.endswith("." + REVERSE6) and (getattr(z, "project_id", None) or project_id) == project_id]


def recordset_of(conn, zone, name, rtype):
    for rs in conn.dns.recordsets(zone, name=name, type=rtype):
        if rs.name == name and rs.type == rtype:
            return rs
    return None


def _ensure(conn, zone, name, rtype, records, ttl, owner):
    name = fqdn(name.lower())
    if name != zone.name and not name.endswith("." + zone.name):
        raise RuntimeError(f"{name} is not inside the zone {zone.name}")
    want = canonical_records(rtype, records)
    desc = owner_description(owner)
    out = {"name": name, "type": rtype, "zone": zone.name, "records": want}
    rs = recordset_of(conn, zone, name, rtype)
    if rs is None:
        rs = conn.dns.create_recordset(zone, name=name, type=rtype, records=want, ttl=ttl, description=desc)
        return {**out, "changed": True, "id": rs.id}
    if (rs.description or "") != desc:
        raise RuntimeError(f"{name} {rtype} is held by another owner")
    if canonical_records(rtype, rs.records) != want or rs.ttl != ttl:
        conn.dns.update_recordset(rs, records=want, ttl=ttl)
        return {**out, "changed": True, "id": rs.id}
    return {**out, "changed": False, "id": rs.id}


def _delete(conn, zone, name, rtype, owner):
    name = fqdn(name.lower())
    out = {"name": name, "type": rtype}
    rs = recordset_of(conn, zone, name, rtype) if zone is not None else None
    if rs is None:
        return {**out, "changed": False, "id": ""}
    if (rs.description or "") != owner_description(owner):
        # not this owner's: nothing of it to delete
        return {**out, "changed": False, "id": "", "kept": "held by another owner"}
    conn.dns.delete_recordset(rs, zone, ignore_missing=True)
    return {**out, "changed": True, "id": rs.id}


def _ttl(req):
    return int(req.get("ttl") or 3600)


def ensure_record(req):
    conn = Account(req).project(req["project_id"])
    zone = project_zone(conn, req["project_id"], req["zone"])
    if zone is None:
        raise RuntimeError(f"zone {fqdn(req['zone'])} not found in project {req['project_id']}")
    return _ensure(conn, zone, req["name"], str(req["type"]).upper(), req["records"], _ttl(req), req["owner"])


def delete_record(req):
    conn = Account(req).project(req["project_id"])
    zone = project_zone(conn, req["project_id"], req["zone"])
    return _delete(conn, zone, req["name"], str(req["type"]).upper(), req["owner"])


def _ptr_zone(conn, req):
    zones = {z.name: z for z in reverse_zones(conn, req["project_id"])}
    name = covering_zone(req["address"], zones)
    return ptr_name(req["address"]), (zones[name] if name else None)


def ensure_ptr(req):
    conn = Account(req).project(req["project_id"])
    name, zone = _ptr_zone(conn, req)
    if zone is None:
        raise RuntimeError(f"no platform reverse zone covers {req['address']}")
    return _ensure(conn, zone, name, "PTR", [req["target"]], _ttl(req), req["owner"])


def delete_ptr(req):
    conn = Account(req).project(req["project_id"])
    name, zone = _ptr_zone(conn, req)
    return _delete(conn, zone, name, "PTR", req["owner"])


def ptr_name_action(req):
    return {"name": ptr_name(req["address"]), "zone": covering_zone(req["address"], req.get("zones") or []) or ""}


ACTIONS = {"probe": probe, "ensure-parent": ensure_parent, "ensure-zone": ensure_zone,
           "delete-zone": delete_zone, "ensure-delegation": ensure_delegation,
           "delete-delegation": delete_delegation, "ensure-record": ensure_record,
           "delete-record": delete_record, "ensure-ptr": ensure_ptr, "delete-ptr": delete_ptr,
           "ptr-name": ptr_name_action}

if __name__ == "__main__":
    request = json.load(sys.stdin)
    try:
        print(json.dumps(ACTIONS[request["action"]](request)))
    except Exception as e:  # one line for the job log
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        sys.exit(1)
