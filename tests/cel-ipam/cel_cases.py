#!/usr/bin/env python3
"""CEL envtest for the HybridNetwork and NetworkPlacement address rules.

Installs CRDs into a throwaway envtest API server (tests/schema-replay/replay.py backend), then runs
each case in its own namespace: create, update, status writes and read-backs, and compares the
server's answer with the expectation for the CRD set under test.

  python3 -I tests/cel-ipam/cel_cases.py --crds operator/primary/helm/templates/crds.yaml \
      --expect candidate [--report out.json] [--only GROUP]
  python3 -I tests/cel-ipam/cel_cases.py --crds <baseline crds.yaml> --expect baseline

--expect baseline checks the regression group against the CRDs before the per-family change (the
harness itself is then proven against known behaviour); --expect candidate checks every group.
Exit 0 only when every selected case matches. Needs KUBEBUILDER_ASSETS (etcd, kube-apiserver,
kubectl) and openssl.
"""
import argparse
import copy
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "schema-replay"))
import replay  # noqa: E402

GROUP = "hybridsovereign.redhat"
API = f"{GROUP}/v1alpha1"


# ─── builders ────────────────────────────────────────────────────────────────

def net(name="n", prefixes=None, platform=None, **spec):
    s = dict(spec)
    if prefixes is not None:
        s["prefixes"] = prefixes
    if platform is not None:
        s["platformPrefixes"] = platform
    return {"apiVersion": API, "kind": "HybridNetwork", "metadata": {"name": name}, "spec": s}


def np(name="p", kind="CloudVirt", ipv4=None, ipv6=None, network="n", backend_name="env"):
    s = {"network": network, "backend": {"kind": kind, "name": backend_name}}
    if ipv4 is not None:
        s["ipv4"] = ipv4
    if ipv6 is not None:
        s["ipv6"] = ipv6
    return {"apiVersion": API, "kind": "NetworkPlacement", "metadata": {"name": name}, "spec": s}


V4 = {"prefixLength": 24}
V6 = {"prefixLength": 64}
NET4 = ["10.60.0.0/16"]
NET46 = ["10.60.0.0/16", "fd12:3456:789a::/48"]
P6 = [{"family": 6}]


def C(cid, group, desc, steps, note=""):
    return {"id": cid, "group": group, "desc": desc, "steps": steps, "note": note}


# A step: (op, payload, expect). op: create | update | status | delete | check.
#  create: payload = object.             update: payload = (kind, name, fn(spec) -> None).
#  status: payload = (kind, name, status dict).
#  check:  payload = (kind, name, fn(obj) -> str|None, error text when not None).
# expect: "ok" | "deny" | "deny:<substring>" or {"baseline": ..., "candidate": ...}.

def upd(kind, name, fn):
    return ("update", (kind, name, fn), None)


def setk(path, value):
    def fn(spec):
        d = spec
        for k in path[:-1]:
            d = d.setdefault(k, {})
        d[path[-1]] = value
    return fn


def delk(path):
    def fn(spec):
        d = spec
        for k in path[:-1]:
            d = d[k]
        d.pop(path[-1], None)
    return fn


def both(e):
    return {"baseline": e, "candidate": e}


def bc(b, c):
    return {"baseline": b, "candidate": c}


CASES = []

# ─── P10 regression (reconstructed from the P10 record: valid sets, canonical form, limits, v6-only,
#     duplicates, overlaps, pinned/size agreement, AWS /24, immutability, status, printer column) ───
R = "p10"
CASES += [
    C("P10-01", R, "network IPv4 only", [("create", net(prefixes=NET4), "ok")]),
    C("P10-02", R, "network dual-stack tenant prefixes", [("create", net(prefixes=NET46), "ok")]),
    C("P10-03", R, "network 8 prefixes (maxItems)", [("create", net(prefixes=[f"10.{i}.0.0/16" for i in range(8)]), "ok")]),
    C("P10-04", R, "network 9 prefixes refused", [("create", net(prefixes=[f"10.{i}.0.0/16" for i in range(9)]), "deny:must have at most 8")]),
    C("P10-05", R, "network empty prefixes refused", [("create", net(prefixes=[]), bc("deny:should have at least 1", "deny:needs address space"))]),
    C("P10-06", R, "network without prefixes refused", [("create", net(), bc("deny:Required value", "deny:needs address space"))]),
    C("P10-07", R, "network IPv6-only tenant ULA (P10 refused; now accepted)", [("create", net(prefixes=["fd12:3456:789a::/48"]), bc("deny:at least one prefix must be IPv4", "ok"))]),
    C("P10-08", R, "IPv4 host bits refused", [("create", net(prefixes=["10.60.0.1/16"]), "deny:canonical form")]),
    C("P10-09", R, "IPv4 without length refused", [("create", net(prefixes=["10.60.0.0"]), "deny:canonical form")]),
    C("P10-10", R, "garbage prefix refused", [("create", net(prefixes=["not-a-cidr"]), "deny:canonical form")]),
    C("P10-11", R, "upper-case IPv6 refused", [("create", net(prefixes=["10.60.0.0/16", "FD12:3456:789A::/48"]), "deny:canonical form")]),
    C("P10-12", R, "zero-padded IPv6 refused", [("create", net(prefixes=["10.60.0.0/16", "fd12:0000:789a::/48"]), "deny:canonical form")]),
    C("P10-13", R, "IPv4 leading zeros refused", [("create", net(prefixes=["010.060.0.0/16"]), "deny:canonical form")]),
    C("P10-14", R, "IPv4 /7 refused", [("create", net(prefixes=["10.0.0.0/7"]), "deny")]),
    C("P10-15", R, "IPv4 /30 refused", [("create", net(prefixes=["10.60.0.0/30"]), "deny:/8 to /29")]),
    C("P10-16", R, "IPv4 /8 accepted", [("create", net(prefixes=["10.0.0.0/8"]), "ok")]),
    C("P10-17", R, "IPv4 /29 accepted", [("create", net(prefixes=["10.60.0.0/29"]), "ok")]),
    C("P10-18", R, "IPv6 /31 refused", [("create", net(prefixes=["10.60.0.0/16", "fd12::/31"]), "deny")]),
    C("P10-19", R, "IPv6 /65 refused", [("create", net(prefixes=["10.60.0.0/16", "fd12:3456:789a::/65"]), "deny:/32 to /64")]),
    C("P10-20", R, "IPv6 /32 accepted", [("create", net(prefixes=["10.60.0.0/16", "fd12:3456::/32"]), "ok")]),
    C("P10-21", R, "IPv6 /64 accepted", [("create", net(prefixes=["10.60.0.0/16", "fd12:3456:789a:1::/64"]), "ok")]),
    C("P10-22", R, "duplicate prefix refused (set)", [("create", net(prefixes=["10.60.0.0/16", "10.60.0.0/16"]), "deny:Duplicate value")]),
    C("P10-23", R, "IPv4 overlap refused", [("create", net(prefixes=["10.60.0.0/16", "10.60.1.0/24"]), "deny:must not overlap")]),
    C("P10-24", R, "IPv6 overlap refused", [("create", net(prefixes=["10.60.0.0/16", "fd12:3456:789a::/48", "fd12:3456:789a:1::/64"]), "deny:must not overlap")]),
    C("P10-25", R, "adjacent prefixes accepted", [("create", net(prefixes=["10.60.0.0/16", "10.61.0.0/16"]), "ok")]),
    C("P10-26", R, "prefix added later", [("create", net(prefixes=NET4), "ok"), upd("hybridnetworks", "n", setk(["prefixes"], ["10.60.0.0/16", "10.61.0.0/16"])) [:2] + ("ok",)]),
    C("P10-27", R, "prefix removed later (CEL; the job checks use)", [("create", net(prefixes=["10.60.0.0/16", "10.61.0.0/16"]), "ok"), upd("hybridnetworks", "n", setk(["prefixes"], NET4))[:2] + ("ok",)]),
    C("P10-28", R, "fabricRef change refused", [("create", net(prefixes=NET4, fabricRef={"name": "a"}), "ok"), upd("hybridnetworks", "n", setk(["fabricRef", "name"], "b"))[:2] + ("deny:immutable",)]),
    C("P10-29", R, "fabricRef removal refused", [("create", net(prefixes=NET4, fabricRef={"name": "a"}), "ok"), upd("hybridnetworks", "n", delk(["fabricRef"]))[:2] + ("deny:cannot be removed",)]),
    C("P10-30", R, "fabricRef set later accepted", [("create", net(prefixes=NET4), "ok"), upd("hybridnetworks", "n", setk(["fabricRef"], {"name": "a"}))[:2] + ("ok",)]),
    C("P10-31", R, "network status addressSpace writable", [("create", net(prefixes=NET46), "ok"),
        ("status", ("hybridnetworks", "n", {"addressSpace": [{"prefix": "10.60.0.0/16", "family": 4, "allocations": 1, "largestFree": 17}]}), "ok")]),
    C("P10-32", R, "mtu below 1280 refused", [("create", net(prefixes=NET4, mtu=1279), "deny")]),
    C("P10-33", R, "placement CloudVirt IPv4 /24", [("create", np(ipv4=V4), "ok")]),
    C("P10-34", R, "placement pinned IPv4", [("create", np(ipv4={"prefix": "10.60.5.0/24"}), "ok")]),
    C("P10-35", R, "placement IPv4 size and pin agree", [("create", np(ipv4={"prefix": "10.60.5.0/24", "prefixLength": 24}), "ok")]),
    C("P10-36", R, "placement IPv4 size and pin disagree", [("create", np(ipv4={"prefix": "10.60.5.0/24", "prefixLength": 25}), "deny:as long as")]),
    C("P10-37", R, "placement ipv4 {} refused", [("create", np(ipv4={}), "deny:needs prefixLength or prefix")]),
    C("P10-38", R, "placement IPv4 /15 refused", [("create", np(ipv4={"prefixLength": 15}), "deny")]),
    C("P10-39", R, "placement IPv4 /30 refused", [("create", np(ipv4={"prefixLength": 30}), "deny")]),
    C("P10-40", R, "placement IPv4 /16 accepted", [("create", np(ipv4={"prefixLength": 16}), "ok")]),
    C("P10-41", R, "placement IPv4 /29 accepted", [("create", np(ipv4={"prefixLength": 29}), "ok")]),
    C("P10-42", R, "pinned IPv4 host bits refused", [("create", np(ipv4={"prefix": "10.60.5.1/24"}), "deny:canonical form")]),
    C("P10-43", R, "pinned IPv4 given an IPv6 prefix refused", [("create", np(ipv4={"prefix": "fd12::/64"}), "deny")]),
    C("P10-44", R, "pinned IPv4 /30 refused", [("create", np(ipv4={"prefix": "10.60.5.0/30"}), "deny:/16 to /29")]),
    C("P10-45", R, "placement with no family refused", [("create", np(), bc("deny:Required value", "deny:needs spec.ipv4, spec.ipv6 or both"))]),
    C("P10-46", R, "CloudAWS IPv4 /24 accepted", [("create", np(kind="CloudAWS", ipv4=V4), "ok")]),
    C("P10-47", R, "CloudAWS IPv4 /25 refused", [("create", np(kind="CloudAWS", ipv4={"prefixLength": 25}), "deny:CloudAWS placement must be /16 to /24")]),
    C("P10-48", R, "CloudAWS IPv4 /16 accepted", [("create", np(kind="CloudAWS", ipv4={"prefixLength": 16}), "ok")]),
    C("P10-49", R, "CloudAWS pinned IPv4 /25 refused", [("create", np(kind="CloudAWS", ipv4={"prefix": "10.60.5.0/25"}), "deny:CloudAWS placement must be /16 to /24")]),
    C("P10-50", R, "CloudAWS pinned IPv4 /24 accepted", [("create", np(kind="CloudAWS", ipv4={"prefix": "10.60.5.0/24"}), "ok")]),
    C("P10-51", R, "CloudOSO IPv4 /29 accepted", [("create", np(kind="CloudOSO", ipv4={"prefixLength": 29}), "ok")]),
    C("P10-52", R, "CloudGCP IPv4 /29 accepted", [("create", np(kind="CloudGCP", ipv4={"prefixLength": 29}), "ok")]),
    C("P10-53", R, "dual-stack placement /24 + /64", [("create", np(ipv4=V4, ipv6=V6), "ok")]),
    C("P10-54", R, "pinned IPv6 /64", [("create", np(ipv4=V4, ipv6={"prefix": "fd12:3456:789a:1::/64"}), "ok")]),
    C("P10-55", R, "CloudVirt IPv6 /60 refused", [("create", np(ipv4=V4, ipv6={"prefixLength": 60}), bc("deny", "deny:must be a /64"))]),
    C("P10-56", R, "CloudVirt ipv6 {} refused", [("create", np(ipv4=V4, ipv6={}), "deny:needs prefixLength or prefix")]),
    C("P10-57", R, "pinned IPv6 not canonical refused", [("create", np(ipv4=V4, ipv6={"prefix": "fd12:3456:789a:1::1/64"}), "deny:canonical form")]),
    C("P10-58", R, "pinned IPv6 given an IPv4 prefix refused", [("create", np(ipv4=V4, ipv6={"prefix": "10.60.5.0/24"}), "deny")]),
    C("P10-59", R, "unknown backend kind refused", [("create", np(kind="CloudAzure", ipv4=V4), "deny:Unsupported value")]),
    C("P10-60", R, "network change refused", [("create", np(ipv4=V4), "ok"), upd("networkplacements", "p", setk(["network"], "m"))[:2] + ("deny:spec.network is immutable",)]),
    C("P10-61", R, "backend change refused", [("create", np(ipv4=V4), "ok"), upd("networkplacements", "p", setk(["backend", "name"], "other"))[:2] + ("deny:spec.backend is immutable",)]),
    C("P10-62", R, "ipv4 change refused", [("create", np(ipv4=V4), "ok"), upd("networkplacements", "p", setk(["ipv4", "prefixLength"], 25))[:2] + ("deny:spec.ipv4 is immutable",)]),
    C("P10-63", R, "ipv6 change refused", [("create", np(ipv4=V4, ipv6=V6), "ok"), upd("networkplacements", "p", setk(["ipv6"], {"prefix": "fd12:3456:789a:1::/64"}))[:2] + ("deny:spec.ipv6 is immutable",)]),
    C("P10-64", R, "ipv6 added refused", [("create", np(ipv4=V4), "ok"), upd("networkplacements", "p", setk(["ipv6"], V6))[:2] + ("deny:spec.ipv6 cannot be added or removed",)]),
    C("P10-65", R, "ipv6 removed refused", [("create", np(ipv4=V4, ipv6=V6), "ok"), upd("networkplacements", "p", delk(["ipv6"]))[:2] + ("deny:spec.ipv6 cannot be added or removed",)]),
    C("P10-66", R, "placement status prefixes writable", [("create", np(ipv4=V4), "ok"),
        ("status", ("networkplacements", "p", {"prefixes": ["10.60.0.0/24"]}), "ok")]),
    C("P10-67", R, "printer column Prefix shows status.prefixes[0]", [("create", np(ipv4=V4), "ok"),
        ("status", ("networkplacements", "p", {"prefixes": ["10.60.0.0/24", "fd12:3456:789a::/64"]}), "ok"),
        ("printer", ("networkplacements", "p", "10.60.0.0/24"), "ok")]),
    C("P10-68", R, "empty network name refused", [("create", np(ipv4=V4, network=""), "deny")]),
]

# ─── R-api §5 cases (C1-C3) ──────────────────────────────────────────────────
A = "r-api"
CASES += [
    C("RAPI-01", A, "IPv6-only network, tenant ULA", [("create", net(prefixes=["fd12:3456:789a::/48"]), "ok")]),
    C("RAPI-02", A, "IPv6-only network, platformPrefixes only", [("create", net(platform=P6), "ok")]),
    C("RAPI-03", A, "empty prefixes, no platformPrefixes", [("create", net(prefixes=[]), "deny:needs address space")]),
    C("RAPI-04", A, "both IPv6 sources refused", [("create", net(prefixes=["fd12:3456:789a::/48"], platform=P6), "deny:either from prefixes or from platformPrefixes")]),
    C("RAPI-05", A, "::/32 refused (outside 2000::/3 and fc00::/7)", [("create", net(prefixes=["::/32"]), "deny:global unicast (2000::/3) or unique local")]),
    C("RAPI-06", A, "fe80::/48 refused (link-local)", [("create", net(prefixes=["fe80::/48"]), "deny:global unicast (2000::/3) or unique local")]),
    C("RAPI-07", A, "ff00::/32 refused (multicast)", [("create", net(prefixes=["ff00::/32"]), "deny:global unicast (2000::/3) or unique local")]),
    C("RAPI-08", A, "::ffff:0:0/96 refused (IPv4-mapped; length too)", [("create", net(prefixes=["::ffff:0.0.0.0/96"]), "deny")]),
    C("RAPI-09", A, "tenant GUA 2001:db8:abcd::/48 accepted", [("create", net(prefixes=["2001:db8:abcd::/48"]), "ok")]),
    C("RAPI-10", A, "ULA fc00::/32 accepted (non-fd ULA half)", [("create", net(prefixes=["fc00:1::/32"]), "ok")]),
    C("RAPI-11", A, "3fff:ffff::/32 accepted (top of 2000::/3)", [("create", net(prefixes=["3fff:ffff::/32"]), "ok")]),
    C("RAPI-12", A, "4000::/32 refused (outside 2000::/3)", [("create", net(prefixes=["4000::/32"]), "deny:global unicast")]),
    C("RAPI-13", A, "platformPrefixes edit refused", [("create", net(prefixes=NET4, platform=[{"family": 6, "prefixLength": 56}]), "ok"),
        upd("hybridnetworks", "n", setk(["platformPrefixes"], [{"family": 6, "prefixLength": 60}]))[:2] + ("deny:cannot be changed",)]),
    C("RAPI-14", A, "platformPrefixes pin added later refused (entry change)", [("create", net(prefixes=NET4, platform=P6), "ok"),
        upd("hybridnetworks", "n", setk(["platformPrefixes"], [{"family": 6, "prefix": "2001:db8:4000::/56"}]))[:2] + ("deny:cannot be changed",)]),
    C("RAPI-15", A, "platformPrefixes removal accepted (CEL; the job checks use)", [("create", net(prefixes=NET4, platform=P6), "ok"),
        upd("hybridnetworks", "n", delk(["platformPrefixes"]))[:2] + ("ok",)]),
    C("RAPI-16", A, "platformPrefixes removal leaving no space refused", [("create", net(platform=P6), "ok"),
        upd("hybridnetworks", "n", delk(["platformPrefixes"]))[:2] + ("deny:needs address space",)]),
    C("RAPI-17", A, "family 4 in platformPrefixes refused", [("create", net(prefixes=NET4, platform=[{"family": 4}]), "deny:Unsupported value")]),
    C("RAPI-18", A, "platformPrefixes without family refused", [("create", net(prefixes=NET4, platform=[{"prefixLength": 56}]), "deny:Required value")]),
    C("RAPI-19", A, "IPv6-only placement on CloudVirt", [("create", np(ipv6=V6), "ok")]),
    C("RAPI-20", A, "IPv6-only placement on CloudOSO", [("create", np(kind="CloudOSO", ipv6=V6), "ok")]),
    C("RAPI-21", A, "IPv6-only placement on CloudAWS (CEL accepts; placeholder IPv4 by the job)", [("create", np(kind="CloudAWS", ipv6={"prefixLength": 60}), "ok")],
      note="R-api §5 listed this as refused; the plan (§2.3 C3 table, §6.4) makes IPv4 optional in CEL on clouds"),
    C("RAPI-22", A, "IPv6-only placement on CloudGCP (CEL accepts)", [("create", np(kind="CloudGCP", ipv6={"source": "Provider"}), "ok")],
      note="as RAPI-21"),
    C("RAPI-23", A, "CloudAWS IPv6 /60 accepted", [("create", np(kind="CloudAWS", ipv4=V4, ipv6={"prefixLength": 60}), "ok")]),
    C("RAPI-24", A, "CloudAWS IPv6 /56 accepted", [("create", np(kind="CloudAWS", ipv4=V4, ipv6={"prefixLength": 56}), "ok")]),
    C("RAPI-25", A, "CloudAWS IPv6 /64 refused", [("create", np(kind="CloudAWS", ipv4=V4, ipv6={"prefixLength": 64}), "deny:a /56 or /60 on CloudAWS")]),
    C("RAPI-26", A, "CloudAWS IPv6 /52 refused", [("create", np(kind="CloudAWS", ipv4=V4, ipv6={"prefixLength": 52}), "deny:Unsupported value")]),
    C("RAPI-27", A, "CloudAWS pinned IPv6 /60 accepted", [("create", np(kind="CloudAWS", ipv4=V4, ipv6={"prefix": "2001:db8:4000:10::/60"}), "ok")]),
    C("RAPI-28", A, "CloudAWS pinned IPv6 /64 refused", [("create", np(kind="CloudAWS", ipv4=V4, ipv6={"prefix": "2001:db8:4000:10::/64"}), "deny:a /56 or /60 on CloudAWS")]),
    C("RAPI-29", A, "CloudVirt IPv6 /60 refused", [("create", np(ipv4=V4, ipv6={"prefixLength": 60}), "deny:must be a /64")]),
    C("RAPI-30", A, "CloudOSO IPv6 /56 refused", [("create", np(kind="CloudOSO", ipv4=V4, ipv6={"prefixLength": 56}), "deny:must be a /64")]),
    C("RAPI-31", A, "CloudVirt pinned IPv6 /60 refused", [("create", np(ipv4=V4, ipv6={"prefix": "2001:db8:4000:10::/60"}), "deny:must be a /64")]),
    C("RAPI-32", A, "ipv4 added refused", [("create", np(ipv6=V6), "ok"), upd("networkplacements", "p", setk(["ipv4"], V4))[:2] + ("deny:spec.ipv4 cannot be added or removed",)]),
    C("RAPI-33", A, "ipv4 removed refused", [("create", np(ipv4=V4, ipv6=V6), "ok"), upd("networkplacements", "p", delk(["ipv4"]))[:2] + ("deny:spec.ipv4 cannot be added or removed",)]),
    C("RAPI-34", A, "IPv6 size and pin disagree refused", [("create", np(kind="CloudAWS", ipv4=V4, ipv6={"prefixLength": 60, "prefix": "2001:db8:4000::/56"}), "deny:spec.ipv6.prefix must be as long as")]),
    C("RAPI-35", A, "IPv6 size and pin agree accepted", [("create", np(ipv4=V4, ipv6={"prefixLength": 64, "prefix": "2001:db8:4000:1::/64"}), "ok")]),
    C("RAPI-36", A, "pinned IPv6 /48 refused", [("create", np(kind="CloudAWS", ipv4=V4, ipv6={"prefix": "2001:db8:4000::/48"}), "deny:must be a /56, /60 or /64")]),
    C("RAPI-37", A, "CloudAWS IPv6-only, no ipv4 (B3 guard: no evaluation error)", [("create", np(kind="CloudAWS", ipv6={"prefix": "2001:db8:4000::/56"}), "ok")]),
    C("RAPI-38", A, "CloudAWS IPv4 /25 still refused with ipv6 present", [("create", np(kind="CloudAWS", ipv4={"prefixLength": 25}, ipv6={"prefixLength": 60}), "deny:CloudAWS placement must be /16 to /24")]),
]

# ─── platformPrefixes block lengths (OD73: no CRD default, /48-/64 nibble-aligned) ──
B = "platform-block"
CASES += [
    C("BLK-01", B, "prefixLength omitted: server adds no default", [("create", net(platform=P6), "ok"),
        ("check", ("hybridnetworks", "n", lambda o: None if "prefixLength" not in o["spec"]["platformPrefixes"][0] else "a default was added", ""), "ok")]),
    C("BLK-02", B, "prefixLength 48 accepted", [("create", net(platform=[{"family": 6, "prefixLength": 48}]), "ok")]),
    C("BLK-03", B, "prefixLength 52 accepted", [("create", net(platform=[{"family": 6, "prefixLength": 52}]), "ok")]),
    C("BLK-04", B, "prefixLength 56 accepted", [("create", net(platform=[{"family": 6, "prefixLength": 56}]), "ok")]),
    C("BLK-05", B, "prefixLength 60 accepted", [("create", net(platform=[{"family": 6, "prefixLength": 60}]), "ok")]),
    C("BLK-06", B, "prefixLength 64 accepted", [("create", net(platform=[{"family": 6, "prefixLength": 64}]), "ok")]),
    C("BLK-07", B, "prefixLength 44 refused", [("create", net(platform=[{"family": 6, "prefixLength": 44}]), "deny:greater than or equal to 48")]),
    C("BLK-08", B, "prefixLength 68 refused", [("create", net(platform=[{"family": 6, "prefixLength": 68}]), "deny:less than or equal to 64")]),
    C("BLK-09", B, "prefixLength 50 refused (not nibble-aligned)", [("create", net(platform=[{"family": 6, "prefixLength": 50}]), "deny:multiple of 4")]),
    C("BLK-10", B, "prefixLength 63 refused", [("create", net(platform=[{"family": 6, "prefixLength": 63}]), "deny:multiple of 4")]),
    C("BLK-11", B, "pinned /56 with prefixLength 56", [("create", net(platform=[{"family": 6, "prefixLength": 56, "prefix": "2001:db8:4000::/56"}]), "ok")]),
    C("BLK-12", B, "pinned /56 without prefixLength", [("create", net(platform=[{"family": 6, "prefix": "2001:db8:4000::/56"}]), "ok")]),
    C("BLK-13", B, "pinned /56 with prefixLength 60 refused", [("create", net(platform=[{"family": 6, "prefixLength": 60, "prefix": "2001:db8:4000::/56"}]), "deny:as long as prefixLength")]),
    C("BLK-14", B, "pinned non-canonical refused", [("create", net(platform=[{"family": 6, "prefix": "2001:db8:4000::1/56"}]), "deny:canonical form")]),
    C("BLK-15", B, "pinned IPv4 refused", [("create", net(platform=[{"family": 6, "prefix": "10.60.0.0/16"}]), "deny:IPv6 network prefix")]),
    C("BLK-16", B, "pinned /50 refused", [("create", net(platform=[{"family": 6, "prefix": "2001:db8:4000::/50"}]), "deny:/48 to /64 in steps of 4")]),
    C("BLK-17", B, "pinned /44 refused", [("create", net(platform=[{"family": 6, "prefix": "2001:db8::/44"}]), "deny:/48 to /64 in steps of 4")]),
    C("BLK-18", B, "two family-6 entries refused (map key)", [("create", net(platform=[{"family": 6}, {"family": 6, "prefixLength": 60}]), "deny:Duplicate value")]),
    C("BLK-19", B, "IPv4 tenant + IPv6 platform (dual-stack mode 2)", [("create", net(prefixes=NET4, platform=[{"family": 6, "prefixLength": 56}]), "ok")]),
    C("BLK-20", B, "platformPrefixes added to an IPv4 network", [("create", net(prefixes=NET4), "ok"),
        upd("hybridnetworks", "n", setk(["platformPrefixes"], P6))[:2] + ("ok",)]),
]

# ─── OD63 modes (derived from the fields) ────────────────────────────────────
M = "od63-mode"
STATUS_T = {"ipv6": {"mode": "Tenant"},
            "addressSpace": [{"prefix": "10.60.0.0/16", "family": 4, "source": "Tenant", "allocations": 0, "largestFree": 16},
                             {"prefix": "2001:db8:abcd::/48", "family": 6, "source": "Tenant", "allocations": 0, "largestFree": 48}],
            "egress": {"internet": "unavailable", "sourceAddresses": ["192.0.2.10"],
                       "families": [{"family": 4, "state": "ready", "mode": "SourceNAT"},
                                    {"family": 6, "state": "unavailable", "reason": "AddressChoice",
                                     "message": "IPv6 internet: not offered for tenant-provided prefixes"}]}}
STATUS_P = {"ipv6": {"mode": "Platform"},
            "addressSpace": [{"prefix": "2001:db8:4000::/56", "family": 6, "source": "Platform", "allocations": 2, "largestFree": 57}],
            "egress": {"internet": "ready", "families": [{"family": 6, "state": "ready", "mode": "Routed"}],
                       "ipv4Translation": {"state": "ready"}}}
STATUS_N = {"ipv6": {"mode": "None"}, "egress": {"internet": "ready", "families": [{"family": 4, "state": "ready", "mode": "SourceNAT"}]}}


def status_roundtrip(kind, name, st):
    def fn(o):
        got = o.get("status", {})
        for k, v in st.items():
            if got.get(k) != v:
                return f"status.{k} came back as {json.dumps(got.get(k))}"
        return None
    return [("status", (kind, name, st), "ok"), ("check", (kind, name, fn, ""), "ok")]


CASES += [
    C("MODE-01", M, "Tenant: IPv4 + tenant GUA", [("create", net(prefixes=["10.60.0.0/16", "2001:db8:abcd::/48"]), "ok")] + status_roundtrip("hybridnetworks", "n", STATUS_T)),
    C("MODE-02", M, "Tenant: IPv6-only ULA", [("create", net(prefixes=["fd12:3456:789a::/48"]), "ok")]),
    C("MODE-03", M, "Tenant: two IPv6 prefixes", [("create", net(prefixes=["2001:db8:abcd::/48", "fd12:3456:789a::/48"]), "ok")]),
    C("MODE-04", M, "Platform: IPv6-only", [("create", net(platform=P6), "ok")] + status_roundtrip("hybridnetworks", "n", STATUS_P)),
    C("MODE-05", M, "Platform: IPv4 tenant + IPv6 platform", [("create", net(prefixes=NET4, platform=P6), "ok")]),
    C("MODE-06", M, "None: IPv4 only", [("create", net(prefixes=NET4), "ok")] + status_roundtrip("hybridnetworks", "n", STATUS_N)),
    C("MODE-07", M, "None without IPv4: no address space refused", [("create", net(prefixes=[], platform=[]), "deny:needs address space")]),
    C("MODE-08", M, "mixed: tenant IPv6 + platform IPv6 refused", [("create", net(prefixes=NET46, platform=P6), "deny:either from prefixes or from platformPrefixes")]),
    C("MODE-09", M, "mixed: tenant GUA + platform, no IPv4, refused", [("create", net(prefixes=["2001:db8:abcd::/48"], platform=[{"family": 6, "prefixLength": 56}]), "deny:either from prefixes or from platformPrefixes")]),
    C("MODE-10", M, "mixed by edit: Tenant network gets platformPrefixes, refused", [("create", net(prefixes=NET46), "ok"),
        upd("hybridnetworks", "n", setk(["platformPrefixes"], P6))[:2] + ("deny:either from prefixes or from platformPrefixes",)]),
    C("MODE-11", M, "mixed by edit: Platform network gets an IPv6 prefix, refused", [("create", net(prefixes=NET4, platform=P6), "ok"),
        upd("hybridnetworks", "n", setk(["prefixes"], NET46))[:2] + ("deny:either from prefixes or from platformPrefixes",)]),
    C("MODE-12", M, "mode change Tenant -> Platform in one edit, with placements: CEL accepts (job: IPv6ModeInUse)", [
        ("create", net(prefixes=NET46), "ok"), ("create", np(ipv4=V4, ipv6=V6), "ok"),
        upd("hybridnetworks", "n", lambda s: (s.__setitem__("prefixes", NET4), s.__setitem__("platformPrefixes", P6)))[:2] + ("ok",)],
      note="CEL can't see placements; the network job refuses with IPv6ModeInUse"),
    C("MODE-13", M, "mode change None -> Platform with placements: CEL accepts (job: IPv6ModeInUse)", [
        ("create", net(prefixes=NET4), "ok"), ("create", np(ipv4=V4), "ok"),
        upd("hybridnetworks", "n", setk(["platformPrefixes"], P6))[:2] + ("ok",)], note="job"),
    C("MODE-14", M, "mode change None -> Tenant with placements: CEL accepts (job: IPv6ModeInUse)", [
        ("create", net(prefixes=NET4), "ok"), ("create", np(ipv4=V4), "ok"),
        upd("hybridnetworks", "n", setk(["prefixes"], NET46))[:2] + ("ok",)], note="job"),
    C("MODE-15", M, "mode change Platform -> None with placements: CEL accepts (job: IPv6ModeInUse / PrefixInUse)", [
        ("create", net(prefixes=NET4, platform=P6), "ok"), ("create", np(ipv4=V4, ipv6=V6), "ok"),
        upd("hybridnetworks", "n", delk(["platformPrefixes"]))[:2] + ("ok",)], note="job"),
    C("MODE-16", M, "mode change Tenant -> Platform, no placements: accepted", [
        ("create", net(prefixes=["fd12:3456:789a::/48"]), "ok"),
        upd("hybridnetworks", "n", lambda s: (s.pop("prefixes"), s.__setitem__("platformPrefixes", P6)))[:2] + ("ok",)]),
    C("MODE-17", M, "ipv6 placement on a None network: CEL accepts (job: FamilyUnavailable)", [
        ("create", net(prefixes=NET4), "ok"), ("create", np(ipv4=V4, ipv6=V6), "ok")], note="CEL can't see the network; job"),
    C("MODE-18", M, "status mode and per-family egress survive (no pruning)", [("create", net(prefixes=NET4, platform=P6), "ok")]
        + status_roundtrip("hybridnetworks", "n", {"ipv6": {"mode": "Platform"},
                                                   "egress": {"internet": "ready", "sourceAddresses": ["192.0.2.10"],
                                                              "families": [{"family": 4, "state": "ready", "mode": "SourceNAT"},
                                                                           {"family": 6, "state": "ready", "mode": "Routed", "reason": "Degraded",
                                                                            "message": "one upstream is down"}]}})),
]

# ─── spec.ipv6.source (OD61/OD68) ────────────────────────────────────────────
S = "source"
CASES += [
    C("SRC-01", S, "CloudAWS Provider, no size", [("create", np(kind="CloudAWS", ipv4=V4, ipv6={"source": "Provider"}), "ok")]),
    C("SRC-02", S, "CloudGCP Provider, no size", [("create", np(kind="CloudGCP", ipv4=V4, ipv6={"source": "Provider"}), "ok")]),
    C("SRC-03", S, "CloudGCP ipv6 {} (site default source)", [("create", np(kind="CloudGCP", ipv4=V4, ipv6={}), "ok")]),
    C("SRC-04", S, "CloudAWS ipv6 {} (site default source and size)", [("create", np(kind="CloudAWS", ipv4=V4, ipv6={}), "ok")]),
    C("SRC-05", S, "CloudVirt Provider refused", [("create", np(ipv4=V4, ipv6={"source": "Provider"}), "deny:Provider is offered only on CloudAWS and CloudGCP")]),
    C("SRC-06", S, "CloudOSO Provider refused", [("create", np(kind="CloudOSO", ipv4=V4, ipv6={"source": "Provider"}), "deny:Provider is offered only on CloudAWS and CloudGCP")]),
    C("SRC-07", S, "Provider with prefixLength refused", [("create", np(kind="CloudAWS", ipv4=V4, ipv6={"source": "Provider", "prefixLength": 56}), "deny:source Provider takes no prefixLength or prefix")]),
    C("SRC-08", S, "Provider with prefix refused", [("create", np(kind="CloudGCP", ipv4=V4, ipv6={"source": "Provider", "prefix": "2001:db8:4000:1::/64"}), "deny:source Provider takes no prefixLength or prefix")]),
    C("SRC-09", S, "CloudVirt Network /64", [("create", np(ipv4=V4, ipv6={"source": "Network", "prefixLength": 64}), "ok")]),
    C("SRC-10", S, "CloudVirt Network without size refused", [("create", np(ipv4=V4, ipv6={"source": "Network"}), "deny:needs prefixLength or prefix")]),
    C("SRC-11", S, "unknown source refused", [("create", np(kind="CloudAWS", ipv4=V4, ipv6={"source": "Amazon"}), "deny:Unsupported value")]),
    C("SRC-12", S, "source change refused", [("create", np(kind="CloudAWS", ipv4=V4, ipv6={"source": "Network", "prefixLength": 60}), "ok"),
        upd("networkplacements", "p", setk(["ipv6"], {"source": "Provider"}))[:2] + ("deny:spec.ipv6 is immutable",)]),
    C("SRC-13", S, "CloudGCP Network /64 (CEL accepts; job: FamilyUnavailable)", [("create", np(kind="CloudGCP", ipv4=V4, ipv6={"source": "Network", "prefixLength": 64}), "ok")], note="job"),
    C("SRC-14", S, "CloudGCP IPv6 /60 refused", [("create", np(kind="CloudGCP", ipv4=V4, ipv6={"prefixLength": 60}), "deny:must be a /64")]),
    C("SRC-15", S, "CloudAWS Network /56 IPv6-only", [("create", np(kind="CloudAWS", ipv6={"source": "Network", "prefixLength": 56}), "ok")]),
    C("SRC-16", S, "CloudAWS Provider IPv6-only", [("create", np(kind="CloudAWS", ipv6={"source": "Provider"}), "ok")]),
    C("SRC-17", S, "placement status subnets and ipv6 survive (no pruning)", [("create", np(kind="CloudAWS", ipv4=V4, ipv6={"source": "Provider"}), "ok")]
        + status_roundtrip("networkplacements", "p", {"prefixes": ["10.61.0.0/24", "2001:db8:1f16::/56"],
                                                      "subnets": [{"family": 4, "prefix": "10.61.0.0/24", "gateway": "10.61.0.1", "source": "Network"},
                                                                  {"family": 6, "prefix": "2001:db8:1f16::/56", "source": "Provider"}],
                                                      "ipv6": {"addressing": "SLAAC", "internet": {"state": "ready", "path": "Provider"},
                                                               "translation": {"pref64": "64:ff9b::/96", "resolvers": ["2001:db8::53"]}}})),
]


# ─── V3 status fields (status.ipv6 block, TenantProfile site capabilities) ───
V = "v3-status"


def tprofile():
    return {"apiVersion": API, "kind": "TenantProfile", "metadata": {"name": "shc"}, "spec": {}}


def tp_site(caps):
    return {"sites": [{"name": "site-a", "type": "openshift", "state": "Available", "capabilities": caps}]}


CASES += [
    C("V3-01", V, "network status ipv6 prefixes, block and holdDownUntil survive", [("create", net(prefixes=NET4, platform=P6), "ok")]
        + status_roundtrip("hybridnetworks", "n", {"ipv6": {"mode": "Platform", "prefixes": ["2001:db8:4000::/56"],
                                                            "block": {"prefix": "2001:db8:4000::/56", "prefixLength": 56, "source": "Allocated"}}})),
    C("V3-02", V, "network status ipv6 holdDownUntil while waiting survives", [("create", net(platform=[{"family": 6, "prefix": "2001:db8:4100::/56"}]), "ok")]
        + status_roundtrip("hybridnetworks", "n", {"ipv6": {"mode": "None", "holdDownUntil": "2026-10-31T12:00:00Z"}})),
    C("V3-03", V, "TenantProfile site capabilities take the IPv6 values", [("create", tprofile(), "ok")]
        + status_roundtrip("tenantprofiles", "shc", tp_site(["Virtualization", "HybridNetworking", "DNS", "IPv6", "IPv6Only",
                                                             "IPv6NetworkSource", "IPv6ProviderSource", "IPv6Translation"]))),
    C("V3-04", V, "TenantProfile unknown site capability refused", [("create", tprofile(), "ok"),
        ("status", ("tenantprofiles", "shc", tp_site(["IPv7"])), "deny:Unsupported value")]),
]


# ─── runner ──────────────────────────────────────────────────────────────────

def expectation(exp, mode):
    if isinstance(exp, dict):
        return exp[mode]
    return exp


def judge(rc_ok, err, exp):
    if exp == "ok":
        return rc_ok, ("" if rc_ok else f"refused: {err}")
    want = exp.split(":", 1)[1] if ":" in exp else ""
    if rc_ok:
        return False, "accepted, expected a refusal" + (f" with '{want}'" if want else "")
    if want and want not in err:
        return False, f"refused, but without '{want}': {err}"
    return True, err


def run_case(kube, case, mode, idx):
    ns = f"c{idx:03d}-{case['id'].lower()}"
    kube.apply_docs([{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns}}])
    results = []
    ok_all = True
    for op, payload, exp in case["steps"]:
        exp = expectation(exp, mode) if exp is not None else "ok"
        if op == "create":
            body = copy.deepcopy(payload)
            body["metadata"]["namespace"] = ns
            r = kube.run(["create", "-f", "-", "-o", "name"], body=body)
            good, why = judge(r.returncode == 0, r.stderr.strip(), exp)
            desc = f"create {body['kind']}"
        elif op == "update":
            kind, name, fn = payload
            live, _, err = kube.json(["get", kind, name, "-n", ns])
            if live is None:
                good, why, desc = False, f"get failed: {err}", f"update {kind}"
            else:
                fn(live["spec"])
                r = kube.run(["replace", "-f", "-", "-o", "name"], body=live)
                good, why = judge(r.returncode == 0, r.stderr.strip(), exp)
                desc = f"update {kind}"
        elif op == "status":
            kind, name, st = payload
            live, _, err = kube.json(["get", kind, name, "-n", ns])
            live["status"] = copy.deepcopy(st)
            r = kube.run(["replace", "--subresource=status", "-f", "-", "-o", "name"], body=live)
            good, why = judge(r.returncode == 0, r.stderr.strip(), exp)
            desc = f"status {kind}"
        elif op == "check":
            kind, name, fn, _ = payload
            live, _, err = kube.json(["get", kind, name, "-n", ns])
            msg = fn(live) if live else f"get failed: {err}"
            good, why, desc = msg is None, msg or "", f"read back {kind}"
        elif op == "printer":
            kind, name, want = payload
            r = kube.run(["get", kind, name, "-n", ns])
            good = r.returncode == 0 and want in r.stdout
            why, desc = ("" if good else f"printer output lacks {want}: {r.stdout.strip()} {r.stderr.strip()}"), "printer column"
        else:
            raise SystemExit(f"unknown op {op}")
        results.append({"step": desc, "expect": exp, "pass": good, "detail": why})
        ok_all = ok_all and good
        if not good:
            break
    kube.run(["delete", "namespace", ns, "--wait=false"])
    return ok_all, results


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--crds", required=True)
    ap.add_argument("--expect", choices=("baseline", "candidate"), required=True)
    ap.add_argument("--assets", default=os.environ.get("KUBEBUILDER_ASSETS", ""))
    ap.add_argument("--report")
    ap.add_argument("--only", action="append", help="case group (repeatable)")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    replay.KUBECTL = str(Path(args.assets) / "kubectl")
    groups = args.only or (["p10"] if args.expect == "baseline" else sorted({c["group"] for c in CASES}))
    cases = [c for c in CASES if c["group"] in groups]
    crds = replay.load_crds(args.crds)
    env = replay.Envtest(args.assets, args.keep)
    report = {"expect": args.expect, "crds": args.crds, "crdInstall": None, "cases": []}
    passed = 0
    try:
        env.start()
        kube = replay.Kube(env.kubeconfig)
        r = kube.run(["create", "-f", "-"], body={"apiVersion": "v1", "kind": "List", "items": list(crds.values())})
        report["crdInstall"] = {"ok": r.returncode == 0, "stderr": r.stderr.strip()}
        if r.returncode != 0:
            print(f"CRD install refused:\n{r.stderr}", file=sys.stderr)
            return 1
        replay.wait_established(kube, list(crds))
        sv = kube.run(["version", "-o", "json"])
        report["serverVersion"] = (json.loads(sv.stdout).get("serverVersion") or {}).get("gitVersion") if sv.returncode == 0 else None
        for i, c in enumerate(cases):
            ok, res = run_case(kube, c, args.expect, i)
            passed += ok
            report["cases"].append({"id": c["id"], "group": c["group"], "desc": c["desc"], "pass": ok, "note": c["note"], "steps": res})
            fail = next((s for s in res if not s["pass"]), None)
            print(f"{'PASS' if ok else 'FAIL'} {c['id']:<8} {c['desc']}" + (f"\n     {fail['step']}: {fail['detail'][:300]}" if fail else ""))
    finally:
        env.stop()
    report["summary"] = {"cases": len(cases), "passed": passed, "failed": len(cases) - passed, "groups": groups}
    print(f"\n{args.expect}: {passed}/{len(cases)} cases pass ({', '.join(groups)}); server {report.get('serverVersion')}")
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=1) + "\n")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
