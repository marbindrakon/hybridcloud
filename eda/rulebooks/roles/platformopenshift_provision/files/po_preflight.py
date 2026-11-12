#!/usr/bin/env python3
"""Decisions of the PlatformOpenshift jobs that are pure computation (no API calls; nothing secret in or out).

Reads one JSON request on stdin and prints one JSON result on stdout; exits non-zero with {"error": ...} when the
request itself is malformed. A refusal is a result, not an error: {"reason": <condition reason>, "message": ...,
"wait": <true when nobody needs to act and the job re-checks later>}; reason "" means the check passed.

Actions (request["action"]):
  build-name     {prefix, entity, name, max}  the cluster's build name <prefix>-<entity>-<name>-<digest>: the readable
                 part (lower case, other characters as "-") cut to fit max characters (default 53, a Helm release
                 name's limit), and 8 hex digits of the SHA-256 of the exact pair (entity, NUL, name), so two pairs
                 whose readable parts read alike (a hyphen from either side, a cut) still get different names.
                 Stable for a pair. -> {buildName}
  acm-names      {buildName, namespace}  the base of the names of the build's ACM objects in the policy namespace
                 (Policy <base>-keycloak-oidc and <base>-cluster-rbac, their Placement <policy>-placement and
                 PlacementBinding <policy>-binding, the ConfigurationPolicy templates <base>-oidc-*): ACM refuses a
                 policy whose namespace and name together exceed 62 characters (its copy on a managed cluster is
                 named <namespace>.<name>, a label value). The build name itself when every policy name fits (so the
                 objects of an existing build keep their names); else its head and 8 hex digits of the SHA-256 of the
                 whole build name. -> {base, bases: [base, and the build name when it differs: names an older job
                 may have used, for the teardown]}
  hub-owner      {namespace: the hub namespace object or {}, uid, recorded, buildName, claim}  whether the cluster
                 (PlatformOpenshift uid) owns the build's hub namespace: it carries the owner label with that uid; or,
                 for a build recorded under that name before the label existed, it has no owner label (claim: true
                 adopts it); a teardown (claim false) of a recorded build whose namespace is gone owns its leftovers.
                 A namespace marked with another uid for a build this object recorded is BuildOwnerMismatch (a
                 restored or moved object: never adopted; the message names the namespace and both uids).
                 -> {owned, adopt, reason, message, owner?}
  networking     {spec: spec.networking, defaults: {clusterNetwork, hostPrefix, serviceNetwork, machineNetwork},
                 machineFromPlacement: cidr or ""}  the cluster's ranges in effect (spec over defaults; the placement's
                 subnet as machine network for a cloud cluster on a network); a malformed default names its setting;
                 hostPrefix must be longer than the clusterNetwork prefix (InvalidSpec).
                 -> {clusterNetwork, hostPrefix, serviceNetwork, machineNetwork, reason, message}
  network        {kind, environment, namespace, network, networkObj, placements, fabric, needPlatform,
                 needRouterAttachments, hubApi, hubPort, cluster}  a cluster on a hybrid network: the network and its
                 placement in the environment (ready; router attachments ready when asked; an IPv4 subnet); with
                 needPlatform the network's condition PlatformReachable, the fabric's service routing domain
                 (status.realization.service) with its service range, the subnet inside service.attachableRanges
                 when the fabric names them (none named: any range the network accepted) and the hub's API address
                 admitted by service.hubEndpoints when it lies in the service range (service.sources). While the
                 platform's side is down (ServiceNotOffered, CircuitNotDeployed) a cluster the network keeps rows
                 for (status.platformAccess.prefixes, its name, pending or ready) waits; a new one is refused.
                 Every check runs that its inputs allow, and a refusal wins over a wait (a wait is reported only when
                 nothing refuses): a cluster never waits for a placement or a network that would refuse it anyway.
                 -> {reason, message, wait, placement (namespace/name), subnet4, space, networkId, networkName,
                     fabric, service, reserved}
  external       {choice, site, networks: CloudInfrastructure status.site.externalNetworks}  the global external
                 network in effect (the one named, else the site's default). -> {reason, message, network: {name, id}}
  fips-check     {addresses, network (namespace/name), service}  an address inside a prefix the service routing
                 domain already carries for another network: ServicePrefixInUse.
  service-wait   {network (namespace/name), prefixes, service, rows: HybridNetwork status.platformAccess.prefixes}
                 -> {done, reason, message}: done when every prefix is carried for the network; a refusal (on the
                 fabric's refused list or a refused row of the network) gives its reason.
  router-gateway {routers: [router objects], name, network_id}  the IPv4 address of the named router's external
                 gateway, when exactly one router has the name and its gateway is on network_id. -> {address, note}
"""
import hashlib
import ipaddress
import json
import re
import socket
import sys
from urllib.parse import urlparse

BUILD_NAME_MAX = 53
BUILD_NAME_DIGEST = 8
# ACM: the policy namespace and the policy name together at most 62 characters (the replicated policy on a managed
# cluster is <namespace>.<name>); the suffixes of the build's policies (keycloak_oidc.yml, cluster_rbac.yml)
ACM_COMBINED_MAX = 62
ACM_POLICY_SUFFIXES = ("keycloak-oidc", "cluster-rbac")
ACM_DIGEST = 8
# the hub namespace of a build carries the uid of the PlatformOpenshift it was made for
OWNER_LABEL = "hybridsovereign.redhat/platformopenshift-uid"

# HybridNetwork condition PlatformReachable False reason -> the cluster's Preflight reason
PLATFORM_REASONS = {
    "NotRequested": "PlatformAccessOff",
    "NoIPv4": "PlacementHasNoIPv4",
    "OutsideAttachableRanges": "OutsideAttachableRanges",
    "InfrastructureOverlap": "PrefixReserved",
    "ServiceNotOffered": "ServiceNotOffered",
    "CircuitNotDeployed": "CircuitNotDeployed",
}
WAITING = {"WaitingForPlacement", "WaitingForPlatformAccess"}
# the platform's side is down (no service, no platform circuit): a cluster whose prefixes the network keeps (rows
# pending or ready, they keep their place while the platform's side is down) waits; a new cluster is refused
PLATFORM_SIDE = {"ServiceNotOffered", "CircuitNotDeployed"}


def flag(value):
    """A request flag: the jobs may send a templated boolean as a string ("True"/"False" on ansible-core 2.16)."""
    return value is True or str(value).strip().lower() in ("true", "yes", "1")


def result(reason="", message="", **extra):
    return dict({"reason": reason, "message": message, "wait": reason in WAITING}, **extra)


def net(value):
    return ipaddress.ip_network(str(value).strip(), strict=False)


def v4(value):
    try:
        n = net(value)
    except ValueError:
        return None
    return n if n.version == 4 else None


def inside(small, bigs):
    s = net(small)
    return any(s.version == b.version and s.subnet_of(b) for b in (net(x) for x in bigs))


def build_name(req):
    entity = str(req.get("entity") or "").strip()
    name = str(req.get("name") or "").strip()
    if not entity or not name:
        raise ValueError("build-name needs entity and name")
    prefix = re.sub(r"[^a-z0-9-]", "-", str(req.get("prefix") or "shc-oso").lower()).strip("-")
    limit = int(req.get("max") or BUILD_NAME_MAX)
    digest = hashlib.sha256(f"{entity}\0{name}".encode()).hexdigest()[:BUILD_NAME_DIGEST]
    room = limit - len(prefix) - len(digest) - 2
    if room < 1:
        raise ValueError(f"build-name: {limit} characters leave no room after {prefix}")
    readable = re.sub(r"[^a-z0-9-]", "-", f"{entity}-{name}".lower())[:room].strip("-")
    return {"buildName": "-".join(x for x in (prefix, readable, digest) if x)}


def acm_names(req):
    build = str(req.get("buildName") or "").strip()
    ns = str(req.get("namespace") or "").strip()
    if not build or not ns:
        raise ValueError("acm-names needs buildName and namespace")
    room = ACM_COMBINED_MAX - len(ns) - 1 - max(len(s) for s in ACM_POLICY_SUFFIXES)
    if room < ACM_DIGEST + 2:
        raise ValueError(f"acm-names: namespace {ns} leaves no room for a policy name")
    if len(build) <= room:
        return {"base": build, "bases": [build]}
    digest = hashlib.sha256(build.encode()).hexdigest()[:ACM_DIGEST]
    base = build[:room - ACM_DIGEST - 1].rstrip("-") + "-" + digest
    return {"base": base, "bases": [base, build]}


def hub_owner(req):
    """The cluster's claim on its build's hub namespace (see the module's docstring)."""
    ns = req.get("namespace") or {}
    uid = str(req.get("uid") or "")
    build = str(req.get("buildName") or "")
    recorded = str(req.get("recorded") or "")
    meta = ns.get("metadata") or {}
    owner = str((meta.get("labels") or {}).get(OWNER_LABEL) or "")
    out = {"owned": False, "adopt": False, "reason": "", "message": ""}
    if not build:
        return dict(out, reason="NoBuildName", message="the cluster recorded no build name (status.buildName), so it "
                    "owns nothing on the hub and nothing on the hub is removed for it")
    if not meta:
        if flag(req.get("claim")):
            return dict(out, reason="NotClaimed", message=f"namespace {build} does not exist on the hub")
        if recorded and recorded == build:
            return dict(out, owned=True)
        return dict(out, reason="NotRecorded", message=f"build {build} is not recorded by this cluster")
    if owner:
        if uid and owner == uid:
            return dict(out, owned=True)
        if recorded and recorded == build:
            # this object recorded the build, the namespace names another uid: an object restored from a backup or
            # moved gets a new uid. Never adopted: said, so that a human decides
            return dict(out, reason="BuildOwnerMismatch", owner=owner, message=(
                f"namespace {build} on the hub carries {OWNER_LABEL}={owner}, but this PlatformOpenshift has uid {uid} "
                "although it recorded that build (an object restored from a backup or moved gets a new uid); nothing "
                "of the build on the hub is touched for this object until the platform team, having checked that the "
                f"build is this cluster's, sets the label to {uid}"))
        return dict(out, reason="NamespaceTaken", message=f"namespace {build} on the hub belongs to another cluster")
    if recorded and recorded == build:
        return dict(out, owned=True, adopt=flag(req.get("claim")))
    return dict(out, reason="NamespaceTaken", message=f"namespace {build} on the hub exists and was not made for "
                "this cluster")


def networking(req):
    spec = req.get("spec") or {}
    defaults = req.get("defaults") or {}
    out = {}
    for key, lo, hi in (("clusterNetwork", 8, 22), ("serviceNetwork", 12, 24), ("machineNetwork", 16, 27)):
        if key in spec and spec[key] not in (None, ""):
            out[key] = str(spec[key])
            continue
        d = defaults.get(key)
        n = v4(d) if d not in (None, "") else None
        if n is None or str(n) != str(d).strip() or not lo <= n.prefixlen <= hi:
            return result("InvalidSpec", f"The installation's default networking.{key} ({d!r}) is not an IPv4 "
                          f"network prefix of /{lo} to /{hi}; set platformopenshift_defaults.networking.{key}")
        out[key] = str(n)
    if req.get("machineFromPlacement"):
        out["machineNetwork"] = str(req["machineFromPlacement"])
    hp = spec.get("hostPrefix")
    if hp in (None, ""):
        hp = defaults.get("hostPrefix")
        try:
            hp = int(str(hp))
        except ValueError:
            return result("InvalidSpec", f"The installation's default networking.hostPrefix ({hp!r}) is not a number; "
                          "set platformopenshift_defaults.networking.hostPrefix")
    hp = int(hp)
    out["hostPrefix"] = hp
    cl = net(out["clusterNetwork"]).prefixlen
    if not cl < hp <= 28:
        return result("InvalidSpec", f"spec.networking.hostPrefix {hp} must be longer than the clusterNetwork prefix "
                      f"/{cl} ({out['clusterNetwork']}) and at most 28", **out)
    return result(**out)


def condition(obj, ctype):
    for c in ((obj or {}).get("status") or {}).get("conditions") or []:
        if isinstance(c, dict) and c.get("type") == ctype:
            return c
    return None


def placement_subnet4(p):
    st = p.get("status") or {}
    subs = [s.get("prefix") for s in st.get("subnets") or [] if isinstance(s, dict) and str(s.get("family")) == "4"]
    subs += [x for x in st.get("prefixes") or [] if ":" not in str(x)]
    return str(subs[0]) if subs else ""


def hub_addresses(hub_api):
    host = urlparse(hub_api).hostname if "://" in (hub_api or "") else (hub_api or "")
    if not host:
        return []
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET)
    except OSError:
        return None
    return sorted({i[4][0] for i in infos})


def hub_port(hub_api, default=6443):
    try:
        return urlparse(hub_api).port or default
    except ValueError:
        return default


def network(req):
    kind, env, ns, name = req["kind"], req["environment"], req["namespace"], req["network"]
    hn = req.get("networkObj") or {}
    if not hn or (hn.get("metadata") or {}).get("deletionTimestamp"):
        return result("NetworkNotFound", f"HybridNetwork {name} not found in namespace {ns}"
                      + (" (it is being deleted)" if hn else ""))
    mine = [p for p in req.get("placements") or []
            if (p.get("spec") or {}).get("network") == name
            and ((p.get("spec") or {}).get("backend") or {}).get("kind") == kind
            and ((p.get("spec") or {}).get("backend") or {}).get("name") == env]
    if not mine:
        return result("NetworkNotFound", f"HybridNetwork {name} has no NetworkPlacement in {kind} {env}")
    p = mine[0]
    pspec = p.get("spec") or {}
    pst = p.get("status") or {}
    pref = f"{p['metadata'].get('namespace', ns)}/{p['metadata']['name']}"
    out = {"placement": pref, "placementName": p["metadata"]["name"]}
    if p["metadata"].get("deletionTimestamp"):
        return result("NetworkNotFound", f"NetworkPlacement {pref} of HybridNetwork {name} in {kind} {env} is being "
                      "deleted", **out)
    # a refusal wins over a wait: the first wait is kept and returned only when no later check refuses
    waits = []

    def hold(reason, message):
        waits.append((reason, message))

    def done():
        return result(*waits[0], **out) if waits else result(**out)
    nov4 = (f"NetworkPlacement {pref} has no IPv4 subnet; the platform reaches clusters over IPv4, so the network "
            "needs IPv4 space")
    # spec.ipv4 cannot be added later: an IPv6-only placement never gets an IPv4 subnet
    if "ipv6" in pspec and "ipv4" not in pspec:
        return result("PlacementHasNoIPv4", nov4, **out)
    ready = pst.get("ready") is True
    if not ready:
        hold("WaitingForPlacement", f"NetworkPlacement {pref} of HybridNetwork {name} in {kind} {env} is not ready yet")
    site = ((pst.get("realization") or {}).get("site") or {})
    out.update(networkId=str(site.get("networkId") or ""), networkName=str(site.get("networkName") or ""))
    if flag(req.get("needRouterAttachments")):
        if not ((pspec.get("use") or {}).get("routerAttachments") is True):
            return result("RouterAttachmentsOff", f"NetworkPlacement {pref} does not allow router attachments "
                          "(spec.use.routerAttachments); an OpenShift cluster on the network needs them", **out)
        state = (((pst.get("use") or {}).get("routerAttachments") or {}).get("state") or "")
        if ready and (state != "ready" or not out["networkId"] or not out["networkName"]):
            hold("WaitingForPlacement", f"NetworkPlacement {pref}: router attachments are not ready yet"
                 + (f" (state {state})" if state else ""))
    subnet4 = placement_subnet4(p)
    out["subnet4"] = subnet4
    if ready and not subnet4:
        return result("PlacementHasNoIPv4", nov4, **out)
    hst = hn.get("status") or {}
    # the network's space: spec.prefixes and status.addressSpace[].prefix (rows {prefix, family, ...})
    space = []
    for x in ((hn.get("spec") or {}).get("prefixes") or []) + [
            a.get("prefix") if isinstance(a, dict) else a for a in hst.get("addressSpace") or []]:
        if x and str(x) not in space:
            space.append(str(x))
    out["space"] = space
    fab = req.get("fabric") or {}
    fst = fab.get("status") or {}
    out["fabric"] = (fab.get("metadata") or {}).get("name", "")
    out["reserved"] = [r for r in fst.get("reservedRanges") or [] if isinstance(r, dict) and r.get("prefix")]
    svc = ((fst.get("realization") or {}).get("service") or {})
    out["service"] = svc
    if not flag(req.get("needPlatform")):
        return done()
    me = str(req.get("cluster") or "")
    held = [r.get("prefix") for r in ((hst.get("platformAccess") or {}).get("prefixes") or [])
            if isinstance(r, dict) and me and r.get("cluster") == me and r.get("state") in ("pending", "ready")]

    def side_down(reason, message):
        """None when the cluster waits (the network keeps its addresses), else the refusal."""
        if held:
            hold("WaitingForPlatformAccess", f"HybridNetwork {name} keeps the cluster's addresses "
                 f"({', '.join(map(str, held))}) while the platform's side is down: {message}")
            return None
        return result(reason, message, **out)
    cond = condition(hn, "PlatformReachable")
    if cond is None:
        hold("WaitingForPlatformAccess", f"HybridNetwork {name} does not report PlatformReachable yet")
    elif str(cond.get("status")) != "True":
        why = cond.get("reason") or ""
        if why != "AttachPending":
            mapped = PLATFORM_REASONS.get(why, "WaitingForPlatformAccess")
            text = (f"HybridNetwork {name}: the platform cannot reach the network ({why}"
                    + (f": {cond.get('message')}" if cond.get("message") else "") + ")")
            if mapped in PLATFORM_SIDE:
                refused = side_down(mapped, text)
                if refused:
                    return refused
            elif mapped in WAITING:
                hold(mapped, text)
            else:
                return result(mapped, text, **out)
    if not fab:
        hold("WaitingForPlatformAccess", f"HybridNetwork {name}: its fabric {hst.get('fabric') or '(none)'} "
             "could not be read")
        return done()
    if not svc.get("enabled"):
        why = (f"it does not realize its service routing domain: {svc.get('message') or svc.get('reason')}"
               if svc.get("reason") else "its service routing domain is off")
        return side_down("ServiceNotOffered", f"HybridFabric {out['fabric']} offers no connection for clusters on "
                         f"networks ({why})") or done()
    if not [r for r in svc.get("range") or [] if v4(r)]:
        # the service range is required with the service (check-fabric): no address of the hub reaches a cluster
        # without it (a fabric status from before it: the fabric's next run writes it, or refuses the fabric by name)
        return side_down("ServiceNotOffered", f"HybridFabric {out['fabric']} names no addresses the platform reaches "
                         "clusters from (service.range)") or done()
    # the ranges a cluster's addresses may lie in (0.0.0.0/0: any range the platform does not refuse; the realization
    # never leaves it empty while the service is realized: Review R4 NH1)
    ranges = [str(r) for r in svc.get("attachableRanges") or []]
    if not ranges:
        return side_down("ServiceNotOffered", f"HybridFabric {out['fabric']} names no ranges for clusters on networks "
                         "(service.attachableRanges)") or done()
    if subnet4 and not inside(subnet4, ranges):
        return result("OutsideAttachableRanges", f"NetworkPlacement {pref}: subnet {subnet4} lies outside the ranges "
                      f"the platform reaches ({', '.join(ranges)})", **out)
    sources = [s for s in svc.get("sources") or [] if v4(s)]
    if sources and req.get("hubApi"):
        addrs = hub_addresses(req["hubApi"])
        port = int(req.get("hubPort") or hub_port(req["hubApi"]))
        for a in addrs or []:
            if not inside(a + "/32", sources):
                continue
            ok = any(v4(e.get("prefix")) and inside(a + "/32", [e["prefix"]])
                     and port in [int(x) for x in e.get("ports") or []]
                     for e in svc.get("hubEndpoints") or [] if isinstance(e, dict))
            if not ok:
                return result("HubEndpointNotAdmitted", f"The hub's API {a} port {port} lies in the hub's ranges and no "
                              "hub endpoint of the fabric admits it; a cluster on the network could not reach the hub",
                              **out)
    return done()


def external(req):
    choice = str(req.get("choice") or "")
    site = str(req.get("site") or "")
    nets = [n for n in req.get("networks") or [] if isinstance(n, dict) and n.get("name")]
    if not site:
        if choice:
            return result("ExternalNetworkNotListed", f"{choice} is not an external network of this site (the "
                          "environment has no site; its clusters need a hybrid network)")
        return result("NetworkChoiceRequired", "The environment has no site with external networks; name a hybrid "
                      "network (spec.openstack.network)")
    if not choice:
        dflt = [n for n in nets if n.get("default") is True]
        if not dflt:
            return result("NetworkChoiceRequired", f"Site {site} has no default external network; name a network "
                          "(spec.openstack.network or spec.openstack.externalNetwork)")
        chosen = dflt[0]
    else:
        same = [n for n in nets if n.get("name") == choice]
        if len(same) != 1:
            return result("ExternalNetworkNotListed", f"{choice} is not an external network of site {site}"
                          + (" (its name is not unique)" if same else "")
                          + (f"; listed: {', '.join(sorted(n['name'] for n in nets))}" if nets else ""))
        chosen = same[0]
    fams = [str(f) for f in chosen.get("families") or []]
    if fams and "4" not in fams:
        return result("PlacementHasNoIPv4", f"External network {chosen['name']} of site {site} has no IPv4 subnet; "
                      "the cluster's floating IPs are IPv4")
    return result(network={"name": chosen["name"], "id": str(chosen.get("id") or "")})


def fips_check(req):
    mine = req.get("network") or ""
    for row in (req.get("service") or {}).get("networks") or []:
        if not isinstance(row, dict) or row.get("network") == mine:
            continue
        for a in req.get("addresses") or []:
            for pfx in row.get("prefixes") or []:
                if inside(f"{a}/32", [pfx]):
                    return result("ServicePrefixInUse", f"Floating IP {a} lies in {pfx}, which the platform already "
                                  "reaches for another network")
    return result()


def service_wait(req):
    mine = req.get("network") or ""
    want = {str(net(p)) for p in req.get("prefixes") or []}
    svc = req.get("service") or {}
    for r in svc.get("refused") or []:
        if isinstance(r, dict) and r.get("network") == mine and r.get("prefix") and str(net(r["prefix"])) in want:
            return dict(result(r.get("reason") or "ServicePrefixInUse",
                               f"The platform refused {r['prefix']} for HybridNetwork {mine.split('/')[-1]}"
                               + (f" ({r.get('reason')})" if r.get("reason") else "")), done=False)
    for r in req.get("rows") or []:
        if (isinstance(r, dict) and r.get("state") == "refused" and r.get("prefix")
                and str(net(r["prefix"])) in want):
            return dict(result(r.get("reason") or "ServicePrefixInUse",
                               r.get("message") or f"HybridNetwork {mine.split('/')[-1]} refused {r['prefix']}"),
                        done=False)
    # waiting for the platform's limit (Review R4 M1): the cluster waits; the message says why, with the numbers
    for r in svc.get("waiting") or []:
        if isinstance(r, dict) and r.get("network") == mine and r.get("prefix") and str(net(r["prefix"])) in want:
            return dict(result("WaitingForPlatformAccess", f"HybridNetwork {mine.split('/')[-1]}: "
                               f"{r.get('reason') or 'ServiceLimitReached'}: {r.get('message') or r['prefix']}"),
                        done=False)
    have = set()
    for row in svc.get("networks") or []:
        if isinstance(row, dict) and row.get("network") == mine:
            have |= {str(net(p)) for p in row.get("prefixes") or []}
    missing = sorted(want - have)
    if missing:
        return dict(result("WaitingForPlatformAccess", "The platform is connecting to HybridNetwork "
                           f"{mine.split('/')[-1]}; not carried yet: {', '.join(missing)}"), done=False)
    return dict(result(), done=True)


def router_gateway(req):
    """The router is found by name in a project the tenant writes to: only a name that is unique there, and a router
    whose gateway is on the cluster's network (network_id), gives an address (Review R2 L9)."""
    named = [r for r in req.get("routers") or [] if isinstance(r, dict) and r.get("name") == req.get("name")]
    if len(named) != 1:
        return {"address": "", "note": f"{len(named)} routers named {req.get('name')}" if named else ""}
    gw = named[0].get("external_gateway_info") or {}
    want = str(req.get("network_id") or "")
    if want and str(gw.get("network_id") or "") != want:
        return {"address": "", "note": f"router {req.get('name')} has its gateway on another network"}
    for f in gw.get("external_fixed_ips") or []:
        a = str((f or {}).get("ip_address") or "")
        if a and ":" not in a:
            return {"address": a, "note": ""}
    return {"address": "", "note": ""}


ACTIONS = {"build-name": build_name, "acm-names": acm_names, "hub-owner": hub_owner, "networking": networking, "network": network, "external": external,
           "fips-check": fips_check, "service-wait": service_wait, "router-gateway": router_gateway}


def main():
    try:
        req = json.load(sys.stdin)
        print(json.dumps(ACTIONS[req["action"]](req)))
    except Exception as e:  # malformed request: one line for the job log
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
