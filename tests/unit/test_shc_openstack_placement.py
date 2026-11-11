"""placement-network of eda/common/files/shc_openstack.py against a fake connection that records every call: a
dual-stack placement (one subnet per prefix with ip_version from the prefix, the RA and address mode on IPv6 subnets
only, resolvers per family, advertise_host on the first router interface only), a re-run that creates nothing, the
attach fallback (409 conflict; a refused plain attach raises), the resolver update of an existing subnet and the teardown
order. The tenant project's uses of the network (spec.use): the share (access_as_shared) and the router attachments
(access_as_external for that project only), created, verified, pruned and removed, refused while in use; a placement
without spec.use sends Neutron exactly the writes of the release before (golden list); the teardown blocked by router
gateways and floating IPs. Prefixes are documentation space only."""
import os
import sys
import types
import ipaddress
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FILES = os.path.join(ROOT, "eda", "common", "files")
sys.path.insert(0, FILES)
try:
    import openstack  # noqa: F401
except ImportError:  # stub the SDK so the module imports; placement-network never calls it directly
    stub = types.ModuleType("openstack")
    stub.exceptions = types.SimpleNamespace(ForbiddenException=Exception, NotFoundException=Exception)
    stub.connect = None
    sys.modules.setdefault("openstack", stub)
import shc_openstack as S  # noqa: E402

V4, V6 = "10.65.1.0/24", "2001:db8:5e00:100::/64"
R4, R6 = "192.0.2.53", "2001:db8::53"
MODE = "dhcpv6-stateful"
TENANT = "proj-tenant"
IFACE = "network:router_interface"


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class Resp:
    def __init__(self, status_code, body=None, text=""):
        self.status_code, self._body, self.text = status_code, body or {}, text

    def json(self):
        return self._body


class FakeNetwork:
    """The subset of the SDK's network proxy the helper uses. Objects live in lists; every mutating call is
    appended to `calls` as (operation, detail). `attach` may be set to a function(body, router) -> Resp or None
    to refuse an add_router_interface the way the evpn plugin does."""

    def __init__(self, conn):
        self.conn = conn
        self.nets, self.subs, self.rtrs, self.plist, self.rbacs, self.fips = [], [], [], [], [], []
        self.attach = None
        self.n = 0

    def _id(self, kind):
        self.n += 1
        return f"{kind}-{self.n}"

    def log(self, op, detail=None):
        self.conn.calls.append((op, detail))

    # reads
    def networks(self, **f):
        return [n for n in self.nets if all(getattr(n, k) == v for k, v in f.items())]

    def routers(self, **f):
        return [r for r in self.rtrs if all(getattr(r, k) == v for k, v in f.items())]

    def subnets(self, **f):
        return [s for s in self.subs if all(getattr(s, k) == v for k, v in f.items())]

    def ports(self, **f):
        return [p for p in self.plist if all(getattr(p, k) == v for k, v in f.items())]

    def rbac_policies(self, **f):
        return [r for r in self.rbacs if all(getattr(r, k) == v for k, v in f.items())]

    def ips(self, **f):
        return [i for i in self.fips if all(getattr(i, k) == v for k, v in f.items())]

    def get_router(self, rid):
        return next(r for r in self.rtrs if r.id == rid)

    def get_network(self, nid):
        # router:external as Neutron computes it: an access_as_external policy of the network exists
        n = next(n for n in self.nets if n.id == nid)
        n.is_router_external = any(r.object_id == nid and r.action == "access_as_external" for r in self.rbacs)
        return n

    def get(self, path):
        rid = path.split("/")[2]
        return Resp(200, {"router": {"id": rid, "evpn_vni": self.get_router(rid).evpn_vni}})

    # network
    def create_network(self, **kw):
        n = Obj(id=self._id("net"), tags=[], **kw)
        self.nets.append(n)
        self.log("create_network", kw["name"])
        return n

    def update_network(self, net, **kw):
        net.__dict__.update(kw)
        self.log("update_network", kw)
        return net

    def set_tags(self, net, tags):
        net.tags = tags
        self.log("set_tags", tags)

    def delete_network(self, net):
        self.nets.remove(net)
        self.log("delete_network", net.id)

    # subnets
    def create_subnet(self, **kw):
        # the SDK returns every attribute, unset ones as None (the helper reads ipv6_address_mode back)
        s = Obj(id=self._id("sn"), **{"dns_nameservers": [], "ipv6_address_mode": None, "ipv6_ra_mode": None, **kw})
        self.subs.append(s)
        self.log("create_subnet", kw)
        return s

    def update_subnet(self, sn, **kw):
        sn.__dict__.update(kw)
        self.log("update_subnet", (sn.cidr, kw))
        return sn

    def delete_subnet(self, sn):
        self.subs.remove(sn)
        self.log("delete_subnet", sn.cidr)

    # routers and interfaces
    def post(self, path, json):
        assert path == "/routers"
        r = Obj(id=self._id("rtr"), **json["router"])
        self.rtrs.append(r)
        self.log("create_router", json["router"]["name"])
        return Resp(201, {"router": {"id": r.id}})

    def put(self, path, json):
        rid = path.split("/")[2]
        router = self.get_router(rid)
        self.log("add_router_interface", dict(json))
        refused = self.attach(json, router) if self.attach else None
        if refused is not None:
            return refused
        sn = next(s for s in self.subs if s.id == json["subnet_id"])
        self.plist.append(Obj(id=self._id("port"), device_id=rid, device_owner=IFACE, network_id=sn.network_id,
                              project_id=router.project_id, name=f"if-{sn.cidr}", fixed_ips=[{"subnet_id": sn.id}],
                              advertise_host=bool(json.get("advertise_host"))))
        return Resp(200, {"port_id": self.plist[-1].id})

    def remove_interface_from_router(self, router, subnet_id):
        for p in list(self.plist):
            if p.device_id == router.id and any(ip["subnet_id"] == subnet_id for ip in p.fixed_ips):
                self.plist.remove(p)
        self.log("remove_interface_from_router", subnet_id)

    def delete_router(self, router):
        self.rtrs.remove(router)
        self.log("delete_router", router.id)

    # shares
    def create_rbac_policy(self, **kw):
        r = Obj(id=self._id("rbac"), **kw)
        self.rbacs.append(r)
        self.log("create_rbac_policy", dict(kw))
        return r

    def delete_rbac_policy(self, r):
        self.rbacs.remove(r)
        self.log("delete_rbac_policy", r.id)


class FakeIdentity:
    def find_domain(self, name, ignore_missing=True):
        return Obj(id="dom-1", name=name)

    def find_project(self, name, domain_id=None):
        return Obj(id="proj-platform", name=name)


class FakeConn:
    def __init__(self):
        self.calls = []
        self.network = FakeNetwork(self)
        self.identity = FakeIdentity()


def request(**over):
    req = {"action": "placement-network", "clouds_yaml": "/dev/null", "domain": "shc", "platformProject": "hybrid-fabric",
           "name": "hn51001-site", "tag": "shc.np/51001", "mtu": 8952, "prefixes": [V4, V6], "l3Vni": 51001,
           "shareProjectId": TENANT, "addressMode": MODE, "resolvers": [R4, R6]}
    req.update(over)
    return req


def run(conn, **over):
    acct = mock.Mock()
    acct.own_scope.return_value = conn
    with mock.patch.object(S, "Account", return_value=acct):
        return S.placement_network(request(**over))


def attaches(conn):
    return [d for op, d in conn.calls if op == "add_router_interface"]


def ops(conn, *names):
    return [op for op, _ in conn.calls if op in names]


class DualStackCreate(unittest.TestCase):
    def test_one_subnet_per_prefix_with_its_family(self):
        conn = FakeConn()
        out = run(conn)
        created = [d for op, d in conn.calls if op == "create_subnet"]
        self.assertEqual([c["ip_version"] for c in created], [4, 6])
        self.assertEqual([c["cidr"] for c in created], [V4, V6])
        self.assertEqual([c["name"] for c in created], ["hn51001-site-0", "hn51001-site-1"])
        self.assertTrue(all(c["enable_dhcp"] for c in created))
        self.assertEqual(sorted(out["subnets"]), sorted([V4, V6]))
        self.assertEqual(out["created"], ["network", f"subnet {V4}", f"subnet {V6}", "router", f"interface {V4}",
                                          f"interface {V6}", "share"])
        self.assertEqual(out["changed"], [])

    def test_address_mode_on_ipv6_subnets_only(self):
        conn = FakeConn()
        out = run(conn)
        v4, v6 = [d for op, d in conn.calls if op == "create_subnet"]
        self.assertNotIn("ipv6_ra_mode", v4)
        self.assertNotIn("ipv6_address_mode", v4)
        self.assertEqual((v6["ipv6_ra_mode"], v6["ipv6_address_mode"]), (MODE, MODE))
        self.assertEqual(out["addressMode"], MODE)

    def test_empty_address_mode_leaves_the_default(self):
        conn = FakeConn()
        out = run(conn, addressMode="")
        v6 = [d for op, d in conn.calls if op == "create_subnet"][1]
        self.assertNotIn("ipv6_ra_mode", v6)
        self.assertNotIn("ipv6_address_mode", v6)
        self.assertEqual(out["addressMode"], "")

    def test_ipv4_only_reports_no_address_mode(self):
        conn = FakeConn()
        out = run(conn, prefixes=[V4])
        self.assertEqual(out["addressMode"], "")
        self.assertEqual(list(out["subnets"]), [V4])

    def test_resolvers_per_family(self):
        conn = FakeConn()
        run(conn, resolvers=[R4, "192.0.2.54", R6])
        v4, v6 = [d for op, d in conn.calls if op == "create_subnet"]
        self.assertEqual(v4["dns_nameservers"], [R4, "192.0.2.54"])
        self.assertEqual(v6["dns_nameservers"], [R6])

    def test_no_resolvers_means_empty_lists(self):
        conn = FakeConn()
        run(conn, resolvers=[])
        self.assertEqual([d["dns_nameservers"] for op, d in conn.calls if op == "create_subnet"], [[], []])

    def test_advertise_host_on_the_first_interface_only(self):
        conn = FakeConn()
        run(conn)
        body = attaches(conn)
        self.assertEqual(len(body), 2)
        self.assertEqual(body[0].get("advertise_host"), True)
        self.assertNotIn("advertise_host", body[1])
        self.assertEqual(sum(1 for b in body if b.get("advertise_host")), 1)


class Rerun(unittest.TestCase):
    def test_same_request_creates_and_changes_nothing(self):
        conn = FakeConn()
        first = run(conn)
        n = len(conn.calls)
        again = run(conn)
        self.assertEqual(again["created"], [])
        self.assertEqual(again["changed"], [])
        self.assertEqual(again["subnets"], first["subnets"])
        self.assertEqual(again["addressMode"], MODE)
        self.assertEqual(conn.calls[n:], [])

    def test_resolver_order_is_not_a_change(self):
        conn = FakeConn()
        run(conn, resolvers=[R4, "192.0.2.54", R6])
        n = len(conn.calls)
        again = run(conn, resolvers=["192.0.2.54", R6, R4])
        self.assertEqual(again["changed"], [])
        self.assertEqual(conn.calls[n:], [])

    def test_ipv6_added_to_an_ipv4_placement_attaches_plainly(self):
        conn = FakeConn()
        run(conn, prefixes=[V4])
        n = len(conn.calls)
        out = run(conn)
        self.assertEqual(out["created"], [f"subnet {V6}", f"interface {V6}"])
        new = [d for op, d in conn.calls[n:] if op == "add_router_interface"]
        self.assertEqual(new, [{"subnet_id": conn.network.subs[1].id}])
        self.assertEqual(sum(1 for p in conn.network.plist if p.advertise_host), 1)

    def test_recreated_router_advertises_its_first_interface_again(self):
        conn = FakeConn()
        run(conn)
        n = len(conn.calls)
        out = run(conn, recreateRouter=True)
        self.assertIn("router recreated", out["changed"])
        self.assertEqual(ops(conn, "remove_interface_from_router", "delete_router", "create_router")[-4:],
                         ["remove_interface_from_router", "remove_interface_from_router", "delete_router", "create_router"])
        new = [d for op, d in conn.calls[n:] if op == "add_router_interface"]
        self.assertEqual([b.get("advertise_host", False) for b in new], [True, False])


class AttachFallback(unittest.TestCase):
    def test_409_conflict_retries_plainly(self):
        conn = FakeConn()
        seen = []

        def attach(body, router):
            seen.append(dict(body))
            if body.get("advertise_host") and len(seen) == 1:
                return Resp(409, text='{"NeutronError": {"type": "RouterInterfaceAttachmentConflict", '
                                      '"message": "network already has an advertised interface"}}')
            return None
        conn.network.attach = attach
        out = run(conn)
        self.assertEqual([b.get("advertise_host", False) for b in attaches(conn)], [True, False, False])
        self.assertEqual(out["created"][-3:], [f"interface {V4}", f"interface {V6}", "share"])
        self.assertEqual(sum(1 for p in conn.network.plist if p.advertise_host), 0)

    def test_refused_plain_attach_raises(self):
        # a plain attach only happens on a router that already holds the advertised interface: nothing to retry
        conn = FakeConn()

        def attach(body, router):
            if "advertise_host" not in body:
                return Resp(400, text="advertise_host is required on this router")
            return None
        conn.network.attach = attach
        with self.assertRaises(RuntimeError) as cm:
            run(conn)
        self.assertIn("HTTP 400", str(cm.exception))
        self.assertEqual([b.get("advertise_host", False) for b in attaches(conn)], [True, False])

    def test_other_errors_raise(self):
        conn = FakeConn()
        conn.network.attach = lambda body, router: Resp(500, text="boom") if body.get("advertise_host") else None
        with self.assertRaises(RuntimeError) as cm:
            run(conn)
        self.assertIn("HTTP 500", str(cm.exception))
        self.assertEqual(len(attaches(conn)), 1)

    def test_second_attempt_failing_raises(self):
        conn = FakeConn()
        conn.network.attach = lambda body, router: Resp(409, text="RouterInterfaceAttachmentConflict") \
            if body.get("advertise_host") else Resp(404, text="no such subnet")
        with self.assertRaises(RuntimeError) as cm:
            run(conn)
        self.assertIn("HTTP 404", str(cm.exception))
        self.assertEqual(len(attaches(conn)), 2)


class ResolverUpdate(unittest.TestCase):
    def test_existing_subnets_without_resolvers_are_updated_per_family(self):
        conn = FakeConn()
        run(conn, resolvers=[])
        n = len(conn.calls)
        out = run(conn)
        self.assertEqual(out["created"], [])
        self.assertEqual(out["changed"], [f"subnet {V4} resolvers", f"subnet {V6} resolvers"])
        updates = [d for op, d in conn.calls[n:] if op == "update_subnet"]
        self.assertEqual(updates, [(V4, {"dns_nameservers": [R4]}), (V6, {"dns_nameservers": [R6]})])
        self.assertEqual([op for op, _ in conn.calls[n:] if op.startswith("create")], [])

    def test_only_the_differing_family_is_updated(self):
        conn = FakeConn()
        run(conn)
        n = len(conn.calls)
        out = run(conn, resolvers=[R4, "2001:db8::54"])
        self.assertEqual(out["changed"], [f"subnet {V6} resolvers"])
        self.assertEqual([d for op, d in conn.calls[n:] if op == "update_subnet"],
                         [(V6, {"dns_nameservers": ["2001:db8::54"]})])

    def test_removed_resolvers_clear_the_list(self):
        conn = FakeConn()
        run(conn)
        out = run(conn, resolvers=[])
        self.assertEqual(out["changed"], [f"subnet {V4} resolvers", f"subnet {V6} resolvers"])
        self.assertEqual([s.dns_nameservers for s in conn.network.subs], [[], []])


class Teardown(unittest.TestCase):
    def test_interfaces_router_subnets_network_in_that_order(self):
        conn = FakeConn()
        run(conn)
        n = len(conn.calls)
        out = run(conn, state="absent")
        self.assertTrue(out["found"])
        self.assertNotIn("blocked", out)
        self.assertEqual(ops(conn, "delete_rbac_policy", "remove_interface_from_router", "delete_router",
                             "delete_subnet", "delete_network")[-7:],
                         ["delete_rbac_policy", "remove_interface_from_router", "remove_interface_from_router",
                          "delete_router", "delete_subnet", "delete_subnet", "delete_network"])
        removed = [d for op, d in conn.calls[n:] if op == "remove_interface_from_router"]
        self.assertEqual(sorted(removed), ["sn-2", "sn-3"])
        self.assertEqual(out["deleted"][0][:5], "rbac ")
        self.assertEqual(out["deleted"][1][:7], "router ")
        self.assertEqual(out["deleted"][2:4], [f"subnet {V4}", f"subnet {V6}"])
        self.assertEqual(out["deleted"][4][:8], "network ")
        self.assertEqual((conn.network.nets, conn.network.subs, conn.network.rtrs, conn.network.plist), ([], [], [], []))

    def test_shared_ipv6_router_port_loses_every_subnet(self):
        # Neutron puts every IPv6 subnet of a network on one router port; each subnet is removed by id
        conn = FakeConn()
        run(conn, prefixes=[V4, V6, "2001:db8:5e00:101::/64"])
        a, b = [p for p in conn.network.plist if ":" in p.name]
        a.fixed_ips += b.fixed_ips
        conn.network.plist.remove(b)
        run(conn, state="absent")
        self.assertEqual(len([1 for op, _ in conn.calls if op == "remove_interface_from_router"]), 3)
        self.assertEqual(conn.network.plist, [])

    def test_tenant_ports_block_the_teardown(self):
        conn = FakeConn()
        run(conn)
        net = conn.network.nets[0]
        conn.network.plist.append(Obj(id="port-vm", device_id="vm-1", device_owner="compute:nova", network_id=net.id,
                                      project_id=TENANT, name="vm-nic", fixed_ips=[]))
        n = len(conn.calls)
        out = run(conn, state="absent")
        self.assertEqual(out["blocked"], ["port of server vm-1"])
        self.assertEqual(out["deleted"], [])
        self.assertEqual(conn.calls[n:], [])

    def test_absent_without_objects(self):
        conn = FakeConn()
        out = run(conn, state="absent")
        self.assertEqual(out, {"name": "hn51001-site", "found": False, "deleted": []})



class ReviewFixes(unittest.TestCase):
    def test_long_hand_resolver_is_not_a_change(self):
        conn = FakeConn()
        run(conn)
        n = len(conn.calls)
        out = run(conn, resolvers=[R4, ipaddress.ip_address(R6).exploded])
        self.assertEqual(out["changed"], [])
        self.assertEqual([op for op, _ in conn.calls[n:] if op == "update_subnet"], [])

    def test_share_is_owned_by_the_platform_project(self):
        conn = FakeConn()
        out = run(conn)
        self.assertEqual([r.project_id for r in conn.network.rbacs], [out["projectId"]])

    def test_gateway_port_is_not_an_interface(self):
        conn = FakeConn()
        run(conn, prefixes=[V4])
        router = conn.network.rtrs[0]
        conn.network.plist = [p for p in conn.network.plist if p.device_id != router.id]
        conn.network.plist.append(Obj(id="port-gw", device_id=router.id, device_owner="network:router_gateway",
                                      network_id="ext-net", project_id="", name="gw", fixed_ips=[{"subnet_id": "ext-subnet"}],
                                      advertise_host=False))
        n = len(conn.calls)
        run(conn)
        new = [d for op, d in conn.calls[n:] if op == "add_router_interface"]
        self.assertEqual([b.get("advertise_host", False) for b in new], [True, False])


# ---- the tenant project's uses of the network (spec.use) ----
OTHER = "proj-other"
GW, FIP = "network:router_gateway", "network:floatingip"

# the Neutron writes of placement-network as released before spec.use (shc-lab 669b781a, this fake): a first run, then
# a run that prunes a share to another project
GOLDEN_CREATE = [
    ("create_network", "hn51001-site"),
    ("set_tags", ["shc.np/51001"]),
    ("create_subnet", {"cidr": V4, "dns_nameservers": [R4], "enable_dhcp": True, "ip_version": 4,
                       "name": "hn51001-site-0", "network_id": "net-1", "project_id": "proj-platform"}),
    ("create_subnet", {"cidr": V6, "dns_nameservers": [R6], "enable_dhcp": True, "ip_version": 6,
                       "ipv6_address_mode": MODE, "ipv6_ra_mode": MODE, "name": "hn51001-site-1",
                       "network_id": "net-1", "project_id": "proj-platform"}),
    ("create_router", "hn51001-site"),
    ("add_router_interface", {"advertise_host": True, "subnet_id": "sn-2"}),
    ("add_router_interface", {"subnet_id": "sn-3"}),
    ("create_rbac_policy", {"action": "access_as_shared", "object_id": "net-1", "object_type": "network",
                            "project_id": "proj-platform", "target_project_id": TENANT}),
]
GOLDEN_PRUNE = [("delete_rbac_policy", "rbac-foreign")]


def policy(conn, pid, action, target):
    conn.network.rbacs.append(Obj(id=pid, object_id=conn.network.nets[0].id, object_type="network", action=action,
                                  target_project_id=target, project_id="proj-platform"))


def golden_run(conn, **over):
    run(conn, **over)
    policy(conn, "rbac-foreign", "access_as_shared", OTHER)
    return run(conn, **over)


def writes(conn, start=0):
    return conn.calls[start:]


def tenant_router(conn, rid="rtr-t", name="r1", project=TENANT, network=None):
    conn.network.rtrs.append(Obj(id=rid, name=name, project_id=project,
                                 external_gateway_info={"network_id": network or conn.network.nets[0].id}))


def tenant_fip(conn, fid="fip-t", address="192.0.2.15", project=TENANT):
    conn.network.fips.append(Obj(id=fid, floating_ip_address=address, floating_network_id=conn.network.nets[0].id,
                                 project_id=project))


class DefaultsUnchanged(unittest.TestCase):
    """A placement that does not set spec.use: the same Neutron writes as before."""

    def test_without_use_the_writes_are_the_released_ones(self):
        conn = FakeConn()
        out = golden_run(conn)
        self.assertEqual(writes(conn), GOLDEN_CREATE + GOLDEN_PRUNE)
        self.assertEqual(out["changed"], [f"share to {OTHER} removed"])

    def test_explicit_defaults_are_the_same(self):
        for use in ({"virtualMachines": True, "routerAttachments": False}, {"virtualMachines": "True",
                    "routerAttachments": "false"}, {}):
            conn = FakeConn()
            golden_run(conn, use=use)
            self.assertEqual(writes(conn), GOLDEN_CREATE + GOLDEN_PRUNE, use)

    def test_status_of_the_defaults(self):
        conn = FakeConn()
        out = run(conn)
        self.assertEqual(out["use"]["virtualMachines"], {"state": "ready", "policyId": out["shareId"], "inUse": []})
        self.assertEqual(out["use"]["routerAttachments"], {"state": "off", "policyId": "", "inUse": []})
        self.assertEqual((out["externalId"], out["routerExternalForProject"], out["inUse"], out["pruneInUse"]),
                         ("", False, [], []))

    def test_no_tenant_project_no_uses(self):
        conn = FakeConn()
        out = run(conn, shareProjectId="", use={"routerAttachments": True})
        self.assertNotIn("use", out)
        self.assertEqual(conn.network.rbacs, [])


class RouterAttachments(unittest.TestCase):
    def test_external_for_the_tenant_project_only(self):
        conn = FakeConn()
        out = run(conn, use={"routerAttachments": True})
        made = [d for op, d in conn.calls if op == "create_rbac_policy"]
        self.assertEqual(made[-1], {"action": "access_as_external", "object_id": "net-1", "object_type": "network",
                                    "project_id": "proj-platform", "target_project_id": TENANT})
        self.assertNotIn("*", [d["target_project_id"] for d in made])
        # the network's own flag is never written
        self.assertEqual([d for op, d in conn.calls if op == "update_network"], [])
        self.assertEqual(out["created"][-2:], ["share", "external"])
        self.assertEqual(out["use"]["routerAttachments"]["state"], "ready")
        self.assertEqual(out["externalId"], out["use"]["routerAttachments"]["policyId"])
        self.assertTrue(out["routerExternalForProject"])

    def test_rerun_writes_nothing(self):
        conn = FakeConn()
        first = run(conn, use={"routerAttachments": True})
        n = len(conn.calls)
        again = run(conn, use={"routerAttachments": True})
        self.assertEqual(writes(conn, n), [])
        self.assertEqual((again["created"], again["changed"]), ([], []))
        self.assertEqual(again["externalId"], first["externalId"])

    def test_router_attachments_without_virtual_machines(self):
        conn = FakeConn()
        out = run(conn, use={"virtualMachines": False, "routerAttachments": True})
        self.assertEqual([r.action for r in conn.network.rbacs], ["access_as_external"])
        self.assertEqual(out["shareId"], "")
        self.assertEqual(out["use"]["virtualMachines"]["state"], "off")
        self.assertEqual(out["use"]["routerAttachments"]["state"], "ready")

    def test_turned_off_unused_is_removed(self):
        conn = FakeConn()
        run(conn, use={"routerAttachments": True})
        n = len(conn.calls)
        out = run(conn)
        self.assertEqual(ops(conn, "delete_rbac_policy")[-1:], ["delete_rbac_policy"])
        self.assertEqual(len(writes(conn, n)), 1)
        self.assertEqual(out["changed"], ["external removed"])
        self.assertEqual(out["use"]["routerAttachments"], {"state": "off", "policyId": "", "inUse": []})
        self.assertFalse(out["routerExternalForProject"])
        self.assertEqual([r.action for r in conn.network.rbacs], ["access_as_shared"])

    def test_turned_off_while_used_waits_and_names_the_users(self):
        conn = FakeConn()
        first = run(conn, use={"routerAttachments": True})
        tenant_router(conn)
        tenant_fip(conn)
        tenant_router(conn, rid="rtr-x", name="elsewhere", network="net-other")  # gateway on another network
        tenant_fip(conn, fid="fip-o", address="192.0.2.16", project=OTHER)        # another project's
        n = len(conn.calls)
        out = run(conn)
        self.assertEqual(writes(conn, n), [])
        ra = out["use"]["routerAttachments"]
        self.assertEqual(ra["state"], "removing")
        self.assertEqual(ra["inUse"], ["router gateway of router r1", "floating IP 192.0.2.15"])
        self.assertEqual(out["inUse"], ra["inUse"])
        self.assertEqual(out["externalId"], first["externalId"])
        self.assertTrue(out["routerExternalForProject"])
        # once the tenant released them, the next run removes the policy
        conn.network.rtrs = [r for r in conn.network.rtrs if r.id not in ("rtr-t",)]
        conn.network.fips = [f for f in conn.network.fips if f.id != "fip-t"]
        out = run(conn)
        self.assertEqual(out["use"]["routerAttachments"]["state"], "off")
        self.assertEqual(out["changed"], ["external removed"])

    def test_wildcard_external_is_refused_before_any_write(self):
        conn = FakeConn()
        run(conn)
        policy(conn, "rbac-wild", "access_as_external", "*")
        n = len(conn.calls)
        with self.assertRaises(RuntimeError) as cm:
            run(conn, use={"routerAttachments": True})
        self.assertIn("external for every project", str(cm.exception))
        self.assertEqual(writes(conn, n), [])
        self.assertIn("rbac-wild", [r.id for r in conn.network.rbacs])

    def test_other_projects_external_policies_are_pruned_unless_used(self):
        conn = FakeConn()
        run(conn, use={"routerAttachments": True})
        policy(conn, "rbac-ext-a", "access_as_external", "proj-a")
        policy(conn, "rbac-ext-b", "access_as_external", "proj-b")
        tenant_fip(conn, fid="fip-b", address="192.0.2.17", project="proj-b")
        out = run(conn, use={"routerAttachments": True})
        ids = [r.id for r in conn.network.rbacs]
        self.assertNotIn("rbac-ext-a", ids)
        self.assertIn("rbac-ext-b", ids)
        self.assertEqual(out["changed"], ["external to proj-a removed"])
        self.assertEqual(out["pruneInUse"], ["project proj-b: floating IP 192.0.2.17"])

    def test_a_failure_after_the_share_converges_on_the_next_run(self):
        conn = FakeConn()
        real = conn.network.create_rbac_policy

        def refuse_external(**kw):
            if kw["action"] == "access_as_external":
                raise RuntimeError("quota exceeded")
            return real(**kw)
        conn.network.create_rbac_policy = refuse_external
        with self.assertRaises(RuntimeError):
            run(conn, use={"routerAttachments": True})
        conn.network.create_rbac_policy = real
        out = run(conn, use={"routerAttachments": True})
        self.assertEqual(out["created"], ["external"])
        self.assertEqual(sorted(r.action for r in conn.network.rbacs), ["access_as_external", "access_as_shared"])


class VirtualMachinesOff(unittest.TestCase):
    def test_no_share_at_create(self):
        conn = FakeConn()
        out = run(conn, use={"virtualMachines": False, "routerAttachments": True})
        self.assertNotIn("share", out["created"])
        self.assertEqual([d["action"] for op, d in conn.calls if op == "create_rbac_policy"], ["access_as_external"])

    def test_turned_off_while_the_tenant_has_ports_waits(self):
        conn = FakeConn()
        first = run(conn)
        net = conn.network.nets[0]
        conn.network.plist.append(Obj(id="port-vm", device_id="vm-1", device_owner="compute:nova", network_id=net.id,
                                      project_id=TENANT, name="vm-nic", fixed_ips=[]))
        n = len(conn.calls)
        out = run(conn, use={"virtualMachines": False, "routerAttachments": True})
        vm = out["use"]["virtualMachines"]
        self.assertEqual((vm["state"], vm["inUse"]), ("removing", ["port of server vm-1"]))
        self.assertEqual(out["shareId"], first["shareId"])
        self.assertEqual([op for op, _ in writes(conn, n)], ["create_rbac_policy"])  # only the external policy
        conn.network.plist = [p for p in conn.network.plist if p.id != "port-vm"]
        out = run(conn, use={"virtualMachines": False, "routerAttachments": True})
        self.assertEqual(out["use"]["virtualMachines"]["state"], "off")
        self.assertIn("share removed", out["changed"])
        self.assertEqual(out["shareId"], "")

    def test_tenant_router_gateways_do_not_hold_the_share(self):
        conn = FakeConn()
        run(conn, use={"routerAttachments": True})
        tenant_router(conn)
        net = conn.network.nets[0]
        conn.network.plist.append(Obj(id="port-gw", device_id="rtr-t", device_owner=GW, network_id=net.id,
                                      project_id="", name="", fixed_ips=[{"ip_address": "192.0.2.20"}]))
        out = run(conn, use={"virtualMachines": False, "routerAttachments": True})
        self.assertEqual(out["use"]["virtualMachines"]["state"], "off")


class TeardownBusy(unittest.TestCase):
    def setUp(self):
        self.conn = FakeConn()
        run(self.conn, use={"routerAttachments": True})
        self.net = self.conn.network.nets[0]

    def port(self, pid, owner, project, device="", ips=None, name=""):
        self.conn.network.plist.append(Obj(id=pid, device_id=device, device_owner=owner, network_id=self.net.id,
                                           project_id=project, name=name, fixed_ips=ips or []))

    def test_gateways_and_floating_ips_of_any_project_block(self):
        tenant_router(self.conn)
        self.port("p-gw", GW, "", device="rtr-t")
        self.port("p-fip", FIP, "", device="fip-t", ips=[{"subnet_id": "sn-2", "ip_address": "192.0.2.15"}])
        n = len(self.conn.calls)
        out = run(self.conn, state="absent")
        self.assertEqual(out["blocked"], ["router gateway of router r1", "floating IP 192.0.2.15"])
        self.assertEqual(writes(self.conn, n), [])

    def test_tenant_router_interface_blocks(self):
        self.port("p-if", IFACE, TENANT, device="rtr-t", name="tif")
        out = run(self.conn, state="absent")
        self.assertEqual(out["blocked"], ["port tif network:router_interface"])

    def test_the_platforms_own_ports_do_not_block(self):
        self.port("p-dhcp", "network:dhcp", "proj-platform")
        self.port("p-meta", "network:distributed", "proj-platform")
        self.port("p-dhcp-x", "network:dhcp", "")  # deleted with the network whoever owns it
        out = run(self.conn, state="absent")
        self.assertNotIn("blocked", out)
        self.assertEqual(sum(1 for d in out["deleted"] if d.startswith("rbac ")), 2)
        self.assertEqual(self.conn.network.rbacs, [])


if __name__ == "__main__":
    unittest.main()
