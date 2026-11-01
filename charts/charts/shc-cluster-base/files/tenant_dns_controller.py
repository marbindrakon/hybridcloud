#!/usr/bin/env python3
"""Tenant DNS controller (shc design/TENANT-DNS.md D3).

Tenants of a CloudVirt environment publish DNS names in the environment's zone <cloudvirt>.<entity>.<domain>, which
the platform holds in its DNS project (Designate), with Kubernetes objects in their VM namespaces:

  - DNSEndpoint (externaldns.k8s.io/v1alpha1): spec.endpoints[] {dnsName, recordType, targets, recordTTL};
  - Services and Routes annotated external-dns.alpha.kubernetes.io/hostname (comma-separated names), with the
    targets from external-dns.alpha.kubernetes.io/target, else the Service's load balancer ingress or the Route's
    router canonical hostname (CNAME); optional external-dns.alpha.kubernetes.io/ttl.

Trust comes only from what the platform sets: the namespaces labelled hybridsovereign.redhat/cloudvirt, and their
annotation hybridsovereign.redhat/dns-zone (the environment zone, written by the CloudVirt job). Tenants can't
change namespaces. A name outside the namespace's zone, a zone that isn't exactly zoneDepth labels below the
domain, an unsupported type or a name another object holds is refused: the object gets the annotation
hybridsovereign.redhat/dns-status and an Event.

Ownership: every recordset the controller writes carries the description "shc-tenant-dns cluster=<cluster>
owner=<namespace>/<Kind>/<name>". It changes and deletes only recordsets with its own cluster's marker (never
NS/SOA), so platform records and other clusters' records are left alone. A record goes away when its object, its
name in the object, or the namespace's label goes. Deleting the environment deletes the zone with everything in
it (CloudVirt teardown).

Reverse names (forward-confirmed PTRs): the platform writes the namespace annotation hybridsovereign.redhat/ipv6-prefixes
(comma-separated IPv6 prefixes of the placements that serve the namespace), the same trust model as the zone
annotation. Every address of an AAAA the controller publishes for an object in that namespace that lies inside one of
those prefixes gets a PTR to the AAAA's name, in the covering reverse zone (*.ip6.arpa.) of the controller's DNS
project (the longest zone that contains the address's reverse name), with the AAAA's ownership description. An address
outside the prefixes, or one no reverse zone covers, gets no PTR (the object's status says so; the AAAA is still
published). A PTR goes with its AAAA (object, name, address or annotation gone) and in the sweep of owned records the
controller no longer wants. Tenants never write PTRs themselves.

Runs level-triggered: watches namespaces, DNSEndpoints, Services and Routes, and reconciles everything a few
seconds after a change, and every resyncSeconds (repairs records changed in Designate). After every round it writes
its health to the ConfigMap tenant-dns-status in its own namespace (lastSuccess, lastError, lastErrorAt, refused,
ptrs), read by the platform's site job, and a Warning Event DNSReconcileFailed on that ConfigMap once per distinct
error.
"""
import argparse
import datetime
import hashlib
import ipaddress
import re
import sys
import threading
import time

import openstack
import yaml
from openstack.dns.v2.recordset import Recordset
from kubernetes import client, config, watch

NS_LABEL = "hybridsovereign.redhat/cloudvirt"
ZONE_ANNOTATION = "hybridsovereign.redhat/dns-zone"
STATUS_ANNOTATION = "hybridsovereign.redhat/dns-status"
PREFIXES_ANNOTATION = "hybridsovereign.redhat/ipv6-prefixes"
REVERSE_SUFFIX = ".ip6.arpa."
STATUS_CONFIGMAP = "tenant-dns-status"
SA_NAMESPACE_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
HOSTNAME_ANNOTATION = "external-dns.alpha.kubernetes.io/hostname"
TARGET_ANNOTATION = "external-dns.alpha.kubernetes.io/target"
TTL_ANNOTATION = "external-dns.alpha.kubernetes.io/ttl"
MARKER = "shc-tenant-dns"
DESCRIPTION_MAX = 160
HOST_RE = re.compile(r"^(\*\.)?([a-z0-9_]([-a-z0-9_]{0,61}[a-z0-9_])?\.)*[a-z0-9_]([-a-z0-9_]{0,61}[a-z0-9_])?$")
DEP = ("externaldns.k8s.io", "v1alpha1", "dnsendpoints")
ROUTE = ("route.openshift.io", "v1", "routes")


def log(msg):
    print(f"{datetime.datetime.now(datetime.timezone.utc):%Y-%m-%dT%H:%M:%SZ} {msg}", flush=True)


def fqdn(name):
    return name.strip().lower().rstrip(".") + "."


def is_host(name):
    return len(name) <= 253 and bool(HOST_RE.match(name.rstrip(".")))


def rfc3339(t=None):
    return (t or datetime.datetime.now(datetime.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def reverse_name(address):
    """The ip6.arpa name of an IPv6 address (fully qualified)."""
    return ipaddress.ip_address(address).reverse_pointer + "."


def covering_zone(name, zone_names):
    """The longest zone of zone_names that contains the name (equal to it or a parent of it), else None."""
    best = None
    for z in zone_names:
        if (name == z or name.endswith("." + z)) and (best is None or len(z) > len(best)):
            best = z
    return best


class Refused(Exception):
    pass


class Controller:
    def __init__(self, cfg, clouds):
        self.cluster = cfg["cluster"]
        self.domain = fqdn(cfg["domain"])
        self.depth = int(cfg.get("zoneDepth", 2))
        self.ttl = int(cfg.get("defaultTTL", 300))
        # PTRs are the controller's own (forward-confirmed, in the platform's reverse zones), never a tenant's
        self.types = set(cfg.get("types") or ["A", "AAAA", "CNAME", "TXT", "MX", "SRV", "CAA"]) - {"NS", "SOA", "PTR"}
        self.resync = int(cfg.get("resyncSeconds", 300))
        self.prefix = f"{MARKER} cluster={self.cluster} "
        with open(clouds) as f:
            self.cloud = yaml.safe_load(f)["clouds"]["openstack"]
        self.conn = None
        self.core = client.CoreV1Api()
        self.custom = client.CustomObjectsApi()
        self.trigger = threading.Event()
        self.tenant_ns = set()
        self.health = cfg.get("healthFile", "/tmp/healthy")
        self.namespace = cfg.get("namespace") or self._own_namespace()
        self.reported_error = None  # the error of the last DNSReconcileFailed event (one event per distinct error)
        self.bad_prefixes = set()  # annotation values already logged as unusable
        self.counts = {"refused": 0, "ptrs": 0}

    @staticmethod
    def _own_namespace():
        try:
            with open(SA_NAMESPACE_FILE) as f:
                return f.read().strip()
        except OSError:
            return ""

    # ── OpenStack ──────────────────────────────────────────────────────────────────────────────────────────────
    def dns(self):
        if self.conn is None:
            c = self.cloud
            self.conn = openstack.connect(auth=c["auth"], region_name=c.get("region_name"),
                                          interface=c.get("interface", "public"), identity_api_version="3")
        return self.conn

    def owned(self, conn):
        """This cluster's recordsets in all the project's zones: one filtered listing (GET /v2/recordsets; the SDK's
        recordsets() without a zone builds a per-zone URL in some releases)."""
        url, params = "/recordsets", {"description": self.prefix + "*", "limit": 500}
        while url:
            r = conn.dns.get(url, params=params)
            openstack.exceptions.raise_from_response(r)
            body = r.json()
            for d in body.get("recordsets") or []:
                yield Recordset.existing(connection=conn, **d)
            url, params = (body.get("links") or {}).get("next"), None

    def description(self, owner):
        d = f"{self.prefix}owner={owner}"
        if len(d) > DESCRIPTION_MAX:
            h = hashlib.sha1(owner.encode()).hexdigest()[:10]
            d = d[:DESCRIPTION_MAX - 11] + "~" + h
        return d

    # ── what the tenants asked for ─────────────────────────────────────────────────────────────────────────────
    def zone_of(self, ns):
        """The namespace's environment zone, or a reason it has none."""
        zone = ((ns.metadata.annotations or {}).get(ZONE_ANNOTATION) or "").strip()
        if not zone:
            return None, "the namespace has no environment zone (no DNSConfig when the environment was created)"
        zone = fqdn(zone)
        rel = zone[:-len(self.domain)] if zone.endswith("." + self.domain) else ""
        if not rel or len(rel.rstrip(".").split(".")) != self.depth or not is_host(zone):
            return None, f"the namespace's zone {zone} is not an environment zone of {self.domain}"
        return zone, None

    def prefixes_of(self, ns):
        """The namespace's placement IPv6 prefixes (platform-set annotation), or None when it has no annotation.
        Entries that are not IPv6 prefixes are skipped (logged once)."""
        raw = (ns.metadata.annotations or {}).get(PREFIXES_ANNOTATION)
        if raw is None or not raw.strip():
            return None
        out = []
        for p in raw.split(","):
            p = p.strip()
            if not p:
                continue
            try:
                net = ipaddress.ip_network(p, strict=False)
                if net.version != 6:
                    raise ValueError("not IPv6")
                out.append(net)
            except ValueError as e:
                if (ns.metadata.name, p) not in self.bad_prefixes:
                    self.bad_prefixes.add((ns.metadata.name, p))
                    log(f"namespace {ns.metadata.name}: {PREFIXES_ANNOTATION} entry {p!r} ignored ({e})")
        return out

    def records(self, rtype, targets):
        """Designate record data for a type; raises Refused."""
        if not targets:
            raise Refused("no targets")
        out = []
        for t in targets:
            t = str(t).strip()
            if rtype in ("A", "AAAA"):
                if not self._ip(t) or ipaddress.ip_address(t).version != (4 if rtype == "A" else 6):
                    raise Refused(f"{rtype} target {t!r} is not an IPv{4 if rtype == 'A' else 6} address")
                out.append(str(ipaddress.ip_address(t)))
            elif rtype == "CNAME":
                if len(targets) != 1 or not is_host(t.lower()):
                    raise Refused("CNAME needs exactly one host name target")
                out.append(fqdn(t))
            elif rtype == "TXT":
                if len(t) > 255:
                    raise Refused("TXT target longer than 255 characters")
                out.append(t if t.startswith('"') and t.endswith('"') and len(t) > 1 else '"' + t.replace('"', '\\"') + '"')
            elif rtype in ("MX", "SRV"):
                parts = t.split()
                n = 2 if rtype == "MX" else 4
                if len(parts) != n or not all(p.isdigit() for p in parts[:-1]) or not is_host(parts[-1].lower()):
                    raise Refused(f"{rtype} target {t!r} is not '{'priority host' if n == 2 else 'priority weight port host'}'")
                out.append(" ".join(parts[:-1] + [fqdn(parts[-1])]))
            elif rtype == "CAA":
                parts = t.split(None, 2)
                if len(parts) != 3 or not parts[0].isdigit():
                    raise Refused(f"CAA target {t!r} is not 'flags tag value'")
                out.append(t)
        return sorted(set(out))

    @staticmethod
    def _ip(t):
        try:
            ipaddress.ip_address(t)
            return True
        except ValueError:
            return False

    def by_targets(self, targets):
        """Record types for a list of targets without a type (annotations): A/AAAA for addresses, CNAME for one name."""
        v4 = [t for t in targets if self._ip(t) and ipaddress.ip_address(t).version == 4]
        v6 = [t for t in targets if self._ip(t) and ipaddress.ip_address(t).version == 6]
        names = [t for t in targets if not self._ip(t)]
        if names and (v4 or v6 or len(names) > 1):
            raise Refused("targets mix addresses and names, or name more than one host (CNAME takes one)")
        out = []
        if v4:
            out.append(("A", v4))
        if v6:
            out.append(("AAAA", v6))
        if names:
            out.append(("CNAME", names))
        return out

    def ttl_of(self, value):
        try:
            v = int(value)
        except (TypeError, ValueError):
            return self.ttl
        return v if 30 <= v <= 86400 else self.ttl

    def wanted(self, kind, obj):
        """[(name, type, targets, ttl)] for an object."""
        ann = obj["metadata"].get("annotations") or {}
        if kind == "DNSEndpoint":
            out = []
            for ep in (obj.get("spec") or {}).get("endpoints") or []:
                out.append((ep.get("dnsName") or "", (ep.get("recordType") or "A").upper(), ep.get("targets") or [],
                            self.ttl_of(ep.get("recordTTL"))))
            return out
        names = [n.strip() for n in (ann.get(HOSTNAME_ANNOTATION) or "").split(",") if n.strip()]
        targets = [t.strip() for t in (ann.get(TARGET_ANNOTATION) or "").split(",") if t.strip()]
        if not targets and kind == "Service":
            for ing in ((obj.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []:
                targets.append(ing.get("ip") or ing.get("hostname") or "")
            targets = [t for t in targets if t]
        if not targets and kind == "Route":
            for ing in (obj.get("status") or {}).get("ingress") or []:
                admitted = any(c.get("type") == "Admitted" and c.get("status") == "True"
                               for c in ing.get("conditions") or [])
                if admitted and ing.get("routerCanonicalHostname"):
                    targets.append(ing["routerCanonicalHostname"])
            targets = sorted(set(targets))[:1]
        if names and not targets:
            raise Refused("no target yet (" + ("load balancer ingress" if kind == "Service" else "admitted route")
                          + f" or {TARGET_ANNOTATION})")
        ttl = self.ttl_of(ann.get(TTL_ANNOTATION))
        return [(n, rtype, ts, ttl) for n in names for rtype, ts in self.by_targets(targets)]

    def objects(self, namespaces):
        """Annotated Services/Routes and DNSEndpoints in the given namespaces: [(kind, obj)]."""
        out = []
        try:
            for o in self.custom.list_cluster_custom_object(*DEP).get("items", []):
                if o["metadata"]["namespace"] in namespaces:
                    out.append(("DNSEndpoint", o))
        except client.ApiException as e:
            if e.status != 404:  # no CRD yet: nothing to publish from it
                raise
        for s in self.core.list_service_for_all_namespaces().items:
            if s.metadata.namespace in namespaces and HOSTNAME_ANNOTATION in (s.metadata.annotations or {}):
                out.append(("Service", client.ApiClient().sanitize_for_serialization(s)))
        try:
            for r in self.custom.list_cluster_custom_object(*ROUTE).get("items", []):
                if r["metadata"]["namespace"] in namespaces and HOSTNAME_ANNOTATION in (r["metadata"].get("annotations") or {}):
                    out.append(("Route", r))
        except client.ApiException as e:
            if e.status != 404:
                raise
        return out

    # ── reconcile ──────────────────────────────────────────────────────────────────────────────────────────────
    def reconcile(self):
        zones_by_ns, ns_problem, prefixes_by_ns = {}, {}, {}
        for ns in self.core.list_namespace(label_selector=NS_LABEL).items:
            zone, problem = self.zone_of(ns)
            if zone:
                zones_by_ns[ns.metadata.name] = zone
                prefixes_by_ns[ns.metadata.name] = self.prefixes_of(ns)
            else:
                ns_problem[ns.metadata.name] = problem
        self.tenant_ns = set(zones_by_ns) | set(ns_problem)
        objs = self.objects(self.tenant_ns)

        conn = self.dns()
        zones = {z.name: z for z in conn.dns.zones()}  # one listing per round: forward and reverse zones
        reverse_zones = [n for n in zones if n.endswith(REVERSE_SUFFIX)]
        actual = {}
        for rs in self.owned(conn):
            if rs.type in ("NS", "SOA") or not (rs.description or "").startswith(self.prefix):
                continue
            actual[(rs.name, rs.type)] = rs
        owner_of = {k: rs.description for k, rs in actual.items()}

        # per object: its records, or why not
        results, claims = {}, []
        for kind, o in objs:
            m = o["metadata"]
            key = f"{m['namespace']}/{kind}/{m['name']}"
            results[key] = {"kind": kind, "obj": o, "records": [], "refused": [], "notes": []}
            if m["namespace"] in ns_problem:
                results[key]["refused"].append(ns_problem[m["namespace"]])
                continue
            zone = zones_by_ns[m["namespace"]]
            try:
                wanted = self.wanted(kind, o)
            except Refused as e:
                results[key]["refused"].append(str(e))
                continue
            seen = set()
            for name, rtype, targets, ttl in wanted:
                try:
                    name = fqdn(name)
                    if not is_host(name):
                        raise Refused(f"{name} is not a valid DNS name")
                    if name != zone and not name.endswith("." + zone):
                        raise Refused(f"{name} is outside this environment's zone {zone}")
                    if rtype not in self.types:
                        raise Refused(f"record type {rtype} is not allowed ({', '.join(sorted(self.types))})")
                    if rtype == "CNAME" and name == zone:
                        raise Refused("a CNAME can't be at the zone apex")
                    if (name, rtype) in seen:
                        raise Refused(f"{name} {rtype} appears twice")
                    seen.add((name, rtype))
                    data = self.records(rtype, targets)
                except Refused as e:
                    results[key]["refused"].append(str(e))
                    continue
                claims.append({"key": key, "zone": zone, "name": name, "type": rtype, "records": data, "ttl": ttl,
                               "description": self.description(key), "namespace": m["namespace"],
                               "created": m.get("creationTimestamp") or ""})

        # one owner per name and type: the current owner keeps it, then the oldest object
        claims.sort(key=lambda c: (owner_of.get((c["name"], c["type"])) != c["description"], c["created"], c["key"]))
        desired = {}
        for c in claims:
            k = (c["name"], c["type"])
            if k in desired:
                results[c["key"]]["refused"].append(f"{c['name']} {c['type']} is held by {desired[k]['key']}")
                continue
            desired[k] = c

        # apply the tenants' records; an AAAA counts for its PTRs only once it is published
        published = [c for c in desired.values() if self.apply(conn, zones, actual, c, results[c["key"]])]

        # forward-confirmed PTRs: the published AAAA addresses inside the namespace's placement prefixes
        ptr_claims = []
        for c in published:
            if c["type"] != "AAAA":
                continue
            prefixes = prefixes_by_ns.get(c["namespace"])
            if prefixes is None:  # the namespace has no placement prefixes: no reverse names there
                continue
            for addr in c["records"]:
                a = ipaddress.ip_address(addr)
                if not any(a in p for p in prefixes):
                    results[c["key"]]["notes"].append(f"no PTR for {addr}: outside the placement prefixes")
                    continue
                name = reverse_name(addr)
                rzone = covering_zone(name, reverse_zones)
                if rzone is None:
                    results[c["key"]]["notes"].append(f"no PTR for {addr}: no platform reverse zone")
                    continue
                ptr_claims.append(dict(c, zone=rzone, name=name, type="PTR", records=[c["name"]], address=addr))
        ptr_claims.sort(key=lambda c: (owner_of.get((c["name"], "PTR")) != c["description"], c["created"], c["key"],
                                       c["records"][0]))
        ptrs = 0
        for c in ptr_claims:
            k = (c["name"], "PTR")
            if k in desired:
                results[c["key"]]["notes"].append(
                    f"no PTR for {c['address']}: its reverse name points to {desired[k]['records'][0].rstrip('.')}")
                continue
            desired[k] = c
            if self.apply(conn, zones, actual, c, results[c["key"]]):
                ptrs += 1

        # the sweep: every owned record nobody wants any more (PTRs whose AAAA went included)
        for k, rs in actual.items():
            if k not in desired:
                try:
                    conn.dns.delete_recordset(rs, rs.zone_id, ignore_missing=True)
                    log(f"deleted {rs.name} {rs.type} ({rs.description[len(self.prefix):]})")
                except openstack.exceptions.HttpException as e:  # e.g. the zone is being deleted with its environment
                    log(f"not deleted {rs.name} {rs.type}: {e.status_code}")

        for key, r in results.items():
            self.report(r)
        self.counts = {"refused": sum(1 for r in results.values() if r["refused"]), "ptrs": ptrs}

    def apply(self, conn, zones, actual, c, result):
        """Create or update one desired recordset; True when it is published."""
        rs = actual.get((c["name"], c["type"]))
        label = f"{c['address']} PTR" if c["type"] == "PTR" else f"{c['name'].rstrip('.')} {c['type']}"
        try:
            if rs is not None:
                if sorted(rs.records) != c["records"] or rs.ttl != c["ttl"] or rs.description != c["description"]:
                    conn.dns.update_recordset(rs, records=c["records"], ttl=c["ttl"], description=c["description"])
                    log(f"updated {c['name']} {c['type']} {c['records']} ({c['key']})")
            else:
                z = zones.get(c["zone"])
                if z is None:
                    raise Refused(f"the environment zone {c['zone']} does not exist (yet)")
                conn.dns.create_recordset(z, name=c["name"], type=c["type"], records=c["records"], ttl=c["ttl"],
                                          description=c["description"])
                log(f"created {c['name']} {c['type']} {c['records']} ({c['key']})")
            result["records"].append(label)
            return True
        except Refused as e:
            result["refused"].append(str(e))
        except openstack.exceptions.ConflictException:
            if c["type"] == "PTR":  # someone else's PTR at that name: not the tenant's fault, the AAAA stands
                result["notes"].append(f"no PTR for {c['address']}: its reverse name is held by another owner")
            else:
                result["refused"].append(f"{c['name']} {c['type']} already exists (not this object's)")
        except openstack.exceptions.BadRequestException as e:
            target = result["notes"] if c["type"] == "PTR" else result["refused"]
            target.append(f"{label}: {getattr(e, 'details', None) or e}")
        return False

    def report(self, r):
        kind, o = r["kind"], r["obj"]
        m = o["metadata"]
        published = f"{len(r['records'])} record(s)" + (": " + ", ".join(sorted(r["records"])) if r["records"] else "")
        if r["refused"]:
            status = "refused: " + "; ".join(r["refused"]) + (f"; published {published}" if r["records"] else "")
        else:
            status = f"ok: {published}"
        if r.get("notes"):  # what was published without its reverse name (not a refusal)
            status += "; " + "; ".join(r["notes"])
        status = status[:1000]
        if (m.get("annotations") or {}).get(STATUS_ANNOTATION) == status:
            return
        body = {"metadata": {"annotations": {STATUS_ANNOTATION: status}}}
        try:
            if kind == "Service":
                self.core.patch_namespaced_service(m["name"], m["namespace"], body)
            elif kind == "Route":
                self.custom.patch_namespaced_custom_object(*ROUTE[:2], m["namespace"], ROUTE[2], m["name"], body)
            else:
                self.custom.patch_namespaced_custom_object(*DEP[:2], m["namespace"], DEP[2], m["name"], body)
                self.custom.patch_namespaced_custom_object_status(
                    *DEP[:2], m["namespace"], DEP[2], m["name"],
                    {"status": {"observedGeneration": m.get("generation", 0)}})
            self.event(kind, o, status)
            log(f"{m['namespace']}/{kind}/{m['name']}: {status}")
        except client.ApiException as e:
            if e.status != 404:  # gone meanwhile
                log(f"{m['namespace']}/{kind}/{m['name']}: status not written ({e.status} {e.reason})")

    def event(self, kind, o, status):
        m = o["metadata"]
        now = datetime.datetime.now(datetime.timezone.utc)
        refused = status.startswith("refused")
        api_version = {"Service": "v1", "Route": "route.openshift.io/v1"}.get(kind, "externaldns.k8s.io/v1alpha1")
        ev = client.CoreV1Event(
            metadata=client.V1ObjectMeta(generate_name=f"{m['name'][:200]}.", namespace=m["namespace"]),
            involved_object=client.V1ObjectReference(api_version=api_version, kind=kind, name=m["name"],
                                                     namespace=m["namespace"], uid=m.get("uid")),
            reason="DNSRecordRefused" if refused else "DNSRecordsPublished", message=status,
            type="Warning" if refused else "Normal", first_timestamp=now, last_timestamp=now, count=1,
            source=client.V1EventSource(component="shc-tenant-dns"),
            reporting_component="shc-tenant-dns", reporting_instance=self.cluster)
        try:
            self.core.create_namespaced_event(m["namespace"], ev)
        except client.ApiException as e:
            log(f"event not written ({e.status} {e.reason})")

    # ── health ─────────────────────────────────────────────────────────────────────────────────────────────────
    def reconcile_round(self):
        """One reconcile with its health record; True on success."""
        try:
            self.reconcile()
        except Exception as e:  # noqa: BLE001 - the caller retries
            msg = f"{type(e).__name__}: {e}"
            log(f"reconcile failed: {msg}")
            self.conn = None
            self.record_health(error=msg)
            return False
        self.record_health()
        return True

    def record_health(self, error=None):
        """The ConfigMap tenant-dns-status in the controller's namespace: lastSuccess (on success), lastError and
        lastErrorAt (on failure; kept after a success, so readers compare the two times), refused (objects with a
        refusal) and ptrs (PTRs held) of the last successful round. A Warning Event DNSReconcileFailed on the
        ConfigMap once per distinct error (again after a success). Never raises."""
        now = rfc3339()
        if error is None:
            data = {"lastSuccess": now, "refused": str(self.counts["refused"]), "ptrs": str(self.counts["ptrs"])}
        else:
            data = {"lastError": error[:1000], "lastErrorAt": now}
        if not self.namespace:
            return
        try:
            try:
                self.core.patch_namespaced_config_map(STATUS_CONFIGMAP, self.namespace, {"data": data})
            except client.ApiException as e:
                if e.status != 404:
                    raise
                base = {"lastSuccess": "", "lastError": "", "lastErrorAt": "", "refused": "0", "ptrs": "0"}
                self.core.create_namespaced_config_map(self.namespace, client.V1ConfigMap(
                    metadata=client.V1ObjectMeta(name=STATUS_CONFIGMAP, namespace=self.namespace,
                                                 labels={"app": "tenant-dns-controller"}),
                    data=dict(base, **data)))
        except Exception as e:  # noqa: BLE001 - the API may be what failed; the loop goes on
            log(f"health not written ({getattr(e, 'status', '')} {getattr(e, 'reason', '') or type(e).__name__})")
        if error is None:
            self.reported_error = None
        elif error != self.reported_error:
            self.reported_error = error
            self.health_event(error, now)

    def health_event(self, error, now):
        ev = client.CoreV1Event(
            metadata=client.V1ObjectMeta(generate_name=f"{STATUS_CONFIGMAP}.", namespace=self.namespace),
            involved_object=client.V1ObjectReference(api_version="v1", kind="ConfigMap", name=STATUS_CONFIGMAP,
                                                     namespace=self.namespace),
            reason="DNSReconcileFailed", message=f"reconcile failed: {error}"[:1000], type="Warning",
            first_timestamp=now, last_timestamp=now, count=1,
            source=client.V1EventSource(component="shc-tenant-dns"),
            reporting_component="shc-tenant-dns", reporting_instance=self.cluster)
        try:
            self.core.create_namespaced_event(self.namespace, ev)
        except Exception as e:  # noqa: BLE001
            log(f"event not written ({getattr(e, 'status', '')} {getattr(e, 'reason', '') or type(e).__name__})")

    # ── loops ──────────────────────────────────────────────────────────────────────────────────────────────────
    def watch_loop(self, what, list_fn, *args, **kwargs):
        while True:
            try:
                for ev in watch.Watch().stream(list_fn, *args, timeout_seconds=300, **kwargs):
                    o = ev["object"]
                    ns = o["metadata"].get("namespace") if isinstance(o, dict) else o.metadata.namespace
                    # namespaces always; other objects only in tenant namespaces (the set of the last round)
                    if what == "namespaces" or ns in self.tenant_ns:
                        self.trigger.set()
            except client.ApiException as e:
                if e.status == 404:  # resource kind not installed (yet)
                    time.sleep(60)
                    continue
                log(f"watch {what}: {e.status} {e.reason}")
                time.sleep(5)
            except Exception as e:  # noqa: BLE001 - keep watching
                log(f"watch {what}: {type(e).__name__}: {e}")
                time.sleep(5)

    def run(self):
        watches = [
            ("namespaces", self.core.list_namespace, (), {"label_selector": NS_LABEL}),
            ("services", self.core.list_service_for_all_namespaces, (), {}),
            ("dnsendpoints", self.custom.list_cluster_custom_object, DEP, {}),
            ("routes", self.custom.list_cluster_custom_object, ROUTE, {}),
        ]
        for what, fn, args, kwargs in watches:
            threading.Thread(target=self.watch_loop, args=(what, fn) + tuple(args), kwargs=kwargs, daemon=True).start()
        log(f"tenant DNS controller: cluster {self.cluster}, domain {self.domain}, resync {self.resync}s")
        self.trigger.set()  # first round right away
        while True:
            self.trigger.wait(self.resync)
            time.sleep(3)  # debounce a burst of events
            self.trigger.clear()
            if not self.reconcile_round():  # next round retries
                time.sleep(10)
                self.trigger.set()
            with open(self.health, "w") as f:  # liveness: the loop runs (DNS outages only show in the log)
                f.write(str(time.time()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--clouds", required=True)
    ap.add_argument("--once", action="store_true", help="reconcile once and exit")
    args = ap.parse_args()
    with open(args.config) as f:
        cfg = yaml.safe_load(f)
    try:
        config.load_incluster_config()
    except config.ConfigException:  # outside a pod (manual run): KUBECONFIG
        config.load_kube_config()
    c = Controller(cfg, args.clouds)
    if args.once:
        return 0 if c.reconcile_round() else 1
    c.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
