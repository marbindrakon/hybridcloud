"""IPAM of the HybridNetworks (eda/common/files/shc_ipam.py): the level-2 allocator (network space -> placement
subnets), the level-1 platform pools (pool -> network block: first fit, pinning, offered sizes, per-tenant
allowance, hold-down), the IPv6 mode rules, provider-assigned subnets and the ledger provider's compare-and-swap,
against an in-memory API. Prefixes are documentation and ULA space only."""
import datetime
import ipaddress
import json
import os
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FILES = os.path.join(ROOT, "eda", "common", "files")
sys.path.insert(0, FILES)
import shc_ipam as I  # noqa: E402
import shc_ledger  # noqa: E402

N = ipaddress.ip_network
OWN = "NetworkPlacement/entity-acme/"
T0 = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.timezone.utc)
POOL_PREFIX = "2001:db8:4000::/50"


def st(prefixes, **alloc):
    return {"prefixes": list(prefixes), "alloc": {OWN + k: list(v) for k, v in alloc.items()}}


def v4(n):
    return [{"family": 4, "prefixLength": n}]


def hn(name, ns="entity-acme"):
    return f"HybridNetwork/{ns}/{name}"


def pool(prefixes=(POOL_PREFIX,), lengths=(56, 60), default=56, hold=7, per=None, **extra):
    cfg = I.pool_config({"pool": "public-v6", "family": 6, "prefixes": list(prefixes), "blockLengths": list(lengths),
                         "defaultBlock": default, "holdDownDays": hold, "perTenantBlocks": per, **extra})
    return {**cfg, "alloc": {}, "tenants": {}, "released": {}}


def take(p, owner, tenant, want=None, at=T0):
    """Allocate as the provider does (plan, then store)."""
    out = I.plan_pool_allocation(p, owner, tenant, want or {"family": 6}, at)
    if out.get("new"):
        p["alloc"][owner] = out["prefixes"]
        p["tenants"][owner] = tenant
        I.prune_released(p, at)
    return out


# ---- level 2 (P10 rules, kept) ------------------------------------------------------------------------------------

class Level2(unittest.TestCase):
    def test_first_fit_lowest_first(self):
        s = st(["10.60.0.0/16"])
        out = I.plan_allocation(s, OWN + "a", v4(24), [])
        self.assertEqual(out, {"prefixes": ["10.60.0.0/24"], "new": True})
        s["alloc"][OWN + "a"] = out["prefixes"]
        self.assertEqual(I.plan_allocation(s, OWN + "b", v4(24), [])["prefixes"], ["10.60.1.0/24"])

    def test_first_fit_alignment_after_small_block(self):
        s = st(["10.60.0.0/16"], a=["10.60.0.0/29"])
        self.assertEqual(I.plan_allocation(s, OWN + "b", v4(24), [])["prefixes"], ["10.60.1.0/24"])
        self.assertEqual(I.plan_allocation(s, OWN + "c", v4(29), [])["prefixes"], ["10.60.0.8/29"])

    def test_exclude_stepped_around(self):
        s = st(["10.128.0.0/12"])
        excl = ["10.128.0.0/14", "172.30.0.0/16", "10.131.255.0/24"]
        self.assertEqual(I.plan_allocation(s, OWN + "a", v4(24), excl)["prefixes"], ["10.132.0.0/24"])

    def test_no_room_waits_and_stores_nothing(self):
        s = st(["10.60.0.0/23"], a=["10.60.0.0/24"], b=["10.60.1.0/24"])
        out = I.plan_allocation(s, OWN + "c", v4(24), [])
        self.assertIn("no free /24", out["waiting"])
        self.assertNotIn(OWN + "c", s["alloc"])

    def test_idempotent_and_mismatch(self):
        s = st(["10.60.0.0/16"], a=["10.60.0.0/24"])
        self.assertEqual(I.plan_allocation(s, OWN + "a", v4(24), []), {"prefixes": ["10.60.0.0/24"], "existing": True})
        with self.assertRaises(RuntimeError):
            I.plan_allocation(s, OWN + "a", v4(25), [])

    def test_pinned_rules(self):
        s = st(["10.60.0.0/16"], a=["10.60.0.0/23"])
        self.assertEqual(I.plan_allocation(s, OWN + "b", [{"family": 4, "prefix": "10.60.5.0/24"}], [])["prefixes"],
                         ["10.60.5.0/24"])
        self.assertIn("entity-acme/a", I.plan_allocation(s, OWN + "b", [{"family": 4, "prefix": "10.60.1.0/24"}],
                                                         [])["waiting"])
        self.assertIn("outside", I.plan_allocation(s, OWN + "b", [{"family": 4, "prefix": "10.61.0.0/24"}],
                                                   [])["invalid"])

    def test_dual_stack_all_or_nothing(self):
        s = st(["10.60.0.0/16", "fd12:3456:789a::/48"])
        want = [{"family": 6, "prefixLength": 64}, {"family": 4, "prefixLength": 24}]
        self.assertEqual(I.plan_allocation(s, OWN + "a", want, [])["prefixes"], ["10.60.0.0/24", "fd12:3456:789a::/64"])
        self.assertIn("IPv6", I.plan_allocation(st(["10.60.0.0/16"]), OWN + "a", want, [])["waiting"])

    def test_space_change_rules(self):
        s = st(["10.60.0.0/16", "10.70.0.0/16"], a=["10.60.0.0/24"])
        self.assertEqual(I.space_change(s, ["10.60.0.0/16"]), [])
        self.assertEqual(I.space_change(s, ["10.60.0.0/15"]), [])
        self.assertIn("10.60.0.0/24 of NetworkPlacement/entity-acme/a", I.space_change(s, ["10.70.0.0/16"])[0])

    def test_usage_has_source(self):
        s = st(["10.60.0.0/16", "2001:db8:4000::/56"], a=["10.60.0.0/24", "2001:db8:4000::/64"])
        s["platform"] = ["2001:db8:4000::/56"]
        u = I.usage(s)
        self.assertEqual(u[0], {"prefix": "10.60.0.0/16", "family": 4, "source": "Tenant", "allocations": 1,
                                "largestFree": 17})
        self.assertEqual(u[1], {"prefix": "2001:db8:4000::/56", "family": 6, "source": "Platform", "allocations": 1,
                                "largestFree": 57})


class FirstFitFamily6(unittest.TestCase):
    """R-api B19: an IPv6 start below 2^32 with a length up to 32 came back as an IPv4 network."""

    def test_low_ipv6_start_stays_ipv6(self):
        got = I.first_fit([N("::/16")], [], 6, 32)
        self.assertEqual(got, N("::/32"))
        self.assertEqual(got.version, 6)
        got = I.first_fit([N("::/16")], [N("::/32")], 6, 32)
        self.assertEqual(str(got), "0:1::/32")
        self.assertEqual(got.version, 6)

    def test_ipv4_unchanged(self):
        self.assertEqual(I.first_fit([N("10.0.0.0/8")], [], 4, 16), N("10.0.0.0/16"))

    def test_network_of(self):
        self.assertEqual(I.network_of(6, 5, 128), N("::5/128"))
        self.assertEqual(I.network_of(4, 5, 32), N("0.0.0.5/32"))


class PlacementIPv6(unittest.TestCase):
    """Per-family placement subnets from the network's IPv6 space: /64 on metal and OpenStack, /60 or /56 on AWS."""

    def setUp(self):
        self.s = st(["10.60.0.0/16", "2001:db8:4000::/56"])
        self.s["platform"] = ["2001:db8:4000::/56"]

    def test_metal_64_lowest_free(self):
        out = I.plan_allocation(self.s, OWN + "a", [{"family": 4, "prefixLength": 24}, {"family": 6, "prefixLength": 64}],
                                [])
        self.assertEqual(out["prefixes"], ["10.60.0.0/24", "2001:db8:4000::/64"])
        self.s["alloc"][OWN + "a"] = out["prefixes"]
        self.assertEqual(I.plan_allocation(self.s, OWN + "b", [{"family": 6, "prefixLength": 64}], [])["prefixes"],
                         ["2001:db8:4000:1::/64"])

    def test_aws_60_after_a_64(self):
        self.s["alloc"][OWN + "a"] = ["2001:db8:4000::/64"]
        out = I.plan_allocation(self.s, OWN + "b", [{"family": 6, "prefixLength": 60}], [])
        self.assertEqual(out["prefixes"], ["2001:db8:4000:10::/60"])

    def test_aws_56_takes_the_whole_block_or_waits(self):
        out = I.plan_allocation(self.s, OWN + "b", [{"family": 6, "prefixLength": 56}], [])
        self.assertEqual(out["prefixes"], ["2001:db8:4000::/56"])
        self.s["alloc"][OWN + "a"] = ["2001:db8:4000::/64"]
        self.assertIn("no free /56", I.plan_allocation(self.s, OWN + "b", [{"family": 6, "prefixLength": 56}],
                                                       [])["waiting"])

    def test_pinned_64_inside_and_outside(self):
        self.assertEqual(I.plan_allocation(self.s, OWN + "a", [{"family": 6, "prefix": "2001:db8:4000:7::/64"}],
                                           [])["prefixes"], ["2001:db8:4000:7::/64"])
        self.assertIn("outside", I.plan_allocation(self.s, OWN + "a", [{"family": 6, "prefix": "2001:db8:4001::/64"}],
                                                   [])["invalid"])

    def test_site_reserved_v6_stepped_around_and_wholly_inside(self):
        s = st(["fd00:1::/48"])
        out = I.plan_allocation(s, OWN + "a", [{"family": 6, "prefixLength": 60}], ["fd00::/16"])
        self.assertIn("lies inside the site's own range fd00::/16", out["invalid"])
        s = st(["fd12::/48"])
        self.assertEqual(I.plan_allocation(s, OWN + "a", [{"family": 6, "prefixLength": 64}], ["fd12::/64"])["prefixes"],
                         ["fd12:0:0:1::/64"])

    def test_provider_source_skipped(self):
        out = I.plan_allocation(self.s, OWN + "a", [{"family": 4, "prefixLength": 24},
                                                    {"family": 6, "source": "Provider"}], [])
        self.assertEqual(out["prefixes"], ["10.60.0.0/24"])
        self.assertEqual(I.plan_allocation(self.s, OWN + "a", [{"family": 6, "source": "Provider"}], []),
                         {"prefixes": [], "existing": True})


class ExternalMap(unittest.TestCase):
    """R-api B14: provider-assigned subnets sit apart from the space and never refuse a space edit."""

    def test_external_never_refuses_a_space_edit(self):
        s = st(["10.60.0.0/16"], a=["10.60.0.0/24"])
        s["external"] = {OWN + "a": ["2001:db8:aaaa:bb00::/56"]}
        self.assertEqual(I.space_change(s, ["10.60.0.0/16"]), [])
        self.assertEqual(I.usage(s)[0]["allocations"], 1)

    def test_subnets_rows_tagged_by_source(self):
        self.assertEqual(I.subnets_of(["10.60.0.0/24"], ["2001:db8:aaaa:bb00::/56"]),
                         [{"family": 4, "prefix": "10.60.0.0/24", "source": "Network"},
                          {"family": 6, "prefix": "2001:db8:aaaa:bb00::/56", "source": "Provider"}])

    def test_external_entries_canonical_and_family_checked(self):
        self.assertEqual(I.external_entries([{"family": 6, "prefix": "2001:DB8:AAAA:BB00::/56"},
                                             "2001:db8:aaaa:bb00::/56"]), ["2001:db8:aaaa:bb00::/56"])
        with self.assertRaises(RuntimeError):
            I.external_entries([{"family": 4, "prefix": "2001:db8::/56"}])


class Modes(unittest.TestCase):
    def test_derive(self):
        self.assertEqual(I.derive_mode6(["10.60.0.0/16"]), "None")
        self.assertEqual(I.derive_mode6(["10.60.0.0/16", "fd12:3456:789a::/48"]), "Tenant")
        self.assertEqual(I.derive_mode6(["2001:db8:4000::/56"], ["2001:db8:4000::/56"]), "Platform")
        self.assertEqual(I.derive_mode6(["10.60.0.0/16", "2001:db8:4000::/56"], ["2001:db8:4000::/56"]), "Platform")

    def test_stored_state_without_mode_derives(self):
        # a network written before modes existed: its ULA makes it Tenant
        self.assertEqual(I.stored_mode6({"prefixes": ["10.60.0.0/16", "fd12:3456:789a::/48"], "alloc": {}}), "Tenant")
        self.assertEqual(I.stored_mode6({"prefixes": ["10.60.0.0/16"], "mode6": "Platform"}), "Platform")

    def test_mode_change_only_refused_with_placements(self):
        old = {"prefixes": ["10.60.0.0/16", "fd12:3456:789a::/48"]}
        self.assertIsNone(I.mode_change(old, "Platform", []))
        self.assertIsNone(I.mode_change(old, "Tenant", [OWN + "a"]))
        self.assertIsNone(I.mode_change(None, "Platform", [OWN + "a"]))
        msg = I.mode_change(old, "Platform", [OWN + "a"])
        self.assertIn("can't change while it has placements", msg)
        self.assertIn("it is Tenant", msg)

    def test_same_tenant_ipv6_overlap(self):
        nets = {hn("a"): {"prefixes": ["fd12:3456::/48"], "tenant": "acme"},
                hn("b", "entity-other"): {"prefixes": ["fd99::/48"], "tenant": "other"},
                hn("c"): {"prefixes": ["2001:db8:4000::/56"], "platform": ["2001:db8:4000::/56"], "tenant": "acme"},
                hn("old"): {"prefixes": ["fd77::/48"]}}  # no tenant recorded: same namespace counts
        self.assertEqual(I.tenant_overlaps(nets, hn("x"), "acme", ["fd12:3456:0:100::/56", "10.0.0.0/8"], []),
                         ["fd12:3456:0:100::/56 overlaps fd12:3456::/48 of network a"])
        # another tenant may repeat the same prefix
        self.assertEqual(I.tenant_overlaps(nets, hn("y", "entity-other2"), "other2", ["fd12:3456::/48"], []), [])
        self.assertEqual(I.tenant_overlaps(nets, hn("z", "entity-other"), "other", ["fd12:3456::/48"], []), [])
        # platform blocks are never the tenant's own prefixes
        self.assertEqual(I.tenant_overlaps(nets, hn("x"), "acme", ["2001:db8:4000::/52"], []),
                         [])
        self.assertEqual(I.tenant_overlaps(nets, hn("x"), "acme", ["fd77::/64"], []),
                         ["fd77::/64 overlaps fd77::/48 of network old"])
        # the network itself is not a conflict
        self.assertEqual(I.tenant_overlaps(nets, hn("a"), "acme", ["fd12:3456::/48"], []), [])


# ---- level 1: platform pools --------------------------------------------------------------------------------------

class PoolConfig(unittest.TestCase):
    def test_canonical(self):
        c = I.pool_config({"pool": "p", "family": 6, "prefixes": [POOL_PREFIX], "blockLengths": [60, 56],
                           "defaultBlock": 56})
        self.assertEqual(c["blockLengths"], [56, 60])
        self.assertEqual(c["holdDownDays"], 7)
        self.assertIsNone(c["perTenantBlocks"])

    def test_malformed(self):
        base = {"pool": "p", "family": 6, "prefixes": [POOL_PREFIX], "blockLengths": [56], "defaultBlock": 56}
        for bad in ({"family": 5}, {"prefixes": []}, {"prefixes": ["10.0.0.0/8"]}, {"prefixes": ["2001:db8::1/50"]},
                    {"blockLengths": []}, {"blockLengths": [48]}, {"blockLengths": [58]}, {"blockLengths": [68]},
                    {"defaultBlock": 60}, {"holdDownDays": -1}, {"perTenantBlocks": -2},
                    {"prefixes": [POOL_PREFIX, "2001:db8:4000::/52"]}):
            with self.assertRaises(ValueError, msg=bad):
                I.pool_config({**base, **bad})

    def test_pool_change(self):
        p = pool()
        take(p, hn("a"), "acme")
        self.assertEqual(I.pool_change(p, I.pool_config({"pool": "p", "family": 6, "prefixes": ["2001:db8:4000::/49"],
                                                         "blockLengths": [56], "defaultBlock": 56}), {}), [])
        refused = I.pool_change(p, I.pool_config({"pool": "p", "family": 6, "prefixes": ["2001:db8:8000::/50"],
                                                  "blockLengths": [56], "defaultBlock": 56}), {})
        self.assertIn("would lie outside the pool", refused[0])
        refused = I.pool_change(p, I.pool_config({"pool": "p", "family": 6, "prefixes": [POOL_PREFIX],
                                                  "reserved": ["2001:db8:4000::/60"], "blockLengths": [56],
                                                  "defaultBlock": 56}), {})
        self.assertIn("reserved range 2001:db8:4000::/60", refused[0])
        refused = I.pool_change({}, I.pool_config({"pool": "q", "family": 6, "prefixes": ["2001:db8:4000::/52"],
                                                   "blockLengths": [56], "defaultBlock": 56}), {"public-v6": p})
        self.assertIn("of pool public-v6", refused[0])


class PoolAllocation(unittest.TestCase):
    def test_first_fit_default_block_lowest_first(self):
        p = pool()
        self.assertEqual(take(p, hn("a"), "acme"), {"prefixes": ["2001:db8:4000::/56"], "new": True})
        self.assertEqual(take(p, hn("b"), "acme")["prefixes"], ["2001:db8:4000:100::/56"])
        self.assertEqual(take(p, hn("c"), "acme", {"family": 6, "prefixLength": 60})["prefixes"],
                         ["2001:db8:4000:200::/60"])
        self.assertEqual(take(p, hn("d"), "acme")["prefixes"], ["2001:db8:4000:300::/56"])

    def test_idempotent_and_default_change(self):
        p = pool()
        take(p, hn("a"), "acme")
        self.assertEqual(take(p, hn("a"), "acme"), {"prefixes": ["2001:db8:4000::/56"], "existing": True})
        p["defaultBlock"] = 60  # the realization's default changed: a network naming no size keeps its block
        self.assertEqual(take(p, hn("a"), "acme")["existing"], True)
        out = take(p, hn("a"), "acme", {"family": 6, "prefixLength": 60})
        self.assertIn("the network holds 2001:db8:4000::/56; release it", out["invalid"])

    def test_unoffered_size_invalid(self):
        p = pool()
        out = take(p, hn("a"), "acme", {"family": 6, "prefixLength": 52})
        self.assertEqual(out, {"invalid": "this fabric offers platform IPv6 blocks of /56, /60"})
        out = take(p, hn("a"), "acme", {"family": 6, "prefix": "2001:db8:4000:ff00::/64", "prefixLength": 64})
        self.assertIn("offers platform IPv6 blocks of /56, /60", out["invalid"])
        self.assertEqual(p["alloc"], {})

    def test_family_mismatch_unavailable(self):
        self.assertIn("unavailable", I.plan_pool_allocation(pool(), hn("a"), "acme", {"family": 4}, T0))

    def test_pinned_free(self):
        p = pool()
        take(p, hn("a"), "acme")
        out = take(p, hn("b"), "acme", {"family": 6, "prefix": "2001:db8:4000:3f00::/56", "prefixLength": 56})
        self.assertEqual(out["prefixes"], ["2001:db8:4000:3f00::/56"])
        # first fit steps around the pinned block
        self.assertEqual(take(p, hn("c"), "acme")["prefixes"], ["2001:db8:4000:100::/56"])

    def test_pinned_outside_pool_invalid(self):
        out = take(pool(), hn("b"), "acme", {"family": 6, "prefix": "2001:db8:8000::/56"})
        self.assertEqual(out["invalid"],
                         f"2001:db8:8000::/56 lies outside the platform's IPv6 address pool ({POOL_PREFIX})")
        # a block as large as the pool, at another place
        out = take(pool(lengths=(52, 56)), hn("b"), "acme", {"family": 6, "prefix": "2001:db8:5000::/52"})
        self.assertIn("outside", out["invalid"])

    def test_pinned_reserved_invalid(self):
        p = pool(reserved=["2001:db8:4000::/56"])
        out = take(p, hn("b"), "acme", {"family": 6, "prefix": "2001:db8:4000::/56"})
        self.assertIn("overlaps a range the platform keeps (2001:db8:4000::/56)", out["invalid"])
        self.assertEqual(take(p, hn("c"), "acme")["prefixes"], ["2001:db8:4000:100::/56"])

    def test_pinned_held_waits_without_naming_the_holder(self):
        p = pool()
        take(p, hn("a", "entity-other"), "other")
        out = take(p, hn("b"), "acme", {"family": 6, "prefix": "2001:db8:4000::/56"})
        self.assertEqual(out, {"waiting": "2001:db8:4000::/56 is assigned to another network", "waitingFor": "held"})
        self.assertNotIn("other", out["waiting"].replace("another", ""))
        # a /60 inside a held /56 is held too
        out = take(p, hn("b"), "acme", {"family": 6, "prefix": "2001:db8:4000:10::/60"})
        self.assertEqual(out["waitingFor"], "held")

    def test_exhaustion_waits(self):
        p = pool(prefixes=["2001:db8:4000::/55"])  # two /56s
        take(p, hn("a"), "acme")
        take(p, hn("b"), "other")
        out = take(p, hn("c"), "third")
        self.assertEqual(out, {"waiting": "no free /56 in the platform's IPv6 address pool", "waitingFor": "exhausted"})
        self.assertNotIn(hn("c"), p["alloc"])
        # a smaller offered size still fits nowhere
        self.assertEqual(take(p, hn("c"), "third", {"family": 6, "prefixLength": 60})["waitingFor"], "exhausted")

    def test_allowance(self):
        p = pool(per=1)
        take(p, hn("a"), "acme")
        out = take(p, hn("b"), "acme")
        self.assertEqual(out, {"waiting": "the tenant's platform IPv6 allowance is used (1 of 1 blocks)",
                               "waitingFor": "allowance"})
        # another tenant is not affected; the holder itself still gets its block back
        self.assertTrue(take(p, hn("c", "entity-other"), "other").get("new"))
        self.assertTrue(take(p, hn("a"), "acme").get("existing"))

    def test_allowance_override_and_unlimited(self):
        p = pool(per=1, perTenantOverrides={"acme": 3})
        for n in ("a", "b", "c"):
            self.assertTrue(take(p, hn(n), "acme").get("new"))
        self.assertEqual(take(p, hn("d"), "acme")["waitingFor"], "allowance")
        p = pool(per=None)
        for n in range(10):
            self.assertTrue(take(p, hn(f"n{n}"), "acme").get("new"))
        p = pool(per=0)
        self.assertEqual(take(p, hn("a"), "acme")["waitingFor"], "allowance")

    def test_allowance_checked_before_space(self):
        p = pool(prefixes=["2001:db8:4000::/56"], per=1)
        take(p, hn("a"), "acme")
        self.assertEqual(take(p, hn("b"), "acme")["waitingFor"], "allowance")
        self.assertEqual(take(p, hn("c"), "other")["waitingFor"], "exhausted")


class HoldDown(unittest.TestCase):
    def test_release_stamps_and_excludes(self):
        p = pool(prefixes=["2001:db8:4000::/55"])
        take(p, hn("a"), "acme")
        take(p, hn("b"), "acme")
        blocks, until = I.release_from_pool(p, hn("a"), T0)
        self.assertEqual(blocks, ["2001:db8:4000::/56"])
        self.assertEqual(until, "2026-01-08T12:00:00Z")
        self.assertEqual(p["released"], {"2001:db8:4000::/56": {"at": "2026-01-01T12:00:00Z", "owner": hn("a")}})
        self.assertNotIn(hn("a"), p["tenants"])
        # the only free block is in hold-down: wait, and say until when
        out = take(p, hn("c"), "other", at=T0 + datetime.timedelta(days=1))
        self.assertEqual(out["waitingFor"], "holdDown")
        self.assertEqual(out["holdDownUntil"], "2026-01-08T12:00:00Z")
        self.assertIn("released blocks are held back for 7 days", out["waiting"])
        # pinned in hold-down: the same
        out = take(p, hn("c"), "other", {"family": 6, "prefix": "2001:db8:4000::/56"}, at=T0)
        self.assertEqual((out["waitingFor"], out["holdDownUntil"]), ("holdDown", "2026-01-08T12:00:00Z"))
        self.assertIn("was released recently and is held back until 2026-01-08T12:00:00Z", out["waiting"])

    def test_reallocation_after_hold_down(self):
        p = pool(prefixes=["2001:db8:4000::/55"])
        take(p, hn("a"), "acme")
        take(p, hn("b"), "acme")
        I.release_from_pool(p, hn("a"), T0)
        later = T0 + datetime.timedelta(days=7, seconds=1)
        out = take(p, hn("c"), "other", at=later)
        self.assertEqual(out, {"prefixes": ["2001:db8:4000::/56"], "new": True})
        self.assertEqual(p["released"], {})  # stamp pruned once the block is allocated again

    def test_first_fit_skips_held_down_when_others_free(self):
        p = pool()
        take(p, hn("a"), "acme")
        I.release_from_pool(p, hn("a"), T0)
        self.assertEqual(take(p, hn("b"), "other")["prefixes"], ["2001:db8:4000:100::/56"])

    def test_hold_down_zero_frees_at_once(self):
        p = pool(prefixes=["2001:db8:4000::/56"], hold=0)
        take(p, hn("a"), "acme")
        blocks, until = I.release_from_pool(p, hn("a"), T0)
        self.assertEqual(until, "2026-01-01T12:00:00Z")
        self.assertEqual(p["released"], {})
        self.assertEqual(take(p, hn("b"), "other")["prefixes"], ["2001:db8:4000::/56"])

    def test_release_of_nothing(self):
        self.assertEqual(I.release_from_pool(pool(), hn("a"), T0), (None, None))

    def test_bare_timestamp_stamp_is_read(self):
        p = pool()
        p["released"] = {"2001:db8:4000::/56": "2026-01-01T12:00:00Z"}
        self.assertEqual(list(I.held_down(p, T0)), [N("2001:db8:4000::/56")])

    def test_allowance_frees_on_release(self):
        p = pool(per=1)
        take(p, hn("a"), "acme")
        I.release_from_pool(p, hn("a"), T0)
        self.assertTrue(take(p, hn("b"), "acme").get("new"))

    def test_usage_counts(self):
        p = pool(prefixes=["2001:db8:4000::/54"])  # four /56s
        take(p, hn("a"), "acme")
        take(p, hn("b"), "acme")
        I.release_from_pool(p, hn("b"), T0)
        u = I.pool_usage("public-v6", p, T0)
        self.assertEqual((u["blocksUsed"], u["blocksHeldDown"], u["blocksFree"]), (1, 1, 2))
        self.assertEqual(u["largestFree"], 55)
        self.assertEqual(u["blockLengths"], [56, 60])


class ChoosePool(unittest.TestCase):
    def test_choose(self):
        pools = {"public-v6": pool()}
        self.assertEqual(I.choose_pool(pools, None, 6)[0], "public-v6")
        self.assertEqual(I.choose_pool(pools, None, 4), (None, None))
        self.assertEqual(I.choose_pool(pools, "nope", 6), (None, None))
        pools["second"] = pool(prefixes=["2001:db8:8000::/50"])
        with self.assertRaises(RuntimeError):
            I.choose_pool(pools, None, 6)


# ---- ledger provider against an in-memory API (compare-and-swap) --------------------------------------------------

class FakeApi:
    """The ConfigMap and the status subresources the helpers touch; PUT honours resourceVersion (409 on a stale
    one); conflicts can be injected."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cm = None
        self.rv = 0
        self.patches = []
        self.conflicts = 0

    def bind(self, req):
        return self

    def call(self, method, path, body=None, content_type=None):
        with self.lock:
            if "/configmaps" in path:
                if method == "GET":
                    if self.cm is None:
                        return 404, {"message": "not found"}
                    return 200, json.loads(json.dumps(self.cm))
                if method == "POST":
                    if self.cm is not None:
                        return 409, {}
                    self.rv += 1
                    self.cm = {**body, "metadata": {**body["metadata"], "resourceVersion": str(self.rv)}}
                    return 201, self.cm
                if method == "PUT":
                    if self.conflicts:
                        self.conflicts -= 1
                        self.rv += 1
                        self.cm["metadata"]["resourceVersion"] = str(self.rv)
                        return 409, {}
                    if body["metadata"].get("resourceVersion") != self.cm["metadata"]["resourceVersion"]:
                        return 409, {}
                    self.rv += 1
                    self.cm = {**body, "metadata": {**body["metadata"], "resourceVersion": str(self.rv)}}
                    return 200, self.cm
            if method == "PATCH":
                self.patches.append((path, body))
                return 200, {}
            return 404, {"message": path}

    def ledger(self):
        return json.loads(self.cm["data"]["ledger.json"])


class Ledger(unittest.TestCase):
    NET = hn("ut")

    def setUp(self):
        self.api = FakeApi()
        self.p_api = mock.patch.object(shc_ledger, "Api", lambda req: self.api)
        self.p_api.start()
        self.p_sleep = mock.patch.object(shc_ledger.time, "sleep", lambda s: None)
        self.p_sleep.start()
        self.now = T0
        self.p_now = mock.patch.object(I, "utcnow", lambda: self.now)
        self.p_now.start()
        self.base = {"api": "https://api.test:6443", "token": "t", "namespace": "sovereign-cloud", "fabric": "ut"}
        out = shc_ledger.ledger_ensure(self.api, {**self.base, "fabric_uid": "u", "pools": {"vni": [1, 9]}})
        self.assertTrue(out["created"])

    def tearDown(self):
        mock.patch.stopall()

    def call(self, **req):
        r = {**self.base, **req}
        return I.ACTIONS[r["action"]](I.provider(r), r)

    def ensure(self, **kw):
        cfg = {"pool": "public-v6", "family": 6, "prefixes": ["2001:db8:4000::/55"], "blockLengths": [56, 60],
               "defaultBlock": 56, "holdDownDays": 7, "perTenantBlocks": 4}
        return self.call(action="ipam-pool-ensure", **{**cfg, **kw})

    def test_pool_lifecycle(self):
        out = self.ensure()
        self.assertTrue(out["changed"])
        self.assertEqual(out["usage"]["blocksFree"], 2)
        self.assertFalse(self.ensure()["changed"])  # idempotent, no write
        a = self.call(action="ipam-pool-allocate", owner=hn("a"), tenant="acme", want=[{"family": 6}])
        self.assertEqual((a["prefixes"], a["pool"], a["new"]), (["2001:db8:4000::/56"], "public-v6", True))
        self.assertTrue(a["statusPublished"])
        path, body = self.api.patches[-1]
        self.assertTrue(path.endswith("/hybridfabrics/ut/status"))
        self.assertEqual(body["status"]["realization"]["ipam"]["pools"][0]["blocksUsed"], 1)
        again = self.call(action="ipam-pool-allocate", owner=hn("a"), tenant="acme", want={"family": 6})
        self.assertEqual(again, {"prefixes": ["2001:db8:4000::/56"], "existing": True, "pool": "public-v6"})
        self.call(action="ipam-pool-allocate", owner=hn("b"), tenant="acme", want={"family": 6})
        w = self.call(action="ipam-pool-allocate", owner=hn("c"), tenant="acme", want={"family": 6})
        self.assertEqual(w["waitingFor"], "exhausted")
        self.assertNotIn(hn("c"), self.api.ledger()["ipamPools"]["public-v6"]["alloc"])
        r = self.call(action="ipam-pool-release", owner=hn("a"))
        self.assertEqual((r["released"], r["holdDownUntil"]), (["2001:db8:4000::/56"], "2026-01-08T12:00:00Z"))
        self.assertEqual(self.call(action="ipam-pool-release", owner=hn("a")), {"released": None})
        w = self.call(action="ipam-pool-allocate", owner=hn("c"), tenant="acme", want={"family": 6})
        self.assertEqual(w["waitingFor"], "holdDown")
        self.now = T0 + datetime.timedelta(days=8)
        a = self.call(action="ipam-pool-allocate", owner=hn("c"), tenant="acme", want={"family": 6})
        self.assertEqual(a["prefixes"], ["2001:db8:4000::/56"])
        g = self.call(action="ipam-pool-get", owner=hn("c"))
        self.assertEqual(g["held"], {"public-v6": ["2001:db8:4000::/56"]})
        self.assertEqual(g["pools"][0]["blocksUsed"], 2)

    def test_pool_unavailable_and_errors(self):
        out = self.call(action="ipam-pool-allocate", owner=hn("a"), tenant="acme", want={"family": 6})
        self.assertEqual(out, {"unavailable": "this fabric offers no platform IPv6 address space"})
        with self.assertRaises(RuntimeError):
            self.call(action="ipam-pool-allocate", owner=hn("a"), want={"family": 6})  # no tenant
        with self.assertRaises(RuntimeError):
            self.ensure(blockLengths=[58])
        self.ensure()
        self.call(action="ipam-pool-allocate", owner=hn("a"), tenant="acme", want={"family": 6})
        r = self.ensure(prefixes=["2001:db8:8000::/55"])
        self.assertIn("would lie outside the pool", r["refused"][0])
        self.assertEqual(self.api.ledger()["ipamPools"]["public-v6"]["prefixes"], ["2001:db8:4000::/55"])

    def test_several_pools_in_one_call(self):
        pools = {"public-v6": {"family": 6, "prefixes": ["2001:db8:4000::/50"], "blockLengths": [56, 60],
                               "defaultBlock": 56, "perTenantBlocks": 4},
                 "second": {"family": 6, "prefixes": ["2001:db8:8000::/50"], "blockLengths": [56], "defaultBlock": 56,
                            "holdDownDays": 0}}
        out = self.call(action="ipam-pool-ensure", pools=pools, defaultHoldDownDays=3)
        self.assertTrue(out["changed"])
        self.assertEqual([(r["name"], r["holdDownDays"]) for r in out["pools"]], [("public-v6", 3), ("second", 0)])
        self.assertFalse(self.call(action="ipam-pool-ensure", pools=pools, defaultHoldDownDays=3)["changed"])
        # two pools of a family: the request must name one
        with self.assertRaises(RuntimeError):
            self.call(action="ipam-pool-allocate", owner=hn("a"), tenant="acme", want={"family": 6})
        a = self.call(action="ipam-pool-allocate", pool="second", owner=hn("a"), tenant="acme", want={"family": 6})
        self.assertEqual(a["prefixes"], ["2001:db8:8000::/56"])
        bad = {**pools, "third": {"family": 6, "prefixes": ["2001:db8:8000::/52"], "blockLengths": [56],
                                  "defaultBlock": 56}}
        r = self.call(action="ipam-pool-ensure", pools=bad)
        self.assertIn("pool third: 2001:db8:8000::/52 overlaps 2001:db8:8000::/50 of pool second", r["refused"])
        self.assertNotIn("third", self.api.ledger()["ipamPools"])

    def test_pool_section_survives_ledger_ensure_and_is_not_counted(self):
        self.ensure()
        self.call(action="ipam-pool-allocate", owner=hn("a"), tenant="acme", want={"family": 6})
        out = shc_ledger.ledger_ensure(self.api, {**self.base, "fabric_uid": "u", "pools": {"vni": [1, 19]}})
        self.assertFalse(out["created"])
        led = self.api.ledger()
        self.assertEqual(led["ipamPools"]["public-v6"]["alloc"], {hn("a"): ["2001:db8:4000::/56"]})
        self.assertEqual(shc_ledger.tenant_allocations(led, "sovereign-cloud", "ut"), 0)

    def test_concurrent_pool_allocations_distinct(self):
        self.ensure(prefixes=["2001:db8:4000::/52"], perTenantBlocks=None)
        with ThreadPoolExecutor(8) as ex:
            outs = list(ex.map(lambda i: self.call(action="ipam-pool-allocate", owner=hn(f"n{i}"), tenant="acme",
                                                   want={"family": 6}), range(8)))
        got = [o["prefixes"][0] for o in outs]
        self.assertEqual(len(set(got)), 8, outs)
        self.assertEqual(len(self.api.ledger()["ipamPools"]["public-v6"]["alloc"]), 8)

    def test_conflict_retried(self):
        self.ensure()
        self.api.conflicts = 3
        out = self.call(action="ipam-pool-allocate", owner=hn("a"), tenant="acme", want={"family": 6})
        self.assertEqual(out["prefixes"], ["2001:db8:4000::/56"])

    def test_network_space_platform_mode_and_tenant(self):
        out = self.call(action="ipam-space", network=self.NET, prefixes=["10.60.0.0/16", "2001:db8:4000::/56"],
                        platform=["2001:db8:4000::/56"], mode6="Platform", holders=[], tenant="acme")
        self.assertTrue(out["changed"])
        self.assertEqual(out["mode6"], "Platform")
        self.assertEqual([r["source"] for r in out["addressSpace"]], ["Tenant", "Platform"])
        a = self.call(action="ipam-allocate", network=self.NET, owner=OWN + "p",
                      want=[{"family": 4, "prefixLength": 24}, {"family": 6, "prefixLength": 64}], exclude=[])
        self.assertEqual(a["subnets"], [{"family": 4, "prefix": "10.60.0.0/24", "source": "Network"},
                                        {"family": 6, "prefix": "2001:db8:4000::/64", "source": "Network"}])
        # a mode change while a placement exists is refused and leaves the space alone
        r = self.call(action="ipam-space", network=self.NET, prefixes=["10.60.0.0/16", "fd12::/48"], mode6="Tenant",
                      holders=[OWN + "p"], tenant="acme")
        self.assertEqual(r["reason"], "IPv6ModeInUse")
        self.assertEqual(self.call(action="ipam-get", network=self.NET)["mode6"], "Platform")
        c = self.call(action="ipam-mode-check", network=self.NET, mode6="None", holders=[OWN + "p"])
        self.assertEqual(c["reason"], "IPv6ModeInUse")
        self.assertTrue(self.call(action="ipam-mode-check", network=self.NET, mode6="Platform",
                                  holders=[OWN + "p"])["ok"])
        # removing the block while a placement holds a /64 in it: PrefixInUse
        r = self.call(action="ipam-space", network=self.NET, prefixes=["10.60.0.0/16"], mode6="None", holders=[],
                      tenant="acme")
        self.assertEqual(r["reason"], "PrefixInUse")
        # a re-run is no change and no write
        rv = self.api.rv
        r = self.call(action="ipam-space", network=self.NET, prefixes=["10.60.0.0/16", "2001:db8:4000::/56"],
                      platform=["2001:db8:4000::/56"], mode6="Platform", holders=[OWN + "p"], tenant="acme")
        self.assertFalse(r["changed"])
        self.assertEqual(self.api.rv, rv)

    def test_old_caller_and_stored_state_unchanged(self):
        # the P10 call shape: no platform, mode or tenant; the stored ULA network derives Tenant
        out = self.call(action="ipam-space", network=self.NET, prefixes=["10.60.0.0/16", "fd12:3456:789a::/48"])
        self.assertEqual(out["mode6"], "Tenant")
        self.assertEqual(self.call(action="ipam-get", network=self.NET)["mode6"], "Tenant")

    def test_same_tenant_overlap_refused_other_tenant_fine(self):
        self.call(action="ipam-space", network=hn("a"), prefixes=["fd12::/48"], tenant="acme")
        r = self.call(action="ipam-space", network=hn("b"), prefixes=["fd12::/56"], tenant="acme")
        self.assertEqual(r["reason"], "PrefixInUse")
        self.assertIn("overlaps fd12::/48 of network a", r["refused"][0])
        r = self.call(action="ipam-space", network=hn("b", "entity-other"), prefixes=["fd12::/48"], tenant="other")
        self.assertTrue(r["changed"])

    def test_external_map(self):
        self.call(action="ipam-space", network=self.NET, prefixes=["10.60.0.0/16"])
        a = self.call(action="ipam-allocate", network=self.NET, owner=OWN + "aws",
                      want=[{"family": 4, "prefixLength": 24}, {"family": 6, "source": "Provider"}], exclude=[])
        self.assertEqual(a["prefixes"], ["10.60.0.0/24"])
        x = self.call(action="ipam-external", network=self.NET, owner=OWN + "aws",
                      external=[{"family": 6, "prefix": "2001:db8:aaaa:bb00::/56"}])
        self.assertTrue(x["changed"])
        self.assertEqual(x["subnets"][1], {"family": 6, "prefix": "2001:db8:aaaa:bb00::/56", "source": "Provider"})
        self.assertFalse(self.call(action="ipam-external", network=self.NET, owner=OWN + "aws",
                                   external=[{"family": 6, "prefix": "2001:db8:aaaa:bb00::/56"}])["changed"])
        g = self.call(action="ipam-get", network=self.NET)
        self.assertEqual(g["external"], {OWN + "aws": ["2001:db8:aaaa:bb00::/56"]})
        self.assertEqual(g["addressSpace"][0]["allocations"], 1)
        # a space edit is not blocked by the provider's prefix
        self.assertTrue(self.call(action="ipam-space", network=self.NET, prefixes=["10.60.0.0/15"])["changed"])
        # the drop waits for the live holder of an external prefix too
        self.assertIn("still held by", self.call(action="ipam-drop", network=self.NET, holders=[OWN + "aws"])["refused"][0])
        r = self.call(action="ipam-release", network=self.NET, owner=OWN + "aws")
        self.assertEqual((r["released"], r["external"]), (["10.60.0.0/24"], ["2001:db8:aaaa:bb00::/56"]))
        self.assertEqual(self.call(action="ipam-get", network=self.NET)["external"], {})

    def test_external_in_allocate_call(self):
        self.call(action="ipam-space", network=self.NET, prefixes=["10.60.0.0/16"])
        a = self.call(action="ipam-allocate", network=self.NET, owner=OWN + "g", want=[{"family": 4, "prefixLength": 24}],
                      exclude=[], external=[{"family": 6, "prefix": "2001:db8:cccc:1::/64"}])
        self.assertEqual([s["source"] for s in a["subnets"]], ["Network", "Provider"])
        d = self.call(action="ipam-drop", network=self.NET, holders=[])
        self.assertEqual(d, {"dropped": True, "stale": [OWN + "g"]})

    def test_teardown_order_releases_block_with_hold_down(self):
        self.ensure()
        blk = self.call(action="ipam-pool-allocate", owner=self.NET, tenant="acme", want={"family": 6})["prefixes"]
        self.call(action="ipam-space", network=self.NET, prefixes=blk, platform=blk, mode6="Platform", tenant="acme")
        self.assertEqual(self.call(action="ipam-drop", network=self.NET, holders=[])["dropped"], True)
        r = self.call(action="ipam-pool-release", owner=self.NET)
        self.assertEqual(r["released"], blk)
        led = self.api.ledger()
        self.assertNotIn(self.NET, led["ipam"])
        self.assertEqual(led["ipamPools"]["public-v6"]["alloc"], {})
        self.assertEqual(list(led["ipamPools"]["public-v6"]["released"]), blk)


class OrphanCheck(unittest.TestCase):
    """shc_fabric.orphan_report reads the pool section and the provider-assigned subnets."""

    def report(self, led, networks=(), placements=()):
        import shc_fabric
        crs = {"networks": [{"kind": "HybridNetwork", "metadata": {"namespace": "entity-acme", "name": n},
                             "status": {"fabric": "lab"}} for n in networks],
               "placements": list(placements)}
        return shc_fabric.orphan_report({"crs": crs, "ledgers": {"lab": led}})["findings"]

    def test_pool_and_external_orphans(self):
        led = {"alloc": {}, "ipam": {hn("a"): {"prefixes": ["2001:db8:4000::/56"], "platform": ["2001:db8:4000::/56"],
                                               "alloc": {}, "external": {OWN + "gone": ["2001:db8:aaaa::/56"]}},
                                     hn("b"): {"prefixes": ["10.0.0.0/16"], "alloc": {}}},
               "ipamPools": {"public-v6": {"alloc": {hn("a"): ["2001:db8:4000::/56"], hn("b"): ["2001:db8:4000:100::/56"],
                                                     hn("gone"): ["2001:db8:4000:200::/56"]}}}}
        f = self.report(led, networks=("a", "b"))
        self.assertIn("ledger lab: IPAM provider-assigned 2001:db8:aaaa::/56 of HybridNetwork/entity-acme/a recorded "
                      "for NetworkPlacement/entity-acme/gone, which no longer exists", f)
        self.assertIn("ledger lab: pool public-v6 block 2001:db8:4000:200::/56 held by HybridNetwork/entity-acme/gone, "
                      "which no longer exists", f)
        self.assertIn("ledger lab: pool public-v6 block 2001:db8:4000:100::/56 of HybridNetwork/entity-acme/b is not "
                      "in its address space", f)
        self.assertEqual(len(f), 3, f)

    def test_placement_claim_counts_provider_prefix(self):
        p = {"kind": "NetworkPlacement", "metadata": {"namespace": "entity-acme", "name": "aws"},
             "spec": {"network": "a"}, "status": {"prefixes": ["10.0.0.0/24", "2001:db8:aaaa::/56"]}}
        led = {"alloc": {}, "ipam": {hn("a"): {"prefixes": ["10.0.0.0/16"], "alloc": {OWN + "aws": ["10.0.0.0/24"]},
                                               "external": {OWN + "aws": ["2001:db8:aaaa::/56"]}}}}
        self.assertEqual(self.report(led, networks=("a",), placements=(p,)), [])


if __name__ == "__main__":
    unittest.main()
