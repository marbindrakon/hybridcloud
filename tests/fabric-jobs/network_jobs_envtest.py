#!/usr/bin/env python3
"""The HybridNetwork and NetworkPlacement jobs against a throwaway API server (envtest), IPv6 per family.

Starts etcd + kube-apiserver (tests/schema-replay/replay.py backend), installs the chart's CRDs, creates a fabric
(HybridFabric with a ready status and its ledger), tenants, a site with its gateway and environment, then runs the
real roles (eda/rulebooks/roles/hybridnetwork_provision, hybridnetwork_teardown, networkplacement_provision, the
Entity job's tenant profile steps, and hybridfabric_provision up to its node work) with ansible-playbook against it
and checks status and ledger after each step. Nothing outside the throwaway server is touched (no site, no border
gateway: every step stops before the realization; the fabric job reads its SSH key from a mock KV endpoint of this
harness and stops at an unreachable documentation address).

  python3 -I tests/fabric-jobs/network_jobs_envtest.py [--only CASE] [--keep] [--verbose]

Needs KUBEBUILDER_ASSETS (etcd, kube-apiserver, kubectl), openssl, ansible-playbook with kubernetes.core and the
kubernetes Python client. Addresses are documentation ranges (2001:db8::/32, 192.0.2.0/24, 198.51.100.0/24) and a
test pool of two /56 blocks, so pool exhaustion, the per-tenant allowance and the hold-down (0 days) show quickly.
Exit 0 only when every case passes.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "tests" / "schema-replay"))
import replay  # noqa: E402

API = "hybridsovereign.redhat/v1alpha1"
PNS = "sovereign-cloud"
FABRIC = "fab"
POOL = "2001:db8:4000::/55"         # two /56 blocks
PIN = "2001:db8:4000:100::/56"
REALIZATION = {
    "ipam": {"publicPools": {"test-v6": {"family": 6, "prefixes": [POOL], "blockLengths": [56, 60], "defaultBlock": 56,
                                         "holdDownDays": 0, "perTenantBlocks": 1, "perTenantOverrides": {"b": 2}}}},
    "network": {"exclusiveRanges": ["2001:db8:4000::/48", "64:ff9b::/96"]},
    "attachments": {"reservedAsns": [64511]},
}


def obj(kind, name, ns, spec, labels=None):
    md = {"name": name, "namespace": ns} if ns else {"name": name}
    if labels:
        md["labels"] = labels
    return {"apiVersion": API, "kind": kind, "metadata": md, "spec": spec}


class Lab:
    def __init__(self, kube, server, token, verbose=False):
        self.kube, self.server, self.token, self.verbose = kube, server, token, verbose
        self.tmp = Path(tempfile.mkdtemp(prefix="shc-jobs-"))

    # ---- objects ----
    def create(self, o):
        self.kube.run(["create", "-f", "-"], body=o, check=True)

    def apply(self, o):
        self.kube.run(["apply", "--server-side", "--force-conflicts", "-f", "-"], body=o, check=True)

    def status(self, plural, name, ns, st):
        live, _, err = self.kube.json(["get", plural, name, "-n", ns] if ns else ["get", plural, name])
        if live is None:
            raise RuntimeError(err)
        live["status"] = st
        self.kube.run(["replace", "--subresource=status", "-f", "-"], body=live, check=True)

    def get(self, plural, name, ns):
        live, _, _ = self.kube.json(["get", plural, name, "-n", ns])
        return live

    def ledger(self):
        cm, _, _ = self.kube.json(["get", "configmap", f"fabric-ledger-{FABRIC}", "-n", PNS])
        return json.loads(cm["data"]["ledger.json"])

    def setup(self):
        for ns, ent in ((PNS, None), ("entity-a", "a"), ("entity-b", "b")):
            md = {"name": ns, "labels": {"hybridsovereign.redhat/entity": ent}} if ent else {"name": ns}
            self.kube.run(["create", "-f", "-"], body={"apiVersion": "v1", "kind": "Namespace", "metadata": md}, check=True)
        self.create(obj("HybridFabric", FABRIC, PNS, {
            "domainAsn": 64500, "overlayMtu": 9000, "vniPool": {"start": 51000, "end": 51999},
            "l2vniPool": {"start": 151000, "end": 151999},
            "borderGateway": {"credentialsRef": {"vaultPath": "fabric/bgw"},
                              "nodes": [{"name": "n1", "address": "192.0.2.1"}]}}))
        self.status("hybridfabrics", FABRIC, PNS, {"status": "ready", "ready": True, "realized": True,
                                                   "realization": {"egress": {"enabled": False}}})
        led = {"version": 1, "fabric": FABRIC,
               "pools": {"vni": [51000, 51999], "l2vni": [151000, 151999], "vpnTarget": [10000, 10999],
                         "peIndex": [1, 64], "egress": [1, 64], "attachVlan": [3000, 3099], "attachLink": [0, 511]},
               "alloc": {}}
        self.kube.run(["create", "-f", "-"], body={"apiVersion": "v1", "kind": "ConfigMap",
                                                  "metadata": {"name": f"fabric-ledger-{FABRIC}", "namespace": PNS},
                                                  "data": {"ledger.json": json.dumps(led)}}, check=True)
        # a site (OpenShift, IPv4 only as every site today), its gateway and a VM environment of tenant a
        self.create(obj("CloudInfrastructure", "site1", PNS, {"type": "openshift", "credentialsRef": {"vaultPath": "x/site1"},
                                                              "openshift": {"clusterRef": "site1"}}))
        self.status("cloudinfrastructures", "site1", PNS, {"status": "ready", "ready": True,
                                                           "capabilities": {"api": True, "evpn": True, "virtualization": True,
                                                                            "ipv6": False, "ipv6Only": False,
                                                                            "ipv6NetworkSource": False}})
        self.create(obj("CloudGateway", "gw-site1", PNS, {
            "fabricRef": {"name": FABRIC}, "cloudRef": {"name": "site1"},
            "siteUnderlay": {"vlan": 100, "cidr": "198.51.100.0/24", "addresses": [{"node": "n1", "address": "198.51.100.10"}]}}))
        self.status("cloudgateways", "gw-site1", PNS, {"status": "ready", "ready": True, "realized": True})
        self.create(obj("CloudVirt", "vms", "entity-a", {"cloudRef": {"name": "site1"}}))
        self.status("cloudvirts", "vms", "entity-a", {"status": "ready", "ready": True, "realized": True})

    # ---- jobs ----
    def job(self, role, kind, name, ns, extra_env=None):
        play = [{"hosts": "localhost", "connection": "local", "gather_facts": False, "roles": [role]}]
        (self.tmp / "play.yml").write_text(yaml.safe_dump(play))
        (self.tmp / "ansible.cfg").write_text("[defaults]\nroles_path = %s\ncollections_path = %s:%s\n" % (
            ROOT / "eda" / "rulebooks" / "roles", ROOT / "eda" / "rulebooks" / "collections",
            ":".join([os.path.expanduser("~/.ansible/collections"), "/usr/share/ansible/collections"])))
        v = {"services_api_host": self.server, "services_bearer_token": self.token, "services_validate_certs": False,
             "fabric_realization": REALIZATION,
             "event_payload": {"regarding": {"kind": kind, "name": name, "namespace": ns}, "eventId": f"t-{time.time()}"}}
        (self.tmp / "vars.json").write_text(json.dumps(v))
        env = dict(os.environ, ANSIBLE_CONFIG=str(self.tmp / "ansible.cfg"), ANSIBLE_LOCALHOST_WARNING="false",
                   ANSIBLE_INVENTORY_UNPARSED_WARNING="false", ANSIBLE_REMOTE_TMP=str(self.tmp / "rt"),
                   ANSIBLE_LOCAL_TEMP=str(self.tmp / "lt"), **(extra_env or {}))
        p = subprocess.run(["ansible-playbook", "-i", "localhost,", "play.yml", "-e", "@vars.json"], cwd=self.tmp,
                           env=env, capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL)
        if self.verbose:
            print(p.stdout[-4000:])
        return p

    def profile(self, entity, ns):
        """The Entity job's tenant profile steps (compute + write) for one tenant; returns the TenantProfile."""
        role = ROOT / "eda" / "rulebooks" / "roles" / "entity_provision"
        steps = [{"name": f"profile {t}", "ansible.builtin.include_role": {"name": str(role), "tasks_from": f"tenant_profile_{t}.yml"}}
                 for t in ("compute", "write")]
        play = [{"hosts": "localhost", "connection": "local", "gather_facts": False, "tasks": steps}]
        (self.tmp / "profile.yml").write_text(yaml.safe_dump(play))
        v = {"services_api_host": self.server, "services_bearer_token": self.token, "services_validate_certs": False,
             "entity_cr_name": entity, "entity_cr_namespace": PNS, "entity_namespace": ns, "entity_spec": {},
             "entity_generation": 1}
        (self.tmp / "pvars.json").write_text(json.dumps(v))
        self.job_env_play("profile.yml", "pvars.json")
        return self.get("tenantprofiles", "shc", ns)

    def job_env_play(self, play, varfile):
        env = dict(os.environ, ANSIBLE_CONFIG=str(self.tmp / "ansible.cfg"), ANSIBLE_LOCALHOST_WARNING="false",
                   ANSIBLE_INVENTORY_UNPARSED_WARNING="false", ANSIBLE_REMOTE_TMP=str(self.tmp / "rt"),
                   ANSIBLE_LOCAL_TEMP=str(self.tmp / "lt"))
        p = subprocess.run(["ansible-playbook", "-i", "localhost,", play, "-e", "@" + varfile], cwd=self.tmp,
                           env=env, capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL)
        if self.verbose:
            print(p.stdout[-4000:])
        if p.returncode != 0:
            raise RuntimeError(p.stdout[-1500:])
        return p

    def network(self, name, ns="entity-a"):
        self.job("hybridnetwork_provision", "HybridNetwork", name, ns)
        return self.get("hybridnetworks", name, ns)

    def placement(self, name, ns="entity-a"):
        self.job("networkplacement_provision", "NetworkPlacement", name, ns)
        return self.get("networkplacements", name, ns)


def ready_reason(o):
    c = [x for x in (o.get("status") or {}).get("conditions") or [] if x.get("type") == "Ready"]
    return c[0].get("reason") if c else None


def net(name, ns="entity-a", prefixes=None, platform=None, internet=False):
    spec = {"fabricRef": {"name": FABRIC}, "egress": {"internet": internet}}
    if prefixes is not None:
        spec["prefixes"] = prefixes
    if platform is not None:
        spec["platformPrefixes"] = platform
    return obj("HybridNetwork", name, ns, spec)


# ─── cases (run in order; later ones use the state the earlier left) ─────────

def c_tenant_core(lab):
    """a core-like network: IPv4 + ULA, internet on -> Ready, mode Tenant, IPv6 egress AddressChoice"""
    lab.create(net("core", prefixes=["10.60.0.0/16", "fd12:3456:789a::/48"], internet=True))
    o = lab.network("core")
    st = o["status"]
    assert ready_reason(o) == "Ready", st.get("message")
    assert st["ipv6"]["mode"] == "Tenant", st["ipv6"]
    assert st["ipv6"]["prefixes"] == ["fd12:3456:789a::/48"]
    fam = {f["family"]: f for f in st["egress"]["families"]}
    assert fam[6]["reason"] == "AddressChoice" and fam[4]["state"] == "unavailable", st["egress"]
    assert st["egress"]["internet"] == "unavailable"           # the fabric offers no egress here: the IPv4 state
    assert [r.get("source", "Tenant") for r in st["addressSpace"]] == ["Tenant", "Tenant"]


def c_platform_dual(lab):
    """dual-stack mode Platform -> the first /56 of the pool, source Platform, IPv6 egress NotOffered"""
    lab.create(net("p1", prefixes=["10.61.0.0/16"], platform=[{"family": 6}], internet=True))
    o = lab.network("p1")
    st = o["status"]
    assert ready_reason(o) == "Ready", st.get("message")
    assert st["ipv6"]["mode"] == "Platform"
    assert st["ipv6"]["block"] == {"prefix": "2001:db8:4000::/56", "prefixLength": 56, "source": "Allocated"}, st["ipv6"]
    rows = {r["prefix"]: r for r in st["addressSpace"]}
    assert rows["2001:db8:4000::/56"].get("source") == "Platform", st["addressSpace"]
    fam = {f["family"]: f for f in st["egress"]["families"]}
    assert fam[6]["reason"] == "NotOffered", st["egress"]
    led = lab.ledger()
    assert led["ipamPools"]["test-v6"]["alloc"]["HybridNetwork/entity-a/p1"] == ["2001:db8:4000::/56"]
    # idempotent
    o = lab.network("p1")
    assert o["status"]["ipv6"]["block"]["prefix"] == "2001:db8:4000::/56"


def c_pinned(lab):
    """pinned block (tenant b) -> source Pinned"""
    lab.create(net("pin", ns="entity-b", platform=[{"family": 6, "prefix": PIN}]))
    o = lab.network("pin", "entity-b")
    st = o["status"]
    assert ready_reason(o) == "Ready", st.get("message")
    assert st["ipv6"]["block"] == {"prefix": PIN, "prefixLength": 56, "source": "Pinned"}, st["ipv6"]


def c_allowance(lab):
    """a second block for tenant a (allowance 1) waits with the allowance message"""
    lab.create(net("p2", platform=[{"family": 6}]))
    o = lab.network("p2")
    assert ready_reason(o) == "WaitingForAddressSpace", o["status"].get("message")
    assert "allowance" in o["status"]["message"], o["status"]["message"]
    assert o["status"]["status"] == "pending"


def c_exhausted(lab):
    """tenant b (allowance 2) asks for a second block: the pool of two is full"""
    lab.create(net("p3", ns="entity-b", platform=[{"family": 6}]))
    o = lab.network("p3", "entity-b")
    assert ready_reason(o) == "WaitingForAddressSpace", o["status"].get("message")
    assert "no free /56" in o["status"]["message"], o["status"]["message"]


def c_size_not_offered(lab):
    """a /52 the pool doesn't offer -> PrefixInvalid"""
    lab.create(net("p52", platform=[{"family": 6, "prefixLength": 52}]))
    o = lab.network("p52")
    assert ready_reason(o) == "PrefixInvalid", o["status"].get("message")


def c_reserved(lab):
    """tenant prefixes overlapping the platform's space (inside, around) -> PrefixReserved; a GUA beside it is fine"""
    for n, pfx in (("r1", "2001:db8:4000:8000::/52"), ("r2", "2001:db8::/32")):
        lab.create(net(n, prefixes=["10.62.0.0/16", pfx]))
        o = lab.network(n)
        assert ready_reason(o) == "PrefixReserved", (n, o["status"].get("message"))
    lab.create(net("gua", prefixes=["2001:db9:abcd::/48"]))
    o = lab.network("gua")
    assert ready_reason(o) == "Ready" and o["status"]["ipv6"]["mode"] == "Tenant", o["status"].get("message")


def c_overlap_tenant(lab):
    """a mode-Tenant IPv6 prefix overlapping another network of the same tenant -> PrefixInUse; other tenant fine"""
    lab.create(net("ula2", prefixes=["fd12:3456:789a:1::/64"]))
    o = lab.network("ula2")
    assert ready_reason(o) == "PrefixInUse", o["status"].get("message")
    lab.create(net("ula-b", ns="entity-b", prefixes=["fd12:3456:789a::/48"]))
    o = lab.network("ula-b", "entity-b")
    assert ready_reason(o) == "Ready", o["status"].get("message")


def c_v6only_tenant(lab):
    """an IPv6-only mode-Tenant network is accepted; its IPv6 placement is refused and holds nothing"""
    lab.create(net("v6only", prefixes=["fd99:1::/48"]))
    o = lab.network("v6only")
    assert ready_reason(o) == "Ready", o["status"].get("message")
    assert o["status"]["ipv6"]["mode"] == "Tenant"
    lab.create(obj("NetworkPlacement", "v6p", "entity-a", {"network": "v6only", "backend": {"kind": "CloudVirt", "name": "vms"},
                                                          "ipv6": {"prefixLength": 64}}))
    p = lab.placement("v6p")
    assert ready_reason(p) == "FamilyUnavailable", (p.get("status") or {}).get("message")
    assert "IPv6 is not available on this environment's site (site1)" in p["status"]["message"], p["status"]["message"]
    led = lab.ledger()
    assert "NetworkPlacement/entity-a/v6p" not in (led["ipam"]["HybridNetwork/entity-a/v6only"].get("alloc") or {})


def c_placement_mode_none(lab):
    """an ipv6 placement of a network without IPv6 -> FamilyUnavailable (no IPv6 address space)"""
    lab.create(net("v4net", prefixes=["10.63.0.0/16"]))
    o = lab.network("v4net")
    assert ready_reason(o) == "Ready" and o["status"]["ipv6"]["mode"] == "None", o["status"].get("message")
    lab.create(obj("NetworkPlacement", "v4p6", "entity-a", {"network": "v4net", "backend": {"kind": "CloudVirt", "name": "vms"},
                                                           "ipv4": {"prefixLength": 24}, "ipv6": {"prefixLength": 64}}))
    p = lab.placement("v4p6")
    assert ready_reason(p) == "FamilyUnavailable", (p.get("status") or {}).get("message")
    assert "the network has no IPv6 address space" in p["status"]["message"], p["status"]["message"]


def c_mode_in_use(lab):
    """a mode change while a placement exists -> IPv6ModeInUse, the realized mode stays; without placements allowed"""
    o = lab.get("hybridnetworks", "v6only", "entity-a")
    o["spec"]["prefixes"] = ["10.64.0.0/16"]
    o["spec"]["platformPrefixes"] = [{"family": 6}]
    lab.kube.run(["replace", "-f", "-"], body=o, check=True)
    o = lab.network("v6only")
    assert ready_reason(o) == "IPv6ModeInUse", o["status"].get("message")
    assert o["status"]["ipv6"]["mode"] == "Tenant"
    led = lab.ledger()
    assert "HybridNetwork/entity-a/v6only" not in led["ipamPools"]["test-v6"]["alloc"]
    # gua (no placements): Tenant -> None is a re-create, allowed
    g = lab.get("hybridnetworks", "gua", "entity-a")
    g["spec"]["prefixes"] = ["10.65.0.0/16"]
    lab.kube.run(["replace", "-f", "-"], body=g, check=True)
    g = lab.network("gua")
    assert ready_reason(g) == "Ready" and g["status"]["ipv6"]["mode"] == "None", g["status"].get("message")
    # (status is merge-patched: a null removes the field)
    assert g["status"]["ipv6"]["prefixes"] == [] and g["status"]["ipv6"].get("block") is None, g["status"]["ipv6"]


def c_release_wakes(lab):
    """teardown of p1 gives its block back (hold-down 0); the waiting p2 of the same tenant then gets it"""
    lab.job("hybridnetwork_teardown", "HybridNetwork", "p1", "entity-a")
    led = lab.ledger()
    pool = led["ipamPools"]["test-v6"]
    assert "HybridNetwork/entity-a/p1" not in pool["alloc"], pool
    assert "HybridNetwork/entity-a/p1" not in led.get("ipam", {}), "address space not dropped"
    p2 = lab.get("hybridnetworks", "p2", "entity-a")
    ann = (p2["metadata"].get("annotations") or {})
    assert "hybridsovereign.redhat/nudged" in ann, "the waiting network was not woken"
    lab.kube.run(["delete", "hybridnetwork", "p1", "-n", "entity-a", "--wait=false"])
    o = lab.network("p2")
    assert ready_reason(o) == "Ready", o["status"].get("message")
    assert o["status"]["ipv6"]["block"]["prefix"] == "2001:db8:4000::/56"


def c_leave_platform(lab):
    """mode Platform -> None without placements gives the block back; tenant b's waiting p3 is woken"""
    o = lab.get("hybridnetworks", "pin", "entity-b")
    o["spec"].pop("platformPrefixes")
    o["spec"]["prefixes"] = ["10.66.0.0/16"]
    lab.kube.run(["replace", "-f", "-"], body=o, check=True)
    o = lab.network("pin", "entity-b")
    assert ready_reason(o) == "Ready" and o["status"]["ipv6"]["mode"] == "None", o["status"].get("message")
    assert "HybridNetwork/entity-b/pin" not in lab.ledger()["ipamPools"]["test-v6"]["alloc"]
    p3 = lab.get("hybridnetworks", "p3", "entity-b")
    assert "hybridsovereign.redhat/nudged" in (p3["metadata"].get("annotations") or {})
    o = lab.network("p3", "entity-b")
    assert ready_reason(o) == "Ready", o["status"].get("message")


def c_profile_offer(lab):
    """the tenant profile carries the fabric's IPv6 block sizes (status.ipv6Platform); removed when the fabric is not the tenant's"""
    fab = lab.get("hybridfabrics", FABRIC, PNS)
    keep = fab["status"]
    rows = [{"name": "test-v6", "family": 6, "prefixes": [POOL], "blockLengths": [56, 60], "defaultBlock": 56, "blocksUsed": 1}]
    lab.status("hybridfabrics", FABRIC, PNS, dict(keep, realization=dict(keep.get("realization") or {}, ipam={"pools": rows})))
    try:
        tp = lab.profile("a", "entity-a")
        assert tp["status"].get("ipv6Platform") == {"blockLengths": [56, 60], "defaultBlockLength": 56}, tp["status"]
        # written through the status subresource only (the tenant roles grant tenantprofiles get/list/watch and
        # nothing on tenantprofiles/status: tests/rbac-access/cases/tenantprofile.yaml R3); the main resource never
        # carried a status field (kubectl hides managed fields unless asked)
        shown, _, _ = lab.kube.json(["get", "tenantprofiles", "shc", "-n", "entity-a", "--show-managed-fields"])
        mf = shown["metadata"].get("managedFields") or []
        fields = lambda m: json.dumps(m.get("fieldsV1") or {})  # noqa: E731
        assert any(m.get("subresource") == "status" and "f:ipv6Platform" in fields(m) for m in mf), mf
        assert not any("f:status" in fields(m) for m in mf if m.get("subresource") != "status"), mf
        # a fabric limited to another tenant: the offer goes away (merge patch with null)
        fab = lab.get("hybridfabrics", FABRIC, PNS)
        fab["spec"]["entityRefs"] = [{"name": "b"}]
        lab.kube.run(["replace", "-f", "-"], body=fab, check=True)
        tp = lab.profile("a", "entity-a")
        assert "ipv6Platform" not in tp["status"], tp["status"]
        tp = lab.profile("b", "entity-b")
        assert tp["status"].get("ipv6Platform", {}).get("blockLengths") == [56, 60], tp["status"]
        # a fabric without an IPv6 pool row: no offer
        lab.status("hybridfabrics", FABRIC, PNS, dict(keep, realization=dict(keep.get("realization") or {}, ipam={"pools": []})))
        tp = lab.profile("b", "entity-b")
        assert "ipv6Platform" not in tp["status"], tp["status"]
    finally:
        fab = lab.get("hybridfabrics", FABRIC, PNS)
        fab["spec"].pop("entityRefs", None)
        lab.kube.run(["replace", "-f", "-"], body=fab, check=True)
        lab.status("hybridfabrics", FABRIC, PNS, keep)


class MockKv:
    """A KV v2 read endpoint for bgw_connect.yml (env VAULT_ADDR / VAULT_TOKEN of the job): every path answers with a
    placeholder that is not a key. It is never used: the node the job then reaches for is a documentation address."""

    def __init__(self):
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"data": {"data": {"ssh_private_key": "placeholder-not-a-key"}}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()


def c_fabric_pools_step(lab):
    """the fabric job's pool step on its own (include_role pools.yml, role defaults): the ledger's pool rows and the tenants' offer"""
    role = ROOT / "eda" / "rulebooks" / "roles" / "hybridfabric_provision"
    out = lab.tmp / "pools.json"
    play = [{"hosts": "localhost", "connection": "local", "gather_facts": False, "tasks": [
        {"name": "pool step", "ansible.builtin.include_role": {"name": str(role), "tasks_from": "pools.yml"}},
        {"name": "keep the facts", "ansible.builtin.copy": {
            "dest": str(out), "content": "{{ {'ipam': hf_ipam, 'changed': hf_ipam_changed, 'offer': hf_tenant_offer} | to_json }}"}}]}]
    (lab.tmp / "pools.yml").write_text(yaml.safe_dump(play))
    v = {"services_api_host": lab.server, "services_bearer_token": lab.token, "services_validate_certs": False,
         "fabric_realization": REALIZATION, "fab_name": FABRIC}
    (lab.tmp / "poolvars.json").write_text(json.dumps(v))
    lab.job_env_play("pools.yml", "poolvars.json")
    r = json.loads(out.read_text())
    led = lab.ledger()["ipamPools"]["test-v6"]
    assert led["prefixes"] == [POOL] and led["blockLengths"] == [56, 60], led
    rows = r["ipam"]["pools"]
    assert len(rows) == 1 and rows[0]["name"] == "test-v6" and rows[0]["family"] == 6, rows
    assert rows[0]["blockLengths"] == [56, 60] and rows[0]["defaultBlock"] == 56, rows
    assert rows[0]["blocksUsed"] == len(led["alloc"]), (rows, led["alloc"])
    assert r["ipam"]["refused"] is None and r["changed"] is False, r      # the ledger held the pool already
    assert r["offer"] == "/56 /60 default /56", r["offer"]


def c_fabric_pools(lab):
    """the HybridFabric job (its role defaults, no playbook variable) keeps the realization's pool in a new fabric's ledger before any node work"""
    name = "fab2"
    # a second fabric, so this case touches neither the first fabric's ledger nor the tenant profiles of the cases
    # above (the Entity job reads the fabrics of the platform namespace); its node is a documentation address
    lab.create(obj("HybridFabric", name, PNS, {
        "domainAsn": 64501, "overlayMtu": 9000, "vniPool": {"start": 52000, "end": 52999},
        "l2vniPool": {"start": 152000, "end": 152999},
        "borderGateway": {"credentialsRef": {"vaultPath": "fabric/bgw2"},
                          "nodes": [{"name": "n2", "address": "192.0.2.2"}]}}))
    kv = MockKv()
    try:
        p = lab.job("hybridfabric_provision", "HybridFabric", name, PNS,
                    extra_env={"VAULT_ADDR": kv.url, "VAULT_TOKEN": "harness"})
        out = p.stdout
        # the job got past the pool step (it fails later, at the node: unreachable, not rescued)
        assert "is undefined" not in out, out[-1500:]
        assert p.returncode != 0 and "n2" in out and "unreachable=1" in out, out[-1500:]
        # the lease is held: every planning step after the pools ran
        lease, _, _ = lab.kube.json(["get", "lease", f"fabric-config-{name}", "-n", PNS])
        assert (lease or {}).get("spec", {}).get("holderIdentity", "").startswith(f"HybridFabric/{name} job"), lease
        # the ledger the job created carries the realization's pool, untouched (no block given out)
        cm, _, _ = lab.kube.json(["get", "configmap", f"fabric-ledger-{name}", "-n", PNS])
        led = json.loads(cm["data"]["ledger.json"])
        pool = led["ipamPools"]["test-v6"]
        assert pool["family"] == 6 and pool["prefixes"] == [POOL], pool
        assert pool["blockLengths"] == [56, 60] and pool["defaultBlock"] == 56, pool
        assert pool["alloc"] == {} and pool["tenants"] == {}, pool
        # the first fabric's ledger is not the one written
        assert "ipamPools" in lab.ledger() and lab.ledger()["fabric"] == FABRIC
    finally:
        kv.stop()
        for args in (["delete", "hybridfabric", name, "-n", PNS], ["delete", "configmap", f"fabric-ledger-{name}", "-n", PNS],
                     ["delete", "lease", f"fabric-config-{name}", "-n", PNS]):
            lab.kube.run(args + ["--ignore-not-found"])


CASES = [c_tenant_core, c_platform_dual, c_pinned, c_allowance, c_exhausted, c_size_not_offered, c_reserved,
         c_overlap_tenant, c_v6only_tenant, c_placement_mode_none, c_mode_in_use, c_release_wakes, c_leave_platform,
         c_profile_offer, c_fabric_pools_step, c_fabric_pools]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--assets", default=os.environ.get("KUBEBUILDER_ASSETS", ""))
    ap.add_argument("--crds", default=str(ROOT / "operator" / "primary" / "helm" / "templates" / "crds.yaml"))
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    if not shutil.which("ansible-playbook"):
        raise SystemExit("network_jobs_envtest: ansible-playbook not found")
    replay.KUBECTL = str(Path(args.assets) / "kubectl")
    crds = replay.load_crds(args.crds)
    env = replay.Envtest(args.assets, args.keep)
    passed = 0
    try:
        env.start()
        kube = replay.Kube(env.kubeconfig)
        kube.run(["create", "-f", "-"], body={"apiVersion": "v1", "kind": "List", "items": list(crds.values())}, check=True)
        replay.wait_established(kube, list(crds))
        kc = yaml.safe_load(Path(env.kubeconfig).read_text())
        lab = Lab(kube, kc["clusters"][0]["cluster"]["server"], kc["users"][0]["user"]["token"], args.verbose)
        lab.setup()
        for c in CASES:
            try:
                c(lab)
                passed += 1
                print(f"PASS {c.__name__:<22} {c.__doc__}")
            except (AssertionError, RuntimeError, KeyError, TypeError) as e:
                print(f"FAIL {c.__name__:<22} {c.__doc__}\n     {type(e).__name__}: {str(e)[:600]}")
        shutil.rmtree(lab.tmp, ignore_errors=True)
    finally:
        env.stop()
    print(f"\n{passed}/{len(CASES)} cases pass")
    return 0 if passed == len(CASES) else 1


if __name__ == "__main__":
    sys.exit(main())
