#!/usr/bin/env python3
"""IPAM of the HybridNetworks: each network's address space and its placements' subnets (shc, design/P10-PLAN.md §3).

Reads one JSON request on stdin ({action, provider, ...}), prints one JSON result; {"error": ...} and exit 1 on
failure. A token arrives on stdin, never in argv.

Provider contract (`network` = HybridNetwork/<ns>/<name>, `owner` = NetworkPlacement/<ns>/<name>):
  ipam-space    {network, prefixes, reserved: [cidr]}   set the network's space and the platform's ranges never
                allocated in it (reserved, kept where they overlap the space); refused (refused: [...]) when an
                allocation would fall outside the space (adding, removing an unused prefix and growing one in place
                work); addressSpace
  ipam-allocate {network, owner, want: [{family, prefixLength?, prefix?}], exclude: [cidr]}
                an owner holding prefixes gets them back (idempotent); else every family or none: a pinned prefix as
                given, a size at the lowest free aligned block of the network's prefixes of that family, clear of the
                network's other allocations, its reserved ranges and exclude (the site's ranges). Result {prefixes}
                (IPv4 first), or {waiting: why} (no room, or a pinned prefix held by another owner), or {invalid:
                why} (a pinned prefix outside the space or on a reserved or excluded range); waiting and invalid
                store nothing
  ipam-release  {network, owner}
  ipam-drop     {network, holders: [owner]}   remove the network's space; refused while a holder (a placement that
                still exists) has prefixes; allocations of other owners are dropped with it and reported (stale)
  ipam-get      {network}   space, allocations, addressSpace
addressSpace: per space prefix {prefix, family, allocations, largestFree (shortest free prefix length; absent when
full)}: the reserved ranges are not free, the site ranges are not counted.

Providers (request field `provider`, default ledger):
  ledger  the fabric ledger: section "ipam" of ConfigMap fabric-ledger-<fabric> (shc_ledger.py, compare-and-swap),
          {"HybridNetwork/<ns>/<name>": {"prefixes": [...], "reserved": [...], "alloc": {owner: [cidr, ...]}}};
          requests also carry
          {api, token, namespace, fabric}. An allocate or release that changes the ledger writes the network's
          status.addressSpace (best effort), so it follows every change and not only the network's own runs.
  Another provider (e.g. NetBox) implements the same five calls; the pure functions below do the arithmetic for all.
"""
import ipaddress
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shc_ledger  # noqa: E402


# ---- pure core (no I/O) -------------------------------------------------------------------------------------------

def net(c, strict=True):
    return ipaddress.ip_network(str(c), strict=strict)


def ordered(nets):
    return sorted(nets, key=lambda n: (n.version, int(n.network_address), n.prefixlen))


def busy_intervals(space, taken):
    """The parts of space (one prefix) that taken (networks) cover, as merged [start, end] integer intervals."""
    lo, hi = int(space.network_address), int(space.broadcast_address)
    out = []
    for t in taken:
        if t.version != space.version:
            continue
        s, e = max(lo, int(t.network_address)), min(hi, int(t.broadcast_address))
        if s <= e:
            out.append([s, e])
    out.sort()
    merged = []
    for s, e in out:
        if merged and s <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return merged


def free_gaps(space, taken):
    lo, hi = int(space.network_address), int(space.broadcast_address)
    cur = lo
    for s, e in busy_intervals(space, taken):
        if s > cur:
            yield cur, s - 1
        cur = max(cur, e + 1)
    if cur <= hi:
        yield cur, hi


def first_fit(space, taken, family, prefix_length):
    """The lowest aligned /prefix_length inside the space prefixes of family, overlapping nothing in taken."""
    bits = 32 if family == 4 else 128
    size = 1 << (bits - prefix_length)
    for sp in ordered(s for s in space if s.version == family):
        if sp.prefixlen > prefix_length:
            continue
        for a, b in free_gaps(sp, taken):
            start = (a + size - 1) // size * size
            if start + size - 1 <= b:
                return ipaddress.ip_network((start, prefix_length))
    return None


def largest_free(space_prefix, taken):
    """Shortest prefix length of a free aligned block in space_prefix, or None when it is full."""
    best = None
    cls = ipaddress.IPv4Address if space_prefix.version == 4 else ipaddress.IPv6Address
    for a, b in free_gaps(space_prefix, taken):
        for n in ipaddress.summarize_address_range(cls(a), cls(b)):
            best = n.prefixlen if best is None else min(best, n.prefixlen)
    return best


def overlapping(ranges, space):
    """The ranges (any host bits) that overlap a prefix of space, canonical and in address order."""
    out = []
    for r in ranges or []:
        rn = net(r, strict=False)
        if any(rn.version == s.version and rn.overlaps(s) for s in space) and rn not in out:
            out.append(rn)
    return [str(r) for r in ordered(out)]


def usage(state):
    """addressSpace of a network's state {prefixes, reserved, alloc}."""
    allocs = [net(c) for cs in (state.get("alloc") or {}).values() for c in cs]
    reserved = [net(c) for c in state.get("reserved") or []]
    out = []
    for p in state.get("prefixes") or []:
        sp = net(p)
        inside = [a for a in allocs if a.version == sp.version and a.subnet_of(sp)]
        row = {"prefix": str(sp), "family": sp.version, "allocations": len(inside)}
        lf = largest_free(sp, inside + reserved)
        if lf is not None:
            row["largestFree"] = lf
        out.append(row)
    return out


def holder_of(state, cidr, but=None):
    """(owner, its prefix) of an allocation overlapping cidr, other than but's; else None."""
    for owner, cs in (state.get("alloc") or {}).items():
        if owner == but:
            continue
        for c in cs:
            n = net(c)
            if n.version == cidr.version and n.overlaps(cidr):
                return owner, str(n)
    return None


def space_change(state, prefixes):
    """Refusals for setting the space to prefixes: allocations that would fall outside, overlapping prefixes."""
    new = [net(p) for p in prefixes]
    refused = []
    for i, a in enumerate(new):
        for b in new[i + 1:]:
            if a.version == b.version and a.overlaps(b):
                refused.append(f"prefixes {a} and {b} overlap")
    for owner, cs in sorted((state.get("alloc") or {}).items()):
        for c in cs:
            n = net(c)
            if not any(n.version == s.version and n.subnet_of(s) for s in new):
                refused.append(f"{n} of {owner} would lie outside the address space")
    return refused


def plan_allocation(state, owner, want, exclude):
    """What ipam-allocate answers for state (see the module docstring); never changes state.
    {prefixes, existing: true} | {prefixes, new: true} | {waiting} | {invalid}"""
    space = [net(p) for p in state.get("prefixes") or []]
    platform = [net(c) for c in state.get("reserved") or []]
    excl = [net(c, strict=False) for c in exclude or []]
    want = sorted(want or [], key=lambda w: int(w["family"]))
    held = (state.get("alloc") or {}).get(owner)
    if held:
        have = {net(c).version: net(c) for c in held}
        for w in want:
            h = have.get(int(w["family"]))
            ok = h is not None and (not w.get("prefix") or str(h) == str(net(w["prefix"]))) \
                and (not w.get("prefixLength") or h.prefixlen == int(w["prefixLength"]))
            if not ok:
                raise RuntimeError(f"the IPAM holds {', '.join(held)} for {owner}, which does not match "
                                   f"IPv{w['family']} {w.get('prefix') or '/' + str(w.get('prefixLength'))}")
        return {"prefixes": [str(h) for h in ordered(have.values())], "existing": True}
    if not space:
        return {"waiting": f"{owner.split('/', 1)[-1]}: the network has no address space in the IPAM yet"}
    taken = [net(c) for o, cs in (state.get("alloc") or {}).items() if o != owner for c in cs]
    got = []
    for w in want:
        fam = int(w["family"])
        fam_space = [s for s in space if s.version == fam]
        shown = ", ".join(str(s) for s in fam_space) or "none"
        if w.get("prefix"):
            p = net(w["prefix"])
            if p.version != fam:
                return {"invalid": f"{p} is not an IPv{fam} prefix"}
            if not any(p.subnet_of(s) for s in fam_space):
                return {"invalid": f"{p} lies outside the network's IPv{fam} address space ({shown})"}
            clash = next((x for x in platform if x.version == p.version and x.overlaps(p)), None)
            if clash is not None:
                return {"invalid": f"{p} overlaps the platform's range {clash}"}
            clash = next((x for x in excl if x.version == p.version and x.overlaps(p)), None)
            if clash is not None:
                return {"invalid": f"{p} overlaps the site's own range {clash}"}
            h = holder_of(state, p, but=owner)
            if h is not None:
                return {"waiting": f"{p} is held by {h[0].split('/', 1)[-1]} ({h[1]})"}
            got.append(p)
        else:
            plen = int(w["prefixLength"])
            p = first_fit(space, taken + platform + excl + got, fam, plen)
            if p is None:
                return {"waiting": f"no free /{plen} in the network's IPv{fam} address space ({shown})"}
            got.append(p)
        taken.append(got[-1])
    return {"prefixes": [str(p) for p in ordered(got)], "new": True}


# ---- ledger provider ----------------------------------------------------------------------------------------------

def network_status_path(network):
    _, ns, name = network.split("/", 2)
    return f"/apis/hybridsovereign.redhat/v1alpha1/namespaces/{ns}/hybridnetworks/{name}/status"


class LedgerProvider:
    def __init__(self, req):
        for k in ("api", "token", "namespace", "fabric"):
            if not req.get(k):
                raise RuntimeError(f"the ledger provider needs {k}")
        self.req = req
        self.api = shc_ledger.Api(req)

    def _networks(self, led):
        return led.setdefault("ipam", {})

    def _update(self, change):
        return shc_ledger.update(self.api, self.req, change)

    def publish(self, network):
        """Write the network's addressSpace to its status, read just before the write (best effort)."""
        try:
            cm, led = shc_ledger.read(self.api, self.req)
            state = ((led or {}).get("ipam") or {}).get(network)
            if cm is None or state is None:
                return False
            code, _ = self.api.call("PATCH", network_status_path(network), {"status": {"addressSpace": usage(state)}},
                                    content_type="application/merge-patch+json")
            return code == 200
        except Exception:  # noqa: BLE001 - the change stands; the network's next run writes the status
            return False

    def space(self, network, prefixes, reserved=None):
        canon = [str(net(p)) for p in prefixes]
        held = overlapping(reserved, [net(p) for p in canon])

        def change(led):
            nets = self._networks(led)
            state = nets.get(network) or {"prefixes": [], "alloc": {}}
            refused = space_change(state, canon)
            if refused:
                return {"refused": refused, "addressSpace": usage(state), "unchanged": True}
            unchanged = network in nets and state.get("prefixes") == canon and (state.get("reserved") or []) == held
            state = {"prefixes": canon, "reserved": held, "alloc": state.get("alloc") or {}}
            nets[network] = state
            return {"addressSpace": usage(state), "changed": not unchanged, "unchanged": unchanged}
        return self._update(change)

    def allocate(self, network, owner, want, exclude):
        def change(led):
            state = self._networks(led).get(network) or {}
            out = plan_allocation(state, owner, want, exclude)
            if out.get("new"):
                state.setdefault("alloc", {})[owner] = out["prefixes"]
            else:
                out["unchanged"] = True
            return out
        result = self._update(change)
        if result.get("new"):
            result["statusPublished"] = self.publish(network)
        return result

    def release(self, network, owner):
        def change(led):
            state = self._networks(led).get(network) or {}
            v = (state.get("alloc") or {}).pop(owner, None)
            return {"released": v, "unchanged": v is None}
        result = self._update(change)
        if result.get("released"):
            result["statusPublished"] = self.publish(network)
        return result

    def drop(self, network, holders):
        live = set(holders or [])

        def change(led):
            nets = self._networks(led)
            state = nets.get(network)
            if state is None:
                return {"dropped": False, "unchanged": True}
            blocking = {o: cs for o, cs in (state.get("alloc") or {}).items() if o in live}
            if blocking:
                return {"refused": [f"{', '.join(cs)} still held by {o}" for o, cs in sorted(blocking.items())],
                        "unchanged": True}
            del nets[network]
            return {"dropped": True, "stale": sorted(state.get("alloc") or {})}
        return self._update(change)

    def get(self, network):
        cm, led = shc_ledger.read(self.api, self.req)
        if cm is None:
            raise RuntimeError(f"ledger fabric-ledger-{self.req['fabric']} does not exist (the HybridFabric creates it)")
        state = ((led or {}).get("ipam") or {}).get(network)
        if state is None:
            return {"exists": False}
        return {"exists": True, "prefixes": state.get("prefixes") or [], "reserved": state.get("reserved") or [],
                "alloc": state.get("alloc") or {}, "addressSpace": usage(state)}


PROVIDERS = {"ledger": LedgerProvider}


def provider(req):
    name = req.get("provider") or "ledger"
    if name not in PROVIDERS:
        raise RuntimeError(f"IPAM provider {name} is not available")
    return PROVIDERS[name](req)


ACTIONS = {
    "ipam-space": lambda p, r: p.space(r["network"], r["prefixes"], r.get("reserved") or []),
    "ipam-allocate": lambda p, r: p.allocate(r["network"], r["owner"], r.get("want") or [], r.get("exclude") or []),
    "ipam-release": lambda p, r: p.release(r["network"], r["owner"]),
    "ipam-drop": lambda p, r: p.drop(r["network"], r.get("holders") or []),
    "ipam-get": lambda p, r: p.get(r["network"]),
}


def main():
    try:
        req = json.load(sys.stdin)
        fn = ACTIONS[req["action"]]
        print(json.dumps(fn(provider(req), req)))
        return 0
    except Exception as e:  # noqa: BLE001 - every failure becomes the job's message
        print(json.dumps({"error": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
