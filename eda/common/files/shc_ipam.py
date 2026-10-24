#!/usr/bin/env python3
"""IPAM of the HybridNetworks: the platform's address pools, each network's address space and its placements'
subnets (shc, design/P10-PLAN.md §3, design/IPV6-PLAN.md §2.7).

Reads one JSON request on stdin ({action, provider, ...}), prints one JSON result; {"error": ...} and exit 1 on
failure. A token arrives on stdin, never in argv. Refusals are results, never errors.

Two levels. Level 1: a platform pool (realization ipam.publicPools) gives each network asking for platform address
space (HybridNetwork spec.platformPrefixes) one block per family. Level 2: a network's space (its own prefixes and its
platform blocks) gives each placement its subnets.

Level 1 (`owner` = HybridNetwork/<ns>/<name>, `tenant` = the network's Entity):
  ipam-pool-ensure   {pool, family, prefixes, reserved?, blockLengths, defaultBlock, holdDownDays?, perTenantBlocks?,
                     perTenantOverrides?, defaultHoldDownDays?} (or {pools: {pool: {family, ...}}, defaultHoldDownDays?}
                     for several at once)   create or update pools from realization (never deletes one); refused
                     (refused: [...]) when an allocated block would fall outside it or under a reserved range, or the
                     pool overlaps another pool of the ledger; a malformed pool is an error
  ipam-pool-allocate {pool?, owner, tenant, want: {family, prefixLength?, prefix?} (or a list of one)}
                     an owner holding a block of the family gets it back (existing); else the pinned block or the
                     lowest free aligned block of the size (prefixLength, else the pool's defaultBlock) clear of the
                     pool's other blocks, its reserved ranges and the blocks in hold-down (new). Refusals store
                     nothing: {unavailable} (no pool of the family), {invalid} (size not offered, pinned block outside
                     the pool or on a reserved range, the owner holds another block), {waiting, waitingFor: allowance
                     | exhausted | holdDown | held, holdDownUntil?}. pool omitted = the ledger's pool of the family.
  ipam-pool-release  {pool?, owner}   give the owner's blocks back (every pool when pool is omitted); each released
                     block is held back for the pool's holdDownDays (released: {block: {at, owner}})
  ipam-pool-get      {pool?, owner?}  pools [{name, family, prefixes, blockLengths, defaultBlock, holdDownDays,
                     perTenantBlocks, blocksUsed, blocksFree, blocksHeldDown, largestFree}]; with owner, held
Level 2 (`network` = HybridNetwork/<ns>/<name>, `owner` = NetworkPlacement/<ns>/<name>):
  ipam-space    {network, prefixes, reserved?, platform?, mode6?, holders?, tenant?}   set the network's space
                (its own prefixes and its platform blocks; platform lists the blocks) and the platform's ranges never
                allocated in it (reserved, kept where they overlap the space). Refused (refused: [...], reason) when
                an allocation would fall outside the space (PrefixInUse; adding, removing an unused prefix and growing
                one in place work), when mode6 differs from the mode last realized while holders (the network's
                placements) exist (IPv6ModeInUse), or when tenant is given and an own IPv6 prefix overlaps one of
                another network of the same tenant (PrefixInUse); addressSpace, mode6
  ipam-mode-check {network, mode6, holders}   ipam-space's IPv6ModeInUse rule alone, changing nothing (run it before
                ipam-pool-allocate, so a refused mode change takes no block): {ok, mode6} or {refused, reason}
  ipam-allocate {network, owner, want: [{family, prefixLength?, prefix?, source?}], exclude: [cidr], external?}
                an owner holding prefixes gets them back (idempotent); else every family or none: a pinned prefix as
                given, a size at the lowest free aligned block of the network's prefixes of that family, clear of the
                network's other allocations, its reserved ranges and exclude (the site's ranges). want entries with
                source Provider are skipped; external [{family, prefix}] (provider-assigned subnets) is recorded
                apart from the space. Result {prefixes (IPv4 first), subnets [{family, prefix, source}]}, or
                {waiting: why} (no room, or a pinned prefix held by another owner), or {invalid: why} (a pinned
                prefix outside the space or on a reserved or excluded range, a space wholly inside the site's
                ranges); waiting and invalid store nothing
  ipam-external {network, owner, external: [{family, prefix}]}   record (replace) an owner's provider-assigned
                subnets
  ipam-release  {network, owner}   its allocations and provider-assigned subnets
  ipam-drop     {network, holders: [owner]}   remove the network's space; refused while a holder (a placement that
                still exists) has prefixes; allocations of other owners are dropped with it and reported (stale)
  ipam-get      {network}   space, platform blocks, mode6, allocations, provider-assigned subnets, addressSpace
addressSpace: per space prefix {prefix, family, source (Tenant | Platform), allocations, largestFree (shortest free
prefix length; absent when full)}: the reserved ranges are not free, the site ranges are not counted.

Providers (request field `provider`, default ledger):
  ledger  the fabric ledger, ConfigMap fabric-ledger-<fabric> (shc_ledger.py, compare-and-swap): section "ipam"
          {"HybridNetwork/<ns>/<name>": {"prefixes", "platform", "mode6", "tenant", "reserved", "alloc": {owner:
          [cidr]}, "external": {owner: [cidr]}}} and section "ipamPools" {pool: {"family", "prefixes", "reserved",
          "blockLengths", "defaultBlock", "holdDownDays", "perTenantBlocks", "perTenantOverrides", "alloc": {owner:
          [block]}, "tenants": {owner: tenant}, "released": {block: {"at", "owner"}}}}; missing keys read as empty.
          Requests also carry {api, token, namespace, fabric}. A level-2 allocate or release that changes the ledger
          writes the network's status.addressSpace, a pool change the fabric's status.realization.ipam.pools (best
          effort), so they follow every change and not only the owners' own runs.
  Another provider (e.g. NetBox) implements the same calls; the pure functions below do the arithmetic for all.
"""
import datetime
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


def network_of(family, start, prefix_length):
    """The network of family at integer start, built per family: ip_network((start, len)) would make an IPv4
    network of an IPv6 start below 2^32 with a length up to 32."""
    cls = ipaddress.IPv4Network if int(family) == 4 else ipaddress.IPv6Network
    return cls((start, prefix_length))


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
                return network_of(family, start, prefix_length)
    return None


def largest_free(space_prefix, taken):
    """Shortest prefix length of a free aligned block in space_prefix, or None when it is full."""
    best = None
    cls = ipaddress.IPv4Address if space_prefix.version == 4 else ipaddress.IPv6Address
    for a, b in free_gaps(space_prefix, taken):
        for n in ipaddress.summarize_address_range(cls(a), cls(b)):
            best = n.prefixlen if best is None else min(best, n.prefixlen)
    return best


def count_free(space, taken, prefix_length):
    """How many aligned /prefix_length blocks of the space prefixes overlap nothing in taken."""
    n = 0
    for sp in space:
        if sp.prefixlen > prefix_length:
            continue
        size = 1 << ((32 if sp.version == 4 else 128) - prefix_length)
        for a, b in free_gaps(sp, taken):
            start = (a + size - 1) // size * size
            if start + size - 1 <= b:
                n += (b - start + 1) // size
    return n


def overlapping(ranges, space):
    """The ranges (any host bits) that overlap a prefix of space, canonical and in address order."""
    out = []
    for r in ranges or []:
        rn = net(r, strict=False)
        if any(rn.version == s.version and rn.overlaps(s) for s in space) and rn not in out:
            out.append(rn)
    return [str(r) for r in ordered(out)]


def derive_mode6(prefixes, platform=None):
    """The network's IPv6 mode from its fields: a platform IPv6 block = Platform, an own IPv6 prefix = Tenant, else
    None (the same rule as the API's status.ipv6.mode)."""
    plat = {str(net(c)) for c in platform or []}
    if any(net(c).version == 6 for c in plat):
        return "Platform"
    if any(net(p).version == 6 and str(net(p)) not in plat for p in prefixes or []):
        return "Tenant"
    return "None"


def stored_mode6(state):
    """The IPv6 mode a network's state last realized (states written before modes existed: derived)."""
    state = state or {}
    return state.get("mode6") or derive_mode6(state.get("prefixes"), state.get("platform"))


def usage(state):
    """addressSpace of a network's state {prefixes, platform, reserved, alloc}."""
    allocs = [net(c) for cs in (state.get("alloc") or {}).values() for c in cs]
    reserved = [net(c) for c in state.get("reserved") or []]
    plat = {str(net(c)) for c in state.get("platform") or []}
    out = []
    for p in state.get("prefixes") or []:
        sp = net(p)
        inside = [a for a in allocs if a.version == sp.version and a.subnet_of(sp)]
        row = {"prefix": str(sp), "family": sp.version, "source": "Platform" if str(sp) in plat else "Tenant",
               "allocations": len(inside)}
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
    """Refusals for setting the space to prefixes: allocations that would fall outside, overlapping prefixes.
    Provider-assigned subnets (external) are not in the space and never refuse an edit."""
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


MODE_IN_USE = "the network's IPv6 mode can't change while it has placements; remove them or create a new network"


def mode_change(state, mode6, holders):
    """Refusal text (or None) for realizing mode6 on a network (its stored state) whose placements (holders) exist."""
    if mode6 is None or not holders or state is None:
        return None
    was = stored_mode6(state)
    if was == mode6:
        return None
    return f"{MODE_IN_USE} (it is {was}, the spec asks for {mode6})"


def own_v6(state):
    """A network state's own (tenant-source) IPv6 prefixes."""
    state = state or {}
    plat = {str(net(c)) for c in state.get("platform") or []}
    return [net(p) for p in state.get("prefixes") or [] if net(p).version == 6 and str(net(p)) not in plat]


def same_tenant(network, tenant, other, other_state):
    """Whether other (a network key) belongs to the tenant of network: its recorded tenant, else (a state written
    before tenants were recorded) the same namespace."""
    t = (other_state or {}).get("tenant")
    if t:
        return t == tenant
    return other.split("/")[1:2] == network.split("/")[1:2]


def tenant_overlaps(networks, network, tenant, prefixes, platform):
    """Refusals: an own IPv6 prefix of network overlapping an own IPv6 prefix of another network of the same tenant
    (networks = the ledger's ipam section). Networks of other tenants may overlap or repeat (separate VRFs)."""
    mine = own_v6({"prefixes": prefixes, "platform": platform})
    out = []
    for other, st in sorted((networks or {}).items()):
        if other == network or not same_tenant(network, tenant, other, st):
            continue
        for q in own_v6(st):
            for p in mine:
                if p.overlaps(q):
                    out.append(f"{p} overlaps {q} of network {other.split('/')[-1]}")
    return out


def subnets_of(prefixes, external):
    """status.subnets rows: the space's allocations (source Network) and provider-assigned subnets (Provider)."""
    rows = [(net(c), "Network") for c in prefixes or []] + [(net(c), "Provider") for c in external or []]
    rows.sort(key=lambda r: (r[0].version, int(r[0].network_address), r[0].prefixlen))
    return [{"family": n.version, "prefix": str(n), "source": src} for n, src in rows]


def external_entries(external):
    """Canonical provider-assigned prefixes from [{family, prefix}] (or plain strings), in address order."""
    out = []
    for e in external or []:
        p = net(e["prefix"] if isinstance(e, dict) else e)
        if isinstance(e, dict) and e.get("family") and int(e["family"]) != p.version:
            raise RuntimeError(f"{p} is not an IPv{e['family']} prefix")
        if p not in out:
            out.append(p)
    return [str(n) for n in ordered(out)]


def plan_allocation(state, owner, want, exclude):
    """What ipam-allocate answers for state (see the module docstring); never changes state.
    {prefixes, existing: true} | {prefixes, new: true} | {waiting} | {invalid}"""
    space = [net(p) for p in state.get("prefixes") or []]
    platform = [net(c) for c in state.get("reserved") or []]
    excl = [net(c, strict=False) for c in exclude or []]
    want = sorted((w for w in want or [] if (w.get("source") or "Network") != "Provider"),
                  key=lambda w: int(w["family"]))
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
    if not want:
        return {"prefixes": [], "existing": True}
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
            inside = [next((x for x in excl if x.version == s.version and s.subnet_of(x)), None) for s in fam_space]
            if fam_space and all(x is not None for x in inside):
                return {"invalid": f"the network's IPv{fam} address space ({shown}) lies inside the site's own "
                                   f"range {inside[0]}"}
            p = first_fit(space, taken + platform + excl + got, fam, plen)
            if p is None:
                return {"waiting": f"no free /{plen} in the network's IPv{fam} address space ({shown})"}
            got.append(p)
        taken.append(got[-1])
    return {"prefixes": [str(p) for p in ordered(got)], "new": True}


# ---- pure core, level 1: platform pools ---------------------------------------------------------------------------

DEFAULT_HOLD_DOWN_DAYS = 7


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def fmt_ts(t):
    return t.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s):
    t = datetime.datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=datetime.timezone.utc)


def family_name(fam):
    return f"IPv{int(fam)}"


def pool_config(req, default_hold_down=DEFAULT_HOLD_DOWN_DAYS):
    """A pool's configuration from realization, canonical; ValueError on a malformed one."""
    name = req.get("pool")
    fam = int(req.get("family") or 0)
    if fam not in (4, 6):
        raise ValueError(f"pool {name}: family must be 4 or 6")
    prefixes = [net(p) for p in req.get("prefixes") or []]
    if not prefixes:
        raise ValueError(f"pool {name}: no prefixes")
    for p in prefixes:
        if p.version != fam:
            raise ValueError(f"pool {name}: {p} is not an {family_name(fam)} prefix")
    for i, a in enumerate(prefixes):
        for b in prefixes[i + 1:]:
            if a.overlaps(b):
                raise ValueError(f"pool {name}: prefixes {a} and {b} overlap")
    lengths = sorted({int(x) for x in req.get("blockLengths") or []})
    if not lengths:
        raise ValueError(f"pool {name}: no blockLengths")
    shortest = min(p.prefixlen for p in prefixes)
    longest = 64 if fam == 6 else 30
    for n in lengths:
        if n < shortest or n > longest or (fam == 6 and n % 4):
            raise ValueError(f"pool {name}: block length /{n} must lie in /{shortest}-/{longest}"
                             + (" in steps of 4" if fam == 6 else ""))
    default = int(req.get("defaultBlock") or lengths[0])
    if default not in lengths:
        raise ValueError(f"pool {name}: defaultBlock /{default} is not one of the blockLengths")
    hold = req.get("holdDownDays")
    hold = int(default_hold_down) if hold is None or hold == "" else int(hold)
    per = req.get("perTenantBlocks")
    per = None if per is None or per == "" else int(per)
    overrides = {str(k): int(v) for k, v in (req.get("perTenantOverrides") or {}).items()}
    if hold < 0 or (per is not None and per < 0) or any(v < 0 for v in overrides.values()):
        raise ValueError(f"pool {name}: holdDownDays, perTenantBlocks and overrides must not be negative")
    return {"family": fam, "prefixes": [str(p) for p in ordered(prefixes)],
            "reserved": [str(r) for r in ordered(net(x, strict=False) for x in req.get("reserved") or [])],
            "blockLengths": lengths, "defaultBlock": default, "holdDownDays": hold, "perTenantBlocks": per,
            "perTenantOverrides": overrides}


def hold_down_days(pool):
    d = pool.get("holdDownDays")
    return DEFAULT_HOLD_DOWN_DAYS if d is None else int(d)


def released_entries(pool):
    """{block: (at, owner)} of a pool's release stamps (a bare timestamp is read too)."""
    out = {}
    for block, v in (pool.get("released") or {}).items():
        if isinstance(v, dict):
            out[block] = (parse_ts(v.get("at")), v.get("owner") or "")
        else:
            out[block] = (parse_ts(v), "")
    return out


def held_down(pool, at):
    """{block network: until} for the released blocks still in hold-down at `at`."""
    days = hold_down_days(pool)
    out = {}
    for block, (t, _) in released_entries(pool).items():
        until = t + datetime.timedelta(days=days)
        if until > at:
            out[net(block)] = until
    return out


def prune_released(pool, at):
    """Drop the release stamps whose hold-down is over and those of blocks allocated again."""
    keep = {str(b) for b in held_down(pool, at)}
    allocated = {c for cs in (pool.get("alloc") or {}).values() for c in cs}
    rel = pool.get("released") or {}
    for block in list(rel):
        if block not in keep or block in allocated:
            del rel[block]
    pool["released"] = rel


def pool_blocks(pool, but=None):
    return [net(c) for o, cs in (pool.get("alloc") or {}).items() if o != but for c in cs]


def allowance_of(pool, tenant):
    """The tenant's block allowance in the pool, None = no limit."""
    per = (pool.get("perTenantOverrides") or {}).get(tenant, pool.get("perTenantBlocks"))
    return None if per is None else int(per)


def tenant_blocks(pool, tenant, but=None):
    tenants = pool.get("tenants") or {}
    return sum(len(cs) for o, cs in (pool.get("alloc") or {}).items() if o != but and tenants.get(o) == tenant)


def pool_change(pool, cfg, others):
    """Refusals for setting a pool to cfg: allocated blocks that would fall outside it or under a reserved range,
    overlap with another pool of the ledger (others = {name: pool})."""
    new = [net(p) for p in cfg["prefixes"]]
    reserved = [net(r) for r in cfg["reserved"]]
    refused = []
    for name, other in sorted((others or {}).items()):
        for q in other.get("prefixes") or []:
            q = net(q)
            for p in new:
                if p.version == q.version and p.overlaps(q):
                    refused.append(f"{p} overlaps {q} of pool {name}")
    for owner, cs in sorted(((pool or {}).get("alloc") or {}).items()):
        for c in cs:
            n = net(c)
            if not any(n.version == s.version and n.subnet_of(s) for s in new):
                refused.append(f"{n} of {owner} would lie outside the pool")
            clash = next((r for r in reserved if r.version == n.version and r.overlaps(n)), None)
            if clash is not None:
                refused.append(f"{n} of {owner} would lie in the reserved range {clash}")
    return refused


def pool_usage(name, pool, at):
    """The pool's row for status: sizes offered, blocks used / free (of the default size) / in hold-down."""
    space = [net(p) for p in pool.get("prefixes") or []]
    used = pool_blocks(pool)
    down = list(held_down(pool, at))
    reserved = [net(r) for r in pool.get("reserved") or []]
    taken = used + reserved + down
    default = int(pool.get("defaultBlock") or 0)
    row = {"name": name, "family": pool.get("family"), "prefixes": list(pool.get("prefixes") or []),
           "blockLengths": list(pool.get("blockLengths") or []), "defaultBlock": pool.get("defaultBlock"),
           "holdDownDays": pool.get("holdDownDays"), "perTenantBlocks": pool.get("perTenantBlocks"),
           "blocksUsed": len(used), "blocksHeldDown": len(down),
           "blocksFree": count_free(space, taken, default) if default else 0}
    frees = [lf for lf in (largest_free(sp, taken) for sp in space) if lf is not None]
    if frees:
        row["largestFree"] = min(frees)
    return row


def plan_pool_allocation(pool, owner, tenant, want, at):
    """What ipam-pool-allocate answers for one pool at time `at` (see the module docstring); never changes the pool.
    {prefixes, existing} | {prefixes, new} | {unavailable} | {invalid} | {waiting, waitingFor, holdDownUntil?}"""
    fam = int(want.get("family") or pool.get("family"))
    fname = family_name(fam)
    if fam != int(pool.get("family")):
        return {"unavailable": f"the fabric has no platform {fname} address pool"}
    pool_space = [net(p) for p in pool.get("prefixes") or []]
    pin = net(want["prefix"]) if want.get("prefix") else None
    if pin is not None and pin.version != fam:
        return {"invalid": f"{pin} is not an {fname} prefix"}
    asked_len = int(want["prefixLength"]) if want.get("prefixLength") else (pin.prefixlen if pin is not None else None)
    held = [net(c) for c in (pool.get("alloc") or {}).get(owner) or [] if net(c).version == fam]
    if held:
        h = held[0]
        if (pin is not None and pin != h) or (asked_len is not None and asked_len != h.prefixlen):
            asked = str(pin) if pin is not None else f"a /{asked_len}"
            return {"invalid": f"the network holds {h}; release it before asking for {asked}"}
        return {"prefixes": [str(h)], "existing": True}
    plen = asked_len if asked_len is not None else int(pool.get("defaultBlock"))
    if plen not in (pool.get("blockLengths") or []):
        offered = ", ".join(f"/{n}" for n in pool.get("blockLengths") or [])
        return {"invalid": f"this fabric offers platform {fname} blocks of {offered}"}
    limit = allowance_of(pool, tenant)
    if limit is not None:
        n = tenant_blocks(pool, tenant, but=owner)
        if n >= limit:
            return {"waiting": f"the tenant's platform {fname} allowance is used ({n} of {limit} blocks)",
                    "waitingFor": "allowance"}
    reserved = [net(r) for r in pool.get("reserved") or []]
    others = pool_blocks(pool, but=owner)
    down = held_down(pool, at)
    if pin is not None:
        if not any(pin.subnet_of(s) for s in pool_space):
            return {"invalid": f"{pin} lies outside the platform's {fname} address pool "
                               f"({', '.join(pool.get('prefixes') or [])})"}
        clash = next((r for r in reserved if r.overlaps(pin)), None)
        if clash is not None:
            return {"invalid": f"{pin} overlaps a range the platform keeps ({clash})"}
        if any(b.overlaps(pin) for b in others):
            return {"waiting": f"{pin} is assigned to another network", "waitingFor": "held"}
        until = [u for b, u in down.items() if b.overlaps(pin)]
        if until:
            u = fmt_ts(max(until))
            return {"waiting": f"{pin} was released recently and is held back until {u}", "waitingFor": "holdDown",
                    "holdDownUntil": u}
        return {"prefixes": [str(pin)], "new": True}
    p = first_fit(pool_space, others + reserved + list(down), fam, plen)
    if p is not None:
        return {"prefixes": [str(p)], "new": True}
    later = first_fit(pool_space, others + reserved, fam, plen)
    if later is not None:
        u = fmt_ts(max(t for b, t in down.items() if b.overlaps(later)))
        return {"waiting": f"no free /{plen} in the platform's {fname} address pool until {u}: released blocks are "
                           f"held back for {hold_down_days(pool)} days", "waitingFor": "holdDown", "holdDownUntil": u}
    return {"waiting": f"no free /{plen} in the platform's {fname} address pool", "waitingFor": "exhausted"}


def release_from_pool(pool, owner, at):
    """Remove owner's blocks from pool and stamp their hold-down: (released blocks or None, until or None)."""
    blocks = (pool.get("alloc") or {}).pop(owner, None)
    (pool.get("tenants") or {}).pop(owner, None)
    if not blocks:
        return None, None
    rel = pool.setdefault("released", {})
    for b in blocks:
        rel[b] = {"at": fmt_ts(at), "owner": owner}
    prune_released(pool, at)
    return blocks, fmt_ts(at + datetime.timedelta(days=hold_down_days(pool)))


def choose_pool(pools, name, family):
    """(name, pool) the request means: the named pool, else the only pool of the family; (None, None) if none."""
    if name:
        return (name, pools[name]) if name in pools else (None, None)
    match = sorted(n for n, p in pools.items() if int(p.get("family") or 0) == int(family))
    if len(match) > 1:
        raise RuntimeError(f"the fabric has {len(match)} {family_name(family)} pools ({', '.join(match)}); name one")
    return (match[0], pools[match[0]]) if match else (None, None)


# ---- ledger provider ----------------------------------------------------------------------------------------------

def network_status_path(network):
    _, ns, name = network.split("/", 2)
    return f"/apis/hybridsovereign.redhat/v1alpha1/namespaces/{ns}/hybridnetworks/{name}/status"


def fabric_status_path(namespace, fabric):
    return f"/apis/hybridsovereign.redhat/v1alpha1/namespaces/{namespace}/hybridfabrics/{fabric}/status"


SPACE_KEYS = ("prefixes", "reserved", "platform")


class LedgerProvider:
    def __init__(self, req):
        for k in ("api", "token", "namespace", "fabric"):
            if not req.get(k):
                raise RuntimeError(f"the ledger provider needs {k}")
        self.req = req
        self.api = shc_ledger.Api(req)

    def _networks(self, led):
        return led.setdefault("ipam", {})

    def _pools(self, led):
        return led.setdefault("ipamPools", {})

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

    def publish_pools(self):
        """Write the pools' usage to the fabric's status.realization.ipam.pools, read just before (best effort)."""
        try:
            cm, led = shc_ledger.read(self.api, self.req)
            if cm is None:
                return False
            at = utcnow()
            rows = [pool_usage(n, p, at) for n, p in sorted(((led or {}).get("ipamPools") or {}).items())]
            code, _ = self.api.call("PATCH", fabric_status_path(self.req["namespace"], self.req["fabric"]),
                                    {"status": {"realization": {"ipam": {"pools": rows}}}},
                                    content_type="application/merge-patch+json")
            return code == 200
        except Exception:  # noqa: BLE001 - the change stands; the next pool change or fabric run writes it
            return False

    # -- level 2

    def space(self, network, prefixes, reserved=None, platform=None, mode6=None, holders=None, tenant=None):
        canon = [str(net(p)) for p in prefixes]
        plat = [str(n) for n in ordered(net(c) for c in platform or [])]
        missing = [c for c in plat if c not in canon]
        if missing:
            raise RuntimeError(f"platform blocks {', '.join(missing)} are not in the prefixes")
        held = overlapping(reserved, [net(p) for p in canon])

        def change(led):
            nets = self._networks(led)
            old = nets.get(network)
            state = old or {"prefixes": [], "alloc": {}}
            mc = mode_change(old, mode6, holders)
            if mc:
                return {"refused": [mc], "reason": "IPv6ModeInUse", "addressSpace": usage(state),
                        "mode6": stored_mode6(state), "unchanged": True}
            refused = space_change(state, canon)
            if tenant:
                refused += tenant_overlaps(nets, network, tenant, canon, plat)
            if refused:
                return {"refused": refused, "reason": "PrefixInUse", "addressSpace": usage(state),
                        "mode6": stored_mode6(state), "unchanged": True}
            new = {"prefixes": canon, "reserved": held, "alloc": state.get("alloc") or {},
                   "mode6": mode6 or derive_mode6(canon, plat)}
            if plat:
                new["platform"] = plat
            if tenant or state.get("tenant"):
                new["tenant"] = tenant or state.get("tenant")
            if state.get("external"):
                new["external"] = state["external"]
            changed = old is None or any((state.get(k) or None) != (new.get(k) or None) for k in SPACE_KEYS)
            same = old is not None and not changed and all(
                (state.get(k) or None) == (new.get(k) or None) for k in ("mode6", "tenant"))
            nets[network] = new
            return {"addressSpace": usage(new), "mode6": new["mode6"], "changed": changed, "unchanged": same}
        return self._update(change)

    def mode_check(self, network, mode6, holders):
        cm, led = shc_ledger.read(self.api, self.req)
        if cm is None:
            raise RuntimeError(f"ledger fabric-ledger-{self.req['fabric']} does not exist (the HybridFabric creates it)")
        state = ((led or {}).get("ipam") or {}).get(network)
        mc = mode_change(state, mode6, holders)
        if mc:
            return {"refused": [mc], "reason": "IPv6ModeInUse", "mode6": stored_mode6(state)}
        return {"ok": True, "mode6": stored_mode6(state) if state is not None else None}

    def allocate(self, network, owner, want, exclude, external=None):
        ext = external_entries(external) if external is not None else None

        def change(led):
            nets = self._networks(led)
            state = nets.get(network)
            out = plan_allocation(state or {}, owner, want, exclude)
            if "prefixes" not in out:
                out["unchanged"] = True
                return out
            stored = False
            if out.get("new"):
                state.setdefault("alloc", {})[owner] = out["prefixes"]
                stored = True
            if ext is not None and ((state or {}).get("external") or {}).get(owner, []) != ext:
                if state is None:
                    raise RuntimeError(f"{network} has no address space in the IPAM")
                if ext:
                    state.setdefault("external", {})[owner] = ext
                else:
                    state["external"].pop(owner, None)
                out["externalChanged"] = True
                stored = True
            out["subnets"] = subnets_of(out["prefixes"], ((state or {}).get("external") or {}).get(owner))
            if not stored:
                out["unchanged"] = True
            return out
        result = self._update(change)
        if result.get("new"):
            result["statusPublished"] = self.publish(network)
        return result

    def set_external(self, network, owner, external):
        ext = external_entries(external)

        def change(led):
            state = self._networks(led).get(network)
            if state is None:
                raise RuntimeError(f"{network} has no address space in the IPAM")
            cur = (state.get("external") or {}).get(owner) or []
            out = {"changed": cur != ext, "subnets": subnets_of((state.get("alloc") or {}).get(owner), ext)}
            if cur == ext:
                out["unchanged"] = True
            elif ext:
                state.setdefault("external", {})[owner] = ext
            else:
                state["external"].pop(owner, None)
            return out
        return self._update(change)

    def release(self, network, owner):
        def change(led):
            state = self._networks(led).get(network) or {}
            v = (state.get("alloc") or {}).pop(owner, None)
            x = (state.get("external") or {}).pop(owner, None)
            out = {"released": v, "unchanged": v is None and x is None}
            if x:
                out["external"] = x
            return out
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
            blocking = {}
            for section in ("alloc", "external"):
                for o, cs in (state.get(section) or {}).items():
                    if o in live:
                        blocking.setdefault(o, []).extend(cs)
            if blocking:
                return {"refused": [f"{', '.join(cs)} still held by {o}" for o, cs in sorted(blocking.items())],
                        "unchanged": True}
            del nets[network]
            return {"dropped": True, "stale": sorted(set(state.get("alloc") or {}) | set(state.get("external") or {}))}
        return self._update(change)

    def get(self, network):
        cm, led = shc_ledger.read(self.api, self.req)
        if cm is None:
            raise RuntimeError(f"ledger fabric-ledger-{self.req['fabric']} does not exist (the HybridFabric creates it)")
        state = ((led or {}).get("ipam") or {}).get(network)
        if state is None:
            return {"exists": False}
        return {"exists": True, "prefixes": state.get("prefixes") or [], "platform": state.get("platform") or [],
                "mode6": stored_mode6(state), "tenant": state.get("tenant") or "",
                "reserved": state.get("reserved") or [], "alloc": state.get("alloc") or {},
                "external": state.get("external") or {}, "addressSpace": usage(state)}

    # -- level 1

    def pool_ensure(self, req):
        """One pool (pool + its configuration) or several ({pools: {name: configuration}}) in one compare-and-swap."""
        dflt = req.get("defaultHoldDownDays")
        dflt = DEFAULT_HOLD_DOWN_DAYS if dflt is None or dflt == "" else int(dflt)
        many = req.get("pools") is not None
        if many:
            wanted = {str(n): {**(c or {}), "pool": str(n)} for n, c in (req.get("pools") or {}).items()}
        elif req.get("pool"):
            wanted = {req["pool"]: req}
        else:
            raise RuntimeError("ipam-pool-ensure needs pool (or pools)")
        try:
            cfgs = {n: pool_config(c, dflt) for n, c in sorted(wanted.items())}
        except ValueError as e:
            raise RuntimeError(f"realization ipam.publicPools: {e}") from e

        def change(led):
            at = utcnow()
            pools = self._pools(led)
            refused, rows, changed = [], [], False
            for name, cfg in cfgs.items():
                cur = pools.get(name) or {}
                others = {n: (cfgs.get(n) or p) for n, p in pools.items() if n != name}
                others.update({n: c for n, c in cfgs.items() if n != name and n not in pools})
                r = pool_change(cur, cfg, others)
                if r:
                    refused += [f"pool {name}: {x}" for x in r]
                    continue
                new = {**cfg, "alloc": cur.get("alloc") or {}, "tenants": cur.get("tenants") or {},
                       "released": dict(cur.get("released") or {})}
                prune_released(new, at)
                if name not in pools or json.dumps(cur, sort_keys=True) != json.dumps(new, sort_keys=True):
                    changed = True
                pools[name] = new
                rows.append(pool_usage(name, new, at))
            if refused:
                return {"refused": refused, "unchanged": True}
            out = {"changed": changed, "unchanged": not changed}
            if many:
                out["pools"] = rows
            else:
                out.update({"pool": rows[0]["name"], "usage": rows[0]})
            return out
        result = self._update(change)
        if result.get("changed"):
            result["statusPublished"] = self.publish_pools()
        return result

    def pool_allocate(self, name, owner, tenant, want):
        if isinstance(want, list):
            if len(want) != 1:
                raise RuntimeError("ipam-pool-allocate takes one want entry (one block per family)")
            want = want[0]
        want = want or {}
        if not tenant:
            raise RuntimeError("ipam-pool-allocate needs tenant (the network's Entity)")

        def change(led):
            at = utcnow()
            fam = int(want.get("family") or 6)
            pname, pool = choose_pool(self._pools(led), name, fam)
            if pool is None:
                return {"unavailable": f"the fabric has no platform {family_name(fam)} address pool", "unchanged": True}
            out = plan_pool_allocation(pool, owner, tenant, want, at)
            out["pool"] = pname
            if out.get("new"):
                pool.setdefault("alloc", {})[owner] = out["prefixes"]
                pool.setdefault("tenants", {})[owner] = tenant
                prune_released(pool, at)
            else:
                out["unchanged"] = True
            return out
        result = self._update(change)
        if result.get("new"):
            result["statusPublished"] = self.publish_pools()
        return result

    def pool_release(self, name, owner):
        def change(led):
            at = utcnow()
            out = {"released": None, "unchanged": True}
            for pname, pool in sorted(self._pools(led).items()):
                if name and pname != name:
                    continue
                blocks, until = release_from_pool(pool, owner, at)
                if blocks:
                    out = {"released": (out["released"] or []) + blocks, "pool": pname, "holdDownUntil": until}
            return out
        result = self._update(change)
        if result.get("released"):
            result["statusPublished"] = self.publish_pools()
        return result

    def pool_get(self, name=None, owner=None):
        cm, led = shc_ledger.read(self.api, self.req)
        if cm is None:
            raise RuntimeError(f"ledger fabric-ledger-{self.req['fabric']} does not exist (the HybridFabric creates it)")
        at = utcnow()
        pools = (led or {}).get("ipamPools") or {}
        out = {"pools": [pool_usage(n, p, at) for n, p in sorted(pools.items()) if not name or n == name]}
        if owner:
            out["held"] = {n: list((p.get("alloc") or {}).get(owner) or []) for n, p in sorted(pools.items())
                           if (p.get("alloc") or {}).get(owner)}
        return out


PROVIDERS = {"ledger": LedgerProvider}


def provider(req):
    name = req.get("provider") or "ledger"
    if name not in PROVIDERS:
        raise RuntimeError(f"IPAM provider {name} is not available")
    return PROVIDERS[name](req)


ACTIONS = {
    "ipam-space": lambda p, r: p.space(r["network"], r["prefixes"], r.get("reserved") or [], r.get("platform") or [],
                                       r.get("mode6"), r.get("holders"), r.get("tenant")),
    "ipam-mode-check": lambda p, r: p.mode_check(r["network"], r.get("mode6"), r.get("holders")),
    "ipam-allocate": lambda p, r: p.allocate(r["network"], r["owner"], r.get("want") or [], r.get("exclude") or [],
                                             r.get("external")),
    "ipam-external": lambda p, r: p.set_external(r["network"], r["owner"], r.get("external") or []),
    "ipam-release": lambda p, r: p.release(r["network"], r["owner"]),
    "ipam-drop": lambda p, r: p.drop(r["network"], r.get("holders") or []),
    "ipam-get": lambda p, r: p.get(r["network"]),
    "ipam-pool-ensure": lambda p, r: p.pool_ensure(r),
    "ipam-pool-allocate": lambda p, r: p.pool_allocate(r.get("pool"), r["owner"], r.get("tenant"), r.get("want")),
    "ipam-pool-release": lambda p, r: p.pool_release(r.get("pool"), r["owner"]),
    "ipam-pool-get": lambda p, r: p.pool_get(r.get("pool"), r.get("owner")),
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
